import pytest
from app.tools.base import Tool
from app.tools.registry import ToolRegistry

@pytest.mark.asyncio
async def test_registry_handles_malformed_and_unknown_calls():
    registry = ToolRegistry(); registry.register(Tool("x","x",{"type":"object","required":["value"]},lambda value: _value(value)))
    assert not (await registry.execute("x", "bad json")).ok
    assert not (await registry.execute("missing", {})).ok
    assert (await registry.execute("x", {"value":"ok"})).data == "ok"
async def _value(value): return value
