from pathlib import Path
from app.errors import ToolExecutionError
from app.security.permissions import safe_path

class FilesystemTools:
    def __init__(self, workspace: Path): self.workspace = workspace
    async def read_file(self, path: str) -> dict:
        target = safe_path(self.workspace, path)
        if not target.is_file(): raise ToolExecutionError("file does not exist")
        if target.stat().st_size > 1_000_000: raise ToolExecutionError("file exceeds 1 MB read limit")
        return {"path": str(target.relative_to(self.workspace)), "content": target.read_text(errors="replace")}
    async def write_file(self, path: str, content: str) -> dict:
        target = safe_path(self.workspace, path)
        if target.name.startswith(".env") or target.name in {"id_rsa", "id_ed25519"}: raise ToolExecutionError("writing secret files is not permitted")
        existed = target.is_file()
        old_lines = len(target.read_text(errors="replace").splitlines()) if existed else 0
        target.parent.mkdir(parents=True, exist_ok=True); target.write_text(content)
        new_lines = len(content.splitlines())
        result = {"path": str(target.relative_to(self.workspace)), "bytes": len(content.encode()), "lines": new_lines}
        # Warn the model when it overwrites an existing file and drops a lot of content.
        # This is the primary cause of unintended deletions: write_file replaces the whole
        # file, so if the model forgets code it is silently lost.
        if existed and old_lines > 0:
            result["previous_lines"] = old_lines
            if new_lines < old_lines * 0.7:
                result["warning"] = (
                    f"This overwrote an existing file of {old_lines} lines with only {new_lines} lines "
                    f"({old_lines - new_lines} lines removed). If you did not intend to delete that much "
                    "code, use edit_file to replace only the specific section you want to change, "
                    "or re-read the original with read_file and write the complete content."
                )
        return result
    async def edit_file(self, path: str, old_string: str, new_string: str) -> dict:
        """Replace one exact occurrence of old_string with new_string in the file.
        The rest of the file is untouched. Use this instead of write_file when
        modifying an existing file so that no unrelated code is lost."""
        target = safe_path(self.workspace, path)
        if target.name.startswith(".env") or target.name in {"id_rsa", "id_ed25519"}: raise ToolExecutionError("editing secret files is not permitted")
        if not target.is_file(): raise ToolExecutionError(f"file '{path}' does not exist — use write_file to create it")
        if not old_string: raise ToolExecutionError("old_string must not be empty")
        content = target.read_text(errors="replace")
        count = content.count(old_string)
        if count == 0:
            raise ToolExecutionError(
                "old_string not found in file. Make sure it matches exactly (including whitespace and "
                "indentation). Use read_file to see the current content."
            )
        if count > 1:
            raise ToolExecutionError(
                f"old_string matches {count} places in the file — it must be unique. "
                "Include more surrounding lines so it matches exactly once."
            )
        new_content = content.replace(old_string, new_string, 1)
        target.write_text(new_content)
        return {
            "path": str(target.relative_to(self.workspace)),
            "replaced": 1,
            "old_lines": len(old_string.splitlines()),
            "new_lines": len(new_string.splitlines()),
            "file_lines": len(new_content.splitlines()),
        }
    async def list_files(self, path: str = ".") -> dict:
        target = safe_path(self.workspace, path)
        if target.is_file():
            raise ToolExecutionError(f"'{path}' is a file, not a directory. Use read_file to read its content.")
        if not target.is_dir():
            raise ToolExecutionError(f"directory '{path}' does not exist")
        entries = sorted(target.iterdir())[:500]
        files = [str(item.relative_to(self.workspace)) for item in entries if item.is_file() and item.name not in {".env"}]
        dirs = [str(item.relative_to(self.workspace)) + "/" for item in entries if item.is_dir() and item.name not in {".git"}]
        return {"files": files, "directories": dirs}
    async def search_code(self, query: str, path: str = ".") -> dict:
        target = safe_path(self.workspace, path)
        if not target.exists():
            raise ToolExecutionError(f"path '{path}' does not exist")
        matches = []
        # Support both single-file and directory search
        candidates = [target] if target.is_file() else target.rglob("*")
        for item in candidates:
            if len(matches) >= 100: break
            if not item.is_file() or ".git" in item.parts or item.stat().st_size > 1_000_000: continue
            try:
                for number, line in enumerate(item.read_text(errors="replace").splitlines(), 1):
                    if query in line:
                        matches.append({"path":str(item.relative_to(self.workspace)),"line":number,"content":line[:500]})
                        if len(matches) >= 100: break
            except OSError: continue
        return {"matches":matches, "searched": str(target.relative_to(self.workspace)), "query": query}
