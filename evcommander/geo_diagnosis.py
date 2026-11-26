"""GEO diagnosis plans and alerts.

A diagnosis is computed from one completed run: numbers, gaps, layers and the
three-month plan are derived from stored answers, while a single text-model
call writes the analysis sections (positioning, buyer, comparison criteria,
per-question intent, answer shape, summary). If the model fails, the data
sections are still delivered and the AI charge is refunded.

Alerts are generated when a diagnosis is created: mention-rate drops,
competitors rising, and runs with too many failed answers. They are read on
the project overview and can be marked handled/ignored.
"""

from datetime import datetime, timezone
import json
import re
import threading
import time

import commander
import geo_matrix
import geo_report
import tenancy
import payments


PROJECT = re.compile(r"/api/geo/projects/([0-9]{1,16})(?:/(.*))?$")
DIAGNOSIS = re.compile(r"/api/geo/diagnoses/([0-9]{1,16})$")
ALERT = re.compile(r"/api/geo/alerts/([0-9]{1,16})$")
MAX_OFFSET = 24
AI_OUTPUT_TOKENS = 1600
ALERT_ACTIONS = {"open", "handled", "ignored"}


def canonical(path):
    if path.startswith("/agent-api"):
        path = path[len("/agent-api"):]
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def can_read(path):
    path = canonical(path)
    if DIAGNOSIS.fullmatch(path):
        return True
    match = PROJECT.fullmatch(path)
    return bool(match and (match[2] or "").strip("/") in {"diagnoses", "diagnoses/estimate", "alerts"})


def can_write(path):
    path = canonical(path)
    if ALERT.fullmatch(path):
        return True
    match = PROJECT.fullmatch(path)
    return bool(match and (match[2] or "").strip("/") == "diagnoses")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_diagnoses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        run_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'running',
        ai_status TEXT NOT NULL DEFAULT '', baseline TEXT NOT NULL DEFAULT '',
        engines_n INTEGER NOT NULL DEFAULT 0, questions_n INTEGER NOT NULL DEFAULT 0,
        samples INTEGER NOT NULL DEFAULT 1, mention_rate REAL NOT NULL DEFAULT 0,
        credits REAL NOT NULL DEFAULT 0, charge_id INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT ''
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        dedupe_key TEXT NOT NULL, kind TEXT NOT NULL, kind_name TEXT NOT NULL,
        level TEXT NOT NULL DEFAULT 'info', title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(project_id, dedupe_key)
    )""")


def _project(db, project_id):
    row = db.execute("SELECT * FROM geo_projects WHERE id=? AND tenant_id=?",
                     (int(project_id), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("GEO 项目不存在。")
    return row


def _run(db, project_id, run_id):
    row = db.execute(
        "SELECT * FROM geo_runs WHERE id=? AND project_id=? AND tenant_id=?",
        (int(run_id), int(project_id), commander.tenant()),
    ).fetchone()
    if row is None:
        raise commander.ChatError("体检记录不存在。")
    return row


def _status_name(status):
    return {"running": "生成中", "done": "已完成", "failed": "失败"}.get(status, status)


def _item(row):
    return {
        "id": row["id"], "project_id": row["project_id"], "run_id": row["run_id"],
        "baseline": row["baseline"], "engines_n": row["engines_n"],
        "questions_n": row["questions_n"], "samples": row["samples"],
        "status": row["status"], "status_name": _status_name(row["status"]),
        "ai_status": row["ai_status"], "mention_rate": row["mention_rate"],
        "credits": row["credits"], "error": row["error"],
        "created_at": row["created_at"], "finished_at": row["finished_at"],
    }


def _detail(row):
    item = _item(row)
    try:
        item["data"] = json.loads(row["data"]) if row["data"] else None
    except (TypeError, ValueError):
        item["data"] = None
    return item


def _engine_count(run):
    try:
        return len(json.loads(run["engines"]))
    except (TypeError, ValueError):
        return 0


def _summary_for_run(db, project_id, run_id):
    _, run, records, _, _ = geo_report.run_context(db, project_id, run_id)
    return run, records, geo_report.summarise(records)


def _existing(db, project_id, run_id):
    return db.execute(
        "SELECT * FROM geo_diagnoses WHERE project_id=? AND tenant_id=? AND run_id=? "
        "AND status IN ('running','done') ORDER BY id DESC LIMIT 1",
        (int(project_id), commander.tenant(), int(run_id)),
    ).fetchone()


def _estimate(db, project_id, run_id):
    blockers = []
    credits = 0.0
    balance = payments.balance_in(db)
    existing = None
    run = records = None
    try:
        run, records, summary = _summary_for_run(db, project_id, run_id)
    except commander.ChatError as error:
        blockers.append(str(error))
    if run is not None:
        if run["status"] != "done":
            blockers.append("这次体检还没完成，等跑完再生成。")
        elif not records:
            blockers.append("这次体检没有有效回答，先重新体检。")
        else:
            messages = geo_report.diagnosis_messages(db, project_id, run_id)
            credits = payments.token_charge(
                payments.estimate_message_tokens(messages), AI_OUTPUT_TOKENS)
            row = _existing(db, project_id, run_id)
            if row is not None:
                existing = {"id": row["id"], "finished_at": row["finished_at"],
                            "created_at": row["created_at"]}
    charging = payments.charging_enabled()
    if not charging:
        credits = 0.0
    enough = not blockers and (not charging or balance >= credits)
    questions = run["total"] // max(1, _engine_count(run) * max(1, int(run["samples"] or 1))) \
        if run is not None else 0
    return {
        "ok": True,
        "estimate": {
            "blockers": blockers, "enough": enough, "credits": round(credits, 4),
            "balance": balance, "existing": existing,
            "engines": _engine_count(run) if run is not None else 0,
            "questions": questions,
            "samples": int(run["samples"] or 1) if run is not None else 1,
        },
    }


def _start(db, project_id, body):
    try:
        run_id = int(body.get("run_id") or 0)
    except (TypeError, ValueError) as error:
        raise commander.ChatError("请选择要诊断的体检。") from error
    force = bool(body.get("force"))
    run = _run(db, project_id, run_id)
    if run["status"] != "done":
        raise commander.ChatError("这次体检还没完成，等跑完再生成。")
    _, records, summary = _summary_for_run(db, project_id, run_id)
    if not records:
        raise commander.ChatError("这次体检没有有效回答，先重新体检。")
    existing = _existing(db, project_id, run_id)
    if existing is not None and existing["status"] == "running":
        return {"ok": True, "reused": True, "diagnosis": _item(existing)}
    if existing is not None and not force:
        return {
            "ok": False, "code": "exists",
            "error": "这次体检已经生成过方案，重新生成会再按用量扣一次积分。",
        }
    messages = geo_report.diagnosis_messages(db, project_id, run_id)
    reserve = payments.token_charge(
        payments.estimate_message_tokens(messages), AI_OUTPUT_TOKENS)
    if not payments.charging_enabled():
        reserve = 0
    timestamp = now()
    key = db.execute(
        "INSERT INTO geo_diagnoses (tenant_id,project_id,run_id,status,ai_status,baseline,"
        "engines_n,questions_n,samples,mention_rate,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (commander.tenant(), int(project_id), run_id, "running", "",
         run["finished_at"] or run["created_at"], _engine_count(run),
         len({row["question_id"] for row in records}), int(run["samples"] or 1),
         summary["mention_rate"], timestamp),
    ).lastrowid
    charge_id = payments.charge_in(db, reserve, f"GEO 诊断 #{key}", str(key))
    if charge_id:
        db.execute("UPDATE geo_diagnoses SET charge_id=? WHERE id=? AND tenant_id=?",
                   (charge_id, key, commander.tenant()))
    tenancy.audit_in(db, commander.tenant(), "geo.diagnosis.start", str(key),
                     f"project={project_id}; run={run_id}")
    row = db.execute("SELECT * FROM geo_diagnoses WHERE id=?", (key,)).fetchone()
    thread = threading.Thread(target=_worker, args=(commander.tenant(), key), daemon=True)
    thread.start()
    return {"ok": True, "reused": False, "diagnosis": _item(row)}


def _worker(tenant, diagnosis_id):
    tenancy.activate({"tenant_id": tenant, "user_id": "geo-diagnosis",
                      "username": "诊断任务", "role": "member"})
    charge_id = 0
    try:
        row = None
        # The request transaction may still be committing when this daemon starts.
        for _ in range(20):
            with commander.database() as db:
                row = db.execute("SELECT * FROM geo_diagnoses WHERE id=? AND tenant_id=?",
                                 (diagnosis_id, tenant)).fetchone()
            if row is not None:
                break
            time.sleep(0.05)
        if row is None:
            return
        with commander.database() as db:
            charge_id = row["charge_id"]
            project_id, run_id = row["project_id"], row["run_id"]
            _, records, _ = _summary_for_run(db, project_id, run_id)
            question_ids = {item["question_id"] for item in records}
            messages = geo_report.diagnosis_messages(db, project_id, run_id)
            prompt_estimate = payments.estimate_message_tokens(messages)
        usage = {}
        ai = None
        ai_note = ""
        raw = ""
        try:
            raw = "".join(commander.generate(
                messages, max_output_tokens=AI_OUTPUT_TOKENS, usage=usage)).strip()
            ai = geo_report.parse_ai(raw, question_ids)
            if ai is None:
                ai_note = "分析段这次没写出来，数据部分不受影响。"
        except Exception:
            ai_note = "分析段这次没写出来，数据部分不受影响。"
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        if not isinstance(prompt, (int, float)):
            prompt = prompt_estimate
        if not isinstance(completion, (int, float)):
            completion = payments.estimate_text_tokens(raw)
        actual = (payments.token_charge(prompt, completion)
                  if ai and payments.charging_enabled() else 0)
        with commander.database() as db:
            data = geo_report.diagnosis_data(
                db, project_id, run_id, ai=ai, ai_note=ai_note,
                generated_at=geo_matrix.display_time(now()))
            payments.adjust_in(db, charge_id, actual,
                               f"GEO 诊断 #{diagnosis_id}（AI 分析）")
            db.execute(
                "UPDATE geo_diagnoses SET status='done', ai_status=?, data=?, credits=?, "
                "finished_at=? WHERE id=? AND tenant_id=?",
                ("ok" if ai else "failed", json.dumps(data, ensure_ascii=False),
                 round(actual, 4), now(), diagnosis_id, tenant),
            )
            _refresh_alerts(db, project_id, run_id)
    except BaseException as error:
        with commander.database() as db:
            if charge_id:
                payments.adjust_in(db, charge_id, 0, f"GEO 诊断 #{diagnosis_id}")
            db.execute(
                "UPDATE geo_diagnoses SET status='failed', ai_status='failed', error=?, "
                "credits=0, finished_at=? WHERE id=? AND tenant_id=?",
                (str(error)[:500], now(), diagnosis_id, tenant),
            )


# ---------------------------------------------------------------------------
# Alerts

def _insert_alert(db, project_id, dedupe_key, kind, kind_name, level, title, detail):
    stamp = now()
    db.execute(
        "INSERT OR IGNORE INTO geo_alerts (tenant_id,project_id,dedupe_key,kind,kind_name,"
        "level,title,detail,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (commander.tenant(), int(project_id), dedupe_key, kind, kind_name, level, title,
         json.dumps(detail, ensure_ascii=False), "open", stamp, stamp),
    )


def _rate_for(records, predicate):
    if not records:
        return 0.0
    return round(sum(1 for row in records if predicate(row)) / len(records), 4)


def _refresh_alerts(db, project_id, run_id):
    run = _run(db, project_id, run_id)
    _, current, _ = _summary_for_run(db, project_id, run_id)
    previous = db.execute(
        "SELECT * FROM geo_runs WHERE project_id=? AND tenant_id=? AND status='done' "
        "AND id<? ORDER BY id DESC LIMIT 1",
        (int(project_id), commander.tenant(), int(run_id)),
    ).fetchone()
    if previous is not None:
        _, before, _ = _summary_for_run(db, project_id, previous["id"])
        if before and current:
            prev_rate = _rate_for(before, lambda row: row["mentioned"])
            cur_rate = _rate_for(current, lambda row: row["mentioned"])
            drop = round(prev_rate - cur_rate, 4)
            if len(before) >= 3 and len(current) >= 3 and drop >= 0.2:
                _insert_alert(
                    db, project_id, f"mention_drop:{run_id}", "mention_drop",
                    "被提到比例下降", "danger" if drop >= 0.4 else "warn",
                    "被提到比例明显下降",
                    {"prev": prev_rate, "cur": cur_rate, "run_id": run_id,
                     "prev_run_id": previous["id"]},
                )
            _, _, _, _, competitor_map = geo_report.run_context(
                db, project_id, run_id)
            items = []
            for name in dict.fromkeys(competitor_map.values()):
                prev_count = sum(1 for row in before if name in row["competitors"])
                cur_count = sum(1 for row in current if name in row["competitors"])
                prev_share = prev_count / len(before) if before else 0
                cur_share = cur_count / len(current) if current else 0
                if cur_count >= 2 and cur_share - prev_share >= 0.2:
                    items.append({"name": name,
                                  "delta": round(cur_share - prev_share, 4)})
            if items:
                _insert_alert(
                    db, project_id, f"competitor_up:{run_id}", "competitor_up",
                    "竞品上升", "warn", "竞品被提到更多了",
                    {"items": items, "run_id": run_id},
                )
    total = int(run["total"] or 0)
    failed = int(run["failed"] or 0)
    if total and failed / total >= 0.2:
        _insert_alert(
            db, project_id, f"run_skipped:{run_id}", "run_skipped", "体检未完成",
            "info", f"这次体检有 {failed} 条没拿到回答",
            {"reason": "上游模型偶发失败，可以重试未完成的回答", "run_id": run_id},
        )


def _alert_item(row):
    try:
        detail = json.loads(row["detail"])
    except (TypeError, ValueError):
        detail = {}
    return {
        "id": row["id"], "kind": row["kind"], "kind_name": row["kind_name"],
        "level": row["level"], "title": row["title"], "detail": detail,
        "status": row["status"],
        "status_name": {"open": "待处理", "handled": "已处理",
                        "ignored": "已忽略"}.get(row["status"], row["status"]),
        "created_at": row["created_at"],
    }


def _list_alerts(db, project_id, query):
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = max(1, min(100, int((query.get("page_size") or query.get("pageSize") or ["10"])[0])))
    except ValueError as error:
        raise commander.ChatError("分页参数无效。") from error
    where, values = "WHERE project_id=? AND tenant_id=?", [int(project_id), commander.tenant()]
    status = (query.get("status") or ["open"])[0].strip()
    if status in ALERT_ACTIONS:
        where += " AND status=?"
        values.append(status)
    rows = db.execute(
        f"SELECT * FROM geo_alerts {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*values, size, (page - 1) * size],
    ).fetchall()
    total = db.execute(f"SELECT COUNT(*) FROM geo_alerts {where}", values).fetchone()[0]
    return {"ok": True, "items": [_alert_item(row) for row in rows], "total": total}


def _update_alert(db, alert_id, body):
    status = str(body.get("status") or "").strip()
    if status not in ALERT_ACTIONS:
        raise commander.ChatError("提醒状态无效。")
    changed = db.execute(
        "UPDATE geo_alerts SET status=?, updated_at=? WHERE id=? AND tenant_id=?",
        (status, now(), int(alert_id), commander.tenant()),
    )
    if changed.rowcount != 1:
        raise commander.ChatError("提醒不存在。")
    row = db.execute("SELECT * FROM geo_alerts WHERE id=? AND tenant_id=?",
                     (int(alert_id), commander.tenant())).fetchone()
    return {"ok": True, "alert": _alert_item(row)}


# ---------------------------------------------------------------------------
# Entry points

def share_snapshot(db, project_id, diagnosis_id):
    initialize(db)
    try:
        key = int(diagnosis_id or 0)
    except (TypeError, ValueError) as error:
        raise commander.ChatError("请选择要分享的诊断方案。") from error
    row = db.execute(
        "SELECT * FROM geo_diagnoses WHERE id=? AND project_id=? AND tenant_id=?",
        (key, int(project_id), commander.tenant()),
    ).fetchone()
    if row is None:
        raise commander.ChatError("诊断方案不存在。")
    if row["status"] != "done" or not row["data"]:
        raise commander.ChatError("这份诊断方案还没生成完，等完成后再分享。")
    try:
        data = json.loads(row["data"])
    except (TypeError, ValueError) as error:
        raise commander.ChatError("诊断方案数据无效。") from error
    data["kind"] = "diagnosis"
    return data


def read(path, query):
    path = canonical(path)
    with commander.database() as db:
        initialize(db)
        match = DIAGNOSIS.fullmatch(path)
        if match:
            row = db.execute(
                "SELECT * FROM geo_diagnoses WHERE id=? AND tenant_id=?",
                (int(match[1]), commander.tenant()),
            ).fetchone()
            if row is None:
                raise commander.ChatError("诊断方案不存在。")
            return {"ok": True, "diagnosis": _detail(row)}
        match = PROJECT.fullmatch(path)
        if not match:
            raise commander.ChatError("GEO 接口暂不可用。")
        project_id, suffix = int(match[1]), (match[2] or "").strip("/")
        _project(db, project_id)
        if suffix == "diagnoses":
            try:
                page = max(1, int((query.get("page") or ["1"])[0]))
                size = max(1, min(100, int(
                    (query.get("page_size") or query.get("pageSize") or ["8"])[0])))
            except ValueError as error:
                raise commander.ChatError("分页参数无效。") from error
            rows = db.execute(
                "SELECT * FROM geo_diagnoses WHERE project_id=? AND tenant_id=? "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (project_id, commander.tenant(), size, (page - 1) * size),
            ).fetchall()
            total = db.execute(
                "SELECT COUNT(*) FROM geo_diagnoses WHERE project_id=? AND tenant_id=?",
                (project_id, commander.tenant()),
            ).fetchone()[0]
            return {"ok": True, "items": [_item(row) for row in rows], "total": total}
        if suffix == "diagnoses/estimate":
            run_id = (query.get("run_id") or ["0"])[0]
            return _estimate(db, project_id, run_id)
        if suffix == "alerts":
            return _list_alerts(db, project_id, query)
    raise commander.ChatError("GEO 接口暂不可用。")


def write(method, path, body):
    path = canonical(path)
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        alert_match = ALERT.fullmatch(path)
        if alert_match and method == "PUT":
            return _update_alert(db, alert_match[1], body)
        match = PROJECT.fullmatch(path)
        if not match:
            raise commander.ChatError("GEO 接口暂不可用。")
        project_id, suffix = int(match[1]), (match[2] or "").strip("/")
        _project(db, project_id)
        if suffix == "diagnoses" and method == "POST":
            return _start(db, project_id, body)
    raise commander.ChatError("GEO 接口暂不可用。")


# ---------------------------------------------------------------------------
# Startup reconciliation: refund diagnoses left behind by a killed process


def reconcile_charges():
    """Refund diagnoses stuck in running after a restart (single instance)."""
    refunded = 0
    with commander.database() as db:
        initialize(db)
        rows = db.execute(
            "SELECT id,tenant_id FROM geo_diagnoses WHERE status='running'").fetchall()
    for row in rows:
        with tenancy.scope(row["tenant_id"], "reconcile", "对账", "member"):
            with commander.database() as db:
                db.execute("BEGIN IMMEDIATE")
                initialize(db)
                current = db.execute(
                    "SELECT status,charge_id FROM geo_diagnoses WHERE id=? AND tenant_id=?",
                    (row["id"], row["tenant_id"])).fetchone()
                if current is None or current["status"] != "running":
                    continue
                if current["charge_id"]:
                    payments.adjust_in(db, current["charge_id"], 0,
                                       "GEO 诊断 #%d（未完成退回）" % row["id"])
                db.execute(
                    "UPDATE geo_diagnoses SET status='failed', ai_status='failed', "
                    "error=?, credits=0, finished_at=? WHERE id=? AND tenant_id=?",
                    ("服务重启，诊断未完成，预扣已退回。", now(), row["id"], row["tenant_id"]))
                tenancy.audit_in(db, row["tenant_id"], "geo.diagnosis.reconcile",
                                 str(row["id"]), "refunded")
                refunded += 1
    return refunded
