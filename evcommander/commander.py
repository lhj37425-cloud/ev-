"""Local inference and conversation history for AgentMax Commander."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import llm_provider
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit
from uuid import UUID, uuid4


LOCAL_TOKEN = "heph-local-preview"
MODEL = llm_provider.model()
MODEL_LABEL = llm_provider.label()
CHAT_CONFIG = {
    "models": [{"code": MODEL, "brand": llm_provider.brand(), "alias": MODEL_LABEL, "supports_vision": False}],
    "default_model": MODEL,
    "enable_attachment": False,
    "enable_web_search": False,
    "enable_voice": False,
    "enable_voice_input": False,
}
COMMAND_STREAM = "/agent-api/api/ext/talent/agent/command/stream"
COMMAND_PROGRESS = "正在分析需求并调度数字员工…"
DATA_DIR = Path(os.environ["EV_DATA_DIR"]) if os.environ.get("EV_DATA_DIR") else Path(__file__).resolve().parent / ".local"


class ChatError(ValueError):
    pass


def conversation_id(value=None):
    if not value:
        return uuid4().hex
    if not isinstance(value, str) or len(value) > 36:
        raise ChatError("会话编号无效。请新建对话后重试。")
    try:
        return UUID(value).hex
    except ValueError as error:
        raise ChatError("会话编号无效。请新建对话后重试。") from error


def parse_chat(body, command):
    if body.get("attachments") or body.get("web_search"):
        raise ChatError("指挥官当前支持纯文本对话和本地知识库。请移除附件或关闭联网搜索后重试。")
    requested = body.get("provider" if command else "model")
    if requested and requested != MODEL:
        raise ChatError("请刷新页面并选择已连接的模型后重试。")
    messages = body.get("history", []) if command else body.get("messages")
    if not isinstance(messages, list):
        raise ChatError("对话消息格式无效。")
    if command:
        messages = [*messages, {"role": "user", "content": body.get("input")}]
    clean = []
    for item in messages[-17:]:
        if not isinstance(item, dict) or item.get("role") not in {"system", "user", "assistant"}:
            raise ChatError("对话消息格式无效。")
        content = item.get("content")
        if not isinstance(content, str) or len(content) > 12000:
            raise ChatError("单条消息过长或格式无效。")
        if content.strip():
            clean.append({"role": item["role"], "content": content})
    if not clean or clean[-1]["role"] != "user":
        raise ChatError("请输入一句话后再发送。")
    system = "你是 EV指挥官。用用户使用的语言回答，回答具体、自然。"
    if command:
        system += "你可以用文字协助分析和规划。你没有执行外部任务的能力，不要声称已经执行。"
    context = [{"role": "system", "content": system}, *clean]
    return conversation_id(body.get("conversation_id")), context, clean[-1]["content"]


def read_openai_key():
    if os.environ.get("EV_TRIAL_ORIGIN"):
        try:
            key = Path(os.environ["EV_OPENAI_KEY_FILE"]).read_text(encoding="utf-8").strip()
        except (KeyError, OSError) as error:
            raise ChatError("试用环境尚未配置模型。") from error
        if not key or any(c.isspace() for c in key):
            raise ChatError("试用环境的模型配置无效。")
        return key
    provider_db = Path.home() / ".cc-switch" / "cc-switch.db"
    try:
        with closing(sqlite3.connect(f"{provider_db.as_uri()}?mode=ro", uri=True)) as connection:
            row = connection.execute(
                "SELECT settings_config FROM providers WHERE id=? AND app_type=?", ("openai", "pi")
            ).fetchone()
        profile = json.loads(row[0]) if row else {}
        endpoint = urlsplit(profile.get("baseUrl", ""))
        key = profile.get("apiKey")
    except (sqlite3.Error, TypeError, ValueError, AttributeError) as error:
        raise ChatError("无法读取 CC Switch 的 OpenAI 官方配置。") from error
    if (
        endpoint.scheme != "https" or endpoint.netloc != "api.openai.com"
        or endpoint.path.rstrip("/") != "/v1" or not isinstance(key, str)
        or not key.strip() or "\r" in key or "\n" in key
    ):
        raise ChatError("CC Switch 的 OpenAI 官方 API 配置不可用。")
    return key.strip()


def generate(messages, *, max_output_tokens=1024):
    if os.environ.get("EV_TRIAL_ORIGIN"):
        reserve_trial_request()
    try:
        yield from llm_provider.stream(messages, max_output_tokens=max_output_tokens)
    except llm_provider.ProviderError as error:
        raise ChatError(str(error)) from error


def reserve_trial_request():
    day = datetime.now(timezone.utc).date().isoformat()
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS trial_usage (day TEXT PRIMARY KEY, count INTEGER NOT NULL)"
        )
        connection.execute("DELETE FROM trial_usage WHERE day < ?", (day,))
        updated = connection.execute(
            "INSERT INTO trial_usage VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET count = count + 1 WHERE count < 40",
            (day,),
        )
        if updated.rowcount != 1:
            raise ChatError("今天的试用模型额度已用完。")


@contextmanager
def database():
    DATA_DIR.mkdir(mode=0o700, exist_ok=True)
    with closing(sqlite3.connect(DATA_DIR / "commander.sqlite3", timeout=8)) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source TEXT NOT NULL,
                messages TEXT NOT NULL
            )""")
            yield connection


def save_turn(key, user_text, reply_text, source, local_plan_id=None, wizard=None):
    updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT messages FROM conversations WHERE id=?", (key,)).fetchone()
        messages = json.loads(row["messages"]) if row else []
        answer = {"role": "assistant", "content": reply_text}
        if local_plan_id is not None:
            answer["local_plan_id"] = local_plan_id
        if wizard is not None:
            answer["local_wizard"] = wizard
        messages.extend([{"role": "user", "content": user_text}, answer])
        connection.execute(
            """INSERT INTO conversations VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, messages=excluded.messages""",
            (key, user_text.strip()[:40], updated, source, json.dumps(messages, ensure_ascii=False)),
        )


def history(source, keyword):
    with database() as connection:
        rows = connection.execute(
            """SELECT id, title, updated_at FROM conversations
               WHERE source=? AND instr(lower(title), lower(?)) > 0
               ORDER BY updated_at DESC, rowid DESC LIMIT 100""",
            (source, keyword),
        )
        return {"list": [dict(row) for row in rows]}


def load_conversation(key):
    with database() as connection:
        row = connection.execute("SELECT * FROM conversations WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ChatError("这个本地会话已不存在。")
        return {"id": row["id"], "model_code": MODEL, "messages": json.loads(row["messages"])}


def delete_conversation(key):
    with database() as connection:
        connection.execute("DELETE FROM conversations WHERE id=?", (key,))
