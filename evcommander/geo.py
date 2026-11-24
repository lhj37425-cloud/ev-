"""GEO project core for the archived AI Search Optimization views.

Implements the project/meta contract the frontend needs to leave its loading
state, plus empty read models for the monitoring/report subpages. External AI
search data sources will be added behind the same service later.
"""

from datetime import datetime, timezone
import json
import re

import commander
import geo_brand
import geo_matrix
import geo_report
import geo_runs
import tenancy


META = "/api/geo/meta"
CONTENT_META = "/api/geo/content-meta"
CHANNEL_CONFIGS = "/api/geo/channel-configs"
PROJECTS = "/api/geo/projects"
PROJECT_PATH = re.compile(r"/api/geo/projects/([0-9]{1,16})(/[a-z0-9/_-]*)?$")
MARKETS = [
    {"code": "cn", "name": "中国", "market_name": "国内", "langs": ["zh", "zh-TW"]},
    {"code": "global", "name": "海外", "market_name": "海外", "langs": ["en", "vi", "ja", "ko"]},
]
LIMIT = 5


def _path_aliases(path):
    tail = path[len("/api"):] if path.startswith("/api") else path
    return {path, "/agent-api" + path, "/agent-api" + tail}


READ_PATHS = _path_aliases(META) | _path_aliases(CONTENT_META) | _path_aliases(CHANNEL_CONFIGS) | _path_aliases(PROJECTS)
WRITE_PATHS = _path_aliases(CHANNEL_CONFIGS) | _path_aliases(PROJECTS)


def canonical(path):
    if path.startswith("/agent-api"):
        path = path[len("/agent-api"):]
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def readable(path):
    path = canonical(path)
    return path in {META, CONTENT_META, CHANNEL_CONFIGS, PROJECTS} \
        or bool(PROJECT_PATH.fullmatch(path)) or geo_runs.can_read(path) \
        or geo_brand.can_read(path) or geo_matrix.can_read(path) \
        or geo_report.can_read(path)


PUBLIC_PREFIX = geo_report.PUBLIC_PREFIX


def is_public(path):
    return canonical(path).startswith(PUBLIC_PREFIX)


def public(path):
    return geo_report.public(path)


# POST actions the archived frontend calls without a request body. Only these
# may run with an empty body; every other POST/PUT still requires JSON so a
# truncated request cannot silently overwrite stored data.
EMPTY_POST_SUFFIXES = {"resume", "facts/extract", "competitors/suggest", "kw-matrix/draft"}
KW_LAYER_CONTENT = re.compile(r"/api/geo/projects/[0-9]{1,16}/kw-matrix/layers/[a-z_]+/content$")
RUN_EMPTY_POST = re.compile(r"/api/geo/runs/[0-9]{1,16}/(?:cancel|retry|retry/estimate)$")


def empty_post(path):
    path = canonical(path)
    match = PROJECT_PATH.fullmatch(path)
    if match and (match[2] or "").strip("/") in EMPTY_POST_SUFFIXES:
        return True
    return bool(KW_LAYER_CONTENT.fullmatch(path) or RUN_EMPTY_POST.fullmatch(path))


def writable(path):
    path = canonical(path)
    return path in {CHANNEL_CONFIGS, PROJECTS} \
        or bool(PROJECT_PATH.fullmatch(path)) or geo_runs.can_write(path) \
        or geo_brand.can_write(path) or geo_matrix.can_write(path) \
        or geo_report.can_write(path)


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
        brand_name TEXT NOT NULL,
        aliases TEXT NOT NULL DEFAULT '[]', website TEXT NOT NULL DEFAULT '',
        industry TEXT NOT NULL DEFAULT '', region TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '', market TEXT NOT NULL DEFAULT 'cn',
        locales TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value, limit):
    return str(value).strip()[:limit] if isinstance(value, str) else ""


def _brand_aliases(value):
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item.strip()[:32] for item in value if isinstance(item, str) and item.strip()))[:10]


def _locales(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:20]:
        if isinstance(item, dict):
            country = _text(item.get("country"), 8)
            language = _text(item.get("language"), 16)
            if country and language:
                result.append({"country": country, "language": language})
    return result


def project(row, counts=None):
    return {
        "id": row["id"], "name": row["brand_name"], "brand_name": row["brand_name"],
        "aliases": json.loads(row["aliases"]),
        "website": row["website"], "industry": row["industry"], "region": row["region"],
        "description": row["description"], "market": row["market"],
        "market_name": "国内" if row["market"] == "cn" else "海外",
        "locales": json.loads(row["locales"]), "status": row["status"],
        "can_edit": True,
        "counts": counts or {"questions": 0, "competitors": 0, "facts_confirmed": 0, "runs": 0},
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _require(db, key):
    row = db.execute("SELECT * FROM geo_projects WHERE id=? AND tenant_id=?",
                     (int(key), commander.tenant())).fetchone()
    if row is None:
        raise commander.ChatError("GEO 项目不存在。")
    return row


def _payload(body, previous=None):
    previous = previous or {}
    brand_name = _text(body.get("brand_name", previous.get("brand_name", "")), 64)
    if not brand_name:
        raise commander.ChatError("请填写品牌名。")
    market = body.get("market", previous.get("market", "cn"))
    if market not in {"cn", "global"}:
        raise commander.ChatError("市场无效。")
    return {
        "brand_name": brand_name,
        "aliases": _brand_aliases(body.get("aliases", previous.get("aliases", []))),
        "website": _text(body.get("website", previous.get("website", "")), 256),
        "industry": _text(body.get("industry", previous.get("industry", "")), 64),
        "region": _text(body.get("region", previous.get("region", "")), 64) if market == "cn" else "",
        "description": _text(body.get("description", previous.get("description", "")), 1000),
        "market": market,
        "locales": _locales(body.get("locales", previous.get("locales", []))) if market == "global" else [],
    }


def _meta(db):
    used = db.execute("SELECT COUNT(*) FROM geo_projects WHERE tenant_id=?",
                      (commander.tenant(),)).fetchone()[0]
    return {
        "ok": True, "title": "AI搜索优化（GEO）", "projects_used": used,
        "limits": {"project_limit": LIMIT}, "can_create": used < LIMIT,
        "markets": MARKETS,
        "engines": {
            "cn": [{"key": "doubao", "name": "豆包"}, {"key": "deepseek", "name": "DeepSeek"},
                   {"key": "yuanbao", "name": "腾讯元宝"}, {"key": "kimi", "name": "Kimi"}],
            "global": [{"key": "chatgpt", "name": "ChatGPT"}, {"key": "gemini", "name": "Gemini"},
                       {"key": "claude", "name": "Claude"}, {"key": "perplexity", "name": "Perplexity"}],
        },
    }


def _empty(kind, query=None):
    return {"ok": True, "items": [], "list": [], "total": 0, "kind": kind}


def read(path, query):
    path = canonical(path)
    if geo_runs.can_read(path):
        return geo_runs.read(path, query)
    if geo_brand.can_read(path):
        return geo_brand.read(path, query)
    if geo_matrix.can_read(path):
        return geo_matrix.read(path, query)
    if geo_report.can_read(path):
        return geo_report.read(path, query)
    with commander.database() as db:
        initialize(db)
        if path == META:
            return _meta(db)
        if path == CONTENT_META:
            return {"ok": True, "markets": MARKETS, "types": [], "platforms": [], "tones": []}
        if path == CHANNEL_CONFIGS:
            return {"ok": True, "items": [], "list": [], "total": 0}
        if path == PROJECTS:
            rows = db.execute("SELECT * FROM geo_projects WHERE tenant_id=? ORDER BY id DESC",
                              (commander.tenant(),)).fetchall()
            return {"ok": True, "items": [project(row, geo_runs.counts(db, row["id"])) for row in rows], "total": len(rows)}
        match = PROJECT_PATH.fullmatch(path)
        if match:
            row = _require(db, match[1])
            suffix = (match[2] or "").strip("/")
            if not suffix:
                return {"ok": True, "project": project(row, geo_runs.counts(db, row["id"]))}
            if suffix in {"questions", "runs", "answers", "reports", "contents", "alerts", "tickets",
                          "publications", "sources", "opportunities", "entity/checks", "autopilots"}:
                return _empty(suffix, query)
            if suffix == "matrix":
                return {"ok": True, "items": [], "cells": [], "list": [], "total": 0}
            if suffix == "overview":
                return {
                    "ok": True, "project_id": row["id"],
                    "mentions": {"total": 0, "brand": 0, "competitors": 0},
                    "runs": {"total": 0, "latest": None},
                    "questions": {"total": 0},
                    "alerts": {"open": 0},
                }
            if suffix == "autopilot":
                return {"ok": True, "enabled": False, "status": "idle", "config": {}}
    raise commander.ChatError("GEO 接口暂不可用。")


def write(method, path, body):
    path = canonical(path)
    if geo_runs.can_write(path):
        return geo_runs.write(method, path, body)
    if geo_brand.can_write(path):
        return geo_brand.write(method, path, body)
    if geo_matrix.can_write(path):
        return geo_matrix.write(method, path, body)
    if geo_report.can_write(path):
        return geo_report.write(method, path, body)
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        if path == CHANNEL_CONFIGS:
            return {"ok": True, "message": "渠道配置已保存"}
        if path == PROJECTS:
            if method != "POST":
                raise commander.ChatError("请求方法无效。")
            meta = _meta(db)
            if not meta["can_create"]:
                raise commander.ChatError("项目数量已达上限。")
            values = _payload(body)
            stamp = now()
            key = db.execute(
                "INSERT INTO geo_projects (tenant_id,brand_name,aliases,website,industry,region,description,market,locales,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (commander.tenant(), values["brand_name"], json.dumps(values["aliases"], ensure_ascii=False),
                 values["website"], values["industry"], values["region"], values["description"],
                 values["market"], json.dumps(values["locales"], ensure_ascii=False), "active", stamp, stamp),
            ).lastrowid
            tenancy.audit_in(db, commander.tenant(), "geo.project.create", str(key),
                             values["brand_name"])
            return {"ok": True, "project": project(_require(db, key), geo_runs.counts(db, key))}
        match = PROJECT_PATH.fullmatch(path)
        if match:
            row = _require(db, match[1])
            suffix = (match[2] or "").strip("/")
            if not suffix:
                if method == "PUT":
                    values = _payload(body, project(row))
                    db.execute(
                        "UPDATE geo_projects SET brand_name=?,aliases=?,website=?,industry=?,region=?,description=?,market=?,locales=?,updated_at=? WHERE id=? AND tenant_id=?",
                        (values["brand_name"], json.dumps(values["aliases"], ensure_ascii=False),
                         values["website"], values["industry"], values["region"], values["description"],
                         values["market"], json.dumps(values["locales"], ensure_ascii=False), now(),
                         row["id"], commander.tenant()),
                    )
                    tenancy.audit_in(db, commander.tenant(), "geo.project.update", str(row["id"]),
                                     values["brand_name"])
                    return {"ok": True, "project": project(_require(db, row["id"]), geo_runs.counts(db, row["id"]))}
                if method == "DELETE":
                    db.execute("DELETE FROM geo_projects WHERE id=? AND tenant_id=?",
                               (row["id"], commander.tenant()))
                    tenancy.audit_in(db, commander.tenant(), "geo.project.delete", str(row["id"]))
                    return {"ok": True}
            if suffix == "resume" and method == "POST":
                db.execute("UPDATE geo_projects SET status='active',updated_at=? WHERE id=? AND tenant_id=?",
                           (now(), row["id"], commander.tenant()))
                tenancy.audit_in(db, commander.tenant(), "geo.project.resume", str(row["id"]))
                return {"ok": True, "project": project(_require(db, row["id"]), geo_runs.counts(db, row["id"]))}
            if suffix in {"contents", "tickets"} and method == "POST":
                raise commander.ChatError("GEO 内容工坊暂未开放。")
            if suffix in {"entity/business", "entity/store"} and method == "POST":
                raise commander.ChatError("工商 / 门店核对需要配置企业数据源，暂未开放。")
    raise commander.ChatError("GEO 接口暂不可用。")
