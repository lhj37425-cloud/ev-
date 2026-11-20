"""Persistent material library used by the existing asset workspace."""

from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import PurePosixPath
import re
from uuid import uuid4

import commander


UPLOAD = "/api/common/upload"
MAX_FILE = 32 * 1024 * 1024
MAX_STORAGE = 2 * 1024 ** 3
FILE_PATH = re.compile(r"/material-media/([a-f0-9]{32})")
READ = {"/api/material/list", "/api/material/folders", "/api/material/folder/items"}
WRITE = {"/api/oss/credential", "/api/material/add", "/api/material/delete", "/api/material/move",
         *("/api/material/folder/" + action for action in ("create", "rename", "move", "delete"))}
MEDIA_TYPES = {
    ".png": ("image", "image/png"), ".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"),
    ".gif": ("image", "image/gif"), ".webp": ("image", "image/webp"), ".avif": ("image", "image/avif"),
    ".mp4": ("video", "video/mp4"), ".mov": ("video", "video/quicktime"), ".webm": ("video", "video/webm"),
    ".mp3": ("audio", "audio/mpeg"), ".wav": ("audio", "audio/wav"), ".m4a": ("audio", "audio/mp4"),
    ".ogg": ("audio", "audio/ogg"), ".aac": ("audio", "audio/aac"), ".flac": ("audio", "audio/flac"),
}


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS material_folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
        parent_id INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS material_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
        file_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        media_type TEXT NOT NULL, mime_type TEXT NOT NULL, size INTEGER NOT NULL,
        folder_id INTEGER NOT NULL DEFAULT 0, registered INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL)""")


def directory():
    return commander.DATA_DIR / "material-media"


def integer(value, maximum=10**12):
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,13}", value):
        number = int(value)
    else:
        raise commander.ChatError("编号或分页参数无效。")
    if not 0 <= number <= maximum:
        raise commander.ChatError("编号或分页参数无效。")
    return number


def name_from(value):
    if not isinstance(value, str):
        raise commander.ChatError("请填写名称。")
    value = value.strip()
    if not value or len(value) > 255 or not value.isprintable() or value in {".", ".."}:
        raise commander.ChatError("名称无效。")
    return value


def upload(content_type, body):
    try:
        header = content_type.encode("ascii")
    except UnicodeError as error:
        raise commander.ChatError("上传格式无效。") from error
    if b"\n" in header or b"\r" in header or len(header) > 512:
        raise commander.ChatError("上传格式无效。")
    message = BytesParser(policy=policy.default).parsebytes(b"Content-Type: " + header + b"\r\n\r\n" + body)
    if not message.is_multipart() or message.defects:
        raise commander.ChatError("上传格式无效。")
    files = [part for part in message.iter_parts()
             if part.get_param("name", header="content-disposition") == "file"]
    if len(files) != 1 or files[0].is_multipart():
        raise commander.ChatError("每次请求应上传一个文件。")
    raw = files[0].get_payload(decode=True)
    if not raw or len(raw) > MAX_FILE:
        raise commander.ChatError("请上传大小在 32 MB 以内的非空文件。")
    filename = name_from((files[0].get_filename() or "").replace("\\", "/").rsplit("/", 1)[-1])
    media_type, mime = MEDIA_TYPES.get(PurePosixPath(filename).suffix.lower(),
                                     ("document", "application/octet-stream"))
    # Active documents always download. Inline media uses server-assigned MIME types.
    folder = directory()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = uuid4().hex
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        expired = db.execute(
            "SELECT file_key FROM material_files WHERE registered=0 AND created_at<? AND tenant_id=?",
            (cutoff, commander.tenant())).fetchall()
        for record in expired:
            (folder / record["file_key"]).unlink(missing_ok=True)
        db.execute("DELETE FROM material_files WHERE registered=0 AND created_at<? AND tenant_id=?",
                   (cutoff, commander.tenant()))
        total = db.execute("SELECT coalesce(sum(size),0) FROM material_files WHERE tenant_id=?",
                           (commander.tenant(),)).fetchone()[0]
        if total + len(raw) > MAX_STORAGE:
            raise commander.ChatError("素材库空间不足，请清理旧文件。")
        target = folder / key
        try:
            with target.open("xb") as output:
                output.write(raw)
            db.execute("""INSERT INTO material_files (tenant_id,file_key,name,media_type,mime_type,size,created_at)
                          VALUES (?,?,?,?,?,?,?)""",
                       (commander.tenant(), key, filename, media_type, mime, len(raw), now))
            db.commit()
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    return {"url": "/material-media/" + key}


def connection_file(key, registered_only=True):
    with commander.database() as db:
        initialize(db)
        if registered_only:
            return db.execute(
                "SELECT * FROM material_files WHERE file_key=? AND registered=1 AND tenant_id=?",
                (key, commander.tenant())).fetchone()
        return db.execute("SELECT * FROM material_files WHERE file_key=? AND tenant_id=?",
                          (key, commander.tenant())).fetchone()


def uploaded_path(key):
    """Local path for a just-uploaded image, including not-yet-registered files."""
    with commander.database() as db:
        initialize(db)
        row = db.execute("SELECT 1 FROM material_files WHERE file_key=? AND tenant_id=?",
                         (key, commander.tenant())).fetchone()
    path = directory() / key
    return path if row and path.is_file() else None


def item(row):
    url = "/material-media/" + row["file_key"]
    return {**{key: row[key] for key in ("id", "name", "media_type", "mime_type", "size", "folder_id", "created_at")},
            "url": url, "thumbnail": url if row["media_type"] == "image" else "", "origin": "material-library"}


def read(path, query):
    args = {key: options[0] for key, options in query.items()}
    with commander.database() as db:
        initialize(db)
        if path == "/api/material/folders":
            entries = db.execute("""SELECT f.*, (SELECT COUNT(*) FROM material_files m
                             WHERE m.folder_id=f.id AND m.registered=1 AND m.tenant_id=f.tenant_id) AS item_count
                             FROM material_folders f WHERE f.tenant_id=? ORDER BY f.id""",
                                 (commander.tenant(),)).fetchall()
            return {"list": [dict(row) for row in entries]}
        folder = integer(args.get("folder_id", args.get("id", "0"))) if (
            "folder_id" in args or path.endswith("/items")
        ) else -1
        page = max(1, integer(args.get("page", "1"), 100000))
        limit = max(1, integer(args.get("page_size", args.get("pageSize", "20")), 100))
        kind = args.get("media_type", "")
        sort = {"time_asc": "id ASC", "name_asc": "name COLLATE NOCASE ASC,id",
                "name_desc": "name COLLATE NOCASE DESC,id", "size_asc": "size ASC,id",
                "size_desc": "size DESC,id"}.get(args.get("sort"), "id DESC")
        where = "WHERE registered=1 AND tenant_id=? AND (?=-1 OR folder_id=?) AND (?='' OR media_type=?)"
        filters = (commander.tenant(), folder, folder, kind, kind)
        total = db.execute(f"SELECT count(*) FROM material_files {where}", filters).fetchone()[0]
        rows = db.execute(f"SELECT * FROM material_files {where} ORDER BY {sort} LIMIT ? OFFSET ?",
                          (*filters, limit, (page - 1) * limit)).fetchall()
        return {"list": [item(row) for row in rows], "total": total}


def check_folder(db, folder_id):
    if folder_id and not db.execute("SELECT 1 FROM material_folders WHERE id=? AND tenant_id=?",
                                    (folder_id, commander.tenant())).fetchone():
        raise commander.ChatError("文件夹不存在。")


def write(path, body):
    action = path.rsplit("/", 1)[-1]
    if action == "credential":
        return {"provider": "local"}
    file_to_delete = None
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        if path == "/api/material/add":
            url = body.get("url")
            match = FILE_PATH.fullmatch(url) if isinstance(url, str) else None
            if not match:
                raise commander.ChatError("请选择已上传的文件。")
            row = db.execute("SELECT * FROM material_files WHERE file_key=? AND tenant_id=?",
                             (match[1], commander.tenant())).fetchone()
            if row is None:
                raise commander.ChatError("上传文件已失效，请重新选择。")
            if not row["registered"]:
                folder = integer(body.get("folder_id") or 0)
                check_folder(db, folder)
                db.execute("UPDATE material_files SET folder_id=?,registered=1 WHERE id=? AND tenant_id=?",
                           (folder, row["id"], commander.tenant()))
                row = db.execute("SELECT * FROM material_files WHERE id=?", (row["id"],)).fetchone()
            return item(row)
        if path == "/api/material/move":
            ids = body.get("ids")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 200:
                raise commander.ChatError("请选择要移动的素材。")
            folder = integer(body.get("folder_id") or 0)
            check_folder(db, folder)
            changed = sum(db.execute(
                "UPDATE material_files SET folder_id=? WHERE id=? AND registered=1 AND tenant_id=?",
                (folder, value, commander.tenant())).rowcount for value in dict.fromkeys(map(integer, ids)))
            return {"moved": changed}
        if path == "/api/material/delete":
            key = integer(body.get("id"))
            row = db.execute("SELECT file_key FROM material_files WHERE id=? AND registered=1 AND tenant_id=?",
                             (key, commander.tenant())).fetchone()
            if row:
                db.execute("DELETE FROM material_files WHERE id=? AND tenant_id=?",
                           (key, commander.tenant()))
                file_to_delete = directory() / row["file_key"]
        elif action == "create":
            parent = integer(body.get("parent_id") or 0)
            check_folder(db, parent)
            key = db.execute(
                "INSERT INTO material_folders (tenant_id,name,parent_id) VALUES (?,?,?)",
                (commander.tenant(), name_from(body.get("name")), parent)).lastrowid
            return {"id": key}
        else:
            key = integer(body.get("id"))
            if not key:
                raise commander.ChatError("不能修改根目录。")
            check_folder(db, key)
            if action == "rename":
                db.execute("UPDATE material_folders SET name=? WHERE id=? AND tenant_id=?",
                           (name_from(body.get("name")), key, commander.tenant()))
            elif action == "move":
                parent = integer(body.get("parent_id") or 0)
                check_folder(db, parent)
                ancestor = parent
                while ancestor:
                    if ancestor == key:
                        raise commander.ChatError("不能把文件夹移到自身或下级文件夹。")
                    ancestor = db.execute(
                        "SELECT parent_id FROM material_folders WHERE id=? AND tenant_id=?",
                        (ancestor, commander.tenant())).fetchone()[0]
                db.execute("UPDATE material_folders SET parent_id=? WHERE id=? AND tenant_id=?",
                           (parent, key, commander.tenant()))
            else:
                parent = db.execute("SELECT parent_id FROM material_folders WHERE id=? AND tenant_id=?",
                                    (key, commander.tenant())).fetchone()[0]
                db.execute("UPDATE material_folders SET parent_id=? WHERE parent_id=? AND tenant_id=?",
                           (parent, key, commander.tenant()))
                db.execute("UPDATE material_files SET folder_id=? WHERE folder_id=? AND tenant_id=?",
                           (parent, key, commander.tenant()))
                db.execute("DELETE FROM material_folders WHERE id=? AND tenant_id=?",
                           (key, commander.tenant()))
    if file_to_delete:
        file_to_delete.unlink(missing_ok=True)
    return {"ok": True}
