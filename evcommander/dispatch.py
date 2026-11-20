"""Allowlisted page actions and local Commander plans."""

import json
import re

import commander
from commander import ChatError, database
import studio


PLAN_STEP = "/agent-api/api/ext/commander/plan/step"


def load_catalog(root):
    source = (root / "assets/homeShowcase-BaxN04LZ.js").read_text(encoding="utf-8")
    catalog = dict(re.findall(r'label:"([^"]+)"[^{}]{0,200}?to:"([^"]+)"', source))
    catalog.update({
        "首页": "/mb/home", "资产中心": "/mb/works", "灵感库": "/mb/works?tab=idea",
        "智能知识库": "/mb/kb", "设备农场": "/mb/device", "WhatsApp 值守": "/mb/global/whatsapp",
    })
    return catalog


def resolve(text, catalog):
    if studio.matches_dispatch(text):
        return studio.action(text)
    if re.search(r"不要|不用|不必|无需|如何|怎么", text):
        return None
    match = re.fullmatch(r"(?:请|帮我|我想|我要|\s)*(?:打开|进入|跳到|切到|带我去|去)\s*[「“]?(.*?)[」”了吗。！!\s]*", text)
    if not match:
        return None
    label = match.group(1).strip()
    for suffix in ("页面", "界面", "功能"):
        label = label.removesuffix(suffix)
    route = catalog.get(label)
    if not route:
        return None
    return {"name": label, "title": f"打开{label}", "route": route, "exec": "frontend", "auto_open": True}


def actions(text, catalog):
    parts = re.split(r"[，,；;。]?\s*(?:然后|接着|之后|再(?=打开|进入|跳到|写))", text)
    if len(parts) == 1:
        action = resolve(text, catalog)
        return [action] if action else []
    if len(parts) > 4:
        return []
    steps = [resolve(part.strip().removeprefix("先"), catalog) for part in parts]
    return steps if all(steps) else []


def ensure_plans(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS dispatch_plans (
           id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
           steps TEXT NOT NULL)"""
    )


def create_plan(conversation_id, actions):
    steps = [
        {**action, "seq": index, "status": "ready" if index == 1 else "pending",
         "prefill": bool(action.get("params")), "autoStart": action["route"] == studio.ROUTE and bool(action.get("params"))}
        for index, action in enumerate(actions, 1)
    ]
    with database() as connection:
        ensure_plans(connection)
        cursor = connection.execute(
            "INSERT INTO dispatch_plans (tenant_id, conversation_id, steps) VALUES (?, ?, ?)",
            (commander.tenant(), conversation_id, json.dumps(steps, ensure_ascii=False)),
        )
        return {"task_id": cursor.lastrowid, "title": "本地调度步骤", "steps": steps}


def attach_saved_plans(conversation):
    ids = {message["local_plan_id"] for message in conversation["messages"] if message.get("local_plan_id")}
    if not ids:
        return conversation
    with database() as connection:
        ensure_plans(connection)
        for message in conversation["messages"]:
            if message.get("local_plan_id") not in ids:
                continue
            plan_id = message["local_plan_id"]
            row = connection.execute(
                "SELECT steps FROM dispatch_plans WHERE id=? AND conversation_id=? AND tenant_id=?",
                (plan_id, conversation["id"], commander.tenant())
            ).fetchone()
            if row:
                message["local_plan"] = {
                    "task_id": plan_id, "title": "本地调度步骤", "steps": json.loads(row["steps"]),
                }
    return conversation


def update_step(body):
    plan_id, sequence, status = body.get("task_id"), body.get("seq"), body.get("status")
    if type(plan_id) is not int or type(sequence) is not int or status not in ("done", "skipped"):
        raise ChatError("步骤变更无效。")
    with database() as connection:
        ensure_plans(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT steps FROM dispatch_plans WHERE id=? AND tenant_id=?",
                                 (plan_id, commander.tenant())).fetchone()
        if not row:
            raise ChatError("这个本地调度计划已不存在。")
        steps = json.loads(row["steps"])
        active = next((step for step in steps if step["status"] == "ready"), None)
        selected = next((step for step in steps if step["seq"] == sequence), None)
        if selected is None or (selected is not active and selected["status"] != status):
            raise ChatError("请先处理当前步骤。")
        if selected is active:
            selected["status"] = status
            active = next((step for step in steps if step["status"] == "pending"), None)
            if active:
                active["status"] = "ready"
            connection.execute("UPDATE dispatch_plans SET steps=? WHERE id=? AND tenant_id=?",
                               (json.dumps(steps), plan_id, commander.tenant()))
        return {"steps": steps, "finished": active is None, "next": active}
