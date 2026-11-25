"""GEO publishing channels: push checked content to the customer's own site.

The archived Publish page can push a fact-checked article straight to a
channel configured under "接入设置". This module implements two providers
that need no platform partnership, only the customer's own site credentials:

* ``wordpress`` — WordPress REST API (``/wp-json/wp/v2/posts``) with an
  Application Password (HTTP Basic auth). Can publish directly or save a draft.
* ``webhook`` — a generic JSON webhook: POST ``{title, content, summary}`` to a
  customer-provided URL, optional Bearer token; the response may include
  ``url``/``link``.

微信公众号/服务号 and other platforms still need their own credentials and
review process, so they stay explicitly closed. Secrets are stored in SQLite
(like WhatsApp credentials) and never returned to the frontend; only a blank
field is shown when editing, and saving a blank secret keeps the old one.
"""

from base64 import b64encode
from datetime import datetime, timezone
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit, urlunsplit

import commander
import geo_content
import tenancy


CHANNEL_CONFIGS = "/api/geo/channel-configs"
CHANNEL_ITEM = re.compile(r"/api/geo/channel-configs/([0-9]{1,16})$")
CHANNEL_TEST = re.compile(r"/api/geo/channel-configs/([0-9]{1,16})/test$")
PUSH = re.compile(r"/api/geo/contents/([0-9]{1,16})/push$")
MAX_BYTES = 500_000
TIMEOUT = 15
PROVIDER_KINDS = {"wordpress", "webhook"}
SECRET_FIELDS = {"app_password", "token"}


def providers(market="cn"):
    return [
        {"kind": "wordpress", "name": "WordPress 官网", "fields": [
            {"key": "base_url", "label": "WordPress 站点地址", "required": True,
             "hint": "例如 https://www.example.com，不要带 /wp-json", "default": ""},
            {"key": "username", "label": "用户名", "required": True,
             "hint": "WordPress 后台账号", "default": ""},
            {"key": "app_password", "label": "应用密码", "required": True,
             "secret": True, "hint": "WordPress 后台 → 用户 → 应用程序密码", "default": ""},
            {"key": "status", "label": "发布状态", "required": False,
             "options": [{"value": "publish", "label": "直接发布"},
                         {"value": "draft", "label": "先存草稿"}],
             "default": "publish", "hint": "草稿需要你到 WordPress 后台确认后发布"},
        ]},
        {"kind": "webhook", "name": "自建官网（Webhook）", "fields": [
            {"key": "url", "label": "接收地址", "required": True,
             "hint": "我们会 POST JSON：title / content / summary", "default": ""},
            {"key": "token", "label": "访问令牌", "required": False, "secret": True,
             "hint": "可留空；会放在 Authorization: Bearer 里", "default": ""},
        ]},
    ]


def _provider(kind):
    for item in providers():
        if item["kind"] == kind:
            return item
    return None


def canonical(path):
    if path.startswith("/agent-api"):
        path = path[len("/agent-api"):]
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def can_read(path):
    return canonical(path) == CHANNEL_CONFIGS


def can_write(path):
    path = canonical(path)
    if path == CHANNEL_CONFIGS or CHANNEL_ITEM.fullmatch(path) \
            or CHANNEL_TEST.fullmatch(path) or PUSH.fullmatch(path):
        return True
    return False


def empty_post(path):
    return bool(CHANNEL_TEST.fullmatch(canonical(path)))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_channel_configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
        kind TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
        config TEXT NOT NULL DEFAULT '{}', status INTEGER NOT NULL DEFAULT 1,
        last_test TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")


def _json(raw, default):
    try:
        value = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _text(value, limit=500):
    return str(value).strip()[:limit] if isinstance(value, str) else ""


def _channel_json(row):
    provider = _provider(row["kind"]) or {"fields": []}
    config = _json(row["config"], {})
    visible = {}
    for field in provider["fields"]:
        key = field["key"]
        value = config.get(key, "")
        if key in SECRET_FIELDS:
            value = ""
        visible[key] = value
    return {
        "id": row["id"], "kind": row["kind"], "name": row["name"],
        "kind_name": next((item["name"] for item in providers()
                           if item["kind"] == row["kind"]), row["kind"]),
        "config": visible, "status": int(row["status"]),
        "last_test": row["last_test"], "last_error": row["last_error"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _channel(db, channel_id):
    row = db.execute("SELECT * FROM geo_channel_configs WHERE id=? AND tenant_id=?",
                     (int(channel_id), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("接入设置不存在。")
    return row


def _clean_config(provider, body, previous=None):
    previous = previous or {}
    incoming = body.get("config") if isinstance(body.get("config"), dict) else {}
    config = {}
    for field in provider["fields"]:
        key = field["key"]
        raw = incoming.get(key, previous.get(key, field.get("default", "")))
        value = _text(raw, 500)
        if key in SECRET_FIELDS and not value:
            value = previous.get(key, "")
        if field.get("required") and not value:
            raise commander.ChatError(f"{field['label']}不能为空。")
        options = field.get("options")
        if options and value and value not in {item["value"] for item in options}:
            raise commander.ChatError(f"{field['label']}选项无效。")
        config[key] = value
    return config


def _add(db, body):
    kind = _text(body.get("kind"), 20)
    provider = _provider(kind)
    if provider is None:
        raise commander.ChatError("不支持的接入类型。")
    name = _text(body.get("name"), 64)
    if len(name) < 2:
        raise commander.ChatError("接入名称至少填 2 个字。")
    config = _clean_config(provider, body)
    stamp = now()
    channel_id = db.execute(
        "INSERT INTO geo_channel_configs (tenant_id,kind,name,config,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (commander.tenant(), kind, name, json.dumps(config, ensure_ascii=False), 1, stamp, stamp),
    ).lastrowid
    tenancy.audit_in(db, commander.tenant(), "geo.channel.add", str(channel_id), kind)
    return {"ok": True, "channel": _channel_json(_channel(db, channel_id))}


def _update(db, channel_id, body):
    row = _channel(db, channel_id)
    provider = _provider(row["kind"])
    if provider is None:
        raise commander.ChatError("接入类型无效。")
    name = _text(body.get("name", row["name"]), 64)
    if len(name) < 2:
        raise commander.ChatError("接入名称至少填 2 个字。")
    previous = _json(row["config"], {})
    if "config" in body:
        config = _clean_config(provider, body, previous)
    else:
        config = previous
    status = body.get("status", row["status"])
    if isinstance(status, bool):
        status = 1 if status else 0
    try:
        status = 1 if int(status) else 0
    except (TypeError, ValueError) as error:
        raise commander.ChatError("状态无效。") from error
    db.execute(
        "UPDATE geo_channel_configs SET name=?,config=?,status=?,updated_at=? "
        "WHERE id=? AND tenant_id=?",
        (name, json.dumps(config, ensure_ascii=False), status, now(),
         row["id"], commander.tenant()),
    )
    tenancy.audit_in(db, commander.tenant(), "geo.channel.update", str(row["id"]),
                     f"status={status}")
    return {"ok": True, "channel": _channel_json(_channel(db, row["id"]))}


def _delete(db, channel_id):
    row = _channel(db, channel_id)
    db.execute("DELETE FROM geo_channel_configs WHERE id=? AND tenant_id=?",
               (row["id"], commander.tenant()))
    tenancy.audit_in(db, commander.tenant(), "geo.channel.delete", str(row["id"]), row["kind"])
    return {"ok": True}


def _allow_private():
    return os.environ.get("EV_CHANNEL_ALLOW_PRIVATE", "").strip() in {"1", "true", "yes"}


def _endpoint(value):
    value = _text(value, 500)
    if "://" not in value:
        raise commander.ChatError("接口地址要以 http(s) 开头。")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise commander.ChatError("接口地址无效。")
    if parsed.username or parsed.password:
        raise commander.ChatError("接口地址无效。")
    if not _allow_private():
        host = parsed.hostname.lower().strip(".")
        if host in {"localhost"} or host.endswith((".local", ".internal")):
            raise commander.ChatError("接口地址指向内网或本机。")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                infos = socket.getaddrinfo(host, None)
            except OSError as error:
                raise commander.ChatError("接口域名解析失败。") from error
            for info in infos:
                try:
                    address = ipaddress.ip_address(info[4][0])
                except ValueError:
                    continue
                if (address.is_private or address.is_loopback or address.is_link_local
                        or address.is_reserved):
                    raise commander.ChatError("接口地址指向内网或本机。")
        else:
            if (address.is_private or address.is_loopback or address.is_link_local
                    or address.is_reserved):
                raise commander.ChatError("接口地址指向内网或本机。")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _request(method, url, payload=None, headers=None):
    url = _endpoint(url)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request_headers = {
        "User-Agent": "EVCommander-Publish/1.0",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    request_headers.update(headers or {})
    opener = urllib.request.build_opener(_NoRedirect())
    current = url
    for _ in range(4):
        _endpoint(current)
        request = urllib.request.Request(current, data=body, headers=request_headers, method=method)
        try:
            response = opener.open(request, timeout=TIMEOUT)
            raw = response.read(MAX_BYTES)
            return response.status, raw.decode("utf-8", "replace"), response.geturl()
        except urllib.error.HTTPError as error:
            if error.code in {301, 302, 303, 307, 308}:
                location = error.headers.get("Location", "")
                if not location:
                    break
                current = urljoin(current, location)
                continue
            raw = error.read(MAX_BYTES) if error.fp else b""
            return error.code, raw.decode("utf-8", "replace"), current
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise commander.ChatError("连接失败，请检查接口地址或稍后重试。") from error
    raise commander.ChatError("接口跳转次数过多。")


def _json_body(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _auth(username, password):
    token = b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": "Basic " + token}


def _wordpress_base(config):
    base = config.get("base_url", "").rstrip("/")
    if base.endswith("/wp-json/wp/v2"):
        base = base[: -len("/wp-json/wp/v2")]
    return base


def _test_channel(channel_id):
    with commander.database() as db:
        initialize(db)
        row = _channel(db, channel_id)
        kind, config = row["kind"], _json(row["config"], {})
    ok, message = False, "接入类型无效。"
    try:
        if kind == "wordpress":
            base = _wordpress_base(config)
            status, text, _ = _request(
                "GET", base + "/wp-json/wp/v2/users/me",
                headers=_auth(config.get("username", ""), config.get("app_password", "")))
            data = _json_body(text)
            if status == 200 and data.get("id"):
                ok, message = True, "WordPress 连接成功。"
            elif status in {401, 403}:
                message = "WordPress 账号或应用密码不对（HTTP %s）。" % status
            else:
                message = "WordPress 返回 HTTP %s：%s" % (
                    status, str(data.get("message") or text)[:120])
        elif kind == "webhook":
            headers = {}
            if config.get("token"):
                headers["Authorization"] = "Bearer " + config["token"]
            status, text, _ = _request("POST", config.get("url", ""),
                                       {"event": "test", "source": "evcommander"}, headers)
            ok = 200 <= status < 300
            message = ("Webhook 连接成功。" if ok
                       else "Webhook 返回 HTTP %s：%s" % (status, text[:120]))
    except commander.ChatError as error:
        message = str(error)
    stamp = now()
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        db.execute(
            "UPDATE geo_channel_configs SET last_test=?,last_error=?,updated_at=? "
            "WHERE id=? AND tenant_id=?",
            (stamp, "" if ok else message, stamp, channel_id, commander.tenant()),
        )
        channel = _channel_json(_channel(db, channel_id))
    return {"ok": True, "ok_test": ok, "message": message, "channel": channel}


def _publication(db, content, channel, status, url):
    stamp = now()
    title = content["title"] or content["question"] or "未命名内容"
    publication_id = db.execute(
        "INSERT INTO geo_publications (tenant_id,project_id,content_id,ticket_id,"
        "channel_kind,platform,platform_name,title,status,url,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (commander.tenant(), content["project_id"], content["id"], 0, "channel",
         channel["kind"], channel["name"], title,
         status if status in {"published", "draft_saved"} else "failed",
         url, stamp, stamp),
    ).lastrowid
    if status == "published":
        db.execute("UPDATE geo_contents SET status='published',updated_at=? "
                   "WHERE id=? AND tenant_id=?",
                   (stamp, content["id"], commander.tenant()))
    return publication_id


def _push_content(content_id, body):
    with commander.database() as db:
        initialize(db)
        geo_content.initialize(db)
        content = db.execute(
            "SELECT * FROM geo_contents WHERE id=? AND tenant_id=?",
            (int(content_id), commander.tenant()),
        ).fetchone()
        if content is None:
            raise commander.ChatError("内容不存在。")
        if not geo_content._can_publish(content):
            raise commander.ChatError("这篇内容还没通过质检或已经发布，不能自动推送。")
        try:
            channel_id = int(body.get("channel_id") or 0)
        except (TypeError, ValueError) as error:
            raise commander.ChatError("请选择要推送的接入。") from error
        if not channel_id:
            raise commander.ChatError("请选择要推送的接入。")
        channel = _channel(db, channel_id)
        if not channel["status"]:
            raise commander.ChatError("这个接入已停用。")
        kind = channel["kind"]
        config = _json(channel["config"], {})
        channel_name = channel["name"]
    title = content["title"] or content["question"] or "未命名内容"
    try:
        if kind == "wordpress":
            base = _wordpress_base(config)
            target_status = config.get("status") if config.get("status") in {"publish", "draft"} \
                else "publish"
            payload = {
                "title": title, "content": content["body"],
                "excerpt": content["summary"], "status": target_status,
            }
            status, text, _ = _request(
                "POST", base + "/wp-json/wp/v2/posts", payload,
                headers=_auth(config.get("username", ""), config.get("app_password", "")))
            data = _json_body(text)
            if status not in {200, 201} or not data.get("id"):
                raise commander.ChatError(
                    "WordPress 返回 HTTP %s：%s" % (
                        status, str(data.get("message") or text)[:160]))
            url = data.get("link") or ""
            publication_status = "published" if target_status == "publish" else "draft_saved"
            message = ("已推送到 WordPress：" + url) if publication_status == "published" \
                else "已存为 WordPress 草稿，请到后台确认后发布。"
        elif kind == "webhook":
            headers = {}
            if config.get("token"):
                headers["Authorization"] = "Bearer " + config["token"]
            payload = {"title": title, "content": content["body"],
                       "summary": content["summary"], "external_id": content["id"]}
            status, text, _ = _request("POST", config.get("url", ""), payload, headers)
            data = _json_body(text)
            if not 200 <= status < 300:
                raise commander.ChatError(
                    "网站接口返回 HTTP %s：%s" % (status, str(data.get("message") or text)[:160]))
            url = data.get("url") or data.get("link") or ""
            publication_status = "published" if url else "draft_saved"
            message = ("已推送到 " + channel_name) if url \
                else "已推送，但对方没有返回文章地址，先记为草稿。"
        else:
            raise commander.ChatError("这个接入类型暂不支持自动推送。")
    except commander.ChatError as error:
        with commander.database() as db:
            db.execute("BEGIN IMMEDIATE")
            initialize(db)
            db.execute("UPDATE geo_channel_configs SET last_error=?,updated_at=? "
                       "WHERE id=? AND tenant_id=?",
                       (str(error)[:300], now(), channel_id, commander.tenant()))
        return {"ok": True, "ok_push": False, "message": str(error)}
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        geo_content.initialize(db)
        channel = _channel(db, channel_id)
        publication_id = _publication(db, content, channel, publication_status, url)
        tenancy.audit_in(db, commander.tenant(), "geo.content.push", str(content["id"]),
                         f"channel={channel_id} status={publication_status}")
    return {"ok": True, "ok_push": True, "message": message,
            "publication_id": publication_id}


# ---------------------------------------------------------------------------
# Entry points


def read(path, query):
    path = canonical(path)
    if path != CHANNEL_CONFIGS:
        raise commander.ChatError("GEO 接口暂不可用。")
    with commander.database() as db:
        initialize(db)
        kind = (query.get("kind") or [""])[0].strip()
        where, values = "WHERE tenant_id=?", [commander.tenant()]
        if kind:
            where += " AND kind=?"
            values.append(kind)
        rows = db.execute(f"SELECT * FROM geo_channel_configs {where} ORDER BY id",
                          values).fetchall()
        return {"ok": True, "items": [_channel_json(row) for row in rows]}


def write(method, path, body):
    path = canonical(path)
    match = CHANNEL_TEST.fullmatch(path)
    if match and method == "POST":
        return _test_channel(int(match[1]))
    match = PUSH.fullmatch(path)
    if match and method == "POST":
        return _push_content(int(match[1]), body)
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        if path == CHANNEL_CONFIGS and method == "POST":
            return _add(db, body)
        match = CHANNEL_ITEM.fullmatch(path)
        if match and method == "PUT":
            return _update(db, int(match[1]), body)
        if match and method == "DELETE":
            return _delete(db, int(match[1]))
    raise commander.ChatError("GEO 接口暂不可用。")
