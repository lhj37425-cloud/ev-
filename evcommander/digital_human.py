"""Digital-human script drafting/translation using the configured text model.

These two endpoints do not require a TTS/avatar/video provider, so they are
safe to implement while AI video generation is deferred.
"""

import json
import re

import commander
import payments


PROPOSE = "/api/ext/digital-human/script/propose"
TRANSLATE = "/api/ext/digital-human/script/translate"
LANGUAGES = {
    "英语": "English", "日语": "Japanese", "韩语": "Korean", "法语": "French",
    "德语": "German", "西班牙语": "Spanish", "俄语": "Russian", "泰语": "Thai",
    "越南语": "Vietnamese", "阿拉伯语": "Arabic", "繁体中文": "Traditional Chinese",
}


def _aliases(path):
    return {path, "/agent-api" + path, "/agent-api" + path[len("/api"):]}


WRITE_PATHS = _aliases(PROPOSE) | _aliases(TRANSLATE)


def canonical(path):
    if path.startswith("/agent-api"):
        path = path[len("/agent-api"):]
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _proposals(raw, count):
    raw = raw.strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            items = [str(item).strip() for item in parsed if str(item).strip()]
            if items:
                return items[:count]
        if isinstance(parsed, dict):
            items = parsed.get("proposals")
            if isinstance(items, list):
                items = [str(item).strip() for item in items if str(item).strip()]
                if items:
                    return items[:count]
    except ValueError:
        pass
    parts = [part.strip(" -\t") for part in re.split(r"\n{2,}|(?m)^\s*\d+[.、)]\s*", raw)]
    parts = [part for part in parts if part]
    return (parts or [raw])[:count]


def propose(body):
    topic = _text(body.get("topic"), 300)
    if not topic:
        raise commander.ChatError("请输入口播主题。")
    style = _text(body.get("style"), 40) or "自然亲和"
    try:
        duration = max(0, min(600, int(body.get("duration") or 0)))
        count = max(1, min(5, int(body.get("count") or 3)))
    except (TypeError, ValueError) as error:
        raise commander.ChatError("口播参数无效。") from error
    system = (
        "你是短视频口播文案策划。只输出 JSON 数组，不要 Markdown 代码块或解释。"
        "数组每个元素是一条可直接朗读的中文口播稿，句子短、自然、适合短视频。"
    )
    user = (
        f"主题：{topic}\n风格：{style}\n"
        + (f"目标时长：约 {duration} 秒\n" if duration else "")
        + f"请给出 {count} 条不同角度的口播稿。"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    usage = {}
    bill_id = payments.chat_reserve(messages, 1800, "数字人口播稿")
    try:
        raw = "".join(commander.generate(
            messages, max_output_tokens=1800, usage=usage)).strip()
    except commander.ChatError:
        payments.refund(bill_id)
        raise
    proposals = _proposals(raw, count)
    if not proposals:
        payments.refund(bill_id)
        raise commander.ChatError("模型没有生成口播稿，请稍后重试。")
    payments.settle_chat(bill_id, usage, messages, raw, "数字人口播稿")
    return {"ok": True, "proposals": proposals}


def translate(body):
    text = _text(body.get("text"), 6000)
    if not text:
        raise commander.ChatError("请先填写要翻译的话术。")
    target = _text(body.get("target_lang"), 40)
    target_name = LANGUAGES.get(target, target or "English")
    system = (
        f"你是专业翻译。把用户提供的中文口播稿翻译成{target_name}。"
        "只输出译文，不要解释、不要加引号、不要 Markdown。保留原意和口语节奏。"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]
    usage = {}
    bill_id = payments.chat_reserve(messages, 2400, "数字人翻译")
    try:
        translated = "".join(commander.generate(
            messages, max_output_tokens=2400, usage=usage)).strip()
    except commander.ChatError:
        payments.refund(bill_id)
        raise
    if not translated:
        payments.refund(bill_id)
        raise commander.ChatError("模型没有返回译文，请稍后重试。")
    payments.settle_chat(bill_id, usage, messages, translated, "数字人翻译")
    return {"ok": True, "text": translated}


def write(path, body):
    path = canonical(path)
    if path == PROPOSE:
        return propose(body)
    if path == TRANSLATE:
        return translate(body)
    raise commander.ChatError("数字人接口暂不可用。")
