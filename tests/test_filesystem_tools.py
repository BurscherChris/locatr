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


# ---------------------------------------------------------------------------
# edit_file — surgical replacement that never loses unrelated code
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_edit_file_replaces_exact_section(tmp_path):
    tools = FilesystemTools(tmp_path)
    (tmp_path / "a.tsx").write_text("import x from 'y'\n\nexport function A() {\n  return <div>old</div>\n}\n\nexport function B() {}\n")
    r = await tools.edit_file("a.tsx", "  return <div>old</div>", "  return <div>new</div>")
    assert r["replaced"] == 1
    content = (tmp_path / "a.tsx").read_text()
    assert "<div>new</div>" in content
    assert "import x from 'y'" in content          # unrelated line preserved
    assert "export function B() {}" in content     # unrelated function preserved


@pytest.mark.asyncio
async def test_edit_file_rejects_missing_string(tmp_path):
    tools = FilesystemTools(tmp_path)
    (tmp_path / "a.ts").write_text("hello\n")
    with pytest.raises(ToolExecutionError, match="not found"):
        await tools.edit_file("a.ts", "does not exist", "x")


@pytest.mark.asyncio
async def test_edit_file_rejects_ambiguous_string(tmp_path):
    tools = FilesystemTools(tmp_path)
    (tmp_path / "a.ts").write_text("foo\nfoo\n")
    with pytest.raises(ToolExecutionError, match="matches 2 places"):
        await tools.edit_file("a.ts", "foo", "bar")


@pytest.mark.asyncio
async def test_edit_file_rejects_nonexistent_file(tmp_path):
    tools = FilesystemTools(tmp_path)
    with pytest.raises(ToolExecutionError, match="does not exist"):
        await tools.edit_file("missing.ts", "a", "b")


@pytest.mark.asyncio
async def test_edit_file_rejects_secret_files(tmp_path):
    tools = FilesystemTools(tmp_path)
    (tmp_path / ".env").write_text("A=1\n")
    with pytest.raises(ToolExecutionError):
        await tools.edit_file(".env", "A=1", "A=2")


@pytest.mark.asyncio
async def test_edit_file_traversal_blocked(tmp_path):
    tools = FilesystemTools(tmp_path)
    with pytest.raises(ToolExecutionError):
        await tools.edit_file("../../etc/passwd", "root", "x")


# ---------------------------------------------------------------------------
# write_file warns when it drops a large portion of an existing file
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_file_warns_on_large_deletion(tmp_path):
    tools = FilesystemTools(tmp_path)
    (tmp_path / "big.tsx").write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
    r = await tools.write_file("big.tsx", "line 0\nline 1\n")
    assert r["previous_lines"] == 100
    assert r["lines"] == 2
    assert "warning" in r
    assert "98 lines removed" in r["warning"]
    assert "edit_file" in r["warning"]


@pytest.mark.asyncio
async def test_write_file_no_warning_on_new_file(tmp_path):
    tools = FilesystemTools(tmp_path)
    r = await tools.write_file("new.tsx", "x\n")
    assert "warning" not in r
    assert "previous_lines" not in r


@pytest.mark.asyncio
async def test_write_file_no_warning_on_growth(tmp_path):
    tools = FilesystemTools(tmp_path)
    (tmp_path / "f.ts").write_text("a\nb\n")
    r = await tools.write_file("f.ts", "a\nb\nc\nd\n")
    assert "warning" not in r
