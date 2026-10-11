"""
Apptainer plumbing shared by the Zephyr twister pipeline: where large files
live, how images are built, and the only way anything gets deleted.

Images are layered, each built FROM the previous one (Bootstrap: localimage):

    python-<py>.sif              Ubuntu 24.04, build tools, uv, Python <py>.
                                 Shared by every PR of that Python; the dry
                                 run uses it directly.
    zephyr-sdk-<sdk>-py<py>.sif  + the Zephyr SDK (host tools, x86 compiler).
                                 Shared by every PR of that Python and SDK.
    instances/<id>.sif           + the PR's workspace: Zephyr at the base
                                 commit, modules, requirements, test patch,
                                 pre-build. One per instance; deleted once the
                                 instance has been validated.

All of it lives under the work root -- $EMBEDEVAL_WORK, else
$SCRATCH/embedeval-twister, else work/ in the repo -- never in the repo itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates" / "zephyr" / "apptainer"


def work_root() -> Path:
    if os.environ.get("EMBEDEVAL_WORK"):
        root = Path(os.environ["EMBEDEVAL_WORK"])
    elif os.environ.get("SCRATCH"):
        root = Path(os.environ["SCRATCH"]) / "embedeval-twister"
    else:
        root = REPO_ROOT / "work"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def path(*parts: str) -> Path:
    p = work_root().joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def build_env() -> dict:
    """Keep Apptainer's cache and temporary build trees on the work root: home
    has a small quota and /tmp is node-local and small."""
    env = dict(os.environ)
    env["APPTAINER_CACHEDIR"] = str(path("cache", "apptainer", "x").parent)
    tmp = path("tmp", "x").parent
    tmp.mkdir(parents=True, exist_ok=True)
    env["APPTAINER_TMPDIR"] = str(tmp)
    return env


def python_image(py: str) -> Path:
    return path("images", f"python-{py}.sif")


def sdk_image(sdk: str, py: str) -> Path:
    return path("images", f"zephyr-sdk-{sdk}-py{py}.sif")


def instance_image(instance_id: str) -> Path:
    return path("images", "instances", f"{instance_id}.sif")


def build(image: Path, definition: Path, build_args: dict, log: Path,
          timeout_min: int = 60, attempts: int = 2) -> bool:
    """apptainer build --fakeroot, with a time limit and a retry.

    The retry is for the network, not for broken recipes: an image build once
    hung for 21 minutes inside `apt-get update`. A recipe that fails the same
    way twice is reported as failed.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    # Absolute paths: the build runs from the recipe's directory, so that the
    # recipe's %files entries resolve next to it.
    image, definition = Path(image).resolve(), Path(definition).resolve()
    cmd = ["apptainer", "build", "--fakeroot", "--force"]
    for k, v in build_args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    cmd += [str(image), str(definition)]
    for attempt in range(1, attempts + 1):
        start = time.time()
        with open(log, "a") as fh:
            fh.write(f"\n=== attempt {attempt}: {' '.join(cmd)}\n")
            fh.flush()
            try:
                rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=build_env(),
                                    cwd=definition.parent, timeout=timeout_min * 60).returncode
            except subprocess.TimeoutExpired:
                rc = "timeout"
            fh.write(f"=== attempt {attempt}: rc={rc} after {(time.time() - start) / 60:.1f} min\n")
        if rc == 0 and image.exists():
            return True
        print(f"    build attempt {attempt} failed (rc={rc}); log: {log}")
    return False


def ensure_base_images(py: str, sdk: str, sdk_archive: str, logdir: Path) -> Path:
    """Build the shared Python and SDK images if they do not exist yet."""
    pimg = python_image(py)
    if not pimg.exists():
        print(f"  building shared image {pimg.name} ...")
        if not build(pimg, TEMPLATES / "python.def", {"PYTHON": py},
                     logdir / f"build-{pimg.stem}.log"):
            raise RuntimeError(f"could not build {pimg.name}")
    simg = sdk_image(sdk, py)
    if not simg.exists():
        print(f"  building shared image {simg.name} ...")
        if not build(simg, TEMPLATES / "sdk.def",
                     {"PYTHON_IMAGE": str(pimg), "SDK": sdk, "SDK_ARCHIVE": sdk_archive},
                     logdir / f"build-{simg.stem}.log"):
            raise RuntimeError(f"could not build {simg.name}")
    return simg


def exec_args(image: Path, binds: dict[Path, str] | None = None,
              overlay: Path | None = None, env: dict | None = None) -> list[str]:
    """`apptainer exec` with nothing from the host leaking in: --containall
    gives a fresh home and /tmp, --cleanenv drops the host environment (the
    cluster's LD_LIBRARY_PATH breaks the SDK's QEMU; its Python confuses CMake)."""
    cmd = ["apptainer", "exec", "--containall", "--cleanenv"]
    if overlay:
        cmd += ["--overlay", str(overlay)]
    for src, dst in (binds or {}).items():
        cmd += ["--bind", f"{src}:{dst}"]
    for k, v in (env or {}).items():
        cmd += ["--env", f"{k}={v}"]
    return cmd + [str(image)]


def remove(target: Path) -> bool:
    """Delete a file or directory, but only inside the work root.

    The single deletion path in the pipeline. Refuses the work root itself,
    anything outside it, and anything that does not resolve -- an unset
    variable must never turn into deleting something real.
    """
    root = work_root()
    t = Path(target).resolve()
    if t == root or root not in t.parents:
        raise ValueError(f"refusing to delete {t}: not inside {root}")
    if not t.exists():
        return False
    if t.is_dir():
        # Overlay work/ directories are created without permissions; restore
        # them so the tree can be removed.
        for d, dirs, _ in os.walk(t):
            for name in dirs:
                try:
                    os.chmod(os.path.join(d, name), 0o700)
                except OSError:
                    pass
        shutil.rmtree(t)
    else:
        t.unlink()
    return True
