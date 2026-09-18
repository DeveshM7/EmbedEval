"""
Metadata access for the build side.

Deliberately independent of harness/: building images and running agents are
separate concerns with separate lifecycles. This module needs only the boring
half of instance handling -- read a JSON file, list a directory -- and never
touches output paths or model naming, which is where naming drift actually
comes from.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTANCES_DIR = REPO_ROOT / "docker" / "instances"
BASES_DIR = REPO_ROOT / "docker" / "bases"

PROJECTS = ("zephyr", "nuttx", "riot")

_INSTANCE_RE = re.compile(r"^(?P<project>[a-z]+)__(?P=project)-(?P<pr>\d+)$")


def split_instance(instance_id: str) -> tuple[str, str]:
    """'nuttx__nuttx-11889' -> ('nuttx', '11889')."""
    m = _INSTANCE_RE.match(instance_id)
    if not m:
        raise ValueError(
            f"malformed instance id {instance_id!r}; expected <project>__<project>-<PR>"
        )
    return m.group("project"), m.group("pr")


def instance_dir(instance_id: str) -> Path:
    return INSTANCES_DIR / instance_id


def load_metadata(instance_id: str) -> dict:
    path = instance_dir(instance_id) / "metadata.json"
    if not path.exists():
        raise FileNotFoundError(f"metadata.json not found at {path}")
    with open(path) as f:
        return json.load(f)


def discover_instances(project: str | None = None) -> list[str]:
    found = []
    for d in sorted(INSTANCES_DIR.iterdir()):
        if not (d.is_dir() and (d / "metadata.json").exists()):
            continue
        if project and split_instance(d.name)[0] != project:
            continue
        found.append(d.name)
    return found
