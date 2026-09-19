"""Shared pytest fixtures: an isolated lab/ directory tree per test.

Tests never touch the real repository lab/ directory; each test gets its own
temporary lab root with the same layout (settings.yaml, principles.yaml,
topologies/, scenarios/, references/).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False)


@pytest.fixture()
def lab_root(tmp_path: Path) -> Path:
    root = tmp_path / "lab"

    _write_yaml(
        root / "settings.yaml",
        {
            "active_topology": "sample_lab",
            "active_scenario": "sample",
            "active_references": ["sample"],
        },
    )
    _write_yaml(root / "principles.yaml", {"general_operating_principles": ["Inspect before changing."]})
    _write_yaml(
        root / "topologies" / "sample_lab.yaml",
        {
            "name": "sample_lab",
            "description": "Sample lab used for tests.",
            "devices": {
                "R1": {
                    "type": "iosxr",
                    "address": "192.0.2.11",
                    "transport": "ssh",
                    "port": 22,
                    "username": "example-user",
                    "password": "example-password",
                },
                "R2": {
                    "type": "iosxr",
                    "address": "192.0.2.12",
                    "transport": "ssh",
                    "port": 22,
                },
            },
            "links": [{"a": "R1", "b": "R2"}],
        },
    )
    _write_yaml(
        root / "scenarios" / "sample.yaml",
        {"name": "sample", "description": "Sample scenario.", "objectives": ["do the thing"]},
    )
    _write_yaml(
        root / "scenarios" / "failover_test.yaml",
        {"name": "failover_test", "description": "Failover scenario."},
    )
    _write_yaml(
        root / "references" / "sample.yaml",
        {"name": "sample", "description": "Sample reference.", "guidance": ["do it well"]},
    )
    _write_yaml(
        root / "references" / "iosxr_basics.yaml",
        {"name": "iosxr_basics", "description": "IOS XR basics."},
    )
    return root
