"""
Shared path and naming conventions for EmbedEval.

Every script that reads or writes agent runs or grades must import from here.
Two independent implementations of these rules is how the repo ended up with
three incompatible output layouts and two spellings of "DeepSeek-V3"; worse,
a path mismatch between the runner and the evaluator does not crash -- the
evaluator reports `grade: fail` for a patch it simply could not find.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import layout  # noqa: E402  (first: reads --run-dir; see scripts/layout.py)
from layout import show  # noqa: E402,F401

# Where instance directories are read from: the validated twister-pipeline
# Zephyr instances in apptainer/instances/ (written there only by
# scripts/promote_instance.py), then the Docker instances in docker/instances/.
# EMBEDEVAL_INSTANCES_DIR replaces both -- e.g. generated/, so a freshly
# generated instance can be built and validated before it is promoted, and
# without any chance of overwriting a committed one.
# With --run-dir the run's own instances/ is the default, and outputs go to
# the run's outputs/. The same rule as scripts/instances.py on the build side.
_override = os.environ.get("EMBEDEVAL_INSTANCES_DIR")
INSTANCE_ROOTS = ([Path(_override) if Path(_override).is_absolute() else REPO_ROOT / _override]
                  if _override else [layout.generated()] if layout.run_dir()
                  else [REPO_ROOT / "apptainer" / "instances", REPO_ROOT / "docker" / "instances"])
#
# Agent runs and their grades go to one folder per run,
# <eval dir>/<model>/<project>/<PR>/, never overwritten by another run. The eval
# dir is EMBEDEVAL_EVAL_DIR; else, with --run-dir, the run's outputs/; else
# $SCRATCH/embedeval/eval on the cluster; else outputs/ in the repository.
def _eval_dir() -> Path:
    if os.environ.get("EMBEDEVAL_EVAL_DIR"):
        return Path(os.environ["EMBEDEVAL_EVAL_DIR"]).expanduser().resolve()
    if layout.run_dir():
        return layout.outputs()
    if os.environ.get("SCRATCH"):
        return Path(os.environ["SCRATCH"]) / "embedeval" / "eval"
    return layout.outputs()


OUTPUTS_DIR = _eval_dir()

PATCH_NAME = "patch.diff"
TRAJECTORY_NAME = "trajectory.json"
RUN_NAME = "run.json"        # how the agent's run ended (harness/run.py)
GRADE_NAME = "grade.json"    # its grade; the run is finished once this exists

PROJECTS = ("zephyr", "nuttx", "riot")

# LiteLLM model name -> directory name under the eval dir.
# Unknown models raise rather than silently creating a new directory.
MODEL_DIRS: dict[str, str] = {
    "anthropic/claude-opus-4-6": "claude-opus-4-6",
    "gemini/gemini-2.5-pro": "gemini-2.5-pro",
    "openai/gpt-5.4": "gpt-5.4",
    "together_ai/Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8": "Qwen-3-coder",
    "together_ai/deepseek-ai/DeepSeek-V3": "DeepSeek-V3",
    # Local open-weights model served by scripts/serve_triage_model.sh (vLLM,
    # OpenAI-compatible); see LOCAL_MODELS in harness/run.py.
    "openai/qwen38-nvfp4": "qwen38-nvfp4",
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
    """Full LiteLLM name (or shorthand) -> the directory name used under the eval dir."""
    return MODEL_DIRS[resolve_model(model_name)]


def run_dir(instance_id: str, model_name: str) -> Path:
    """<eval dir>/<model>/<project>/<PR>/ for one (instance, model) pair."""
    project, pr = split_instance(instance_id)
    return OUTPUTS_DIR / model_dir(model_name) / project / pr


def patch_path(instance_id: str, model_name: str) -> Path:
    return run_dir(instance_id, model_name) / PATCH_NAME


def trajectory_path(instance_id: str, model_name: str) -> Path:
    return run_dir(instance_id, model_name) / TRAJECTORY_NAME


def run_record_path(instance_id: str, model_name: str) -> Path:
    return run_dir(instance_id, model_name) / RUN_NAME


def grade_path(instance_id: str, model_name: str) -> Path:
    return run_dir(instance_id, model_name) / GRADE_NAME


def write_json(path: Path, data: dict) -> None:
    """Write through a temporary file, so a reader (or a job killed mid-write)
    never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


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
    """All instance ids that have a metadata.json, optionally filtered by project."""
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
