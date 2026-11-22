"""Configurable text-model provider for the existing chat and agent views.

Supports two wire protocols behind the same interface:
- ``responses``: OpenAI Responses API (the original production path).
- ``chat_completions``: OpenAI-compatible Chat Completions (relays, gateways).

Only the backend is adapted here. The archived frontend keeps its existing
request/response format; ``commander`` translates to the configured provider.
"""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from urllib.parse import urlsplit


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_LABEL = "GPT-6 Luna（OpenAI）"
DEFAULT_BRAND = "OpenAI"
DEFAULT_PROTOCOL = "responses"
PROTOCOLS = {"responses", "chat_completions"}


class ProviderError(ValueError):
    """A provider configuration or transport problem the UI can display."""


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def base_url():
    return _env("EV_LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def model():
    return _env("EV_LLM_MODEL", DEFAULT_MODEL)


def label():
    return _env("EV_LLM_LABEL", DEFAULT_LABEL if model() == DEFAULT_MODEL else model())


def brand():
    return _env("EV_LLM_BRAND", DEFAULT_BRAND)


def protocol():
    value = _env("EV_LLM_PROTOCOL", DEFAULT_PROTOCOL).lower()
    if value not in PROTOCOLS:
        raise ProviderError("模型协议配置无效。")
    return value


def endpoint():
    base = base_url()
    if not base:
        raise ProviderError("模型地址未配置。")
    return base + ("/responses" if protocol() == "responses" else "/chat/completions")


def curl_bin():
    return _env("EV_CURL_BIN") or shutil.which("curl") or "/usr/bin/curl"


def _validate_key(key):
    if not isinstance(key, str):
        raise ProviderError("模型 key 配置无效。")
    key = key.strip()
    if not key or any(character.isspace() for character in key):
        raise ProviderError("模型 key 配置无效。")
    return key


def _file_key(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise ProviderError("模型 key 文件不可读。") from error


def _cc_switch_key():
    provider_db = Path.home() / ".cc-switch" / "cc-switch.db"
    try:
        with closing(sqlite3.connect(f"{provider_db.as_uri()}?mode=ro", uri=True)) as connection:
            row = connection.execute(
                "SELECT settings_config FROM providers WHERE id=? AND app_type=?", ("openai", "pi")
            ).fetchone()
        profile = json.loads(row[0]) if row else {}
        parsed = urlsplit(profile.get("baseUrl", ""))
        key = profile.get("apiKey")
    except (sqlite3.Error, TypeError, ValueError, AttributeError) as error:
        raise ProviderError("无法读取 CC Switch 的 OpenAI 官方配置。") from error
    if (
        parsed.scheme != "https" or parsed.netloc != "api.openai.com"
        or parsed.path.rstrip("/") != "/v1"
        or not isinstance(key, str)
    ):
        raise ProviderError("CC Switch 的 OpenAI 官方 API 配置不可用。")
    return _validate_key(key)


def read_key():
    key = _env("EV_LLM_API_KEY")
    if key:
        return _validate_key(key)
    for name in ("EV_LLM_API_KEY_FILE", "EV_OPENAI_KEY_FILE"):
        path = _env(name)
        if path:
            return _validate_key(_file_key(path))
    if os.environ.get("EV_TRIAL_ORIGIN"):
        raise ProviderError("试用环境尚未配置模型。")
    if base_url() != DEFAULT_BASE_URL:
        raise ProviderError("模型 key 未配置。")
    return _cc_switch_key()


def output_limit(default=1024):
    """Effective completion limit; chat_completions honours EV_LLM_MAX_TOKENS."""
    if protocol() != "chat_completions":
        return default
    raw = _env("EV_LLM_MAX_TOKENS")
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _payload(messages, max_output_tokens):
    if protocol() == "responses":
        return {
            "model": model(),
            "input": messages,
            "stream": True,
            "store": False,
            "max_output_tokens": max_output_tokens,
        }
    payload = {
        "model": model(),
        "messages": messages,
        "stream": True,
        "max_tokens": output_limit(max_output_tokens),
    }
    if _env("EV_LLM_STREAM_USAGE", "1") != "0":
        payload["stream_options"] = {"include_usage": True}
    return payload


def _sse_events(process):
    with process.stdout:
        for line in process.stdout:
            if not line.startswith(b"data: "):
                continue
            data = line[6:].strip()
            if data == b"[DONE]":
                yield {"type": "__done__"}
                return
            if not data:
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            problem = event.get("error")
            if not problem and event.get("type") == "response.failed":
                response = event.get("response")
                if isinstance(response, dict):
                    problem = response.get("error")
            if problem:
                message = problem.get("message") if isinstance(problem, dict) else str(problem)
                raise ProviderError(str(message or "模型返回错误。"))
            yield event


def _responses_text(event):
    if event.get("type") in {"response.output_text.delta", "response.refusal.delta"}:
        return event.get("delta") or ""
    return ""


def _chat_text(event):
    pieces = []
    for choice in event.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        if isinstance(delta, dict) and delta.get("content"):
            pieces.append(delta["content"])
        message = choice.get("message") or {}
        if isinstance(message, dict) and message.get("content"):
            pieces.append(message["content"])
    return "".join(pieces)


def _finished(event):
    for choice in event.get("choices") or []:
        if isinstance(choice, dict) and choice.get("finish_reason"):
            return True
    return event.get("type") in {"response.completed", "response.incomplete"}


def stream(messages, *, max_output_tokens=1024, usage=None):
    """Yield text deltas from the configured provider.

    When ``usage`` is a dict, provider-reported token counts are merged into it
    (OpenAI chat completions and Responses API shapes are both recognised).
    """
    key = read_key()
    wire = protocol()
    target = endpoint()
    parsed = urlsplit(target)
    if parsed.scheme not in {"http", "https"}:
        raise ProviderError("模型地址只支持 HTTP 或 HTTPS。")
    authorization = json.dumps("Authorization: Bearer " + key)
    body = json.dumps(json.dumps(_payload(messages, max_output_tokens), ensure_ascii=False), ensure_ascii=False)
    config = f"header = {authorization}\ndata-binary = {body}\n".encode("utf-8")
    command = [
        curl_bin(), "--disable", "--config", "-", "--proto", "=https" if parsed.scheme == "https" else "=http,https",
        "--noproxy", "*", "--connect-timeout", "12", "--max-time", "100", "--silent", "--fail",
        "--no-buffer", "--header", "Content-Type: application/json", target,
    ]
    yielded = False
    finished = False
    with subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as process:
        try:
            with process.stdin:
                process.stdin.write(config)
            for event in _sse_events(process):
                if event.get("type") == "__done__":
                    return
                if usage is not None:
                    event_usage = event.get("usage")
                    if isinstance(event_usage, dict):
                        usage.update(event_usage)
                    response = event.get("response")
                    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                        usage.update(response["usage"])
                if finished:
                    # Chat completions put the usage chunk right after the
                    # finish marker; one extra event captures it.
                    return
                if wire == "responses":
                    text = _responses_text(event)
                else:
                    text = _chat_text(event)
                if text:
                    yielded = True
                    yield text
                if _finished(event):
                    finished = True
        finally:
            if process.poll() is None:
                process.terminate()
    if yielded and (process.returncode == 0 or finished):
        return
    raise ProviderError("模型未返回完整回复，请检查服务配置和额度后重试。")
