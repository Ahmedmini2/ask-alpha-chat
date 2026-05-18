from typing import Callable, Awaitable, Any
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession

# Handlers receive (db, args, ctx). ctx carries cross-tool info like user_id and channel.
# Existing handlers that don't need ctx can simply ignore it.
@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[AsyncSession, dict, dict], Awaitable[Any]]


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def to_bedrock_config(self) -> dict:
        """Converts registered tools into the format Bedrock's Converse API expects."""
        return {
            "tools": [
                {
                    "toolSpec": {
                        "name": t.name,
                        "description": t.description,
                        "inputSchema": {"json": t.input_schema},
                    }
                }
                for t in self._tools.values()
            ]
        }

registry = ToolRegistry()
