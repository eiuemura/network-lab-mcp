"""External YAML editor support for topology/scenario/reference definitions.

Resolution order: $VISUAL, then $EDITOR, then a `vim` fallback. Network Lab
MCP never adds vim-specific options when the operator explicitly chose an
editor via $VISUAL/$EDITOR -- the syntax-highlighting flags below apply only
to the fallback it selects itself.

The candidate flow is: committed definition -> candidate data -> a secure,
unique `.yaml` temporary file -> external editor -> (editor exits) -> read
the temporary file -> parse -> validate -> update the in-memory candidate.
Nothing under lab/ is touched here; only `commit` writes real YAML. The
temporary file is always removed, including on every error path, and is
never written to with world/group-readable permissions.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml


class EditorError(Exception):
    """A clear, user-facing error from launching or reading the external editor."""


def resolve_editor_command() -> list[str]:
    """Return the argv prefix for the external editor: $VISUAL, then
    $EDITOR (each parsed with shlex.split() since it may embed arguments;
    `shell=True` is never used), then a `vim` fallback with just enough
    options to make an unfamiliar YAML temp file legible."""
    for var in ("VISUAL", "EDITOR"):
        value = os.environ.get(var)
        if not value:
            continue
        try:
            parts = shlex.split(value)
        except ValueError as exc:
            raise EditorError(f"Could not parse ${var}: {exc}") from exc
        if parts:
            return parts
    return ["vim", "-c", "syntax on", "-c", "set filetype=yaml"]


def edit_yaml_candidate(data: Any) -> dict:
    """Open `data` in the resolved external editor via a secure temp file,
    then parse and minimally validate the result (valid YAML, root is a
    mapping) and return it. Raises EditorError -- leaving the caller's
    candidate untouched -- on a non-zero editor exit, an unparseable editor
    result, or a non-mapping root. The temporary file is removed on every
    path, including exceptions."""
    command = resolve_editor_command()
    fd, path_str = tempfile.mkstemp(suffix=".yaml", prefix="network-lab-mcp-")
    path = Path(path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False, default_flow_style=False)

        try:
            result = subprocess.run([*command, str(path)])
        except FileNotFoundError as exc:
            raise EditorError(f"Editor '{command[0]}' was not found.") from exc

        if result.returncode != 0:
            raise EditorError(
                f"Editor exited with a non-zero status ({result.returncode}). Candidate was not updated."
            )

        text = path.read_text(encoding="utf-8")
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            if mark is not None:
                raise EditorError(
                    f"YAML validation failed at line {mark.line + 1}, column {mark.column + 1}. "
                    "Candidate was not updated."
                ) from exc
            raise EditorError("YAML validation failed. Candidate was not updated.") from exc

        if not isinstance(parsed, dict):
            raise EditorError("Edited content must be a YAML mapping. Candidate was not updated.")
        return parsed
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
