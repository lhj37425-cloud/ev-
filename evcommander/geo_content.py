"""GEO content workshop, manual publishing and tickets.

Contents are written from brand facts/knowledge/brief (one text-model call),
scored by a local rubric, checked against confirmed facts and ad-law wording,
and can be adapted per platform. Publishing is manual for now: a ticket gives
the editor a copy-ready draft and records the public URL back, which creates a
publication row used by reports and keyword coverage.
"""

from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import re
import threading

import commander
import geo_brand
import geo_matrix
import geo_report
import geo_runs
import knowledge
import tenancy


CONTENT = re.compile(r"/api/geo/contents/([0-9]{1,16})$")
CONTENT_ACTION = re.compile(
    r"/api/geo/contents/([0-9]{1,16})/(generate|score|check|approve|apply-suggestion|adapt|publish|push|tickets)$")
CONTENT_VARIANT = re.compile(r"/api/geo/contents/([0-9]{1,16})/variants/([a-z0-9_]{1,32})$")
TICKET = re.compile(r"/api/geo/tickets/([0-9]{1,16})$")
PUBLICATION_URL = re.compile(r"/api/geo/publications/([0-9]{1,16})/url$")
PROJECT = re.compile(r"/api/geo/projects/([0-9]{1,16})(?:/(.*))?$")
META = "/api/geo/content-meta"

GENRES_CN = [
    {"key": "qa_answer", "name": "问答文章", "for": "回答一个具体问题，结论写在第一段"},
    {"key": "comparison", "name": "对比 / 盘点", "for": "把几个选择放在一起比较，给出适用场景"},
    {"key": "howto", "name": "教程 / 指南", "for": "一步步教客户怎么做，顺便带出你的方案"},
    {"key": "case", "name": "客户案例", "for": "讲一个真实场景和结果，不要编造客户"},
]
GENRES_GLOBAL = [
    {"key": "g_answer", "name": "Global answer", "for": "Answer one concrete question directly"},
    {"key": "comparison", "name": "Comparison", "for": "Compare options and explain when to use each"},
    {"key": "howto", "name": "How-to guide", "for": "Show the steps and where you help"},
    {"key": "case", "name": "Case study", "for": "Tell a real scenario and result"},
]
TARGETS_CN = [
    {"key": "website", "name": "官网 / 网站", "kind": "channel", "max": 5000},
    {"key": "wechat_mp", "name": "公众号", "kind": "channel", "max": 20000},
    {"key": "zhihu", "name": "知乎", "kind": "community", "max": 8000,
     "login_url": "https://zhuanlan.zhihu.com/write", "login_label": "打开知乎写文章"},
    {"key": "toutiao", "name": "头条号", "kind": "community", "max": 8000,
     "login_url": "https://mp.toutiao.com", "login_label": "打开头条号后台"},
    {"key": "sohu", "name": "搜狐号", "kind": "community", "max": 8000,
     "login_url": "https://mp.sohu.com", "login_label": "打开搜狐号后台"},
    {"key": "csdn", "name": "CSDN", "kind": "community", "max": 8000,
     "login_url": "https://mp.csdn.net", "login_label": "打开 CSDN 后台"},
    {"key": "baijiahao", "name": "百家号", "kind": "community", "max": 8000,
     "login_url": "https://baijiahao.baidu.com", "login_label": "打开百家号后台"},
    {"key": "baike", "name": "百度百科", "kind": "community", "max": 10000},
    {"key": "dianping", "name": "大众点评", "kind": "community", "max": 3000},
    {"key": "media", "name": "媒体发稿", "kind": "media", "max": 8000},
    {"key": "hub", "name": "品牌知识页", "kind": "hub", "max": 8000},
]
TARGETS_GLOBAL = [
    {"key": "wordpress", "name": "官网 / WordPress", "kind": "channel", "max": 5000},
    {"key": "medium", "name": "Medium", "kind": "community", "max": 8000,
     "login_url": "https://medium.com/new-story", "login_label": "打开 Medium"},
    {"key": "reddit", "name": "Reddit", "kind": "community", "max": 8000,
     "login_url": "https://www.reddit.com/submit", "login_label": "打开 Reddit"},
    {"key": "quora", "name": "Quora", "kind": "community", "max": 8000,
     "login_url": "https://www.quora.com", "login_label": "打开 Quora"},
    {"key": "linkedin", "name": "LinkedIn", "kind": "community", "max": 5000,
     "login_url": "https://www.linkedin.com/feed/", "login_label": "打开 LinkedIn"},
    {"key": "substack", "name": "Substack", "kind": "community", "max": 8000},
    {"key": "x", "name": "X", "kind": "community", "max": 3000},
    {"key": "youtube", "name": "YouTube", "kind": "community", "max": 5000},
    {"key": "media", "name": "Media outreach", "kind": "media", "max": 8000},
    {"key": "hub", "name": "Brand hub", "kind": "hub", "max": 8000},
]
CHANNEL_KINDS = [
    {"kind": "channel", "name": "官网与公众号"},
    {"kind": "community", "name": "社区 / 自媒体"},
    {"kind": "media", "name": "媒体发稿"},
    {"kind": "hub", "name": "品牌知识页"},
]
STATUS_NAMES = {"draft": "草稿", "ready": "可发布", "published": "已发布", "archived": "已归档"}
RISK_WORDS = {
    "最好": "risk", "最佳": "risk", "最强": "risk", "最大": "risk", "最高": "risk",
    "最低": "risk", "最优": "risk", "最先进": "risk", "第一": "risk", "唯一": "risk",
    "顶级": "risk", "国家级": "risk", "世界级": "risk", "绝对": "risk", "100%": "risk",
    "万能": "risk", "永久": "warn", "保证": "warn", "零风险": "warn", "领先": "warn",
}
CLAIM_HINTS = ("成立", "年", "价格", "免费", "认证", "专利", "资质", "服务", "客户",
               "覆盖", "支持", "保证", "第一", "唯一", "领先", "销量", "门店", "％", "%")
MAX_CONTENT = 40


def canonical(path):
    if path.startswith("/agent-api"):
        path = path[len("/agent-api"):]
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def can_read(path):
    path = canonical(path)
    if path == META or CONTENT.fullmatch(path):
        return True
    match = PROJECT.fullmatch(path)
    if not match:
        return False
    return (match[2] or "").strip("/") in {
        "contents", "opportunities", "publications", "tickets",
        "media", "media/recommend", "media-orders", "media-effect",
    }


def can_write(path):
    path = canonical(path)
    if CONTENT.fullmatch(path) or CONTENT_ACTION.fullmatch(path) \
            or CONTENT_VARIANT.fullmatch(path) or TICKET.fullmatch(path) \
            or PUBLICATION_URL.fullmatch(path):
        return True
    match = PROJECT.fullmatch(path)
    if not match:
        return False
    return (match[2] or "").strip("/") in {
        "contents", "tickets", "media-orders", "media-orders/estimate",
    }


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_contents (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '', brief TEXT NOT NULL DEFAULT '',
        question TEXT NOT NULL DEFAULT '', question_id INTEGER NOT NULL DEFAULT 0,
        genre TEXT NOT NULL DEFAULT 'qa_answer', source_kind TEXT NOT NULL DEFAULT 'manual',
        source_ref TEXT NOT NULL DEFAULT '{}', platforms TEXT NOT NULL DEFAULT '[]',
        locale TEXT NOT NULL DEFAULT 'zh', language TEXT NOT NULL DEFAULT 'zh',
        status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1,
        zh_outline TEXT NOT NULL DEFAULT '[]', missing TEXT NOT NULL DEFAULT '[]',
        evidence TEXT NOT NULL DEFAULT '[]', score TEXT NOT NULL DEFAULT '',
        score_sig TEXT NOT NULL DEFAULT '', gate TEXT NOT NULL DEFAULT '',
        gate_detail TEXT NOT NULL DEFAULT '', gate_sig TEXT NOT NULL DEFAULT '',
        variants TEXT NOT NULL DEFAULT '{}', busy TEXT NOT NULL DEFAULT '',
        busy_name TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
        created_by_name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        edited_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        content_id INTEGER NOT NULL, platform TEXT NOT NULL, platform_name TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '', draft TEXT NOT NULL DEFAULT '',
        draft_text TEXT NOT NULL DEFAULT '', draft_html TEXT NOT NULL DEFAULT '',
        draft_md TEXT NOT NULL DEFAULT '', rich INTEGER NOT NULL DEFAULT 1,
        tips TEXT NOT NULL DEFAULT '[]', login_url TEXT NOT NULL DEFAULT '',
        login_label TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'todo',
        url TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_publications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        content_id INTEGER NOT NULL, ticket_id INTEGER NOT NULL DEFAULT 0,
        channel_kind TEXT NOT NULL DEFAULT 'community', platform TEXT NOT NULL DEFAULT '',
        platform_name TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'published', url TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '', cited_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")


def _project(db, project_id):
    row = db.execute("SELECT * FROM geo_projects WHERE id=? AND tenant_id=?",
                     (int(project_id), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("GEO 项目不存在。")
    return row


def _text(value, limit):
    return str(value).strip()[:limit] if isinstance(value, str) else ""


def _hash(value):
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


def _json(raw, default):
    try:
        value = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _meta(market):
    global_market = market == "global"
    targets = TARGETS_GLOBAL if global_market else TARGETS_CN
    return {
        "ok": True,
        "genres": GENRES_GLOBAL if global_market else GENRES_CN,
        "targets": targets, "channel_kinds": CHANNEL_KINDS,
        "models": {"write": commander.MODEL, "check": commander.MODEL},
        "markets": [{"code": "cn", "name": "国内"}, {"code": "global", "name": "海外"}],
    }


def _genres(market):
    return GENRES_GLOBAL if market == "global" else GENRES_CN


def _targets(market):
    return TARGETS_GLOBAL if market == "global" else TARGETS_CN


def _target(market, key):
    for item in _targets(market):
        if item["key"] == key:
            return item
    return None


def _genre(market, key):
    for item in _genres(market):
        if item["key"] == key:
            return item
    return _genres(market)[0]


def _locale_name(project, locale):
    if project["market"] != "global":
        return "中文"
    try:
        locales = json.loads(project["locales"])
    except (TypeError, ValueError):
        locales = []
    for item in locales:
        if isinstance(item, dict):
            key = f"{item.get('country')}-{item.get('language')}"
            if key == locale:
                return f"{item.get('country')} · {item.get('language')}"
    return locale or "中文"


def _default_locale(project):
    if project["market"] != "global":
        return "zh"
    try:
        locales = json.loads(project["locales"])
    except (TypeError, ValueError):
        locales = []
    if locales and isinstance(locales[0], dict):
        return f"{locales[0].get('country')}-{locales[0].get('language')}"
    return "en"


def _publication(row):
    return {
        "id": row["id"], "content_id": row["content_id"], "ticket_id": row["ticket_id"],
        "channel_kind": row["channel_kind"],
        "channel_kind_name": next((item["name"] for item in CHANNEL_KINDS
                                   if item["kind"] == row["channel_kind"]), row["channel_kind"]),
        "platform": row["platform"], "platform_name": row["platform_name"],
        "title": row["title"], "content_title": row["title"],
        "status": row["status"], "status_name": {
            "published": "已发布", "draft_saved": "已存草稿", "queued": "排队中",
            "running": "发布中", "failed": "失败", "offline": "已下线",
        }.get(row["status"], row["status"]),
        "url": row["url"], "error": row["error"], "cited_count": row["cited_count"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _ticket(row):
    try:
        tips = json.loads(row["tips"])
    except (TypeError, ValueError):
        tips = []
    return {
        "id": row["id"], "content_id": row["content_id"], "content_title": row["title"],
        "title": row["title"], "platform": row["platform"], "platform_name": row["platform_name"],
        "draft": row["draft"], "draft_text": row["draft_text"],
        "draft_html": row["draft_html"], "draft_md": row["draft_md"],
        "rich": bool(row["rich"]), "tips": tips,
        "login_url": row["login_url"], "login_label": row["login_label"],
        "status": row["status"], "status_name": {"todo": "待发布", "done": "已发布",
                                                 "abandoned": "已放弃"}.get(row["status"], row["status"]),
        "url": row["url"], "created_at": row["created_at"],
    }


def _content_row(db, project_id, content_id):
    row = db.execute(
        "SELECT * FROM geo_contents WHERE id=? AND project_id=? AND tenant_id=?",
        (int(content_id), int(project_id), commander.tenant()),
    ).fetchone()
    if row is None:
        raise commander.ChatError("内容不存在。")
    return row


def _gate_summary(row):
    gate = _json(row["gate"], {})
    valid = bool(gate) and row["gate_sig"] == _hash(row["body"])
    if not gate:
        return None
    return {
        "valid": valid,
        "decision": gate.get("decision", ""),
        "decision_name": gate.get("decision_name", ""),
        "reasons": gate.get("reasons", []),
    }


def _can_publish(row):
    gate = _gate_summary(row)
    return bool(row["body"].strip()) and row["status"] in {"draft", "ready"} \
        and bool(gate and gate["valid"] and gate["decision"] in {"pass", "approved"})


def _content_json(db, project, row, detail=False):
    platforms = []
    for key in _json(row["platforms"], []):
        target = _target(project["market"], key)
        if target:
            platforms.append({"key": target["key"], "name": target["name"]})
    language = row["language"] or "zh"
    body = row["body"] or ""
    chars = len(re.sub(r"\s", "", body))
    words = 0 if language.startswith("zh") else len(re.findall(r"\S+", body))
    score = _json(row["score"], {}) if row["score"] else None
    score_fresh = bool(score) and row["score_sig"] == _hash(body)
    gate = _gate_summary(row)
    gate_detail = _json(row["gate_detail"], {}) if row["gate_detail"] else None
    if gate_detail:
        gate_detail = {**gate_detail, "valid": bool(gate and gate["valid"])}
    editable = row["status"] != "published"
    item = {
        "id": row["id"], "project_id": row["project_id"],
        "title": row["title"], "body": body, "summary": row["summary"], "brief": row["brief"],
        "question": row["question"], "question_id": row["question_id"],
        "genre": row["genre"],
        "genre_name": _genre(project["market"], row["genre"])["name"],
        "source_kind": row["source_kind"], "source_ref": _json(row["source_ref"], {}),
        "platforms": platforms, "locale": row["locale"],
        "locale_name": _locale_name(project, row["locale"]), "language": language,
        "status": row["status"], "status_name": STATUS_NAMES.get(row["status"], row["status"]),
        "version": row["version"], "words": words, "chars": chars,
        "score_total": score.get("total") if score else None,
        "score_fresh": score_fresh,
        "gate": gate, "busy": row["busy"], "busy_name": row["busy_name"],
        "error": row["error"], "editable": editable,
        "can_publish": _can_publish(row), "can_approve": True,
        "created_by_name": row["created_by_name"], "created_at": row["created_at"],
        "edited_at": row["edited_at"], "updated_at": row["updated_at"],
    }
    if not detail:
        return item
    variants = {}
    for key, variant in _json(row["variants"], {}).items():
        target = _target(project["market"], key)
        variants[key] = {
            **variant,
            "name": target["name"] if target else key,
            "stale": variant.get("source_sig") != _hash(body),
        }
    publications = db.execute(
        "SELECT * FROM geo_publications WHERE content_id=? AND tenant_id=? ORDER BY id DESC",
        (row["id"], commander.tenant()),
    ).fetchall()
    item.update({
        "zh_outline": _json(row["zh_outline"], []),
        "missing": _json(row["missing"], []),
        "evidence": _json(row["evidence"], []),
        "score": (score if score_fresh else None),
        "gate_detail": gate_detail,
        "variants": variants,
        "publications": [_publication(entry) for entry in publications],
    })
    return item


def _list_contents(db, project_id, project, query):
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = max(1, min(100, int((query.get("page_size") or query.get("pageSize") or ["20"])[0])))
    except ValueError as error:
        raise commander.ChatError("分页参数无效。") from error
    status = (query.get("status") or [""])[0].strip()
    keyword = (query.get("keyword") or [""])[0].strip()
    where, values = "WHERE project_id=? AND tenant_id=?", [int(project_id), commander.tenant()]
    if status and status != "publishable":
        where += " AND status=?"
        values.append(status)
    elif status != "publishable":
        where += " AND status<>'archived'"
    if keyword:
        where += " AND (instr(lower(title), lower(?)) > 0 OR instr(lower(question), lower(?)) > 0)"
        values.extend([keyword, keyword])
    rows = db.execute(
        f"SELECT * FROM geo_contents {where} ORDER BY id DESC",
        values,
    ).fetchall()
    items = [_content_json(db, project, row) for row in rows]
    if status == "publishable":
        items = [item for item in items if item["can_publish"]]
    total = len(items)
    items = items[(page - 1) * size:page * size]
    all_rows = db.execute(
        "SELECT status,body,gate,gate_sig FROM geo_contents WHERE project_id=? AND tenant_id=?",
        (int(project_id), commander.tenant()),
    ).fetchall()
    counts = {"ready": 0, "published": 0, "draft": 0, "archived": 0}
    for row in all_rows:
        if row["status"] in {"draft", "published", "archived"}:
            counts[row["status"]] += 1
        if row["status"] != "published" and _can_publish(row):
            counts["ready"] += 1
    return {"ok": True, "items": items, "total": total, "counts": counts}


def _create_content(db, project_id, project, body):
    question = _text(body.get("question"), 200)
    try:
        question_id = int(body.get("question_id") or 0)
    except (TypeError, ValueError) as error:
        raise commander.ChatError("问题编号无效。") from error
    if question_id:
        row = db.execute(
            "SELECT text FROM geo_questions WHERE id=? AND project_id=? AND tenant_id=?",
            (question_id, int(project_id), commander.tenant()),
        ).fetchone()
        if row is None:
            raise commander.ChatError("问题不存在。")
        question = row["text"]
    if not question:
        raise commander.ChatError("先写一句这篇要回答的问题。")
    genre = _genre(project["market"], str(body.get("genre") or ""))["key"]
    platforms = []
    for key in body.get("platforms") or []:
        if isinstance(key, str) and _target(project["market"], key) and key not in platforms:
            platforms.append(key)
    locale = _text(body.get("locale"), 16) or _default_locale(project)
    language = locale.split("-")[-1] if "-" in locale else locale
    timestamp = now()
    key = db.execute(
        "INSERT INTO geo_contents (tenant_id,project_id,title,question,question_id,genre,"
        "source_kind,source_ref,platforms,locale,language,status,created_by_name,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (commander.tenant(), int(project_id), _text(body.get("title"), 120) or question[:60],
         question, question_id, genre, _text(body.get("source_kind"), 20) or "manual",
         json.dumps(body.get("source_ref") if isinstance(body.get("source_ref"), dict) else {},
                    ensure_ascii=False),
         json.dumps(platforms), locale, language, "draft",
         tenancy.current_identity().get("username") or "本地演示", timestamp, timestamp),
    ).lastrowid
    tenancy.audit_in(db, commander.tenant(), "geo.content.create", str(key),
                     f"project={project_id}; question={question[:40]}")
    return {"ok": True, "content": _content_json(db, project, _content_row(db, project_id, key),
                                                 detail=True)}


def _update_content(db, project_id, project, content_id, body):
    row = _content_row(db, project_id, content_id)
    if row["status"] == "published":
        raise commander.ChatError("已发布的内容不能直接修改，请先复制或归档。")
    fields = {}
    for key, limit in (("title", 120), ("body", 20000), ("summary", 200), ("brief", 2000)):
        if key in body:
            fields[key] = _text(body.get(key), limit)
    status = str(body.get("status") or "").strip()
    if status in {"archived", "draft"}:
        fields["status"] = status
    if not fields:
        raise commander.ChatError("没有要保存的内容。")
    timestamp = now()
    assignments = ",".join(f"{key}=?" for key in fields)
    db.execute(
        f"UPDATE geo_contents SET {assignments}, edited_at=?, updated_at=? "
        f"WHERE id=? AND tenant_id=?",
        [*fields.values(), timestamp, timestamp, int(content_id), commander.tenant()],
    )
    return {"ok": True, "content": _content_json(
        db, project, _content_row(db, project_id, content_id), detail=True)}


def _delete_content(db, project_id, content_id):
    row = _content_row(db, project_id, content_id)
    if row["status"] == "published":
        raise commander.ChatError("发布过的内容不能删除，可以归档。")
    pubs = db.execute("SELECT COUNT(*) FROM geo_publications WHERE content_id=? AND tenant_id=?",
                      (int(content_id), commander.tenant())).fetchone()[0]
    if pubs:
        raise commander.ChatError("这篇内容有发布记录，不能删除，可以归档。")
    db.execute("DELETE FROM geo_contents WHERE id=? AND tenant_id=?",
               (int(content_id), commander.tenant()))
    tenancy.audit_in(db, commander.tenant(), "geo.content.delete", str(content_id))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Scoring and fact checking

def _score(content):
    body = content["body"] or ""
    title = content["title"] or ""
    question = content["question"] or ""
    first = re.split(r"\n\s*\n", body.strip(), maxsplit=1)[0] if body.strip() else ""
    headings = len(re.findall(r"(?m)^#{1,3}\s", body))
    lists = len(re.findall(r"(?m)^\s*[-*]\s", body))
    digits = len(re.findall(r"[0-9０-９]+", body))
    brand_terms = [content.get("brand_name") or "", content.get("industry") or ""]
    brand_hit = any(term and term in body for term in brand_terms)
    key_terms = [term for term in re.findall(r"[一-龥A-Za-z]{2,}", question) if term][:4]
    answer_hit = any(term in first for term in key_terms) if key_terms else bool(first)
    risks = _compliance(body)
    length = len(re.sub(r"\s", "", body))
    items = [
        {"key": "answer", "name": "首段直接回答", "score": 20 if answer_hit and 40 <= len(first) <= 400 else 10,
         "max": 20},
        {"key": "structure", "name": "结构清晰", "score": min(15, (headings * 4) + (lists * 2)), "max": 15},
        {"key": "data", "name": "数据与出处", "score": 15 if digits >= 2 else (8 if digits else 0), "max": 15},
        {"key": "brand", "name": "品牌与业务写在一起", "score": 15 if brand_hit else 5, "max": 15},
        {"key": "length", "name": "篇幅合适", "score": 20 if 500 <= length <= 3000 else (10 if length >= 250 else 0),
         "max": 20},
        {"key": "word", "name": "没有极限词", "score": 15 if not risks else 0, "max": 15},
    ]
    for item in items:
        item["rate"] = round(item["score"] / item["max"], 4) if item["max"] else 0
    penalties = []
    if length and length < 300:
        penalties.append({"key": "short", "name": "篇幅偏短", "minus": 10,
                          "tip": "少于 300 字，AI 很难完整回答一个问题。"})
    if length > 5000:
        penalties.append({"key": "long", "name": "篇幅过长", "minus": 5,
                          "tip": "超过 5000 字，建议拆成几篇。"})
    total = sum(item["score"] for item in items) - sum(item["minus"] for item in penalties)
    total = max(0, min(100, total))
    tips = []
    if not answer_hit:
        tips.append("第一段先用一句话回答标题里的问题，再展开。")
    if headings < 2:
        tips.append("用小标题拆成几段，AI 更容易摘取。")
    if digits < 2:
        tips.append("补上可以核对的数据（年份、价格、数量），并说明出处。")
    if risks:
        tips.append("删掉「" + "、".join(sorted({item['quote'] for item in risks})[:3]) + "」这类极限词。")
    return {
        "total": total, "items": items, "penalties": penalties, "tips": tips,
        "notes": ["本地规则评分：首段、结构、数据、品牌、篇幅和极限词；未调用模型。"],
        "model_reviewed": False,
    }


def _sentences(body):
    return [part.strip() for part in re.split(r"(?<=[。！？!?；;])|\n+", body or "")
            if part.strip()]


def _compliance(text):
    hits = []
    for sentence in _sentences(text):
        found = []
        for word, level in RISK_WORDS.items():
            if word in sentence:
                found.append((word, level))
        if not found:
            continue
        for word, level in found:
            hits.append({
                "quote": sentence[:160], "level": level, "type": "极限词 / 绝对化用语",
                "reason": f"出现「{word}」，广告法对绝对化用语有风险。",
                "advice": "改成可核对、有限定条件的说法。",
            })
    return hits[:12]


def _evidence(db, project_id, project, content):
    geo_brand.initialize(db)
    rows = db.execute(
        "SELECT label,value FROM geo_facts WHERE project_id=? AND tenant_id=? "
        "AND status='confirmed' ORDER BY id LIMIT 20",
        (int(project_id), commander.tenant()),
    ).fetchall()
    evidence = [{"id": f"F{index}", "label": f"事实：{row['label']}", "text": row["value"]}
                for index, row in enumerate(rows, 1)]
    knowledge.tables(db)
    sources = db.execute(
        "SELECT name,content FROM knowledge_text WHERE tenant_id=? "
        "AND status IN ('accepted','accepted_resource') ORDER BY id DESC LIMIT 6",
        (commander.tenant(),),
    ).fetchall()
    for index, row in enumerate(sources, 1):
        evidence.append({"id": f"K{index}", "label": f"知识库：{row['name']}",
                         "text": (row["content"] or "")[:1200]})
    if content["brief"]:
        evidence.append({"id": "B1", "label": "写作要求", "text": content["brief"]})
    return evidence


def _claims(body, evidence):
    claims = []
    for sentence in _sentences(body):
        if len(claims) >= 12:
            break
        if not any(hint in sentence for hint in CLAIM_HINTS) \
                and not re.search(r"[0-9０-９]", sentence):
            continue
        numbers = set(re.findall(r"[0-9０-９]{2,}", sentence))
        matched = []
        for item in evidence:
            text = item["text"] or ""
            if numbers and any(number in text for number in numbers):
                matched.append(item["id"])
            elif len(sentence) >= 8 and sentence[:12] in text:
                matched.append(item["id"])
        verdict = "match" if matched else "unverified"
        claims.append({
            "quote": sentence[:160], "verdict": verdict,
            "verdict_name": "有依据" if matched else "待核实",
            "reason": ("和资料里的数字/说法一致。" if matched
                       else "资料里没有找到可以直接核对的依据。"),
            "evidence": matched[:3],
            "suggestion": "" if matched else "删掉具体数字，或补上可核对资料。",
            "resolved": False,
        })
    return claims


def _run_check(db, project_id, project, content):
    evidence = _evidence(db, project_id, project, content)
    claims = _claims(content["body"], evidence)
    compliance = _compliance(content["body"])
    risk = [item for item in compliance if item["level"] == "risk"]
    unverified = [item for item in claims if item["verdict"] == "unverified"]
    if risk:
        decision, name = "block", "必须改"
        reasons = [f"有 {len(risk)} 处极限词或绝对化用语。"]
    elif unverified:
        decision, name = "review", "需要确认"
        reasons = [f"有 {len(unverified)} 处说法没有资料依据。"]
    else:
        decision, name = "pass", "通过"
        reasons = ["关键说法都有资料依据。"]
    detail = {
        "valid": True, "decision": decision, "decision_name": name, "reasons": reasons,
        "claims": claims, "compliance": compliance, "evidence": evidence,
        "approved_by_name": "", "approved_at": "", "approved_reason": "",
    }
    gate = {"valid": True, "decision": decision, "decision_name": name, "reasons": reasons}
    sig = _hash(content["body"])
    return gate, detail, sig, evidence


def _store_check(db, content_id, gate, detail, sig, evidence, status):
    if evidence is None:
        db.execute(
            "UPDATE geo_contents SET gate=?, gate_detail=?, gate_sig=?, status=?, "
            "updated_at=? WHERE id=? AND tenant_id=?",
            (json.dumps(gate, ensure_ascii=False), json.dumps(detail, ensure_ascii=False),
             sig, status, now(), int(content_id), commander.tenant()),
        )
        return
    db.execute(
        "UPDATE geo_contents SET gate=?, gate_detail=?, gate_sig=?, evidence=?, "
        "status=?, updated_at=? WHERE id=? AND tenant_id=?",
        (json.dumps(gate, ensure_ascii=False), json.dumps(detail, ensure_ascii=False),
         sig, json.dumps(evidence, ensure_ascii=False), status, now(),
         int(content_id), commander.tenant()),
    )


# ---------------------------------------------------------------------------
# AI generation and adaptation

def _model_json(raw):
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


def _generation_messages(project, content, evidence, genre, target_names):
    blocks = "\n".join(f"[{item['id']}] {item['label']}：{item['text']}" for item in evidence)
    system = (
        "你是 GEO 内容写作助手。只使用给定的品牌事实、知识库资料和写作要求，"
        "不要编造客户、案例、数字或资质；资料里没有的写「待补充」。"
        "文章要让 AI 愿意引用：第一段直接回答标题里的问题，再用小标题展开，"
        "每段给可以核对的信息。用 Markdown 写正文。只输出一个 JSON 对象："
        "{\"title\":\"标题\",\"body\":\"Markdown 正文\",\"summary\":\"一句话摘要\","
        "\"zh_outline\":[\"中文要点\"]}。title 不超过 120 字，summary 不超过 200 字；"
        "正文 600-1800 字；中文写作时 zh_outline 可为空数组。"
        "引用资料时在句末写 [F1]/[K1] 这类编号，没有资料支撑的句子不要写数字。"
    )
    user = (
        f"品牌：{project['brand_name']}\n行业：{project['industry']}\n"
        f"地区：{project['region']}\n介绍：{project['description'][:800]}\n"
        f"文章类型：{genre['name']}（{genre['for']}）\n"
        f"要回答的问题：{content['question']}\n"
        f"写作要求：{content['brief'] or '（无）'}\n"
        f"打算发到：{'、'.join(target_names) or '（未指定）'}\n\n资料：\n{blocks[:6000]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _generate_worker(tenant, content_id):
    tenancy.activate({"tenant_id": tenant, "user_id": "geo-content",
                      "username": "内容任务", "role": "member"})
    try:
        row = None
        for _ in range(20):
            with commander.database() as db:
                row = db.execute("SELECT * FROM geo_contents WHERE id=? AND tenant_id=?",
                                 (content_id, tenant)).fetchone()
            if row is not None:
                break
            threading.Event().wait(0.05)
        if row is None:
            return
        with commander.database() as db:
            project = _project(db, row["project_id"])
            content = dict(row)
            content.update({"brand_name": project["brand_name"], "industry": project["industry"]})
            evidence = _evidence(db, row["project_id"], project, content)
            genre = _genre(project["market"], row["genre"])
            target_names = [item["name"] for item in _targets(project["market"])
                            if item["key"] in _json(row["platforms"], [])]
            messages = _generation_messages(project, content, evidence, genre, target_names)
        raw = "".join(commander.generate(messages, max_output_tokens=2200)).strip()
        parsed = _model_json(raw)
        body = _text(parsed.get("body"), 20000) or raw
        title = _text(parsed.get("title"), 120) or row["title"] or row["question"][:60]
        summary = _text(parsed.get("summary"), 200)
        outline = parsed.get("zh_outline")
        outline = [str(item)[:200] for item in outline[:12]] if isinstance(outline, list) else []
        missing = []
        if not any(item["id"].startswith("F") for item in evidence):
            missing.append("品牌档案里还没有确认的事实")
        if not any(item["id"].startswith("K") for item in evidence):
            missing.append("知识库还没有可用资料")
        if not body:
            raise commander.ChatError("模型没有返回正文")
        with commander.database() as db:
            db.execute(
                "UPDATE geo_contents SET title=?, body=?, summary=?, zh_outline=?, missing=?, "
                "evidence=?, version=version+1, busy='', busy_name='', error='', "
                "score='', score_sig='', gate='', gate_detail='', gate_sig='', "
                "edited_at=?, updated_at=? WHERE id=? AND tenant_id=?",
                (title, body, summary, json.dumps(outline, ensure_ascii=False),
                 json.dumps(missing, ensure_ascii=False),
                 json.dumps(evidence, ensure_ascii=False), now(), now(),
                 content_id, tenant),
            )
    except BaseException as error:
        with commander.database() as db:
            db.execute(
                "UPDATE geo_contents SET busy='', busy_name='', error=? WHERE id=? AND tenant_id=?",
                (f"初稿没写出来：{str(error)[:300]}", content_id, tenant),
            )


def _start_generate(db, project_id, project, content_id):
    row = _content_row(db, project_id, content_id)
    if row["status"] == "published":
        raise commander.ChatError("已发布的内容不能重写。")
    if row["busy"]:
        return {"ok": True, "content": _content_json(db, project, row, detail=True)}
    db.execute(
        "UPDATE geo_contents SET busy='generate', busy_name='AI 写初稿', error='', "
        "updated_at=? WHERE id=? AND tenant_id=?",
        (now(), int(content_id), commander.tenant()),
    )
    thread = threading.Thread(target=_generate_worker,
                              args=(commander.tenant(), int(content_id)), daemon=True)
    thread.start()
    return {"ok": True, "content": _content_json(
        db, project, _content_row(db, project_id, content_id), detail=True)}


def _adapt_worker(tenant, content_id, platforms):
    tenancy.activate({"tenant_id": tenant, "user_id": "geo-content",
                      "username": "内容任务", "role": "member"})
    try:
        row = None
        for _ in range(20):
            with commander.database() as db:
                row = db.execute("SELECT * FROM geo_contents WHERE id=? AND tenant_id=?",
                                 (content_id, tenant)).fetchone()
            if row is not None:
                break
            threading.Event().wait(0.05)
        if row is None:
            return
        with commander.database() as db:
            project = _project(db, row["project_id"])
            targets = [item for item in _targets(project["market"]) if item["key"] in platforms]
            system = (
                "你是 GEO 内容改写助手。把给定文章改写成各平台能直接发布的一版，"
                "事实、数字、品牌名不能改。只输出一个 JSON 对象，键是平台 key，"
                "值 {\"title\":\"\",\"body\":\"\",\"tags\":[\"\"]}；不要解释或 Markdown。"
            )
            user = json.dumps({
                "title": row["title"], "body": row["body"],
                "platforms": [{"key": item["key"], "name": item["name"],
                               "max_chars": item["max"]} for item in targets],
            }, ensure_ascii=False)[:9000]
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": user}]
        raw = "".join(commander.generate(messages, max_output_tokens=2600)).strip()
        parsed = _model_json(raw)
        variants = _json(row["variants"], {})
        source_sig = _hash(row["body"])
        made = 0
        for target in targets:
            value = parsed.get(target["key"])
            if not isinstance(value, dict):
                continue
            title = _text(value.get("title"), 120) or row["title"]
            body = _text(value.get("body"), 20000)
            if not body:
                continue
            tags = value.get("tags")
            tags = [str(item)[:30] for item in tags[:8]] if isinstance(tags, list) else []
            similarity = round(SequenceMatcher(None, row["body"], body).ratio(), 3)
            risk = sorted({word for word in RISK_WORDS if word in body})
            variants[target["key"]] = {
                "title": title, "body": body, "tags": tags,
                "similarity": similarity, "stale": False,
                "similar_warn": ("和原文太像，发布前最好再改改。" if similarity > 0.92 else ""),
                "risk_words": risk, "adapted_at": now(), "edited_at": "",
                "version": 1, "source_sig": source_sig,
            }
            made += 1
        if not made:
            raise commander.ChatError("模型没有返回可用的平台版本")
        with commander.database() as db:
            db.execute(
                "UPDATE geo_contents SET variants=?, busy='', busy_name='', error='', "
                "updated_at=? WHERE id=? AND tenant_id=?",
                (json.dumps(variants, ensure_ascii=False), now(), content_id, tenant),
            )
    except BaseException as error:
        with commander.database() as db:
            db.execute(
                "UPDATE geo_contents SET busy='', busy_name='', error=? WHERE id=? AND tenant_id=?",
                (f"改写没完成：{str(error)[:300]}", content_id, tenant),
            )


# ---------------------------------------------------------------------------
# Tickets and publications

def _strip_refs(body):
    return re.sub(r"\s*\[[A-Z][0-9]{1,3}\]", "", body or "")


def _plain(body):
    text = _strip_refs(body)
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = re.sub(r"[*_`>]", "", text)
    return text.strip()


def _html(body):
    parts = []
    for line in _strip_refs(body).splitlines():
        line = line.strip()
        if not line:
            continue
        heading = re.match(r"^(#{1,3})\s*(.+)$", line)
        if heading:
            level = min(3, len(heading.group(1)) + 1)
            parts.append(f"<h{level}>{heading.group(2)}</h{level}>")
        elif re.match(r"^[-*]\s+", line):
            parts.append(f"<li>{re.sub(r'^[-*]\\s+', '', line)}</li>")
        else:
            parts.append(f"<p>{line}</p>")
    return "\n".join(parts)


def _create_ticket(db, project_id, project, content_id, body):
    row = _content_row(db, project_id, content_id)
    if not _can_publish(row):
        raise commander.ChatError("这篇内容还没质检通过，不能发布。")
    platform = str(body.get("platform") or body.get("platform_key") or "").strip()
    target = _target(project["market"], platform)
    if target is None:
        raise commander.ChatError("发布平台无效。")
    tips = ["标题带上客户会搜的关键词。", "正文第一段先直接回答。",
            "发布后把公开链接贴回来，下次体检才能看到 AI 有没有引用。"]
    timestamp = now()
    key = db.execute(
        "INSERT INTO geo_tickets (tenant_id,project_id,content_id,platform,platform_name,title,"
        "draft,draft_text,draft_html,draft_md,rich,tips,login_url,login_label,status,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (commander.tenant(), int(project_id), int(content_id), target["key"], target["name"],
         row["title"], _strip_refs(row["body"]), _plain(row["body"]), _html(row["body"]),
         row["body"], 1, json.dumps(tips, ensure_ascii=False), target.get("login_url", ""),
         target.get("login_label", ""), "todo", timestamp, timestamp),
    ).lastrowid
    tenancy.audit_in(db, commander.tenant(), "geo.ticket.create", str(key),
                     f"content={content_id}; platform={target['key']}")
    ticket = db.execute("SELECT * FROM geo_tickets WHERE id=?", (key,)).fetchone()
    return {"ok": True, "ticket": _ticket(ticket)}


def _list_tickets(db, project_id, query):
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = max(1, min(100, int((query.get("page_size") or query.get("pageSize") or ["10"])[0])))
    except ValueError as error:
        raise commander.ChatError("分页参数无效。") from error
    status = (query.get("status") or ["todo"])[0].strip()
    where, values = "WHERE project_id=? AND tenant_id=?", [int(project_id), commander.tenant()]
    if status:
        where += " AND status=?"
        values.append(status)
    rows = db.execute(
        f"SELECT * FROM geo_tickets {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*values, size, (page - 1) * size],
    ).fetchall()
    total = db.execute(f"SELECT COUNT(*) FROM geo_tickets {where}", values).fetchone()[0]
    todo = db.execute(
        "SELECT COUNT(*) FROM geo_tickets WHERE project_id=? AND tenant_id=? AND status='todo'",
        (int(project_id), commander.tenant()),
    ).fetchone()[0]
    return {"ok": True, "items": [_ticket(row) for row in rows], "total": total, "todo": todo}


def _update_ticket(db, ticket_id, body):
    row = db.execute("SELECT * FROM geo_tickets WHERE id=? AND tenant_id=?",
                     (int(ticket_id), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("发布草稿不存在。")
    status = str(body.get("status") or row["status"]).strip()
    if status not in {"todo", "done", "abandoned"}:
        raise commander.ChatError("状态无效。")
    url = _text(body.get("url"), 500) or row["url"]
    if status == "done":
        if not re.match(r"^https?://", url):
            raise commander.ChatError("请填写 http(s) 开头的公开链接。")
        publication = db.execute(
            "SELECT * FROM geo_publications WHERE ticket_id=? AND tenant_id=?",
            (int(ticket_id), commander.tenant()),
        ).fetchone()
        timestamp = now()
        if publication is None:
            db.execute(
                "INSERT INTO geo_publications (tenant_id,project_id,content_id,ticket_id,"
                "channel_kind,platform,platform_name,title,status,url,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (commander.tenant(), row["project_id"], row["content_id"], int(ticket_id),
                 "community", row["platform"], row["platform_name"], row["title"],
                 "published", url, timestamp, timestamp),
            )
        else:
            db.execute(
                "UPDATE geo_publications SET url=?, status='published', title=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (url, row["title"], timestamp, publication["id"], commander.tenant()),
            )
        db.execute(
            "UPDATE geo_contents SET status='published', updated_at=? "
            "WHERE id=? AND tenant_id=?",
            (timestamp, row["content_id"], commander.tenant()),
        )
    if status != "done":
        publication = db.execute(
            "SELECT * FROM geo_publications WHERE ticket_id=? AND tenant_id=?",
            (int(ticket_id), commander.tenant()),
        ).fetchone()
        if publication is not None:
            db.execute(
                "UPDATE geo_publications SET status='offline', updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (now(), publication["id"], commander.tenant()),
            )
            remaining = db.execute(
                "SELECT COUNT(*) FROM geo_publications WHERE content_id=? AND tenant_id=? "
                "AND status='published'",
                (row["content_id"], commander.tenant()),
            ).fetchone()[0]
            if not remaining:
                db.execute(
                    "UPDATE geo_contents SET status='ready', updated_at=? "
                    "WHERE id=? AND tenant_id=?",
                    (now(), row["content_id"], commander.tenant()),
                )
    db.execute(
        "UPDATE geo_tickets SET status=?, url=?, updated_at=? WHERE id=? AND tenant_id=?",
        (status, url, now(), int(ticket_id), commander.tenant()),
    )
    ticket = db.execute("SELECT * FROM geo_tickets WHERE id=?", (int(ticket_id),)).fetchone()
    tenancy.audit_in(db, commander.tenant(), "geo.ticket.update", str(ticket_id), f"status={status}")
    return {"ok": True, "ticket": _ticket(ticket)}


def _list_publications(db, project_id, query):
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = max(1, min(100, int((query.get("page_size") or query.get("pageSize") or ["20"])[0])))
    except ValueError as error:
        raise commander.ChatError("分页参数无效。") from error
    where, values = "WHERE project_id=? AND tenant_id=?", [int(project_id), commander.tenant()]
    channel_kind = (query.get("channel_kind") or [""])[0].strip()
    if channel_kind:
        where += " AND channel_kind=?"
        values.append(channel_kind)
    status = (query.get("status") or [""])[0].strip()
    if status:
        where += " AND status=?"
        values.append(status)
    rows = db.execute(
        f"SELECT * FROM geo_publications {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*values, size, (page - 1) * size],
    ).fetchall()
    total = db.execute(f"SELECT COUNT(*) FROM geo_publications {where}", values).fetchone()[0]
    return {"ok": True, "items": [_publication(row) for row in rows], "total": total}


def _set_publication_url(db, publication_id, body):
    url = _text(body.get("url"), 500)
    if not re.match(r"^https?://", url):
        raise commander.ChatError("请填写 http(s) 开头的链接。")
    changed = db.execute(
        "UPDATE geo_publications SET url=?, updated_at=? WHERE id=? AND tenant_id=?",
        (url, now(), int(publication_id), commander.tenant()),
    )
    if changed.rowcount != 1:
        raise commander.ChatError("发布记录不存在。")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Opportunities and coverage

def _opportunities(db, project_id, project):
    geo_runs.initialize(db)
    run = db.execute(
        "SELECT * FROM geo_runs WHERE project_id=? AND tenant_id=? AND status='done' "
        "ORDER BY id DESC LIMIT 1",
        (int(project_id), commander.tenant()),
    ).fetchone()
    if run is None:
        return {"ok": True, "items": [], "has_run": False}
    _, _, records, _, _ = geo_report.run_context(db, project_id, run["id"])
    questions = geo_report.question_rows(records)
    genre = "g_answer" if project["market"] == "global" else "qa_answer"
    genre_name = _genre(project["market"], genre)["name"]
    contents = db.execute(
        "SELECT id,question_id,source_kind,source_ref FROM geo_contents "
        "WHERE project_id=? AND tenant_id=? AND status<>'archived' ORDER BY id DESC",
        (int(project_id), commander.tenant()),
    ).fetchall()
    by_question = {}
    by_layer = {}
    for row in contents:
        if row["question_id"]:
            by_question.setdefault(row["question_id"], row["id"])
        if row["source_kind"] == "matrix":
            layer = _json(row["source_ref"], {}).get("layer")
            if layer:
                by_layer.setdefault(layer, row["id"])
    items = []
    for item in questions:
        rate = item["mention_rate"]
        if rate >= 1.0 and not item["competitors"]:
            continue
        names = [entry["name"] for entry in item["competitors"]]
        kind = "gap" if item["mentioned_n"] == 0 else "low"
        items.append({
            "key": f"q-{item['question_id']}", "kind": kind,
            "kind_name": "被竞品占住" if kind == "gap" else "提到偏少",
            "question": item["question"], "question_id": item["question_id"],
            "detail": {"question_id": item["question_id"],
                       "sample_n": item["sample_n"], "mentioned_n": item["mentioned_n"],
                       "competitors": names},
            "reason": (f"{item['sample_n']} 条回答里 {item['mentioned_n']} 条提到你"
                       + (f"，竞品：{'、'.join(names)}" if names else "")),
            "suggest_genre": genre, "suggest_genre_name": genre_name,
            "content_id": by_question.get(item["question_id"]),
        })
    geo_matrix.initialize(db)
    terms = db.execute(
        "SELECT layer,text FROM geo_kw_terms WHERE project_id=? AND tenant_id=? ORDER BY id",
        (int(project_id), commander.tenant()),
    ).fetchall()
    grouped = {}
    for row in terms:
        grouped.setdefault(row["layer"], []).append(row["text"])
    for layer in geo_matrix.LAYERS:
        key = layer["key"]
        if not grouped.get(key):
            continue
        items.append({
            "key": f"matrix-{key}", "kind": "matrix", "kind_name": "词库缺口",
            "question": f"{layer['name']}：{len(grouped[key])} 个说法还没集中写过",
            "detail": {"layer": key, "terms": grouped[key][:12]},
            "reason": f"词库矩阵「{layer['name']}」里的说法还没有对应内容",
            "suggest_genre": "howto",
            "suggest_genre_name": _genre(project["market"], "howto")["name"],
            "content_id": by_layer.get(key),
        })
    items.sort(key=lambda item: (item["kind"] != "gap", item["kind"] != "low"))
    return {"ok": True, "items": items[:20], "has_run": True}


def coverage_map(db, project_id):
    """Term text -> covered by a published content, plus the document count."""
    initialize(db)
    rows = db.execute(
        "SELECT c.body FROM geo_contents c JOIN geo_publications p ON p.content_id=c.id "
        "WHERE c.project_id=? AND c.tenant_id=? AND p.status='published' "
        "AND c.tenant_id=? ORDER BY p.id DESC LIMIT 100",
        (int(project_id), commander.tenant(), commander.tenant()),
    ).fetchall()
    return [row["body"] or "" for row in rows]


def publishing_summary(db, project_id, start_iso, end_iso):
    initialize(db)
    rows = db.execute(
        "SELECT * FROM geo_publications WHERE project_id=? AND tenant_id=? "
        "AND status='published' AND created_at>=? AND created_at<? ORDER BY id DESC",
        (int(project_id), commander.tenant(), start_iso, end_iso),
    ).fetchall()
    if not rows:
        return None
    domains = {}
    for row in rows:
        key = row["platform"]
        entry = domains.setdefault(key, {
            "domain": "", "platform_name": row["platform_name"],
            "kind_name": next((item["name"] for item in CHANNEL_KINDS
                               if item["kind"] == row["channel_kind"]), row["channel_kind"]),
            "count": 0,
        })
        entry["count"] += 1
    total = len(rows)
    return {
        "total": total,
        "domains": [{**entry, "share": round(entry["count"] / total, 4)}
                    for entry in domains.values()],
    }


def created_layer_content(db, project_id, layer):
    initialize(db)
    project = _project(db, project_id)
    layer_item = geo_matrix.LAYER_MAP.get(layer)
    if layer_item is None:
        raise commander.ChatError("层次无效。")
    geo_matrix.initialize(db)
    terms = [row["text"] for row in db.execute(
        "SELECT text FROM geo_kw_terms WHERE project_id=? AND tenant_id=? AND layer=? "
        "ORDER BY id LIMIT 12",
        (int(project_id), commander.tenant(), layer),
    ).fetchall()]
    timestamp = now()
    key = db.execute(
        "INSERT INTO geo_contents (tenant_id,project_id,title,question,genre,source_kind,"
        "source_ref,platforms,locale,language,status,created_by_name,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (commander.tenant(), int(project_id),
         f"按「{layer_item['name']}」写一篇", f"客户在「{layer_item['name']}」会怎么找你？",
         "howto", "matrix", json.dumps({"layer": layer, "terms": terms}, ensure_ascii=False),
         "[]", _default_locale(project), _default_locale(project).split("-")[-1],
         "draft", tenancy.current_identity().get("username") or "本地演示",
         timestamp, timestamp),
    ).lastrowid
    return {"ok": True, "content": {"id": key}}


# ---------------------------------------------------------------------------
# Entry points

def read(path, query):
    path = canonical(path)
    with commander.database() as db:
        initialize(db)
        if path == META:
            return _meta((query.get("market") or ["cn"])[0])
        match = CONTENT.fullmatch(path)
        if match:
            row = db.execute("SELECT * FROM geo_contents WHERE id=? AND tenant_id=?",
                             (int(match[1]), commander.tenant())).fetchone()
            if row is None:
                raise commander.ChatError("内容不存在。")
            project = _project(db, row["project_id"])
            return {"ok": True, "content": _content_json(db, project, row, detail=True)}
        match = PROJECT.fullmatch(path)
        if not match:
            raise commander.ChatError("GEO 接口暂不可用。")
        project_id, suffix = int(match[1]), (match[2] or "").strip("/")
        project = _project(db, project_id)
        if suffix == "contents":
            return _list_contents(db, project_id, project, query)
        if suffix == "opportunities":
            return _opportunities(db, project_id, project)
        if suffix == "publications":
            return _list_publications(db, project_id, query)
        if suffix == "tickets":
            return _list_tickets(db, project_id, query)
        if suffix == "media":
            return {"ok": True, "items": [], "types": [], "total": 0}
        if suffix == "media/recommend":
            return {"ok": True, "items": [], "has_sources": False}
        if suffix == "media-orders":
            return {"ok": True, "items": [], "total": 0}
        if suffix == "media-effect":
            return {"ok": True, "days": 180, "items": [], "orders": 0,
                    "answers": 0, "citations": 0}
    raise commander.ChatError("GEO 接口暂不可用。")


def write(method, path, body):
    path = canonical(path)
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        match = PUBLICATION_URL.fullmatch(path)
        if match and method == "PUT":
            return _set_publication_url(db, match[1], body)
        match = TICKET.fullmatch(path)
        if match and method == "PUT":
            return _update_ticket(db, match[1], body)
        match = CONTENT.fullmatch(path)
        if match:
            project_id = db.execute(
                "SELECT project_id FROM geo_contents WHERE id=? AND tenant_id=?",
                (int(match[1]), commander.tenant())).fetchone()
            if project_id is None:
                raise commander.ChatError("内容不存在。")
            project = _project(db, project_id[0])
            if method == "PUT":
                return _update_content(db, project_id[0], project, match[1], body)
            if method == "DELETE":
                return _delete_content(db, project_id[0], match[1])
        match = CONTENT_VARIANT.fullmatch(path)
        if match and method == "PUT":
            project_id = _content_id_project(db, match[1])
            row = _content_row(db, project_id, match[1])
            project = _project(db, project_id)
            variants = _json(row["variants"], {})
            key = match[2]
            if body.get("remove"):
                variants.pop(key, None)
            else:
                target = _target(project["market"], key)
                if target is None:
                    raise commander.ChatError("平台无效。")
                tags = body.get("tags")
                tags = ([str(item)[:30] for item in tags[:8]]
                        if isinstance(tags, list) else [])
                variants[key] = {
                    "title": _text(body.get("title"), 120), "body": _text(body.get("body"), 20000),
                    "tags": tags,
                    "similarity": (variants.get(key) or {}).get("similarity", 0),
                    "stale": False, "similar_warn": "", "risk_words": [],
                    "adapted_at": (variants.get(key) or {}).get("adapted_at", now()),
                    "edited_at": now(), "version": (variants.get(key) or {}).get("version", 1),
                    "source_sig": _hash(row["body"]),
                }
            db.execute("UPDATE geo_contents SET variants=?, updated_at=? WHERE id=? AND tenant_id=?",
                       (json.dumps(variants, ensure_ascii=False), now(), row["id"],
                        commander.tenant()))
            return {"ok": True, "content": _content_json(
                db, project, _content_row(db, project["id"], row["id"]), detail=True)}
        match = CONTENT_ACTION.fullmatch(path)
        if match:
            content_id, action = int(match[1]), match[2]
            project_id = _content_id_project(db, content_id)
            project = _project(db, project_id)
            row = _content_row(db, project_id, content_id)
            if action == "generate":
                return _start_generate(db, project_id, project, content_id)
            if action == "score":
                score = _score({**dict(row), "brand_name": project["brand_name"],
                                "industry": project["industry"]})
                db.execute(
                    "UPDATE geo_contents SET score=?, score_sig=?, updated_at=? "
                    "WHERE id=? AND tenant_id=?",
                    (json.dumps(score, ensure_ascii=False), _hash(row["body"]), now(),
                     content_id, commander.tenant()),
                )
                return {"ok": True, "content": _content_json(
                    db, project, _content_row(db, project_id, content_id), detail=True)}
            if action == "check":
                gate, detail, sig, evidence = _run_check(
                    db, project_id, project, {**dict(row), "brand_name": project["brand_name"],
                                              "industry": project["industry"]})
                score = _score({**dict(row), "brand_name": project["brand_name"],
                                "industry": project["industry"]})
                db.execute(
                    "UPDATE geo_contents SET score=?, score_sig=? WHERE id=? AND tenant_id=?",
                    (json.dumps(score, ensure_ascii=False), _hash(row["body"]),
                     content_id, commander.tenant()),
                )
                status = row["status"]
                if gate["decision"] == "pass":
                    status = "ready"
                elif status == "ready":
                    status = "draft"
                _store_check(db, content_id, gate, detail, sig, evidence, status)
                return {"ok": True, "content": _content_json(
                    db, project, _content_row(db, project_id, content_id), detail=True)}
            if action == "approve":
                gate = _json(row["gate"], {})
                if not gate or row["gate_sig"] != _hash(row["body"]):
                    raise commander.ChatError("正文改过了，请先重新质检。")
                reason = _text(body.get("reason"), 300)
                if len(reason) < 2:
                    raise commander.ChatError("请填写放行理由。")
                detail = _json(row["gate_detail"], {})
                detail.update({
                    "valid": True, "decision": "approved", "decision_name": "人工放行",
                    "approved_by_name": tenancy.current_identity().get("username") or "本地演示",
                    "approved_at": now(), "approved_reason": reason,
                })
                summary = {"valid": True, "decision": "approved", "decision_name": "人工放行",
                           "reasons": detail.get("reasons", [])}
                _store_check(db, content_id, summary, detail, row["gate_sig"], None, "ready")
                tenancy.audit_in(db, commander.tenant(), "geo.content.approve", str(content_id),
                                 reason[:60])
                return {"ok": True, "content": _content_json(
                    db, project, _content_row(db, project_id, content_id), detail=True)}
            if action == "apply-suggestion":
                kind = str(body.get("kind") or "")
                try:
                    index = int(body.get("index"))
                except (TypeError, ValueError) as error:
                    raise commander.ChatError("建议序号无效。") from error
                text = _text(body.get("text"), 500)
                detail = _json(row["gate_detail"], {})
                bucket = detail.get("claims" if kind == "claim" else "compliance") or []
                if index < 0 or index >= len(bucket):
                    raise commander.ChatError("建议不存在，请重新质检。")
                quote = bucket[index].get("quote") or ""
                if quote and quote in row["body"]:
                    new_body = row["body"].replace(quote, text, 1)
                else:
                    raise commander.ChatError("正文里找不到这句话，可能已经改过。")
                db.execute(
                    "UPDATE geo_contents SET body=?, gate='', gate_detail='', gate_sig='', "
                    "score='', score_sig='', status=CASE WHEN status='ready' THEN 'draft' "
                    "ELSE status END, edited_at=?, updated_at=? WHERE id=? AND tenant_id=?",
                    (new_body, now(), now(), content_id, commander.tenant()),
                )
                return {"ok": True, "content": _content_json(
                    db, project, _content_row(db, project_id, content_id), detail=True)}
            if action == "adapt":
                platforms = body.get("platforms")
                if not isinstance(platforms, list) or not platforms:
                    raise commander.ChatError("请选择要改写的平台。")
                valid = [key for key in platforms
                         if isinstance(key, str) and _target(project["market"], key)]
                if not valid:
                    raise commander.ChatError("平台无效。")
                if row["busy"]:
                    return {"ok": True, "content": _content_json(db, project, row, detail=True)}
                db.execute(
                    "UPDATE geo_contents SET busy='adapt', busy_name='改写到平台', error='', "
                    "updated_at=? WHERE id=? AND tenant_id=?",
                    (now(), content_id, commander.tenant()),
                )
                thread = threading.Thread(target=_adapt_worker,
                                          args=(commander.tenant(), content_id, valid),
                                          daemon=True)
                thread.start()
                return {"ok": True, "content": _content_json(
                    db, project, _content_row(db, project_id, content_id), detail=True)}
            if action == "publish":
                return {"ok": False,
                        "error": "自有账号自动发布暂未开放，请用「自己去发」手工发布并贴回链接。"}
            if action == "push":
                return {"ok": True, "ok_push": False,
                        "message": "还没接通自动发布渠道，请用「自己去发」手工发布并贴回链接。"}
            if action == "tickets":
                return _create_ticket(db, project_id, project, content_id, body)
        match = PROJECT.fullmatch(path)
        if not match:
            raise commander.ChatError("GEO 接口暂不可用。")
        project_id, suffix = int(match[1]), (match[2] or "").strip("/")
        project = _project(db, project_id)
        if suffix == "contents" and method == "POST":
            return _create_content(db, project_id, project, body)
        if suffix == "media-orders/estimate":
            return {"ok": True, "estimate": {
                "blockers": ["媒体发稿暂未开放。"], "enough": False, "credits": 0,
                "balance": payments_balance(db),
            }}
        if suffix == "media-orders" and method == "POST":
            raise commander.ChatError("媒体发稿暂未开放。")
    raise commander.ChatError("GEO 接口暂不可用。")


def _content_id_project(db, content_id):
    row = db.execute("SELECT project_id FROM geo_contents WHERE id=? AND tenant_id=?",
                     (int(content_id), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("内容不存在。")
    return row["project_id"]


def payments_balance(db):
    import payments
    return payments.balance_in(db)
