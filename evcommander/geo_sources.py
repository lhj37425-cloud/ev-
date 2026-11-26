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
from html.parser import HTMLParser
import hashlib
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import commander
import geo_brand
import geo_matrix
import geo_runs
import tenancy


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
MAX_ANALYZE_PAGES = 5
ANALYZE_BYTES = 600_000
ANALYZE_TIMEOUT = 8
ANALYZE_CACHE_MINUTES = 360
QUESTION_WORDS = ("什么", "怎么", "如何", "哪家", "哪个", "为什么", "多少", "是否",
                  "吗", "?", "？")
DATE_RE = re.compile(r"20\d{2}[-/年]\d{1,2}(?:[-/月]\d{1,2})?")
NUMERIC_RE = re.compile(r"\d+\s*(?:%|％|年|万|元|亿|家|个|天|小时|分钟|人|次)")
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _AnalyzeParser(HTMLParser):
    def __init__(self, base_domain):
        super().__init__(convert_charrefs=True)
        self.base_domain = base_domain
        self.title = ""
        self.headings = []
        self.lists = 0
        self.tables = 0
        self.chars = 0
        self.links = 0
        self.external = 0
        self.json_ld = False
        self.ld_text = ""
        self.meta = {}
        self.text = []
        self._in_title = False
        self._heading = None
        self._heading_text = ""
        self._skip = 0
        self._in_ld = False

    def handle_starttag(self, tag, attrs):
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag == "title":
            self._in_title = True
        elif tag in {"script", "style", "noscript", "template", "svg", "head"}:
            self._skip += 1
            if tag == "script" and "ld+json" in values.get("type", "").lower():
                self.json_ld = True
                self._in_ld = True
        if tag in {"h1", "h2", "h3"}:
            self._heading = tag
            self._heading_text = ""
        elif tag == "meta":
            name = (values.get("name") or values.get("property") or "").lower()
            if name and values.get("content"):
                self.meta[name] = values["content"][:300]
        elif tag in {"ul", "ol"}:
            self.lists += 1
        elif tag == "table":
            self.tables += 1
        elif tag == "a" and values.get("href", "").startswith(("http://", "https://")):
            self.links += 1
            host = (urlsplit(values["href"]).hostname or "").lower()
            if host and not _matches(host, self.base_domain):
                self.external += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in {"script", "style", "noscript", "template", "svg", "head"}:
            self._skip = max(0, self._skip - 1)
            if tag == "script":
                self._in_ld = False
        elif tag == self._heading:
            text = re.sub(r"\s+", " ", self._heading_text).strip()
            if text:
                self.headings.append(text[:120])
            self._heading = None
            self._heading_text = ""

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_ld:
            self.ld_text += data[:2000]
        if self._heading:
            self._heading_text += data
        if not self._skip:
            self.chars += len("".join(data.split()))
            if sum(len(item) for item in self.text) < 6000:
                self.text.append(data)


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_cited_analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        cache_key TEXT NOT NULL, urls TEXT NOT NULL DEFAULT '[]',
        result TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
        UNIQUE(tenant_id, project_id, cache_key)
    )""")


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
    if os.environ.get("EV_CITED_ALLOW_PRIVATE", "").strip() in {"1", "true", "yes"}:
        return False
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
    try:
        port = parsed.port
    except ValueError:
        return None
    if port and port not in {80, 443}:
        host_with_port = f"{host}:{port}"
    else:
        host_with_port = host
    path = unquote(parsed.path) or "/"
    clean = path.rstrip("/") or "/"
    url = urlunsplit((parsed.scheme, host_with_port, path, parsed.query, ""))
    return url, host, (host_with_port, clean)


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


REVIEW_WORDS = ("盘点", "推荐", "评测", "测评", "排行", "榜单", "对比", "哪个好",
                "怎么选", "指南", "清单", "十大")
REVIEW_RE = re.compile(r"(top|best|review|comparison|guide|vs)", re.IGNORECASE)


def _is_review_title(title):
    if any(word in title for word in REVIEW_WORDS):
        return True
    return bool(REVIEW_RE.search(title))


def _ranking(rows, records):
    """How many titled citations are roundups/reviews, and whether they name us."""
    titled = [row for row in rows if str(row.get("title") or "").strip()]
    reviews = [row for row in titled if _is_review_title(str(row["title"]))]
    mentioned = {}
    for record in records:
        mentioned[_field(record, "id")] = bool(_field(record, "mentioned"))
    answer_ids = {row["answer_id"] for row in reviews}
    examples, seen = [], set()
    for row in reviews:
        title = str(row["title"]).strip()
        if title and title not in seen:
            seen.add(title)
            examples.append(title)
            if len(examples) >= 3:
                break
    return {
        "titled": len(titled), "count": len(reviews),
        "share": round(len(reviews) / len(titled), 4) if titled else 0,
        "answers": len(answer_ids),
        "answers_self": sum(1 for answer_id in answer_ids if mentioned.get(answer_id)),
        "examples": examples,
    }


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
        "ranking": _ranking(rows, records),
    }


def _allow_private_fetch():
    return os.environ.get("EV_CITED_ALLOW_PRIVATE", "").strip() in {"1", "true", "yes"}


def _public_host(host):
    if _allow_private_fetch():
        return
    host = (host or "").lower().strip(".")
    if host in {"localhost"} or host.endswith((".local", ".internal")):
        raise commander.ChatError("这个地址指向内网或本机，不能拆解。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError as error:
            raise commander.ChatError("网页域名解析失败。") from error
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if (address.is_private or address.is_loopback or address.is_link_local
                    or address.is_reserved):
                raise commander.ChatError("这个地址指向内网或本机，不能拆解。")
        return
    if (address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved):
        raise commander.ChatError("这个地址指向内网或本机，不能拆解。")


def _decode_page(raw, content_type):
    match = re.search(r"charset=([A-Za-z0-9_.-]+)", content_type or "")
    for charset in ([match[1]] if match else []) + ["utf-8", "gb18030"]:
        try:
            return raw.decode(charset, "replace")
        except LookupError:
            continue
    return raw.decode("utf-8", "replace")


def _public_fetch(url):
    current = url
    opener = urllib.request.build_opener(_NoRedirect())
    for _ in range(5):
        parsed = urlsplit(current)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise commander.ChatError("网页地址无效。")
        if parsed.username or parsed.password:
            raise commander.ChatError("网页地址无效。")
        _public_host(parsed.hostname)
        request = urllib.request.Request(current, headers={
            "User-Agent": "EVCommander-CitedPages/1.0",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "identity",
        })
        try:
            response = opener.open(request, timeout=ANALYZE_TIMEOUT)
            raw = response.read(ANALYZE_BYTES + 1)
            return (response.status, response.geturl(),
                    _decode_page(raw[:ANALYZE_BYTES], response.headers.get("Content-Type", "")))
        except urllib.error.HTTPError as error:
            if error.code in {301, 302, 303, 307, 308}:
                location = error.headers.get("Location", "")
                if not location:
                    raise commander.ChatError(f"网页返回 HTTP {error.code} 但没有跳转地址。")
                current = urljoin(current, location)
                continue
            raw = error.read(ANALYZE_BYTES) if error.fp else b""
            return (error.code, current,
                    _decode_page(raw, error.headers.get("Content-Type", "")))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise commander.ChatError("打开网页失败，请稍后重试。") from error
    raise commander.ChatError("网页跳转次数过多。")


def _genre(page, source_type):
    title = page.title or ""
    if source_type == "wiki":
        return "百科"
    if source_type == "media":
        return "媒体报道"
    if source_type == "review":
        return "榜单 / 点评"
    if source_type == "community":
        return "社区问答"
    if re.search(r"对比|哪个好|排行|榜单|测评", title):
        return "对比 / 榜单"
    if re.search(r"怎么|如何|指南|教程|步骤|入门", title):
        return "教程 / 指南"
    if re.search(r"案例|客户故事|实践", title):
        return "客户案例"
    if re.search(r"为什么|是什么|常见问题|问答", title):
        return "问答"
    return "文章"


def _page_features(page):
    text = "".join(page.text)[:6000]
    headings = page.headings
    questions = [item for item in headings if any(word in item for word in QUESTION_WORDS)]
    features = []
    if len(headings) >= 2:
        features.append("有小标题")
    if page.lists or page.tables:
        features.append("有列表或表格")
    if NUMERIC_RE.search(text):
        features.append("有具体数字")
    if DATE_RE.search(text) or any(key in page.meta for key in (
            "article:published_time", "article:modified_time", "date", "pubdate")):
        features.append("有更新时间")
    if page.meta.get("author") or re.search(r"作者[:：]", text):
        features.append("有作者")
    if page.json_ld:
        features.append("有结构化数据")
    if len(questions) >= 2 or "FAQPage" in page.ld_text:
        features.append("有问答结构")
    if page.chars >= 800:
        features.append("正文充分")
    if page.external >= 2:
        features.append("有引用来源")
    if any(word in page.title for word in QUESTION_WORDS) or len(page.title) <= 40:
        features.append("标题直接")
    return features


def _why(page, features):
    if not features:
        return "页面内容比较薄，AI 能摘取的要点不多。"
    summary = "、".join(features[:4])
    if page.chars < 300:
        return f"正文只有约 {page.chars} 字，内容偏薄；能给到 AI 的要点有限。"
    return f"正文约 {page.chars} 字，{summary}，AI 更容易摘取。"


def _analyze_page(url, own, competitors, answers):
    parsed = _normalize(url)
    if not parsed:
        return {"url": url, "title": url, "source_name": "", "genre": "",
                "domain": "", "answers": 0, "ok": False, "features": [],
                "why": "", "headings": [], "error": "网页地址无效。"}
    _url, domain, key = parsed
    source_type = _source_type(domain, own, competitors)
    source_name = TYPE_NAMES.get(source_type, source_type)
    try:
        status, final_url, text = _public_fetch(_url)
    except commander.ChatError as error:
        return {"url": _url, "title": _url, "source_name": source_name,
                "genre": "", "domain": domain, "answers": len(answers.get(key, set())),
                "ok": False, "features": [], "why": "", "headings": [],
                "error": str(error)}
    if status >= 400:
        return {"url": _url, "title": _url, "source_name": source_name,
                "genre": "", "domain": domain, "answers": len(answers.get(key, set())),
                "ok": False, "features": [], "why": "", "headings": [],
                "error": f"页面返回 HTTP {status}。"}
    page = _AnalyzeParser(domain)
    page.feed(text)
    page.close()
    title = re.sub(r"\s+", " ", page.title).strip()[:200] or _url
    features = _page_features(page)
    return {
        "url": _url, "title": title, "source_name": source_name,
        "genre": _genre(page, source_type) if features else "",
        "domain": domain, "answers": len(answers.get(key, set())),
        "ok": True, "features": features, "why": _why(page, features),
        "headings": page.headings[:8], "error": "",
    }


def _aggregate(pages):
    ok_pages = [page for page in pages if page["ok"]]
    if not ok_pages:
        return [], []
    counts = {}
    for page in ok_pages:
        for feature in page["features"]:
            counts[feature] = counts.get(feature, 0) + 1
    total = len(ok_pages)
    threshold = max(2, (total + 1) // 2)
    patterns = [f"{count}/{total} 篇有：{feature}"
                for feature, count in sorted(counts.items(), key=lambda item: -item[1])
                if count >= threshold][:6]
    advice = []
    rules = [
        ("有小标题", "用二级标题把内容分成 3–6 段，每段先给结论。"),
        ("有列表或表格", "把要点整理成列表或对比表，方便 AI 直接摘取。"),
        ("有具体数字", "补充可核对的数字、时间和事实，少写空泛的形容词。"),
        ("有更新时间", "在页面或文章里写清发布时间和更新时间。"),
        ("有问答结构", "直接用客户会问的一句话做标题，开头 2–3 句回答它。"),
        ("有结构化数据", "给页面加 FAQ / Article 结构化数据（可用「生成代码」）。"),
        ("有作者", "写清作者或编辑，AI 更愿意把它当成可靠资料。"),
    ]
    for feature, text in rules:
        if counts.get(feature, 0) < threshold:
            advice.append(text)
    advice.append("把重要内容放在公开、可抓取的 HTML 里，不要只放在脚本或图片里。")
    return patterns, advice[:6]


def _cache_key(urls, days):
    raw = "\n".join(sorted(urls)) + f"|{days}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _analyze_cited(project_id, body):
    try:
        days = max(1, min(90, int(body.get("days") or DEFAULT_DAYS)))
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    with commander.database() as db:
        initialize(db)
        project = _project(db, project_id)
        own, competitors = _site_hosts(db, project)
        requested = body.get("urls")
        urls = []
        if isinstance(requested, list):
            for value in requested[:MAX_ANALYZE_PAGES]:
                parsed = _normalize(value)
                if parsed and parsed[0] not in urls:
                    urls.append(parsed[0])
        _, start, end = _window({"days": [str(days)]})
        records = _answer_records(db, project["id"], start, end)
        rows = citations(records, own, competitors)
        if not urls:
            summary = summarize(rows)
            for item in summary["domains"]:
                for sample in item["samples"]:
                    if sample["url"] and sample["url"] not in urls:
                        urls.append(sample["url"])
                    if len(urls) >= MAX_ANALYZE_PAGES:
                        break
                if len(urls) >= MAX_ANALYZE_PAGES:
                    break
        if not urls:
            raise commander.ChatError("这段时间没有可拆解的引用页面。")
        key = _cache_key(urls, days)
        cached = db.execute(
            "SELECT * FROM geo_cited_analyses WHERE tenant_id=? AND project_id=? AND cache_key=?",
            (commander.tenant(), project["id"], key),
        ).fetchone()
        if cached is not None:
            created = _json_time(cached["created_at"])
            if (datetime.now(timezone.utc) - created).total_seconds() < ANALYZE_CACHE_MINUTES * 60:
                result = _json(cached["result"], {})
                return {"ok": True, "cached": True, "credits": 0,
                        **{k: result.get(k, []) for k in ("patterns", "advice", "pages")}}
        answers = defaultdict(set)
        for row in rows:
            answers[row["key"]].add(row["answer_id"])
    pages = [_analyze_page(url, own, competitors, answers) for url in urls]
    patterns, advice = _aggregate(pages)
    result = {"patterns": patterns, "advice": advice, "pages": pages}
    if any(page["ok"] for page in pages):
        with commander.database() as db:
            db.execute("BEGIN IMMEDIATE")
            initialize(db)
            db.execute(
                "INSERT INTO geo_cited_analyses (tenant_id,project_id,cache_key,urls,result,created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id,project_id,cache_key) "
                "DO UPDATE SET urls=excluded.urls,result=excluded.result,created_at=excluded.created_at",
                (commander.tenant(), int(project_id), key,
                 json.dumps(urls, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False), now()),
            )
            tenancy.audit_in(db, commander.tenant(), "geo.cited.analyze",
                             str(project_id), f"{len(urls)} pages")
    return {"ok": True, "cached": False, "credits": 0, **result}


def _json(raw, default):
    try:
        value = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _json_time(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


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
    if method != "POST":
        raise commander.ChatError("GEO 接口暂不可用。")
    return _analyze_cited(match[1], body)
