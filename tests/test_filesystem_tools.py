import pytest
from app.tools.filesystem import FilesystemTools
from app.errors import ToolExecutionError

@pytest.mark.asyncio
async def test_filesystem_rejects_traversal_and_writes(tmp_path):
    tools = FilesystemTools(tmp_path)
    await tools.write_file("src/a.txt", "hello")
    assert (await tools.read_file("src/a.txt"))["content"] == "hello"
    with pytest.raises(ToolExecutionError): await tools.read_file("../../etc/passwd")
    with pytest.raises(ToolExecutionError): await tools.write_file(".env", "secret")
