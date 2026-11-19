"""Locally stored drafts for the archived inspiration picker."""

from datetime import datetime, timezone
import json

from commander import ChatError, database


PREFIX = "/agent-api/api/ext/inspiration/"
LIST = PREFIX + "list"
CREATE = PREFIX + "create"


def table(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS inspirations (
           id INTEGER PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
           kind TEXT NOT NULL, kind_label TEXT NOT NULL, payload TEXT NOT NULL,
           source_id INTEGER, source_label TEXT NOT NULL, created_at TEXT NOT NULL)"""
    )


def save(title, content, kind, kind_label, payload, source_label, source_id=None):
    if not isinstance(content, str) or not content.strip() or len(content) > 30000:
        raise ChatError("灵感内容为空或过长。")
    title = title.strip()[:80] if isinstance(title, str) else ""
    title = title or content.splitlines()[0].lstrip("# ")[:80]
    with database() as connection:
        table(connection)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id FROM inspirations WHERE content=? AND kind=? AND payload=?",
            (content, kind, json.dumps(payload, ensure_ascii=False)),
        ).fetchone()
        if existing:
            return {"ok": True, "id": existing["id"], "existed": True}
        cursor = connection.execute(
            "INSERT INTO inspirations (title,content,kind,kind_label,payload,source_id,source_label,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (title,content,kind,kind_label,json.dumps(payload, ensure_ascii=False),source_id,source_label,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        return {"ok": True, "id": cursor.lastrowid}


def item(row):
    result = dict(row)
    result["summary"] = result["content"].replace("\n", " ")[:130]
    result["payload"] = json.loads(result["payload"])
    return result


def listing(query):
    def number(key, default, maximum):
        try:
            return max(1, min(int(query.get(key, [default])[0]), maximum))
        except (ValueError, TypeError):
            return default

    page, size = number("page", 1, 1000), number("page_size", 20, 100)
    kind = query.get("kind", [""])[0]
    keyword = query.get("keyword", [""])[0][:100]
    rules = "WHERE (?='' OR kind=?) AND (instr(lower(title), lower(?))>0 OR instr(lower(content), lower(?))>0)"
    filters = (kind, kind, keyword, keyword)
    with database() as connection:
        table(connection)
        rows = connection.execute(
            f"SELECT * FROM inspirations {rules} ORDER BY id DESC LIMIT ? OFFSET ?", (*filters,size,(page-1)*size)
        ).fetchall()
        total = connection.execute(f"SELECT count(*) FROM inspirations {rules}", filters).fetchone()[0]
        kinds = connection.execute(
            "SELECT kind AS value, kind AS key, min(kind_label) AS label, count(*) AS count"
            " FROM inspirations GROUP BY kind"
        ).fetchall()
        return {"list": [item(row) for row in rows], "total": total, "facets": {"kinds": [dict(row) for row in kinds]}}


def detail(key):
    with database() as connection:
        table(connection)
        row = connection.execute("SELECT * FROM inspirations WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ChatError("这条本地灵感不存在。")
        return {"item": item(row)}
