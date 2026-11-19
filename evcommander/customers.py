"""Customer records, follow-ups, and the workspace's lead pool."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import re

import commander
from materials import integer


CRM = "/scrm-api/api/scrm/contacts"
LEADS = "/agent-api/api/acquisition/leads"
CONTACT_PATH = re.compile(
    re.escape(CRM) + r"/([0-9]{1,12})(/followups|/wechat-added|/profile|/research|/history/sync)?"
)
READ = {CRM, LEADS, *(CRM + "/" + key for key in (
    "assignees", "filters", "overview", "stats/analytics", "stats/funnel", "stats/by-channel", "merge/suggestions")),
    LEADS + "/duplicates", LEADS + "/transfer-targets"}
WRITE = {CRM, CRM + "/share", CRM + "/pool/release", CRM + "/pool/claim", CRM + "/merge",
         LEADS + "/save", LEADS + "/share", LEADS + "/transfer", LEADS + "/unmerge",
         LEADS + "/claim", LEADS + "/duplicates/ai-review",
         LEADS + "/pool/release", LEADS + "/pool/claim", LEADS + "/merge", LEADS + "/handover"}
STAGES = {"new": "新线索", "contacted": "已联系", "qualified": "意向明确",
          "negotiating": "洽谈中", "won": "已成交", "lost": "已流失"}
STATUSES = {"contact": "初步接触", "talking": "意向沟通", "quote": "商务谈判",
            "approval": "合同审批", "deal": "已成交", "hold": "暂缓", "lost": "已流失"}
GRADES = {"A": "A级 · 重点意向", "B": "B级 · 意向培育", "C": "C级 · 普通线索", "P": "P级 · 渠道/伙伴"}
OWNER = {"user_id": "1", "name": "当前工作区"}
FIELDS = {"name": 128, "company": 128, "contact_person": 64, "phone": 32, "wechat": 128,
          "email": 128, "referrer": 128, "opportunity": 1000, "region": 128, "expire_at": 32,
          "notes": 2000, "industry": 128, "website": 256}


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS crm_customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, details TEXT NOT NULL, stage TEXT NOT NULL,
        crm_status TEXT NOT NULL, grade TEXT NOT NULL, source TEXT NOT NULL, in_pool INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, next_at TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS crm_followups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,
        method TEXT NOT NULL, content TEXT NOT NULL, next_at TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS crm_followups_by_customer ON crm_followups (customer_id, id)")
    db.execute("""CREATE TABLE IF NOT EXISTS crm_imports (
        token TEXT PRIMARY KEY, rows TEXT NOT NULL, created_at TEXT NOT NULL, result TEXT)""")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def text(value, limit):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise commander.ChatError("客户资料中的字段格式或长度无效。")
    return value.strip()


def choices(mapping):
    return [{"key": key, "label": label} for key, label in mapping.items()]


def contact_data(body, previous):
    details = {key: text(body[key], limit) if key in body else previous.get(key, "")
               for key, limit in FIELDS.items()}
    tags = body.get("tags", previous.get("tags", []))
    if not isinstance(tags, list) or len(tags) > 30:
        raise commander.ChatError("最多可填写 30 个标签。")
    details["tags"] = list(dict.fromkeys(value for tag in tags if (value := text(tag, 40))))
    details["name"] = details["company"] or details["contact_person"] or details["name"]
    if not details["name"] and not (details["phone"] or details["wechat"] or details["email"]):
        raise commander.ChatError("请填写公司、联系人或联系方式。")
    return details


def require_customer(db, key):
    row = db.execute("SELECT * FROM crm_customers WHERE id=?", (key,)).fetchone()
    if row is None:
        raise commander.ChatError("客户已不存在。")
    return row


def contacts(db):
    return db.execute("SELECT * FROM crm_customers ORDER BY updated_at DESC,id DESC").fetchall()


def contact(row):
    details = json.loads(row["details"])
    return {
        **details, "id": row["id"], "stage": row["stage"], "stage_label": STAGES.get(row["stage"], row["stage"]),
        "crm_status": row["crm_status"], "crm_status_label": STATUSES.get(row["crm_status"], row["crm_status"]),
        "grade": row["grade"], "source": row["source"], "source_key": row["source"],
        "source_label": {"manual": "手动录入", "spreadsheet": "表格导入"}.get(row["source"], row["source"]),
        "contact_type": "person", "pool": "pool" if row["in_pool"] else "private", "in_pool": bool(row["in_pool"]),
        "assignee_user_id": "" if row["in_pool"] else OWNER["user_id"],
        "assignee_name": "" if row["in_pool"] else OWNER["name"],
        "created_at": row["created_at"], "first_seen_at": row["created_at"], "updated_at": row["updated_at"], "next_at": row["next_at"],
        "next_label": row["next_at"], "next_follow_at": row["next_at"],
        "connect_state": "private" if details["wechat"] else "untouched",
        "channels": [], "intent_score": 0, "follow_alert": "", "profile": {}, "research": {},
    }


def followups(db, key, limit=-1):
    rows = db.execute("""SELECT * FROM crm_followups WHERE customer_id=?
                         ORDER BY id DESC LIMIT ?""", (key, limit)).fetchall()
    return [{"id": row["id"], "at": row["created_at"], "by_name": OWNER["name"],
             "method": row["method"], "method_label": {"phone": "电话", "wechat": "微信", "other": "其他"}.get(row["method"], row["method"]),
             "content": row["content"], "next_label": row["next_at"]} for row in rows]


def detail(db, key):
    result = contact(require_customer(db, key))
    history = followups(db, key)
    return {**result, "followups": history, "follow_count": len(history), "events": [],
            "leads": [lead(result)], "identities": [], "messages": [], "threads": [],
            "wechat_messages": [], "milestones": []}


def duplicate(db, details, exclude=0):
    phone = re.sub(r"[\s()+-]", "", details["phone"])
    wechat = details["wechat"].casefold()
    email = details["email"].casefold()
    for row in contacts(db):
        if row["id"] == exclude:
            continue
        record = json.loads(row["details"])
        if ((phone and phone == re.sub(r"[\s()+-]", "", record["phone"]))
                or (wechat and wechat == record["wechat"].casefold())
                or (email and email == record["email"].casefold())):
            return row
    return None


def save(db, body, source="manual", key=None):
    row = require_customer(db, key) if key is not None else None
    details = contact_data(body, json.loads(row["details"]) if row else {})
    match = duplicate(db, details, key or 0)
    if match:
        if row:
            raise commander.ChatError("此联系方式已属于其他客户。")
        return {"id": match["id"], "created": False}
    stage = text(body.get("stage") or (row["stage"] if row else "new"), 32)
    status = text(body.get("crm_status") or (row["crm_status"] if row else "contact"), 32)
    grade = text(body.get("grade") or (row["grade"] if row else "B"), 4)
    if stage not in STAGES or status not in STATUSES or grade not in GRADES:
        raise commander.ChatError("阶段、客户状态或等级无效。")
    timestamp = now()
    payload = json.dumps(details, ensure_ascii=False)
    if row:
        db.execute("""UPDATE crm_customers SET details=?,stage=?,crm_status=?,grade=?,updated_at=?
                      WHERE id=?""", (payload, stage, status, grade, timestamp, key))
        return detail(db, key)
    key = db.execute("""INSERT INTO crm_customers (details,stage,crm_status,grade,source,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?)""", (payload, stage, status, grade, source, timestamp, timestamp)).lastrowid
    return {"id": key, "created": True}


def page_items(rows, args, size_key):
    page = max(1, integer(args.get("page") or 1, 100000))
    size = max(1, integer(args.get(size_key) or 20, 100))
    return rows[(page - 1) * size:page * size]


def matches(record, args):
    keyword = args.get("keyword", "").strip().casefold()
    if keyword and not any(keyword in str(record.get(key, "")).casefold() for key in
                           ("company", "name", "contact_person", "phone", "wechat", "email", "referrer", "tags")):
        return False
    if args.get("pool") in {"pool", "private"} and args["pool"] != record["pool"]:
        return False
    if any(args.get(key) and args[key] != record.get(key) for key in
           ("stage", "crm_status", "source", "grade", "contact_type", "region")):
        return False
    if args.get("only_contact") == "1" and not (record["phone"] or record["wechat"] or record["email"]):
        return False
    return not ((args.get("assignee") == "me" and record["in_pool"])
                or (args.get("assignee") and args["assignee"] not in {"me", "all", "1", "unassigned"}))


def lead(record):
    return {**record, "nickname": record["contact_person"] or record["name"],
            "customer_id": record["id"], "contact_id": record["id"],
            "status": "new" if record["stage"] == "new" else "handed_over",
            "content": record["opportunity"], "platform": "", "source_detail": record["company"]}


def statistics(records):
    return {"total": len(records), "public_pool": sum(record["in_pool"] for record in records),
            "new": sum(record["stage"] == "new" for record in records),
            "handed_over": sum(record["stage"] != "new" for record in records)}


def read(path, query):
    args = {key: values[0] for key, values in query.items()}
    with commander.database() as db:
        initialize(db)
        target = CONTACT_PATH.fullmatch(path)
        if target:
            return {"data": detail(db, int(target[1]))}
        if path == CRM + "/assignees":
            return {"data": [OWNER]}
        if path == LEADS + "/transfer-targets":
            return {"list": [OWNER]}
        records = [contact(row) for row in contacts(db)]
        stats = statistics(records)
        if path == LEADS:
            selected = [lead(record) for record in records if matches(record, args)]
            status = args.get("status")
            selected = [record for record in selected
                        if (not status or record["status"] == status)
                        and (not args.get("date_from") or record["created_at"][:10] >= args["date_from"])
                        and (not args.get("date_to") or record["created_at"][:10] <= args["date_to"])]
            if args.get("dup_only") == "1":
                selected = []
            selected.sort(key=lambda record: (record["created_at"], record["id"]),
                          reverse=args.get("sort") != "time_asc")
            return {"leads": page_items(selected, args, "page_size"), "total": len(selected),
                    "stats": stats, "is_admin": True, "shared": True}
        if path == CRM:
            filtered = [record for record in records if matches(record, args)]
            result = page_items(filtered, args, "limit")
            for record in result:
                history = followups(db, record["id"], 1)
                if history:
                    record.update(last_follow_at=history[0]["at"], last_follow_note=history[0]["content"],
                                  last_follow_method_label=history[0]["method_label"])
            return {"data": result, "total": len(filtered), "is_enterprise_admin": True,
                    "stages": choices(STAGES), "crm_statuses": choices(STATUSES), "grades": choices(GRADES),
                    "realms": [], "channels": []}
        if path.endswith("/overview"):
            return {"data": {**stats, "is_enterprise_admin": True}}
        if path.endswith("/filters"):
            return {"data": {"sources": [{"key": "manual", "label": "手动录入"},
                                         {"key": "spreadsheet", "label": "表格导入"}],
                             "regions": sorted({record["region"] for record in records if record["region"]})}}
        if path.endswith("/funnel"):
            tally = Counter(record["stage"] for record in records)
            return {"data": {"total": len(records), "stages": [
                {"key": key, "label": label, "current": tally[key],
                 "reached": sum(tally[phase] for phase in list(STAGES)[index:] if phase != "lost") if key != "lost" else tally[key]}
                for index, (key, label) in enumerate(STAGES.items())]}}
        if path.endswith("/by-channel"):
            return {"data": []}
        if path == LEADS + "/duplicates":
            return {"items": [], "total": 0}
        if path.endswith("/suggestions"):
            return {"data": []}
        if path.endswith("/analytics"):
            days = integer(args.get("days", "30"), 365)
            return {"data": analytics(db, records, days)}
        raise commander.ChatError("该客户操作暂不可用。")


def selected_ids(body, field):
    values = body.get(field)
    if not isinstance(values, list) or not 1 <= len(values) <= 200:
        raise commander.ChatError("请选择要操作的记录。")
    return list(dict.fromkeys(map(integer, values)))


def analytics(db, records, days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat() if days else ""
    period = [record for record in records if record["created_at"][:10] >= cutoff]
    new_by_day = Counter(record["created_at"][:10] for record in period)
    touched_by_day = dict(db.execute("""SELECT substr(created_at,1,10),count(DISTINCT customer_id)
                                        FROM crm_followups WHERE substr(created_at,1,10)>=? GROUP BY 1""", (cutoff,)))
    touched = {row[0] for row in db.execute("SELECT DISTINCT customer_id FROM crm_followups")}
    connect = Counter()
    for record in records:
        key = record["crm_status"]
        group = (key if key in {"deal", "lost"} else "private" if record["wechat"]
                 else "talking" if key in {"talking", "quote", "approval"}
                 else "touched" if record["id"] in touched else "untouched")
        connect[group] += 1
    sources = Counter((record["source"], record["source_label"]) for record in period)
    regions = Counter(record["region"] for record in period if record["region"])
    return {
        "total": len(records), "period_total": len(period), "region_known": sum(regions.values()),
        "reach": {"reached": 0, "replied": 0}, "alerts": {"due": 0, "overdue": 0, "hot_untouched": 0},
        "insights": [], "regions": [{"name": name, "count": count} for name, count in regions.most_common()],
        "intent": [{"level": 0, "label": "未评估", "count": len(period)}],
        "connect": [{"key": key, "label": label, "count": connect[key]} for key, label in
                    {"untouched": "未触达", "touched": "已跟进", "talking": "沟通中",
                     "private": "已加微", "deal": "已成交", "lost": "已流失"}.items()],
        "sources": [{"key": key, "label": label, "total": total, "intent": 0, "intent_rate": 0}
                    for (key, label), total in sources.most_common()],
        "trend": [{"day": day, "new": new_by_day[day], "followed": touched_by_day.get(day, 0), "converted": 0}
                  for day in sorted(new_by_day.keys() | touched_by_day.keys())],
    }


def save_leads(db, body):
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise commander.ChatError("请选择要存入线索池的线索。")
    source = text(body.get("source") or "acquisition", 64)
    platform = text(body.get("platform") or "", 128)
    created = existing = 0
    for item in items[:200]:
        if not isinstance(item, dict):
            continue
        name = text(item.get("name") or item.get("company") or "", 128)
        phone = text(item.get("phone") or "", 32)
        if not name and not phone:
            continue
        extra = item.get("extra")
        notes = json.dumps(extra, ensure_ascii=False) if isinstance(extra, dict) else ""
        payload = {
            "name": name,
            "company": text(item.get("company") or name, 128),
            "contact_person": text(item.get("contact_person") or name, 64),
            "phone": phone,
            "wechat": text(item.get("wechat") or "", 128),
            "email": text(item.get("email") or "", 128),
            "referrer": text(item.get("source_detail") or platform, 128),
            "opportunity": text(item.get("content") or "", 1000),
            "region": text(item.get("address") or item.get("region") or "", 128),
            "website": text(item.get("profile_url") or item.get("website") or "", 256),
            "industry": text(item.get("industry") or "", 128),
            "notes": text(notes or item.get("content") or "", 2000),
        }
        result = save(db, payload, source=source)
        if result.get("created"):
            created += 1
        else:
            existing += 1
    return {"ok": True, "ingested": created, "existing": existing,
            "message": f"已存入线索池 {created} 条" + (f"，{existing} 条之前就在池里" if existing else "")}


def write(method, path, body):
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        if path == CRM:
            return {"data": save(db, body)}
        if path == LEADS + "/save":
            return save_leads(db, body)
        if path == LEADS + "/duplicates/ai-review":
            return {"ok": False, "error": "AI 去重暂未开放"}
        if path == LEADS + "/share" or path == CRM + "/share":
            if not (body.get("lead_ids") or body.get("contact_ids") or body.get("shared_scope")):
                raise commander.ChatError("请选择要共享的线索。")
            return {"ok": False, "error": "线索共享暂未开放"}
        if path == LEADS + "/unmerge":
            return {"ok": False, "error": "取消合并暂未开放"}
        if path == LEADS + "/transfer":
            to_user = str(body.get("to_user_id") or OWNER["user_id"])
            if to_user not in {OWNER["user_id"], "1", ""}:
                return {"ok": False, "error": "多用户转让暂未开放"}
            ids = selected_ids(body, "lead_ids")
            for key in ids:
                require_customer(db, key)
                db.execute("UPDATE crm_customers SET in_pool=0,stage='contacted',updated_at=? WHERE id=?",
                           (now(), key))
            return {"ok": True, "moved": len(ids), "to_name": OWNER["name"], "denied": 0}
        if path == LEADS + "/claim":
            ids = selected_ids(body, "lead_ids")
            for key in ids:
                require_customer(db, key)
                db.execute("UPDATE crm_customers SET in_pool=0,updated_at=? WHERE id=?", (now(), key))
            return {"ok": True, "moved": len(ids), "note": "已认领线索"}
        target = CONTACT_PATH.fullmatch(path)
        if target:
            key = int(target[1])
            row = require_customer(db, key)
            action = target[2]
            if action in {"/wechat-added", "/profile", "/research", "/history/sync"}:
                if action == "/wechat-added":
                    values = json.loads(row["details"])
                    values["wechat"] = values.get("wechat") or "已添加"
                    db.execute("UPDATE crm_customers SET details=?,updated_at=? WHERE id=?",
                               (json.dumps(values, ensure_ascii=False), now(), key))
                    return {"ok": True, "message": "已标记微信已添加"}
                if action == "/history/sync":
                    return {"ok": False, "error": "外部聊天记录同步暂未开放"}
                return {"ok": False, "error": "外部企业调研暂未接入"}
            if method == "PUT":
                return {"data": save(db, body, key=key)}
            if method == "DELETE":
                db.execute("DELETE FROM crm_followups WHERE customer_id=?", (key,))
                db.execute("DELETE FROM crm_customers WHERE id=?", (key,))
                return {"ok": True, "note": "已删除客户"}
            method_name = text(body.get("method", "other"), 32)
            content = text(body.get("content", ""), 3000)
            next_at = text(body.get("next_at", row["next_at"]), 32)
            if not content and "next_at" not in body:
                raise commander.ChatError("请填写跟进内容。")
            timestamp = now()
            if content or next_at:
                db.execute("""INSERT INTO crm_followups (customer_id,method,content,next_at,created_at)
                              VALUES (?,?,?,?,?)""", (key, method_name, content, next_at, timestamp))
            status = text(body.get("crm_status") or row["crm_status"], 32)
            if status not in STATUSES:
                raise commander.ChatError("客户状态无效。")
            stage = "contacted" if content and row["stage"] == "new" else row["stage"]
            db.execute("UPDATE crm_customers SET next_at=?,crm_status=?,stage=?,updated_at=? WHERE id=?",
                       (next_at, status, stage, timestamp, key))
            return {"data": detail(db, key)}
        is_lead = path.startswith(LEADS)
        if "/pool/" in path:
            ids = selected_ids(body, "lead_ids" if is_lead else "contact_ids")
            release = int(path.endswith("/release"))
            for key in ids:
                require_customer(db, key)
                db.execute("UPDATE crm_customers SET in_pool=?,updated_at=? WHERE id=?", (release, now(), key))
            return {"ok": True, "moved": len(ids), "note": "已更新线索池"}
        if path == LEADS + "/handover":
            to_user = str(body.get("to_user_id") or OWNER["user_id"])
            if to_user not in {OWNER["user_id"], "1", ""}:
                return {"ok": False, "error": "多用户移交暂未开放"}
            ids = selected_ids(body, "lead_ids")
            for key in ids:
                require_customer(db, key)
                db.execute("UPDATE crm_customers SET in_pool=0,stage='contacted',updated_at=? WHERE id=?", (now(), key))
            return {"ok": True, "handed": len(ids), "to_name": OWNER["name"]}
        if path.endswith("/merge"):
            key = integer(body.get("keep_id"))
            drops = selected_ids(body, "drop_ids") if is_lead else [integer(body.get("drop_id"))]
            if key in drops:
                raise commander.ChatError("请选择不同的客户。")
            keep = require_customer(db, key)
            entries = [require_customer(db, entry) for entry in drops]
            values = json.loads(keep["details"])
            for entry in entries:
                for field, value in json.loads(entry["details"]).items():
                    if field == "tags":
                        values[field] = list(dict.fromkeys(values[field] + value))[:30]
                    elif not values[field]:
                        values[field] = value
                db.execute("UPDATE crm_followups SET customer_id=? WHERE customer_id=?", (key, entry["id"]))
                db.execute("DELETE FROM crm_customers WHERE id=?", (entry["id"],))
            db.execute("UPDATE crm_customers SET details=?,updated_at=? WHERE id=?",
                       (json.dumps(values, ensure_ascii=False), now(), key))
            return {"ok": True, "merged": len(drops)}
        raise commander.ChatError("该客户操作暂不可用。")
