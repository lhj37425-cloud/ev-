"""GEO brand facts and competitors for the archived brand page.

Implements the facts/competitors contract the frontend already calls:
manual entry, bulk review, AI extraction from the tenant knowledge base,
competitor add/update/delete, AI suggestions and share of voice computed
from the stored simulated answers.

No external data source is required. The two AI actions reuse the configured
text model through ``commander.generate`` and are free for now (same policy as
GEO question generation). Every write is tenant scoped and audited.
"""

from datetime import datetime, timedelta, timezone
import json
import re

import commander
import knowledge
import payments
import tenancy


PROJECT = re.compile(r"/api/geo/projects/([0-9]{1,16})(?:/(.*))?$")
FACT_LIMIT = 100
COMPETITOR_LIMIT = 10
SOV_DEFAULT_DAYS = 30

FACT_KEYS = [
    {"key": "founded", "name": "成立时间"},
    {"key": "headquarters", "name": "总部 / 所在地"},
    {"key": "business_scope", "name": "主营业务"},
    {"key": "main_products", "name": "主营产品"},
    {"key": "target_customers", "name": "目标客户"},
    {"key": "price_range", "name": "价格区间"},
    {"key": "service_area", "name": "服务区域"},
    {"key": "certifications", "name": "资质认证"},
    {"key": "awards", "name": "荣誉奖项"},
    {"key": "contact", "name": "联系方式"},
    {"key": "other", "name": "其他"},
]
FACT_KEY_MAP = {item["key"]: item for item in FACT_KEYS}

# Latin tokens that are model/platform names rather than comparable brands.
OTHER_STOPWORDS = {
    "ai", "api", "app", "b2b", "b2c", "crm", "erp", "geo", "ios", "android",
    "seo", "saas", "llm", "pc", "openai", "chatgpt", "gpt", "gemini", "claude",
    "deepseek", "kimi", "perplexity", "google", "microsoft", "apple", "meta",
}


def canonical(path):
    if path.startswith("/agent-api"):
        path = path[len("/agent-api"):]
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def can_read(path):
    path = canonical(path)
    match = PROJECT.fullmatch(path)
    return bool(match and (match[2] or "").strip("/") in {"facts", "competitors", "competitors/sov"})


def can_write(path):
    path = canonical(path)
    match = PROJECT.fullmatch(path)
    if not match:
        return False
    suffix = (match[2] or "").strip("/")
    return suffix in {"facts", "facts/review", "facts/extract", "competitors", "competitors/suggest"} \
        or bool(re.fullmatch(r"(facts|competitors)/[0-9]{1,16}", suffix))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        fact_key TEXT NOT NULL DEFAULT 'other', label TEXT NOT NULL, value TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'confirmed', source_name TEXT NOT NULL DEFAULT '',
        source_ref TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(project_id, label)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_competitors (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        name TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', website TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(project_id, name)
    )""")


def _project(db, project_id):
    row = db.execute("SELECT * FROM geo_projects WHERE id=? AND tenant_id=?",
                     (int(project_id), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("GEO 项目不存在。")
    return row


def _text(value, limit):
    return str(value).strip()[:limit] if isinstance(value, str) else ""


def _aliases(value):
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        item.strip()[:32] for item in value
        if isinstance(item, str) and item.strip()
    ))[:10]


def _ref(value):
    try:
        parsed = json.loads(value) if value else {}
    except (TypeError, ValueError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _fact(row):
    return {
        "id": row["id"], "fact_key": row["fact_key"], "label": row["label"],
        "value": row["value"], "status": row["status"],
        "source_name": row["source_name"], "source_ref": _ref(row["source_ref"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _competitor(row):
    try:
        aliases = json.loads(row["aliases"])
    except (TypeError, ValueError):
        aliases = []
    return {
        "id": row["id"], "name": row["name"], "aliases": aliases,
        "website": row["website"], "source": row["source"],
        "source_name": "体检发现" if row["source"] == "discovered" else "手动添加",
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def counts(db, project_id):
    initialize(db)
    tenant = commander.tenant()
    competitors = db.execute(
        "SELECT COUNT(*) FROM geo_competitors WHERE project_id=? AND tenant_id=?",
        (int(project_id), tenant),
    ).fetchone()[0]
    facts = db.execute(
        "SELECT COUNT(*) FROM geo_facts WHERE project_id=? AND tenant_id=? AND status='confirmed'",
        (int(project_id), tenant),
    ).fetchone()[0]
    return {"competitors": competitors, "facts_confirmed": facts}


# ---------------------------------------------------------------------------
# Facts

def _list_facts(db, project_id):
    rows = db.execute(
        "SELECT * FROM geo_facts WHERE project_id=? AND tenant_id=? "
        "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END, id DESC",
        (int(project_id), commander.tenant()),
    ).fetchall()
    return {"ok": True, "items": [_fact(row) for row in rows],
            "keys": FACT_KEYS, "total": len(rows)}


def _fact_fields(body):
    key = body.get("fact_key") if isinstance(body.get("fact_key"), str) else "other"
    if key not in FACT_KEY_MAP:
        key = "other"
    label = _text(body.get("label"), 64) or FACT_KEY_MAP[key]["name"]
    value = _text(body.get("value"), 1000)
    if not value:
        raise commander.ChatError("请填写事实内容。")
    return key, label, value


def _add_fact(db, project_id, body):
    key, label, value = _fact_fields(body)
    tenant = commander.tenant()
    existing = db.execute(
        "SELECT * FROM geo_facts WHERE project_id=? AND tenant_id=? AND label=?",
        (int(project_id), tenant, label),
    ).fetchone()
    timestamp = now()
    if existing:
        db.execute(
            "UPDATE geo_facts SET fact_key=?,value=?,status='confirmed',updated_at=? "
            "WHERE id=? AND project_id=? AND tenant_id=?",
            (key, value, timestamp, existing["id"], int(project_id), tenant),
        )
        tenancy.audit_in(db, tenant, "geo.fact.update", str(existing["id"]),
                         f"project={project_id}; label={label}")
        row = db.execute("SELECT * FROM geo_facts WHERE id=?", (existing["id"],)).fetchone()
        return {"ok": True, "item": _fact(row)}
    total = db.execute("SELECT COUNT(*) FROM geo_facts WHERE project_id=? AND tenant_id=?",
                       (int(project_id), tenant)).fetchone()[0]
    if total >= FACT_LIMIT:
        raise commander.ChatError(f"每个项目最多保存 {FACT_LIMIT} 条品牌事实。")
    key_id = db.execute(
        "INSERT INTO geo_facts (tenant_id,project_id,fact_key,label,value,status,source_name,source_ref,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tenant, int(project_id), key, label, value, "confirmed", "手动录入", "{}", timestamp, timestamp),
    ).lastrowid
    tenancy.audit_in(db, tenant, "geo.fact.create", str(key_id),
                     f"project={project_id}; label={label}")
    row = db.execute("SELECT * FROM geo_facts WHERE id=?", (key_id,)).fetchone()
    return {"ok": True, "item": _fact(row)}


def _update_fact(db, project_id, fact_id, body):
    tenant = commander.tenant()
    row = db.execute(
        "SELECT * FROM geo_facts WHERE id=? AND project_id=? AND tenant_id=?",
        (int(fact_id), int(project_id), tenant),
    ).fetchone()
    if row is None:
        raise commander.ChatError("品牌事实不存在。")
    key, label, value = _fact_fields({**dict(row), **body})
    duplicate = db.execute(
        "SELECT id FROM geo_facts WHERE project_id=? AND tenant_id=? AND label=? AND id<>?",
        (int(project_id), tenant, label, int(fact_id)),
    ).fetchone()
    if duplicate:
        raise commander.ChatError("已有一条同名事实，请直接修改那一条。")
    db.execute(
        "UPDATE geo_facts SET fact_key=?,label=?,value=?,status='confirmed',updated_at=? "
        "WHERE id=? AND project_id=? AND tenant_id=?",
        (key, label, value, now(), int(fact_id), int(project_id), tenant),
    )
    tenancy.audit_in(db, tenant, "geo.fact.update", str(fact_id),
                     f"project={project_id}; label={label}")
    row = db.execute("SELECT * FROM geo_facts WHERE id=?", (int(fact_id),)).fetchone()
    return {"ok": True, "item": _fact(row)}


def _review_facts(db, project_id, body):
    ids = body.get("ids")
    action = body.get("action")
    if not isinstance(ids, list) or not ids:
        raise commander.ChatError("请选择要处理的事实。")
    if action not in {"confirm", "reject", "delete"}:
        raise commander.ChatError("处理方式无效。")
    try:
        values = [int(item) for item in ids]
    except (TypeError, ValueError) as error:
        raise commander.ChatError("事实编号无效。") from error
    slots = ",".join("?" for _ in values)
    tenant = commander.tenant()
    if action == "delete":
        changed = db.execute(
            f"DELETE FROM geo_facts WHERE project_id=? AND tenant_id=? AND id IN ({slots})",
            [int(project_id), tenant, *values],
        )
    else:
        status = "confirmed" if action == "confirm" else "rejected"
        changed = db.execute(
            f"UPDATE geo_facts SET status=?,updated_at=? "
            f"WHERE project_id=? AND tenant_id=? AND id IN ({slots})",
            [status, now(), int(project_id), tenant, *values],
        )
    tenancy.audit_in(db, tenant, f"geo.fact.review.{action}", str(project_id),
                     f"count={changed.rowcount}")
    return {"ok": True, "count": changed.rowcount}


def _json_items(raw):
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(text[start:end + 1])
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def _extract_facts(project, body):
    tenant = commander.tenant()
    with commander.database() as db:
        initialize(db)
        knowledge.tables(db)
        sources = db.execute(
            "SELECT name, content FROM knowledge_text WHERE tenant_id=? "
            "AND status IN ('accepted','accepted_resource') ORDER BY id DESC LIMIT 8",
            (tenant,),
        ).fetchall()
    blocks = [f"【{row['name']}】\n{row['content'][:1200]}" for row in sources]
    if project["description"]:
        blocks.append(f"【项目介绍】\n{project['description'][:1200]}")
    if not blocks:
        return {"ok": True, "added": 0,
                "message": "知识库还没有可用资料，先上传资料再整理。"}
    allowed = "、".join(f"{item['key']}={item['name']}" for item in FACT_KEYS)
    system = (
        "你是品牌资料整理助手。只从给定资料中提取可以核对的事实，不要编造、"
        "不要补充资料中没有的信息。只输出 JSON 字符串数组，不要解释或 Markdown。"
        f"数组元素格式：{{\"fact_key\":\"...\",\"label\":\"名称\",\"value\":\"内容\"}}。"
        f"fact_key 只能取：{allowed}。每条最多 60 字，最多输出 12 条。"
    )
    user = f"品牌：{project['brand_name']}\n\n资料：\n" + "\n\n".join(blocks)[:7000]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    usage = {}
    bill_id = payments.chat_reserve(messages, 1000, f"GEO 事实整理 #{project['id']}")
    try:
        raw = "".join(commander.generate(
            messages, max_output_tokens=1000, usage=usage)).strip()
    except commander.ChatError:
        payments.refund(bill_id)
        raise
    credits = payments.settle_chat(bill_id, usage, messages, raw, "GEO 事实整理")
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        existing = {
            row["label"] for row in db.execute(
                "SELECT label FROM geo_facts WHERE project_id=? AND tenant_id=?",
                (project["id"], tenant),
            ).fetchall()
        }
        timestamp = now()
        added = 0
        for item in _json_items(raw)[:12]:
            if not isinstance(item, dict):
                continue
            key = item.get("fact_key") if item.get("fact_key") in FACT_KEY_MAP else "other"
            label = _text(item.get("label"), 64) or FACT_KEY_MAP[key]["name"]
            value = _text(item.get("value"), 300)
            if not value or label in existing or added >= 12:
                continue
            db.execute(
                "INSERT INTO geo_facts (tenant_id,project_id,fact_key,label,value,status,source_name,"
                "source_ref,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tenant, project["id"], key, label, value, "pending", "AI 整理自知识库",
                 json.dumps({"kb_name": "知识库"}, ensure_ascii=False), timestamp, timestamp),
            )
            existing.add(label)
            added += 1
        tenancy.audit_in(db, tenant, "geo.facts.extract", str(project["id"]), f"added={added}")
    return {"ok": True, "added": added, "credits": round(credits, 4),
            "message": f"整理出 {added} 条待确认事实。" if added else "没有整理出新的事实。"}


# ---------------------------------------------------------------------------
# Competitors

def _list_competitors(db, project_id):
    rows = db.execute(
        "SELECT * FROM geo_competitors WHERE project_id=? AND tenant_id=? ORDER BY id ASC",
        (int(project_id), commander.tenant()),
    ).fetchall()
    return {"ok": True, "items": [_competitor(row) for row in rows],
            "total": len(rows), "limit": COMPETITOR_LIMIT}


def _add_competitors(db, project_id, body):
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise commander.ChatError("请填写竞品名称。")
    source = body.get("source") if body.get("source") in {"manual", "discovered"} else "manual"
    tenant = commander.tenant()
    rows = db.execute(
        "SELECT name FROM geo_competitors WHERE project_id=? AND tenant_id=?",
        (int(project_id), tenant),
    ).fetchall()
    existing = {row["name"].casefold() for row in rows}
    added = []
    message = ""
    for item in items[:COMPETITOR_LIMIT]:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), 64)
        if not name:
            continue
        if name.casefold() in existing:
            message = message or f"「{name}」已经在竞品列表里了。"
            continue
        if len(existing) >= COMPETITOR_LIMIT:
            message = f"最多添加 {COMPETITOR_LIMIT} 个竞品。"
            break
        timestamp = now()
        key = db.execute(
            "INSERT INTO geo_competitors (tenant_id,project_id,name,aliases,website,source,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (tenant, int(project_id), name, json.dumps(_aliases(item.get("aliases")), ensure_ascii=False),
             _text(item.get("website"), 256), source, timestamp, timestamp),
        ).lastrowid
        existing.add(name.casefold())
        row = db.execute("SELECT * FROM geo_competitors WHERE id=?", (key,)).fetchone()
        added.append(_competitor(row))
    if added:
        tenancy.audit_in(db, tenant, "geo.competitor.create", str(project_id),
                         f"count={len(added)}; source={source}")
    return {"ok": True, "items": added, "total": len(existing), "message": message}


def _update_competitor(db, project_id, competitor_id, body):
    tenant = commander.tenant()
    row = db.execute(
        "SELECT * FROM geo_competitors WHERE id=? AND project_id=? AND tenant_id=?",
        (int(competitor_id), int(project_id), tenant),
    ).fetchone()
    if row is None:
        raise commander.ChatError("竞品不存在。")
    name = _text(body.get("name"), 64) or row["name"]
    duplicate = db.execute(
        "SELECT id FROM geo_competitors WHERE project_id=? AND tenant_id=? AND name=? AND id<>?",
        (int(project_id), tenant, name, int(competitor_id)),
    ).fetchone()
    if duplicate:
        raise commander.ChatError("已有同名竞品。")
    aliases = _aliases(body.get("aliases", json.loads(row["aliases"])))
    website = _text(body.get("website"), 256) if "website" in body else row["website"]
    db.execute(
        "UPDATE geo_competitors SET name=?,aliases=?,website=?,updated_at=? "
        "WHERE id=? AND project_id=? AND tenant_id=?",
        (name, json.dumps(aliases, ensure_ascii=False), website, now(),
         int(competitor_id), int(project_id), tenant),
    )
    tenancy.audit_in(db, tenant, "geo.competitor.update", str(competitor_id),
                     f"project={project_id}; name={name}")
    row = db.execute("SELECT * FROM geo_competitors WHERE id=?", (int(competitor_id),)).fetchone()
    return {"ok": True, "item": _competitor(row)}


def _delete_competitor(db, project_id, competitor_id):
    tenant = commander.tenant()
    row = db.execute(
        "SELECT id FROM geo_competitors WHERE id=? AND project_id=? AND tenant_id=?",
        (int(competitor_id), int(project_id), tenant),
    ).fetchone()
    if row is None:
        raise commander.ChatError("竞品不存在。")
    db.execute("DELETE FROM geo_competitors WHERE id=? AND project_id=? AND tenant_id=?",
               (int(competitor_id), int(project_id), tenant))
    tenancy.audit_in(db, tenant, "geo.competitor.delete", str(competitor_id),
                     f"project={project_id}")
    return {"ok": True}


def _suggest_competitors(project, body):
    tenant = commander.tenant()
    with commander.database() as db:
        initialize(db)
        existing = {row["name"].casefold() for row in db.execute(
            "SELECT name FROM geo_competitors WHERE project_id=? AND tenant_id=?",
            (project["id"], tenant),
        ).fetchall()}
    own = {project["brand_name"].casefold()}
    try:
        own.update(name.casefold() for name in json.loads(project["aliases"]))
    except (TypeError, ValueError):
        pass
    market = "国内" if project["market"] == "cn" else "海外"
    system = (
        "你是市场调研助手。根据品牌资料，列出同一品类里客户会拿来比较的竞品品牌。"
        "不要包含该品牌自己，不要编造不存在的品牌。只输出 JSON 字符串数组，"
        "数组元素格式：{\"name\":\"品牌名\",\"reason\":\"一句话理由\"}，最多 8 个。"
    )
    user = (
        f"品牌：{project['brand_name']}\n行业/品类：{project['industry'] or '未填写'}\n"
        f"市场：{market}\n地区：{project['region'] or '未填写'}\n"
        f"介绍：{project['description'] or '未填写'}"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    usage = {}
    bill_id = payments.chat_reserve(messages, 600, f"GEO 竞品推荐 #{project['id']}")
    try:
        raw = "".join(commander.generate(
            messages, max_output_tokens=600, usage=usage)).strip()
    except commander.ChatError:
        payments.refund(bill_id)
        raise
    credits = payments.settle_chat(bill_id, usage, messages, raw, "GEO 竞品推荐")
    items = []
    for item in _json_items(raw):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), 64)
        if not name or name.casefold() in existing or name.casefold() in own:
            continue
        items.append({"name": name, "reason": _text(item.get("reason"), 120),
                      "exists": False})
        if len(items) >= 8:
            break
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        tenancy.audit_in(db, tenant, "geo.competitor.suggest", str(project["id"]),
                         f"count={len(items)}")
    return {"ok": True, "items": items, "total": len(items),
            "names": [item["name"] for item in items],
            "credits": round(credits, 4)}


# ---------------------------------------------------------------------------
# Share of voice

def _quoted_names(text):
    names = re.findall(r"[「《“\"]([A-Za-z0-9一-鿿 .&+\-]{2,30})[」》”\"]", text)
    names += re.findall(r"\b[A-Z][A-Za-z0-9&.+\-]{1,24}\b", text)
    return [name.strip() for name in names if name.strip()]


def _sov(db, project_id, query):
    try:
        days = max(1, min(365, int((query.get("days") or [SOV_DEFAULT_DAYS])[0])))
    except (TypeError, ValueError):
        days = SOV_DEFAULT_DAYS
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.execute(
        "SELECT text FROM geo_answers WHERE project_id=? AND tenant_id=? "
        "AND status='ok' AND created_at>=?",
        (int(project_id), commander.tenant(), cutoff),
    ).fetchall()
    total = len(rows)
    texts = [row["text"] or "" for row in rows]

    def rate(count):
        return round(count / total, 4) if total else 0

    own_names = []
    project = db.execute("SELECT * FROM geo_projects WHERE id=? AND tenant_id=?",
                         (int(project_id), commander.tenant())).fetchone()
    if project is not None:
        own_names.append(project["brand_name"])
        try:
            own_names.extend(json.loads(project["aliases"]))
        except (TypeError, ValueError):
            pass

    def mentioned(text, names):
        lower = text.casefold()
        return any(name and name.casefold() in lower for name in names)

    brand_answers = sum(1 for text in texts if mentioned(text, own_names))
    competitors = db.execute(
        "SELECT * FROM geo_competitors WHERE project_id=? AND tenant_id=? ORDER BY id ASC",
        (int(project_id), commander.tenant()),
    ).fetchall()
    items = []
    excluded = {name.casefold() for name in own_names if name}
    for row in competitors:
        names = [row["name"]]
        try:
            names.extend(json.loads(row["aliases"]))
        except (TypeError, ValueError):
            pass
        excluded.update(name.casefold() for name in names if name)
        count = sum(1 for text in texts if mentioned(text, names))
        items.append({"id": row["id"], "name": row["name"],
                      "answers": count, "mention_rate": rate(count)})
    items.sort(key=lambda item: item["answers"], reverse=True)

    others = {}
    for text in texts:
        for name in _quoted_names(text):
            key = name.casefold()
            if key in excluded or key in OTHER_STOPWORDS or len(name) < 2:
                continue
            others[name] = others.get(name, 0) + 1
    other_items = [
        {"name": name, "answers": count, "mention_rate": rate(count)}
        for name, count in others.items() if count >= 2
    ]
    other_items.sort(key=lambda item: item["answers"], reverse=True)
    own = own_names[0] if own_names else ""
    brand_rows = ([{"name": own, "kind": "self", "kind_name": "你",
                    "answers": brand_answers}] if own else [])
    brand_rows.extend({"name": item["name"], "kind": "competitor", "kind_name": "竞品",
                       "answers": item["answers"]} for item in items)
    brand_rows.extend({"name": item["name"], "kind": "other", "kind_name": "其他品牌",
                       "answers": item["answers"]} for item in other_items[:8])
    denominator = sum(row["answers"] for row in brand_rows)
    brands = [
        {**row, "share": round(row["answers"] / denominator, 4) if denominator else 0}
        for row in brand_rows
    ]
    return {
        "ok": True, "days": days, "total_answers": total, "answers": total,
        "brand": ({"name": own, "answers": brand_answers,
                   "mention_rate": rate(brand_answers)}),
        "items": items, "others": other_items[:8], "brands": brands,
    }


# ---------------------------------------------------------------------------
# Entry points

def read(path, query):
    path = canonical(path)
    with commander.database() as db:
        initialize(db)
        match = PROJECT.fullmatch(path)
        if not match:
            raise commander.ChatError("GEO 接口暂不可用。")
        project_id, suffix = int(match[1]), (match[2] or "").strip("/")
        _project(db, project_id)
        if suffix == "facts":
            return _list_facts(db, project_id)
        if suffix == "competitors":
            return _list_competitors(db, project_id)
        if suffix == "competitors/sov":
            return _sov(db, project_id, query)
    raise commander.ChatError("GEO 接口暂不可用。")


def write(method, path, body):
    path = canonical(path)
    match = PROJECT.fullmatch(path)
    if match and method == "POST" \
            and (match[2] or "").strip("/") in {"facts/extract", "competitors/suggest"}:
        # The model call and its charge run outside the write transaction.
        with commander.database() as db:
            initialize(db)
            project = _project(db, match[1])
        if (match[2] or "").strip("/") == "facts/extract":
            return _extract_facts(project, body)
        return _suggest_competitors(project, body)
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        match = PROJECT.fullmatch(path)
        if not match:
            raise commander.ChatError("GEO 接口暂不可用。")
        project_id, suffix = int(match[1]), (match[2] or "").strip("/")
        project = _project(db, project_id)
        if suffix == "facts" and method == "POST":
            return _add_fact(db, project_id, body)
        fact_match = re.fullmatch(r"facts/([0-9]{1,16})", suffix)
        if fact_match and method == "PUT":
            return _update_fact(db, project_id, fact_match[1], body)
        if suffix == "facts/review" and method == "POST":
            return _review_facts(db, project_id, body)
        if suffix == "competitors" and method == "POST":
            return _add_competitors(db, project_id, body)
        competitor_match = re.fullmatch(r"competitors/([0-9]{1,16})", suffix)
        if competitor_match:
            if method == "PUT":
                return _update_competitor(db, project_id, competitor_match[1], body)
            if method == "DELETE":
                return _delete_competitor(db, project_id, competitor_match[1])
    raise commander.ChatError("GEO 接口暂不可用。")
