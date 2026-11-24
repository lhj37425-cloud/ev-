"""GEO weekly/monthly reports and public report sharing.

Builds the data the archived report, print and public share pages render from
stored questions/runs/answers. AI-search providers are still simulated; every
report carries the same method note as the run pages. Shares are immutable
snapshots behind random public tokens, so later runs do not change a link.
"""

from datetime import datetime, timedelta, timezone
import json
import re
import secrets

import commander
import geo_brand
import geo_matrix
import geo_runs
import tenancy


PROJECT = re.compile(r"/api/geo/projects/([0-9]{1,16})(?:/(.*))?$")
SHARE = re.compile(r"/api/geo/report-shares/([0-9]{1,16})$")
PUBLIC_PREFIX = "/api/public/geo/report/"
PUBLIC = re.compile(r"/api/public/geo/report/([A-Za-z0-9_-]{16,64})$")
SHARE_DAYS = {1, 7, 30, 90}
MAX_OFFSET = 24
EXCERPT_CHARS = 700


def canonical(path):
    if path.startswith("/agent-api"):
        path = path[len("/agent-api"):]
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def can_read(path):
    path = canonical(path)
    match = PROJECT.fullmatch(path)
    return bool(match and (match[2] or "").strip("/") in {"reports", "report-shares"})


def can_write(path):
    path = canonical(path)
    if SHARE.fullmatch(path):
        return True
    match = PROJECT.fullmatch(path)
    return bool(match and (match[2] or "").strip("/") == "report-shares")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_report_shares (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        token TEXT NOT NULL UNIQUE, period TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
        days INTEGER NOT NULL DEFAULT 7, title TEXT NOT NULL DEFAULT '',
        period_name TEXT NOT NULL DEFAULT '', range_label TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'active',
        views INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, expire_at TEXT NOT NULL
    )""")


# ---------------------------------------------------------------------------
# Period and matching helpers

def _project(db, project_id):
    row = db.execute("SELECT * FROM geo_projects WHERE id=? AND tenant_id=?",
                     (int(project_id), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("GEO 项目不存在。")
    return row


def _period(period, offset):
    try:
        offset = max(0, min(MAX_OFFSET, int(offset or 0)))
    except (TypeError, ValueError):
        offset = 0
    current = datetime.now(timezone.utc)
    if period == "month":
        index = current.year * 12 + (current.month - 1) - offset
        year, month = divmod(index, 12)
        start = datetime(year, month + 1, 1, tzinfo=timezone.utc)
        end = (datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 11
               else datetime(year, month + 2, 1, tzinfo=timezone.utc))
        period_name = "月报"
    else:
        today = current.date()
        monday = today - timedelta(days=today.weekday()) - timedelta(weeks=offset)
        start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
        end = start + timedelta(days=7)
        period_name = "周报"
    last = end - timedelta(days=1)
    return {
        "period": "month" if period == "month" else "week",
        "period_name": period_name, "start": start, "end": end,
        "range": {"label": f"{start.date()} 至 {last.date()}"},
    }


def _brand_names(project):
    names = [project["brand_name"]]
    try:
        names.extend(json.loads(project["aliases"]))
    except (TypeError, ValueError):
        pass
    return [name for name in dict.fromkeys(names) if name]


def _competitor_map(db, project_id):
    """Alias -> canonical competitor name, so "甲" and "甲科技" count once."""
    geo_brand.initialize(db)
    rows = db.execute(
        "SELECT name, aliases FROM geo_competitors WHERE project_id=? AND tenant_id=? ORDER BY id",
        (int(project_id), commander.tenant()),
    ).fetchall()
    mapping = {}
    for row in rows:
        mapping[row["name"]] = row["name"]
        try:
            aliases = json.loads(row["aliases"])
        except (TypeError, ValueError):
            aliases = []
        for alias in aliases:
            if isinstance(alias, str) and alias:
                mapping[alias] = row["name"]
    return mapping


def _offsets(text, names, canonical=None):
    folded = text.lower()
    found = []
    for name in names:
        needle = name.lower()
        if not needle:
            continue
        start = 0
        while True:
            index = folded.find(needle, start)
            if index < 0:
                break
            found.append({
                "name": (canonical or {}).get(name, name),
                "start": index, "end": index + len(needle),
            })
            start = index + len(needle)
    return found


def _mention(text, brand_names, competitor_map):
    brand_hits = _offsets(text, brand_names)
    competitor_hits = _offsets(text, list(competitor_map), competitor_map)
    mentioned = bool(brand_hits)
    competitors = sorted({hit["name"] for hit in competitor_hits})
    if not mentioned:
        return {"mentioned": False, "top1": False, "rank": None, "competitors": competitors}
    first_brand = min(hit["start"] for hit in brand_hits)
    before = {hit["name"] for hit in competitor_hits if hit["start"] < first_brand}
    first_competitor = min(hit["start"] for hit in competitor_hits) if competitor_hits else None
    return {
        "mentioned": True,
        "top1": first_competitor is None or first_brand <= first_competitor,
        "rank": 1 + len(before),
        "competitors": competitors,
    }


def _rate(count, total):
    return round(count / total, 4) if total else 0


def _summary(records):
    sample_n = len(records)
    mentioned_n = sum(1 for row in records if row["mentioned"])
    top1_n = sum(1 for row in records if row["top1"])
    competitor_n = sum(len(row["competitors"]) for row in records)
    denominator = mentioned_n + competitor_n
    return {
        "sample_n": sample_n, "mentioned_n": mentioned_n,
        "mention_rate": _rate(mentioned_n, sample_n),
        "top1_rate": _rate(top1_n, sample_n),
        "sov": round(mentioned_n / denominator, 4) if denominator else 0,
        "accuracy": None,
    }


def _collect(db, project_id, start, end, brand_names, competitor_map):
    runs = db.execute(
        "SELECT * FROM geo_runs WHERE project_id=? AND tenant_id=? AND status='done' "
        "AND created_at>=? AND created_at<? ORDER BY id",
        (int(project_id), commander.tenant(), start.isoformat(timespec="seconds"),
         end.isoformat(timespec="seconds")),
    ).fetchall()
    records = []
    if runs:
        marks = ",".join("?" for _ in runs)
        rows = db.execute(
            f"SELECT a.id, a.run_id, a.engine_key, a.engine_name, a.text, a.created_at, "
            f"q.id AS question_id, q.text AS question_text, q.intent, q.layer "
            f"FROM geo_answers a JOIN geo_questions q ON q.id=a.question_id "
            f"WHERE a.tenant_id=? AND a.project_id=? AND a.run_id IN ({marks}) "
            f"AND a.status='ok' ORDER BY a.id",
            [commander.tenant(), int(project_id), *[row["id"] for row in runs]],
        ).fetchall()
        for row in rows:
            info = _mention(row["text"], brand_names, competitor_map)
            records.append({
                "id": row["id"], "run_id": row["run_id"],
                "engine_key": row["engine_key"], "engine_name": row["engine_name"],
                "text": row["text"], "created_at": row["created_at"],
                "question_id": row["question_id"], "question_text": row["question_text"],
                "intent": row["intent"], "layer": row["layer"],
                **info,
            })
    return runs, records


def _group(records, key):
    groups = {}
    for row in records:
        groups.setdefault(row[key], []).append(row)
    return groups


def _engine_rows(records):
    rows = []
    for key, group in _group(records, "engine_key").items():
        rate = _rate(sum(1 for row in group if row["mentioned"]), len(group))
        rows.append({
            "engine": {"key": key, "name": group[0]["engine_name"]},
            "sample_n": len(group),
            "mention_rate": rate,
            "top1_rate": _rate(sum(1 for row in group if row["top1"]), len(group)),
            "tier": geo_matrix.judge_layer(rate, len(group)),
        })
    return rows


def _question_rows(records, limit=None):
    rows = []
    for question_id, group in _group(records, "question_id").items():
        ranks = [row["rank"] for row in group if row["rank"]]
        rate = _rate(sum(1 for row in group if row["mentioned"]), len(group))
        rows.append({
            "question_id": question_id, "question": group[0]["question_text"],
            "intent_name": geo_runs.INTENTS.get(group[0]["intent"], "购买决策"),
            "mentioned_n": sum(1 for row in group if row["mentioned"]),
            "sample_n": len(group), "mention_rate": rate,
            "best_rank": min(ranks) if ranks else None,
            "tier": geo_matrix.judge_layer(rate, len(group)),
        })
    rows.sort(key=lambda item: (-item["mention_rate"], -item["sample_n"], item["question_id"]))
    return rows[:limit] if limit else rows


def _intent_rows(records):
    rows = []
    for intent, group in _group(records, "intent").items():
        rows.append({
            "intent": intent, "intent_name": geo_runs.INTENTS.get(intent, "购买决策"),
            "sample_n": len(group),
            "mention_rate": _rate(sum(1 for row in group if row["mentioned"]), len(group)),
            "top1_rate": _rate(sum(1 for row in group if row["top1"]), len(group)),
        })
    rows.sort(key=lambda item: -item["sample_n"])
    return rows


def _layer_rows(db, project_id, records):
    active = {
        row["layer"]: row["count"] for row in db.execute(
            "SELECT layer, COUNT(*) AS count FROM geo_questions "
            "WHERE project_id=? AND tenant_id=? AND active=1 GROUP BY layer",
            (int(project_id), commander.tenant()),
        ).fetchall()
    }
    grouped = _group(records, "layer")
    rows = []
    for layer in geo_matrix.LAYERS:
        key = layer["key"]
        questions = active.get(key, 0)
        group = grouped.get(key, [])
        if not questions and not group:
            continue
        rate = _rate(sum(1 for row in group if row["mentioned"]), len(group))
        rows.append({
            "layer": key, "name": layer["name"], "full": layer["full"],
            "questions": questions, "sample_n": len(group),
            "mention_rate": rate,
            "top1_rate": _rate(sum(1 for row in group if row["top1"]), len(group)),
            "tier": geo_matrix.judge_layer(rate, len(group)),
        })
    layered = sum(count for key, count in active.items() if key in geo_matrix.LAYER_MAP)
    unlayered = sum(count for key, count in active.items() if key not in geo_matrix.LAYER_MAP)
    return {"rows": rows, "layered": layered, "unlayered": unlayered}


def _trend(records, start, end):
    by_day = {}
    for row in records:
        by_day.setdefault(row["created_at"][:10], []).append(row)
    days = []
    cursor = start.date()
    while cursor < end.date():
        key = cursor.isoformat()
        group = by_day.get(key, [])
        summary = _summary(group) if group else None
        days.append({
            "date": key,
            "mention_rate": summary["mention_rate"] if summary else None,
            "top1_rate": summary["top1_rate"] if summary else None,
            "sov": summary["sov"] if summary else None,
        })
        cursor += timedelta(days=1)
    return days


def _latest(runs, records):
    if not runs:
        return None
    run = runs[-1]
    group = [row for row in records if row["run_id"] == run["id"]]
    overall = _summary(group)
    engines = _engine_rows(group)
    return {
        "time": run["finished_at"] or run["created_at"],
        "trigger_name": "手动",
        "questions": len({row["question_id"] for row in group}),
        "engines": [{
            "engine": item["engine"], "sample_n": item["sample_n"],
            "mentioned_n": sum(1 for row in group
                               if row["engine_key"] == item["engine"]["key"] and row["mentioned"]),
            "mention_rate": item["mention_rate"],
        } for item in engines],
        "overall": {
            "sample_n": overall["sample_n"], "mentioned_n": overall["mentioned_n"],
            "mention_rate": overall["mention_rate"],
        },
    }


def _gaps(records):
    gaps = []
    for question_id, group in _group(records, "question_id").items():
        if any(row["mentioned"] for row in group):
            continue
        if not any(row["competitors"] for row in group):
            continue
        gaps.append({
            "question_id": question_id, "question": group[0]["question_text"],
            "intent_name": geo_runs.INTENTS.get(group[0]["intent"], "购买决策"),
            "sample_n": len(group),
        })
    gaps.sort(key=lambda item: -item["sample_n"])
    return gaps[:10], len(gaps)


def _excerpt(text, brand_names, competitor_map):
    text = text or ""
    brand_hits = _offsets(text, brand_names)
    competitor_hits = _offsets(text, list(competitor_map), competitor_map)
    hits = brand_hits + competitor_hits
    if len(text) <= EXCERPT_CHARS:
        start, end = 0, len(text)
    else:
        anchor = min(hit["start"] for hit in hits) if hits else 0
        start = max(0, min(anchor - 150, len(text) - EXCERPT_CHARS))
        end = min(len(text), start + EXCERPT_CHARS)
    sliced = text[start:end]
    marks = []
    for kind, names in (("self", brand_names), ("competitor", list(competitor_map))):
        offsets = [[hit["start"], hit["end"]] for hit in
                   _offsets(sliced, names, competitor_map if kind == "competitor" else None)]
        if offsets:
            marks.append({"kind": kind, "offsets": offsets})
    return {
        "text": sliced, "marks": marks,
        "head_cut": start > 0, "tail_cut": end < len(text),
    }


def _examples(records, brand_names, competitor_map):
    preferred, rest = [], []
    for row in records:
        if row["mentioned"] and row["rank"] == 1:
            preferred.append(row)
        elif row["mentioned"]:
            rest.append(row)
        elif row["competitors"]:
            rest.append(row)
    picked = []
    for group in (preferred, rest):
        for row in group:
            if row["id"] not in {item["id"] for item in picked}:
                picked.append(row)
            if len(picked) >= 3:
                break
        if len(picked) >= 3:
            break
    for row in records:
        if len(picked) >= 3:
            break
        if row["id"] not in {item["id"] for item in picked}:
            picked.append(row)
    return [{
        "question": row["question_text"],
        "engine": {"key": row["engine_key"], "name": row["engine_name"]},
        "time": row["created_at"], "citations": 0,
        "mentioned": row["mentioned"], "rank_no": row["rank"],
        "competitors": row["competitors"],
        "excerpt": _excerpt(row["text"], brand_names, competitor_map),
    } for row in picked[:3]]


def _findings(records, gaps, layers, has_competitors):
    findings = []
    overall = _summary(records)
    if records and len(records) >= 3 and overall["mention_rate"] < 0.3:
        findings.append({
            "level": "danger", "finding": "被提到比例偏低",
            "evidence": f"本期 {overall['sample_n']} 条有效回答中只有 "
                        f"{overall['mentioned_n']} 条提到你",
            "action": "先补品牌事实和内容，再跑一次体检", "to": "/geo/brand",
            "to_label": "去品牌档案",
        })
    if gaps:
        findings.append({
            "level": "warn", "finding": f"有 {gaps} 个问题被竞品占住",
            "evidence": "这些问题 AI 推荐了竞品、一次都没提你",
            "action": "去看这些问题，优先为它们准备内容", "to": "/geo/questions",
            "to_label": "去问题库",
        })
    weak = [row for row in layers["rows"]
            if row["sample_n"] >= 3 and row["mention_rate"] < max(0.3, overall["mention_rate"] - 0.1)]
    if weak:
        item = min(weak, key=lambda row: row["mention_rate"])
        findings.append({
            "level": "warn", "finding": f"{item['name']}这一层偏弱",
            "evidence": f"{item['sample_n']} 条回答里只有 "
                        f"{round(item['mention_rate'] * item['sample_n'])} 条提到你",
            "action": "为这一层写一两篇内容，把这一层的说法自然写进去",
            "to": "/geo/questions?tab=matrix", "to_label": "去词库矩阵",
        })
    if has_competitors and len(records) >= 3 and overall["sov"] < 0.3:
        findings.append({
            "level": "warn", "finding": "同行占比偏低",
            "evidence": f"提到你和竞品的回答里，你只占 "
                        f"{round(overall['sov'] * 100)}%",
            "action": "看竞品在哪些问题里被推荐", "to": "/geo/brand",
            "to_label": "去品牌档案",
        })
    if records and len(records) >= 3 and overall["mention_rate"] >= 0.6:
        findings.append({
            "level": "ok", "finding": "整体表现稳定",
            "evidence": f"本期 {overall['sample_n']} 条回答里有 "
                        f"{overall['mentioned_n']} 条提到你",
            "action": "保持更新，盯住被竞品占住的问题",
        })
    return findings[:4]


def _report(db, project_id, project, period, offset):
    try:
        offset = max(0, min(MAX_OFFSET, int(offset or 0)))
    except (TypeError, ValueError):
        offset = 0
    info = _period(period, offset)
    brand_names = _brand_names(project)
    competitor_map = _competitor_map(db, project_id)
    runs, records = _collect(db, project_id, info["start"], info["end"],
                             brand_names, competitor_map)
    previous = _period(info["period"], offset + 1)
    _, previous_records = _collect(db, project_id, previous["start"], previous["end"],
                                   brand_names, competitor_map)
    current = _summary(records)
    prev = _summary(previous_records) if previous_records else None
    delta = {
        key: (round(current[key] - prev[key], 4)
              if prev and current[key] is not None and prev[key] is not None else None)
        for key in ("mention_rate", "top1_rate", "sov", "accuracy")
    }
    gaps, gap_total = _gaps(records)
    layers = _layer_rows(db, project_id, records)
    return {
        "ok": True,
        "report": {
            "period": info["period"], "period_name": info["period_name"],
            "range": info["range"], "note": geo_runs.METHOD_NOTE,
            "project": {"brand_name": project["brand_name"], "industry": project["industry"],
                        "region": project["region"]},
            "runs": len(runs), "credits": round(sum(float(row["credits"]) for row in runs), 4),
            "current": current, "delta": delta,
            "overview": {"sample_n": current["sample_n"],
                         "mentioned_n": current["mentioned_n"],
                         "mention_rate": current["mention_rate"]},
            "alerts": {"total": 0, "kinds": []},
            "publishing": None, "cited": None,
            "trend": _trend(records, info["start"], info["end"]),
            "engines": _engine_rows(records),
            "latest": _latest(runs, records),
            "questions": _question_rows(records),
            "layers": layers,
            "intents": _intent_rows(records),
            "sources": {"domains": []},
            "gaps": gaps, "gap_total": gap_total,
            "findings": _findings(records, gap_total, layers, bool(competitor_map)),
            "examples": _examples(records, brand_names, competitor_map),
        },
    }


# ---------------------------------------------------------------------------
# Shares

def _share_status(row):
    if row["status"] == "revoked":
        return "revoked", "已撤销"
    if row["expire_at"] <= now():
        return "expired", "已过期"
    return "active", "有效"


def _share_item(row):
    status, status_name = _share_status(row)
    return {
        "id": row["id"], "token": row["token"], "period_name": row["period_name"],
        "range_label": row["range_label"], "status": status, "status_name": status_name,
        "views": row["views"], "expire_at": geo_matrix.display_time(row["expire_at"]),
        "created_at": row["created_at"],
    }


def _create_share(db, project_id, project, body):
    period = str(body.get("period") or "week")
    try:
        offset = max(0, min(MAX_OFFSET, int(body.get("offset") or 0)))
    except (TypeError, ValueError) as error:
        raise commander.ChatError("报告期数无效。") from error
    try:
        days = int(body.get("days") or 7)
    except (TypeError, ValueError) as error:
        raise commander.ChatError("有效期无效。") from error
    if days not in SHARE_DAYS:
        raise commander.ChatError("有效期无效。")
    if period == "matrix":
        geo_matrix.initialize(db)
        report = geo_matrix.document(db, project_id, project)
        report["kind"] = "kw_matrix"
        period_name, range_label = "词库矩阵", report["range"]["label"]
        title = report["title"]
    elif period in {"week", "month"}:
        report = _report(db, project_id, project, period, offset)["report"]
        period_name = report["period_name"]
        range_label = report["range"]["label"]
        title = f"{project['brand_name']} AI 搜索表现{period_name}（{range_label}）"
    elif period == "diag":
        raise commander.ChatError("诊断方案暂未开放。")
    else:
        raise commander.ChatError("报告类型无效。")
    token = secrets.token_urlsafe(24)
    expire_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    key = db.execute(
        "INSERT INTO geo_report_shares (tenant_id, project_id, token, period, offset, days, "
        "title, period_name, range_label, payload, status, views, created_at, expire_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (commander.tenant(), int(project_id), token, period, offset, days, title,
         period_name, range_label, json.dumps({"report": report}, ensure_ascii=False),
         "active", 0, now(), expire_at),
    ).lastrowid
    tenancy.audit_in(db, commander.tenant(), "geo.report.share", str(project_id),
                     f"share={key}; period={period}; days={days}")
    row = db.execute("SELECT * FROM geo_report_shares WHERE id=?", (key,)).fetchone()
    return {"ok": True, "share": _share_item(row)}


def _list_shares(db, project_id, query):
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = max(1, min(100, int((query.get("page_size") or query.get("pageSize") or ["8"])[0])))
    except ValueError as error:
        raise commander.ChatError("分页参数无效。") from error
    where, values = "WHERE project_id=? AND tenant_id=?", [int(project_id), commander.tenant()]
    rows = db.execute(
        f"SELECT * FROM geo_report_shares {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*values, size, (page - 1) * size],
    ).fetchall()
    total = db.execute(f"SELECT COUNT(*) FROM geo_report_shares {where}", values).fetchone()[0]
    return {"ok": True, "items": [_share_item(row) for row in rows], "total": total}


def _revoke(db, share_id):
    row = db.execute(
        "SELECT id FROM geo_report_shares WHERE id=? AND tenant_id=?",
        (int(share_id), commander.tenant()),
    ).fetchone()
    if row is None:
        raise commander.ChatError("分享链接不存在。")
    changed = db.execute(
        "UPDATE geo_report_shares SET status='revoked' "
        "WHERE id=? AND tenant_id=? AND status='active'",
        (int(share_id), commander.tenant()),
    )
    if changed.rowcount:
        tenancy.audit_in(db, commander.tenant(), "geo.report.revoke", str(share_id))
    return {"ok": True}


def public(path):
    path = canonical(path)
    match = PUBLIC.fullmatch(path)
    if not match:
        return 404, {"ok": False, "error": "分享链接不存在或已撤销。"}
    token = match[1]
    with commander.database() as db:
        initialize(db)
        row = db.execute("SELECT * FROM geo_report_shares WHERE token=?", (token,)).fetchone()
        if row is None:
            return 200, {"ok": False, "error": "分享链接不存在或已撤销。"}
        status, _ = _share_status(row)
        if status != "active":
            return 200, {"ok": False, "error": "链接已失效。"}
        db.execute("UPDATE geo_report_shares SET views=views+1 WHERE id=?", (row["id"],))
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            payload = {}
        report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
        return 200, {"ok": True, "report": report, "expire_at": geo_matrix.display_time(row["expire_at"])}


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
        if suffix == "reports":
            period = (query.get("period") or ["week"])[0]
            if period not in {"week", "month"}:
                raise commander.ChatError("报告类型无效。")
            offset = (query.get("offset") or ["0"])[0]
            return _report(db, project_id, project, period, offset)
        if suffix == "report-shares":
            return _list_shares(db, project_id, query)
    raise commander.ChatError("GEO 接口暂不可用。")


def write(method, path, body):
    path = canonical(path)
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        share_match = SHARE.fullmatch(path)
        if share_match and method == "DELETE":
            return _revoke(db, share_match[1])
        match = PROJECT.fullmatch(path)
        if not match:
            raise commander.ChatError("GEO 接口暂不可用。")
        project_id, suffix = int(match[1]), (match[2] or "").strip("/")
        project = _project(db, project_id)
        if suffix == "report-shares" and method == "POST":
            return _create_share(db, project_id, project, body)
    raise commander.ChatError("GEO 接口暂不可用。")
