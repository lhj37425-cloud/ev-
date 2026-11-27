"""Local sample data for the archived AgentMax frontend."""

from datetime import datetime, timedelta, timezone
from http import HTTPStatus

from commander import CHAT_CONFIG, LOCAL_TOKEN
from image_generation import MODEL as IMAGE_MODEL


ACCOUNT = {"id": 1, "nickname": "本地演示", "avatar": "", "mobile": ""}
BALANCE = {"balance_power": 100, "balance_commission": 0, "freeze_commission": 0, "total_recharge": 0}
BRAND_NAME = "EV指挥官"
BRAND = {
    "system_name": BRAND_NAME,
    "logo_url": "/logo.png",
    "edition": "enterprise",
    "ui_theme": "classic",
    "menu_template_default": "dept",
    "department_overrides": {
        "创意部门": "同城内容",
        "生产部门": "门店素材",
        "获客部门": "同城获客",
        "成交部门": "到店转化",
    },
    "menu_items": [{"key": "channel", "hidden": True}],
}
OFFLINE_MESSAGE = "此操作需要连接后端。本地演示未执行此操作。"
IMAGE_RATIOS = {
    "key": "size",
    "label": "画幅",
    "default": "1:1",
    "options": [
        {"value": value, "label": label}
        for value, label in [
            ("1:1", "1:1 方图"), ("16:9", "16:9 横屏"), ("9:16", "9:16 竖屏"),
            ("4:3", "4:3 横图"), ("3:4", "3:4 竖图"), ("3:2", "3:2 横图"),
            ("2:3", "2:3 竖图"),
        ]
    ],
}
IMAGE_MODELS = [{
    "code": IMAGE_MODEL, "brand": "OpenAI", "name": "GPT Image 2.6 Flare",
    "usable": True, "recommended": True, "supports_reference": False,
    "max_count": 1, "params": [IMAGE_RATIOS],
}]
PUBLISH_PLATFORMS = [
    {"key": key, "name": name, "region": "cn", "supports": formats}
    for key, name, formats in [
        ("douyin", "抖音", ["video", "image"]),
        ("xiaohongshu", "小红书", ["video", "image"]),
        ("kuaishou", "快手", ["video", "image"]),
        ("shipinhao", "视频号", ["video"]),
    ]
]

BOOTSTRAP = b"""<script>
if (location.hostname === '127.0.0.1' && location.pathname !== '/login') {
  localStorage.setItem('token', '__HEPH_LOCAL_TOKEN__');
  if (!localStorage.getItem('pc-theme')) localStorage.setItem('pc-theme', 'dark');
}
</script>""".replace(b"__HEPH_LOCAL_TOKEN__", LOCAL_TOKEN.encode("ascii"))

READ_FIXTURES = {
    "/api/user/info": ACCOUNT,
    "/api/user/asset": BALANCE,
    "/api/personal/center": BALANCE,
    "/api/brand/by-domain": BRAND,
    "/api/brand/config": BRAND,
    "/api/brand/current": BRAND,
    "/api/signin/info": {"signed": False},
    "/api/chat/config": CHAT_CONFIG,
    "/agent-api/health": {"status": "ok"},
    "/agent-api/api/menu/access": {"edition": "enterprise", "workspace": "full"},
    "/agent-api/api/ext/talent/templates": {"templates": [], "groups": []},
    "/agent-api/api/ext/talent/template/active": {"has_pref": False},
    "/agent-api/api/ext/talent/menu": {"departments": [], "groups": [], "employees": []},
    "/agent-api/api/ext/talent/employees": {"employees": []},
    "/agent-api/api/ext/talent/employees/visible": {"employees": []},
    "/agent-api/api/ext/talent/agent/capability-prefs": {},
    "/agent-api/api/ext/commander/catalog": {"list": []},
    "/agent-api/api/ext/commander/runs/summary": {},
    "/agent-api/api/chat/default-model": {},
    "/agent-api/api/companion/presence": {"online": False},
    "/agent-api/api/mgmt/my-tasks": {"tasks": []},
    "/agent-api/api/machines": {"list": [], "details": []},
    "/agent-api/api/machines/online": {"list": [], "details": []},
    "/agent-api/api/rpa/devices": {"list": [], "devices": []},
    "/agent-api/api/planning/projects": {"projects": [], "total": 0},
    "/agent-api/api/planning/models": {"models": [], "picks": []},
    "/agent-api/api/planning/markets": {"markets": []},
    "/agent-api/api/daily/tasks": {"tasks": []},
    "/agent-api/api/ext/designer/models": {"image_models": IMAGE_MODELS, "video_models": []},
    "/agent-api/api/generation/list": {"list": [], "total": 0},
    "/agent-api/api/generation/count": {"count": 0},
    "/agent-api/api/creative-dept/studio/options": {},
    "/agent-api/api/acquisition/video/platforms": {"list": []},
    "/agent-api/api/acquisition/video/outreach/settings": {},
    "/agent-api/api/acquisition/video/outreach/queue": {"list": [], "total": 0},
    "/agent-api/api/acquisition/video/history": {"list": []},
    "/agent-api/api/acquisition/video/tasks": {"tasks": []},
    "/agent-api/api/acquisition/video/risk-quota": {},
    "/agent-api/api/acquisition/video/customer-types": {"list": []},
    "/agent-api/api/acquisition/video/templates": {"list": []},
    "/agent-api/api/acquisition/exclude": {"list": []},
    "/agent-api/api/acquisition/dm/quota": {},
    "/agent-api/api/acquisition/leads": {"leads": [], "total": 0},
    "/agent-api/api/methodology/readiness": {},
    "/agent-api/api/publish/accounts": {"list": []},
    "/agent-api/api/publish/platforms": {"list": PUBLISH_PLATFORMS},
    "/agent-api/api/publish/records": {"list": [], "total": 0},
}

READ_ONLY_POST_FIXTURES = {
    "/agent-api/api/avatar/batch": {"avatars": {}},
}


def release_summary():
    today = datetime.now(timezone(timedelta(hours=8))).date()
    return {
        "available": True,
        "days": 7,
        "timezone": "Asia/Shanghai",
        "from": (today - timedelta(days=6)).isoformat(),
        "to": today.isoformat(),
        "scopes": ["platform", "pc", "android"],
        "batch_rule": "scope+release_date+(version|title|row_id):v1",
        "release_count": 26,
        "record_count": 26,
    }


def reply(method, path):
    if method in {"GET", "HEAD"} and path == "/api/release-note/summary":
        return HTTPStatus.OK, {"code": "0000", "msg": "", "data": release_summary()}
    if method in {"GET", "HEAD"} and path in READ_FIXTURES:
        data = READ_FIXTURES[path]
        if path.startswith("/api/"):
            data = {"code": "0000", "msg": "", "data": data}
        return HTTPStatus.OK, data
    if method == "POST" and path in READ_ONLY_POST_FIXTURES:
        return HTTPStatus.OK, READ_ONLY_POST_FIXTURES[path]
    api_path = path.removeprefix("/agent-api")
    if api_path in {"/api/digital-human/create-video", "/api/digital-human/video-task",
                    "/api/production/digital-human"}:
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "code": "LOCAL_PREVIEW", "msg": "数字人视频合成暂缓，当前可生成和翻译口播稿。",
        }
    if path.startswith("/agent-api/"):
        return HTTPStatus.SERVICE_UNAVAILABLE, {"detail": OFFLINE_MESSAGE}
    return HTTPStatus.SERVICE_UNAVAILABLE, {"code": "LOCAL_PREVIEW", "msg": OFFLINE_MESSAGE}
