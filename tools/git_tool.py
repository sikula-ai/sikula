from __future__ import annotations

import subprocess
from pathlib import Path

from tools.base_tool import BaseTool, Sandbox, ToolResult


class GitTool(BaseTool):
    def __init__(self, sandbox: Sandbox, project_root: Path) -> None:
        super().__init__(sandbox)
        self._root = project_root

    def diff_head(self) -> ToolResult:
        try:
            r = subprocess.run(
                ["git", "diff", "--relative", "HEAD"],
                capture_output=True,
                text=True,
                cwd=self._root,
            )
            if r.returncode != 0:
                return ToolResult(success=False, output=r.stdout, error=r.stderr)
            return ToolResult(success=True, output=r.stdout)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
