"""GEO official-website audit and AI-readiness generators.

The archived "官网工具" page first shows whether AI can actually read the
customer's own website. This module fetches one page (plus robots.txt,
sitemap.xml and llms.txt) **without executing JavaScript**, checks the common
blockers (robots rules for AI/search crawlers, meta robots, HTTPS, JS-only
content, missing basics) and stores the result for the history list.

Only URLs under the project's own website are allowed; private/loopback hosts
are rejected (``EV_SITE_AUDIT_ALLOW_PRIVATE=1`` exists only for local
acceptance). Redirects are followed manually and re-validated.

The "生成代码" tab is also implemented locally and free of charge: robots.txt
AI section, llms.txt, Schema JSON-LD and entity/directory drafts are built from
the project's confirmed brand facts and content. The other Site tabs (beacon
traffic, crawl logs, search-console imports, indexing submission, product feed)
need a script on the customer site or search-engine credentials and stay
explicitly closed.
"""

from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from fnmatch import fnmatchcase
import csv
import gzip
import hashlib
from html.parser import HTMLParser
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import urllib.error
import urllib.request
import zipfile
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import commander
import customers
import geo_brand
import geo_content
import tenancy


PROJECT = re.compile(r"/api/geo/projects/([0-9]{1,16})(?:/(.*))?$")
AUDIT = re.compile(r"/api/geo/site-audits/([0-9]{1,16})$")
GENERATOR = re.compile(r"/api/geo/projects/([0-9]{1,16})/generators/([a-z]{2,20})$")
CRAWL_LOG = re.compile(r"/api/geo/crawl-logs/([0-9]{1,16})$")
CRAWL_UPLOAD = re.compile(r"/api/geo/projects/([0-9]{1,16})/crawl-logs$")
IMPORT_ITEM = re.compile(r"/api/geo/imports/([0-9]{1,16})$")
IMPORT_UPLOAD = re.compile(r"/api/geo/projects/([0-9]{1,16})/imports$")
MAX_IMPORT_BYTES = 20 * 1024 * 1024
IMPORT_SOURCES = [
    {"key": "bing_ai", "name": "必应 AI 表现（Copilot 引用）"},
    {"key": "bing", "name": "必应站长工具（搜索表现）"},
    {"key": "gsc", "name": "Google Search Console"},
]
KIND_NAMES = {"daily": "按天", "pages": "按页面", "queries": "按搜索词"}
METRIC_NAMES = {"clicks": "点击", "impressions": "展示", "ctr": "点击率",
                "position": "平均排名", "citations": "引用"}
METRIC_ALIASES = {
    "clicks": {"clicks", "点击", "点击次数"},
    "impressions": {"impressions", "展示", "展示次数", "曝光", "曝光次数"},
    "ctr": {"ctr", "点击率", "点击率(%)", "点击率（%）"},
    "position": {"position", "average position", "avg. position", "avg position",
                 "排名", "平均排名", "平均位置"},
    "citations": {"citations", "ai citations", "引用", "引用次数", "被引用次数"},
}
KEY_ALIASES = {
    "date": {"date", "日期", "天"},
    "query": {"query", "queries", "top queries", "search query", "搜索词", "查询",
              "热门查询", "搜索查询", "热门搜索词"},
    "page": {"page", "pages", "top pages", "网页", "页面", "url", "地址", "热门网页"},
}
BEACON_SCRIPT = "/geo-beacon.js"
BEACON_HIT = "/geo-beacon/hit"
BEACON_LEAD = "/geo-beacon/lead"
BEACON_SOURCES = [
    ("chatgpt", "ChatGPT", ("chatgpt.com", "chat.openai.com")),
    ("perplexity", "Perplexity", ("perplexity.ai",)),
    ("gemini", "Gemini", ("gemini.google.com", "bard.google.com")),
    ("copilot", "Copilot", ("copilot.microsoft.com",)),
    ("doubao", "豆包", ("doubao.com",)),
    ("kimi", "Kimi", ("kimi.moonshot.cn", "kimi.com")),
    ("deepseek", "DeepSeek", ("deepseek.com",)),
    ("claude", "Claude", ("claude.ai",)),
    ("yuanbao", "腾讯元宝", ("yuanbao.tencent.com",)),
    ("qwen", "通义千问", ("qianwen.aliyun.com", "tongyi.aliyun.com", "chat.qwen.ai")),
    ("wenxin", "文心一言", ("yiyan.baidu.com", "wenxin.baidu.com")),
    ("metaso", "秘塔", ("metaso.cn",)),
    ("tiangong", "天工", ("tiangong.cn",)),
    ("you", "You.com", ("you.com",)),
    ("poe", "Poe", ("poe.com",)),
]
BEACON_KEYS = {key for key, _name, _domains in BEACON_SOURCES}
PHONE_RE = re.compile(r"^\+?[0-9][0-9\-\s]{5,19}$")
MAX_BEACON_BODY = 4096
MAX_BYTES = 900_000
FETCH_TIMEOUT = 10
REUSE_MINUTES = 10
DAILY_DEFAULT = 20
MAX_LOG_BYTES = 30 * 1024 * 1024
MAX_LOG_LINES = 1_000_000
STATUS_NAMES = {"running": "体检中", "done": "已完成", "failed": "没完成"}
CHECK_NAMES = {"pass": "通过", "warn": "建议改", "info": "提示", "fail": "要修"}
SCORE_WEIGHT = {"pass": 1.0, "info": 0.7, "warn": 0.4, "fail": 0.0}

BOTS = [
    ("Googlebot", "Google", "Google", "search", "搜索引擎"),
    ("Bingbot", "Bing", "Microsoft", "search", "搜索引擎"),
    ("Baiduspider", "百度", "百度", "search", "搜索引擎"),
    ("YisouSpider", "夸克", "阿里巴巴", "search", "搜索引擎"),
    ("Sogou web spider", "搜狗", "腾讯", "search", "搜索引擎"),
    ("Applebot", "Apple Search", "Apple", "search", "搜索引擎"),
    ("Amazonbot", "Amazon", "Amazon", "engine", "搜索索引"),
    ("OAI-SearchBot", "OAI Search", "OpenAI", "engine", "搜索索引"),
    ("PerplexityBot", "Perplexity", "Perplexity", "engine", "搜索索引"),
    ("ChatGPT-User", "ChatGPT", "OpenAI", "user", "用户访问"),
    ("Perplexity-User", "Perplexity", "Perplexity", "user", "用户访问"),
    ("Claude-User", "Claude", "Anthropic", "user", "用户访问"),
    ("GPTBot", "GPT", "OpenAI", "training", "模型训练"),
    ("ClaudeBot", "Claude", "Anthropic", "training", "模型训练"),
    ("Google-Extended", "Google AI", "Google", "training", "模型训练"),
    ("CCBot", "Common Crawl", "Common Crawl", "training", "模型训练"),
    ("Bytespider", "豆包 / 字节", "字节跳动", "training", "模型训练"),
    ("Meta-ExternalAgent", "Meta AI", "Meta", "training", "模型训练"),
    ("cohere-ai", "Cohere", "Cohere", "training", "模型训练"),
]
CORE_PURPOSES = {"search", "engine", "user"}
DIRECTORIES_CN = [
    {"name": "百度地图", "url": "https://map.baidu.com/", "kind_name": "地图",
     "fit": "有门店或本地服务"},
    {"name": "高德地图", "url": "https://www.amap.com/", "kind_name": "地图",
     "fit": "有门店或本地服务"},
    {"name": "大众点评", "url": "https://www.dianping.com/", "kind_name": "点评",
     "fit": "面向消费者的门店"},
    {"name": "天眼查", "url": "https://www.tianyancha.com/", "kind_name": "企业信息",
     "fit": "所有企业"},
    {"name": "企查查", "url": "https://www.qcc.com/", "kind_name": "企业信息",
     "fit": "所有企业"},
    {"name": "百度百科", "url": "https://baike.baidu.com/", "kind_name": "百科",
     "fit": "有公开资料可查的品牌"},
]
DIRECTORIES_GLOBAL = [
    {"name": "Google Business Profile", "url": "https://business.google.com/",
     "kind_name": "商家资料", "fit": "有门店或服务区域"},
    {"name": "Bing Places", "url": "https://www.bingplaces.com/",
     "kind_name": "商家资料", "fit": "有门店或服务区域"},
    {"name": "Crunchbase", "url": "https://www.crunchbase.com/", "kind_name": "企业信息",
     "fit": "B2B / 科技公司"},
    {"name": "G2", "url": "https://www.g2.com/", "kind_name": "软件点评",
     "fit": "SaaS / 软件"},
    {"name": "Yelp", "url": "https://www.yelp.com/", "kind_name": "点评",
     "fit": "本地服务"},
    {"name": "Trustpilot", "url": "https://www.trustpilot.com/", "kind_name": "点评",
     "fit": "有客户评价的商家"},
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta = {}
        self.canonical = ""
        self.lang = ""
        self.h1 = 0
        self.h2 = 0
        self.lists = 0
        self.tables = 0
        self.json_ld = False
        self.visible = 0
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag == "title":
            self._in_title = True
        elif tag in {"script", "style", "noscript", "template", "svg", "head"}:
            self._skip += 1
            if tag == "script" and "ld+json" in values.get("type", "").lower():
                self.json_ld = True
        if tag == "html" and values.get("lang"):
            self.lang = values["lang"][:20]
        elif tag == "meta":
            name = values.get("name", "").lower()
            if name and values.get("content"):
                self.meta[name] = values["content"][:500]
        elif tag == "link" and "canonical" in values.get("rel", "").lower():
            href = values.get("href", "")
            if href.startswith(("http://", "https://", "/")):
                self.canonical = href[:500]
        elif tag == "h1":
            self.h1 += 1
        elif tag == "h2":
            self.h2 += 1
        elif tag in {"ul", "ol"}:
            self.lists += 1
        elif tag == "table":
            self.tables += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in {"script", "style", "noscript", "template", "svg", "head"}:
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self._skip:
            self.visible += len("".join(data.split()))


def canonical(path):
    if path.startswith("/agent-api"):
        path = path[len("/agent-api"):]
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def can_read(path):
    path = canonical(path)
    if AUDIT.fullmatch(path) or IMPORT_ITEM.fullmatch(path):
        return True
    match = PROJECT.fullmatch(path)
    return bool(match and (match[2] or "").strip("/")
                in {"site-audits", "crawl-logs", "crawl-stats", "beacon", "beacon/stats",
                    "imports"})


def can_write(path):
    path = canonical(path)
    if GENERATOR.fullmatch(path) or CRAWL_LOG.fullmatch(path) \
            or IMPORT_ITEM.fullmatch(path):
        return True
    match = PROJECT.fullmatch(path)
    return bool(match and (match[2] or "").strip("/")
                in {"site-audits", "crawl-logs", "beacon", "imports"})


def upload_limit(path):
    path = canonical(path)
    if CRAWL_UPLOAD.fullmatch(path):
        return MAX_LOG_BYTES
    if IMPORT_UPLOAD.fullmatch(path):
        return MAX_IMPORT_BYTES
    return None


def uploadable(path):
    """Multipart uploads go through their own handler, not the JSON path."""
    return upload_limit(path) is not None


def handle_upload(path, content_type, body):
    path = canonical(path)
    match = CRAWL_UPLOAD.fullmatch(path)
    if match:
        return upload_crawl_log(int(match[1]), content_type, body)
    match = IMPORT_UPLOAD.fullmatch(path)
    if match:
        return upload_import(int(match[1]), content_type, body)
    raise commander.ChatError("请求格式无效。")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_site_audits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        url TEXT NOT NULL, final_url TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'running', score INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '{}', checks TEXT NOT NULL DEFAULT '[]',
        bots TEXT NOT NULL DEFAULT '[]', bots_synced INTEGER NOT NULL DEFAULT 1,
        page TEXT NOT NULL DEFAULT '{}', notice TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        finished_at TEXT NOT NULL DEFAULT ''
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS geo_site_audits_by_project "
        "ON geo_site_audits (project_id, created_at)")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_crawl_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
        project_id INTEGER NOT NULL, filename TEXT NOT NULL,
        file_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        lines INTEGER NOT NULL DEFAULT 0, parsed INTEGER NOT NULL DEFAULT 0,
        hits INTEGER NOT NULL DEFAULT 0, range_start TEXT NOT NULL DEFAULT '',
        range_end TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS geo_crawl_logs_by_project "
        "ON geo_crawl_logs (project_id, created_at)")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_crawl_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
        project_id INTEGER NOT NULL, log_id INTEGER NOT NULL,
        date TEXT NOT NULL, bot TEXT NOT NULL, purpose TEXT NOT NULL,
        hits INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
        paths TEXT NOT NULL DEFAULT '[]'
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_crawl_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
        project_id INTEGER NOT NULL, log_id INTEGER NOT NULL,
        date TEXT NOT NULL, path TEXT NOT NULL, status INTEGER NOT NULL,
        bot TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 0
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS geo_crawl_errors_by_project "
        "ON geo_crawl_errors (project_id, date)")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_beacons (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
        project_id INTEGER NOT NULL, token TEXT NOT NULL UNIQUE,
        leads_enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS geo_beacons_by_project "
        "ON geo_beacons (project_id)")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_beacon_hits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
        project_id INTEGER NOT NULL, date TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'direct', path TEXT NOT NULL DEFAULT '/',
        lead INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS geo_beacon_hits_by_project "
        "ON geo_beacon_hits (project_id, date)")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
        project_id INTEGER NOT NULL, filename TEXT NOT NULL,
        source TEXT NOT NULL, kind TEXT NOT NULL,
        period_start TEXT NOT NULL DEFAULT '', period_end TEXT NOT NULL DEFAULT '',
        rows INTEGER NOT NULL DEFAULT 0, main TEXT NOT NULL DEFAULT '',
        metrics TEXT NOT NULL DEFAULT '[]', metric_names TEXT NOT NULL DEFAULT '{}',
        totals TEXT NOT NULL DEFAULT '{}', items TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS geo_imports_by_project "
        "ON geo_imports (project_id, created_at)")


def _project(db, key):
    row = db.execute("SELECT * FROM geo_projects WHERE id=? AND tenant_id=?",
                     (int(key), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("GEO 项目不存在。")
    return row


def _json(raw, default):
    try:
        value = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _audit_json(row):
    return {
        "id": row["id"], "project_id": row["project_id"],
        "url": row["url"], "final_url": row["final_url"],
        "status": row["status"], "status_name": STATUS_NAMES.get(row["status"], row["status"]),
        "score": row["score"], "summary": _json(row["summary"], {}),
        "checks": _json(row["checks"], []), "bots": _json(row["bots"], []),
        "bots_synced": row["bots_synced"], "page": _json(row["page"], {}),
        "notice": row["notice"], "error": row["error"],
        "created_at": row["created_at"], "finished_at": row["finished_at"],
    }


def _audit_list(row):
    return {
        "id": row["id"], "project_id": row["project_id"],
        "url": row["url"], "final_url": row["final_url"],
        "status": row["status"], "status_name": STATUS_NAMES.get(row["status"], row["status"]),
        "score": row["score"], "summary": _json(row["summary"], {}),
        "created_at": row["created_at"], "finished_at": row["finished_at"],
    }


def _website(project):
    value = str(project["website"] or "").strip()
    if not value:
        raise commander.ChatError("项目还没填官网地址。先在品牌档案的「基本信息」里填上官网。")
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise commander.ChatError("项目官网地址无效，请先在品牌档案里改成完整的 http(s) 地址。")
    return value, parsed.hostname.lower()


def _host_match(host, base):
    host = (host or "").lower().strip(".")
    base = (base or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    if base.startswith("www."):
        base = base[4:]
    return bool(host and base) and (host == base or host.endswith("." + base))


def _allow_private():
    return os.environ.get("EV_SITE_AUDIT_ALLOW_PRIVATE", "").strip() in {"1", "true", "yes"}


def _private(host):
    if _allow_private():
        return False
    host = (host or "").lower().strip(".")
    if host in {"localhost"} or host.endswith((".local", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError as error:
            raise commander.ChatError("官网域名解析失败，请检查地址或网络。") from error
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if (address.is_private or address.is_loopback or address.is_link_local
                    or address.is_reserved):
                return True
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved


def _target(project, value):
    website, base_host = _website(project)
    value = str(value or "").strip() or website
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise commander.ChatError("体检地址无效。") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise commander.ChatError("体检地址无效，请填 http(s) 开头的完整地址。")
    if parsed.username or parsed.password:
        raise commander.ChatError("体检地址无效。")
    host = parsed.hostname.lower()
    if not _host_match(host, base_host):
        raise commander.ChatError("只能体检本项目官网下的地址。")
    if _private(host):
        raise commander.ChatError("这个地址指向内网或本机，不能体检。")
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, "")), base_host


class _Fetched:
    def __init__(self, status, final_url, text, truncated=False):
        self.status = status
        self.final_url = final_url
        self.text = text
        self.truncated = truncated


def _decode(raw, content_type):
    match = re.search(r"charset=([A-Za-z0-9_.-]+)", content_type or "")
    charsets = [match[1]] if match else []
    charsets += ["utf-8", "gb18030"]
    for charset in charsets:
        try:
            return raw.decode(charset, "replace")
        except LookupError:
            continue
    return raw.decode("utf-8", "replace")


def _fetch(url, base_host, timeout=FETCH_TIMEOUT):
    current = url
    opener = urllib.request.build_opener(_NoRedirect())
    for _ in range(5):
        parsed = urlsplit(current)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise commander.ChatError("官网地址无效。")
        if not _host_match(parsed.hostname, base_host):
            raise commander.ChatError("官网跳转到了其它域名，体检中止。")
        if _private(parsed.hostname):
            raise commander.ChatError("官网跳转到了内网地址，体检中止。")
        request = urllib.request.Request(current, headers={
            "User-Agent": "EVCommander-SiteAudit/1.0",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "identity",
        })
        try:
            response = opener.open(request, timeout=timeout)
            raw = response.read(MAX_BYTES + 1)
            content_type = response.headers.get("Content-Type", "")
            return _Fetched(response.status, response.geturl(),
                            _decode(raw[:MAX_BYTES], content_type), len(raw) > MAX_BYTES)
        except urllib.error.HTTPError as error:
            if error.code in {301, 302, 303, 307, 308}:
                location = error.headers.get("Location", "")
                if not location:
                    raise commander.ChatError(f"官网返回 HTTP {error.code} 但没有跳转地址。")
                current = urljoin(current, location)
                continue
            raw = error.read(MAX_BYTES) if error.fp else b""
            return _Fetched(error.code, current, _decode(raw, error.headers.get("Content-Type", "")))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise commander.ChatError("抓取官网失败，请检查网址或稍后重试。") from error
    raise commander.ChatError("官网跳转次数过多，体检中止。")


def _origin(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _robots_groups(text):
    groups = []
    agents, rules = [], []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value)
        elif key in {"allow", "disallow"} and agents:
            rules.append((key, value))
    if agents:
        groups.append((agents, rules))
    return groups


def _rule_match(pattern, path):
    pattern = pattern.strip()
    if not pattern:
        return False
    anchored = pattern.endswith("$")
    core = pattern[:-1] if anchored else pattern
    regex = re.escape(core).replace(r"\*", ".*")
    return bool(re.match("^" + regex + ("$" if anchored else ""), path))


def _robots_allowed(groups, agent, path="/"):
    exact = [group for group in groups
             if any(item.strip().lower() == agent.lower() for item in group[0])]
    chosen = exact or [group for group in groups
                       if any(item.strip() == "*" for item in group[0])]
    rules = [rule for group in chosen for rule in group[1]]
    best, allowed = -1, True
    for kind, pattern in rules:
        if not pattern or not _rule_match(pattern, path):
            continue
        size = len(pattern)
        if size > best or (size == best and kind == "allow"):
            best = size
            allowed = kind == "allow"
    return allowed


def _bots(groups):
    result = []
    for agent, product, operator, purpose, purpose_name in BOTS:
        allowed = _robots_allowed(groups, agent)
        result.append({
            "agent": agent, "product": product, "operator": operator,
            "purpose": purpose, "purpose_name": purpose_name,
            "allowed": allowed,
            "note": "robots.txt 放行" if allowed else "被 robots.txt 的 Disallow 挡住",
        })
    return result


def _check(key, name, status, detail, impact="", fix=""):
    return {"key": key, "name": name, "status": status,
            "status_name": CHECK_NAMES[status], "detail": detail,
            "impact": impact, "fix": fix}


def _sitemap_count(text):
    if not text:
        return 0, False
    index = "<sitemapindex" in text.lower()
    count = len(re.findall(r"<loc[^>]*>", text, re.I))
    if not index and "<urlset" not in text.lower():
        return 0, False
    return count, True


def _audit(project, target, page_fetch, robots_fetch, llms_fetch, sitemap_fetch):
    page = _PageParser()
    page.feed(page_fetch.text)
    page.close()
    title = re.sub(r"\s+", " ", page.title).strip()[:200]
    robots_text = robots_fetch.text if robots_fetch and robots_fetch.status == 200 else ""
    groups = _robots_groups(robots_text)
    bots = _bots(groups)
    blocked_core = [bot for bot in bots if bot["purpose"] in CORE_PURPOSES and not bot["allowed"]]
    blocked_training = [bot for bot in bots if bot["purpose"] == "training" and not bot["allowed"]]
    if blocked_core:
        names = "、".join(f"{bot['product']}（{bot['agent']}）" for bot in blocked_core[:4])
        robots_check = _check(
            "robots_ai", "AI 爬虫访问", "fail",
            f"{len(blocked_core)} 个搜索 / AI 爬虫被 robots.txt 挡住：{names}",
            "被挡住的爬虫读不到官网，AI 在回答里就很难引用你。",
            "在 robots.txt 里给这些 User-agent 加 Allow: /，或删掉对应的 Disallow。")
    elif blocked_training:
        robots_check = _check(
            "robots_ai", "AI 爬虫访问", "pass",
            f"搜索与 AI 搜索爬虫都放行；训练模型类有 {len(blocked_training)} 个被挡住（不影响被搜索引用）",
            fix="")
    else:
        robots_check = _check(
            "robots_ai", "AI 爬虫访问", "pass",
            "常见搜索与 AI 爬虫都可以读取官网。" if robots_text else "没有 robots.txt，默认全部放行。")

    llms_check = None
    if llms_fetch and llms_fetch.status == 200 and len(llms_fetch.text.strip()) >= 20:
        llms_check = _check("llms_txt", "llms.txt", "pass",
                            f"发现 llms.txt（{len(llms_fetch.text.strip())} 字）。")
    elif llms_fetch and llms_fetch.status == 200:
        llms_check = _check("llms_txt", "llms.txt", "warn", "llms.txt 几乎是空的。",
                            fix="在「生成代码」里生成一份 llms.txt，放到官网根目录。")
    else:
        llms_check = _check("llms_txt", "llms.txt", "info", "还没有 llms.txt。",
                            "AI 需要先猜你的品牌和页面，容易读偏。",
                            "在「生成代码」里生成一份 llms.txt，放到官网根目录。")

    if sitemap_fetch and sitemap_fetch.status == 200:
        count, valid = _sitemap_count(sitemap_fetch.text)
        if valid:
            sitemap_check = _check("sitemap", "网站地图", "pass",
                                   f"发现网站地图，包含 {count} 个地址。")
        else:
            sitemap_check = _check("sitemap", "网站地图", "warn",
                                   "网站地图存在，但内容看起来不是有效的 XML。",
                                   fix="检查 sitemap.xml 的格式，或让网站管理员重新生成。")
    else:
        sitemap_check = _check("sitemap", "网站地图", "warn", "没有找到网站地图（sitemap.xml）。",
                               "AI 需要靠链接自己找页面，可能漏掉重要内容。",
                               "生成 sitemap.xml 并在 robots.txt 里用 Sitemap: 这行指向它。")

    noindex = "noindex" in page.meta.get("robots", "").lower()
    meta_check = _check("meta_robots", "页面是否允许收录", "fail" if noindex else "pass",
                        "页面 meta robots 写了 noindex，搜索引擎和 AI 不会收录。" if noindex
                        else "页面没有禁止收录的 meta robots。",
                        "整页不会被 AI 搜索收录。" if noindex else "",
                        '删掉页面里的 <meta name="robots" content="noindex">。' if noindex else "")
    scheme = urlsplit(page_fetch.final_url or target).scheme
    https_check = _check("https", "HTTPS", "pass" if scheme == "https" else "fail",
                         "官网使用 HTTPS。" if scheme == "https" else "官网还在用 HTTP。",
                         "" if scheme == "https" else "浏览器和 AI 会把 HTTP 页面标为不安全，部分抓取会直接失败。",
                         "" if scheme == "https" else "配置 HTTPS 证书，并把 HTTP 跳转到 HTTPS。")
    title_check = _check("title", "页面标题", "pass" if 2 <= len(title) <= 70 else "warn",
                         f"标题：{title}" if title else "页面没有 title。",
                         "" if title else "AI 不知道这页讲什么。",
                         "" if title else "给页面加一个能说清品牌和业务的 title。")
    description = page.meta.get("description", "").strip()
    desc_check = _check("description", "页面描述",
                        "pass" if len(description) >= 20 else ("warn" if description else "info"),
                        f"描述：{description[:120]}" if description else "页面没有 meta description。",
                        "" if description else "摘要信息由 AI 自己猜，容易读偏。",
                        "" if description else "加一段 50–160 字的 meta description，写清你是谁、做什么。")
    h1_status = "pass" if page.h1 == 1 else ("warn" if page.h1 == 0 else "info")
    h1_check = _check("h1", "一级标题", h1_status,
                      f"页面有 {page.h1} 个 h1。" if page.h1 else "页面没有 h1。",
                      "" if page.h1 else "AI 抓不到页面主标题。",
                      "" if page.h1 else "给正文加一个唯一的 h1，写清这页的主题。")
    if page.visible >= 300:
        content_status, content_detail = "pass", f"不执行脚本时能看到约 {page.visible} 字正文。"
    elif page.visible >= 80:
        content_status, content_detail = "warn", f"不执行脚本时只能看到约 {page.visible} 字正文。"
    else:
        content_status, content_detail = "fail", f"不执行脚本时几乎看不到正文（约 {page.visible} 字）。"
    content_check = _check("content", "正文可读性", content_status, content_detail,
                           "" if content_status == "pass" else
                           "正文如果只放在 JS 里，AI 抓取时看到的是空页面。",
                           "" if content_status == "pass" else
                           "把主要内容放到服务端渲染的 HTML 里，或做预渲染。")
    structure_ok = page.h2 >= 2 or page.lists or page.tables
    structure_check = _check("structure", "内容结构", "pass" if structure_ok else "warn",
                             "有分段标题或列表 / 表格，结构清楚。" if structure_ok
                             else "页面缺少小标题、列表或表格。",
                             "" if structure_ok else "AI 难以按段理解内容。",
                             "" if structure_ok else "用 h2 分段，并用列表或表格整理要点。")
    ld_check = _check("structured_data", "结构化数据", "pass" if page.json_ld else "info",
                      "发现 JSON-LD 结构化数据。" if page.json_ld else "没有 JSON-LD 结构化数据。",
                      "" if page.json_ld else "AI 需要更多推断才能确认品牌、产品和联系方式。",
                      "" if page.json_ld else "用「生成代码」生成 Schema，放进页面 <head>。")
    canonical_check = _check("canonical", "规范地址", "pass" if page.canonical else "info",
                             f"canonical：{page.canonical}" if page.canonical else "没有 canonical 规范地址。",
                             "" if page.canonical else "同一内容多个地址时，AI 可能分散引用。",
                             "" if page.canonical else "给页面加 <link rel=\"canonical\">。")
    viewport_check = _check("viewport", "移动端适配",
                            "pass" if page.meta.get("viewport") else "warn",
                            "有 viewport 设置。" if page.meta.get("viewport") else "没有 viewport 设置。",
                            "" if page.meta.get("viewport") else "移动端打开会缩放，影响体验和抓取判断。",
                            "" if page.meta.get("viewport") else
                            '在 <head> 加 <meta name="viewport" content="width=device-width,initial-scale=1">。')
    lang_check = _check("lang", "页面语言", "pass" if page.lang else "info",
                        f"页面语言：{page.lang}" if page.lang else "html 标签没有 lang。",
                        "" if page.lang else "AI 需要猜页面语言。",
                        "" if page.lang else '在 <html> 上写 lang，例如 lang="zh-CN"。')
    checks = [https_check, robots_check, meta_check, llms_check, sitemap_check,
              title_check, desc_check, h1_check, content_check, structure_check,
              ld_check, canonical_check, viewport_check, lang_check]
    summary = {key: sum(1 for item in checks if item["status"] == key)
               for key in ("pass", "warn", "info", "fail")}
    score = round(sum(SCORE_WEIGHT[item["status"]] for item in checks) / len(checks) * 100)
    notice = ("AI 直接读网页（不执行脚本）；动态渲染、登录后或需要点击才出现的内容可能看不到。"
              "分数只代表本次抓取。")
    if page_fetch.truncated:
        notice += " 页面过大，只分析了前面一部分。"
    return {
        "final_url": page_fetch.final_url, "score": score, "summary": summary,
        "checks": checks, "bots": bots, "bots_synced": 1,
        "page": {"title": title, "chars": page.visible, "h1": page.h1},
        "notice": notice,
    }


def _daily_limit():
    try:
        value = int(os.environ.get("EV_SITE_AUDIT_DAILY_LIMIT", DAILY_DEFAULT))
    except (TypeError, ValueError):
        value = DAILY_DEFAULT
    return max(0, value)


def _recover_stale(db):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=REUSE_MINUTES)).isoformat(
        timespec="seconds")
    db.execute(
        "UPDATE geo_site_audits SET status='failed',error=?,finished_at=? "
        "WHERE status='running' AND created_at<?",
        ("体检中断，请重新体检。", now(), cutoff),
    )


def _prepare_audit(db, project, body):
    target, base_host = _target(project, body.get("url"))
    recent = db.execute(
        "SELECT * FROM geo_site_audits WHERE tenant_id=? AND project_id=? AND url=? "
        "AND status='done' AND finished_at>=? ORDER BY id DESC LIMIT 1",
        (commander.tenant(), project["id"], target,
         (datetime.now(timezone.utc) - timedelta(minutes=REUSE_MINUTES)).isoformat(timespec="seconds")),
    ).fetchone()
    if recent is not None:
        return target, base_host, recent, None
    limit = _daily_limit()
    if limit:
        day = datetime.now(timezone.utc).date().isoformat()
        used = db.execute(
            "SELECT COUNT(*) FROM geo_site_audits WHERE tenant_id=? AND project_id=? "
            "AND substr(created_at,1,10)=?", (commander.tenant(), project["id"], day),
        ).fetchone()[0]
        if used >= limit:
            raise commander.ChatError("今天官网体检的次数已用完，明天再继续。")
    stamp = now()
    audit_id = db.execute(
        "INSERT INTO geo_site_audits (tenant_id,project_id,url,status,created_at) "
        "VALUES (?,?,?,?,?)",
        (commander.tenant(), project["id"], target, "running", stamp),
    ).lastrowid
    return target, base_host, None, audit_id


def _collect_audit(target, base_host):
    origin = _origin(target)
    page = _fetch(target, base_host)
    if page.status >= 400:
        raise commander.ChatError(f"官网返回 HTTP {page.status}，体检没有完成。")
    robots = llms = sitemap = None
    try:
        robots = _fetch(origin + "/robots.txt", base_host)
    except commander.ChatError:
        robots = None
    try:
        llms = _fetch(origin + "/llms.txt", base_host)
    except commander.ChatError:
        llms = None
    sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots.text or "") \
        if robots and robots.status == 200 else []
    try:
        sitemap = _fetch(sitemaps[0] if sitemaps else origin + "/sitemap.xml", base_host)
    except commander.ChatError:
        sitemap = None
    return _audit(None, target, page, robots, llms, sitemap)


def _start_audit(project_id, body):
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        _recover_stale(db)
        project = _project(db, project_id)
        target, base_host, reused, audit_id = _prepare_audit(db, project, body)
        if reused is not None:
            return {"ok": True, "reused": True, "audit": _audit_json(reused)}
    error_message = ""
    try:
        data = _collect_audit(target, base_host)
    except commander.ChatError as error:
        error_message = str(error)[:300]
    except Exception:
        error_message = "体检过程出错，请稍后重试。"
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        if error_message:
            db.execute(
                "UPDATE geo_site_audits SET status='failed',error=?,finished_at=? "
                "WHERE id=? AND tenant_id=?",
                (error_message, now(), audit_id, commander.tenant()),
            )
            tenancy.audit_in(db, commander.tenant(), "geo.site.audit", str(audit_id), "failed")
        else:
            db.execute(
                "UPDATE geo_site_audits SET status='done',final_url=?,score=?,summary=?,checks=?,"
                "bots=?,bots_synced=?,page=?,notice=?,finished_at=? WHERE id=? AND tenant_id=?",
                (data["final_url"], data["score"],
                 json.dumps(data["summary"], ensure_ascii=False),
                 json.dumps(data["checks"], ensure_ascii=False),
                 json.dumps(data["bots"], ensure_ascii=False), data["bots_synced"],
                 json.dumps(data["page"], ensure_ascii=False), data["notice"], now(),
                 audit_id, commander.tenant()),
            )
            tenancy.audit_in(db, commander.tenant(), "geo.site.audit", str(audit_id),
                             f"{target} score={data['score']}")
        row = db.execute("SELECT * FROM geo_site_audits WHERE id=?", (audit_id,)).fetchone()
    return {"ok": True, "reused": False, "audit": _audit_json(row)}


def _facts(db, project_id):
    geo_brand.initialize(db)
    rows = db.execute(
        "SELECT label,value FROM geo_facts WHERE tenant_id=? AND project_id=? "
        "AND status='confirmed' ORDER BY id LIMIT 30",
        (commander.tenant(), int(project_id)),
    ).fetchall()
    return [(row["label"], row["value"]) for row in rows]


def _contact_facts(facts):
    found = {}
    for label, value in facts:
        for key in ("电话", "手机", "邮箱", "地址", "联系人"):
            if key in label and key not in found:
                found[key] = value
    return found


def _generator(db, project, kind, body):
    facts = _facts(db, project["id"])
    website = str(project["website"] or "").strip()
    origin = _origin(website) if website else ""
    name = project["brand_name"]
    description = project["description"] or ""
    missing = []
    if not website:
        missing.append("官网")
    if not project["industry"]:
        missing.append("行业")
    if project["market"] == "cn" and not project["region"]:
        missing.append("地区")
    if not description:
        missing.append("一句话介绍")
    contact = _contact_facts(facts)
    if not contact:
        missing.append("联系方式（电话/邮箱/地址）")
    if kind == "robots":
        policy = body.get("policy") if isinstance(body.get("policy"), dict) else {}
        training = str(policy.get("training") or "allow").strip()
        allow_training = training != "deny"
        core = [item for item in BOTS if item[3] in CORE_PURPOSES]
        train = [item for item in BOTS if item[3] == "training"]
        lines = ["# AI 与搜索爬虫：建议放行"]
        lines += [f"User-agent: {item[0]}" for item in core]
        lines += ["Allow: /", "", "# 训练模型类爬虫：" + ("放行" if allow_training else "不放行")]
        for item in train:
            lines += [f"User-agent: {item[0]}", "Allow: /" if allow_training else "Disallow: /"]
        if body.get("with_sitemap") and origin:
            lines += ["", f"Sitemap: {origin}/sitemap.xml"]
        return {"kind": "robots", "file": "robots-AI段.txt", "text": "\n".join(lines) + "\n",
                "missing": missing}
    if kind == "llms":
        lines = [f"# {name}", ""]
        if description:
            lines += [f"> {description}", ""]
        if facts:
            lines.append("## 品牌信息")
            lines += [f"- {label}: {value}" for label, value in facts[:12]]
            lines.append("")
        if website:
            lines += ["## 官网", f"- [官网]({website})", ""]
        return {"kind": "llms", "file": "llms.txt", "text": "\n".join(lines),
                "missing": missing}
    if kind == "schema":
        schema_type = str(body.get("type") or "organization").strip()
        content = None
        content_id = body.get("content_id")
        if content_id:
            row = db.execute(
                "SELECT * FROM geo_contents WHERE id=? AND project_id=? AND tenant_id=?",
                (int(content_id), project["id"], commander.tenant()),
            ).fetchone()
            if row is None:
                raise commander.ChatError("内容不存在。")
            content = row
        if schema_type == "organization":
            data = {"@context": "https://schema.org", "@type": "Organization",
                    "name": name, "description": description,
                    "url": website or None,
                    "address": {"@type": "PostalAddress", "addressLocality": project["region"]}
                    if project["region"] else None}
        elif schema_type == "localbusiness":
            contact = _contact_facts(facts)
            data = {"@context": "https://schema.org", "@type": "LocalBusiness",
                    "name": name, "description": description, "url": website or None,
                    "address": {"@type": "PostalAddress",
                                "addressLocality": project["region"],
                                "streetAddress": contact.get("地址", "")},
                    "telephone": contact.get("电话") or contact.get("手机") or None}
        elif schema_type == "product":
            data = {"@context": "https://schema.org", "@type": "Product",
                    "name": name, "description": description, "url": website or None,
                    "brand": {"@type": "Brand", "name": name}}
        elif schema_type == "faq":
            questions = []
            if content is not None:
                questions.append((content["question"] or content["title"],
                                  content["summary"] or content["body"][:300]))
            else:
                questions = [(f"{label}是什么？", value) for label, value in facts[:8]]
            if not questions:
                raise commander.ChatError("还没有可用的品牌事实或内容，先补品牌档案。")
            data = {"@context": "https://schema.org", "@type": "FAQPage",
                    "mainEntity": [{"@type": "Question", "name": question,
                                    "acceptedAnswer": {"@type": "Answer", "text": answer}}
                                   for question, answer in questions]}
        elif schema_type == "article":
            if content is None:
                raise commander.ChatError("请先选一篇内容。")
            data = {"@context": "https://schema.org", "@type": "Article",
                    "headline": content["title"], "description": content["summary"],
                    "datePublished": content["created_at"], "dateModified": content["updated_at"],
                    "publisher": {"@type": "Organization", "name": name}}
        else:
            raise commander.ChatError("不支持的结构化数据类型。")
        data = {key: value for key, value in data.items() if value not in (None, "", [])}
        text = ('<script type="application/ld+json">\n'
                + json.dumps(data, ensure_ascii=False, indent=2) + "\n</script>")
        return {"kind": "schema", "type": schema_type, "file": f"schema-{schema_type}.html",
                "text": text, "missing": missing}
    if kind == "entity":
        entity_type = str(body.get("type") or "directories").strip()
        lines = [f"名称（一字不差）：{name}",
                 f"官网：{website or '（未填）'}",
                 f"行业：{project['industry'] or '（未填）'}",
                 f"地区：{project['region'] or '（未填）'}",
                 f"一句话介绍：{description or '（未填）'}"]
        for key in ("电话", "手机", "邮箱", "地址", "联系人"):
            if key in contact:
                lines.append(f"{key}：{contact[key]}")
        if entity_type == "wikidata":
            text = ("# Wikidata 草稿（QuickStatements）；提交前请逐条核对\n"
                    "CREATE\n"
                    f'LAST|Lzh|"{name}"\n'
                    + (f'LAST|Dzh|"{description[:120]}"\n' if description else "")
                    + (f'LAST|P856|"{website}"\n' if website else ""))
        elif entity_type == "gbp":
            text = "Google 商家资料填写清单：\n" + "\n".join(lines) + \
                   "\n营业时间 / 服务区域：按实际情况填写，保持和官网一致。\n"
        else:
            text = "平台资料清单（每个平台都按这份填，名称、地址、电话、网址保持一致）：\n" + \
                   "\n".join(lines)
        result = {"kind": "entity", "type": entity_type,
                  "file": f"entity-{entity_type}.txt", "text": text,
                  "missing": missing,
                  "notice": "资料是草稿，提交前请逐项核对；平台是否会收录由平台决定。"}
        if entity_type == "directories":
            platforms = DIRECTORIES_GLOBAL if project["market"] == "global" else DIRECTORIES_CN
            result["items"] = [{**item, "status": "todo"} for item in platforms]
        elif entity_type == "gbp":
            result["rows"] = [
                {"prop": "name", "name": "名称", "value": name,
                 "note": "和官网、营业执照保持一致"},
                {"prop": "website", "name": "官网", "value": website, "note": ""},
                {"prop": "industry", "name": "行业", "value": project["industry"], "note": ""},
                {"prop": "region", "name": "地区", "value": project["region"], "note": ""},
                {"prop": "phone", "name": "电话",
                 "value": contact.get("电话") or contact.get("手机") or "",
                 "note": "填前台能接听的号码"},
                {"prop": "description", "name": "简介", "value": description,
                 "note": "750 字以内"},
            ]
        return result
    raise commander.ChatError("不支持的生成类型。")


# ---------------------------------------------------------------------------
# Crawl logs: what AI crawlers read on the customer's site

PURPOSE_NAMES = {"search": "搜索引擎", "user": "用户访问",
                 "engine": "搜索索引", "training": "模型训练"}
PURPOSE_ORDER = ("search", "user", "engine", "training")
BOT_AGENTS = sorted(BOTS, key=lambda item: len(item[0]), reverse=True)
MONTHS = {month: index for index, month in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
COMBINED = re.compile(
    r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) ([^ ]*)(?: [^"]*)?" (\d{3}) \S+')
STATUS_TEXT = {404: "找不到页面", 403: "禁止访问", 401: "要登录",
               429: "请求太频繁", 500: "服务器出错", 502: "服务器出错",
               503: "服务器出错", 504: "服务器超时"}


def _log_date(value):
    value = str(value or "").strip()
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        return "-".join(match.groups())
    match = re.match(r"(\d{2})/([A-Za-z]{3})/(\d{4}):", value)
    if match:
        month = MONTHS.get(match.group(2).title())
        if month:
            return "%s-%02d-%s" % (match.group(3), month, match.group(1))
    return ""


def _log_status(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if 0 <= number <= 599 else 0


def _log_path(value):
    path = str(value or "").strip().split("?", 1)[0].split("#", 1)[0]
    return path[:200] if path.startswith("/") else (("/" + path.lstrip("/"))[:200] if path else "/")


def _log_bot(agent):
    agent = str(agent or "").strip()
    lowered = agent.lower()
    if not agent or agent == "-":
        return None
    for item in BOT_AGENTS:
        if item[0].lower() in lowered:
            return item
    return None


def _log_line(line):
    """Return ``(date, path, status, agent)`` for one access-log line."""
    if line.lstrip().startswith("{"):
        try:
            data = json.loads(line)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        stamp = (data.get("time") or data.get("timestamp") or data.get("@timestamp")
                 or data.get("date") or "")
        agent = data.get("http_user_agent") or data.get("user_agent") or data.get("ua") or ""
        headers = data.get("request_headers")
        if not agent and isinstance(headers, dict):
            agent = headers.get("User-Agent") or headers.get("user-agent") or ""
        request = data.get("request") or ""
        path = data.get("path") or data.get("uri") or data.get("request_uri") or ""
        if request:
            parts = str(request).split()
            if len(parts) >= 2:
                path = parts[1]
        return _log_date(stamp), _log_path(path), _log_status(
            data.get("status") or data.get("status_code")), str(agent)
    match = COMBINED.match(line)
    if not match:
        return None
    tail = line[match.end():]
    quotes = re.findall(r'"([^"]*)"', tail)
    agent = quotes[-1] if quotes else ""
    return (_log_date(match.group(2)), _log_path(match.group(4)),
            _log_status(match.group(5)), agent)


def _decode_log(raw):
    if raw[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as handle:
                raw = handle.read(MAX_LOG_BYTES * 4 + 1)
        except OSError as error:
            raise commander.ChatError("压缩日志解不开，请重新导出。") from error
        if len(raw) > MAX_LOG_BYTES * 4:
            raise commander.ChatError("压缩日志解压后超过 120 MB，请分次上传。")
    return raw.decode("utf-8", "replace")


def _parse_log(raw):
    """Summarise one access log; the raw lines are never stored."""
    text = _decode_log(raw)
    lines = parsed = 0
    first_date = last_date = ""
    daily, errors = {}, {}
    for line in text.splitlines():
        if not line.strip():
            continue
        lines += 1
        if lines > MAX_LOG_LINES:
            break
        row = _log_line(line)
        if row is None or not row[0]:
            continue
        date, path, status, agent = row
        if not first_date or date < first_date:
            first_date = date
        if not last_date or date > last_date:
            last_date = date
        bot = _log_bot(agent)
        if bot is None:
            continue
        parsed += 1
        name, operator, product, purpose, purpose_name = bot
        bucket = daily.setdefault((date, name), {
            "date": date, "bot": name, "purpose": purpose,
            "hits": 0, "errors": 0, "paths": {},
        })
        bucket["hits"] += 1
        bucket["paths"][path] = bucket["paths"].get(path, 0) + 1
        if status >= 400:
            bucket["errors"] += 1
            key = (date, path, status, name)
            errors[key] = errors.get(key, 0) + 1
    return {"lines": lines, "parsed": parsed, "start": first_date,
            "end": last_date, "daily": list(daily.values()),
            "errors": [{"date": key[0], "path": key[1], "status": key[2],
                        "bot": key[3], "hits": hits}
                       for key, hits in errors.items()]}


def _upload_fields(content_type, body):
    """Parse one multipart upload; returns ``(filename, raw, form_fields)``."""
    try:
        header = content_type.encode("ascii")
    except UnicodeError as error:
        raise commander.ChatError("上传格式无效。") from error
    if b"\n" in header or b"\r" in header or len(header) > 512:
        raise commander.ChatError("上传格式无效。")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + header + b"\r\n\r\n" + body)
    if not message.is_multipart() or message.defects:
        raise commander.ChatError("上传格式无效。")
    files, fields = [], {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name == "file":
            files.append(part)
        elif name and not part.is_multipart():
            payload = part.get_payload(decode=True) or b""
            fields[name] = payload.decode("utf-8", "replace").strip()[:100]
    if len(files) != 1 or files[0].is_multipart():
        raise commander.ChatError("每次请求应上传一个文件。")
    raw = files[0].get_payload(decode=True)
    if not raw:
        raise commander.ChatError("上传的文件是空的。")
    filename = (files[0].get_filename() or "upload.bin")
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1][:120]
    return filename, raw, fields


def _upload_file(content_type, body, max_bytes=None):
    filename, raw, _fields = _upload_fields(content_type, body)
    limit = MAX_LOG_BYTES if max_bytes is None else max_bytes
    if len(raw) > limit:
        raise commander.ChatError("请上传 %d MB 以内的文件。" % (limit // (1024 * 1024)))
    return filename, raw


def _crawl_log_json(row):
    return {
        "id": row["id"], "filename": row["filename"],
        "range_start": row["range_start"], "range_end": row["range_end"],
        "lines": row["lines"], "parsed": row["parsed"], "hits": row["hits"],
        "created_at": row["created_at"],
    }


def upload_crawl_log(project_id, content_type, body):
    """Parse an uploaded access log; only per-day summaries are stored."""
    filename, raw = _upload_file(content_type, body)
    digest = hashlib.sha256(raw).hexdigest()
    with commander.database() as db:
        initialize(db)
        project = _project(db, project_id)
        duplicate = db.execute(
            "SELECT * FROM geo_crawl_logs WHERE tenant_id=? AND project_id=? AND file_hash=?",
            (commander.tenant(), project["id"], digest)).fetchone()
    if duplicate is not None:
        return {"ok": True, "duplicate": True,
                "message": "这份文件已经传过，没有重复统计。",
                "upload": _crawl_log_json(duplicate)}
    summary = _parse_log(raw)
    stamp = now()
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        project = _project(db, project_id)
        log_id = db.execute(
            "INSERT INTO geo_crawl_logs (tenant_id,project_id,filename,file_hash,size_bytes,"
            "lines,parsed,hits,range_start,range_end,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (commander.tenant(), project["id"], filename, digest, len(raw),
             summary["lines"], summary["parsed"],
             sum(item["hits"] for item in summary["daily"]),
             summary["start"], summary["end"], stamp),
        ).lastrowid
        for item in summary["daily"]:
            paths = sorted(item["paths"].items(), key=lambda pair: pair[1], reverse=True)[:20]
            db.execute(
                "INSERT INTO geo_crawl_daily (tenant_id,project_id,log_id,date,bot,purpose,"
                "hits,errors,paths) VALUES (?,?,?,?,?,?,?,?,?)",
                (commander.tenant(), project["id"], log_id, item["date"], item["bot"],
                 item["purpose"], item["hits"], item["errors"],
                 json.dumps([{"path": path, "hits": hits} for path, hits in paths],
                            ensure_ascii=False)),
            )
        for item in summary["errors"]:
            db.execute(
                "INSERT INTO geo_crawl_errors (tenant_id,project_id,log_id,date,path,status,"
                "bot,hits) VALUES (?,?,?,?,?,?,?,?)",
                (commander.tenant(), project["id"], log_id, item["date"], item["path"],
                 item["status"], item["bot"], item["hits"]),
            )
        tenancy.audit_in(db, commander.tenant(), "geo.crawl.upload", str(log_id),
                         "filename=%s parsed=%s" % (filename, summary["parsed"]))
        row = db.execute("SELECT * FROM geo_crawl_logs WHERE id=?", (log_id,)).fetchone()
    if not summary["parsed"]:
        return {"ok": True, "duplicate": False,
                "message": "解析完成：这份日志里没识别出 AI 爬虫。",
                "upload": _crawl_log_json(row)}
    return {"ok": True, "duplicate": False,
            "message": "解析完成：认出 %d 次 AI 爬虫访问。" % summary["parsed"],
            "upload": _crawl_log_json(row)}


def _crawl_logs(db, project_id, query):
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = max(1, min(50, int((query.get("page_size") or ["8"])[0])))
    except ValueError as error:
        raise commander.ChatError("分页参数无效。") from error
    rows = db.execute(
        "SELECT * FROM geo_crawl_logs WHERE tenant_id=? AND project_id=? ORDER BY id DESC",
        (commander.tenant(), project_id)).fetchall()
    return {"ok": True, "total": len(rows),
            "items": [_crawl_log_json(row) for row in rows[(page - 1) * size:page * size]]}


def _status_name(status):
    if status in STATUS_TEXT:
        return STATUS_TEXT[status]
    return "服务器出错" if status >= 500 else "抓取失败"


def _crawl_stats(db, project_id, query):
    try:
        days = int((query.get("days") or ["30"])[0])
    except ValueError as error:
        raise commander.ChatError("统计天数无效。") from error
    days = days if days in {7, 30, 90} else 30
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    start_s, end_s = start.isoformat(), end.isoformat()
    daily = db.execute(
        "SELECT * FROM geo_crawl_daily WHERE tenant_id=? AND project_id=? "
        "AND date>=? AND date<=?", (commander.tenant(), project_id, start_s, end_s)).fetchall()
    error_rows = db.execute(
        "SELECT * FROM geo_crawl_errors WHERE tenant_id=? AND project_id=? "
        "AND date>=? AND date<=?", (commander.tenant(), project_id, start_s, end_s)).fetchall()
    uploads = db.execute(
        "SELECT COUNT(*) FROM geo_crawl_logs WHERE tenant_id=? AND project_id=?",
        (commander.tenant(), project_id)).fetchone()[0]
    bots, purposes, series, error_paths = {}, {}, {}, {}
    for item in daily:
        info = next((entry for entry in BOTS if entry[0] == item["bot"]), None)
        operator, product, purpose = (info[1], info[2], info[3]) if info else ("", "", "training")
        bucket = bots.setdefault(item["bot"], {
            "bot": item["bot"], "operator": operator, "product": product,
            "purpose": purpose, "purpose_name": PURPOSE_NAMES.get(purpose, purpose),
            "hits": 0, "errors": 0, "last_date": "", "paths": {},
        })
        bucket["hits"] += int(item["hits"])
        bucket["errors"] += int(item["errors"])
        if item["date"] > bucket["last_date"]:
            bucket["last_date"] = item["date"]
        try:
            paths = json.loads(item["paths"])
        except (TypeError, ValueError):
            paths = []
        for entry in paths if isinstance(paths, list) else []:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path", ""))[:200]
            try:
                hits = int(entry.get("hits", 0))
            except (TypeError, ValueError):
                hits = 0
            if path and hits > 0:
                bucket["paths"][path] = bucket["paths"].get(path, 0) + hits
        purpose_bucket = purposes.setdefault(purpose, 0)
        purposes[purpose] = purpose_bucket + int(item["hits"])
        day = series.setdefault(item["date"], {key: 0 for key in PURPOSE_ORDER})
        day[purpose] = day.get(purpose, 0) + int(item["hits"])
    for item in error_rows:
        key = (item["path"], int(item["status"]))
        bucket = error_paths.setdefault(key, {
            "path": item["path"], "status": int(item["status"]),
            "status_name": _status_name(int(item["status"])),
            "hits": 0, "bots": set(),
        })
        bucket["hits"] += int(item["hits"])
        bucket["bots"].add(item["bot"])
    bot_rows = []
    for bucket in bots.values():
        top = sorted(bucket["paths"].items(), key=lambda pair: pair[1], reverse=True)[:5]
        bot_rows.append({
            "bot": bucket["bot"], "operator": bucket["operator"],
            "product": bucket["product"], "purpose": bucket["purpose"],
            "purpose_name": bucket["purpose_name"], "hits": bucket["hits"],
            "errors": bucket["errors"],
            "error_rate": round(bucket["errors"] / bucket["hits"], 3) if bucket["hits"] else 0,
            "last_date": bucket["last_date"],
            "top_paths": [{"path": path, "hits": hits} for path, hits in top],
        })
    bot_rows.sort(key=lambda item: item["hits"], reverse=True)
    error_rows_out = [{
        **{key: value for key, value in bucket.items() if key != "bots"},
        "bots": sorted(bucket["bots"]),
    } for bucket in error_paths.values()]
    error_rows_out.sort(key=lambda item: item["hits"], reverse=True)
    days_span = [start + timedelta(days=offset) for offset in range(days)]
    return {
        "ok": True, "total": sum(item["hits"] for item in bot_rows),
        "start": start_s, "end": end_s, "uploads": uploads,
        "note": "只统计识别出的 AI 爬虫；原始日志不保存，只留按天汇总的次数。",
        "bots": bot_rows,
        "purposes": [{"key": key, "name": PURPOSE_NAMES[key],
                      "hits": purposes.get(key, 0)} for key in PURPOSE_ORDER],
        "series": [{"date": day.isoformat(),
                    **{key: series.get(day.isoformat(), {}).get(key, 0)
                       for key in PURPOSE_ORDER}} for day in days_span],
        "error_paths": error_rows_out[:20],
    }


def _delete_crawl_log(db, log_id):
    row = db.execute("SELECT * FROM geo_crawl_logs WHERE id=? AND tenant_id=?",
                     (int(log_id), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("上传记录不存在。")
    db.execute("DELETE FROM geo_crawl_daily WHERE log_id=? AND tenant_id=?",
               (row["id"], commander.tenant()))
    db.execute("DELETE FROM geo_crawl_errors WHERE log_id=? AND tenant_id=?",
               (row["id"], commander.tenant()))
    db.execute("DELETE FROM geo_crawl_logs WHERE id=? AND tenant_id=?",
               (row["id"], commander.tenant()))
    tenancy.audit_in(db, commander.tenant(), "geo.crawl.delete", str(row["id"]),
                     row["filename"])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Beacon: visits that AI answers bring to the customer's site


def _beacon_origin(value):
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return "%s://%s" % (parsed.scheme, parsed.netloc)
    return ""


def _source_name(key):
    for item in BEACON_SOURCES:
        if item[0] == key:
            return item[1]
    return "直接访问" if key == "direct" else "其他来源"


def _beacon_script():
    rules = "[" + ",".join(
        "[%s,[%s]]" % (json.dumps(key), ",".join(json.dumps(domain) for domain in domains))
        for key, _name, domains in BEACON_SOURCES) + "]"
    script = """(function(){
  var s=document.currentScript;
  if(!s)return;
  var u;
  try{u=new URL(s.src);}catch(e){return;}
  var origin=u.origin,key=u.searchParams.get('k');
  if(!key)return;
  var R=__RULES__;
  function source(){
    var ref=document.referrer||'';
    if(!ref)return 'direct';
    var host='';
    try{host=new URL(ref).hostname.toLowerCase().replace(/^www\\./,'');}catch(e){return 'other';}
    for(var i=0;i<R.length;i++){var d=R[i][1];for(var j=0;j<d.length;j++){
      var x=d[j];
      if(host===x||host.slice(-(x.length+1))==='.'+x)return R[i][0];
    }}
    return 'other';
  }
  function send(path,data){
    try{
      var body=JSON.stringify(data);
      if(navigator.sendBeacon){navigator.sendBeacon(origin+path,body);return;}
      var x=new XMLHttpRequest();x.open('POST',origin+path,true);
      x.setRequestHeader('Content-Type','text/plain');x.send(body);
    }catch(e){}
  }
  var page=location.pathname.slice(0,200);
  send('/geo-beacon/hit',{k:key,s:source(),p:page});
  document.addEventListener('submit',function(event){
    var form=event.target;
    if(!form||!form.querySelectorAll)return;
    var fields=form.querySelectorAll('input');
    for(var i=0;i<fields.length;i++){
      var field=fields[i];
      var hint=((field.name||'')+' '+(field.id||'')+' '+(field.type||'')+' '+(field.placeholder||'')).toLowerCase();
      if(field.type==='tel'||/phone|mobile|tel|手机|电话/.test(hint)){
        var value=(field.value||'').trim();
        if(/^\\+?[0-9][0-9\\-\\s]{5,19}$/.test(value)){
          send('/geo-beacon/lead',{k:key,s:source(),p:page,phone:value.slice(0,30)});
        }
        break;
      }
    }
  },true);
})();"""
    return script.replace("__RULES__", rules)


def beacon_public(path):
    return path in {BEACON_SCRIPT, BEACON_HIT, BEACON_LEAD}


def _beacon_row(db, project):
    row = db.execute("SELECT * FROM geo_beacons WHERE tenant_id=? AND project_id=?",
                     (commander.tenant(), project["id"])).fetchone()
    if row is not None:
        return row
    stamp = now()
    db.execute(
        "INSERT INTO geo_beacons (tenant_id,project_id,token,leads_enabled,created_at,updated_at) "
        "VALUES (?,?,?,1,?,?)",
        (commander.tenant(), project["id"], secrets.token_urlsafe(18), stamp, stamp))
    return db.execute("SELECT * FROM geo_beacons WHERE tenant_id=? AND project_id=?",
                      (commander.tenant(), project["id"])).fetchone()


def _beacon_site(db, project, origin):
    row = _beacon_row(db, project)
    last = db.execute(
        "SELECT created_at FROM geo_beacon_hits WHERE tenant_id=? AND project_id=? "
        "ORDER BY id DESC LIMIT 1", (commander.tenant(), project["id"])).fetchone()
    base = _beacon_origin(origin)
    snippet = '<script async src="%s/geo-beacon.js?k=%s"></script>' % (base, row["token"])
    return {
        "project_id": project["id"], "snippet": snippet,
        "installed": last is not None,
        "last_hit_at": last["created_at"] if last else "",
        "leads_enabled": bool(row["leads_enabled"]),
    }


def _beacon_read(db, project, query):
    origin = (query.get("origin") or [""])[0]
    return {"ok": True, "site": _beacon_site(db, project, origin),
            "sources": [{"key": key, "name": name} for key, name, _domains in BEACON_SOURCES]}


def _beacon_update(db, project, body):
    row = _beacon_row(db, project)
    stamp = now()
    if body.get("reset_code"):
        db.execute("UPDATE geo_beacons SET token=?,updated_at=? WHERE id=?",
                   (secrets.token_urlsafe(18), stamp, row["id"]))
        tenancy.audit_in(db, commander.tenant(), "geo.beacon.reset", str(project["id"]), "")
    if "leads_enabled" in body:
        enabled = 1 if body.get("leads_enabled") else 0
        db.execute("UPDATE geo_beacons SET leads_enabled=?,updated_at=? WHERE id=?",
                   (enabled, stamp, row["id"]))
    origin = body.get("origin") if isinstance(body.get("origin"), str) else ""
    return {"ok": True, "site": _beacon_site(db, project, origin)}


def _beacon_stats(db, project_id, query):
    try:
        days = int((query.get("days") or ["30"])[0])
    except ValueError as error:
        raise commander.ChatError("统计天数无效。") from error
    days = days if days in {7, 30, 90} else 30
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    rows = db.execute(
        "SELECT * FROM geo_beacon_hits WHERE tenant_id=? AND project_id=? "
        "AND date>=? AND date<=?",
        (commander.tenant(), project_id, start.isoformat(), end.isoformat())).fetchall()
    total = ai = leads = 0
    by_source, by_page, by_day = {}, {}, {}
    for row in rows:
        source = row["source"]
        if row["lead"]:
            leads += 1
            if source in BEACON_KEYS:
                by_source.setdefault(source, {"visits": 0, "leads": 0})["leads"] += 1
            continue
        total += 1
        if source not in BEACON_KEYS:
            continue
        ai += 1
        by_source.setdefault(source, {"visits": 0, "leads": 0})["visits"] += 1
        by_page[row["path"]] = by_page.get(row["path"], 0) + 1
        by_day[row["date"]] = by_day.get(row["date"], 0) + 1
    series = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        series.append({"date": day, "ai": by_day.get(day, 0)})
    sources = [
        {"key": key, "name": _source_name(key),
         "visits": value["visits"], "leads": value["leads"]}
        for key, value in sorted(by_source.items(),
                                 key=lambda item: item[1]["visits"], reverse=True)
    ]
    pages = [{"path": path, "visits": visits} for path, visits in
             sorted(by_page.items(), key=lambda item: item[1], reverse=True)[:10]]
    return {"ok": True, "total_visits": total, "ai_visits": ai, "ai_leads": leads,
            "start": start.isoformat(), "end": end.isoformat(),
            "series": series, "sources": sources, "pages": pages}


def beacon_response(method, path, body):
    """Public beacon endpoints; returns ``(status, content, content_type, headers)``."""
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }
    if path == BEACON_SCRIPT and method in {"GET", "HEAD"}:
        return (200, _beacon_script().encode("utf-8"),
                "application/javascript; charset=utf-8",
                {**headers, "Cache-Control": "public, max-age=3600"})
    if method == "OPTIONS":
        return 204, b"", "text/plain; charset=utf-8", headers
    if method != "POST" or path not in {BEACON_HIT, BEACON_LEAD}:
        return 404, b"", "text/plain; charset=utf-8", headers
    if len(body) > MAX_BEACON_BODY:
        return 413, b"", "text/plain; charset=utf-8", headers
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        return 400, b"", "text/plain; charset=utf-8", headers
    if not isinstance(data, dict):
        return 400, b"", "text/plain; charset=utf-8", headers
    _beacon_record(path, data)
    return 204, b"", "text/plain; charset=utf-8", headers


def _beacon_record(path, data):
    token = str(data.get("k") or "").strip()[:64]
    if not token:
        return
    with commander.database() as db:
        initialize(db)
        beacon = db.execute("SELECT * FROM geo_beacons WHERE token=?", (token,)).fetchone()
        if beacon is None:
            return
        tenant = beacon["tenant_id"]
        project_id = beacon["project_id"]
        source = str(data.get("s") or "").strip()[:20]
        if source != "direct":
            source = source if source in BEACON_KEYS else "other"
        page = str(data.get("p") or "").strip()[:200]
        page = page if page.startswith("/") else "/" + page.lstrip("/")
        stamp = now()
        date = datetime.now(timezone.utc).date().isoformat()
        if path == BEACON_LEAD:
            if not beacon["leads_enabled"]:
                return
            phone = str(data.get("phone") or "").strip()[:30]
            if not PHONE_RE.fullmatch(phone):
                return
            with tenancy.scope(tenant, "beacon", "官网访客", "member"):
                customers.initialize(db)
                try:
                    saved = customers.save(db, {
                        "name": "官网访客", "phone": phone,
                        "notes": "来自官网访问表单（来源：%s）" % _source_name(source),
                        "grade": "B", "stage": "new", "crm_status": "contact",
                    }, source="beacon_lead")
                except commander.ChatError:
                    return
            if not saved.get("created"):
                return
            db.execute(
                "INSERT INTO geo_beacon_hits (tenant_id,project_id,date,source,path,lead,"
                "created_at) VALUES (?,?,?,?,?,1,?)",
                (tenant, project_id, date, source, page, stamp))
            return
        limited = db.execute(
            "SELECT COUNT(*) FROM geo_beacon_hits WHERE tenant_id=? AND project_id=? "
            "AND date=?", (tenant, project_id, date)).fetchone()[0]
        if limited >= 5000:
            return
        db.execute(
            "INSERT INTO geo_beacon_hits (tenant_id,project_id,date,source,path,lead,"
            "created_at) VALUES (?,?,?,?,?,0,?)",
            (tenant, project_id, date, source, page, stamp))


# ---------------------------------------------------------------------------
# Search-console imports: Bing AI Performance / Bing / Google Search Console


def _norm_column(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _csv_rows(raw):
    text = raw.decode("utf-8-sig", "replace")
    sample = text[:4096]
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    rows = []
    for index, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter)):
        if index > 200_000:
            break
        rows.append([cell.strip() for cell in row])
    return rows


def _metric_value(name, raw):
    value = str(raw or "").strip().replace(",", "")
    if not value or value in {"—", "-", "--"}:
        return 0
    if name == "ctr":
        percent = value.endswith("%")
        try:
            number = float(value.rstrip("%"))
        except ValueError:
            return 0
        if percent:
            return round(number / 100, 6)
        return round(number if number <= 1 else number / 100, 6)
    try:
        return float(value)
    except ValueError:
        return 0


def _parse_import(raw, filename, source, period_start, period_end):
    rows = _csv_rows(raw)
    if not rows or not any(cell for cell in rows[0]):
        raise commander.ChatError("报表是空的。")
    header = [_norm_column(cell) for cell in rows[0]]
    keys, metrics = {}, {}
    for name, aliases in KEY_ALIASES.items():
        for position, column in enumerate(header):
            if column in aliases:
                keys[name] = position
                break
    for name, aliases in METRIC_ALIASES.items():
        for position, column in enumerate(header):
            if column in aliases:
                metrics[name] = position
                break
    if not metrics:
        raise commander.ChatError("没认出报表的指标列（点击、展示、引用等）。")
    if "date" in keys:
        kind, key_position = "daily", keys["date"]
    elif "query" in keys:
        kind, key_position = "queries", keys["query"]
    elif "page" in keys:
        kind, key_position = "pages", keys["page"]
    else:
        raise commander.ChatError("没认出报表的行列（日期、页面或搜索词）。")
    detected = source
    if not detected:
        detected = "bing_ai" if "citations" in metrics else (
            "gsc" if any(column in {"top pages", "top queries"} for column in header)
            else "bing")
    values = []
    first_date = last_date = ""
    for row in rows[1:]:
        if not row or len(row) <= key_position:
            continue
        key = row[key_position].strip()
        if not key:
            continue
        if kind == "daily":
            key = _log_date(key)
            if not key:
                continue
            first_date = first_date or key
            last_date = key
        item = {"key": key[:200]}
        for name, position in metrics.items():
            item[name] = _metric_value(
                name, row[position] if position < len(row) else "")
        values.append(item)
    if not values:
        raise commander.ChatError("报表里没有有效数据行。")
    if kind == "daily":
        period_start, period_end = first_date, last_date
    totals = {}
    for name in metrics:
        totals[name] = round(sum(
            item.get(name, 0) for item in values
            if isinstance(item.get(name), (int, float))), 4)
    if totals.get("impressions") and "ctr" in metrics:
        totals["ctr"] = round(totals.get("clicks", 0) / totals["impressions"], 6)
    if "position" in metrics:
        positions = [item.get("position", 0) for item in values if item.get("position")]
        totals["position"] = round(sum(positions) / len(positions), 2) if positions else 0
    main = "clicks" if "clicks" in metrics else (
        "citations" if "citations" in metrics else next(iter(metrics)))
    return {
        "filename": filename[:120], "source": detected, "kind": kind,
        "period_start": str(period_start or "")[:20],
        "period_end": str(period_end or "")[:20],
        "rows": len(values), "main": main, "metrics": list(metrics),
        "metric_names": {name: METRIC_NAMES[name] for name in metrics},
        "totals": totals, "items": values[:100],
    }


def _import_files(raw, filename):
    if filename.lower().endswith(".zip") or raw[:4] == b"PK\x03\x04":
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as error:
            raise commander.ChatError("ZIP 文件解不开。") from error
        files, total = [], 0
        for info in archive.infolist()[:20]:
            if info.is_dir():
                continue
            name = info.filename.rsplit("/", 1)[-1]
            if not name.lower().endswith((".csv", ".tsv", ".txt")):
                continue
            data = archive.read(info)[:MAX_IMPORT_BYTES]
            total += len(data)
            if total > MAX_IMPORT_BYTES * 2:
                raise commander.ChatError("ZIP 解压后过大，请分次上传。")
            files.append((name, data))
        if not files:
            raise commander.ChatError("ZIP 里没有 CSV 文件。")
        return files[:8]
    return [(filename, raw)]


def _mark_own_pages(db, project_id, items):
    geo_content.initialize(db)
    rows = db.execute(
        "SELECT url FROM geo_publications WHERE tenant_id=? AND project_id=? "
        "AND url<>'' AND status IN ('published','draft_saved')",
        (commander.tenant(), project_id)).fetchall()
    paths = set()
    for row in rows:
        url = row["url"]
        path = urlsplit(url).path.rstrip("/") or "/"
        paths.add(path)
        paths.add(url[:200])
    for item in items:
        key = str(item.get("key", ""))
        path = urlsplit(key).path.rstrip("/") if "://" in key else key.rstrip("/")
        item["ours"] = bool(key in paths or path in paths)


def _import_json(row):
    return {
        "id": row["id"], "filename": row["filename"],
        "source": row["source"],
        "source_name": next((item["name"] for item in IMPORT_SOURCES
                             if item["key"] == row["source"]), row["source"]),
        "kind": row["kind"], "kind_name": KIND_NAMES.get(row["kind"], row["kind"]),
        "period_start": row["period_start"], "period_end": row["period_end"],
        "rows": row["rows"], "main": row["main"],
        "metrics": _json(row["metrics"], []),
        "metric_names": _json(row["metric_names"], {}),
        "totals": _json(row["totals"], {}),
        "items": _json(row["items"], []),
        "created_at": row["created_at"],
    }


def upload_import(project_id, content_type, body):
    filename, raw, fields = _upload_fields(content_type, body)
    if len(raw) > MAX_IMPORT_BYTES:
        raise commander.ChatError("请上传 20 MB 以内的报表文件。")
    source = fields.get("source", "")
    if source and source not in {item["key"] for item in IMPORT_SOURCES}:
        raise commander.ChatError("报表来源无效。")
    period_start = fields.get("period_start", "")[:10]
    period_end = fields.get("period_end", "")[:10]
    files = _import_files(raw, filename)
    entries = []
    for name, data in files:
        try:
            entries.append(_parse_import(data, name, source, period_start, period_end))
        except commander.ChatError:
            if len(files) == 1:
                raise
            continue
    if not entries:
        raise commander.ChatError("报表里没认出可导入的数据。")
    stamp = now()
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        project = _project(db, project_id)
        for entry in entries:
            if entry["kind"] == "pages":
                _mark_own_pages(db, project["id"], entry["items"])
            db.execute(
                "INSERT INTO geo_imports (tenant_id,project_id,filename,source,kind,"
                "period_start,period_end,rows,main,metrics,metric_names,totals,items,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (commander.tenant(), project["id"], entry["filename"], entry["source"],
                 entry["kind"], entry["period_start"], entry["period_end"],
                 entry["rows"], entry["main"],
                 json.dumps(entry["metrics"], ensure_ascii=False),
                 json.dumps(entry["metric_names"], ensure_ascii=False),
                 json.dumps(entry["totals"], ensure_ascii=False),
                 json.dumps(entry["items"], ensure_ascii=False), stamp),
            )
        tenancy.audit_in(db, commander.tenant(), "geo.import.add", str(project["id"]),
                         "files=%d" % len(entries))
    total_rows = sum(entry["rows"] for entry in entries)
    return {"ok": True,
            "message": "导入完成：%d 份报表，%d 行。" % (len(entries), total_rows)}


def _imports(db, project_id, query):
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = max(1, min(100, int((query.get("page_size") or ["10"])[0])))
    except ValueError as error:
        raise commander.ChatError("分页参数无效。") from error
    where, values = "WHERE tenant_id=? AND project_id=?", [commander.tenant(), project_id]
    source = (query.get("source") or [""])[0].strip()
    if source:
        where += " AND source=?"
        values.append(source)
    rows = db.execute(
        f"SELECT * FROM geo_imports {where} ORDER BY id DESC", values).fetchall()
    items = []
    for row in rows[(page - 1) * size:page * size]:
        item = _import_json(row)
        item.pop("items", None)
        item.pop("metrics", None)
        items.append(item)
    return {"ok": True, "items": items, "total": len(rows),
            "sources": IMPORT_SOURCES}


def _import_detail(db, import_id):
    row = db.execute("SELECT * FROM geo_imports WHERE id=? AND tenant_id=?",
                     (int(import_id), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("报表不存在。")
    return {"ok": True, "import": _import_json(row)}


def _delete_import(db, import_id):
    row = db.execute("SELECT * FROM geo_imports WHERE id=? AND tenant_id=?",
                     (int(import_id), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("报表不存在。")
    db.execute("DELETE FROM geo_imports WHERE id=? AND tenant_id=?",
               (row["id"], commander.tenant()))
    tenancy.audit_in(db, commander.tenant(), "geo.import.delete", str(row["id"]),
                     row["filename"])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Entry points


def read(path, query):
    path = canonical(path)
    with commander.database() as db:
        initialize(db)
        _recover_stale(db)
        match = AUDIT.fullmatch(path)
        if match:
            row = db.execute("SELECT * FROM geo_site_audits WHERE id=? AND tenant_id=?",
                             (int(match[1]), commander.tenant())).fetchone()
            if row is None:
                raise commander.ChatError("体检记录不存在。")
            return {"ok": True, "audit": _audit_json(row)}
        match = IMPORT_ITEM.fullmatch(path)
        if match:
            return _import_detail(db, match[1])
        match = PROJECT.fullmatch(path)
        if not match:
            raise commander.ChatError("GEO 接口暂不可用。")
        suffix = (match[2] or "").strip("/")
        project = _project(db, match[1])
        if suffix == "beacon":
            return _beacon_read(db, project, query)
        if suffix == "beacon/stats":
            return _beacon_stats(db, project["id"], query)
        if suffix == "imports":
            return _imports(db, project["id"], query)
        if suffix == "crawl-stats":
            return _crawl_stats(db, project["id"], query)
        if suffix == "crawl-logs":
            return _crawl_logs(db, project["id"], query)
        if suffix != "site-audits":
            raise commander.ChatError("GEO 接口暂不可用。")
        try:
            page = max(1, int((query.get("page") or ["1"])[0]))
            size = max(1, min(50, int((query.get("page_size") or ["8"])[0])))
        except ValueError as error:
            raise commander.ChatError("分页参数无效。") from error
        rows = db.execute(
            "SELECT * FROM geo_site_audits WHERE tenant_id=? AND project_id=? "
            "ORDER BY id DESC", (commander.tenant(), project["id"]),
        ).fetchall()
        total = len(rows)
        return {"ok": True, "items": [_audit_list(row) for row in rows[(page - 1) * size:page * size]],
                "total": total}


def write(method, path, body):
    path = canonical(path)
    match = PROJECT.fullmatch(path)
    if match and (match[2] or "").strip("/") == "site-audits" and method == "POST":
        # Fetch the site before holding a write transaction.
        return _start_audit(match[1], body)
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        match = CRAWL_LOG.fullmatch(path)
        if match and method == "DELETE":
            return _delete_crawl_log(db, match[1])
        match = IMPORT_ITEM.fullmatch(path)
        if match and method == "DELETE":
            return _delete_import(db, match[1])
        match = PROJECT.fullmatch(path)
        if match and (match[2] or "").strip("/") == "beacon" and method == "PUT":
            project = _project(db, match[1])
            return _beacon_update(db, project, body)
        match = GENERATOR.fullmatch(path)
        if match and method == "POST":
            project = _project(db, match[1])
            return {"ok": True, "result": _generator(db, project, match[2], body)}
    raise commander.ChatError("GEO 接口暂不可用。")
