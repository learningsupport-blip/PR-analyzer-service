from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class ToolCall:
    name: str
    path: str


@dataclass(frozen=True)
class ModelTurn:
    data: dict | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    raw: str = ""


class ChatModel:
    async def complete(
        self,
        *,
        purpose: str,
        model: str,
        system: str,
        user: str,
        response_model: type[BaseModel] | None,
        enable_read_file: bool,
    ) -> ModelTurn:
        raise NotImplementedError
