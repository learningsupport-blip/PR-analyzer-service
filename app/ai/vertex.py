import asyncio
import logging

from pydantic import BaseModel

from app.ai.parse import parse_model_response
from app.ai.protocol import ModelTurn

logger = logging.getLogger("pr-analyzer")


class VertexChat:
    def __init__(self, project: str, location: str):
        from google import genai
        from google.genai import types

        self._types = types
        self._read_tool = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="read_file",
                    description="Read a changed file, or a file imported by a changed file, at the pull request head.",
                    parameters_json_schema={
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Path relative to the repository root",
                            }
                        },
                        "required": ["path"],
                    },
                )
            ]
        )
        self._client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
            http_options=types.HttpOptions(timeout=45_000),
        )

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
        logger.info("vertex_call purpose=%s model=%s", purpose, model)
        return await asyncio.to_thread(
            self._complete,
            model,
            system,
            user,
            response_model,
            enable_read_file,
        )

    def _complete(
        self,
        model: str,
        system: str,
        user: str,
        response_model: type[BaseModel] | None,
        enable_read_file: bool,
    ) -> ModelTurn:
        types = self._types
        config: dict = {"system_instruction": system, "temperature": 0, "max_output_tokens": 8192}
        if enable_read_file:
            config["tools"] = [self._read_tool]
            config["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
        if response_model is not None:
            config["response_mime_type"] = "application/json"
            config["response_schema"] = response_model
        response = self._client.models.generate_content(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(**config),
        )
        return parse_model_response(response)
