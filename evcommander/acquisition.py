"""Configuration placeholders for the acquisition channel pages.

The pages can render their forms without provider credentials. Search/capture
endpoints return an explicit "not configured" result instead of claiming
success; business profile is stored locally because it is just user input.
"""

import json

import commander


REGIONS = "/api/acquisition/regions"
MAP_PROVIDERS = "/api/acquisition/map/providers"
MAP_INDUSTRIES = "/api/acquisition/map/industries"
TIANYANCHA_PROVIDERS = "/api/acquisition/tianyancha/providers"
WEB_META = "/api/acquisition/web-hunter/meta"
WEB_HISTORY = "/api/acquisition/web-hunter/history"
BIZ_PROFILE = "/api/acquisition/business-profile"
CREATOR_PLATFORMS = "/api/acquisition/creator/platforms"
CREATOR_ROLES = "/api/acquisition/creator/roles"
CREATOR_FOLLOW_QUOTA = "/api/acquisition/creator/follow-quota"
CREATOR_RISK_QUOTA = "/api/acquisition/creator/risk-quota"
CREATOR_TASKS = "/api/acquisition/creator/tasks"
CREATOR_HISTORY = "/api/acquisition/creator/history"
MAP_SEARCH = "/api/acquisition/map/search"
TIANYANCHA_SEARCH = "/api/acquisition/tianyancha/search"
WEB_START = "/api/acquisition/web-hunter/start"
CAPTURE = "/api/acquisition/capture"
VIDEO_CAPTURE = "/api/acquisition/video/capture"
CREATOR_CAPTURE = "/api/acquisition/creator/capture"
KEYWORD_SUGGEST = "/api/acquisition/video/keyword/suggest"
CREATOR_KEYWORD_SUGGEST = "/api/acquisition/creator/keyword/suggest"

NOT_CONFIGURED = "未配置获客渠道，请联系管理员在后台上传对应账号/API key。"


def _aliases(path):
    tail = path[len("/api"):] if path.startswith("/api") else path
    return {path, "/agent-api" + path, "/agent-api" + tail}


READ_PATHS = set().union(*(_aliases(path) for path in {
    REGIONS, MAP_PROVIDERS, MAP_INDUSTRIES, TIANYANCHA_PROVIDERS, WEB_META,
    WEB_HISTORY, BIZ_PROFILE, CREATOR_PLATFORMS, CREATOR_ROLES,
    CREATOR_FOLLOW_QUOTA, CREATOR_RISK_QUOTA, CREATOR_TASKS, CREATOR_HISTORY,
}))
WRITE_PATHS = set().union(*(_aliases(path) for path in {
    BIZ_PROFILE, MAP_SEARCH, TIANYANCHA_SEARCH, WEB_START, CAPTURE,
    VIDEO_CAPTURE, CREATOR_CAPTURE, KEYWORD_SUGGEST, CREATOR_KEYWORD_SUGGEST,
}))


def canonical(path):
    if path.startswith("/agent-api"):
        path = path[len("/agent-api"):]
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS acquisition_profile (
        id INTEGER PRIMARY KEY CHECK (id=1), data TEXT NOT NULL
    )""")


def _profile(db):
    row = db.execute("SELECT data FROM acquisition_profile WHERE id=1").fetchone()
    data = {}
    if row:
        try:
            data = json.loads(row["data"])
        except ValueError:
            data = {}
    return {
        "ok": True,
        "main_products": data.get("main_products", ""),
        "target_customer": data.get("target_customer", ""),
        "exclude_scope": data.get("exclude_scope", ""),
    }


def read(path, query):
    path = canonical(path)
    with commander.database() as db:
        initialize(db)
        if path == REGIONS:
            return {"list": []}
        if path == MAP_PROVIDERS:
            return {"list": [], "providers": []}
        if path == MAP_INDUSTRIES:
            return {"list": [], "industries": []}
        if path == TIANYANCHA_PROVIDERS:
            return {"list": [], "providers": []}
        if path == WEB_META:
            return {"ok": True, "configured": False, "providers": [], "message": NOT_CONFIGURED}
        if path == WEB_HISTORY:
            return {"list": [], "total": 0}
        if path == BIZ_PROFILE:
            return _profile(db)
        if path == CREATOR_PLATFORMS:
            return {"list": []}
        if path == CREATOR_ROLES:
            return {"list": []}
        if path in {CREATOR_FOLLOW_QUOTA, CREATOR_RISK_QUOTA}:
            return {"ok": True, "remaining": 0, "limit": 0, "used": 0}
        if path == CREATOR_TASKS:
            return {"tasks": [], "list": [], "total": 0}
        if path == CREATOR_HISTORY:
            return {"list": []}
    raise commander.ChatError("获客接口暂不可用。")


def write(path, body):
    path = canonical(path)
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        if path == BIZ_PROFILE:
            data = {
                "main_products": str(body.get("main_products") or "").strip()[:500],
                "target_customer": str(body.get("target_customer") or "").strip()[:500],
                "exclude_scope": str(body.get("exclude_scope") or "").strip()[:1000],
            }
            db.execute(
                "INSERT INTO acquisition_profile (id,data) VALUES (1,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (json.dumps(data, ensure_ascii=False),),
            )
            return {"ok": True, **data}
    return {"ok": False, "error": NOT_CONFIGURED}
