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
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASES_DIR = REPO_ROOT / "docker" / "bases"

# Where instance directories are read from: the validated twister-pipeline
# Zephyr instances in apptainer/instances/ (written there only by
# scripts/promote_instance.py), then the Docker instances in docker/instances/.
# EMBEDEVAL_INSTANCES_DIR replaces both -- e.g. generated/, so a freshly
# generated instance can be built and validated before it is promoted, and
# without any chance of overwriting a committed one.
APPTAINER_INSTANCES = REPO_ROOT / "apptainer" / "instances"
_override = os.environ.get("EMBEDEVAL_INSTANCES_DIR")
INSTANCE_ROOTS = ([Path(_override) if Path(_override).is_absolute() else REPO_ROOT / _override]
                  if _override else [APPTAINER_INSTANCES, REPO_ROOT / "docker" / "instances"])

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
    for root in INSTANCE_ROOTS:
        if (root / instance_id / "metadata.json").exists():
            return root / instance_id
    return INSTANCE_ROOTS[0] / instance_id


def load_metadata(instance_id: str) -> dict:
    path = instance_dir(instance_id) / "metadata.json"
    if not path.exists():
        raise FileNotFoundError(f"metadata.json not found at {path}")
    with open(path) as f:
        return json.load(f)


def discover_instances(project: str | None = None) -> list[str]:
    found = []
    for root in INSTANCE_ROOTS:
        for d in sorted(root.iterdir()) if root.is_dir() else []:
            if d.name in found or not (d.is_dir() and (d / "metadata.json").exists()):
                continue
            if project and split_instance(d.name)[0] != project:
                continue
            # Zephyr instances come from the twister pipeline (an instance.def).
            # The hand-built Zephyr Docker folders are reference metadata only.
            if split_instance(d.name)[0] == "zephyr" and not (d / "instance.def").exists():
                continue
            found.append(d.name)
    return sorted(found)
