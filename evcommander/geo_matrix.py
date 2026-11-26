"""GEO keyword matrix: the five-layer term library behind the Questions view.

Stores root words per group, expands them into combined terms, lets the user
adjust tiers, rewrite terms into questions and export a document. AI drafting
and question rewriting use the configured text model and are free for now
(same policy as GEO fact extraction and question generation).
"""

from datetime import datetime, timezone
import json
import re

import commander
import payments
import tenancy


PROJECT = re.compile(r"/api/geo/projects/([0-9]{1,16})(?:/(.*))?$")
LAYER_CONTENT = re.compile(r"kw-matrix/layers/([a-z_]+)/content")

MAX_TERMS = 300
MAX_ROOT_TEXT = 24
MAX_TERM_TEXT = 40
MAX_ADD_TERMS = 50
MAX_CONVERT = 30
QUESTION_LIMIT = 200

LAYERS = [
    {"key": "entity", "name": "找商家", "full": "找商家 / 代理 / 采购",
     "desc": "客户在找具体的店、厂家或服务商时用的词，通常会带地名或商家类型。"},
    {"key": "category", "name": "找品类", "full": "找产品 / 品类",
     "desc": "客户在找这类产品时用的词，可能还没决定找哪一家。"},
    {"key": "scene", "name": "按场景", "full": "按使用场景",
     "desc": "客户按使用场景或用途找产品时用的词。"},
    {"key": "slang", "name": "口语说法", "full": "客户的口语说法",
     "desc": "客户随口会说、但不太规范的短说法。"},
    {"key": "question", "name": "具体疑问", "full": "具体的疑问句",
     "desc": "客户会直接问 AI 的完整问题。"},
]
LAYER_MAP = {item["key"]: item for item in LAYERS}

TIERS = [
    {"key": "S", "name": "强需求", "desc": "马上要买、要下单，离成交最近。"},
    {"key": "A", "name": "有意向", "desc": "在比较、在了解价格，还没决定。"},
    {"key": "B", "name": "了解阶段", "desc": "刚开始了解，离成交还远。"},
]
TIER_MAP = {item["key"]: item for item in TIERS}
TIER_RANK = {"S": 3, "A": 2, "B": 1}
TIER_ORDER = "CASE tier WHEN 'S' THEN 0 WHEN 'A' THEN 1 ELSE 2 END"

GROUPS = [
    {"key": "region", "name": "地域", "max": 12,
     "hint": "客户会搜的地名，比如城市、区县、批发市场"},
    {"key": "industry", "name": "行业叫法", "max": 12,
     "hint": "客户对这类产品的叫法，正式名称和俗称都算"},
    {"key": "merchant", "name": "商家类型", "max": 10,
     "hint": "客户会找的商家类型，比如工厂店、批发商、专卖店"},
    {"key": "scene", "name": "场景", "max": 12,
     "hint": "客户在什么场景下用，比如送礼、自用、开店进货"},
    {"key": "slang", "name": "口语说法", "max": 12,
     "hint": "客户随口会说的短说法，比如「哪家好」「多少钱」"},
]
GROUP_MAP = {item["key"]: item for item in GROUPS}

INTENTS = {"purchase", "compare", "recommend", "price", "usage"}
LAYER_INTENT = {"entity": "purchase", "category": "recommend", "scene": "usage",
                "slang": "recommend", "question": "purchase"}


def canonical(path):
    if path.startswith("/agent-api"):
        path = path[len("/agent-api"):]
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def can_read(path):
    path = canonical(path)
    match = PROJECT.fullmatch(path)
    return bool(match and (match[2] or "").strip("/") in {"kw-matrix", "kw-matrix/terms", "kw-matrix/doc"})


def can_write(path):
    path = canonical(path)
    match = PROJECT.fullmatch(path)
    if not match:
        return False
    suffix = (match[2] or "").strip("/")
    return suffix in {"kw-matrix", "kw-matrix/draft", "kw-matrix/terms",
                      "kw-matrix/terms/batch", "kw-matrix/convert",
                      "kw-matrix/questions"} or bool(LAYER_CONTENT.fullmatch(suffix))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_kw_state (
        tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        updated_at TEXT NOT NULL, drafted_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (tenant_id, project_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_kw_roots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        grp TEXT NOT NULL, text TEXT NOT NULL, tier TEXT NOT NULL,
        UNIQUE(project_id, grp, text)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_kw_layers (
        tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL, layer TEXT NOT NULL,
        pain TEXT NOT NULL DEFAULT '', service TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (tenant_id, project_id, layer)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_kw_terms (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        text TEXT NOT NULL, layer TEXT NOT NULL, tier TEXT NOT NULL,
        tier_set INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'rule',
        converted INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(project_id, text)
    )""")


# ---------------------------------------------------------------------------
# Shared helpers

def _text(value, limit):
    return str(value).strip()[:limit] if isinstance(value, str) else ""


def _tier(value, default="A"):
    value = str(value or "").strip().upper()
    return value if value in TIER_MAP else default


def _weakest(tiers):
    return min(tiers, key=lambda tier: TIER_RANK.get(tier, 2))


def _project(db, project_id):
    row = db.execute("SELECT * FROM geo_projects WHERE id=? AND tenant_id=?",
                     (int(project_id), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("GEO 项目不存在。")
    return row


def _brands(project):
    names = [project["brand_name"]]
    try:
        names.extend(json.loads(project["aliases"]))
    except (TypeError, ValueError):
        pass
    return [name for name in dict.fromkeys(names) if name]


def _has_profile(project):
    return bool((project["industry"] or "").strip() and (project["description"] or "").strip())


def _blockers(project):
    if not _has_profile(project):
        return ["品牌档案里还没写行业和一句话介绍，AI 不知道你做什么。"]
    return []


def _default_locale(project):
    if project["market"] != "global":
        return "zh"
    try:
        locales = json.loads(project["locales"])
    except (TypeError, ValueError):
        locales = []
    for item in locales:
        if isinstance(item, dict) and _text(item.get("language"), 16):
            return _text(item.get("language"), 16)
    return "en"


def display_time(value, with_time=True):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value or ""
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def judge_layer(rate, sample_n):
    """Turn a layer's mention rate into the badge the monitor page renders."""
    if not sample_n or sample_n < 3:
        return None
    if rate >= 0.6:
        return {"level": "ok", "text": "稳定"}
    if rate >= 0.3:
        return {"level": "warm", "text": "有差距"}
    return {"level": "err", "text": "偏弱"}


def _join(left, right):
    if not left:
        return right
    if not right:
        return left
    if left[-1].isascii() and left[-1].isalnum() and right[0].isascii() and right[0].isalnum():
        return f"{left} {right}"
    return left + right


def _roots(db, project_id):
    result = {group["key"]: [] for group in GROUPS}
    rows = db.execute(
        "SELECT grp, text, tier FROM geo_kw_roots WHERE project_id=? AND tenant_id=? ORDER BY id",
        (int(project_id), commander.tenant()),
    ).fetchall()
    for row in rows:
        if row["grp"] in result:
            result[row["grp"]].append({"text": row["text"], "tier": row["tier"]})
    return result


def _layer_texts(db, project_id):
    result = {layer["key"]: {"pain": "", "service": ""} for layer in LAYERS}
    rows = db.execute(
        "SELECT layer, pain, service FROM geo_kw_layers WHERE project_id=? AND tenant_id=?",
        (int(project_id), commander.tenant()),
    ).fetchall()
    for row in rows:
        if row["layer"] in result:
            result[row["layer"]] = {"pain": row["pain"], "service": row["service"]}
    return result


def _terms(db, project_id):
    return db.execute(
        "SELECT * FROM geo_kw_terms WHERE project_id=? AND tenant_id=?",
        (int(project_id), commander.tenant()),
    ).fetchall()


def _touch(db, project_id, drafted=False):
    stamp = now()
    db.execute(
        "INSERT INTO geo_kw_state (tenant_id, project_id, updated_at, drafted_at) VALUES (?,?,?,?) "
        "ON CONFLICT(tenant_id, project_id) DO UPDATE SET updated_at=excluded.updated_at"
        + (", drafted_at=excluded.drafted_at" if drafted else ""),
        (commander.tenant(), int(project_id), stamp, stamp if drafted else ""),
    )


# ---------------------------------------------------------------------------
# Root expansion

def _expand(roots):
    """Combine the five root groups into complete terms.

    The layer of a combined term follows how the customer would search:
    place/merchant combinations are 找商家, industry/scene combinations are
    按场景, single roots keep their own layer. A combined term takes the
    weakest tier of the roots that produced it.
    """
    result = {}
    order = []

    def add(text, layer, tiers):
        text = (text or "").strip()[:MAX_TERM_TEXT]
        if len(text) < 2 or text in result:
            return
        if len(order) >= MAX_TERMS:
            return
        result[text] = {"text": text, "layer": layer, "tier": _weakest(tiers)}
        order.append(text)

    for item in roots.get("industry", []):
        add(item["text"], "category", [item["tier"]])
    for item in roots.get("scene", []):
        add(item["text"], "scene", [item["tier"]])
    for item in roots.get("slang", []):
        add(item["text"], "slang", [item["tier"]])

    for region in roots.get("region", []):
        for industry in roots.get("industry", []):
            add(_join(region["text"], industry["text"]), "entity",
                [region["tier"], industry["tier"]])
            for merchant in roots.get("merchant", []):
                add(_join(_join(region["text"], industry["text"]), merchant["text"]),
                    "entity", [region["tier"], industry["tier"], merchant["tier"]])
    for industry in roots.get("industry", []):
        for merchant in roots.get("merchant", []):
            add(_join(industry["text"], merchant["text"]), "entity",
                [industry["tier"], merchant["tier"]])
    for industry in roots.get("industry", []):
        for scene in roots.get("scene", []):
            add(_join(industry["text"], scene["text"]), "scene",
                [industry["tier"], scene["tier"]])
    for region in roots.get("region", []):
        for scene in roots.get("scene", []):
            add(_join(region["text"], scene["text"]), "scene",
                [region["tier"], scene["tier"]])

    return [result[text] for text in order]


def _clean_roots(value):
    if value is None:
        return {group["key"]: [] for group in GROUPS}
    if not isinstance(value, dict):
        raise commander.ChatError("词根格式无效。")
    result = {}
    for group in GROUPS:
        items = value.get(group["key"])
        cleaned = []
        if isinstance(items, list):
            seen = set()
            for item in items:
                if not isinstance(item, dict):
                    continue
                text = _text(item.get("text"), MAX_ROOT_TEXT)
                if len(text) < 2:
                    continue
                key = text.casefold()
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append({"text": text, "tier": _tier(item.get("tier"))})
                if len(cleaned) >= group["max"]:
                    break
        result[group["key"]] = cleaned
    return result


def _clean_layer_texts(value):
    result = {}
    if not isinstance(value, dict):
        return result
    for layer in LAYERS:
        item = value.get(layer["key"])
        if isinstance(item, dict):
            result[layer["key"]] = {
                "pain": _text(item.get("pain"), 300),
                "service": _text(item.get("service"), 300),
            }
    return result


def _write_roots(db, project_id, roots):
    tenant = commander.tenant()
    db.execute("DELETE FROM geo_kw_roots WHERE project_id=? AND tenant_id=?",
               (int(project_id), tenant))
    for group in GROUPS:
        for item in roots.get(group["key"], []):
            db.execute(
                "INSERT OR IGNORE INTO geo_kw_roots (tenant_id, project_id, grp, text, tier) "
                "VALUES (?,?,?,?,?)",
                (tenant, int(project_id), group["key"], item["text"], item["tier"]),
            )


def _write_layer_texts(db, project_id, layers):
    for key, value in layers.items():
        db.execute(
            "INSERT INTO geo_kw_layers (tenant_id, project_id, layer, pain, service) VALUES (?,?,?,?,?) "
            "ON CONFLICT(tenant_id, project_id, layer) DO UPDATE SET "
            "pain=excluded.pain, service=excluded.service",
            (commander.tenant(), int(project_id), key, value["pain"], value["service"]),
        )


def _sync_terms(db, project_id, expansion):
    tenant = commander.tenant()
    timestamp = now()
    existing = {row["text"]: row for row in _terms(db, project_id)}
    keep = set()
    for item in expansion:
        text = item["text"]
        row = existing.get(text)
        keep.add(text)
        if row is None:
            db.execute(
                "INSERT OR IGNORE INTO geo_kw_terms (tenant_id, project_id, text, layer, tier, "
                "tier_set, source, converted, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tenant, int(project_id), text, item["layer"], item["tier"], 0, "rule", 0,
                 timestamp, timestamp),
            )
            continue
        if row["source"] == "manual":
            continue
        tier = row["tier"] if row["tier_set"] else item["tier"]
        if row["layer"] != item["layer"] or row["tier"] != tier:
            db.execute(
                "UPDATE geo_kw_terms SET layer=?, tier=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (item["layer"], tier, timestamp, row["id"], tenant),
            )
    for text, row in existing.items():
        if text in keep:
            continue
        if row["source"] == "rule" and not row["tier_set"] and not row["converted"]:
            db.execute("DELETE FROM geo_kw_terms WHERE id=? AND tenant_id=?",
                       (row["id"], tenant))


# ---------------------------------------------------------------------------
# Read models

def _layer_checks(db, project_id, project):
    tenant = commander.tenant()
    run = db.execute(
        "SELECT * FROM geo_runs WHERE project_id=? AND tenant_id=? AND status='done' "
        "ORDER BY id DESC LIMIT 1",
        (int(project_id), tenant),
    ).fetchone()
    if run is None:
        return {}
    brands = [name.casefold() for name in _brands(project)]
    result = {}
    for layer in LAYERS:
        answers = db.execute(
            "SELECT a.text FROM geo_answers a JOIN geo_questions q ON q.id=a.question_id "
            "WHERE a.run_id=? AND a.tenant_id=? AND q.tenant_id=? AND q.layer=? AND a.status='ok'",
            (run["id"], tenant, tenant, layer["key"]),
        ).fetchall()
        if not answers:
            continue
        mentioned = sum(1 for row in answers
                        if any(name in row["text"].casefold() for name in brands))
        questions = db.execute(
            "SELECT COUNT(*) FROM geo_questions WHERE project_id=? AND tenant_id=? "
            "AND active=1 AND layer=?",
            (int(project_id), tenant, layer["key"]),
        ).fetchone()[0]
        result[layer["key"]] = {
            "mention_rate": round(mentioned / len(answers), 4),
            "questions": questions,
        }
    return result


def _coverage_bodies(db, project_id):
    try:
        import geo_content
        return geo_content.coverage_map(db, project_id)
    except Exception:
        return []


def _matrix(db, project_id, project=None, credits=None):
    project = project or _project(db, project_id)
    roots = _roots(db, project_id)
    layer_texts = _layer_texts(db, project_id)
    rows = _terms(db, project_id)
    bodies = _coverage_bodies(db, project_id)
    checks = _layer_checks(db, project_id, project)
    state = db.execute(
        "SELECT * FROM geo_kw_state WHERE project_id=? AND tenant_id=?",
        (int(project_id), commander.tenant()),
    ).fetchone()

    layer_items = []
    for layer in LAYERS:
        key = layer["key"]
        counts = {"S": 0, "A": 0, "B": 0}
        layer_rows = [row for row in rows if row["layer"] == key]
        for row in layer_rows:
            if row["tier"] in counts:
                counts[row["tier"]] += 1
        strong = counts["S"] + counts["A"]
        strong_covered = sum(1 for row in layer_rows
                             if row["tier"] in {"S", "A"}
                             and any(row["text"] in body for body in bodies))
        layer_items.append({
            **layer,
            "pain": layer_texts[key]["pain"],
            "service": layer_texts[key]["service"],
            "counts": counts,
            "strong": strong,
            "strong_covered": strong_covered,
            "converted": sum(1 for row in layer_rows if row["converted"]),
            "total": len(layer_rows),
            "check": checks.get(key),
        })

    has_matrix = any(roots.get(group["key"]) for group in GROUPS) or bool(rows)
    if state and state["updated_at"]:
        updated_at = state["updated_at"]
    elif rows:
        updated_at = max(row["updated_at"] for row in rows)
    else:
        updated_at = ""

    return {
        "ok": True, "has_matrix": has_matrix, "has_profile": _has_profile(project),
        "updated_at": display_time(updated_at), "total": len(rows), "docs": len(bodies),
        "credits": credits,
        "groups": [{"key": group["key"], "name": group["name"], "hint": group["hint"],
                    "max": group["max"]} for group in GROUPS],
        "roots": roots, "layers": layer_items, "tiers": TIERS,
        "draft": {"blockers": _blockers(project), "credits": None},
    }


def _term(row, bodies):
    return {
        "id": row["id"], "text": row["text"], "layer": row["layer"],
        "layer_name": LAYER_MAP.get(row["layer"], {}).get("name", row["layer"]),
        "tier": row["tier"], "tier_name": TIER_MAP.get(row["tier"], {}).get("name", row["tier"]),
        "tier_set": bool(row["tier_set"]),
        "source": row["source"],
        "source_name": {"manual": "手动添加", "rule": "自动组合", "ai": "AI 起草"}.get(
            row["source"], row["source"]),
        "covered": any(row["text"] in body for body in bodies),
        "converted": bool(row["converted"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _list_terms(db, project_id, query):
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = max(1, min(100, int((query.get("page_size") or query.get("pageSize") or ["20"])[0])))
    except ValueError as error:
        raise commander.ChatError("分页参数无效。") from error
    where, values = "WHERE project_id=? AND tenant_id=?", [int(project_id), commander.tenant()]
    for key in ("layer", "tier"):
        value = (query.get(key) or [""])[0].strip()
        if value:
            where += f" AND {key}=?"
            values.append(value)
    state = (query.get("state") or [""])[0].strip()
    keyword = (query.get("keyword") or [""])[0].strip()
    if keyword:
        where += " AND instr(lower(text), lower(?)) > 0"
        values.append(keyword)
    rows = db.execute(
        f"SELECT * FROM geo_kw_terms {where} ORDER BY {TIER_ORDER}, id",
        values,
    ).fetchall()
    bodies = _coverage_bodies(db, project_id)
    items = [_term(row, bodies) for row in rows]
    if state == "covered":
        items = [item for item in items if item["covered"]]
    elif state == "uncovered":
        items = [item for item in items if not item["covered"]]
    elif state == "converted":
        items = [item for item in items if item["converted"]]
    elif state == "unconverted":
        items = [item for item in items if not item["converted"]]
    total = len(items)
    return {"ok": True, "items": items[(page - 1) * size:page * size], "total": total}


def _doc(db, project_id, project):
    layer_texts = _layer_texts(db, project_id)
    rows = _terms(db, project_id)
    ordered = sorted(rows, key=lambda row: (TIER_RANK.get(row["tier"], 0) * -1, row["id"]))
    state = db.execute(
        "SELECT * FROM geo_kw_state WHERE project_id=? AND tenant_id=?",
        (int(project_id), commander.tenant()),
    ).fetchone()
    updated_at = state["updated_at"] if state else ""
    layers = []
    for layer in LAYERS:
        key = layer["key"]
        layer_rows = [row for row in ordered if row["layer"] == key]
        counts = {"S": 0, "A": 0, "B": 0}
        for row in layer_rows:
            if row["tier"] in counts:
                counts[row["tier"]] += 1
        layers.append({
            **layer,
            "pain": layer_texts[key]["pain"], "service": layer_texts[key]["service"],
            "counts": counts,
            "terms": [{"text": row["text"], "tier": row["tier"]} for row in layer_rows],
        })
    return {
        "ok": True,
        "doc": {
            "title": f"{project['brand_name']} · 词库矩阵",
            "project": {"industry": project["industry"], "region": project["region"]},
            "total": len(rows),
            "range": {"label": f"{display_time(updated_at, with_time=False)} 整理"
                              if updated_at else "尚未整理"},
            "tiers": TIERS,
            "layers": layers,
            "notes": [
                "词根要短、能互相拼起来；组合出来的词，档位取几个词根里最弱的那一档。",
                "S 档是强需求，A 档是有意向，B 档是了解阶段；写内容时优先照顾 S、A 档。",
                "词库里的短词用来写内容，不用全拿去体检；每层写一两篇，把这一层的说法自然写进去。",
                "这份文档不含体检覆盖情况和积分，可以直接发给客户。",
            ],
        },
    }


def document(db, project_id, project=None):
    """The Word/share document the frontend renders (without the ``ok`` envelope)."""
    return _doc(db, project_id, project or _project(db, project_id))["doc"]


# ---------------------------------------------------------------------------
# Writes

def _save(db, project_id, project, body):
    if "roots" not in body or not isinstance(body.get("roots"), dict):
        raise commander.ChatError("词根格式无效。")
    roots = _clean_roots(body.get("roots"))
    layer_texts = _clean_layer_texts(body.get("layers"))
    _write_roots(db, project_id, roots)
    _write_layer_texts(db, project_id, layer_texts)
    _sync_terms(db, project_id, _expand(roots))
    _touch(db, project_id)
    tenancy.audit_in(db, commander.tenant(), "geo.matrix.save", str(project_id),
                     f"roots={sum(len(items) for items in roots.values())}")
    return _matrix(db, project_id, project)


def _json_object(raw):
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_strings(raw):
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
    if not isinstance(parsed, list):
        return []
    result = []
    for item in parsed:
        if isinstance(item, str):
            result.append(item.strip())
        elif isinstance(item, dict):
            result.append(_text(item.get("question") or item.get("text"), 300))
        else:
            result.append("")
    return result


def _draft(project):
    project_id = project["id"]
    if not _has_profile(project):
        raise commander.ChatError("品牌档案里还没写行业和一句话介绍，AI 不知道你做什么。")
    group_lines = "\n".join(
        f"- {group['key']}（{group['name']}）：{group['hint']}，最多 {group['max']} 个"
        for group in GROUPS
    )
    layer_lines = "、".join(f"{layer['key']}={layer['name']}" for layer in LAYERS)
    system = (
        "你是 GEO 词库整理助手。根据品牌档案，帮客户整理「词库矩阵」的词根。\n"
        f"词根分组：\n{group_lines}\n"
        "档位只有 S（强需求，马上要买）、A（有意向，在比较）、B（了解阶段）。\n"
        f"层次：{layer_lines}。\n"
        "只输出一个 JSON 对象，不要解释或 Markdown。格式："
        "{\"roots\":{\"region\":[{\"text\":\"词根\",\"tier\":\"A\"}],"
        "\"industry\":[],\"merchant\":[],\"scene\":[],\"slang\":[]},"
        "\"layers\":{\"entity\":{\"pain\":\"客户卡在哪儿\",\"service\":\"你能提供什么\"},"
        "\"category\":{...},\"scene\":{...},\"slang\":{...},\"question\":{...}}}。\n"
        "词根要短（2-10 个字）、不要带品牌名、不要重复；layers 每层两句话各不超过 80 字，"
        "只写档案里能支持的真实内容。"
    )
    user = (
        f"品牌：{project['brand_name']}\n行业：{project['industry']}\n"
        f"地区：{project['region']}\n介绍：{project['description'][:800]}\n"
        f"市场：{'海外' if project['market'] == 'global' else '国内'}"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    usage = {}
    bill_id = payments.chat_reserve(messages, 1600, f"GEO 词库起草 #{project_id}")
    try:
        raw = "".join(commander.generate(
            messages, max_output_tokens=1600, usage=usage)).strip()
        data = _json_object(raw)
        roots = _clean_roots(data.get("roots") if isinstance(data.get("roots"), dict) else {})
        if not any(roots.values()):
            raise commander.ChatError("AI 没有返回可用的词根，请重试。")
    except commander.ChatError:
        payments.refund(bill_id)
        raise
    credits = payments.settle_chat(bill_id, usage, messages, raw, "GEO 词库起草")
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        _write_roots(db, project_id, roots)
        _write_layer_texts(db, project_id, _clean_layer_texts(data.get("layers")))
        _sync_terms(db, project_id, _expand(roots))
        _touch(db, project_id, drafted=True)
        tenancy.audit_in(db, commander.tenant(), "geo.matrix.draft", str(project_id),
                         f"roots={sum(len(items) for items in roots.values())}")
        return _matrix(db, project_id, project, credits=credits)


def _add_terms(db, project_id, body):
    layer = str(body.get("layer") or "").strip()
    if layer not in LAYER_MAP:
        raise commander.ChatError("层次无效。")
    tier = _tier(body.get("tier"))
    texts = body.get("texts")
    if not isinstance(texts, list):
        raise commander.ChatError("请填写要加入的词。")
    cleaned, seen = [], set()
    for item in texts:
        if not isinstance(item, str):
            continue
        text = item.strip()[:MAX_TERM_TEXT]
        if len(text) < 2:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) >= MAX_ADD_TERMS:
            break
    if not cleaned:
        raise commander.ChatError("请填写至少 2 个字的词。")
    timestamp = now()
    added = 0
    for text in cleaned:
        changed = db.execute(
            "INSERT OR IGNORE INTO geo_kw_terms (tenant_id, project_id, text, layer, tier, "
            "tier_set, source, converted, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (commander.tenant(), int(project_id), text, layer, tier, 0, "manual", 0,
             timestamp, timestamp),
        )
        added += changed.rowcount
    _touch(db, project_id)
    tenancy.audit_in(db, commander.tenant(), "geo.matrix.terms.add", str(project_id),
                     f"layer={layer}; added={added}")
    result = {"ok": True, "added": added}
    skipped = len(cleaned) - added
    if skipped:
        result["message"] = f"重复的 {skipped} 个已跳过"
    return result


def _batch(db, project_id, body):
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise commander.ChatError("请选择要操作的词。")
    try:
        clean_ids = list(dict.fromkeys(int(item) for item in ids))[:100]
    except (TypeError, ValueError) as error:
        raise commander.ChatError("选择的词无效。") from error
    placeholders = ",".join("?" for _ in clean_ids)
    action = str(body.get("action") or "").strip()
    tenant = commander.tenant()
    if action == "tier":
        tier = _tier(body.get("tier"))
        changed = db.execute(
            f"UPDATE geo_kw_terms SET tier=?, tier_set=1, updated_at=? "
            f"WHERE project_id=? AND tenant_id=? AND id IN ({placeholders})",
            [tier, now(), int(project_id), tenant, *clean_ids],
        )
    elif action == "delete":
        changed = db.execute(
            f"DELETE FROM geo_kw_terms WHERE project_id=? AND tenant_id=? AND id IN ({placeholders})",
            [int(project_id), tenant, *clean_ids],
        )
    else:
        raise commander.ChatError("操作无效。")
    _touch(db, project_id)
    tenancy.audit_in(db, commander.tenant(), f"geo.matrix.terms.{action}", str(project_id),
                     f"count={changed.rowcount}")
    return {"ok": True, "count": changed.rowcount}


def _impact(db, project_id):
    price = payments.geo_answer_price()
    engines, samples = 2, 1
    per_question = round(engines * samples * price, 4)
    active = db.execute(
        "SELECT COUNT(*) FROM geo_questions WHERE project_id=? AND tenant_id=? AND active=1",
        (int(project_id), commander.tenant()),
    ).fetchone()[0]
    return {
        "limit": QUESTION_LIMIT, "active": active, "per_question": per_question,
        "runs_per_month": 0, "confirm_run": 5, "confirm_month": 20,
        "engines": engines, "samples": samples,
        "run_now": round(active * per_question, 4), "schedule_name": "",
    }


def _convert(project, body):
    project_id = project["id"]
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise commander.ChatError("请选择要转成问题的词。")
    if len(ids) > MAX_CONVERT:
        raise commander.ChatError(f"一次最多改写 {MAX_CONVERT} 个词，先挑最重要的。")
    try:
        clean_ids = list(dict.fromkeys(int(item) for item in ids))
    except (TypeError, ValueError) as error:
        raise commander.ChatError("选择的词无效。") from error
    placeholders = ",".join("?" for _ in clean_ids)
    with commander.database() as db:
        initialize(db)
        rows = db.execute(
            f"SELECT * FROM geo_kw_terms WHERE project_id=? AND tenant_id=? AND id IN ({placeholders})",
            [int(project_id), commander.tenant(), *clean_ids],
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        ordered = [by_id[key] for key in clean_ids if key in by_id]
        if not ordered:
            raise commander.ChatError("这些词不存在或已删除，请刷新后重试。")
        existing = {
            row["text"] for row in db.execute(
                "SELECT text FROM geo_questions WHERE project_id=? AND tenant_id=?",
                (int(project_id), commander.tenant()),
            ).fetchall()
        }
    locale = _text(body.get("locale"), 16) or _default_locale(project)
    items = [{"term": row["text"], "layer": LAYER_MAP[row["layer"]]["name"]} for row in ordered]
    system = (
        "你是 GEO 问题改写助手。把每个短词改写成客户会直接问 AI 的一句自然问题。"
        "保持原意，不要编造品牌名，每条不超过 40 个字。"
        f"用语言代码 {locale} 输出。按输入顺序只输出 JSON 字符串数组，不要解释或 Markdown。"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
    ]
    usage = {}
    bill_id = payments.chat_reserve(messages, 1200, f"GEO 词库改写 #{project_id}")
    try:
        raw = "".join(commander.generate(
            messages, max_output_tokens=1200, usage=usage)).strip()
        rewritten = _json_strings(raw)
        candidates = []
        for row, text in zip(ordered, rewritten):
            text = (text or "").strip()[:300]
            if len(text) < 2:
                continue
            candidates.append({
                "term_id": row["id"], "text": text, "term": row["text"],
                "layer": row["layer"],
                "layer_name": LAYER_MAP.get(row["layer"], {}).get("name", row["layer"]),
                "tier": row["tier"],
                "intent": LAYER_INTENT.get(row["layer"], "purchase"), "locale": locale,
                "exists": text in existing,
            })
        if not candidates:
            raise commander.ChatError("这次没改写出来，请少选几个词再试。")
    except commander.ChatError:
        payments.refund(bill_id)
        raise
    credits = payments.settle_chat(bill_id, usage, messages, raw, "GEO 词库改写")
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        tenancy.audit_in(db, commander.tenant(), "geo.matrix.convert", str(project_id),
                         f"count={len(candidates)}")
        return {"ok": True, "items": candidates, "impact": _impact(db, project_id),
                "credits": credits}


def _add_questions(db, project_id, project, body):
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise commander.ChatError("请选择要加入问题库的问题。")
    tenant = commander.tenant()
    timestamp = now()
    added, skipped = 0, 0
    for item in items[:MAX_CONVERT]:
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"), 300)
        if len(text) < 2:
            continue
        term_id = item.get("term_id")
        term = None
        if term_id not in (None, "", 0):
            try:
                term = db.execute(
                    "SELECT * FROM geo_kw_terms WHERE id=? AND project_id=? AND tenant_id=?",
                    (int(term_id), int(project_id), tenant),
                ).fetchone()
            except (TypeError, ValueError):
                term = None
            if term is None:
                raise commander.ChatError("词已被删除，请刷新后重试。")
        layer = term["layer"] if term else ""
        if layer not in LAYER_MAP:
            layer = ""
        intent = str(item.get("intent") or "purchase")
        if intent not in INTENTS:
            intent = "purchase"
        locale = _text(item.get("locale"), 16) or _default_locale(project)
        changed = db.execute(
            "INSERT OR IGNORE INTO geo_questions (tenant_id, project_id, text, intent, locale, "
            "layer, term_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (tenant, int(project_id), text, intent, locale, layer,
             term["id"] if term is not None else 0, timestamp),
        )
        if changed.rowcount:
            added += 1
        else:
            skipped += 1
        if term is not None:
            db.execute("UPDATE geo_kw_terms SET converted=1, updated_at=? WHERE id=? AND tenant_id=?",
                       (timestamp, term["id"], tenant))
    if not added and not skipped:
        raise commander.ChatError("没有可以加入的问题。")
    _touch(db, project_id)
    tenancy.audit_in(db, tenant, "geo.matrix.questions.add", str(project_id),
                     f"added={added}")
    result = {"ok": True, "added": added}
    if skipped:
        result["message"] = f"重复的 {skipped} 个已跳过"
    return result


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
        project = _project(db, project_id)
        if suffix == "kw-matrix":
            return _matrix(db, project_id, project)
        if suffix == "kw-matrix/terms":
            return _list_terms(db, project_id, query)
        if suffix == "kw-matrix/doc":
            return _doc(db, project_id, project)
    raise commander.ChatError("GEO 接口暂不可用。")


def write(method, path, body):
    path = canonical(path)
    match = PROJECT.fullmatch(path)
    if match and method == "POST":
        suffix = (match[2] or "").strip("/")
        if suffix in {"kw-matrix/draft", "kw-matrix/convert"}:
            # The model call and its charge run outside the write transaction.
            with commander.database() as db:
                initialize(db)
                project = _project(db, match[1])
            if suffix == "kw-matrix/draft":
                return _draft(project)
            return _convert(project, body)
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        match = PROJECT.fullmatch(path)
        if not match:
            raise commander.ChatError("GEO 接口暂不可用。")
        project_id, suffix = int(match[1]), (match[2] or "").strip("/")
        project = _project(db, project_id)
        if suffix == "kw-matrix" and method == "PUT":
            return _save(db, project_id, project, body)
        if suffix == "kw-matrix/terms" and method == "POST":
            return _add_terms(db, project_id, body)
        if suffix == "kw-matrix/terms/batch" and method == "POST":
            return _batch(db, project_id, body)
        if suffix == "kw-matrix/questions" and method == "POST":
            return _add_questions(db, project_id, project, body)
        layer_match = LAYER_CONTENT.fullmatch(suffix)
        if layer_match and method == "POST":
            if layer_match[1] not in LAYER_MAP:
                raise commander.ChatError("层次无效。")
            import geo_content
            return geo_content.created_layer_content(db, project_id, layer_match[1])
    raise commander.ChatError("GEO 接口暂不可用。")
