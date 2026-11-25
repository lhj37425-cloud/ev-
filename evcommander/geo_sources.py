"""GEO citation parsing: which websites AI answers actually cite.

Scope (口径) is deliberately conservative: only explicit ``http(s)`` links that
appear in an AI answer count as citations — Markdown links and bare URLs. Bare
domains without a protocol are **not** guessed, and a link is counted once per
answer. The same answer citing several pages of one site still counts once for
that site's "answers" number, while ``count`` counts the individual links.

Citations are parsed on demand from the stored answers (runs are capped at a
few answers each, so this stays cheap). The parsed data feeds:

* ``GET /api/geo/projects/{id}/sources`` — the "引用网站与竞品" page;
* weekly/monthly reports — ``report.sources.domains`` and ``report.cited``;
* the project overview — "N 篇被 AI 引用";
* diagnosis bodies — source types/domains and own/competitor mentions;
* ``geo_publications.cited_count`` for the publish list and hub pages.

``POST .../cited-pages/analyze`` (AI breakdown of cited pages) is explicitly
not open yet: it would fetch third-party pages and spend model credits.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import ipaddress
import re
from urllib.parse import unquote, urlsplit, urlunsplit

import commander
import geo_brand
import geo_matrix
import geo_runs


PROJECT = re.compile(r"/api/geo/projects/([0-9]{1,16})(?:/(.*))?$")
SOURCES_SUFFIX = "sources"
ANALYZE_SUFFIX = "cited-pages/analyze"
DEFAULT_DAYS = 30
MAX_SAMPLES = 4
MAX_DOMAINS = 10
MAX_LINKS_PER_ANSWER = 20
MARKDOWN_LINK = re.compile(r"\[([^\]\n]{0,200})\]\(\s*(https?://[^)\s]{1,500})\s*\)")
BARE_URL = re.compile(r"https?://[^\s<>\"'）)】\]，。；;！!？?]+")
TRAILING = ".,;:!?)]}>'\"”’。，；：！？、"

SOURCE_TYPES = [
    {"key": "official", "name": "品牌官网"},
    {"key": "wiki", "name": "百科"},
    {"key": "media", "name": "媒体 / 新闻"},
    {"key": "community", "name": "社区 / 问答"},
    {"key": "ecommerce", "name": "电商"},
    {"key": "review", "name": "点评 / 榜单"},
    {"key": "social", "name": "社交 / 自媒体"},
    {"key": "academic", "name": "政府 / 学术"},
    {"key": "other", "name": "其他"},
]
TYPE_NAMES = {item["key"]: item["name"] for item in SOURCE_TYPES}
TYPE_SUFFIXES = {
    "wiki": ("baike.baidu.com", "wikipedia.org", "baike.so.com", "baike.sogou.com",
             "wikiwand.com", "fandom.com"),
    "media": ("xinhuanet.com", "people.com.cn", "chinanews.com.cn", "cctv.com",
              "thepaper.cn", "yicai.com", "cls.cn", "jiemian.com", "36kr.com",
              "huxiu.com", "tmtpost.com", "sina.com.cn", "sohu.com", "163.com",
              "qq.com", "ifeng.com", "caixin.com", "reuters.com", "bloomberg.com",
              "forbes.com", "nytimes.com", "techcrunch.com", "theverge.com",
              "wired.com", "cnbc.com", "ft.com", "wsj.com"),
    "community": ("zhihu.com", "douban.com", "tieba.baidu.com", "v2ex.com",
                  "reddit.com", "quora.com", "stackoverflow.com", "github.com",
                  "gitee.com", "csdn.net", "juejin.cn", "segmentfault.com"),
    "ecommerce": ("taobao.com", "tmall.com", "jd.com", "pinduoduo.com", "suning.com",
                  "amazon.com", "amazon.cn", "ebay.com", "shopify.com",
                  "alibaba.com", "1688.com"),
    "review": ("dianping.com", "meituan.com", "yelp.com", "tripadvisor.com",
               "g2.com", "capterra.com", "gartner.com", "trustpilot.com"),
    "social": ("weibo.com", "xiaohongshu.com", "douyin.com", "bilibili.com",
               "mp.weixin.qq.com", "medium.com", "x.com", "twitter.com",
               "facebook.com", "linkedin.com", "youtube.com", "instagram.com",
               "tiktok.com"),
    "academic": ("gov.cn", "edu.cn", "cnki.net", "arxiv.org", "researchgate.net",
                 "ieee.org", "acm.org"),
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
    return bool(match and (match[2] or "").strip("/") == SOURCES_SUFFIX)


def can_write(path):
    path = canonical(path)
    match = PROJECT.fullmatch(path)
    return bool(match and (match[2] or "").strip("/") == ANALYZE_SUFFIX)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _project(db, key):
    row = db.execute("SELECT * FROM geo_projects WHERE id=? AND tenant_id=?",
                     (int(key), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("GEO 项目不存在。")
    return row


def _host(value):
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else f"//{value}")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _matches(host, suffix):
    return host == suffix or host.endswith("." + suffix)


def _site_hosts(db, project):
    own = {_host(project["website"])}
    own.discard("")
    competitors = set()
    try:
        geo_brand.initialize(db)
        rows = db.execute("SELECT website FROM geo_competitors WHERE project_id=? AND tenant_id=?",
                          (project["id"], commander.tenant())).fetchall()
        for row in rows:
            host = _host(row["website"])
            if host:
                competitors.add(host)
    except Exception:
        pass
    return own, competitors


def _private(host):
    if host in {"localhost"} or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved


def _normalize(raw):
    """Return ``(url, domain, key)`` or ``None`` for unusable links."""
    value = str(raw or "").strip().strip("<>")
    while value and value[-1] in TRAILING:
        value = value[:-1]
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or _private(host):
        return None
    path = unquote(parsed.path) or "/"
    clean = path.rstrip("/") or "/"
    url = urlunsplit((parsed.scheme, host, path, parsed.query, ""))
    return url, host, (host, clean)


def _source_type(host, own, competitors):
    if any(_matches(host, item) for item in own) or any(_matches(host, item) for item in competitors):
        return "official"
    for key, suffixes in TYPE_SUFFIXES.items():
        if any(_matches(host, suffix) for suffix in suffixes):
            return key
    return "other"


def _clean_title(value):
    title = re.sub(r"[*_`#>]+", "", str(value or "")).strip()
    return title[:200]


def extract_links(text):
    """Markdown links first, then bare URLs; deduplicated per answer by URL."""
    found = []
    masked = list(text or "")
    for match in MARKDOWN_LINK.finditer(text or ""):
        parsed = _normalize(match.group(2))
        if parsed and all(parsed[0] != item[0] for item in found):
            found.append((parsed[0], _clean_title(match.group(1))))
        for index in range(match.start(2), match.end(2)):
            if index < len(masked):
                masked[index] = " "
    for match in BARE_URL.finditer("".join(masked)):
        parsed = _normalize(match.group(0))
        if parsed and all(parsed[0] != item[0] for item in found):
            found.append((parsed[0], ""))
        if len(found) >= MAX_LINKS_PER_ANSWER:
            break
    return found[:MAX_LINKS_PER_ANSWER]


def _answer_records(db, project_id, start=None, end=None):
    geo_runs.initialize(db)
    where = "WHERE a.tenant_id=? AND a.project_id=? AND a.status='ok'"
    values = [commander.tenant(), int(project_id)]
    if start is not None:
        where += " AND a.created_at>=?"
        values.append(start.isoformat(timespec="seconds"))
    if end is not None:
        where += " AND a.created_at<=?"
        values.append(end.isoformat(timespec="seconds"))
    return db.execute(
        "SELECT a.id,a.run_id,a.engine_key,a.engine_name,a.text,a.created_at,"
        "q.id AS question_id,q.text AS question_text "
        "FROM geo_answers a JOIN geo_questions q ON q.id=a.question_id "
        f"{where} ORDER BY a.id", values,
    ).fetchall()


def _field(record, key, default=""):
    try:
        return record[key]
    except (KeyError, IndexError, TypeError):
        return default


def citations(records, own, competitors):
    """Flatten ``records`` (dicts or sqlite rows) into citation rows."""
    rows = []
    for record in records:
        parsed = []
        for url, title in extract_links(_field(record, "text")):
            target = _normalize(url)
            if not target:
                continue
            _url, domain, key = target
            source_type = _source_type(domain, own, competitors)
            parsed.append({
                "answer_id": record["id"], "run_id": _field(record, "run_id"),
                "question_id": _field(record, "question_id"),
                "engine_key": _field(record, "engine_key"),
                "engine_name": _field(record, "engine_name"),
                "created_at": _field(record, "created_at"),
                "url": url, "title": title, "domain": domain,
                "key": key, "source_type": source_type,
                "source_name": TYPE_NAMES.get(source_type, source_type),
                "own": any(_matches(domain, item) for item in own),
                "competitor": any(_matches(domain, item) for item in competitors),
            })
        rows.extend(parsed)
    return rows


def summarize(rows):
    """Aggregate citations into types/domains; answer sets keep unique answers."""
    types = {}
    domains = {}
    for row in rows:
        entry = types.setdefault(row["source_type"], {
            "source_type": row["source_type"], "name": row["source_name"],
            "answers": set(), "count": 0,
        })
        entry["answers"].add(row["answer_id"])
        entry["count"] += 1
        key = (row["domain"], row["source_type"])
        item = domains.setdefault(key, {
            "domain": row["domain"], "source_type": row["source_type"],
            "source_name": row["source_name"], "answers": set(),
            "count": 0, "samples": [], "sample_urls": set(),
        })
        item["answers"].add(row["answer_id"])
        item["count"] += 1
        if row["url"] not in item["sample_urls"] and len(item["samples"]) < MAX_SAMPLES:
            item["sample_urls"].add(row["url"])
            item["samples"].append({"url": row["url"], "title": row["title"]})
    return {
        "total_citations": len(rows),
        "types": sorted(types.values(), key=lambda item: (-len(item["answers"]), -item["count"])),
        "domains": sorted(domains.values(),
                          key=lambda item: (-len(item["answers"]), -item["count"], item["domain"])),
    }


def _window(query):
    try:
        days = max(1, min(90, int((query.get("days") or [str(DEFAULT_DAYS)])[0])))
    except ValueError:
        days = DEFAULT_DAYS
    end = datetime.now(timezone.utc)
    return days, end - timedelta(days=days), end


def _page(query):
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = max(1, min(100, int((query.get("page_size") or ["20"])[0])))
    except ValueError as error:
        raise commander.ChatError("分页参数无效。") from error
    return page, size


def _sources_page(db, project, query):
    days, start, end = _window(query)
    records = _answer_records(db, project["id"], start, end)
    own, competitors = _site_hosts(db, project)
    summary = summarize(citations(records, own, competitors))
    answer_total = len(records)
    filter_type = (query.get("source_type") or [""])[0].strip()
    items = []
    for item in summary["domains"]:
        if filter_type and item["source_type"] != filter_type:
            continue
        entries = len(item["answers"])
        items.append({
            "domain": item["domain"], "source_type": item["source_type"],
            "source_name": item["source_name"], "answers": entries,
            "count": item["count"],
            "share": round(entries / answer_total, 4) if answer_total else 0,
            "samples": item["samples"],
        })
    total = len(items)
    page, size = _page(query)
    items = items[(page - 1) * size:page * size]
    types = [{
        "source_type": item["source_type"], "name": item["name"],
        "answers": len(item["answers"]), "count": item["count"],
    } for item in summary["types"]]
    return {"ok": True, "answers": answer_total, "types": types,
            "items": items, "total": total}


def report_data(db, project, start, end):
    """``report.sources`` payload for one period."""
    records = _answer_records(db, project["id"], start, end)
    own, competitors = _site_hosts(db, project)
    summary = summarize(citations(records, own, competitors))
    domains = [{
        "domain": item["domain"], "source_name": item["source_name"],
        "answers": len(item["answers"]),
    } for item in summary["domains"][:MAX_DOMAINS]]
    return {"domains": domains}


def _ensure_publications(db):
    # Lazy import avoids a cycle: geo_content imports this module at top level.
    import geo_content
    geo_content.initialize(db)


def _publications(db, project_id, published_only=True):
    _ensure_publications(db)
    where = "WHERE project_id=? AND tenant_id=?"
    if published_only:
        where += " AND status='published'"
    return db.execute(f"SELECT * FROM geo_publications {where} ORDER BY id DESC",
                      (int(project_id), commander.tenant())).fetchall()


def _publication_key(url):
    _url, _domain, key = _normalize(url)
    return key


def publication_citations(db, project_id):
    """``{publication_id: {count, last_at, answer_ids}}`` over all answers."""
    project = _project(db, project_id)
    records = _answer_records(db, project["id"])
    own, competitors = _site_hosts(db, project)
    rows = citations(records, own, competitors)
    by_key = defaultdict(list)
    for row in rows:
        by_key[row["key"]].append(row)
    result = {}
    for publication in _publications(db, project_id, published_only=False):
        key = _publication_key(publication["url"])
        if not key:
            continue
        matched = by_key.get(key, [])
        if matched:
            result[publication["id"]] = {
                "count": len({row["answer_id"] for row in matched}),
                "last_at": max(row["created_at"] for row in matched),
            }
    return result


def refresh_publication_counts(db, project_id):
    """Keep ``geo_publications.cited_count`` in sync (called on publish reads)."""
    try:
        counts = publication_citations(db, project_id)
    except Exception:
        return
    for publication_id, item in counts.items():
        db.execute("UPDATE geo_publications SET cited_count=? WHERE id=? AND tenant_id=?",
                   (item["count"], publication_id, commander.tenant()))
    known = set(counts)
    for row in _publications(db, project_id, published_only=False):
        if row["id"] not in known and row["cited_count"]:
            db.execute("UPDATE geo_publications SET cited_count=0 WHERE id=? AND tenant_id=?",
                       (row["id"], commander.tenant()))


def cited_report(db, project_id, project, start, end):
    """``report.cited`` payload; ``None`` when the project never published."""
    publications = _publications(db, project_id)
    if not publications:
        return None
    records = _answer_records(db, project_id, start, end)
    own, competitors = _site_hosts(db, project)
    rows = citations(records, own, competitors)
    by_key = defaultdict(list)
    for row in rows:
        by_key[row["key"]].append(row)
    start_iso = start.isoformat(timespec="seconds")
    end_iso = end.isoformat(timespec="seconds")
    items = []
    cited_answers, cited_questions, cited_pubs = set(), set(), 0
    for publication in publications:
        key = _publication_key(publication["url"])
        matched = by_key.get(key, []) if key else []
        answers = len({row["answer_id"] for row in matched})
        if answers:
            cited_pubs += 1
            cited_answers.update(row["answer_id"] for row in matched)
            cited_questions.update(row["question_id"] for row in matched)
        new = start_iso <= publication["created_at"] < end_iso
        if not answers and not new:
            continue
        items.append({
            "id": publication["id"], "title": publication["title"],
            "platform_name": publication["platform_name"], "answers": answers,
            "new": new,
        })
    items.sort(key=lambda item: (-item["answers"], not item["new"], item["id"]))
    return {
        "pubs": cited_pubs, "answers": len(cited_answers),
        "questions": len(cited_questions),
        "new_pubs": sum(1 for item in items if item["new"] and item["answers"]),
        "items": items[:MAX_DOMAINS],
    }


def overview_cited(db, project_id):
    """``(cited_pubs, cited items)`` for the project workbench."""
    publications = {row["id"]: row for row in _publications(db, project_id)}
    counts = publication_citations(db, project_id)
    items = []
    for publication_id, item in counts.items():
        publication = publications.get(publication_id)
        if publication is None:
            continue
        items.append({
            "id": publication_id, "title": publication["title"],
            "content_title": publication["title"],
            "platform_name": publication["platform_name"],
            "cited_count": item["count"],
            "last_cited_at": geo_matrix.display_time(item["last_at"]),
        })
    items.sort(key=lambda item: (-item["cited_count"], item["id"]))
    return len(items), items[:5]


def engine_citations(db, project_id, records):
    """Per-engine citation counts and top domains for the diagnosis body."""
    project = _project(db, project_id)
    own, competitors = _site_hosts(db, project)
    grouped = defaultdict(list)
    for row in citations(records, own, competitors):
        grouped[row["engine_key"]].append(row)
    result = {}
    for key, rows in grouped.items():
        domains = {}
        for row in rows:
            entry = domains.setdefault(row["domain"], {"domain": row["domain"], "count": 0,
                                                        "answers": set()})
            entry["count"] += 1
            entry["answers"].add(row["answer_id"])
        top = sorted(domains.values(), key=lambda item: (-item["count"], item["domain"]))[:5]
        result[key] = {
            "citations": len(rows),
            "domains": [{"domain": item["domain"], "count": item["count"],
                         "answers": len(item["answers"])} for item in top],
        }
    return result


def diagnosis_sources(db, project_id, records):
    """``DiagnosisBody.sources`` for the baseline run's answer records."""
    project = _project(db, project_id)
    own, competitors = _site_hosts(db, project)
    rows = citations(records, own, competitors)
    summary = summarize(rows)
    total = summary["total_citations"]
    types = [{
        "source_type": item["source_type"], "name": item["name"],
        "count": item["count"], "share": round(item["count"] / total, 4) if total else 0,
    } for item in summary["types"]]
    domains = [{
        "domain": item["domain"], "name": item["source_name"],
        "source_type": item["source_type"], "source_name": item["source_name"],
        "count": item["count"], "answers": len(item["answers"]),
        "share": round(item["count"] / total, 4) if total else 0,
    } for item in summary["domains"][:MAX_DOMAINS]]
    return {
        "total": total, "types": types, "domains": domains,
        "unique_domains": len(summary["domains"]),
        "own": sum(1 for row in rows if row["own"]),
        "competitor": sum(1 for row in rows if row["competitor"]),
        "ranking": {"titled": 0, "count": 0, "share": 0, "answers": 0,
                    "answers_self": 0, "examples": []},
    }


# ---------------------------------------------------------------------------
# Entry points


def read(path, query):
    path = canonical(path)
    with commander.database() as db:
        match = PROJECT.fullmatch(path)
        if not match or (match[2] or "").strip("/") != SOURCES_SUFFIX:
            raise commander.ChatError("GEO 接口暂不可用。")
        project = _project(db, match[1])
        return _sources_page(db, project, query)


def write(method, path, body):
    path = canonical(path)
    match = PROJECT.fullmatch(path)
    if not match or (match[2] or "").strip("/") != ANALYZE_SUFFIX:
        raise commander.ChatError("GEO 接口暂不可用。")
    raise commander.ChatError("被引用页面拆解需要抓取第三方网页并调用模型，暂未开放。")
