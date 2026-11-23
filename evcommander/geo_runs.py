"""GEO question library, simulated runs, answer parsing and matrix results.

The real AI-search providers are not connected yet. Runs use the configured
text model as a clearly labelled simulation: engine names carry "（模拟）" and
answer details carry a method note. The frontend needs no changes.
"""

from datetime import datetime, timezone
import json
import re
import threading
import time

import commander
import geo_brand
import payments
import tenancy


PROJECT = re.compile(r"/api/geo/projects/([0-9]{1,16})(?:/(.*))?$")
RUN = re.compile(r"/api/geo/runs/([0-9]{1,16})(?:/(.*))?$")
ANSWER = re.compile(r"/api/geo/answers/([0-9]{1,16})$")
INTENTS = {
    "purchase": "购买决策", "compare": "品牌对比", "recommend": "推荐清单",
    "price": "价格咨询", "usage": "使用场景",
}
ENGINES = {
    "doubao": "豆包（模拟）", "deepseek": "DeepSeek（模拟）", "yuanbao": "腾讯元宝（模拟）",
    "kimi": "Kimi（模拟）", "chatgpt": "ChatGPT（模拟）", "gemini": "Gemini（模拟）",
    "claude": "Claude（模拟）", "perplexity": "Perplexity（模拟）",
}
METHOD_NOTE = "当前为文本模型模拟，不代表真实 AI 搜索结果；接入真实引擎后会替换。"
MAX_QUESTIONS = 40
MAX_RUN_ANSWERS = 8


def canonical(path):
    if path.startswith("/agent-api"):
        path = path[len("/agent-api"):]
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def can_read(path):
    path = canonical(path)
    if ANSWER.fullmatch(path):
        return True
    match = PROJECT.fullmatch(path)
    if match and match[2] in {"questions", "runs", "matrix", "layers", "answers", "reports", "diagnoses"}:
        return True
    match = RUN.fullmatch(path)
    return bool(match and not match[2])


def can_write(path):
    path = canonical(path)
    match = PROJECT.fullmatch(path)
    if match:
        suffix = match[2] or ""
        return suffix in {"questions", "questions/generate", "questions/delete", "runs", "runs/estimate"} \
            or bool(re.fullmatch(r"questions/[0-9]{1,16}", suffix)) \
            or suffix in {"runs/retry", "runs/retry/estimate"}
    match = RUN.fullmatch(path)
    if match:
        return (match[2] or "") in {"cancel", "retry", "retry/estimate"}
    return False


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        text TEXT NOT NULL, intent TEXT NOT NULL DEFAULT 'purchase', locale TEXT NOT NULL DEFAULT 'zh',
        layer TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
        UNIQUE(project_id, text)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', engines TEXT NOT NULL DEFAULT '[]',
        samples INTEGER NOT NULL DEFAULT 1, total INTEGER NOT NULL DEFAULT 0,
        done INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
        credits REAL NOT NULL DEFAULT 0, charge_id INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        method_note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        started_at TEXT NOT NULL DEFAULT '', finished_at TEXT NOT NULL DEFAULT ''
    )""")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(geo_runs)").fetchall()}
    if "charge_id" not in columns:
        connection.execute("ALTER TABLE geo_runs ADD COLUMN charge_id INTEGER NOT NULL DEFAULT 0")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        run_id INTEGER NOT NULL, question_id INTEGER NOT NULL,
        engine_key TEXT NOT NULL, engine_name TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )""")


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
    except ValueError:
        pass
    return [name for name in dict.fromkeys(names) if name]


def _question(row):
    return {
        "id": row["id"], "text": row["text"], "intent": row["intent"],
        "intent_name": INTENTS.get(row["intent"], "购买决策"), "locale": row["locale"],
        "layer": row["layer"], "layer_name": "", "active": bool(row["active"]),
        "priority": 0, "created_at": row["created_at"],
    }


def _run(row):
    try:
        engine_keys = json.loads(row["engines"])
    except ValueError:
        engine_keys = []
    return {
        "id": row["id"], "project_id": row["project_id"], "status": row["status"],
        "status_name": {"queued": "排队中", "running": "体检中", "done": "已完成",
                        "failed": "失败", "cancelled": "已停止"}.get(row["status"], row["status"]),
        "trigger_name": "手动", "created_at": row["created_at"],
        "started_at": row["started_at"], "finished_at": row["finished_at"],
        "total": row["total"], "done": row["done"], "failed": row["failed"],
        "credits": row["credits"], "samples": row["samples"], "error": row["error"],
        "retryable": row["failed"], "method_note": row["method_note"],
        "engines": [{"key": key, "name": ENGINES.get(key, key)} for key in engine_keys],
    }


def _engines(value):
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list):
        value = []
    result = [str(item) for item in value if isinstance(item, str) and item.strip()]
    return list(dict.fromkeys(result))[:3]


def _question_count(db, project_id):
    return db.execute(
        "SELECT COUNT(*) FROM geo_questions WHERE project_id=? AND active=1 AND tenant_id=?",
        (project_id, commander.tenant())
    ).fetchone()[0]


def counts(db, project_id):
    initialize(db)
    brand = geo_brand.counts(db, project_id)
    return {
        "questions": _question_count(db, project_id),
        "competitors": brand["competitors"],
        "facts_confirmed": brand["facts_confirmed"],
        "runs": db.execute(
            "SELECT COUNT(*) FROM geo_runs WHERE project_id=? AND tenant_id=?",
            (project_id, commander.tenant())
        ).fetchone()[0],
    }


def _list_questions(db, project_id, query):
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = max(1, min(100, int((query.get("page_size") or query.get("pageSize") or ["20"])[0])))
    except ValueError as error:
        raise commander.ChatError("分页参数无效。") from error
    where, values = "WHERE project_id=? AND tenant_id=?", [int(project_id), commander.tenant()]
    for key in ("intent", "locale", "layer"):
        value = (query.get(key) or [""])[0]
        if value:
            where += f" AND {key}=?"
            values.append(value)
    active = (query.get("active") or [""])[0]
    if active in {"0", "1"}:
        where += " AND active=?"
        values.append(int(active))
    keyword = (query.get("keyword") or [""])[0].strip()
    if keyword:
        where += " AND instr(lower(text), lower(?)) > 0"
        values.append(keyword)
    rows = db.execute(
        f"SELECT * FROM geo_questions {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*values, size, (page - 1) * size],
    ).fetchall()
    total = db.execute(f"SELECT COUNT(*) FROM geo_questions {where}", values).fetchone()[0]
    active_count = _question_count(db, project_id)
    return {
        "ok": True, "items": [_question(row) for row in rows], "total": total,
        "active": active_count, "limit": 200,
        "intents": [{"key": key, "label": label} for key, label in INTENTS.items()],
        "layers": [], "locales": [{"key": "zh", "name": "中文"}],
    }


def _add_questions(db, project_id, items, source="manual"):
    if not isinstance(items, list) or not items:
        raise commander.ChatError("请选择要加入问题库的问题。")
    created = 0
    timestamp = now()
    for item in items[:MAX_QUESTIONS]:
        if isinstance(item, str):
            text = item.strip()
            intent, locale = "purchase", "zh"
        elif isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            intent = str(item.get("intent") or "purchase")
            locale = str(item.get("locale") or "zh")
        else:
            continue
        if not text or len(text) > 300:
            continue
        if intent not in INTENTS:
            intent = "purchase"
        changed = db.execute(
            "INSERT OR IGNORE INTO geo_questions (tenant_id,project_id,text,intent,locale,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (commander.tenant(), int(project_id), text, intent, locale[:16], timestamp),
        )
        created += changed.rowcount
    tenancy.audit_in(db, commander.tenant(), "geo.questions.add", str(project_id),
                     f"added={created}; source={source}")
    return {"ok": True, "added": created, "source": source}


def _json_items(raw):
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        parsed = json.loads(text)
    except ValueError:
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except ValueError:
            return []
    if isinstance(parsed, dict):
        parsed = parsed.get("items") or parsed.get("questions") or []
    if not isinstance(parsed, list):
        return []
    result = []
    for item in parsed:
        if isinstance(item, str):
            result.append(item.strip())
        elif isinstance(item, dict):
            value = str(item.get("text") or item.get("question") or "").strip()
            if value:
                result.append(value)
    return [item for item in result if item][:MAX_QUESTIONS]


def _generate_questions(db, project, body):
    try:
        count = max(1, min(40, int(body.get("count") or 15)))
    except (TypeError, ValueError) as error:
        raise commander.ChatError("生成数量无效。") from error
    locale = str(body.get("locale") or "zh")[:16]
    topic = project["industry"] or project["brand_name"]
    system = (
        "你是市场调研助手。站在客户角度，写出他们向 AI 搜索/助手提出的购买决策问题。"
        "大部分问题不要出现品牌名，只输出 JSON 字符串数组，不要解释或 Markdown。"
    )
    user = f"行业/品类：{topic}\n品牌：{project['brand_name']}\n请生成 {count} 个不同的中文问题。"
    try:
        raw = "".join(commander.generate([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ], max_output_tokens=1200)).strip()
        texts = _json_items(raw)
    except commander.ChatError:
        texts = []
    if not texts:
        base = topic or "这个品类"
        templates = [
            f"{base}哪个品牌好？", f"{base}怎么选？", f"{base}有哪些推荐品牌？",
            f"{base}选购要注意什么？", f"{base}性价比高的有哪些？",
            f"第一次买{base}，应该看哪些指标？", f"{base}的常见坑有哪些？",
        ]
        texts = templates[:count]
    existing = {
        row["text"] for row in db.execute(
            "SELECT text FROM geo_questions WHERE project_id=? AND tenant_id=?",
            (project["id"], commander.tenant())
        ).fetchall()
    }
    items = [{"text": text, "intent": "purchase", "locale": locale, "exists": text in existing}
             for text in texts[:count]]
    return {"ok": True, "items": items, "customers": [], "credits": 0}


def _estimate(db, project_id, body):
    engines = _engines(body.get("engines")) or ["doubao", "deepseek"]
    try:
        samples = max(1, min(3, int(body.get("samples") or 1)))
    except (TypeError, ValueError):
        samples = 1
    questions = min(
        _question_count(db, project_id),
        max(1, MAX_RUN_ANSWERS // (len(engines) * samples)),
    )
    blockers = []
    if not questions:
        blockers.append("问题库至少要有 1 个启用的问题。")
    if not engines:
        blockers.append("至少选择一个 AI 引擎。")
    answers = questions * samples
    credits = round(answers * len(engines) * payments.geo_answer_price(), 4)
    balance = payments.balance_in(db)
    return {
        "ok": True,
        "estimate": {
            "questions": questions, "samples": samples, "credits": credits,
            "balance": balance, "enough": not blockers and balance >= credits,
            "blockers": blockers,
            "skipped_engines": [],
            "engines": [{"key": key, "name": ENGINES.get(key, key), "answers": answers} for key in engines],
        },
    }


def _start_run(db, project_id, body):
    project = _project(db, project_id)
    engines = _engines(body.get("engines")) or ["doubao", "deepseek"]
    if not engines:
        raise commander.ChatError("至少选择一个 AI 引擎。")
    try:
        samples = max(1, min(3, int(body.get("samples") or 1)))
    except (TypeError, ValueError):
        samples = 1
    question_limit = max(1, MAX_RUN_ANSWERS // (len(engines) * samples))
    questions = db.execute(
        "SELECT * FROM geo_questions WHERE project_id=? AND active=1 AND tenant_id=? "
        "ORDER BY id ASC LIMIT ?",
        (project_id, commander.tenant(), question_limit),
    ).fetchall()
    if not questions:
        raise commander.ChatError("问题库至少要有 1 个启用的问题。")
    total = len(questions) * len(engines) * samples
    timestamp = now()
    owner = commander.tenant()
    run_id = db.execute(
        "INSERT INTO geo_runs (tenant_id,project_id,status,engines,samples,total,method_note,created_at,started_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (owner, project_id, "running", json.dumps(engines), samples, total,
         METHOD_NOTE, timestamp, timestamp),
    ).lastrowid
    tenancy.audit_in(db, owner, "geo.run.start", str(run_id), f"project={project_id}; total={total}")
    reserved = round(total * payments.geo_answer_price(), 4)
    charge_id = payments.charge_in(db, reserved, f"GEO 体检 #{run_id}", str(run_id))
    if charge_id:
        db.execute("UPDATE geo_runs SET charge_id=? WHERE id=? AND tenant_id=?",
                   (charge_id, run_id, owner))
    run = _run(db.execute("SELECT * FROM geo_runs WHERE id=? AND tenant_id=?",
                          (run_id, owner)).fetchone())
    thread = threading.Thread(
        target=_execute_run,
        args=(owner, run_id, [row["id"] for row in questions], engines, samples, _brands(project)),
        daemon=True,
    )
    thread.start()
    return {"ok": True, "run": run}


def _execute_run(tenant, run_id, question_ids, engines, samples, brands):
    # This daemon thread runs outside the request that started the run.
    tenancy.activate({"tenant_id": tenant, "user_id": "geo-worker",
                      "username": "体检任务", "role": "member"})
    try:
        for engine in engines:
            engine_name = ENGINES.get(engine, engine)
            for question_id in question_ids:
                for attempt in range(samples):
                    with commander.database() as db:
                        question = db.execute(
                            "SELECT text FROM geo_questions WHERE id=? AND tenant_id=?",
                            (question_id, tenant)).fetchone()
                        run = db.execute("SELECT status FROM geo_runs WHERE id=? AND tenant_id=?",
                                         (run_id, tenant)).fetchone()
                        if question is None or run is None or run["status"] == "cancelled":
                            continue
                    system = (
                        "你是一个通用 AI 搜索助手。请直接回答客户的问题，给出你认为值得推荐的品牌和理由。"
                        "只输出回答正文，不要提到你在模拟。"
                    )
                    user = f"客户问题：{question['text']}"
                    try:
                        text = "".join(commander.generate([
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ], max_output_tokens=260)).strip()
                        status, error = ("ok", "") if text else ("failed", "模型没有返回回答")
                    except commander.ChatError as exc:
                        text, status, error = "", "failed", str(exc)
                    with commander.database() as db:
                        db.execute(
                            "INSERT INTO geo_answers (tenant_id,project_id,run_id,question_id,engine_key,engine_name,"
                            "attempt,status,text,error,created_at) "
                            "SELECT tenant_id,project_id,?,?,?,?,?,?,?,?,? FROM geo_runs WHERE id=? AND tenant_id=?",
                            (run_id, question_id, engine, engine_name, attempt, status, text, error,
                             now(), run_id, tenant),
                        )
                        if status == "ok":
                            db.execute("UPDATE geo_runs SET done=done+1 WHERE id=? AND tenant_id=?",
                                       (run_id, tenant))
                        else:
                            db.execute("UPDATE geo_runs SET failed=failed+1 WHERE id=? AND tenant_id=?",
                                       (run_id, tenant))
        with commander.database() as db:
            row = db.execute("SELECT done,status,charge_id FROM geo_runs WHERE id=? AND tenant_id=?",
                             (run_id, tenant)).fetchone()
            if row:
                actual = round(float(row["done"]) * payments.geo_answer_price(), 4)
                payments.adjust_in(db, row["charge_id"], actual,
                                   f"GEO 体检 #{run_id}（{row['done']} 条回答）")
                if row["status"] != "cancelled":
                    db.execute(
                        "UPDATE geo_runs SET status='done',credits=?,finished_at=? WHERE id=? AND tenant_id=?",
                        (actual, now(), run_id, tenant),
                    )
    except BaseException as error:
        with commander.database() as db:
            row = db.execute("SELECT done,charge_id FROM geo_runs WHERE id=? AND tenant_id=?",
                             (run_id, tenant)).fetchone()
            credits = 0
            if row:
                credits = round(float(row["done"]) * payments.geo_answer_price(), 4)
                payments.adjust_in(db, row["charge_id"], credits,
                                   f"GEO 体检 #{run_id}（{row['done']} 条回答）")
            db.execute(
                "UPDATE geo_runs SET status='failed',credits=?,error=?,finished_at=? WHERE id=? AND tenant_id=?",
                (credits, str(error)[:500], now(), run_id, tenant),
            )


def _answer_detail(db, answer_id):
    tenant = commander.tenant()
    row = db.execute(
        "SELECT a.*, q.text AS question_text, r.method_note FROM geo_answers a "
        "JOIN geo_questions q ON q.id=a.question_id JOIN geo_runs r ON r.id=a.run_id "
        "WHERE a.id=? AND a.tenant_id=? AND q.tenant_id=? AND r.tenant_id=?",
        (answer_id, tenant, tenant, tenant),
    ).fetchone()
    if row is None:
        raise commander.ChatError("回答不存在。")
    project = _project(db, row["project_id"])
    text = row["text"]
    mentions = []
    folded = text.casefold()
    for brand in _brands(project):
        needle = brand.casefold()
        start = 0
        offsets = []
        while needle:
            index = folded.find(needle, start)
            if index < 0:
                break
            offsets.append([index, index + len(brand)])
            start = index + len(needle)
        if offsets:
            mentions.append({
                "id": f"{row['id']}-{len(mentions)}", "brand_kind": "self",
                "brand_name": brand, "rank_no": None,
                "sentiment": "neu", "sentiment_name": "中性", "offsets": offsets,
                "corrected": False, "facts": [], "reason": "",
            })
    return {
        "ok": True,
        "answer": {
            "id": row["id"], "question": row["question_text"],
            "engine": {"key": row["engine_key"], "name": row["engine_name"]},
            "created_at": row["created_at"], "text": text, "mentions": mentions,
            "citations": [],
            "status": row["status"], "error": row["error"], "purged": False,
            "method_note": row["method_note"],
        },
    }


def _matrix(db, project_id, query):
    project = _project(db, project_id)
    try:
        run_id = int((query.get("run_id") or ["0"])[0] or 0)
    except ValueError:
        run_id = 0
    tenant = commander.tenant()
    if run_id:
        run_row = db.execute(
            "SELECT * FROM geo_runs WHERE id=? AND project_id=? AND tenant_id=?",
            (run_id, project_id, tenant)).fetchone()
    else:
        run_row = db.execute(
            "SELECT * FROM geo_runs WHERE project_id=? AND tenant_id=? ORDER BY id DESC LIMIT 1",
            (project_id, tenant)
        ).fetchone()
    if run_row is None:
        return {"ok": True, "run": None, "rows": [], "total": 0, "engines": []}
    engines = _run(run_row)["engines"]
    questions = db.execute(
        "SELECT * FROM geo_questions WHERE project_id=? AND active=1 AND tenant_id=? ORDER BY id ASC",
        (project_id, tenant)
    ).fetchall()
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = max(1, min(100, int((query.get("page_size") or query.get("pageSize") or ["20"])[0])))
    except ValueError:
        page, size = 1, 20
    rows = []
    for question in questions[(page - 1) * size:page * size]:
        cells = {}
        for engine in engines:
            answers = db.execute(
                "SELECT * FROM geo_answers WHERE run_id=? AND question_id=? AND engine_key=? "
                "AND tenant_id=? ORDER BY id ASC",
                (run_row["id"], question["id"], engine["key"], tenant),
            ).fetchall()
            ok = [row for row in answers if row["status"] == "ok"]
            mentioned = 0
            for row in ok:
                if any(brand.casefold() in row["text"].casefold() for brand in _brands(project)):
                    mentioned += 1
            cells[engine["key"]] = {
                "pending": run_row["status"] in {"queued", "running"} and not answers,
                "ok": len(ok), "mentioned": mentioned, "best_rank": None, "competitors": 0,
                "samples": run_row["samples"], "answer_ids": [row["id"] for row in answers],
                "error": next((row["error"] for row in answers if row["error"]), ""),
                "sentiment": "neu" if mentioned else None,
            }
        rows.append({
            "question": {"id": question["id"], "text": question["text"],
                         "intent_name": INTENTS.get(question["intent"], "购买决策"), "layer_name": ""},
            "cells": cells,
        })
    return {"ok": True, "run": _run(run_row), "rows": rows, "total": len(questions), "engines": engines}


def _report(db, project_id, query):
    period = (query.get("period") or ["week"])[0]
    try:
        offset = max(0, int((query.get("offset") or ["0"])[0]))
    except ValueError:
        offset = 0
    rows = db.execute(
        "SELECT * FROM geo_runs WHERE project_id=? AND status='done' AND tenant_id=? "
        "ORDER BY id DESC LIMIT 20",
        (project_id, commander.tenant()),
    ).fetchall()
    label = {"week": "本周", "month": "本月"}.get(period, "本周")
    if offset:
        label = f"往前第 {offset} 期"
    return {
        "ok": True,
        "report": {
            "period_name": "月报" if period == "month" else "周报",
            "range": {"label": label},
            "runs": len(rows),
            "credits": sum(float(row["credits"]) for row in rows),
            "alerts": {"total": 0, "kinds": []},
            "note": METHOD_NOTE, "trend": [],
            "engines": [], "intents": [],
            "layers": {"rows": [], "layered": 0, "unlayered": 0},
            "sources": {"domains": []}, "gaps": [],
            "gap_total": 0, "examples": [], "findings": [], "cited": [],
            "publishing": None, "current": None, "delta": None, "latest": None,
            "overview": {"sample_n": 0, "mentioned_n": 0, "mention_rate": 0},
            "questions": _question_count(db, project_id),
            "answers": sum(int(row["done"]) for row in rows),
            "mentions": 0, "citations": 0,
            "method_note": METHOD_NOTE,
        },
    }


def read(path, query):
    path = canonical(path)
    with commander.database() as db:
        initialize(db)
        answer_match = ANSWER.fullmatch(path)
        if answer_match:
            return _answer_detail(db, int(answer_match[1]))
        run_match = RUN.fullmatch(path)
        if run_match and not run_match[2]:
            row = db.execute("SELECT * FROM geo_runs WHERE id=? AND tenant_id=?",
                             (int(run_match[1]), commander.tenant())).fetchone()
            if row is None:
                raise commander.ChatError("体检记录不存在。")
            return {"ok": True, "run": _run(row)}
        match = PROJECT.fullmatch(path)
        if not match:
            raise commander.ChatError("GEO 接口暂不可用。")
        project_id, suffix = int(match[1]), match[2] or ""
        if suffix == "questions":
            return _list_questions(db, project_id, query)
        if suffix == "runs":
            try:
                page = max(1, int((query.get("page") or ["1"])[0]))
                size = max(1, min(100, int((query.get("page_size") or query.get("pageSize") or ["10"])[0])))
            except ValueError:
                page, size = 1, 10
            rows = db.execute(
                "SELECT * FROM geo_runs WHERE project_id=? AND tenant_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (project_id, commander.tenant(), size, (page - 1) * size),
            ).fetchall()
            total = db.execute(
                "SELECT COUNT(*) FROM geo_runs WHERE project_id=? AND tenant_id=?",
                (project_id, commander.tenant())).fetchone()[0]
            return {"ok": True, "items": [_run(row) for row in rows], "total": total}
        if suffix == "matrix":
            return _matrix(db, project_id, query)
        if suffix == "reports":
            return _report(db, project_id, query)
        if suffix == "diagnoses":
            return {"ok": True, "items": [], "total": 0}
        if suffix == "layers":
            return {"ok": True, "rows": [], "layered": 0,
                    "unlayered": _question_count(db, project_id)}
        if suffix == "answers":
            rows = db.execute(
                "SELECT * FROM geo_answers WHERE project_id=? AND tenant_id=? ORDER BY id DESC LIMIT 100",
                (project_id, commander.tenant())
            ).fetchall()
            return {"ok": True, "items": [dict(row) for row in rows], "total": len(rows)}
    raise commander.ChatError("GEO 接口暂不可用。")


def write(method, path, body):
    path = canonical(path)
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        run_match = RUN.fullmatch(path)
        if run_match:
            run_id, suffix = int(run_match[1]), run_match[2] or ""
            if suffix == "cancel" and method == "POST":
                db.execute("UPDATE geo_runs SET status='cancelled',finished_at=? WHERE id=? AND tenant_id=?",
                           (now(), run_id, commander.tenant()))
                tenancy.audit_in(db, commander.tenant(), "geo.run.cancel", str(run_id))
                return {"ok": True}
            if suffix == "retry/estimate" and method == "POST":
                row = db.execute("SELECT project_id FROM geo_runs WHERE id=? AND tenant_id=?",
                                 (run_id, commander.tenant())).fetchone()
                if row is None:
                    raise commander.ChatError("体检记录不存在。")
                return _estimate(db, row[0], {})
            if suffix == "retry" and method == "POST":
                row = db.execute("SELECT * FROM geo_runs WHERE id=? AND tenant_id=?",
                                 (run_id, commander.tenant())).fetchone()
                if row is None:
                    raise commander.ChatError("体检记录不存在。")
                return {"ok": True, "run": _run(row)}
            raise commander.ChatError("GEO 接口暂不可用。")
        match = PROJECT.fullmatch(path)
        if not match:
            raise commander.ChatError("GEO 接口暂不可用。")
        project_id, suffix = int(match[1]), match[2] or ""
        _project(db, project_id)
        if suffix == "questions" and method == "POST":
            return _add_questions(db, project_id, body.get("items"), str(body.get("source") or "manual"))
        if suffix == "questions/generate" and method == "POST":
            return _generate_questions(db, _project(db, project_id), body)
        if suffix == "questions/delete" and method == "POST":
            ids = body.get("ids")
            if not isinstance(ids, list) or not ids:
                raise commander.ChatError("请选择要删除的问题。")
            placeholders = ",".join("?" for _ in ids)
            changed = db.execute(
                f"DELETE FROM geo_questions WHERE project_id=? AND tenant_id=? AND id IN ({placeholders})",
                [project_id, commander.tenant(), *[int(item) for item in ids]],
            )
            tenancy.audit_in(db, commander.tenant(), "geo.questions.delete", str(project_id),
                             f"count={changed.rowcount}")
            return {"ok": True, "count": changed.rowcount}
        question_match = re.fullmatch(r"questions/([0-9]{1,16})", suffix)
        if question_match and method == "PUT":
            question_id = int(question_match[1])
            row = db.execute(
                "SELECT * FROM geo_questions WHERE id=? AND project_id=? AND tenant_id=?",
                (question_id, project_id, commander.tenant())
            ).fetchone()
            if row is None:
                raise commander.ChatError("问题不存在。")
            text = str(body.get("text") or row["text"]).strip()[:300]
            if not text:
                raise commander.ChatError("问题不能为空。")
            active = 1 if body.get("active", bool(row["active"])) else 0
            db.execute(
                "UPDATE geo_questions SET text=?,intent=?,locale=?,active=? WHERE id=? AND tenant_id=?",
                (text, str(body.get("intent") or row["intent"]), str(body.get("locale") or row["locale"]),
                 active, question_id, commander.tenant()),
            )
            return {"ok": True, "question": _question(db.execute(
                "SELECT * FROM geo_questions WHERE id=? AND tenant_id=?",
                (question_id, commander.tenant())
            ).fetchone())}
        if suffix == "runs/estimate" and method == "POST":
            return _estimate(db, project_id, body)
        if suffix == "runs" and method == "POST":
            return _start_run(db, project_id, body)
    raise commander.ChatError("GEO 接口暂不可用。")
