"""GEO brand knowledge hub: hosted, public brand pages.

Each GEO project can publish a small public site ("品牌知识页") built from
content-workshop articles that passed the fact check. Pages are served by this
backend itself, so they work without the customer owning a website; binding a
custom domain is optional.

The management contract mirrors the archived frontend (``/geo/hub``):

* ``GET/PUT  /api/geo/projects/{id}/hub``            site settings + address
* ``PUT      /api/geo/projects/{id}/hub/domain``     bind / clear a domain
* ``POST     /api/geo/projects/{id}/hub/domain/check`` verify DNS + TLS
* ``GET/POST /api/geo/projects/{id}/hub/pages``      pages / add a page
* ``GET      /api/geo/projects/{id}/hub/candidates`` content that may go up
* ``GET      /api/geo/projects/{id}/hub/stats``      visits, bots, leads
* ``PUT      /api/geo/hub/pages/{id}``               change the section
* ``POST     /api/geo/hub/pages/{id}/sync``          republish the latest draft

Public pages are rendered server-side under ``/geo-hub/{project_id}/`` (the
preview address, always ``noindex``) and, once a domain is verified and TLS is
configured by the deployment, under that domain. A public ``/llms.txt``,
``robots.txt`` and ``sitemap.xml`` are provided for AI crawlers and search
engines. Lead forms write into the tenant's CRM with source ``hub_lead``.
"""

from datetime import datetime, timedelta, timezone
from html import escape
import ipaddress
import json
import os
import re
import secrets
import time
import urllib.request
from urllib.parse import parse_qs, quote, urlsplit

import commander
import customers
import geo_brand
import geo_content
import tenancy


PROJECT_PATH = re.compile(r"/api/geo/projects/([0-9]{1,16})(?:/(.*))?$")
PAGE_PATH = re.compile(r"/api/geo/hub/pages/([0-9]{1,16})$")
PAGE_SYNC = re.compile(r"/api/geo/hub/pages/([0-9]{1,16})/sync$")
PUBLIC_PREFIX = "/geo-hub/"
DOMAIN_PAGE = re.compile(r"^/p/([0-9]{1,16})$")
DOMAIN_VERIFY = "/_ev-hub/verify"
DOMAIN_ROUTES = {"", "/", "/llms.txt", "/robots.txt", "/sitemap.xml", "/lead", DOMAIN_VERIFY}

MAX_TITLE = 120
MAX_INTRO = 500
MAX_ORIGIN = 300
DEFAULT_DAILY = 20
LEAD_LIMIT = 5
LEAD_WINDOW = 600

SECTIONS_CN = [
    {"key": "answers", "name": "常见问题", "genres": ["qa_answer"]},
    {"key": "compare", "name": "对比与选择", "genres": ["comparison"]},
    {"key": "guides", "name": "使用指南", "genres": ["howto"]},
    {"key": "stories", "name": "客户案例", "genres": ["case"]},
    {"key": "other", "name": "更多内容", "genres": []},
]
SECTIONS_GLOBAL = [
    {"key": "answers", "name": "Questions", "genres": ["g_answer"]},
    {"key": "compare", "name": "Compare", "genres": ["comparison"]},
    {"key": "guides", "name": "Guides", "genres": ["howto"]},
    {"key": "stories", "name": "Case studies", "genres": ["case"]},
    {"key": "other", "name": "More", "genres": []},
]
DOMAIN_STATUS_NAMES = {
    "none": "未绑定", "pending_dns": "等待解析", "pending_cert": "等待证书",
    "active": "已生效", "failed": "检测失败",
}
PAGE_STATUS_NAMES = {"live": "已上线", "offline": "已下线"}
BOTS = [
    ("GPTBot", "GPTBot", "OpenAI · 训练语料"),
    ("OAI-SearchBot", "OAI-SearchBot", "OpenAI · 搜索索引"),
    ("ChatGPT-User", "ChatGPT-User", "OpenAI · 用户代抓"),
    ("PerplexityBot", "PerplexityBot", "Perplexity · 搜索索引"),
    ("ClaudeBot", "ClaudeBot", "Anthropic · 抓取"),
    ("Claude-Web", "Claude-Web", "Anthropic · 抓取"),
    ("Google-Extended", "Google-Extended", "Google · AI 训练"),
    ("Googlebot", "Googlebot", "Google · 搜索引擎"),
    ("Bingbot", "Bingbot", "Bing · 搜索引擎"),
    ("Bytespider", "Bytespider", "字节跳动 · 抓取"),
    ("Baiduspider", "Baiduspider", "百度 · 搜索引擎"),
    ("YisouSpider", "YisouSpider", "夸克 · 搜索引擎"),
    ("Amazonbot", "Amazonbot", "Amazon · 索引"),
    ("Applebot", "Applebot", "Apple · 搜索引擎"),
    ("CCBot", "CCBot", "Common Crawl · 语料"),
    ("cohere-ai", "cohere-ai", "Cohere · 抓取"),
]
BOT_NAMES = {(agent.casefold(), name.casefold()): (name, purpose) for agent, name, purpose in BOTS}
AI_SOURCES = [
    ("chatgpt.com", "ChatGPT"), ("chat.openai.com", "ChatGPT"),
    ("perplexity.ai", "Perplexity"), ("claude.ai", "Claude"),
    ("gemini.google.com", "Gemini"), ("copilot.microsoft.com", "Copilot"),
    ("doubao.com", "豆包"), ("yuanbao.tencent.com", "腾讯元宝"),
    ("kimi.com", "Kimi"), ("kimi.moonshot.cn", "Kimi"),
    ("deepseek.com", "DeepSeek"), ("tongyi.aliyun.com", "通义千问"),
    ("qianwen.com", "通义千问"), ("metaso.cn", "秘塔"),
    ("mijia.com", "米塔"), ("chatglm.cn", "智谱清言"),
    ("baichuan-ai.com", "百川"), ("minimaxi.com", "MiniMax"),
]
DOMAIN_RE = re.compile(r"^(?=.{4,191}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.:\[\]-]{1,200}$")
TEXT_LIMITS = {"name": 128, "phone": 32, "contact": 128, "website": 64, "page_id": 16,
               "message": 1000}
_LEAD_HITS = {}


class GateError(commander.ChatError):
    """A publish gate failure carrying the code the frontend reacts to."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def canonical(path):
    if path.startswith("/agent-api"):
        path = path[len("/agent-api"):]
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def can_read(path):
    path = canonical(path)
    match = PROJECT_PATH.fullmatch(path)
    if not match:
        return False
    return (match[2] or "").strip("/") in {"hub", "hub/pages", "hub/candidates", "hub/stats"}


def can_write(path):
    path = canonical(path)
    if PAGE_PATH.fullmatch(path) or PAGE_SYNC.fullmatch(path):
        return True
    match = PROJECT_PATH.fullmatch(path)
    if not match:
        return False
    return (match[2] or "").strip("/") in {"hub", "hub/domain", "hub/domain/check", "hub/pages"}


def empty_post(path):
    path = canonical(path)
    if PAGE_SYNC.fullmatch(path):
        return True
    match = PROJECT_PATH.fullmatch(path)
    return bool(match and (match[2] or "").strip("/") == "hub/domain/check")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value, limit):
    return str(value).strip()[:limit] if isinstance(value, str) else ""


def _hash(value):
    import hashlib
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


def _json(raw, default):
    try:
        value = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_hubs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        title TEXT NOT NULL DEFAULT '', intro TEXT NOT NULL DEFAULT '',
        leads_enabled INTEGER NOT NULL DEFAULT 0, status INTEGER NOT NULL DEFAULT 1,
        domain TEXT NOT NULL DEFAULT '', domain_token TEXT NOT NULL DEFAULT '',
        domain_status TEXT NOT NULL DEFAULT 'none', domain_note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(tenant_id, project_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_hub_pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        content_id INTEGER NOT NULL, section TEXT NOT NULL DEFAULT 'other',
        status TEXT NOT NULL DEFAULT 'live', title TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
        content_sig TEXT NOT NULL DEFAULT '',
        published_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(tenant_id, project_id, content_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_hub_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        page_id INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'other',
        name TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS geo_hub_views_by_project ON geo_hub_views (project_id, created_at)")
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_hub_leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, project_id INTEGER NOT NULL,
        page_id INTEGER NOT NULL DEFAULT 0, customer_id INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS geo_hub_leads_by_project ON geo_hub_leads (project_id, created_at)")


def _sections(market):
    return SECTIONS_GLOBAL if market == "global" else SECTIONS_CN


def _section(key, market):
    wanted = key or "other"
    for item in _sections(market):
        if item["key"] == wanted:
            return item
    return _sections(market)[-1]


def _default_section(genre, market):
    for item in _sections(market):
        if genre in item["genres"]:
            return item["key"]
    return "other"


def _project(db, key):
    row = db.execute("SELECT * FROM geo_projects WHERE id=? AND tenant_id=?",
                     (int(key), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("GEO 项目不存在。")
    return row


def _public_project(db, key):
    return db.execute("SELECT * FROM geo_projects WHERE id=?", (int(key),)).fetchone()


def _ensure_hub(db, project):
    row = db.execute("SELECT * FROM geo_hubs WHERE tenant_id=? AND project_id=?",
                     (project["tenant_id"], project["id"])).fetchone()
    if row is None:
        stamp = now()
        title = project["brand_name"][:MAX_TITLE]
        intro = (project["description"] or "")[:MAX_INTRO]
        db.execute(
            "INSERT OR IGNORE INTO geo_hubs (tenant_id,project_id,title,intro,leads_enabled,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (project["tenant_id"], project["id"], title, intro, 0, 1, stamp, stamp),
        )
        row = db.execute("SELECT * FROM geo_hubs WHERE tenant_id=? AND project_id=?",
                         (project["tenant_id"], project["id"])).fetchone()
    return row


def _origin(value):
    """Accept only a bare http(s) origin; anything else is ignored."""
    value = _text(value, MAX_ORIGIN)
    if not value:
        return ""
    try:
        target = urlsplit(value)
    except ValueError:
        return ""
    if target.scheme not in {"http", "https"} or not target.netloc:
        return ""
    if target.path not in {"", "/"} or target.query or target.fragment or target.username or target.password:
        return ""
    if not HOST_RE.fullmatch(target.netloc):
        return ""
    return f"{target.scheme}://{target.netloc}"


def _host(value):
    value = _text(value, 200)
    target = urlsplit(f"//{value}")
    host = (target.hostname or "").strip().lower().rstrip(".")
    return host


def _base_url(headers):
    host = _text(headers.get("Host", ""), 200)
    if not host or not HOST_RE.fullmatch(host):
        return ""
    proto = headers.get("X-Forwarded-Proto", "")
    proto = proto if proto in {"http", "https"} else "http"
    return f"{proto}://{host}"


def _absolute(base, path):
    return (base.rstrip("/") + path) if base else path


def _normalize_domain(value):
    domain = _text(value, 191).lower().strip(".")
    if not domain:
        return ""
    if not DOMAIN_RE.fullmatch(domain):
        raise commander.ChatError("域名格式无效，请填写不带 http 和路径的域名，如 help.example.com。")
    return domain


def _platform_target(origin=""):
    value = os.environ.get("EV_HUB_CNAME", "").strip()
    if value:
        host = _host(value)
        return host if host and DOMAIN_RE.fullmatch(host) else ""
    parsed = urlsplit(origin) if origin else None
    host = (parsed.hostname or "").lower() if parsed else ""
    if host and DOMAIN_RE.fullmatch(host):
        return host
    return ""


def _dns_records(hub, origin=""):
    target = _platform_target(origin)
    records = []
    if target:
        records.append({"type": "CNAME", "host": hub["domain"], "value": target,
                        "purpose": "把访问转到品牌知识页"})
    records.append({"type": "TXT", "host": f"_ev-hub.{hub['domain']}", "value": hub["domain_token"],
                    "purpose": "证明域名归你所有（证书签好后也请保留）"})
    return records


def _site(db, project, hub, origin=""):
    live = db.execute(
        "SELECT COUNT(*) FROM geo_hub_pages WHERE tenant_id=? AND project_id=? AND status='live'",
        (hub["tenant_id"], project["id"]),
    ).fetchone()[0]
    domain = hub["domain"]
    active = bool(domain) and hub["domain_status"] == "active"
    return {
        "id": hub["id"], "project_id": project["id"],
        "title": hub["title"], "intro": hub["intro"],
        "leads_enabled": bool(hub["leads_enabled"]), "status": int(hub["status"]),
        "can_edit": True, "pages_live": live,
        "preview_url": _absolute(origin, f"/geo-hub/{project['id']}/"),
        "domain": domain,
        "domain_url": f"https://{domain}/" if active else "",
        "domain_status": hub["domain_status"],
        "domain_status_name": DOMAIN_STATUS_NAMES.get(hub["domain_status"], hub["domain_status"]),
        "domain_note": hub["domain_note"],
        "dns": _dns_records(hub, origin) if domain and not active else [],
        "icp_notice": bool(domain) and project["market"] == "cn",
        "created_at": hub["created_at"], "updated_at": hub["updated_at"],
    }


def _gate(row):
    gate = _json(row["gate"], {})
    return gate, bool(gate) and row["gate_sig"] == _hash(row["body"])


def _content(db, project_id, content_id):
    return db.execute(
        "SELECT * FROM geo_contents WHERE id=? AND project_id=? AND tenant_id=?",
        (int(content_id), int(project_id), commander.tenant()),
    ).fetchone()


def _page_url(project_id, page_id, domain=""):
    if domain:
        return f"https://{domain}/p/{page_id}"
    return f"/geo-hub/{project_id}/p/{page_id}"


def _page_json(db, project, page, origin=""):
    content = db.execute("SELECT * FROM geo_contents WHERE id=? AND tenant_id=?",
                         (page["content_id"], page["tenant_id"])).fetchone()
    hub = db.execute("SELECT * FROM geo_hubs WHERE tenant_id=? AND project_id=?",
                     (page["tenant_id"], page["project_id"])).fetchone()
    active = bool(hub and hub["domain"]) and hub["domain_status"] == "active"
    url = _page_url(project["id"], page["id"], hub["domain"] if active else "")
    if not active:
        url = _absolute(origin, url)
    return {
        "id": page["id"], "content_id": page["content_id"],
        "title": page["title"], "summary": page["summary"],
        "section": page["section"], "section_name": _section(page["section"], project["market"])["name"],
        "status": page["status"], "status_name": PAGE_STATUS_NAMES.get(page["status"], page["status"]),
        "url": url, "preview_url": _absolute(origin, _page_url(project["id"], page["id"])),
        "cited_count": 0, "published_at": page["published_at"], "updated_at": page["updated_at"],
        "outdated": bool(content) and _hash(content["body"]) != page["content_sig"],
        "content_missing": content is None,
    }


def _page_snapshot(content):
    title = (content["title"] or content["question"] or "未命名内容")[:200]
    summary = (content["summary"] or "")[:300]
    return title, content["body"], summary


def _sync_publication(db, project, content, page, status):
    hub = db.execute("SELECT * FROM geo_hubs WHERE tenant_id=? AND project_id=?",
                     (page["tenant_id"], page["project_id"])).fetchone()
    active = bool(hub and hub["domain"]) and hub["domain_status"] == "active"
    url = _page_url(project["id"], page["id"], hub["domain"] if active else "")
    row = db.execute(
        "SELECT * FROM geo_publications WHERE tenant_id=? AND content_id=? AND channel_kind='hub'",
        (page["tenant_id"], content["id"]),
    ).fetchone()
    stamp = now()
    title = page["title"]
    if row is None:
        db.execute(
            "INSERT INTO geo_publications (tenant_id,project_id,content_id,ticket_id,"
            "channel_kind,platform,platform_name,title,status,url,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (page["tenant_id"], project["id"], content["id"], 0, "hub", "hub", "品牌知识页",
             title, status, url if status == "published" else "", stamp, stamp),
        )
    else:
        db.execute(
            "UPDATE geo_publications SET status=?, title=?, url=?, updated_at=? "
            "WHERE id=? AND tenant_id=?",
            (status, title, url if status == "published" else row["url"], stamp, row["id"],
             page["tenant_id"]),
        )


def _site_update(db, project, hub, body):
    title = _text(body.get("title"), MAX_TITLE)
    if len(title) < 2:
        raise commander.ChatError("站点名称至少填 2 个字。")
    intro = _text(body.get("intro"), MAX_INTRO)
    leads = body.get("leads_enabled", bool(hub["leads_enabled"]))
    if isinstance(leads, str):
        leads = leads.strip().lower() in {"1", "true", "yes", "on"}
    status = body.get("status", hub["status"])
    if isinstance(status, bool):
        status = 1 if status else 0
    try:
        status = 1 if int(status) else 0
    except (TypeError, ValueError) as error:
        raise commander.ChatError("对外开放状态无效。") from error
    stamp = now()
    db.execute(
        "UPDATE geo_hubs SET title=?, intro=?, leads_enabled=?, status=?, updated_at=? "
        "WHERE id=? AND tenant_id=?",
        (title, intro, 1 if leads else 0, status, stamp, hub["id"], hub["tenant_id"]),
    )
    tenancy.audit_in(db, hub["tenant_id"], "geo.hub.update", str(project["id"]), f"status={status}")
    return db.execute("SELECT * FROM geo_hubs WHERE id=?", (hub["id"],)).fetchone()


def _site_domain(db, project, hub, body, origin):
    domain = _normalize_domain(body.get("domain"))
    if domain == hub["domain"]:
        return hub
    if domain:
        clash = db.execute(
            "SELECT 1 FROM geo_hubs WHERE lower(domain)=? AND NOT (tenant_id=? AND project_id=?)",
            (domain, hub["tenant_id"], project["id"]),
        ).fetchone()
        if clash:
            raise commander.ChatError("这个域名已经被其它品牌知识页绑定。")
        token = hub["domain_token"] or secrets.token_urlsafe(18).replace("-", "x").replace("_", "y")[:32]
        note = "保存后去域名服务商添加下面的解析记录，生效后点「重新检查」。"
        stamp = now()
        db.execute(
            "UPDATE geo_hubs SET domain=?, domain_token=?, domain_status='pending_dns', "
            "domain_note=?, updated_at=? WHERE id=? AND tenant_id=?",
            (domain, token, note, stamp, hub["id"], hub["tenant_id"]),
        )
        tenancy.audit_in(db, hub["tenant_id"], "geo.hub.domain", str(project["id"]), domain)
    else:
        db.execute(
            "UPDATE geo_hubs SET domain='', domain_status='none', domain_note='', updated_at=? "
            "WHERE id=? AND tenant_id=?",
            (now(), hub["id"], hub["tenant_id"]),
        )
        tenancy.audit_in(db, hub["tenant_id"], "geo.hub.domain", str(project["id"]), "clear")
    return db.execute("SELECT * FROM geo_hubs WHERE id=?", (hub["id"],)).fetchone()


def _dns_values(name, rtype):
    """Resolve through DNS-over-HTTPS so domain checks work without extra libs."""
    base = os.environ.get("EV_HUB_DNS_DOH", "https://cloudflare-dns.com/dns-query")
    target = f"{base}?name={quote(name)}&type={rtype}"
    request = urllib.request.Request(
        target, headers={"Accept": "application/dns-json", "User-Agent": "EVCommander-Hub/1.0"})
    with urllib.request.urlopen(request, timeout=6) as response:
        payload = json.loads(response.read(200_000).decode("utf-8"))
    wanted = {"A": {1}, "AAAA": {28}, "CNAME": {5}, "TXT": {16}}.get(rtype, {1})
    values = []
    for answer in payload.get("Answer") or []:
        if int(answer.get("type") or 0) in wanted:
            values.append(str(answer.get("data") or "").strip().strip('"'))
    return values


def _addresses(host):
    try:
        return {str(ipaddress.ip_address(host))}
    except ValueError:
        pass
    return {value for value in _dns_values(host, "A") if value}


def _probe_verify(domain, token):
    request = urllib.request.Request(
        f"https://{domain}{DOMAIN_VERIFY}",
        headers={"User-Agent": "EVCommander-Hub/1.0"})
    with urllib.request.urlopen(request, timeout=6) as response:
        return token in response.read(400).decode("utf-8", "replace")


def _domain_check(db, project, hub, origin):
    domain = hub["domain"]
    if not domain:
        raise commander.ChatError("请先填写要绑定的域名。")
    target = _platform_target(origin)
    if not target:
        status, note = "failed", "平台还没配置品牌知识页的域名（EV_HUB_CNAME），暂时无法检测解析。"
    else:
        try:
            addresses = _addresses(domain)
            target_addresses = _addresses(target)
            txts = _dns_values(f"_ev-hub.{domain}", "TXT")
        except Exception:
            addresses = set()
            status, note = "failed", "DNS 查询暂时失败，请检查网络后重试。"
        else:
            if not addresses:
                status, note = "pending_dns", "还没查到解析记录，保存后可能要几分钟到几小时生效。"
            elif not (addresses & target_addresses):
                status, note = "pending_dns", "域名还没指向平台，请按上面的记录修改解析。"
            elif not any(hub["domain_token"] in value for value in txts):
                status, note = "failed", "TXT 校验记录还没生效或值不对，请检查后重试。"
            else:
                try:
                    verified = _probe_verify(domain, hub["domain_token"])
                except Exception:
                    verified = False
                if verified:
                    status, note = "active", ""
                else:
                    status, note = "pending_cert", "解析已生效，证书还没签发完成；部署完成后点「重新检查」。"
    db.execute("UPDATE geo_hubs SET domain_status=?, domain_note=?, updated_at=? "
               "WHERE id=? AND tenant_id=?",
               (status, note, now(), hub["id"], hub["tenant_id"]))
    tenancy.audit_in(db, hub["tenant_id"], "geo.hub.domain.check", str(project["id"]), status)
    return db.execute("SELECT * FROM geo_hubs WHERE id=?", (hub["id"],)).fetchone()


def _daily_limit():
    try:
        value = int(os.environ.get("EV_HUB_DAILY_LIMIT", DEFAULT_DAILY))
    except (TypeError, ValueError):
        value = DEFAULT_DAILY
    return max(0, value)


def _add_page(db, project, hub, body, origin):
    content = _content(db, project["id"], body.get("content_id") or 0)
    if content is None:
        raise commander.ChatError("内容不存在。")
    if not content["body"].strip():
        raise commander.ChatError("这篇内容还没有正文。")
    gate, valid = _gate(content)
    if not gate:
        raise GateError("这篇还没做事实质检，先到内容工坊质检。", "gate_required")
    if not valid:
        raise GateError("正文改过之后还没重新质检。", "gate_stale")
    if gate.get("decision") not in {"pass", "approved"}:
        raise GateError("质检结果是「需要确认 / 必须改」，先在内容工坊放行或修改。", "gate_review")
    existing = db.execute(
        "SELECT 1 FROM geo_hub_pages WHERE tenant_id=? AND project_id=? AND content_id=?",
        (hub["tenant_id"], project["id"], content["id"]),
    ).fetchone()
    if existing:
        raise commander.ChatError("这篇已经在品牌知识页上了。")
    limit = _daily_limit()
    if limit:
        day = datetime.now(timezone.utc).date().isoformat()
        used = db.execute(
            "SELECT COUNT(*) FROM geo_hub_pages WHERE tenant_id=? AND project_id=? "
            "AND substr(created_at,1,10)=?",
            (hub["tenant_id"], project["id"], day),
        ).fetchone()[0]
        if used >= limit:
            raise commander.ChatError("今天上页的数量已达到上限，明天再继续。")
    section = _text(body.get("section"), 20) or _default_section(content["genre"], project["market"])
    section = _section(section, project["market"])["key"]
    title, page_body, summary = _page_snapshot(content)
    stamp = now()
    page_id = db.execute(
        "INSERT INTO geo_hub_pages (tenant_id,project_id,content_id,section,status,title,body,"
        "summary,content_sig,published_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (hub["tenant_id"], project["id"], content["id"], section, "live", title, page_body,
         summary, _hash(page_body), stamp, stamp, stamp),
    ).lastrowid
    page = db.execute("SELECT * FROM geo_hub_pages WHERE id=?", (page_id,)).fetchone()
    _sync_publication(db, project, content, page, "published")
    tenancy.audit_in(db, hub["tenant_id"], "geo.hub.page.add", str(page_id), title)
    return page


def _sync_page(db, project, hub, page):
    content = db.execute("SELECT * FROM geo_contents WHERE id=? AND tenant_id=?",
                         (page["content_id"], page["tenant_id"])).fetchone()
    if content is None:
        raise commander.ChatError("原内容已删除，不能同步。")
    gate, valid = _gate(content)
    if not gate:
        raise GateError("这篇还没做事实质检，先到内容工坊质检。", "gate_required")
    if not valid:
        raise GateError("正文改过之后还没重新质检。", "gate_stale")
    if gate.get("decision") not in {"pass", "approved"}:
        raise GateError("质检结果是「需要确认 / 必须改」，先在内容工坊放行或修改。", "gate_review")
    title, page_body, summary = _page_snapshot(content)
    stamp = now()
    db.execute(
        "UPDATE geo_hub_pages SET title=?, body=?, summary=?, content_sig=?, updated_at=? "
        "WHERE id=? AND tenant_id=?",
        (title, page_body, summary, _hash(page_body), stamp, page["id"], page["tenant_id"]),
    )
    page = db.execute("SELECT * FROM geo_hub_pages WHERE id=?", (page["id"],)).fetchone()
    if page["status"] == "live":
        _sync_publication(db, project, content, page, "published")
    tenancy.audit_in(db, hub["tenant_id"], "geo.hub.page.sync", str(page["id"]), title)
    return page


def _update_page(db, project, hub, page, body):
    stamp = now()
    if "section" in body:
        section = _text(body.get("section"), 20)
        if not section:
            raise commander.ChatError("请选择栏目。")
        section = _section(section, project["market"])["key"]
        db.execute("UPDATE geo_hub_pages SET section=?, updated_at=? WHERE id=? AND tenant_id=?",
                   (section, stamp, page["id"], page["tenant_id"]))
        tenancy.audit_in(db, page["tenant_id"], "geo.hub.page.section", str(page["id"]), section)
    if "live" in body:
        live = body.get("live")
        if isinstance(live, str):
            live = live.strip().lower() in {"1", "true", "yes", "on"}
        status = "live" if live else "offline"
        db.execute("UPDATE geo_hub_pages SET status=?, updated_at=? WHERE id=? AND tenant_id=?",
                   (status, stamp, page["id"], page["tenant_id"]))
        content = db.execute("SELECT * FROM geo_contents WHERE id=? AND tenant_id=?",
                             (page["content_id"], page["tenant_id"])).fetchone()
        if content is not None:
            page = db.execute("SELECT * FROM geo_hub_pages WHERE id=?", (page["id"],)).fetchone()
            _sync_publication(db, project, content, page,
                              "published" if status == "live" else "offline")
        tenancy.audit_in(db, page["tenant_id"], "geo.hub.page.live", str(page["id"]), status)
    return db.execute("SELECT * FROM geo_hub_pages WHERE id=?", (page["id"],)).fetchone()


def _list_pages(db, project, hub, query, origin):
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = max(1, min(100, int((query.get("page_size") or query.get("pageSize") or ["20"])[0])))
    except ValueError as error:
        raise commander.ChatError("分页参数无效。") from error
    status = (query.get("status") or [""])[0].strip()
    where, values = "WHERE tenant_id=? AND project_id=?", [hub["tenant_id"], project["id"]]
    if status in PAGE_STATUS_NAMES:
        where += " AND status=?"
        values.append(status)
    rows = db.execute(f"SELECT * FROM geo_hub_pages {where} ORDER BY id DESC", values).fetchall()
    total = len(rows)
    rows = rows[(page - 1) * size:page * size]
    return {"ok": True, "items": [_page_json(db, project, row, origin) for row in rows],
            "total": total, "sections": _sections(project["market"])}


def _candidates(db, project, hub, query):
    try:
        page = max(1, int((query.get("page") or ["1"])[0]))
        size = max(1, min(100, int((query.get("page_size") or ["10"])[0])))
    except ValueError as error:
        raise commander.ChatError("分页参数无效。") from error
    focus = (query.get("focus") or ["0"])[0]
    used = {row[0] for row in db.execute(
        "SELECT content_id FROM geo_hub_pages WHERE tenant_id=? AND project_id=?",
        (hub["tenant_id"], project["id"])).fetchall()}
    rows = db.execute(
        "SELECT * FROM geo_contents WHERE tenant_id=? AND project_id=? AND status<>'archived' "
        "ORDER BY id DESC", (hub["tenant_id"], project["id"])).fetchall()
    items = []
    for row in rows:
        if row["id"] in used:
            continue
        gate, valid = _gate(row)
        ok, reason = True, ""
        if not row["body"].strip():
            ok, reason = False, "正文还是空的"
        elif not gate:
            ok, reason = False, "还没做事实质检"
        elif not valid:
            ok, reason = False, "正文改过之后还没重新质检"
        elif gate.get("decision") not in {"pass", "approved"}:
            ok, reason = False, "质检还没放行"
        items.append({
            "content_id": row["id"], "title": row["title"] or row["question"] or "未命名内容",
            "genre": row["genre"],
            "genre_name": next((item["name"] for item in _genres(project["market"])
                                if item["key"] == row["genre"]), row["genre"]),
            "updated_at": row["updated_at"], "ok": ok, "reason": reason,
            "section": _default_section(row["genre"], project["market"]),
        })
    if focus and focus.isdigit():
        items.sort(key=lambda item: 0 if item["content_id"] == int(focus) else 1)
    total = len(items)
    items = items[(page - 1) * size:page * size]
    return {"ok": True, "items": items, "total": total, "sections": _sections(project["market"])}


def _genres(market):
    return geo_content.GENRES_GLOBAL if market == "global" else geo_content.GENRES_CN


def _bot(user_agent):
    lowered = (user_agent or "").casefold()
    for agent, name, purpose in BOTS:
        if agent.casefold() in lowered:
            return agent, name, purpose
    return None


def _ai_source(referer):
    try:
        host = (urlsplit(referer or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    for domain, name in AI_SOURCES:
        if host == domain or host.endswith("." + domain):
            return host, name
    return None


def _record_view(db, hub, page_id, headers):
    bot = _bot(headers.get("User-Agent", ""))
    if bot:
        agent, name, _purpose = bot
        kind, name, source = "bot", name, agent
    else:
        found = _ai_source(headers.get("Referer", ""))
        if found:
            source, name = found
            kind = "ai"
        else:
            kind, name, source = "other", "", ""
    db.execute(
        "INSERT INTO geo_hub_views (tenant_id,project_id,page_id,kind,name,source,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (hub["tenant_id"], hub["project_id"], int(page_id or 0), kind, name, source, now()),
    )


def _stats(db, project, hub, query):
    try:
        days = max(1, min(365, int((query.get("days") or ["30"])[0])))
    except ValueError:
        days = 30
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.execute(
        "SELECT kind,name,source,page_id,created_at FROM geo_hub_views "
        "WHERE tenant_id=? AND project_id=? AND created_at>=?",
        (hub["tenant_id"], project["id"], since),
    ).fetchall()
    bots, sources = {}, {}
    bot_total = ai_visits = 0
    for row in rows:
        if row["kind"] == "bot":
            bot_total += 1
            entry = bots.setdefault(row["name"], {"agent": row["name"], "name": row["name"],
                                                  "purpose_name": BOT_NAMES.get(
                                                      (row["name"].casefold(), row["name"].casefold()),
                                                      ("", ""))[1], "hits": 0})
            entry["hits"] += 1
        elif row["kind"] == "ai":
            ai_visits += 1
            entry = sources.setdefault(row["source"], {"source": row["source"],
                                                       "name": row["name"], "hits": 0})
            entry["hits"] += 1
    leads = db.execute(
        "SELECT COUNT(*) FROM geo_hub_leads WHERE tenant_id=? AND project_id=? AND created_at>=?",
        (hub["tenant_id"], project["id"], since),
    ).fetchone()[0]
    by_hits = lambda item: (-item["hits"], item["name"])  # noqa: E731
    return {
        "ok": True,
        "note": f"近 {days} 天；AI 爬虫按访问时的 User-Agent 识别，从 AI 点进来按来源域名识别。",
        "bot_total": bot_total, "ai_visits": ai_visits,
        "other_visits": max(0, len(rows) - bot_total - ai_visits),
        "leads": leads,
        "bots": sorted(bots.values(), key=by_hits)[:8],
        "sources": sorted(sources.values(), key=by_hits)[:8],
    }


# ---------------------------------------------------------------------------
# Management entry points


def read(path, query):
    path = canonical(path)
    origin = _origin((query.get("origin") or [""])[0])
    with commander.database() as db:
        initialize(db)
        match = PROJECT_PATH.fullmatch(path)
        if not match:
            raise commander.ChatError("GEO 接口暂不可用。")
        project = _project(db, match[1])
        hub = _ensure_hub(db, project)
        suffix = (match[2] or "").strip("/")
        if suffix == "hub":
            return {"ok": True, "site": _site(db, project, hub, origin)}
        if suffix == "hub/pages":
            return _list_pages(db, project, hub, query, origin)
        if suffix == "hub/candidates":
            return _candidates(db, project, hub, query)
        if suffix == "hub/stats":
            return _stats(db, project, hub, query)
    raise commander.ChatError("GEO 接口暂不可用。")


def write(method, path, body):
    path = canonical(path)
    origin = _origin((body.get("origin") or "") if isinstance(body.get("origin"), str) else "")
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        match = PROJECT_PATH.fullmatch(path)
        if match:
            project = _project(db, match[1])
            hub = _ensure_hub(db, project)
            suffix = (match[2] or "").strip("/")
            if suffix == "hub" and method == "PUT":
                hub = _site_update(db, project, hub, body)
                return {"ok": True, "site": _site(db, project, hub, origin)}
            if suffix == "hub/domain" and method == "PUT":
                hub = _site_domain(db, project, hub, body, origin)
                return {"ok": True, "site": _site(db, project, hub, origin)}
            if suffix == "hub/domain/check" and method == "POST":
                hub = _domain_check(db, project, hub, origin)
                return {"ok": True, "site": _site(db, project, hub, origin)}
            if suffix == "hub/pages" and method == "POST":
                page = _add_page(db, project, hub, body, origin)
                return {"ok": True, "page": _page_json(db, project, page, origin),
                        "site": _site(db, project, hub, origin)}
        match = PAGE_PATH.fullmatch(path)
        if match and method == "PUT":
            page = db.execute("SELECT * FROM geo_hub_pages WHERE id=? AND tenant_id=?",
                              (int(match[1]), commander.tenant())).fetchone()
            if page is None:
                raise commander.ChatError("页面不存在。")
            project = db.execute("SELECT * FROM geo_projects WHERE id=?",
                                 (page["project_id"],)).fetchone()
            hub = _ensure_hub(db, project)
            page = _update_page(db, project, hub, page, body)
            return {"ok": True, "page": _page_json(db, project, page, origin)}
        match = PAGE_SYNC.fullmatch(path)
        if match and method == "POST":
            page = db.execute("SELECT * FROM geo_hub_pages WHERE id=? AND tenant_id=?",
                              (int(match[1]), commander.tenant())).fetchone()
            if page is None:
                raise commander.ChatError("页面不存在。")
            project = db.execute("SELECT * FROM geo_projects WHERE id=?",
                                 (page["project_id"],)).fetchone()
            hub = _ensure_hub(db, project)
            page = _sync_page(db, project, hub, page)
            return {"ok": True, "page": _page_json(db, project, page, origin)}
    raise commander.ChatError("GEO 接口暂不可用。")


# ---------------------------------------------------------------------------
# Public site


def _hub_for_project(db, project_id):
    return db.execute("SELECT * FROM geo_hubs WHERE project_id=?", (int(project_id),)).fetchone()


def _hub_for_domain(db, domain):
    if not domain:
        return None
    return db.execute("SELECT * FROM geo_hubs WHERE lower(domain)=?", (domain,)).fetchone()


def _domain_bound(host):
    if not host or not DOMAIN_RE.fullmatch(host):
        return False
    try:
        with commander.database() as db:
            initialize(db)
            row = _hub_for_domain(db, host)
            return row is not None
    except Exception:
        return False


def handles(path, headers):
    if path.startswith(PUBLIC_PREFIX):
        return True
    if path in DOMAIN_ROUTES or DOMAIN_PAGE.fullmatch(path):
        return _domain_bound(_host(headers.get("Host", "")))
    return False


def _page_error(status, message):
    body = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex,nofollow\">"
            f"<title>{escape(message)}</title></head><body style=\"font:16px/1.6 system-ui;"
            "margin:12vh auto;max-width:560px;padding:0 16px;color:#333\">"
            f"<h1 style=\"font-size:20px\">{escape(message)}</h1></body></html>")
    return status, body.encode("utf-8"), "text/html; charset=utf-8", {"X-Robots-Tag": "noindex, nofollow"}


def _not_found(message="页面不存在。"):
    return _page_error(404, message)


def _public_context(db, project_id, domain):
    hub = _hub_for_project(db, project_id) if project_id else None
    if hub is None:
        return None, None
    project = _public_project(db, project_id)
    if project is None:
        return None, None
    if hub["status"] != 1:
        return None, None
    if domain is not None:
        if hub["domain_status"] != "active" or hub["domain"] != domain:
            return None, None
    return project, hub


def _live_pages(db, hub):
    return db.execute(
        "SELECT * FROM geo_hub_pages WHERE tenant_id=? AND project_id=? AND status='live' "
        "ORDER BY id", (hub["tenant_id"], hub["project_id"]),
    ).fetchall()


def _grouped(pages, market):
    groups = []
    for section in _sections(market):
        entries = [page for page in pages if page["section"] == section["key"]]
        if entries:
            groups.append((section["name"], entries))
    return groups


def _facts(db, project):
    geo_brand.initialize(db)
    return db.execute(
        "SELECT label,value FROM geo_facts WHERE tenant_id=? AND project_id=? AND status='confirmed' "
        "ORDER BY id LIMIT 12", (project["tenant_id"], project["id"]),
    ).fetchall()


def _form_html(action, thanks, page_id=0):
    if thanks:
        return ('<div class="ok"><b>已收到，谢谢！</b><div>我们会尽快联系你。</div></div>')
    hidden = f'<input type="hidden" name="page_id" value="{int(page_id)}">' if page_id else ""
    return f"""<form class="lead" method="post" action="{escape(action)}">
{hidden}<input type="text" name="website" value="" tabindex="-1" autocomplete="off" class="hp">
<label>称呼 / 公司<input name="name" maxlength="128" placeholder="怎么称呼你"></label>
<label>手机<input name="phone" maxlength="32" inputmode="tel" placeholder="方便联系的手机号"></label>
<label>微信 / 邮箱<input name="contact" maxlength="128" placeholder="微信或邮箱，任选其一"></label>
<label>想了解什么<textarea name="message" rows="3" maxlength="1000" placeholder="简单说一下你的需求"></textarea></label>
<button type="submit">提交，让我联系你</button>
</form>"""


def _layout(title, description, canonical, preview, lang, content, json_ld=None):
    robots = '<meta name="robots" content="noindex,nofollow">' if preview else ""
    canon = f'<link rel="canonical" href="{escape(canonical)}">' if canonical else ""
    ld = ""
    if json_ld:
        ld = ('<script type="application/ld+json">'
              + json.dumps(json_ld, ensure_ascii=False).replace("</", "<\\/") + "</script>")
    css = """
    :root{--ink:#1f2430;--muted:#667085;--line:#e4e7ec;--brand:#0f4c81;--bg:#f7f8fa}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
    font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
    a{color:var(--brand);text-decoration:none}a:hover{text-decoration:underline}
    .wrap{max-width:820px;margin:0 auto;padding:24px 16px 56px}
    header.site{padding:28px 0 8px}header.site h1{font-size:26px;margin:0 0 6px}
    header.site p{margin:0;color:var(--muted)}
    .panel{background:#fff;border:1px solid var(--line);border-radius:12px;padding:24px;margin-top:20px}
    .panel h2{font-size:19px;margin:0 0 14px}
    .card{display:block;padding:14px 0;border-top:1px solid var(--line);color:inherit}
    .card:first-of-type{border-top:0}.card b{display:block;font-size:16px}
    .card span{color:var(--muted);font-size:14px}
    .meta{color:var(--muted);font-size:14px}.crumb{font-size:14px;color:var(--muted);margin-bottom:8px}
    article h2{font-size:20px;margin:24px 0 8px}article h3{font-size:17px;margin:20px 0 6px}
    article p{margin:12px 0}article ul,article ol{margin:12px 0;padding-left:24px}
    article blockquote{margin:12px 0;padding:8px 14px;border-left:3px solid var(--line);color:var(--muted)}
    article code{background:#f2f3f5;padding:1px 5px;border-radius:4px;font-size:14px}
    article pre{background:#f2f3f5;padding:14px;border-radius:8px;overflow:auto}
    .facts{margin:0;padding-left:18px}.facts li{margin:6px 0}.facts b{font-weight:600}
    .lead{display:grid;gap:12px;margin-top:18px}.lead label{display:grid;gap:4px;font-size:14px;color:var(--muted)}
    .lead input,.lead textarea{font:inherit;padding:9px 11px;border:1px solid var(--line);border-radius:8px}
    .lead button{font:inherit;padding:10px 16px;border:0;border-radius:8px;background:var(--brand);color:#fff;cursor:pointer}
    .lead .hp{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}
    .ok{background:#ecfdf3;border:1px solid #abefc6;border-radius:10px;padding:14px 16px}
    footer.site{margin-top:28px;color:var(--muted);font-size:13px}
    """
    return f"""<!doctype html>
<html lang="{escape(lang)}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><meta name="description" content="{escape(description)}">
{robots}{canon}<meta property="og:title" content="{escape(title)}">
<meta property="og:description" content="{escape(description)}"><meta property="og:type" content="website">
<style>{css}</style>{ld}</head><body><div class="wrap">{content}
<footer class="site">本页由品牌知识页生成，内容由品牌方维护。</footer></div></body></html>"""


def _inline(markdown):
    value = escape(markdown)
    codes = []
    def hold(match):
        codes.append(match.group(1))
        return f"\x00{len(codes) - 1}\x00"
    value = re.sub(r"`([^`]{1,200})`", hold, value)

    def link(match):
        text, url = match.group(1), match.group(2).replace("&amp;", "&")
        if not re.match(r"^https?://", url):
            return match.group(1)
        return f'<a href="{escape(url)}" rel="nofollow noopener">{text}</a>'
    value = re.sub(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]{1,500})\)", link, value)
    value = re.sub(r"\*\*([^*\n]{1,300})\*\*", r"<b>\1</b>", value)
    value = re.sub(r"(?<!\*)\*([^*\n]{1,300})\*(?!\*)", r"<i>\1</i>", value)
    for index, code in enumerate(codes):
        value = value.replace(f"\x00{index}\x00", f"<code>{code}</code>")
    return value


def _markdown(text):
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, paragraph, items, ordered = [], [], [], False
    in_code, code_lines = False, []

    def flush_paragraph():
        if paragraph:
            out.append("<p>" + _inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()

    def flush_list():
        if items:
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(item)}</li>" for item in items)
                       + f"</{tag}>")
            items.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            flush_list()
            if in_code:
                out.append("<pre>" + escape("\n".join(code_lines)) + "</pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        number = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if not stripped:
            flush_paragraph()
            flush_list()
        elif heading:
            flush_paragraph()
            flush_list()
            level = min(6, len(heading.group(1)) + 1)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif re.fullmatch(r"-{3,}", stripped):
            flush_paragraph()
            flush_list()
            out.append("<hr>")
        elif stripped.startswith(">"):
            flush_paragraph()
            flush_list()
            out.append("<blockquote>" + _inline(stripped.lstrip(">").strip()) + "</blockquote>")
        elif bullet:
            flush_paragraph()
            if items and ordered:
                flush_list()
            ordered = False
            items.append(bullet.group(1))
        elif number:
            flush_paragraph()
            if items and not ordered:
                flush_list()
            ordered = True
            items.append(number.group(1))
        else:
            flush_list()
            paragraph.append(stripped)
    if in_code and code_lines:
        out.append("<pre>" + escape("\n".join(code_lines)) + "</pre>")
    flush_paragraph()
    flush_list()
    return "\n".join(out)


def _home_html(db, project, hub, pages, base, preview, thanks):
    facts = _facts(db, project)
    title = hub["title"]
    intro = hub["intro"]
    lead_action = "/lead" if not preview else f"/geo-hub/{project['id']}/lead"
    body = [f'<header class="site"><h1>{escape(title)}</h1>'
            + (f'<p>{escape(intro)}</p>' if intro else "") + "</header>"]
    if facts:
        entries = "".join(f"<li><b>{escape(row['label'])}</b>：{escape(row['value'])}</li>"
                          for row in facts)
        body.append(f'<section class="panel"><h2>品牌信息</h2><ul class="facts">{entries}</ul></section>')
    groups = _grouped(pages, project["market"])
    if groups:
        for name, entries in groups:
            cards = []
            for page in entries:
                href = (f"/p/{page['id']}" if not preview
                        else f"/geo-hub/{project['id']}/p/{page['id']}")
                summary = page["summary"] or _plain(page["body"], 90)
                cards.append(f'<a class="card" href="{escape(href)}" data-page="{page["id"]}">'
                             f'<b>{escape(page["title"])}</b>'
                             + (f'<span>{escape(summary)}</span>' if summary else "") + "</a>")
            body.append(f'<section class="panel"><h2>{escape(name)}</h2>{"".join(cards)}</section>')
    else:
        body.append('<section class="panel"><p class="meta">还没有公开的内容。</p></section>')
    if hub["leads_enabled"]:
        body.append('<section class="panel"><h2>联系我们</h2>'
                    + _form_html(lead_action, thanks) + "</section>")
    canonical = f"https://{hub['domain']}/" if hub["domain_status"] == "active" else ""
    json_ld = {"@context": "https://schema.org", "@type": "Organization",
               "name": title, "description": intro or title,
               "url": canonical or _absolute(base, f"/geo-hub/{project['id']}/")}
    lang = "en" if project["market"] == "global" else "zh-CN"
    return _layout(title, intro or title, canonical, preview, lang, "".join(body), json_ld)


def _page_html(db, project, hub, page, base, preview, thanks):
    facts = _facts(db, project)
    lead_action = "/lead" if not preview else f"/geo-hub/{project['id']}/lead"
    home = "/" if not preview else f"/geo-hub/{project['id']}/"
    body = [f'<div class="crumb"><a href="{escape(home)}">{escape(hub["title"])}</a>'
            f' / {escape(_section(page["section"], project["market"])["name"])}</div>',
            f'<article class="panel"><h1>{escape(page["title"])}</h1>'
            + (f'<p class="meta">{escape(page["summary"])}</p>' if page["summary"] else "")
            + _markdown(page["body"]) + "</article>"]
    if facts:
        entries = "".join(f"<li><b>{escape(row['label'])}</b>：{escape(row['value'])}</li>"
                          for row in facts)
        body.append(f'<section class="panel"><h2>品牌信息</h2><ul class="facts">{entries}</ul></section>')
    if hub["leads_enabled"]:
        body.append('<section class="panel"><h2>联系我们</h2>'
                    + _form_html(lead_action, thanks, page["id"]) + "</section>")
    canonical = (f"https://{hub['domain']}/p/{page['id']}"
                 if hub["domain_status"] == "active" else "")
    lang = "en" if project["market"] == "global" else "zh-CN"
    json_ld = {"@context": "https://schema.org", "@type": "Article",
               "headline": page["title"], "description": page["summary"] or page["title"],
               "datePublished": page["published_at"], "dateModified": page["updated_at"],
               "publisher": {"@type": "Organization", "name": hub["title"]}}
    return _layout(f"{page['title']} - {hub['title']}", page["summary"] or page["title"],
                   canonical, preview, lang, "".join(body), json_ld)


def _plain(markdown, limit):
    value = re.sub(r"[#>*`_\[\]()!]", "", markdown or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _llms(db, project, hub, pages, base, preview):
    lines = [f"# {hub['title']}", ""]
    if hub["intro"]:
        lines += [f"> {hub['intro']}", ""]
    facts = _facts(db, project)
    if facts:
        lines.append("## 品牌信息")
        lines += [f"- {row['label']}: {row['value']}" for row in facts]
        lines.append("")
    if pages:
        lines.append("## 页面")
        for page in pages:
            path = (f"/geo-hub/{project['id']}/p/{page['id']}" if preview
                    else f"/p/{page['id']}")
            url = _absolute(base, path)
            summary = page["summary"] or _plain(page["body"], 120)
            lines.append(f"- [{page['title']}]({url})" + (f": {summary}" if summary else ""))
        lines.append("")
    return "\n".join(lines)


def _robots(preview, domain=""):
    if preview:
        return "User-agent: *\nDisallow: /\n"
    return (f"User-agent: *\nAllow: /\n\nSitemap: https://{domain}/sitemap.xml\n")


def _sitemap(project, pages, domain, base):
    if domain:
        locations = [f"https://{domain}/"] + [f"https://{domain}/p/{page['id']}" for page in pages]
    else:
        locations = [_absolute(base, f"/geo-hub/{project['id']}/")]
        locations += [_absolute(base, f"/geo-hub/{project['id']}/p/{page['id']}") for page in pages]
    entries = "".join(f"<url><loc>{escape(url)}</loc></url>" for url in locations if url)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{entries}</urlset>")


def _lead_allowed(key):
    stamp = time.time()
    hits = [value for value in _LEAD_HITS.get(key, []) if stamp - value < LEAD_WINDOW]
    if len(hits) >= LEAD_LIMIT:
        _LEAD_HITS[key] = hits
        return False
    hits.append(stamp)
    _LEAD_HITS[key] = hits
    return True


def _handle_lead(db, project, hub, body, client_ip):
    try:
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeError:
        return 400, None
    values = {}
    for key in ("name", "phone", "contact", "message", "website", "page_id"):
        values[key] = (fields.get(key) or [""])[0].strip()[:TEXT_LIMITS.get(key, 32)]
    if values["website"]:
        return 303, "/"
    contact = values["contact"]
    wechat = contact if contact and "@" not in contact else ""
    email = contact if "@" in contact else ""
    if not (values["phone"] or wechat or email):
        return 400, None
    if not _lead_allowed((project["id"], client_ip)):
        return 429, None
    note = "来自品牌知识页：" + hub["title"]
    page_id = int(values["page_id"]) if values["page_id"].isdigit() else 0
    if page_id:
        page = db.execute("SELECT title FROM geo_hub_pages WHERE id=? AND tenant_id=? AND project_id=?",
                          (page_id, hub["tenant_id"], project["id"])).fetchone()
        if page:
            note += f" / {page['title']}"
    payload = {
        "name": values["name"], "phone": values["phone"], "wechat": wechat,
        "email": email, "opportunity": values["message"], "notes": note,
        "grade": "B", "stage": "new", "crm_status": "contact",
    }
    with tenancy.scope(hub["tenant_id"], "hub", "品牌知识页", "member"):
        customers.initialize(db)
        try:
            saved = customers.save(db, payload, source="hub_lead")
        except commander.ChatError:
            return 400, None
    if saved.get("created"):
        db.execute(
            "INSERT INTO geo_hub_leads (tenant_id,project_id,page_id,customer_id,created_at) "
            "VALUES (?,?,?,?,?)",
            (hub["tenant_id"], project["id"], page_id, saved["id"], now()),
        )
    return 303, f"/p/{page_id}" if page_id else "/"


def public_response(method, path, query, headers, body, client_ip):
    """Serve public hub pages; returns ``(status, bytes, content_type, headers)``."""
    host = _host(headers.get("Host", ""))
    domain = None
    rest = ""
    if path.startswith(PUBLIC_PREFIX):
        match = re.fullmatch(r"/geo-hub/([0-9]{1,16})(?:/(.*))?", path)
        if not match:
            return _not_found()
        project_id = int(match[1])
        rest = (match.group(2) or "").strip("/")
    else:
        with commander.database() as db:
            initialize(db)
            hub = _hub_for_domain(db, host)
            if hub is None:
                return _not_found()
            project_id = hub["project_id"]
            if path == DOMAIN_VERIFY:
                if not hub["domain_token"]:
                    return _not_found()
                return (200, hub["domain_token"].encode("utf-8"),
                        "text/plain; charset=utf-8", {"X-Robots-Tag": "noindex"})
            domain = host
        rest = path.strip("/")
    domain_route = domain is not None
    if domain_route and rest == "":
        page_match = None
    else:
        page_match = DOMAIN_PAGE.fullmatch("/" + rest) if domain_route else \
            re.fullmatch(r"p/([0-9]{1,16})", rest)
    actions = {"llms.txt", "robots.txt", "sitemap.xml", "lead"}
    if domain_route:
        if rest not in actions and rest != "" and page_match is None:
            return _not_found()
    else:
        if rest not in actions and rest != "" and page_match is None:
            return _not_found()
    if rest == "lead" and method != "POST":
        return 405, b"", "text/plain; charset=utf-8", {}
    if rest != "lead" and method not in {"GET", "HEAD"}:
        return 405, b"", "text/plain; charset=utf-8", {}
    thanks = (query.get("thanks") or [""])[0] == "1"
    with commander.database() as db:
        initialize(db)
        project, hub = _public_context(db, project_id, domain)
        if hub is None:
            return _not_found()
        base = f"https://{hub['domain']}" if domain_route else _base_url(headers)
        pages = _live_pages(db, hub)
        preview = not domain_route
        extra = {"X-Robots-Tag": "noindex, nofollow"} if preview else {}
        if rest == "lead":
            status, location = _handle_lead(db, project, hub, body, client_ip)
            if status == 303:
                prefix = "" if domain_route else f"/geo-hub/{project['id']}"
                target = location.strip("/")
                destination = f"{prefix}/" if not target else (
                    f"{prefix}/p/{target[2:]}" if target.startswith("p/") else f"{prefix}/{target}")
                return (303, b"", "text/plain; charset=utf-8",
                        {"Location": destination + ("?thanks=1" if "?" not in destination else "")})
            message = "没有收到有效的联系方式。" if status == 400 else "提交太频繁，请稍后再试。"
            return _page_error(status, message)
        if rest == "robots.txt":
            return (200, _robots(preview, hub["domain"]).encode("utf-8"),
                    "text/plain; charset=utf-8", {"X-Robots-Tag": "noindex"} if preview else {})
        if rest == "sitemap.xml":
            if preview:
                return _not_found()
            return (200, _sitemap(project, pages, hub["domain"], base).encode("utf-8"),
                    "application/xml; charset=utf-8", extra)
        if rest == "llms.txt":
            if method == "GET":
                _record_view(db, hub, 0, headers)
            return (200, _llms(db, project, hub, pages, base, preview).encode("utf-8"),
                    "text/plain; charset=utf-8", extra)
        if page_match:
            page_id = int(page_match[1])
            page = next((row for row in pages if row["id"] == page_id), None)
            if page is None:
                return _not_found()
            if method == "GET":
                _record_view(db, hub, page_id, headers)
            html = _page_html(db, project, hub, page, base, preview, thanks)
            return (200, html.encode("utf-8"), "text/html; charset=utf-8", extra)
        if method == "GET":
            _record_view(db, hub, 0, headers)
        html = _home_html(db, project, hub, pages, base, preview, thanks)
        return (200, html.encode("utf-8"), "text/html; charset=utf-8", extra)
