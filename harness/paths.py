"""
Shared path and naming conventions for EmbedEval.

Every script that reads or writes under outputs/ must import from here.
Two independent implementations of these rules is how the repo ended up with
three incompatible output layouts and two spellings of "DeepSeek-V3"; worse,
a path mismatch between the runner and the evaluator does not crash -- the
evaluator reports `grade: fail` for a patch it simply could not find.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTANCES_DIR = REPO_ROOT / "docker" / "instances"
OUTPUTS_DIR = REPO_ROOT / "outputs"

PATCH_NAME = "patch.diff"
TRAJECTORY_NAME = "trajectory.json"

PROJECTS = ("zephyr", "nuttx", "riot")

# LiteLLM model name -> directory name under outputs/<repo>/.
# Unknown models raise rather than silently creating a new directory.
MODEL_DIRS: dict[str, str] = {
    "anthropic/claude-opus-4-6": "claude-opus-4-6",
    "gemini/gemini-2.5-pro": "gemini-2.5-pro",
    "openai/gpt-5.4": "gpt-5.4",
    "together_ai/Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8": "Qwen-3-coder",
    "together_ai/deepseek-ai/DeepSeek-V3": "DeepSeek-V3",
}

# Accepted shorthands, so --model gpt-5.4 works as well as --model openai/gpt-5.4.
MODEL_ALIASES: dict[str, str] = {short: full for full, short in MODEL_DIRS.items()}

_INSTANCE_RE = re.compile(r"^(?P<project>[a-z]+)__(?P=project)-(?P<pr>\d+)$")


class UnknownModelError(ValueError):
    """Raised for a model with no entry in MODEL_DIRS."""


def split_instance(instance_id: str) -> tuple[str, str]:
    """'zephyr__zephyr-65697' -> ('zephyr', '65697')."""
    m = _INSTANCE_RE.match(instance_id)
    if not m:
        raise ValueError(
            f"malformed instance id {instance_id!r}; expected <project>__<project>-<PR>"
        )
    return m.group("project"), m.group("pr")


def resolve_model(name: str) -> str:
    """Accept either a full LiteLLM name or a directory shorthand; return the full name."""
    if name in MODEL_DIRS:
        return name
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    raise UnknownModelError(
        f"unknown model {name!r}. Known: "
        + ", ".join(sorted(MODEL_DIRS) + sorted(MODEL_ALIASES))
    )


def model_dir(model_name: str) -> str:
    """Full LiteLLM name (or shorthand) -> the directory name used under outputs/."""
    return MODEL_DIRS[resolve_model(model_name)]


def run_dir(instance_id: str, model_name: str) -> Path:
    """outputs/<repo>/<model>/<PR>/ for one (instance, model) pair."""
    project, pr = split_instance(instance_id)
    return OUTPUTS_DIR / project / model_dir(model_name) / pr


def patch_path(instance_id: str, model_name: str) -> Path:
    return run_dir(instance_id, model_name) / PATCH_NAME


def trajectory_path(instance_id: str, model_name: str) -> Path:
    return run_dir(instance_id, model_name) / TRAJECTORY_NAME


def instance_dir(instance_id: str) -> Path:
    return INSTANCES_DIR / instance_id


def load_metadata(instance_id: str) -> dict:
    path = instance_dir(instance_id) / "metadata.json"
    if not path.exists():
        raise FileNotFoundError(f"metadata.json not found at {path}")
    with open(path) as f:
        return json.load(f)


def discover_instances(project: str | None = None) -> list[str]:
    """All instance ids that have a metadata.json, optionally filtered by project."""
    found = []
    for d in sorted(INSTANCES_DIR.iterdir()):
        if not (d.is_dir() and (d / "metadata.json").exists()):
            continue
        if project and split_instance(d.name)[0] != project:
            continue
        found.append(d.name)
    return found
