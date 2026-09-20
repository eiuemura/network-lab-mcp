"""Network Lab MCP: a lightweight AI Network Engineer Layer for Claude Code."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import metadata as _package_metadata
from importlib.metadata import version as _package_version

_DISTRIBUTION_NAME = "network-lab-mcp"


def _metadata_field(field: str) -> str:
    """Read one field from installed package metadata (pyproject.toml is
    its source), never raising -- `show version` must not fail merely
    because the package is unavailable or the field is unset."""
    try:
        value = _package_metadata(_DISTRIBUTION_NAME).get(field)
    except PackageNotFoundError:
        return "unavailable"
    return value or "unavailable"


try:
    __version__ = _package_version(_DISTRIBUTION_NAME)
except PackageNotFoundError:
    # Not installed (e.g. run directly from a checkout without `pip install
    # -e .`); mcp_server.py and `show version` both tolerate this.
    __version__ = "unavailable"

__author__ = _metadata_field("Author")
__license__ = _metadata_field("License")

# Not a standard packaging metadata field, so pyproject.toml has nowhere to
# put it; this constant is the single source of truth for it instead of a
# second copy living in cli/main.py.
__release_date__ = "2026-09-20"
