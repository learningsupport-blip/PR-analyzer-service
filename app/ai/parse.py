import json

from app.ai.protocol import ModelTurn, ToolCall


def _call_name(call: object) -> str:
    if isinstance(call, dict):
        return str(call.get("name") or "")
    return str(getattr(call, "name", "") or "")


def _call_path(call: object) -> str:
    args = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
    if not isinstance(args, dict):
        try:
            args = dict(args) if args is not None else {}
        except (TypeError, ValueError):
            args = {}
    return str(args.get("path") or "")


def _response_text(response: object) -> str:
    try:
        value = getattr(response, "text", "")
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def parse_model_response(response: object) -> ModelTurn:
    calls = tuple(
        ToolCall(name=_call_name(call), path=_call_path(call))
        for call in (getattr(response, "function_calls", None) or [])
    )
    parsed = getattr(response, "parsed", None)
    data = None
    if hasattr(parsed, "model_dump"):
        dumped = parsed.model_dump()
        data = dumped if isinstance(dumped, dict) else None
    elif isinstance(parsed, dict):
        data = parsed
    raw = _response_text(response)
    if data is None and raw and not calls:
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
    return ModelTurn(data=data, tool_calls=calls, raw=raw)
