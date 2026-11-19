"""The Commander memory panel and explicitly saved conversation preferences."""

import re
from datetime import datetime, timezone

from commander import ChatError, database


PREFIX = "/agent-api/api/memory/"
READ_PATHS = {PREFIX + "list", PREFIX + "export"}
WRITE_PATHS = {PREFIX + action for action in ("add", "update", "delete", "clear", "compact", "capture")}


def table(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS commander_memory (
           id INTEGER PRIMARY KEY, content TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
           salience INTEGER NOT NULL, updated_at TEXT NOT NULL)"""
    )


def listing(limit=100):
    with database() as connection:
        table(connection)
        rows = connection.execute(
            "SELECT * FROM commander_memory ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def bullets(items):
    return "\n".join(f"- {item['content']}" for item in items)


def read(path):
    items = listing()
    if path.endswith("/export"):
        return {"markdown": "# 我的 AI 长期记忆\n\n" + bullets(items)}
    return {"items": items, "summary": bullets(items[:8])}


def content_text(content):
    if not isinstance(content, str) or not content.strip() or len(content) > 2000:
        raise ChatError("记忆内容为空或过长。")
    return content.strip()


def save(content, kind="fact", salience=4):
    content = content_text(content)
    with database() as connection:
        table(connection)
        inserted = connection.execute(
            "INSERT OR IGNORE INTO commander_memory (content,kind,salience,updated_at) VALUES (?,?,?,?)",
            (content,kind,salience,datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ).rowcount
        row = connection.execute("SELECT * FROM commander_memory WHERE content=?", (content,)).fetchone()
        return dict(row), bool(inserted)


def write(path, body):
    operation = path.removeprefix(PREFIX)
    if operation == "capture":
        text = body.get("text", "")
        match = re.match(r"^(?:请(?:你)?|帮我)?\s*(?:请记住|记住|记下|记一下|牢记)[：:,，\s]*(.+)", text) if isinstance(text, str) else None
        if not match:
            return {"captured": False}
        item, inserted = save(match[1])
        return {"captured": True, "items": [item], "memory": {"inserted": int(inserted), "bumped": int(not inserted)}}
    if operation == "add":
        item, _ = save(body.get("content"))
        return {"ok": True, "item": item}
    if operation == "compact":
        return {"ok": False, "error": "现有记忆已自动去重。"}
    key = body.get("id")
    if operation != "clear" and type(key) is not int:
        raise ChatError("记忆编号无效。")
    with database() as connection:
        table(connection)
        if operation == "clear":
            connection.execute("DELETE FROM commander_memory")
        elif operation == "delete":
            connection.execute("DELETE FROM commander_memory WHERE id=?", (key,))
        else:
            connection.execute(
                "UPDATE OR IGNORE commander_memory SET content=?, updated_at=? WHERE id=?",
                (content_text(body.get("content")), datetime.now(timezone.utc).isoformat(timespec="seconds"), key),
            )
    return {"ok": True}


def context(messages):
    saved = listing(8)
    if not saved:
        return messages
    preferences = {
        "role": "system",
        "content": "以下是当前用户明确保存的偏好和信息。只在与当前问题相关时参考。\n" + bullets(saved),
    }
    return [messages[0], preferences, *messages[1:]]
