"""Local content drafting using the original studio's request format."""

import re
from pathlib import Path

from commander import ChatError


ROUTE = "/mb/creative/studio"
GUIDE_PATH = "/agent-api/api/creative-dept/studio/guide"
STREAM_PATH = "/agent-api/api/creative-dept/studio/stream"
SAMPLE_TOPIC = "周末松弛感"
BRAIN_ASSET = "/assets/Brain-COTF_N10.js"
STUDIO_ASSET = "/assets/Creative-ClDZqVc2.js"


def load_frontend_assets(root):
    """Adapt the pinned local build without modifying its archived JavaScript."""
    brain = [
        (
            'const e=_n(t.params,void 0,bn(t.needs));if(t.locked)',
            f'const e=_n(t.params,void 0,bn(t.needs));'
            f'if(t.autoOpen&&t.route==="{ROUTE}")e.autoStart=!0;if(t.locked)',
        ),
        (
            'alternatives:Array.isArray(o.alternatives)?o.alternatives:[]},K()',
            'alternatives:Array.isArray(o.alternatives)?o.alternatives:[],autoOpen:o.auto_open===!0},K()',
        ),
        (
            '.catch(()=>{})},onError:o=>{r.content||(r.content="指挥调度失败：',
            '.catch(()=>{});if(r.action?.autoOpen&&qe.currentRoute.value.path==="/mb/brain")ko(r.action)'
            '},onError:o=>{r.content||(r.content="指挥调度失败：',
        ),
        (
            'u.taskId=String(t.task_id),u.stepSeq=e.seq,u.stepTitle=e.title,u.autoReport=!1,At(e.route,u)',
            f'if(e.autoStart&&e.route==="{ROUTE}")u.autoStart=!0;'
            'u.taskId=String(t.task_id),u.stepSeq=e.seq,u.stepTitle=e.title,u.autoReport=!1,At(e.route,u)',
        ),
        (
            'empName:r.emp_name||void 0};if(r.role==="assistant"',
            'empName:r.emp_name||void 0};if(r.local_plan)n.plan=r.local_plan;'
            'if(r.local_wizard)n.wizard={kind:"result",result:r.local_wizard};if(r.role==="assistant"',
        ),
        (
            'v.persist(`【起号向导】${D}`,w.content)',
            'v.persist(`【起号向导】${D}`,w.content,T)',
        ),
        (
            'persist:(t,e)=>{e&&re.persistSkill({conversation_id:W.value||void 0,user_text:t,'
            'assistant_text:e,skill_label:"起号向导",source:J.value||void 0})',
            'persist:(t,e,r)=>{e&&re.persistSkill({conversation_id:W.value||void 0,user_text:t,'
            'assistant_text:e,skill_label:"起号向导",source:J.value||void 0,'
            'project_id:r?.project_id,board_date:r?.board_date})',
        ),
        (
            '指挥官会在与你的对话中自动记住关于你的稳定信息（业务/偏好/在做的事/表达风格）， 并结合你的知识库资料，下次自动据此更懂你、更省 token。在任何对话框里说「记住…」「以后都…」， 或指出 AI 哪里说错了，也会记在这里。记忆按账号隔离，仅你可见。你可以在这里查看、补充或删除。',
            '在这里添加偏好，或在指挥官里说「记住…」。记忆保存在本机，后续对话会将最近保存的记忆提供给当前模型。你可以查看、删除或导出记忆。',
        ),
        (
            '在对话里说「记住…」后，这些要求会同时存进你的知识库「我的偏好与习惯」，可以在那里修改"+m(cn.value?"，或分享给企业同事":"")+"。',
            '本地记忆在当前面板管理。',
        ),
        ('还没有记忆。多和指挥官聊聊，它会自动学习了解你。', '还没有记忆。添加一条偏好，或告诉指挥官「记住…」。'),
        ('长期记忆：AI 记住并理解你（自动学习 · 可编辑 · 可导出）', '长期记忆：保存、查看和导出偏好'),
        ('勾选整库作为参考（想引用单个文件/图片，请用「附件 → 知识库文件」）', '勾选本地知识库，使用其中已确认的文本资料。'),
    ]
    studio = [
        (
            'function it(){var q,z;const k=Ps(fo);',
            'let hephDraft="",hephReady=!1;'
            'function hephStartDraft(){if(!hephReady||A.value||!hephDraft)return;'
            'const text=hephDraft;hephDraft="";G.value={};f.value="";'
            'T.value=!1;b.value=0;V.value=text;void gt()}'
            'Zt(A,busy=>{if(!busy)hephStartDraft()});'
            'function it(){var q,z;const k=Ps(fo);',
        ),
        (
            'w=String(k.prompt||k.text||"").trim();x&&',
            'w=String(k.prompt||k.text||"").trim();'
            'if(k.autoStart===!0&&w){hephDraft=w;V.value=w;queueMicrotask(hephStartDraft);return}x&&',
        ),
        (
            'rt("内容创作台暂时用不了，请稍后重试。",{error:!0})}});',
            'rt("内容创作台暂时用不了，请稍后重试。",{error:!0})}'
            'finally{hephReady=!0;hephStartDraft()}});',
        ),
    ]
    daily = [
        ('return t&&Se.replace({path:$.path,query:{}}).catch(()=>{}),t', 'return t'),
        ('m.value=t.project_id,P.value=!1', 'm.value=t.project_id,P.value=!1,'
         'Se.replace({path:$.path,query:{...$.query,project:t.project_id}}).catch(()=>{})'),
    ]
    planning = [
        ('n&&(mt.replace({path:ht.path,query:{}}).catch(()=>{}),Be(),Bt(n))', 'n&&(Be(),Bt(n))'),
    ]
    assets = {}
    for path, replacements in [(BRAIN_ASSET, brain), (STUDIO_ASSET, studio),
                               ("/assets/Daily-CUAOghH_.js", daily), ("/assets/Planning-B8baVhZ9.js", planning)]:
        text = (Path(root) / path[1:]).read_text(encoding="utf-8")
        for original, replacement in replacements:
            if text.count(original) != 1:
                raise RuntimeError(f"Studio adapter does not match archived asset {path}.")
            text = text.replace(original, replacement, 1)
        assets[path] = text.encode("utf-8")
    return assets


GOALS = {
    "video_script": ("短视频脚本", "写出片头钩子，再按时间段列出画面、口播和字幕，最后给收尾。", "script"),
    "note": ("种草文案", "提供一个标题、正文和适合平台的话题标签。正文具体、有场景。", "post"),
    "live": ("直播口播", "按开场、讲解、互动、收尾排列可直接朗读的直播话术。", "live"),
    "voiceover": ("口播稿", "输出可直接朗读的口播稿，短句为主，在合适处标记停顿。", "voiceover"),
}
GOAL_PATTERNS = {
    "video_script": r"短视频(?:脚本|文案)|拍摄脚本|视频脚本|分镜脚本",
    "live": r"直播(?:间)?(?:口播|稿|话术|脚本)",
    "voiceover": r"口播(?:稿|文案|话术)|旁白稿|解说稿",
    "note": r"种草(?:文案|文|笔记)|文案|笔记|图文|帖子|推文",
}
PLATFORMS = {
    "xiaohongshu": ("小红书", r"小红书|xiaohongshu", "从具体生活场景入手，使用相关话题标签。"),
    "douyin": ("抖音", r"抖音|douyin", "前三秒给出钩子，节奏紧凑，适合竖屏。"),
    "wechat_channels": ("视频号", r"视频号|WeChat Channels", "讲解清楚，用容易转述的短句。"),
    "kuaishou": ("快手", r"快手|kuaishou", "口语化，强调真实感和实用信息。"),
    "bilibili": ("哔哩哔哩", r"哔哩哔哩|B站|bilibili", "信息密度高，有明确叙事或知识点。"),
    "wechat_article": ("公众号", r"公众号|WeChat Articles", "有清楚的段落层次和易读的小标题。"),
    "tiktok": ("TikTok", r"(?<![A-Za-z])TikTok(?![A-Za-z])", "开场直接，按竖屏短视频节奏组织。"),
    "instagram": ("Instagram", r"(?<![A-Za-z])Instagram(?![A-Za-z])", "围绕视觉亮点，配简洁说明。"),
    "youtube": ("YouTube", r"(?<![A-Za-z])YouTube(?![A-Za-z])", "突出视频看点，并给出节奏和结构。"),
    "facebook": ("Facebook", r"(?<![A-Za-z])Facebook(?![A-Za-z])", "以读者能参与的讨论切入，语言清晰。"),
}
AUDIENCES = {
    "mass": "大众消费者",
    "young": "年轻人 / 学生",
    "family": "宝妈 / 家庭",
    "business": "专业买家 / B端",
    "owners": "老板 / 创业者",
    "senior": "中老年",
}
TONES = {"share": "自然分享", "professional": "专业可信", "casual": "轻松活泼", "warm": "温暖真诚"}
MAX_PLATFORMS = 4
REQUEST_VERB = r"写|生成|起草|撰写|创作|来一篇|来一条|出一份|做一篇"


def platform_ids(text):
    matches = []
    for key, (_, pattern, _) in PLATFORMS.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            matches.append((match.start(), key))
    return [key for _, key in sorted(matches)][:MAX_PLATFORMS]


def goal_id(text):
    return next((key for key, pattern in GOAL_PATTERNS.items() if re.search(pattern, text)), "")


def named_choice(text, choices):
    return next((key for key, label in choices.items() if label in text), "")


def matches_dispatch(text):
    return (
        bool(goal_id(text))
        and bool(re.search(REQUEST_VERB, text))
        and not bool(re.search(r"怎么写|如何写|怎样写|为什么|不要|不用|不必|无需", text))
    )


def action(text):
    return {
        "capability_key": "bf_content_studio",
        "name": "内容创作台",
        "title": GOALS[goal_id(text)][0],
        "route": ROUTE,
        "exec": "frontend",
        "prefill": True,
        "params": {"prompt": text},
        "auto_open": True,
    }


def topic_for_request(text):
    topic = re.sub(r"^(?:请你|能不能|给我|帮我|我想|我要|请|\s)*", "", text)
    topic = re.sub(r"^(?:写|生成|起草|撰写|创作|做|出|来)(?:一篇|一条|一份|一个|一段)?", "", topic)
    marked = re.search(r"[，,。；;]\s*(?:主题[是为：:]|围绕|关于)\s*([^，,。；;]+)", topic)
    if marked:
        return marked.group(1).strip()[:500]
    for _, pattern, _ in PLATFORMS.values():
        topic = re.sub(pattern, " ", topic, flags=re.IGNORECASE)
    for pattern in GOAL_PATTERNS.values():
        topic = re.sub(pattern, " ", topic)
    for label in (*AUDIENCES.values(), *TONES.values()):
        topic = topic.replace(label, " ")
    topic = re.sub(r"分别发布到|发布到|适合发在|发到|发在|各写一篇|各写一条|各一篇|各一条|各一份|主题[是为：:]|围绕|关于", " ", topic)
    topic = re.sub(r"(?<!\S)(?:以及|同时|和|及|的|与)(?!\S)", " ", topic)
    topic = re.sub(r"\s+", " ", topic).strip(" ，,、。！？；的和与")
    topic = re.sub(r"^(?:给|为)(.+?)(?:写|生成|创作)(?:一篇|一条|一份|一个|一段)?$", r"\1", topic)
    return topic[:500]


def safe_slots(previous):
    if not isinstance(previous, dict):
        raise ChatError("文案需求格式无效。")
    slots = {}
    goal = previous.get("goal")
    if isinstance(goal, str) and goal in GOALS:
        slots["goal"] = goal
    platforms = previous.get("platform")
    if isinstance(platforms, list):
        slots["platform"] = selected_platforms(platforms)
    for key, size in (("topic", 500), ("audience", 100), ("tone", 100), ("extra", 12000)):
        value = previous.get(key)
        if isinstance(value, str) and value.strip():
            slots[key] = value.strip()[:size]
    return slots


def selected_platforms(values):
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value in PLATFORMS))[:MAX_PLATFORMS]


def update_slots(slots, text, picked, value):
    if picked == "platform":
        slots["platform"] = selected_platforms(value if isinstance(value, list) else [value])
    elif isinstance(picked, str) and picked in {"goal", "audience", "tone"}:
        options = {"goal": GOALS, "audience": AUDIENCES, "tone": TONES}[picked]
        if isinstance(value, str) and value in options:
            slots[picked] = value
    elif text:
        draft_request = matches_dispatch(text)
        found_goal = goal_id(text) if draft_request or "goal" not in slots or "改成" in text else ""
        found_platforms = platform_ids(text)
        found_audience, found_tone = named_choice(text, AUDIENCES), named_choice(text, TONES)
        if found_goal:
            slots["goal"] = found_goal
        if found_platforms:
            extra_platforms = slots.get("platform", []) if re.search(r"再加|也发|加上|同时", text) else []
            slots["platform"] = selected_platforms([*extra_platforms, *found_platforms])
        if found_audience:
            slots["audience"] = found_audience
        if found_tone:
            slots["tone"] = found_tone
        if draft_request:
            topic = topic_for_request(text)
            if topic:
                slots["topic"] = topic
            elif not slots.get("topic") and text.strip("。！! ") == "帮我写一篇小红书文案":
                slots["topic"] = SAMPLE_TOPIC
            if slots.get("topic") and slots.get("platform"):
                slots.setdefault("audience", "mass")
                slots.setdefault("tone", "share")
            slots["extra"] = text
        elif slots.get("goal") and not slots.get("topic") and not any((found_goal, found_platforms, found_audience, found_tone)):
            slots["topic"] = text[:500]
        elif slots.get("topic") and not all(key in slots for key in ("audience", "platform", "tone")):
            if not slots.get("audience") and not any((found_platforms, found_tone)):
                slots["audience"] = text[:100]
            elif slots.get("platform") and not slots.get("tone"):
                slots["tone"] = found_tone or text[:100]
        elif slots.get("topic"):
            slots["extra"] = text


def guide(body):
    text = body.get("text", "")
    if not isinstance(text, str) or len(text) > 12000:
        raise ChatError("请简短描述这次要写的内容。")
    slots = safe_slots(body.get("slots", {}))
    update_slots(slots, text.strip(), body.get("pick_slot"), body.get("pick_value"))
    platform = slots.get("platform", [])
    summary = [
        {"key": key, "label": label, "value": name}
        for key, label, name in [
            ("goal", "形态", GOALS[slots["goal"]][0] if "goal" in slots else ""),
            ("topic", "主题", slots.get("topic", "")),
            ("audience", "受众", AUDIENCES.get(slots.get("audience"), slots.get("audience", ""))),
            ("platform", "平台", "、".join(PLATFORMS[item][0] for item in platform)),
            ("tone", "语气", TONES.get(slots.get("tone"), slots.get("tone", ""))),
        ]
        if name
    ]
    result = {"ok": True, "slots": slots, "summary": summary, "understood": True, "steps": 5}
    stages = [
        ("goal", "想做什么", "先说说这次要做什么？", {key: goal[0] for key, goal in GOALS.items()}),
        ("topic", "文案主题", "讲什么、卖什么？说具体点，写出来更贴合你的需求。", {}),
        ("audience", "给谁看", "这条内容主要给谁看？", AUDIENCES),
        ("platform", "发布平台", "准备发到哪些平台？可以多选，我会分别出稿。", {key: item[0] for key, item in PLATFORMS.items()}),
        ("tone", "内容语气", "想用什么语气？", TONES),
    ]
    for step, (key, title, question, options) in enumerate(stages, 1):
        if not slots.get(key):
            return {
                **result, "slot": key, "step": step, "title": title, "question": question,
                "choices": [{"value": value, "label": label} for value, label in options.items()],
                "multi": key == "platform", "max": MAX_PLATFORMS if key == "platform" else 0,
            }
    return {**result, "ready": True}


def requests(body):
    topic = body.get("topic")
    goal = body.get("goal")
    platform = body.get("platforms")
    if (
        not isinstance(goal, str) or goal not in GOALS or not isinstance(platform, list)
        or not platform or platform != selected_platforms(platform)
        or not isinstance(topic, str) or not topic.strip() or len(topic) > 500
    ):
        raise ChatError("请填写文案主题，选择稿件类型和发布平台。")
    audience, tone = body.get("audience"), body.get("tone")
    audience_label = AUDIENCES.get(audience, audience[:100]) if isinstance(audience, str) and audience else AUDIENCES["mass"]
    tone_label = TONES.get(tone, tone[:100]) if isinstance(tone, str) and tone else TONES["share"]
    fields = [f"主题：{topic}", f"受众：{audience_label}", f"语气：{tone_label}"]
    for key, label in [("extra", "用户补充"), ("selling_points", "卖点"), ("material", "参考素材")]:
        value = body.get(key)
        if value:
            if not isinstance(value, str) or len(value) > 12000:
                raise ChatError("文案需求或参考素材格式无效。")
            fields.append(f"{label}：{value}")
    kind, instructions, _ = GOALS[goal]
    for item in platform:
        name, _, style = PLATFORMS[item]
        system = (
            f"为{name}写一份{kind}。{style}{instructions} "
            "首行用 Markdown 一级标题。只输出成稿，不输出说明。以当前主题、受众、语气和平台为准，"
            "旧需求中的冲突信息不适用。避免虚构品牌、价格、优惠或真实用户体验。"
        )
        yield item, goal, [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(fields)}]


def result(markdown, platform, goal, inspiration_id=None):
    title = markdown.splitlines()[0].lstrip("# ").strip()
    platform_name = PLATFORMS[platform][0]
    if platform_name not in title:
        title = f"{title}｜{platform_name}"
        body = markdown.split("\n", 1)
        markdown = "\n".join([f"# {title}", *body[1:]])
    return {
        "platform": platform, "output": GOALS[goal][2], "markdown": markdown,
        "inspiration_id": inspiration_id,
        "content": {"title": title, "body": markdown, "tags": re.findall(r"(?<!\w)#[^#\s]+", markdown)},
    }
