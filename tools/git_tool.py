from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from tools.base_tool import BaseTool, Sandbox, ToolResult


class GitTool(BaseTool):
    def __init__(self, sandbox: Sandbox, project_root: Path) -> None:
        super().__init__(sandbox)
        self._root = project_root

    def diff_head(self, paths: Sequence[str] | None = None) -> ToolResult:
        try:
            args = ["git", "diff", "--relative", "HEAD"]
            if isinstance(paths, (str, bytes)):
                return ToolResult(success=False, error="Git diff paths must be a sequence of relative paths")
            scoped_paths: list[str] = []
            for path in paths or []:
                if not isinstance(path, str):
                    return ToolResult(success=False, error="Git diff paths must be strings")
                if not path.strip():
                    continue
                candidate = Path(path)
                if candidate.is_absolute() or ".." in candidate.parts or "\0" in path:
                    return ToolResult(success=False, error=f"Git diff path is outside the project scope: {path!r}")
                if path not in scoped_paths:
                    scoped_paths.append(path)
            if scoped_paths:
                args = ["git", "--literal-pathspecs", "diff", "--relative", "HEAD", "--", *scoped_paths]
            r = subprocess.run(
                args,
                capture_output=True,
                text=True,
                cwd=self._root,
            )
            if r.returncode != 0:
                return ToolResult(success=False, output=r.stdout, error=r.stderr)
            return ToolResult(success=True, output=r.stdout)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
