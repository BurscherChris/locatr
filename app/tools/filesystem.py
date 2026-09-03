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
        target.parent.mkdir(parents=True, exist_ok=True); target.write_text(content)
        return {"path": str(target.relative_to(self.workspace)), "bytes": len(content.encode())}
    async def list_files(self, path: str = ".") -> dict:
        target = safe_path(self.workspace, path)
        if not target.is_dir(): raise ToolExecutionError("directory does not exist")
        files = [str(item.relative_to(self.workspace)) for item in sorted(target.iterdir())[:500] if item.name not in {".git", ".env"}]
        return {"files": files}
    async def search_code(self, query: str, path: str = ".") -> dict:
        target = safe_path(self.workspace, path)
        if not target.is_dir(): raise ToolExecutionError("directory does not exist")
        matches = []
        for item in target.rglob("*"):
            if len(matches) >= 100: break
            if not item.is_file() or ".git" in item.parts or item.stat().st_size > 1_000_000: continue
            try:
                for number, line in enumerate(item.read_text(errors="replace").splitlines(), 1):
                    if query in line:
                        matches.append({"path":str(item.relative_to(self.workspace)),"line":number,"content":line[:500]})
                        if len(matches) >= 100: break
            except OSError: continue
        return {"matches":matches}
