#!/usr/bin/env python3
"""
Validated instance images in a container registry, so that evaluation pulls
the image that was validated instead of rebuilding it.

    python scripts/registry.py push zephyr__zephyr-95188       # after validation
    python scripts/registry.py pull zephyr__zephyr-95188       # into the work root
    python scripts/registry.py publish zephyr__zephyr-95188 ... # build, validate, push

The registry is EMBEDEVAL_REGISTRY, e.g. oras://ghcr.io/deveshm7/embedeval-zephyr
(cluster/config.yaml `registry`); each image is the tag named after its
instance. A push records where the image went in the instance's
validation.json, so the record travels with the recipe when it is promoted:

    "published": {"ref": "oras://ghcr.io/deveshm7/embedeval-zephyr:zephyr__zephyr-95188",
                  "sha256": "1a7586...", "bytes": 2661556224, "pushed_at": "..."}

A pull checks the file against that sha256 and refuses anything else, so an
evaluation always runs the exact image that was validated. A tag can be
pushed again; the sha256 is what pins the image.

Pushing needs a GitHub token allowed to write packages: it is read from the
gh CLI when needed (`gh auth token`; once: `gh auth refresh -s
write:packages`) and given to apptainer through its environment only. Pulling
a public package needs no token. A new package is private: make it public
once on its GitHub page (Package settings, Change visibility).

`publish` is for instances validated before the registry existed (the
committed ones): their images were deleted after validation, so each is
built again, validated again (validation.json is rewritten) and pushed only
if VALID. The work root (EMBEDEVAL_WORK) holds the images while it runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apptainer_util as au  # noqa: E402
import instances  # noqa: E402

HERE = Path(__file__).resolve().parent


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 24), b""):
            h.update(chunk)
    return h.hexdigest()


def _report(instance_id: str) -> tuple[Path, dict]:
    f = instances.instance_dir(instance_id) / "validation.json"
    return f, (json.loads(f.read_text()) if f.exists() else {})


def published(instance_id: str) -> dict | None:
    return _report(instance_id)[1].get("published")


def _gh() -> str:
    return shutil.which("gh") or str(Path.home() / ".local" / "bin" / "gh")


def _push_env() -> dict:
    token = subprocess.run([_gh(), "auth", "token"], capture_output=True, text=True,
                           timeout=60).stdout.strip()
    user = subprocess.run([_gh(), "api", "user", "--jq", ".login"], capture_output=True,
                          text=True, timeout=60).stdout.strip()
    if not token or not user:
        raise RuntimeError("no GitHub token from `gh auth token` (gh auth login, then "
                           "gh auth refresh -s write:packages)")
    return {**os.environ, "APPTAINER_DOCKER_USERNAME": user, "APPTAINER_DOCKER_PASSWORD": token}


def push(instance_id: str, registry: str | None = None, timeout: int = 1800) -> dict:
    """Push a VALID instance's image and record it in validation.json."""
    registry = (registry or os.environ.get("EMBEDEVAL_REGISTRY") or "").rstrip("/")
    if not registry:
        raise RuntimeError("no registry: set EMBEDEVAL_REGISTRY (cluster/config.yaml `registry`)")
    report_file, report = _report(instance_id)
    if not report.get("valid"):
        raise RuntimeError(f"{instance_id} is not VALID; only validated images are pushed")
    image = au.instance_image(instance_id)
    if not image.exists():
        raise RuntimeError(f"no image to push: {image}")
    ref = f"{registry}:{instance_id}"
    digest, size, t = sha256(image), image.stat().st_size, time.time()
    r = subprocess.run(["apptainer", "push", str(image), ref], env=_push_env(),
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"apptainer push failed: {(r.stderr or r.stdout).strip()[-300:]}")
    report["published"] = {"ref": ref, "sha256": digest, "bytes": size,
                           "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "seconds": round(time.time() - t)}
    report_file.write_text(json.dumps(report, indent=2) + "\n")
    return report["published"]


def pull(instance_id: str, timeout: int = 1800) -> Path:
    """The published image, in the work root: downloaded unless an identical
    file is there already. Raises if it cannot be had as validated."""
    pub = published(instance_id)
    if not pub:
        raise RuntimeError(f"{instance_id} has no published image (validation.json "
                           "has no 'published'); build it, or push it after validation")
    image = au.instance_image(instance_id)
    if image.exists() and sha256(image) == pub["sha256"]:
        return image
    image.parent.mkdir(parents=True, exist_ok=True)
    part = image.with_name(f".{image.name}.pull-{os.getpid()}")
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPTAINER_DOCKER_")}
    try:
        r = subprocess.run(["apptainer", "pull", "--force", str(part), pub["ref"]], env=env,
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError(f"apptainer pull {pub['ref']} failed: "
                               f"{(r.stderr or r.stdout).strip()[-300:]}")
        got = sha256(part)
        if got != pub["sha256"]:
            raise RuntimeError(f"{pub['ref']} is not the validated image "
                               f"(sha256 {got[:12]}..., validated {pub['sha256'][:12]}...)")
        part.replace(image)               # whole or not at all, even with pulls in parallel
    finally:
        if part.exists():
            au.remove(part)
    return image


def image_for(instance_id: str) -> Path:
    """The image an evaluation should run: the published one when there is
    one (pulled if the local file is missing or differs), else a local build."""
    if published(instance_id):
        return pull(instance_id)
    image = au.instance_image(instance_id)
    if not image.exists():
        raise FileNotFoundError(f"instance image not built and not published: {image} "
                                f"(scripts/build_instances.py --instance {instance_id})")
    return image


def publish(instance_id: str, registry: str | None = None) -> str:
    """Build, validate and push one instance; the image is removed after."""
    py, env = sys.executable, os.environ
    step = lambda *a: subprocess.run([py, str(HERE / a[0]), *a[1:]], env=env).returncode
    try:
        if step("build_instances.py", "--instance", instance_id):
            return "build failed"
        step("validate_instance.py", instance_id)
        if not _report(instance_id)[1].get("valid"):
            return f"not VALID: {_report(instance_id)[1].get('reason')}"
        pub = push(instance_id, registry)
        return f"published {pub['ref']} ({pub['bytes'] / 2**30:.2f} GB, {pub['seconds']} s)"
    finally:
        step("cleanup_instance.py", instance_id)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["push", "pull", "publish"])
    p.add_argument("instance", nargs="+")
    p.add_argument("--registry", help="default: $EMBEDEVAL_REGISTRY")
    args = p.parse_args()
    bad = 0
    for iid in args.instance:
        try:
            if args.command == "push":
                pub = push(iid, args.registry)
                msg = f"pushed {pub['ref']} ({pub['seconds']} s)"
            elif args.command == "pull":
                msg = f"ready {pull(iid)}"
            else:
                msg = publish(iid, args.registry)
                bad += not msg.startswith("published")
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            msg, bad = f"FAILED: {e}", bad + 1
        print(f"  {iid}: {msg}", flush=True)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
