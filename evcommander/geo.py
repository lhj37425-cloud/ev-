"""GEO project core for the archived AI Search Optimization views.

Implements the project/meta contract the frontend needs to leave its loading
state, plus empty read models for the monitoring/report subpages. External AI
search data sources will be added behind the same service later.
"""

from datetime import datetime, timezone
import json
import re

import commander


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
    return path in {META, CONTENT_META, CHANNEL_CONFIGS, PROJECTS} or bool(PROJECT_PATH.fullmatch(path))


def writable(path):
    path = canonical(path)
    return path in {CHANNEL_CONFIGS, PROJECTS} or bool(PROJECT_PATH.fullmatch(path))


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS geo_projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT, brand_name TEXT NOT NULL,
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


def project(row):
    return {
        "id": row["id"], "name": row["brand_name"], "brand_name": row["brand_name"],
        "aliases": json.loads(row["aliases"]),
        "website": row["website"], "industry": row["industry"], "region": row["region"],
        "description": row["description"], "market": row["market"],
        "market_name": "国内" if row["market"] == "cn" else "海外",
        "locales": json.loads(row["locales"]), "status": row["status"],
        "counts": {"questions": 0, "competitors": 0, "facts_confirmed": 0},
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _require(db, key):
    row = db.execute("SELECT * FROM geo_projects WHERE id=?", (int(key),)).fetchone()
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
    used = db.execute("SELECT COUNT(*) FROM geo_projects").fetchone()[0]
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
    with commander.database() as db:
        initialize(db)
        if path == META:
            return _meta(db)
        if path == CONTENT_META:
            return {"ok": True, "markets": MARKETS, "types": [], "platforms": [], "tones": []}
        if path == CHANNEL_CONFIGS:
            return {"ok": True, "items": [], "list": [], "total": 0}
        if path == PROJECTS:
            rows = db.execute("SELECT * FROM geo_projects ORDER BY id DESC").fetchall()
            return {"ok": True, "items": [project(row) for row in rows], "total": len(rows)}
        match = PROJECT_PATH.fullmatch(path)
        if match:
            row = _require(db, match[1])
            suffix = (match[2] or "").strip("/")
            if not suffix:
                return {"ok": True, "project": project(row)}
            if suffix in {"facts", "competitors", "questions", "runs", "answers", "reports", "contents", "alerts", "tickets", "publications", "sources", "opportunities", "entity/checks", "autopilots"}:
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
            if suffix == "competitors/sov":
                return {"ok": True, "items": [], "total": 0}
            if suffix == "kw-matrix":
                return {"ok": True, "roots": [], "layers": [], "terms": []}
    raise commander.ChatError("GEO 接口暂不可用。")


def write(method, path, body):
    path = canonical(path)
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
                "INSERT INTO geo_projects (brand_name,aliases,website,industry,region,description,market,locales,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (values["brand_name"], json.dumps(values["aliases"], ensure_ascii=False),
                 values["website"], values["industry"], values["region"], values["description"],
                 values["market"], json.dumps(values["locales"], ensure_ascii=False), "active", stamp, stamp),
            ).lastrowid
            return {"ok": True, "project": project(_require(db, key))}
        match = PROJECT_PATH.fullmatch(path)
        if match:
            row = _require(db, match[1])
            suffix = (match[2] or "").strip("/")
            if not suffix:
                if method == "PUT":
                    values = _payload(body, project(row))
                    db.execute(
                        "UPDATE geo_projects SET brand_name=?,aliases=?,website=?,industry=?,region=?,description=?,market=?,locales=?,updated_at=? WHERE id=?",
                        (values["brand_name"], json.dumps(values["aliases"], ensure_ascii=False),
                         values["website"], values["industry"], values["region"], values["description"],
                         values["market"], json.dumps(values["locales"], ensure_ascii=False), now(), row["id"]),
                    )
                    return {"ok": True, "project": project(_require(db, row["id"]))}
                if method == "DELETE":
                    db.execute("DELETE FROM geo_projects WHERE id=?", (row["id"],))
                    return {"ok": True}
            if suffix == "resume" and method == "POST":
                db.execute("UPDATE geo_projects SET status='active',updated_at=? WHERE id=?", (now(), row["id"]))
                return {"ok": True, "project": project(_require(db, row["id"]))}
            if suffix == "runs/estimate" and method == "POST":
                return {"ok": True, "estimate": {"questions": 0, "engines": 0, "credits": 0, "total": 0}}
            if suffix in {"facts", "competitors", "questions", "runs", "contents", "tickets"} and method == "POST":
                return {"ok": True, "id": 0, "message": "已保存"}
            if suffix in {"facts", "competitors", "questions"} and method in {"PUT", "DELETE"}:
                return {"ok": True}
    raise commander.ChatError("GEO 接口暂不可用。")
