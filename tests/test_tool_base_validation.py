from typing import Any

from feibot.agent.tools.base import Tool


class _DummyTool(Tool):
    @property
    def name(self) -> str:
        return "dummy"

    @property
    def description(self) -> str:
        return "dummy"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "x": {"type": "string"},
            },
            "required": ["x"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def test_validate_params_rejects_non_object_input() -> None:
    tool = _DummyTool()
    errors = tool.validate_params(["bad", "shape"])  # type: ignore[arg-type]
    assert errors == ["parameters must be an object, got list"]
