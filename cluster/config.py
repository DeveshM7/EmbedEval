"""
The cluster settings: cluster/config.example.yaml, overlaid by the user's
cluster/config.yaml (or --config FILE).

Every setting has a default in the example file, so config.yaml need only
hold what differs. An unknown or misspelled setting, or one of the wrong type,
is an error naming it -- caught here, not halfway through a job.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

CLUSTER_DIR = Path(__file__).resolve().parent
REPO_ROOT = CLUSTER_DIR.parent
EXAMPLE = CLUSTER_DIR / "config.example.yaml"
DEFAULT = CLUSTER_DIR / "config.yaml"


class ConfigError(Exception):
    pass


def _expand(value):
    if isinstance(value, str):
        def var(m):
            name = m.group(1)
            if name not in os.environ:
                raise ConfigError(f"${{{name}}} is used in the config but not set")
            return os.environ[name]
        return re.sub(r"\$\{(\w+)\}", var, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _overlay(base: dict, user: dict, where: str = "") -> dict:
    out = dict(base)
    for key, value in user.items():
        name = f"{where}{key}"
        if key not in base:
            raise ConfigError(f"unknown setting {name!r}; known here: {', '.join(base)}")
        default = base[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise ConfigError(f"{name!r} must be a section of settings")
            out[key] = _overlay(default, value, name + ".")
        elif default is None or value is None:
            out[key] = value                       # optional paths
        elif isinstance(default, bool) != isinstance(value, bool) or (
                not isinstance(value, type(default))
                and not (isinstance(default, float) and isinstance(value, int))):
            raise ConfigError(f"{name!r} must be {type(default).__name__}, "
                              f"got {type(value).__name__} {value!r}")
        else:
            out[key] = value
    return out


def load(path: str | Path | None = None) -> dict:
    """The settings, with every path expanded and the derived ones filled in."""
    base = yaml.safe_load(EXAMPLE.read_text())
    path = Path(path) if path else DEFAULT
    user = {}
    if path.exists():
        user = yaml.safe_load(path.read_text()) or {}
        if not isinstance(user, dict):
            raise ConfigError(f"{path}: not a mapping of settings")
    elif path != DEFAULT:
        raise ConfigError(f"no config file {path}")
    cfg = _expand(_overlay(base, user))
    if cfg["project"] not in ("zephyr", "nuttx"):
        raise ConfigError(f"project must be zephyr or nuttx, not {cfg['project']!r}")
    root = Path(cfg["scratch_root"])
    p = cfg["paths"]
    shared = Path(p["shared"]) if p["shared"] else root / "shared"
    cfg["derived"] = {
        "config_file": str(path) if path.exists() else None,
        "root": str(root),
        "shared": str(shared),
        "runs": str(root / "runs"),
        "zephyr_clone": p["zephyr_clone"] or str(shared / "zephyr.git"),
        "vllm_cache": p["vllm_cache"] or str(shared / "vllm-cache"),
        "ledger": str(shared / ("ledger.jsonl" if cfg["project"] == "zephyr"
                                else f"ledger-{cfg['project']}.jsonl")),
    }
    return cfg


def env_for(cfg: dict, run_dir: Path | None = None) -> dict:
    """The environment the pipeline scripts read (scripts/layout.py,
    scripts/apptainer_util.py, scripts/serve_triage_model.sh)."""
    d, p = cfg["derived"], cfg["paths"]
    env = {"EMBEDEVAL_SHARED": d["shared"],
           "EMBEDEVAL_ZEPHYR_CLONE": d["zephyr_clone"],
           "EMBEDEVAL_MODELS": p["models"],
           "EMBEDEVAL_VLLM_IMAGE": p["vllm_image"]}
    if cfg["project"] == "nuttx" and cfg["nuttx"]["data"]:
        env["EMBEDEVAL_NUTTX_DATA"] = cfg["nuttx"]["data"]
    if run_dir is not None:
        env["EMBEDEVAL_RUN_DIR"] = str(run_dir)
    return env


def load_or_exit(path=None) -> dict:
    try:
        return load(path)
    except (ConfigError, yaml.YAMLError) as e:
        sys.exit(f"config error: {e}")


if __name__ == "__main__":
    import json
    print(json.dumps(load_or_exit(sys.argv[1] if len(sys.argv) > 1 else None), indent=2))
