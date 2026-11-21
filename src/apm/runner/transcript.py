"""Run events as plain text, for following a run in the terminal."""

import json
from typing import Any


def format_event(event: dict[str, Any]) -> list[str]:
    """The lines to print for one stream-json event. Most events print nothing."""
    kind = event.get("type")
    if kind == "assistant":
        return [line for block in _blocks(event) for line in _assistant_block(block)]
    if kind == "user":
        return [line for block in _blocks(event) for line in _result_block(block)]
    if kind == "result":
        cost = event.get("total_cost_usd")
        return [f"— finished{f' · ${cost:.2f}' if isinstance(cost, (int, float)) else ''}"]
    if kind == "apm_notice" and (text := str(event.get("text", "")).strip()):
        return [f"APM: {text}"]
    return []


def _blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    content = (event.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _assistant_block(block: dict[str, Any]) -> list[str]:
    if block.get("type") == "text" and (text := str(block.get("text", "")).strip()):
        return [text]
    if block.get("type") == "tool_use":
        name = str(block.get("name", "")).removeprefix("mcp__apm__")
        return [f"→ {name}({_brief(block.get('input'))})"]
    return []


def _result_block(block: dict[str, Any]) -> list[str]:
    if block.get("type") != "tool_result" or not block.get("is_error"):
        return []
    return [f"  ✗ {_brief(block.get('content'), 200)}"]


def _brief(value: Any, limit: int = 100) -> str:
    """A value on one line, cut to `limit` characters."""
    if isinstance(value, list):
        value = " ".join(str(v.get("text", v)) if isinstance(v, dict) else str(v) for v in value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
