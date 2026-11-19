"""Options and request builders for the archived Image Studio views.

The frontend owns the layout and field rendering. The backend supplies the
same option shapes it already expects, then turns the submitted form into one
normalized image request for ``image_generation.generate``.
"""

import os

from image_generation import (
    EDIT_STREAM,
    MODEL,
    PRODUCT_BG_STREAM,
    SIZES,
    TRYON_STREAM,
)

IMAGE_BRAND = os.environ.get("EV_IMAGE_BRAND", "OpenAI")
IMAGE_NAME = os.environ.get("EV_IMAGE_NAME", "GPT Image 2")

RATIOS = [
    {"value": value, "label": label}
    for value, label in [
        ("1:1", "1:1 方图"), ("16:9", "16:9 横屏"), ("9:16", "9:16 竖屏"),
        ("4:3", "4:3 横图"), ("3:4", "3:4 竖图"), ("3:2", "3:2 横图"),
        ("2:3", "2:3 竖图"),
    ]
]

MODEL_OPTION = {
    "code": MODEL,
    "brand": IMAGE_BRAND,
    "name": IMAGE_NAME,
    "usable": True,
    "recommended": True,
    "supports_reference": True,
    "supports_output_size": True,
    "max_count": 1,
    "params": [{"key": "size", "label": "画幅", "default": "1:1", "options": RATIOS}],
}

PRESETS = [
    {
        "key": "image-edit",
        "tool": "image-edit",
        "group": "改图",
        "name": "AI 改图",
        "desc": "上传原图，用文字描述要改什么；支持选区局部重绘。",
        "scales_output": True,
        "model_codes": [MODEL],
        "default_model": MODEL,
        "slots": [{"key": "image", "label": "原图", "required": True, "tip": "要修改的图片"}],
        "fields": [{
            "key": "prompt",
            "label": "修改要求",
            "type": "textarea",
            "placeholder": "例如：把背景换成海边日落，保留人物和衣服",
        }],
        "tips": ["描述越具体越好", "选区局部重绘时，先涂抹或框选要修改的区域"],
    }
]


def designer_models():
    return {"image_models": [MODEL_OPTION], "video_models": []}


def edit_options(query=None):
    return {"presets": PRESETS, "models": [MODEL_OPTION]}


def tryon_options():
    return {
        "models": [{
            "code": MODEL,
            "name": IMAGE_NAME,
            "brand": IMAGE_BRAND,
            "recommended": True,
            "recommend_reason": "人物图和服装图一起交给模型，脸、姿势与背景保持不变",
            "note": "建议上传全身正面照和背景干净的平铺服装图",
            "mask_mode": "native",
            "supports_output_size": True,
        }],
        "default_model": MODEL,
    }


def _ratio(value):
    return SIZES[value] if isinstance(value, str) and value in SIZES else SIZES["1:1"]


def _model(value):
    model = value if isinstance(value, str) and value else MODEL
    if model != MODEL:
        raise ValueError("不支持的图片模型。")
    return model


def _values_prompt(values):
    if not isinstance(values, dict):
        return ""
    parts = []
    direct = values.get("prompt") or values.get("ref_prompt")
    if isinstance(direct, str) and direct.strip():
        parts.append(direct.strip())
    for key, value in values.items():
        if key in {"prompt", "ref_prompt", "size"} or not isinstance(value, str):
            continue
        if value.strip():
            parts.append(f"{key}: {value.strip()}")
    return "\n".join(parts)


def _edit_request(body):
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    prompt = _values_prompt(values) or "Edit the provided image according to the user's request."
    image = body.get("image_url")
    if not isinstance(image, str) or not image:
        raise ValueError("请先上传要修改的图片。")
    images = [image]
    ref = body.get("ref_image_url") or values.get("ref_image_url")
    if isinstance(ref, str) and ref:
        images.append(ref)
    mask = body.get("mask_url") if isinstance(body.get("mask_url"), str) else None
    return {
        "prompt": prompt,
        "size": _ratio(values.get("size")),
        "images": images,
        "mask": mask or None,
        "model": _model(body.get("model_code")),
    }


def _product_bg_request(body):
    image = body.get("image_url")
    if not isinstance(image, str) or not image:
        raise ValueError("请先上传商品主体图。")
    ref_prompt = body.get("ref_prompt")
    ref_prompt = ref_prompt.strip() if isinstance(ref_prompt, str) else ""
    prompt = "Replace the background of the product image with " + (
        ref_prompt or "a clean, professional e-commerce background"
    ) + ". Keep the product itself unchanged, including shape, color and text."
    images = [image]
    ref_image = body.get("ref_image_url")
    if isinstance(ref_image, str) and ref_image:
        images.append(ref_image)
        prompt += " Follow the visual style of the additional background reference image."
    return {"prompt": prompt, "size": SIZES["1:1"], "images": images, "mask": None, "model": MODEL}


def _tryon_request(body):
    person = body.get("person_image")
    if not isinstance(person, str) or not person:
        raise ValueError("请先上传人物图。")
    top = body.get("top_garment")
    bottom = body.get("bottom_garment")
    images = [person]
    if isinstance(top, str) and top:
        images.append(top)
    if isinstance(bottom, str) and bottom:
        images.append(bottom)
    if len(images) < 2:
        raise ValueError("请至少上传一件上装或下装。")
    skill = body.get("skill") or "tryon"
    if skill == "changewear":
        prompt = (
            "Replace the person's garment in the first image with the garment shown in the "
            "reference image. Keep the person's face, pose and background unchanged. "
            "Generate one realistic full-body photo."
        )
    else:
        prompt = (
            "Put the garment(s) from the reference images onto the person in the first image. "
            "Keep the person's face, pose and background unchanged. "
            "Generate one realistic full-body try-on photo."
        )
    if body.get("restore_face") is False:
        prompt += " You may generate a natural new face."
    else:
        prompt += " Keep the original face."
    return {
        "prompt": prompt,
        "size": "1024x1536",
        "images": images,
        "mask": None,
        "model": _model(body.get("model_code")),
    }


def build(path, body):
    """Turn a frontend stream body into a normalized image request."""
    if path == EDIT_STREAM:
        return _edit_request(body)
    if path == PRODUCT_BG_STREAM:
        return _product_bg_request(body)
    if path == TRYON_STREAM:
        return _tryon_request(body)
    raise ValueError("不支持的图片接口。")
