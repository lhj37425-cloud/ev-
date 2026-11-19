"""Extract API calls from the archived minified frontend (read-only).

Outputs a JSON inventory with file, path, method and a short surrounding snippet.
The snippets are for manual contract review; no frontend files are modified.
"""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "evcommander" / "mirror" / "assets"
OUT = ROOT / "验收-本地基线-2026-11-19" / "接口提取.json"

URL_RE = re.compile(r"url:\s*[`\"']([^`\"']+)[`\"']")
METHOD_RE = re.compile(r"method:\s*[`\"'](get|post|put|delete|patch)[`\"']", re.I)
CALL_RE = re.compile(
    r"(?:fetch|\b(?P<verb>get|post|put|delete|patch))\(\s*[`\"']"
    r"([^`\"']*(?:/api/|/agent-api/|/scrm-api/|/pay/|/order/|/bill/|/card/|/asset/)[^`\"']*)[`\"']",
    re.I,
)


def module_of(path: str) -> str:
    if path.startswith("/api/geo/"):
        return "GEO"
    if path.startswith(("/api/machines", "/api/rpa/devices", "/api/desktop", "/api/rpa/agent", "/api/rpa/bind", "/api/rpa/jobs", "/api/rpa/scripts", "/api/rpa/status")):
        return "设备农场/PC助理"
    if path.startswith(("/api/ext/digital-human", "/api/production/digital-human", "/api/ext/livehost")):
        return "数字人"
    if path.startswith(("/pay/", "/order/", "/bill/", "/card/", "/asset/", "/api/pay/", "/api/order/", "/api/bill/", "/api/card/", "/api/asset/")):
        return "充值和订单"
    if path.startswith(("/api/ext/designer/image", "/api/ext/ecom-model/image-edit", "/api/ext/designer/models", "/api/ext/designer/optimize-prompt", "/api/ext/designer/prompt-library", "/api/generation/", "/api/ext/designer/ecom")):
        return "参考图和图片编辑"
    if path.startswith(("/api/acquisition/", "/api/scrm/", "/api/sales/", "/api/rpa/acquisition/", "/api/moments-ads/")):
        return "获客/WhatsApp"
    return "其他"


def main() -> None:
    records = []
    for file in sorted(ASSETS.glob("*.js")):
        text = file.read_text(encoding="utf-8", errors="ignore")
        for match in URL_RE.finditer(text):
            path = match.group(1)
            if not path.startswith(("/", "http")):
                continue
            start = max(0, match.start() - 400)
            end = min(len(text), match.end() + 600)
            snippet = text[start:end]
            # Only look inside the object literal that owns this url, so a
            # neighbouring call's method is not attributed to this endpoint.
            tail = text[match.end():match.end() + 280]
            close = tail.find("}")
            owner = tail[:close] if close >= 0 else tail
            method = METHOD_RE.search(owner)
            records.append({
                "file": file.name,
                "path": path,
                "method": (method.group(1).upper() if method else "GET"),
                "module": module_of(path),
                "snippet": snippet,
            })
        for match in CALL_RE.finditer(text):
            path = match.group(2)
            start = max(0, match.start() - 450)
            end = min(len(text), match.end() + 450)
            snippet = text[start:end]
            verb = (match.groupdict().get("verb") or "").upper()
            method = METHOD_RE.search(snippet[: match.end() - match.start() + 120])
            records.append({
                "file": file.name,
                "path": path,
                "method": (verb or (method.group(1).upper() if method else "GET")),
                "module": module_of(path),
                "snippet": snippet,
            })
    # Deduplicate by file + path + method, keeping the longest snippet.
    best = {}
    for item in records:
        key = (item["file"], item["path"], item["method"])
        if key not in best or len(item["snippet"]) > len(best[key]["snippet"]):
            best[key] = item
    items = sorted(best.values(), key=lambda x: (x["module"], x["path"], x["method"], x["file"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    by_module = collections.Counter(item["module"] for item in items)
    print(f"wrote {OUT} ({len(items)} unique calls)")
    for module, count in by_module.most_common():
        print(f"{module}: {count}")


if __name__ == "__main__":
    main()
