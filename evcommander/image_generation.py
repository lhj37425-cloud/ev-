"""Generate and edit images for the existing Designer and Image Studio views.

The archived frontend is not changed. This module adapts the backend to the
frontend's OpenAI-compatible image contract and supports both:
- original OpenAI Images API (JSON generations / multipart edits);
- OpenAI-compatible relays that return a public image URL instead of b64_json.
"""

import base64
import binascii
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from threading import BoundedSemaphore
from uuid import uuid4

import commander


MODEL = os.environ.get("EV_IMAGE_MODEL", "gpt-image-2.6-flare")
BASE_URL = os.environ.get("EV_IMAGE_BASE_URL", "https://api.openai.com/v1").rstrip("/")
DESIGNER = "/agent-api/api/ext/designer/"
STREAM_PATHS = {
    "/api/ext/designer/image/stream",
    "/agent-api/api/ext/designer/image/stream",
}
EDIT_STREAM = "/api/ext/ecom-model/image-edit/stream"
PRODUCT_BG_STREAM = "/api/ext/ecom-model/product-bg/stream"
TRYON_STREAM = "/api/ext/ecom-model/tryon/stream"
MODELS_PATH = "/api/ext/designer/models"
EDIT_OPTIONS = "/api/ext/ecom-model/image-edit/options"
TRYON_OPTIONS = "/api/ext/ecom-model/tryon/options"
PROMPT_LIBRARY = "/api/ext/designer/prompt-library"
FOLLOWUP = "/api/ext/designer/image/followup"


def _both(path):
    return {path, "/agent-api" + path}


EDIT_STREAM_PATHS = _both(EDIT_STREAM)
PRODUCT_BG_STREAM_PATHS = _both(PRODUCT_BG_STREAM)
TRYON_STREAM_PATHS = _both(TRYON_STREAM)
MODELS_PATHS = _both(MODELS_PATH)
EDIT_OPTIONS_PATHS = _both(EDIT_OPTIONS)
TRYON_OPTIONS_PATHS = _both(TRYON_OPTIONS)
PROMPT_LIBRARY_PATHS = _both(PROMPT_LIBRARY)
FOLLOWUP_PATHS = _both(FOLLOWUP)
MEDIA_PATH = re.compile(r"/generated-media/([0-9a-f]{32}\.(?:png|jpg))")
WORK_DETAIL = re.compile(r"/api/works/(?:detail/)?([0-9]{1,16})")
SIZES = {
    "1:1": "1024x1024", "16:9": "1536x864", "9:16": "864x1536",
    "4:3": "1280x960", "3:4": "960x1280",
    "3:2": "1536x1024", "2:3": "1024x1536",
}
MAX_IMAGE_BYTES = 12 * 1024 * 1024
ACTIVE = BoundedSemaphore(1)


def read_image_key():
    key = os.environ.get("EV_IMAGE_API_KEY")
    if key:
        return key.strip()
    path = os.environ.get("EV_IMAGE_API_KEY_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise commander.ChatError("生图 key 文件不可读。") from error
    return commander.read_openai_key()


def _curl():
    return os.environ.get("EV_CURL_BIN") or shutil.which("curl") or "/usr/bin/curl"


def _proto(url):
    return "=https" if url.startswith("https://") else "=http,https"


def _image_type(content):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    return None, None


def _download(url, label="图片"):
    if isinstance(url, Path):
        try:
            content = url.read_bytes()
        except OSError as error:
            raise commander.ChatError(f"{label}读取失败，请重新上传后重试。") from error
    elif isinstance(url, str) and not url.startswith(("https://", "http://")):
        try:
            content = Path(url).read_bytes()
        except OSError as error:
            raise commander.ChatError(f"{label}读取失败，请重新上传后重试。") from error
    elif isinstance(url, str):
        try:
            result = subprocess.run(
                [_curl(), "--disable", "--proto", _proto(url), "--noproxy", "*",
                 "--connect-timeout", "12", "--max-time", "60", "--silent", "--show-error",
                 "--fail", "--max-filesize", str(MAX_IMAGE_BYTES), url],
                capture_output=True, timeout=70, check=True,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise commander.ChatError(f"{label}下载失败，请重新上传后重试。") from error
        content = result.stdout
    else:
        raise commander.ChatError(f"{label}地址无效。")
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise commander.ChatError(f"{label}大小不可用。")
    if not _image_type(content)[0]:
        raise commander.ChatError(f"{label}不是可用的 PNG/JPEG 图片。")
    return content


def _request_meta(response):
    usage = response.get("usage") if isinstance(response, dict) else None
    return usage if isinstance(usage, dict) else {}


def _content_from_response(response):
    problem = response.get("error") if isinstance(response, dict) else None
    if problem:
        code = problem.get("code") if isinstance(problem, dict) else None
        message = str(problem.get("message", "")) if isinstance(problem, dict) else ""
        if "verif" in message.lower() and "organization" in message.lower():
            raise commander.ChatError("图片模型需要完成组织验证后才能使用。")
        if code in {"insufficient_quota", "billing_hard_limit_reached"}:
            raise commander.ChatError("图片生成额度不足。")
        if code in {"model_not_found", "permission_denied"}:
            raise commander.ChatError("当前账号尚未开通所选图片模型。")
        raise commander.ChatError(message or "图片模型未受理请求，请稍后重试。")
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise commander.ChatError("图片模型没有返回图片，请稍后重试。")
    item = data[0]
    if item.get("b64_json"):
        try:
            content = base64.b64decode(item["b64_json"], validate=True)
        except (binascii.Error, TypeError, ValueError) as error:
            raise commander.ChatError("图片模型返回的图片格式不可用。") from error
    elif item.get("url"):
        content = _download(item["url"], "生成结果")
    else:
        raise commander.ChatError("图片模型没有返回图片地址。")
    if not content or len(content) > MAX_IMAGE_BYTES or not _image_type(content)[0]:
        raise commander.ChatError("图片模型返回的图片格式或大小不可用。")
    return content


def _json_generate(prompt, size, model, key):
    payload = json.dumps({
        "model": model, "prompt": prompt, "size": size, "n": 1,
        "quality": "low", "output_format": "png",
    })
    config = (
        "header = " + json.dumps("Authorization: Bearer " + key) + "\n"
        "data-binary = " + json.dumps(payload) + "\n"
    ).encode("utf-8")
    target = BASE_URL + "/images/generations"
    try:
        result = subprocess.run(
            [_curl(), "--disable", "--config", "-", "--proto", _proto(target),
             "--noproxy", "*", "--connect-timeout", "12", "--max-time", "155",
             "--max-filesize", str(2 * MAX_IMAGE_BYTES), "--silent", "--show-error",
             "--header", "Content-Type: application/json", target],
            input=config, capture_output=True, timeout=160, check=True,
        )
        response = json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        raise commander.ChatError("生图服务没有返回完整图片，请稍后重试。") from error
    return _content_from_response(response), _request_meta(response)


def _multipart_edit(prompt, size, model, key, images, mask=None):
    folder = directory()
    folder.mkdir(mode=0o700, exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(prefix="image-input-", dir=folder) as workdir:
        files = []
        for index, url in enumerate(images):
            content = _download(url, f"参考图 {index + 1}")
            kind, suffix = _image_type(content)
            path = Path(workdir) / f"input-{index}{suffix}"
            path.write_bytes(content)
            files.append((path, kind))
        mask_path = None
        if mask:
            content = _download(mask, "选区图")
            kind, suffix = _image_type(content)
            if not kind:
                raise commander.ChatError("选区图不是可用的 PNG/JPEG 图片。")
            mask_path = Path(workdir) / f"mask{suffix}"
            mask_path.write_bytes(content)
        config = ("header = " + json.dumps("Authorization: Bearer " + key) + "\n").encode("utf-8")
        target = BASE_URL + "/images/edits"
        command = [
            _curl(), "--disable", "--config", "-", "--proto", _proto(target),
            "--noproxy", "*", "--connect-timeout", "12", "--max-time", "240",
            "--max-filesize", str(2 * MAX_IMAGE_BYTES), "--silent", "--show-error",
            "-F", f"model={model}", "-F", f"prompt={prompt}", "-F", f"size={size}",
            "-F", "quality=low", "-F", "n=1",
        ]
        for path, kind in files:
            command += ["-F", f"image[]=@{path};type={kind}"]
        if mask_path:
            command += ["-F", f"mask=@{mask_path};type={_image_type(mask_path.read_bytes())[0]}"]
        command.append(target)
        try:
            result = subprocess.run(
                command, input=config, capture_output=True, timeout=260, check=True,
            )
            response = json.loads(result.stdout)
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            raise commander.ChatError("图片编辑没有返回完整图片，请稍后重试。") from error
    return _content_from_response(response), _request_meta(response)


def generate(request):
    """Return (image bytes, metadata) for one normalized image request."""
    prompt = request["prompt"]
    size = request.get("size") or SIZES["1:1"]
    model = request.get("model") or MODEL
    images = request.get("images") or []
    mask = request.get("mask")
    key = read_image_key()
    if images or mask:
        return _multipart_edit(prompt, size, model, key, images, mask)
    return _json_generate(prompt, size, model, key)


def table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS designer_images (
        id INTEGER PRIMARY KEY AUTOINCREMENT, file_name TEXT NOT NULL UNIQUE,
        prompt TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL
    )""")


def directory():
    return commander.DATA_DIR / "generated-media"


def save(content, prompt, model=MODEL):
    folder = directory()
    folder.mkdir(mode=0o700, exist_ok=True, parents=True)
    kind, suffix = _image_type(content)
    if not kind:
        raise commander.ChatError("图片格式不可用。")
    filename = uuid4().hex + suffix
    target = folder / filename
    try:
        with target.open("xb") as output:
            output.write(content)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with commander.database() as connection:
            table(connection)
            work_id = connection.execute(
                "INSERT INTO designer_images (file_name,prompt,model,created_at) VALUES (?,?,?,?)",
                (filename, prompt, model, now),
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
        raise commander.ChatError("请选择当前可用的生图模型。")
    ratio = options.get("size", "1:1")
    if not isinstance(ratio, str) or ratio not in SIZES:
        raise commander.ChatError("请选择支持的图片画幅。")
    count = body.get("count", 1)
    if type(count) is not int or count != 1:
        raise commander.ChatError("当前支持每次生成 1 张图。")
    references = body.get("reference_images") or []
    if not isinstance(references, list) or not all(
        isinstance(item, str) and item.startswith(("https://", "http://")) for item in references
    ):
        raise commander.ChatError("参考图格式无效。")
    if len(references) > 4:
        raise commander.ChatError("参考图最多 4 张。")
    return {
        "prompt": prompt.strip(),
        "size": SIZES[ratio],
        "images": references,
        "model": MODEL,
    }


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
