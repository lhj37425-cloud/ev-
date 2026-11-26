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
import geo_sources
import payments
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
    return bool(match and (match[2] or "").strip("/") in {
        "reports", "report-shares", "overview", "gaps",
    })


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


def _fact_rows(db, project_id):
    try:
        import geo_brand
        geo_brand.initialize(db)
        rows = db.execute(
            "SELECT label,value FROM geo_facts WHERE tenant_id=? AND project_id=? "
            "AND status='confirmed'", (commander.tenant(), int(project_id))).fetchall()
    except Exception:
        return []
    return [{"label": row["label"], "value": row["value"]} for row in rows]


def _brand_window(text, brand_names):
    lowered = text.lower()
    start = -1
    for name in brand_names:
        index = lowered.find(str(name).lower())
        if index >= 0 and (start < 0 or index < start):
            start = index
    if start < 0:
        return ""
    return text[max(0, start - 120): start + 200]


def _accuracy(records, facts, brand_names):
    """Rule-based fact check: year/phone near the brand mention."""
    years, phones = set(), set()
    for fact in facts:
        label = (fact["label"] + " " + fact["value"]).lower()
        value = fact["value"]
        if "成立" in label or "founded" in label:
            match = re.search(r"(?:19|20)\d{2}", value)
            if match:
                years.add(match.group(0))
        if "电话" in label or "phone" in label or "tel" in label:
            number = re.sub(r"[^\d]", "", value)
            if 6 <= len(number) <= 20:
                phones.add(number)
    if not years and not phones:
        return None, 0, 0
    right = wrong = 0
    for record in records:
        if not record.get("mentioned"):
            continue
        window = _brand_window(record.get("text") or "", brand_names or [])
        if not window:
            continue
        if years:
            found = re.findall(r"(?:19|20)\d{2}", window)
            if found:
                if any(year in years for year in found):
                    right += 1
                else:
                    wrong += 1
        if phones:
            digits = re.sub(r"[^\d]", "", window)
            if any(phone in digits for phone in phones):
                right += 1
    if right + wrong == 0:
        return None, 0, 0
    return round(right / (right + wrong), 4), right, wrong


def _summary(records, facts=None, brand_names=None):
    sample_n = len(records)
    mentioned_n = sum(1 for row in records if row["mentioned"])
    top1_n = sum(1 for row in records if row["top1"])
    competitor_n = sum(len(row["competitors"]) for row in records)
    denominator = mentioned_n + competitor_n
    accuracy, right, wrong = _accuracy(records, facts or [], brand_names or [])
    return {
        "sample_n": sample_n, "mentioned_n": mentioned_n,
        "mention_rate": _rate(mentioned_n, sample_n),
        "top1_rate": _rate(top1_n, sample_n),
        "sov": round(mentioned_n / denominator, 4) if denominator else 0,
        "accuracy": accuracy, "right": right, "wrong": wrong,
    }


def _records_for_runs(db, project_id, runs, brand_names, competitor_map):
    records = []
    if not runs:
        return records
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
    return records


def _collect(db, project_id, start, end, brand_names, competitor_map):
    runs = db.execute(
        "SELECT * FROM geo_runs WHERE project_id=? AND tenant_id=? AND status='done' "
        "AND created_at>=? AND created_at<? ORDER BY id",
        (int(project_id), commander.tenant(), start.isoformat(timespec="seconds"),
         end.isoformat(timespec="seconds")),
    ).fetchall()
    return runs, _records_for_runs(db, project_id, runs, brand_names, competitor_map)


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
        competitors = sorted({name for row in group for name in row["competitors"]})
        rows.append({
            "question_id": question_id, "question": group[0]["question_text"],
            "intent_name": geo_runs.INTENTS.get(group[0]["intent"], "购买决策"),
            "mentioned_n": sum(1 for row in group if row["mentioned"]),
            "sample_n": len(group), "mention_rate": rate,
            "best_rank": min(ranks) if ranks else None,
            "competitors": [{"name": name} for name in competitors],
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


def _gaps_detail(db, project_id, project):
    """Questions the latest run let competitors own (Sources page section)."""
    geo_runs.initialize(db)
    geo_brand.initialize(db)
    run = db.execute(
        "SELECT * FROM geo_runs WHERE project_id=? AND tenant_id=? AND status='done' "
        "ORDER BY id DESC LIMIT 1", (int(project_id), commander.tenant()),
    ).fetchone()
    if run is None:
        return {"ok": True, "run": None, "items": []}
    records = _records_for_runs(db, project_id, [run], _brand_names(project),
                                _competitor_map(db, project_id))
    grouped = _group(records, "question_id")
    items = []
    for question_id, group in grouped.items():
        if any(row["mentioned"] for row in group):
            continue
        counts = {}
        for row in group:
            for name in row["competitors"]:
                counts[name] = counts.get(name, 0) + 1
        if not counts:
            continue
        engines = sorted({row["engine_name"] for row in group if row["competitors"]})
        items.append({
            "question_id": question_id, "question": group[0]["question_text"],
            "intent_name": geo_runs.INTENTS.get(group[0]["intent"], "购买决策"),
            "sample_n": len(group),
            "competitors": [{"name": name, "answers": count}
                            for name, count in sorted(counts.items(), key=lambda item: -item[1])],
            "engines": engines,
        })
    items.sort(key=lambda item: (-item["sample_n"], item["question_id"]))
    return {"ok": True,
            "run": {"id": run["id"], "time": run["finished_at"] or run["created_at"]},
            "items": items}


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
        "time": row["created_at"], "citations": len(geo_sources.extract_links(row["text"])),
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


def _text(value, limit):
    return str(value).strip()[:limit] if isinstance(value, str) else ""


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


def summarise(records):
    """Public wrapper for the mention/top1/sov summary used by reports."""
    return _summary(records)


def question_rows(records):
    """Public wrapper for per-question report rows (used by content gaps)."""
    return _question_rows(records)


def run_context(db, project_id, run_id):
    """Project, run row and mention-annotated answers for one completed run."""
    project = _project(db, project_id)
    brand_names = _brand_names(project)
    competitor_map = _competitor_map(db, project_id)
    run = db.execute(
        "SELECT * FROM geo_runs WHERE id=? AND project_id=? AND tenant_id=?",
        (int(run_id), int(project_id), commander.tenant()),
    ).fetchone()
    if run is None:
        raise commander.ChatError("体检记录不存在。")
    records = _records_for_runs(db, project_id, [run], brand_names, competitor_map)
    return project, run, records, brand_names, competitor_map


def _engine_list(run):
    try:
        keys = json.loads(run["engines"])
    except (TypeError, ValueError):
        keys = []
    return [{"key": key, "name": geo_runs.ENGINES.get(key, key), "approx": False}
            for key in keys if isinstance(key, str)]


def diagnosis_messages(db, project_id, run_id):
    """Build the single text-model call that writes the analysis sections."""
    project, run, records, brand_names, competitor_map = run_context(db, project_id, run_id)
    summary = _summary(records)
    questions = _question_rows(records)
    gaps, gap_total = _gaps(records)
    geo_brand.initialize(db)
    facts = db.execute(
        "SELECT label, value FROM geo_facts WHERE project_id=? AND tenant_id=? "
        "AND status='confirmed' ORDER BY id LIMIT 20",
        (int(project_id), commander.tenant()),
    ).fetchall()
    samples = []
    for row in _examples(records, brand_names, competitor_map)[:6]:
        samples.append({
            "question": row["question"],
            "engine": row["engine"]["name"],
            "mentioned": row["mentioned"],
            "text": row["excerpt"]["text"][:500],
        })
    payload = {
        "brand": {
            "name": project["brand_name"], "industry": project["industry"],
            "region": project["region"], "description": project["description"],
            "facts": [{"label": row["label"], "value": row["value"]} for row in facts],
        },
        "run": {
            "engines": [item["name"] for item in _engine_list(run)],
            "questions": len(questions), "samples": run["samples"],
            "ok": len(records), "total": run["total"],
            "mention_rate": summary["mention_rate"],
            "top1_rate": summary["top1_rate"],
            "sov": summary["sov"],
        },
        "questions": [{
            "id": item["question_id"], "text": item["question"],
            "mentioned_n": item["mentioned_n"], "sample_n": item["sample_n"],
            "competitors": [entry["name"] for entry in item["competitors"]],
        } for item in questions],
        "gaps": [{"question": item["question"], "sample_n": item["sample_n"]}
                 for item in gaps],
        "gap_total": gap_total,
        "answers": samples,
    }
    system = (
        "你是 GEO 诊断顾问。只根据给定的品牌事实、问题和回答摘要写分析段，"
        "不要编造数字、客户、案例或资料里没有的结论。用简体中文。"
        "只输出一个 JSON 对象，不要解释或 Markdown。格式："
        "{\"positioning\":\"品牌与业务定位\",\"buyer\":\"提问的人和他们看重什么\","
        "\"criteria\":[{\"name\":\"比较维度\",\"detail\":\"说明\"}],"
        "\"questions\":[{\"id\":数字,\"intent\":\"问的人想决定什么\"}],"
        "\"answer_shape\":\"AI 推荐了谁、回答大致是什么样\","
        "\"summary\":\"总结与下一步\"}。"
        "positioning/buyer/answer_shape/summary 各 2-4 句；criteria 3-6 条；"
        "questions 必须覆盖输入的每个 id，intent 每条不超过 80 字。"
    )
    user = json.dumps(payload, ensure_ascii=False)[:7000]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _clean_ai(value, valid_ids):
    if not isinstance(value, dict):
        return None
    result = {
        "positioning": _text(value.get("positioning"), 1200),
        "buyer": _text(value.get("buyer"), 1000),
        "answer_shape": _text(value.get("answer_shape"), 1000),
        "summary": _text(value.get("summary"), 1200),
        "criteria": [],
        "questions": [],
    }
    criteria = value.get("criteria")
    if isinstance(criteria, list):
        for item in criteria[:8]:
            if not isinstance(item, dict):
                continue
            name = _text(item.get("name"), 60)
            detail = _text(item.get("detail"), 300)
            if name:
                result["criteria"].append({"name": name, "detail": detail})
    questions = value.get("questions")
    if isinstance(questions, list):
        seen = set()
        for item in questions:
            if not isinstance(item, dict):
                continue
            try:
                question_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if question_id not in valid_ids or question_id in seen:
                continue
            seen.add(question_id)
            result["questions"].append({
                "id": question_id, "intent": _text(item.get("intent"), 300),
            })
    if not any([result["positioning"], result["buyer"], result["answer_shape"],
                result["summary"], result["criteria"], result["questions"]]):
        return None
    return result


def parse_ai(raw, question_ids):
    """Parse the model's analysis JSON; invalid/empty output becomes None."""
    return _clean_ai(_model_json(raw), set(question_ids))


def _diagnosis_scope(run, records):
    engines = _engine_list(run)
    questions = len({row["question_id"] for row in records})
    if not questions:
        per_engine = len(engines) * max(1, int(run["samples"] or 1))
        questions = int(run["total"]) // per_engine if per_engine else 0
    ok = len(records)
    total = int(run["total"])
    return {
        "run_time": run["finished_at"] or run["created_at"],
        "trigger_name": "手动", "engines": engines,
        "questions": questions, "samples": run["samples"],
        "total": total, "ok": ok, "invalid": max(0, total - ok),
        "citations": 0, "unique_domains": 0, "avg_citations": None,
        "note": f"数字来自这次体检的 {ok} 条有效回答（共尝试 {total} 条，"
                f"{max(0, total - ok)} 条没拿到回答）；{geo_runs.METHOD_NOTE}",
    }


def _diagnosis_candidates(project, records, competitor_map):
    items = []
    own = [row for row in records if row["mentioned"]]
    items.append({
        "name": project["brand_name"], "kind": "self", "kind_name": "你",
        "answers": len(own),
        "questions": len({row["question_id"] for row in own}),
        "engines": len({row["engine_key"] for row in own}),
    })
    for name in dict.fromkeys(competitor_map.values()):
        group = [row for row in records if name in row["competitors"]]
        if not group:
            continue
        items.append({
            "name": name, "kind": "competitor", "kind_name": "竞品",
            "answers": len(group),
            "questions": len({row["question_id"] for row in group}),
            "engines": len({row["engine_key"] for row in group}),
        })
    items.sort(key=lambda item: (item["kind"] != "self", -item["answers"]))
    return items


def _diagnosis_targets(summary):
    return {
        "baseline": summary["mention_rate"], "baseline_n": summary["sample_n"],
        "low_sample": summary["sample_n"] < 10,
        "rows": [{"weeks": weeks, "month": month, "target": None, "low": None,
                  "high": None, "basis": "没有足够的真实数据，先不给"}
                 for weeks, month in ((4, 1), (8, 2), (12, 3))],
        "own": None,
        "notes": [
            "参考目标只用真实数据算：优先看平台上其它项目在同样的时间点实际变化了多少"
            "（起点相近的优先，不足 5 个项目不给），其次看本项目自己的走势。",
            "目前本项目的体检历史不足，先不给目标；至少积累两周以上再算。",
        ],
    }


def _diagnosis_plan(run, questions, engines):
    per_run = int(run["total"])
    price = payments.geo_answer_price()
    charging = payments.charging_enabled()
    credits_weekly = round(per_run * price, 4) if charging else 0
    volume = {
        "questions": questions, "engines": len(engines),
        "samples": run["samples"], "per_run": per_run,
        "weekly": per_run, "total_12w": per_run * 12, "schedule": "weekly",
        "schedule_note": "按每周一次体检算；可以在「自动执行」里开启定期体检。",
        "credits_weekly": credits_weekly, "credits_12w": round(credits_weekly * 12, 4),
        "credits_basis": (f"按这次体检的单价 {price} 算力/条成功回答。"
                          if charging else ""),
    }
    months = [
        {"month": 1, "weeks": "第 1–4 周", "name": "补基础",
         "goal": "把品牌事实、竞品和可引用资料补齐，先处理被竞品占住的问题。",
         "articles": 2,
         "tasks": [
             {"text": "补齐品牌事实和竞品", "to": "/geo/brand"},
             {"text": "看被竞品占住的问题", "to": "/geo/questions"},
             {"text": "给最弱的层次写一篇内容", "to": "/geo/questions?tab=matrix"},
         ]},
        {"month": 2, "weeks": "第 5–8 周", "name": "扩覆盖",
         "goal": "为薄弱问题和层次补内容，覆盖更多客户问法。",
         "articles": 3,
         "tasks": [
             {"text": "按层次补词和问题", "to": "/geo/questions?tab=matrix"},
             {"text": "为薄弱问题写内容", "to": "/geo/questions"},
             {"text": "再跑一次体检看变化", "to": "/geo/monitor"},
         ]},
        {"month": 3, "weeks": "第 9–12 周", "name": "看变化",
         "goal": "对比第 1 周和第 12 周，按体检信号调整。",
         "articles": 2,
         "tasks": [
             {"text": "看效果报告对比", "to": "/geo/reports"},
             {"text": "按信号调整内容", "to": "/geo/questions?tab=matrix"},
         ]},
    ]
    signals = [
        {"when": "连续两周被提到比例没有提升", "within": "2 周内",
         "do": "先检查问题库和内容是否覆盖主要问法，再调整"},
        {"when": "某个问题一直被竞品占住", "within": "下次体检前",
         "do": "为它单独写一篇能直接回答的内容"},
        {"when": "AI 仍然没有给出引用来源", "within": "1 个月内",
         "do": "先补官网和品牌知识页，把可核对的资料做全"},
    ]
    return {"volume": volume, "months": months, "signals": signals}


def _diagnosis_gaps(records, summary, gaps, gap_total, layers, competitor_map):
    items = []
    for gap in gaps[:4]:
        items.append({
            "key": f"gap-{gap['question_id']}", "level": "danger",
            "title": f"「{gap['question']}」被竞品占住",
            "evidence": f"这次 {gap['sample_n']} 条回答都没提到你，竞品被推荐",
            "fix": "为这个问题写一篇能直接回答的内容",
            "to": "/geo/questions?tab=matrix", "to_label": "去写内容",
        })
    if summary["sample_n"] >= 3 and summary["mention_rate"] < 0.3:
        items.append({
            "key": "coverage", "level": "warn", "title": "整体被提到比例偏低",
            "evidence": f"{summary['sample_n']} 条回答里只有 "
                        f"{summary['mentioned_n']} 条提到你",
            "fix": "先补品牌事实和可引用资料，再按问题写内容",
            "to": "/geo/brand", "to_label": "去品牌档案",
        })
    if not competitor_map:
        items.append({
            "key": "competitors", "level": "info", "title": "还没设置竞品",
            "evidence": "看不出 AI 更偏向谁，也判断不了同行占比",
            "fix": "在品牌档案里加几个竞品", "to": "/geo/brand",
            "to_label": "去设竞品",
        })
    if layers["unlayered"]:
        items.append({
            "key": "layers", "level": "info",
            "title": f"有 {layers['unlayered']} 个问题没标层次",
            "evidence": "按需求层次分析时，这些回答不算在里面",
            "fix": "在词库矩阵把问题按层次整理",
            "to": "/geo/questions?tab=matrix", "to_label": "去词库矩阵",
        })
    return items[:5]


def _diagnosis_headline(summary, gap_total, competitor_map):
    text = (f"这次 {summary['sample_n']} 条有效回答里，{summary['mentioned_n']} 条提到你，"
            f"被提到比例 {round(summary['mention_rate'] * 100)}%；"
            f"排第一比例 {round(summary['top1_rate'] * 100)}%。")
    if gap_total:
        text += f"有 {gap_total} 个问题被竞品占住，优先处理。"
    else:
        text += "没有发现明显被竞品占住的问题。"
    if not competitor_map:
        text += "还没设置竞品，暂时看不出 AI 更偏向谁。"
    return text


def diagnosis_data(db, project_id, run_id, ai=None, ai_note="", generated_at=None):
    """The complete DiagnosisBody object for one completed run."""
    project, run, records, brand_names, competitor_map = run_context(db, project_id, run_id)
    facts = _fact_rows(db, project_id)
    summary = _summary(records, facts, brand_names)
    engines = _engine_rows(records)
    questions = _question_rows(records)
    layers = _layer_rows(db, project_id, records)
    gaps, gap_total = _gaps(records)
    examples = _examples(records, brand_names, competitor_map)
    scope = _diagnosis_scope(run, records)
    try:
        source_data = geo_sources.diagnosis_sources(db, project_id, records)
        engine_data = geo_sources.engine_citations(db, project_id, records)
    except Exception:
        source_data = {"total": 0, "types": [], "domains": [], "unique_domains": 0,
                       "own": 0, "competitor": 0,
                       "ranking": {"titled": 0, "count": 0, "share": 0, "answers": 0,
                                   "answers_self": 0, "examples": []}}
        engine_data = {}
    ok = scope["ok"]
    scope.update({
        "citations": source_data["total"],
        "unique_domains": source_data["unique_domains"],
        "avg_citations": (round(source_data["total"] / ok, 1) if ok and source_data["total"]
                          else (0 if ok else None)),
    })
    for item in engines:
        source = engine_data.get(item["engine"]["key"], {})
        domains = source.get("domains", [])
        if item["mention_rate"] < 0.3:
            focus = "这家 AI 很少提到你，先把品牌事实和官网资料做全。"
            action = {"to": "/geo/brand", "label": "去补品牌资料"}
        elif gaps:
            focus = "为被竞品占住的问题写内容，增加可被引用的说法。"
            action = {"to": "/geo/questions?tab=matrix", "label": "去写内容"}
        else:
            focus = "保持官网和品牌知识页更新，方便 AI 引用。"
            action = {"to": "/geo/brand", "label": "维护品牌资料"}
        item.update({
            "citations": source.get("citations", 0), "domains": domains,
            "focus": {"main": [{"name": row["domain"], "count": row["count"]}
                               for row in domains[:3]],
                      "focus": focus, "action": action},
        })
    ai_data = _clean_ai(ai, {item["question_id"] for item in questions}) if ai else None
    if ai_data is None and not ai_note:
        ai_note = "分析段这次没写出来，数据部分不受影响。"
    plan = _diagnosis_plan(run, scope["questions"], scope["engines"])
    return {
        "title": f"{project['brand_name']} AI 搜索表现诊断与优化方案",
        "generated_at": generated_at or geo_matrix.display_time(now()),
        "scope": scope,
        "headline": {
            "mention_rate": summary["mention_rate"],
            "mentioned_n": summary["mentioned_n"],
            "top1_rate": summary["top1_rate"],
            "accuracy": summary["accuracy"],
            "right": summary["right"], "wrong": summary["wrong"],
        },
        "headline_text": _diagnosis_headline(summary, gap_total, competitor_map),
        "ai": ai_data, "ai_note": ai_note,
        "questions": questions,
        "layers": layers,
        "engines": engines,
        "candidates": _diagnosis_candidates(project, records, competitor_map),
        "candidates_note": "只统计你自己和品牌档案里已设置的竞品；其他品牌暂不统计。",
        "sources": source_data,
        "gaps": _diagnosis_gaps(records, summary, gaps, gap_total, layers, competitor_map),
        "targets": _diagnosis_targets(summary),
        "plan": plan,
        "examples": examples,
        "boundaries": [
            f"数字来自这次体检的 {scope['ok']} 条有效回答；没拿到回答的不计入比例。",
            geo_runs.METHOD_NOTE,
            ("引用来源只统计回答里明确出现的 http(s) 链接；同一回答同一网站只算一次。"
             if source_data["total"] else
             "这次没有采集到引用来源，来源类型和官网引用数据暂不展示。"),
            "参考目标只用真实数据算；本项目历史不足时先不给目标。",
            "分析段由 AI 根据回答摘要写，不改变数字；没写出来时只显示数据部分。",
        ],
    }


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
    facts = _fact_rows(db, project_id)
    current = _summary(records, facts, brand_names)
    prev = _summary(previous_records, facts, brand_names) if previous_records else None
    delta = {
        key: (round(current[key] - prev[key], 4)
              if prev and current[key] is not None and prev[key] is not None else None)
        for key in ("mention_rate", "top1_rate", "sov", "accuracy")
    }
    gaps, gap_total = _gaps(records)
    layers = _layer_rows(db, project_id, records)
    try:
        import geo_content
        publishing = geo_content.publishing_summary(
            db, project_id, info["start"].isoformat(timespec="seconds"),
            info["end"].isoformat(timespec="seconds"))
    except Exception:
        publishing = None
    try:
        source_data = geo_sources.report_data(db, project, info["start"], info["end"])
        cited = geo_sources.cited_report(db, project_id, project, info["start"], info["end"])
    except Exception:
        source_data = {"domains": []}
        cited = None
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
            "publishing": publishing, "cited": cited,
            "trend": _trend(records, info["start"], info["end"]),
            "engines": _engine_rows(records),
            "latest": _latest(runs, records),
            "questions": _question_rows(records),
            "layers": layers,
            "intents": _intent_rows(records),
            "sources": source_data,
            "gaps": gaps, "gap_total": gap_total,
            "findings": _findings(records, gap_total, layers, bool(competitor_map)),
            "examples": _examples(records, brand_names, competitor_map),
        },
    }


def _overview(db, project_id, project):
    tenant = commander.tenant()
    done = db.execute(
        "SELECT * FROM geo_runs WHERE project_id=? AND tenant_id=? AND status='done' "
        "ORDER BY id DESC LIMIT 2",
        (int(project_id), tenant),
    ).fetchall()
    brand_names = _brand_names(project)
    competitor_map = _competitor_map(db, project_id)
    latest = done[0] if done else None
    previous = done[1] if len(done) > 1 else None
    facts = _fact_rows(db, project_id)
    current = _summary(_records_for_runs(db, project_id, [latest], brand_names,
                                         competitor_map), facts, brand_names) if latest else None
    prev = _summary(_records_for_runs(db, project_id, [previous], brand_names,
                                      competitor_map), facts, brand_names) if previous else None
    delta = {
        key: (round(current[key] - prev[key], 4)
              if current and prev and current[key] is not None and prev[key] is not None
              else None)
        for key in ("mention_rate", "top1_rate", "sov", "accuracy")
    }
    latest_records = _records_for_runs(db, project_id, [latest], brand_names,
                                       competitor_map) if latest else []
    if current is not None:
        ranks = [row["rank"] for row in latest_records if row["rank"]]
        current = {
            **current,
            "low_sample": current["sample_n"] < 10,
            "avg_rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
        }
    now_utc = datetime.now(timezone.utc)
    start = datetime.combine((now_utc - timedelta(days=29)).date(), datetime.min.time(),
                             tzinfo=timezone.utc)
    end = datetime.combine((now_utc + timedelta(days=1)).date(), datetime.min.time(),
                           tzinfo=timezone.utc)
    _, trend_records = _collect(db, project_id, start, end, brand_names, competitor_map)
    counts = geo_runs.counts(db, project_id)
    geo_brand.initialize(db)
    facts_pending = db.execute(
        "SELECT COUNT(*) FROM geo_facts WHERE project_id=? AND tenant_id=? AND status='pending'",
        (int(project_id), tenant),
    ).fetchone()[0]
    runs_done = db.execute(
        "SELECT COUNT(*) FROM geo_runs WHERE project_id=? AND tenant_id=? AND status='done'",
        (int(project_id), tenant),
    ).fetchone()[0]
    running = db.execute(
        "SELECT done,failed,total FROM geo_runs WHERE project_id=? AND tenant_id=? "
        "AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
        (int(project_id), tenant),
    ).fetchone()
    import geo_diagnosis
    geo_diagnosis.initialize(db)
    alerts_open = db.execute(
        "SELECT COUNT(*) FROM geo_alerts WHERE project_id=? AND tenant_id=? AND status='open'",
        (int(project_id), tenant),
    ).fetchone()[0]
    try:
        import geo_content
        geo_content.initialize(db)
        content_total = db.execute(
            "SELECT COUNT(*) FROM geo_contents WHERE project_id=? AND tenant_id=?",
            (int(project_id), tenant)).fetchone()[0]
        content_ready = db.execute(
            "SELECT COUNT(*) FROM geo_contents WHERE project_id=? AND tenant_id=? "
            "AND status='ready'", (int(project_id), tenant)).fetchone()[0]
        published_pubs = db.execute(
            "SELECT COUNT(*) FROM geo_publications WHERE project_id=? AND tenant_id=? "
            "AND status='published'", (int(project_id), tenant)).fetchone()[0]
        tickets_todo = db.execute(
            "SELECT COUNT(*) FROM geo_tickets WHERE project_id=? AND tenant_id=? "
            "AND status='todo'", (int(project_id), tenant)).fetchone()[0]
    except Exception:
        content_total = content_ready = published_pubs = tickets_todo = 0
    try:
        cited_pubs, cited_items = geo_sources.overview_cited(db, project_id)
    except Exception:
        cited_pubs, cited_items = 0, []
    last_run = None
    if latest is not None:
        last_run = {
            "finished_at": latest["finished_at"], "started_at": latest["started_at"],
            "samples": latest["samples"],
            "questions": len({row["question_id"] for row in latest_records}),
            "trigger_name": "手动", "total": latest["total"],
            "done": latest["done"], "failed": latest["failed"],
        }
    return {
        "ok": True,
        "running": ({"done": running["done"], "failed": running["failed"],
                     "total": running["total"]} if running else None),
        "current": current, "delta": delta,
        "last_run": last_run,
        "trend": _trend(trend_records, start, end),
        "engines": [{**item, "neg": 0} for item in _engine_rows(latest_records)],
        "counts": {
            "facts_confirmed": counts["facts_confirmed"], "facts_pending": facts_pending,
            "competitors": counts["competitors"], "runs_done": runs_done,
            "questions": counts["questions"], "cited_pubs": cited_pubs,
        },
        "content_counts": {"total": content_total, "ready": content_ready,
                           "published_pubs": published_pubs, "tickets_todo": tickets_todo},
        "alerts_open": alerts_open, "todos": [], "cited": cited_items,
        "ai_visits": {"installed": False}, "by_locale": [], "steps": [],
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
        import geo_diagnosis
        report = geo_diagnosis.share_snapshot(db, project_id, body.get("diagnosis_id"))
        period_name = "诊断方案"
        range_label = report.get("scope", {}).get("run_time", "")
        title = report["title"]
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
        if suffix == "overview":
            return _overview(db, project_id, project)
        if suffix == "gaps":
            return _gaps_detail(db, project_id, project)
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
