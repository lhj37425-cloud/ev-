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
from fnmatch import fnmatchcase
from html.parser import HTMLParser
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import commander
import geo_brand
import geo_content
import tenancy


PROJECT = re.compile(r"/api/geo/projects/([0-9]{1,16})(?:/(.*))?$")
AUDIT = re.compile(r"/api/geo/site-audits/([0-9]{1,16})$")
GENERATOR = re.compile(r"/api/geo/projects/([0-9]{1,16})/generators/([a-z]{2,20})$")
MAX_BYTES = 900_000
FETCH_TIMEOUT = 10
REUSE_MINUTES = 10
DAILY_DEFAULT = 20
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
    if AUDIT.fullmatch(path):
        return True
    match = PROJECT.fullmatch(path)
    return bool(match and (match[2] or "").strip("/") == "site-audits")


def can_write(path):
    path = canonical(path)
    if GENERATOR.fullmatch(path):
        return True
    match = PROJECT.fullmatch(path)
    return bool(match and (match[2] or "").strip("/") == "site-audits")


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
        match = PROJECT.fullmatch(path)
        if not match or (match[2] or "").strip("/") != "site-audits":
            raise commander.ChatError("GEO 接口暂不可用。")
        project = _project(db, match[1])
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
        match = GENERATOR.fullmatch(path)
        if match and method == "POST":
            project = _project(db, match[1])
            return {"ok": True, "result": _generator(db, project, match[2], body)}
    raise commander.ChatError("GEO 接口暂不可用。")
