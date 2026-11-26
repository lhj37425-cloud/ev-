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
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import PurePosixPath
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urljoin, urlsplit
from uuid import uuid4

import commander
import geo_content
import materials
import tenancy


CHANNEL_CONFIGS = "/api/geo/channel-configs"
CHANNEL_ITEM = re.compile(r"/api/geo/channel-configs/([0-9]{1,16})$")
CHANNEL_TEST = re.compile(r"/api/geo/channel-configs/([0-9]{1,16})/test$")
PUSH = re.compile(r"/api/geo/contents/([0-9]{1,16})/push$")
MAX_BYTES = 500_000
TIMEOUT = 15
PROVIDER_KINDS = {"wordpress", "webhook"}
SECRET_FIELDS = {"app_password", "token"}
MAX_ATTEMPTS = 3
RETRY_BASE = 5
COVER_PATH = re.compile(r"/material-media/([a-f0-9]{32})$")
IMAGE_SUFFIXES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif"}

_push_lock = threading.Lock()
_push_worker_started = False


class ChannelError(commander.ChatError):
    """A delivery failure; ``retryable`` decides whether the queue retries."""

    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = bool(retryable)


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
            {"key": "categories", "label": "默认分类", "required": False, "default": "",
             "hint": "逗号分隔的分类名，不存在会自动创建；可留空"},
            {"key": "tags", "label": "默认标签", "required": False, "default": "",
             "hint": "逗号分隔的标签名，不存在会自动创建；可留空"},
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
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_push_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
        publication_id INTEGER NOT NULL, content_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL, cover_url TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_at TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS geo_push_jobs_pending ON geo_push_jobs (status, next_at)")


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
        raise ChannelError("接口地址要以 http(s) 开头。", retryable=False)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ChannelError("接口地址无效。", retryable=False)
    if parsed.username or parsed.password:
        raise ChannelError("接口地址无效。", retryable=False)
    if not _allow_private():
        host = parsed.hostname.lower().strip(".")
        if host in {"localhost"} or host.endswith((".local", ".internal")):
            raise ChannelError("接口地址指向内网或本机。", retryable=False)
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                infos = socket.getaddrinfo(host, None)
            except OSError as error:
                raise ChannelError("接口域名解析失败。", retryable=False) from error
            for info in infos:
                try:
                    address = ipaddress.ip_address(info[4][0])
                except ValueError:
                    continue
                if (address.is_private or address.is_loopback or address.is_link_local
                        or address.is_reserved):
                    raise ChannelError("接口地址指向内网或本机。", retryable=False)
        else:
            if (address.is_private or address.is_loopback or address.is_link_local
                    or address.is_reserved):
                raise ChannelError("接口地址指向内网或本机。", retryable=False)
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _request(method, url, payload=None, headers=None, body=None, content_type=None):
    url = _endpoint(url)
    if body is None and payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        content_type = "application/json; charset=utf-8"
    request_headers = {
        "User-Agent": "EVCommander-Publish/1.0",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    if body is not None:
        request_headers["Content-Type"] = content_type or "application/octet-stream"
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
            raise ChannelError("连接失败，请检查接口地址或稍后重试。", retryable=True) from error
    raise ChannelError("接口跳转次数过多。", retryable=True)


def _json_body(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_value(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _http_error(status, text, prefix):
    message = "%s返回 HTTP %s：%s" % (prefix, status, str(text)[:160])
    return ChannelError(message, retryable=status >= 500 or status in {408, 429})


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


# ---------------------------------------------------------------------------
# Delivery queue


def _sync_push():
    return os.environ.get("EV_CHANNEL_SYNC_PUSH") == "1"


def _push_wait():
    try:
        return max(0.0, min(15.0, float(os.environ.get("EV_CHANNEL_PUSH_WAIT", "3"))))
    except ValueError:
        return 3.0


def _ensure_push_worker():
    global _push_worker_started
    with _push_lock:
        if _push_worker_started:
            return
        _push_worker_started = True
    threading.Thread(target=_push_worker, daemon=True).start()


def recover_push_jobs():
    """Put jobs left 'sending' back into the queue after a restart."""
    with commander.database() as db:
        initialize(db)
        db.execute("UPDATE geo_push_jobs SET status='pending' WHERE status='sending'")
        pending = db.execute(
            "SELECT 1 FROM geo_push_jobs WHERE status='pending' LIMIT 1").fetchone()
    if pending is not None:
        _ensure_push_worker()


def _claim_push_job():
    with commander.database() as db:
        initialize(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM geo_push_jobs WHERE status='pending' "
            "AND (next_at='' OR next_at<=?) ORDER BY id LIMIT 1", (now(),)).fetchone()
        if row is None:
            return None
        changed = db.execute(
            "UPDATE geo_push_jobs SET status='sending',updated_at=? WHERE id=? AND status='pending'",
            (now(), row["id"]))
        return row if changed.rowcount == 1 else None


def _push_result(publication_id, wait):
    deadline = time.monotonic() + wait
    row = None
    while True:
        with commander.database() as db:
            initialize(db)
            geo_content.initialize(db)
            row = db.execute("SELECT * FROM geo_publications WHERE id=? AND tenant_id=?",
                             (publication_id, commander.tenant())).fetchone()
        if row is None:
            raise commander.ChatError("发布记录不存在。")
        if row["status"] not in {"queued", "running"} or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    if row["status"] == "published":
        return {"ok": True, "ok_push": True, "queued": False,
                "message": "已推送并发布：" + (row["url"] or ""),
                "publication_id": row["id"], "url": row["url"]}
    if row["status"] == "draft_saved":
        return {"ok": True, "ok_push": True, "queued": False,
                "message": "已推送并存为草稿，请到对方站点确认后发布。",
                "publication_id": row["id"], "url": row["url"]}
    if row["status"] == "failed":
        return {"ok": True, "ok_push": False, "queued": False,
                "message": row["error"] or "推送失败。", "publication_id": row["id"]}
    return {"ok": True, "ok_push": True, "queued": True,
            "message": "已加入推送队列，稍后自动发布；失败会自动重试。",
            "publication_id": row["id"]}


def _finish_push_failure(job, message, attempts=None):
    message = str(message)[:300]
    attempts = int(job["attempts"]) + 1 if attempts is None else attempts
    stamp = now()
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        db.execute("UPDATE geo_push_jobs SET status='failed',attempts=?,last_error=?,"
                   "updated_at=? WHERE id=?", (attempts, message, stamp, job["id"]))
        db.execute("UPDATE geo_publications SET status='failed',error=?,updated_at=? "
                   "WHERE id=? AND tenant_id=?",
                   (message, stamp, job["publication_id"], job["tenant_id"]))
        db.execute("UPDATE geo_channel_configs SET last_error=?,updated_at=? "
                   "WHERE id=? AND tenant_id=?",
                   (message, stamp, job["channel_id"], job["tenant_id"]))


def _retry_push_job(job, message):
    """Put a retryable failure back into the queue with backoff."""
    attempts = int(job["attempts"]) + 1
    message = str(message)[:300]
    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=RETRY_BASE * attempts)).isoformat(
        timespec="seconds")
    stamp = now()
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        db.execute("UPDATE geo_push_jobs SET status='pending',attempts=?,next_at=?,last_error=?,"
                   "updated_at=? WHERE id=?", (attempts, retry_at, message, stamp, job["id"]))
        db.execute("UPDATE geo_publications SET status='queued',error=?,updated_at=? "
                   "WHERE id=? AND tenant_id=?",
                   (message, stamp, job["publication_id"], job["tenant_id"]))
        db.execute("UPDATE geo_channel_configs SET last_error=?,updated_at=? "
                   "WHERE id=? AND tenant_id=?",
                   (message, stamp, job["channel_id"], job["tenant_id"]))
    _ensure_push_worker()


def _process_push_job(job):
    tenant = job["tenant_id"]
    with tenancy.scope(tenant, user_id="geo-push", username="GEO 推送", role="member"):
        with commander.database() as db:
            initialize(db)
            geo_content.initialize(db)
            publication = db.execute(
                "SELECT * FROM geo_publications WHERE id=? AND tenant_id=?",
                (job["publication_id"], tenant)).fetchone()
            content = db.execute("SELECT * FROM geo_contents WHERE id=? AND tenant_id=?",
                                 (job["content_id"], tenant)).fetchone()
            channel = db.execute("SELECT * FROM geo_channel_configs WHERE id=? AND tenant_id=?",
                                 (job["channel_id"], tenant)).fetchone()
        if publication is None:
            _finish_push_failure(job, "发布记录已被删除。")
            return
        if publication["status"] == "offline":
            with commander.database() as db:
                db.execute("UPDATE geo_push_jobs SET status='cancelled',updated_at=? "
                           "WHERE id=?", (now(), job["id"]))
            return
        if channel is None:
            _finish_push_failure(job, "接入设置已删除，无法推送。")
            return
        if not channel["status"]:
            _finish_push_failure(job, "这个接入已停用。")
            return
        if content is None:
            _finish_push_failure(job, "内容已不存在。")
            return
        if not geo_content._can_publish(content):
            _finish_push_failure(job, "这篇内容已发布、被修改或未通过质检，不能自动推送。")
            return
        stamp = now()
        with commander.database() as db:
            db.execute("BEGIN IMMEDIATE")
            initialize(db)
            db.execute("UPDATE geo_publications SET status='running',updated_at=? "
                       "WHERE id=? AND tenant_id=?", (stamp, publication["id"], tenant))
        try:
            result = _deliver(content, channel, job["cover_url"])
        except ChannelError as error:
            attempts = int(job["attempts"]) + 1
            if error.retryable and attempts < MAX_ATTEMPTS:
                _retry_push_job(job, error)
            else:
                _finish_push_failure(job, error, attempts=attempts)
            return
        except Exception:  # defensive: never leave a job stuck
            _finish_push_failure(job, "推送异常，请稍后重试。")
            return
        stamp = now()
        with commander.database() as db:
            db.execute("BEGIN IMMEDIATE")
            initialize(db)
            geo_content.initialize(db)
            db.execute("UPDATE geo_publications SET status=?,url=?,remote_id=?,error='',"
                       "updated_at=? WHERE id=? AND tenant_id=?",
                       (result["status"], result["url"], result["remote_id"],
                        stamp, publication["id"], tenant))
            if result["status"] == "published":
                db.execute("UPDATE geo_contents SET status='published',updated_at=? "
                           "WHERE id=? AND tenant_id=?",
                           (stamp, content["id"], tenant))
            db.execute("UPDATE geo_push_jobs SET status='sent',attempts=attempts+1,"
                       "last_error='',updated_at=? WHERE id=?", (stamp, job["id"]))
            db.execute("UPDATE geo_channel_configs SET last_error='',updated_at=? "
                       "WHERE id=? AND tenant_id=?", (stamp, channel["id"], tenant))
            tenancy.audit_in(db, tenant, "geo.content.push", str(content["id"]),
                             f"channel={channel['id']} status={result['status']}")


def flush_push_jobs(limit=100):
    processed = 0
    while processed < limit:
        job = _claim_push_job()
        if job is None:
            break
        _process_push_job(job)
        processed += 1
    return processed


def _push_worker():
    while True:
        try:
            job = _claim_push_job()
        except Exception:
            time.sleep(1)
            continue
        if job is None:
            time.sleep(0.5)
            continue
        try:
            _process_push_job(job)
        except Exception:
            try:
                with commander.database() as db:
                    db.execute("UPDATE geo_push_jobs SET status='pending' "
                               "WHERE id=? AND status='sending'", (job["id"],))
            except Exception:
                pass
            time.sleep(1)


# ---------------------------------------------------------------------------
# Providers


def _cover_key(value):
    value = _text(value, 1000)
    if not value:
        return ""
    parsed = urlsplit(value)
    path = parsed.path if parsed.scheme in {"http", "https"} else value
    match = COVER_PATH.fullmatch(path)
    if not match:
        raise ChannelError("封面图无效，请通过上传按钮重新上传。", retryable=False)
    return match.group(1)


def _term_names(value):
    value = _text(value, 500)
    if not value:
        return []
    names, seen = [], set()
    for part in re.split(r"[,，]", value):
        name = part.strip()[:50]
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= 10:
            break
    return names


def _wp_terms(base, auth, kind, value):
    names = _term_names(value)
    if not names:
        return []
    label = "分类" if kind == "categories" else "标签"
    ids = []
    for name in names:
        status, text, _ = _request(
            "GET", "%s/wp-json/wp/v2/%s?search=%s&per_page=100" % (base, kind, quote(name)),
            headers=auth)
        if status != 200:
            raise _http_error(status, text, "WordPress 查询" + label)
        items = _json_value(text)
        if not isinstance(items, list):
            raise ChannelError("WordPress 返回的%s数据无效。" % label, retryable=False)
        found = next((item for item in items
                      if isinstance(item, dict)
                      and str(item.get("name", "")).strip().lower() == name.lower()), None)
        if found and found.get("id"):
            ids.append(int(found["id"]))
            continue
        status, text, _ = _request("POST", "%s/wp-json/wp/v2/%s" % (base, kind),
                                   {"name": name}, headers=auth)
        data = _json_body(text)
        if status not in {200, 201} or not data.get("id"):
            raise _http_error(status, text, "WordPress 创建" + label)
        ids.append(int(data["id"]))
    return ids


def _wp_upload_cover(base, auth, cover_url):
    """Upload a material-library image to WordPress and return its media id."""
    key = _cover_key(cover_url)
    with commander.database() as db:
        materials.initialize(db)
        row = db.execute(
            "SELECT name,media_type,mime_type FROM material_files "
            "WHERE file_key=? AND tenant_id=?", (key, commander.tenant())).fetchone()
    if row is None or row["media_type"] != "image":
        raise ChannelError("封面图不存在或不是图片，请重新上传。", retryable=False)
    path = materials.uploaded_path(key)
    if path is None:
        raise ChannelError("封面图不存在，请重新上传。", retryable=False)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ChannelError("封面图读取失败，请重新上传。", retryable=False) from error
    suffix = PurePosixPath(row["name"] or "").suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        suffix = ".png"
    filename = "cover" + suffix
    mime = row["mime_type"] or IMAGE_SUFFIXES[suffix]
    boundary = "----EVCommander" + uuid4().hex
    body = (
        ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
         "Content-Type: %s\r\n\r\n" % (boundary, filename, mime)).encode("utf-8")
        + raw
        + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    )
    headers = {**auth,
               "Content-Disposition": 'attachment; filename="%s"' % filename}
    status, text, _ = _request("POST", base + "/wp-json/wp/v2/media", headers=headers,
                               body=body,
                               content_type="multipart/form-data; boundary=" + boundary)
    data = _json_body(text)
    if status not in {200, 201} or not data.get("id"):
        raise _http_error(status, text, "WordPress 封面上传")
    return int(data["id"])


def _deliver_wordpress(title, content, config, cover_url):
    base = _wordpress_base(config)
    target_status = config.get("status") if config.get("status") in {"publish", "draft"} \
        else "publish"
    auth = _auth(config.get("username", ""), config.get("app_password", ""))
    payload = {"title": title, "content": content["body"],
               "excerpt": content["summary"], "status": target_status}
    if cover_url:
        payload["featured_media"] = _wp_upload_cover(base, auth, cover_url)
    categories = _wp_terms(base, auth, "categories", config.get("categories", ""))
    if categories:
        payload["categories"] = categories
    tags = _wp_terms(base, auth, "tags", config.get("tags", ""))
    if tags:
        payload["tags"] = tags
    status, text, _ = _request("POST", base + "/wp-json/wp/v2/posts", payload, auth)
    data = _json_body(text)
    if status not in {200, 201} or not data.get("id"):
        raise _http_error(status, text, "WordPress ")
    return {"status": "published" if target_status == "publish" else "draft_saved",
            "url": data.get("link") or "", "remote_id": str(data.get("id") or "")}


def _deliver_webhook(title, content, config, cover_url):
    headers = {}
    if config.get("token"):
        headers["Authorization"] = "Bearer " + config["token"]
    payload = {"title": title, "content": content["body"],
               "summary": content["summary"], "external_id": content["id"]}
    if cover_url:
        payload["cover_url"] = cover_url
    status, text, _ = _request("POST", config.get("url", ""), payload, headers)
    data = _json_body(text)
    if not 200 <= status < 300:
        raise _http_error(status, text, "网站接口")
    url = data.get("url") or data.get("link") or ""
    return {"status": "published" if url else "draft_saved",
            "url": url, "remote_id": str(data.get("id") or "")}


def _deliver(content, channel, cover_url):
    kind = channel["kind"]
    config = _json(channel["config"], {})
    title = content["title"] or content["question"] or "未命名内容"
    if kind == "wordpress":
        return _deliver_wordpress(title, content, config, cover_url)
    if kind == "webhook":
        return _deliver_webhook(title, content, config, cover_url)
    raise ChannelError("这个接入类型暂不支持自动推送。", retryable=False)


# ---------------------------------------------------------------------------
# Push entry


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
        cover_url = _text(body.get("cover_url"), 1000)
        if cover_url:
            _cover_key(cover_url)
        existing = db.execute(
            "SELECT p.* FROM geo_publications p JOIN geo_push_jobs j "
            "ON j.publication_id=p.id WHERE p.content_id=? AND p.channel_id=? "
            "AND p.tenant_id=? AND j.status IN ('pending','sending') "
            "ORDER BY p.id DESC LIMIT 1",
            (content["id"], channel_id, commander.tenant())).fetchone()
        if existing is not None:
            return {"ok": True, "ok_push": True, "queued": True,
                    "message": "这篇内容已经在这条渠道的推送队列里。",
                    "publication_id": existing["id"]}
        stamp = now()
        title = content["title"] or content["question"] or "未命名内容"
        publication_id = db.execute(
            "INSERT INTO geo_publications (tenant_id,project_id,content_id,ticket_id,"
            "channel_kind,platform,platform_name,title,status,url,error,channel_id,remote_id,"
            "created_at,updated_at) "
            "VALUES (?,?,?,0,'channel',?,?,?,'queued','','',?,'',?,?)",
            (commander.tenant(), content["project_id"], content["id"], channel["kind"],
             channel["name"], title, channel_id, stamp, stamp),
        ).lastrowid
        db.execute(
            "INSERT INTO geo_push_jobs (tenant_id,publication_id,content_id,channel_id,"
            "cover_url,status,attempts,next_at,last_error,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'pending',0,'','',?,?)",
            (commander.tenant(), publication_id, content["id"], channel_id, cover_url,
             stamp, stamp),
        )
        tenancy.audit_in(db, commander.tenant(), "geo.content.push", str(content["id"]),
                         f"channel={channel_id} queued")
    if _sync_push():
        flush_push_jobs()
        return _push_result(publication_id, 0)
    _ensure_push_worker()
    return _push_result(publication_id, _push_wait())


def offline_publication(publication_id):
    """Take one pushed article offline (WordPress: move to trash) and mark it."""
    with commander.database() as db:
        initialize(db)
        geo_content.initialize(db)
        row = db.execute("SELECT * FROM geo_publications WHERE id=? AND tenant_id=?",
                         (int(publication_id), commander.tenant())).fetchone()
        if row is None:
            raise commander.ChatError("发布记录不存在。")
        if row["channel_kind"] != "channel":
            raise commander.ChatError("只有自动推送的记录可以在这里下线。")
        if row["status"] in {"queued", "running"}:
            cancelled = db.execute(
                "UPDATE geo_push_jobs SET status='cancelled',updated_at=? "
                "WHERE publication_id=? AND status='pending'", (now(), row["id"]))
            if cancelled.rowcount != 1:
                raise commander.ChatError("这条记录正在推送，请等推送结束后再下线。")
            row = db.execute("SELECT * FROM geo_publications WHERE id=? AND tenant_id=?",
                             (row["id"], commander.tenant())).fetchone()
        channel = None
        if row["channel_id"]:
            channel = db.execute("SELECT * FROM geo_channel_configs WHERE id=? AND tenant_id=?",
                                 (row["channel_id"], commander.tenant())).fetchone()
    remote = False
    message = "已把这条发布记录标记为下线。"
    if row["status"] in {"published", "draft_saved"} and row["remote_id"]:
        if channel is None:
            message = "接入设置已删除，已把记录标记为下线；请在对方站点手动删除文章。"
        elif channel["kind"] == "wordpress":
            config = _json(channel["config"], {})
            base = _wordpress_base(config)
            auth = _auth(config.get("username", ""), config.get("app_password", ""))
            status, text, _ = _request(
                "DELETE", base + "/wp-json/wp/v2/posts/" + quote(row["remote_id"]), headers=auth)
            if status not in {200, 201, 404}:
                raise _http_error(status, text, "WordPress 下线")
            remote = status in {200, 201}
            message = ("已从 WordPress 下线（移到回收站）。" if remote
                       else "远端文章已不存在，记录已标记为下线。")
        else:
            message = "该渠道不支持下线，已在本地标记为已下线。"
    stamp = now()
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        geo_content.initialize(db)
        db.execute("UPDATE geo_publications SET status='offline',error='',updated_at=? "
                   "WHERE id=? AND tenant_id=?", (stamp, row["id"], commander.tenant()))
        active = db.execute(
            "SELECT COUNT(*) FROM geo_publications WHERE content_id=? AND tenant_id=? "
            "AND id<>? AND status IN ('published','draft_saved','queued','running')",
            (row["content_id"], commander.tenant(), row["id"])).fetchone()[0]
        if not active:
            db.execute("UPDATE geo_contents SET status='ready',updated_at=? "
                       "WHERE id=? AND tenant_id=? AND status='published'",
                       (stamp, row["content_id"], commander.tenant()))
        tenancy.audit_in(db, commander.tenant(), "geo.publication.offline", str(row["id"]),
                         f"channel={row['channel_id']} remote={remote}")
        updated = db.execute("SELECT * FROM geo_publications WHERE id=? AND tenant_id=?",
                             (row["id"], commander.tenant())).fetchone()
        return {"ok": True, "remote": remote, "message": message,
                "publication": geo_content._publication(updated)}


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
