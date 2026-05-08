from __future__ import annotations

import json
import urllib.error
import urllib.request


class TimewebAIError(Exception):
    pass


def strip_markdown_fences(text: str) -> str:
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    lines = t.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def chat_completion(
    *,
    agent_access_id: str,
    bearer_token: str,
    x_proxy_source: str,
    messages: list[dict],
    max_completion_tokens: int = 8192,
    timeout_sec: int = 120,
) -> dict:
    url = (
        "https://agent.timeweb.cloud/api/v1/cloud-ai/agents/"
        f"{agent_access_id}/v1/chat/completions"
    )
    payload = {
        "model": "gpt-4",
        "messages": messages,
        "stream": False,
        "max_completion_tokens": max_completion_tokens,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {bearer_token}")
    req.add_header("x-proxy-source", x_proxy_source or "")

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise TimewebAIError(
            f"Ошибка API агента ({e.code}): {detail or e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise TimewebAIError(f"Не удалось связаться с ИИ-агентом: {e.reason}") from e

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise TimewebAIError("Некорректный JSON в ответе агента") from e


def extract_message_content(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts).strip()
    return str(content).strip()
