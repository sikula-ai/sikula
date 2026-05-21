from __future__ import annotations

from pathlib import Path

from tools.base_tool import BaseTool, Sandbox, ToolResult


class FileTool(BaseTool):
    def __init__(self, sandbox: Sandbox, project_root: Path) -> None:
        super().__init__(sandbox)
        self._root = project_root

    def read(self, path: str) -> ToolResult:
        p = Path(path)
        try:
            self.sandbox.check_read(p)
            full = (self._root / p).resolve()
            return ToolResult(success=True, output=full.read_text())
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def write(self, path: str, content: str) -> ToolResult:
        p = Path(path)
        try:
            self.sandbox.check_write(p)
            full = (self._root / p).resolve()
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
            return ToolResult(success=True, output=f"Written: {path}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
