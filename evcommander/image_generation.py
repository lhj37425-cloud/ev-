"""Generate and store images for the existing Designer and Asset Center views."""

import base64
import binascii
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import json
import re
import subprocess
from threading import BoundedSemaphore
from uuid import uuid4

import commander


MODEL = "gpt-image-2.6-flare"
DESIGNER = "/agent-api/api/ext/designer/"
STREAM = DESIGNER + "image/stream"
MEDIA_PATH = re.compile(r"/generated-media/([0-9a-f]{32}\.png)")
WORK_DETAIL = re.compile(r"/api/works/(?:detail/)?([0-9]{1,16})")
SIZES = {
    "1:1": "1024x1024", "16:9": "1536x864", "9:16": "864x1536",
    "4:3": "1280x960", "3:4": "960x1280",
    "3:2": "1536x1024", "2:3": "1024x1536",
}
MAX_IMAGE_BYTES = 12 * 1024 * 1024
ACTIVE = BoundedSemaphore(1)


def generate(prompt, size):
    key = commander.read_openai_key()
    payload = json.dumps({
        "model": MODEL, "prompt": prompt, "size": size, "n": 1,
        "quality": "low", "output_format": "png",
    })
    config = (
        "header = " + json.dumps("Authorization: Bearer " + key) + "\n"
        "data-binary = " + json.dumps(payload) + "\n"
    )
    try:
        result = subprocess.run(
            ["/usr/bin/curl", "--disable", "--config", "-", "--proto", "=https",
             "--noproxy", "*", "--connect-timeout", "12", "--max-time", "155",
             "--max-filesize", str(2 * MAX_IMAGE_BYTES), "--silent", "--show-error",
             "--header", "Content-Type: application/json",
             "https://api.openai.com/v1/images/generations"],
            input=config.encode(), capture_output=True, timeout=160, check=True,
        )
        response = json.loads(result.stdout)
        problem = response.get("error")
        if problem:
            code = problem.get("code") if isinstance(problem, dict) else None
            message = str(problem.get("message", "")) if isinstance(problem, dict) else ""
            if "verif" in message.lower() and "organization" in message.lower():
                raise commander.ChatError("OpenAI 账号需要完成组织验证才能使用生图模型。")
            if code in {"insufficient_quota", "billing_hard_limit_reached"}:
                raise commander.ChatError("OpenAI 生图额度不足。")
            if code in {"model_not_found", "permission_denied"}:
                raise commander.ChatError("当前 OpenAI 账号尚未开通所选生图模型。")
            raise commander.ChatError("OpenAI 未受理生图请求，请调整提示词后重试。")
        raw = base64.b64decode(response["data"][0]["b64_json"], validate=True)
        if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) > MAX_IMAGE_BYTES:
            raise commander.ChatError("模型返回的图片格式或大小不可用。")
        return raw
    except commander.ChatError:
        raise
    except (subprocess.SubprocessError, KeyError, IndexError, TypeError, ValueError,
            binascii.Error, OSError) as error:
        raise commander.ChatError("生图服务没有返回完整图片，请稍后重试。") from error


def table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS designer_images (
        id INTEGER PRIMARY KEY AUTOINCREMENT, file_name TEXT NOT NULL UNIQUE,
        prompt TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL
    )""")


def directory():
    return commander.DATA_DIR / "generated-media"


def save(content, prompt):
    folder = directory()
    folder.mkdir(mode=0o700, exist_ok=True, parents=True)
    filename = uuid4().hex + ".png"
    target = folder / filename
    try:
        with target.open("xb") as output:
            output.write(content)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with commander.database() as connection:
            table(connection)
            work_id = connection.execute(
                "INSERT INTO designer_images (file_name,prompt,model,created_at) VALUES (?,?,?,?)",
                (filename, prompt, MODEL, now),
            ).lastrowid
        return work_id, "/generated-media/" + filename
    except BaseException:
        with suppress(FileNotFoundError):
            target.unlink()
        raise


def file_for(filename):
    with commander.database() as connection:
        table(connection)
        row = connection.execute("SELECT 1 FROM designer_images WHERE file_name=?", (filename,)).fetchone()
    path = directory() / filename
    return path if row and path.is_file() else None


def work(row):
    url = "/generated-media/" + row["file_name"]
    return {
        "id": row["id"], "type": 2, "media_type": "image", "task_name": row["prompt"][:80],
        "title": row["prompt"][:80], "prompt": row["prompt"], "model_code": row["model"],
        "output_url": url, "image_url": url, "thumbnail": url, "status": 2,
        "category": "designer",
        "created_at": row["created_at"],
    }


def list_works(query):
    media_type = query.get("media_type", [""])[0]
    if (media_type not in ("", "image", "2")
            or query.get("type", [""])[0] not in ("", "2")
            or query.get("status", [""])[0] not in ("", "2")
            or query.get("category", [""])[0] not in ("", "designer")
            or query.get("boost", [""])[0] == "1"):
        return {"list": [], "total": 0}
    try:
        page = max(1, min(10000, int(query.get("page", ["1"])[0])))
        size = max(1, min(100, int(query.get("page_size", query.get("pageSize", ["20"]))[0])))
        days = max(0, min(365, int(query.get("days", ["0"])[0])))
    except ValueError as error:
        raise commander.ChatError("分页参数无效。") from error
    sort = {"time_asc": "id ASC", "name_asc": "prompt ASC, id DESC",
            "name_desc": "prompt DESC, id DESC"}.get(query.get("sort", [""])[0], "id DESC")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else ""
    keyword = query.get("keyword", [""])[0].strip()
    criteria = "WHERE created_at >= ? AND instr(lower(prompt), lower(?)) > 0"
    with commander.database() as connection:
        table(connection)
        rows = connection.execute(
            f"SELECT * FROM designer_images {criteria} ORDER BY {sort} LIMIT ? OFFSET ?",
            (since, keyword, size, (page - 1) * size),
        ).fetchall()
        count, = connection.execute(f"SELECT COUNT(*) FROM designer_images {criteria}", (since, keyword)).fetchone()
    return {"list": [work(row) for row in rows], "total": count}


def detail(key):
    with commander.database() as connection:
        table(connection)
        row = connection.execute("SELECT * FROM designer_images WHERE id=?", (int(key),)).fetchone()
    if row is None:
        raise commander.ChatError("图片作品不存在。")
    return work(row)


def validate(body):
    prompt = body.get("prompt")
    options = body.get("params", {})
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 3000:
        raise commander.ChatError("请输入 1 到 3000 字的生图提示词。")
    if not isinstance(options, dict) or body.get("model_code") != MODEL:
        raise commander.ChatError("请选择当前可用的 OpenAI 生图模型。")
    ratio = options.get("size", "1:1")
    if not isinstance(ratio, str) or ratio not in SIZES:
        raise commander.ChatError("请选择支持的图片画幅。")
    count = body.get("count", 1)
    if type(count) is not int or count != 1 or body.get("reference_images"):
        raise commander.ChatError("当前支持每次生成 1 张图，请移除参考图后重试。")
    return prompt.strip(), SIZES[ratio]


def reserve():
    limit = 8
    day = datetime.now(timezone.utc).date().isoformat()
    with commander.database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS image_usage (day TEXT PRIMARY KEY, count INTEGER NOT NULL)")
        connection.execute("DELETE FROM image_usage WHERE day < ?", (day,))
        changed = connection.execute(
            "INSERT INTO image_usage VALUES (?,1) ON CONFLICT(day) DO UPDATE "
            "SET count=count+1 WHERE count < ?", (day, limit),
        )
        if changed.rowcount != 1:
            raise commander.ChatError("今天的试用生图次数已用完。")
