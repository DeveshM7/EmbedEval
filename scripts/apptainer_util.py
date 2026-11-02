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

All of it lives under the work root -- $EMBEDEVAL_WORK, else <run dir>/work
with --run-dir (scripts/layout.py), else $SCRATCH/embedeval-twister, else
work/ in the repo -- never in the repo itself. What every run reuses (the
Python and SDK images, dry-run venvs, the uv cache) lives under the shared
root, $EMBEDEVAL_SHARED; without it that is the work root, or, in a run, the
default work root, where the shared images have always been.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import layout

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates" / "zephyr" / "apptainer"


def _default_root() -> Path:
    if os.environ.get("SCRATCH"):
        return Path(os.environ["SCRATCH"]) / "embedeval-twister"
    return REPO_ROOT / "work"


def work_root() -> Path:
    """This run's images, logs, overlays and temp files."""
    if os.environ.get("EMBEDEVAL_WORK"):
        root = Path(os.environ["EMBEDEVAL_WORK"])
    elif layout.run_dir():
        root = layout.run_dir() / "work"
    else:
        root = _default_root()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def shared_root() -> Path:
    """What every run reuses: Python and SDK images, dry-run venvs, uv cache."""
    if os.environ.get("EMBEDEVAL_SHARED"):
        root = Path(os.environ["EMBEDEVAL_SHARED"])
    elif layout.run_dir() and not os.environ.get("EMBEDEVAL_WORK"):
        root = _default_root()
    else:
        return work_root()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def path(*parts: str) -> Path:
    p = work_root().joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def shared_path(*parts: str) -> Path:
    p = shared_root().joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def build_env(tmp: Path | None = None) -> dict:
    """Keep Apptainer's download cache on the shared root and its temporary
    build trees on the work root: home has a small quota and /tmp is
    node-local and small."""
    env = dict(os.environ)
    env["APPTAINER_CACHEDIR"] = str(shared_path("cache", "apptainer", "x").parent)
    tmp = tmp or path("tmp", "x").parent
    tmp.mkdir(parents=True, exist_ok=True)
    env["APPTAINER_TMPDIR"] = str(tmp)
    return env


def _descendants(pid: int) -> list[int]:
    """Every process below `pid`, from /proc."""
    children: dict[int, list[int]] = {}
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(int(d.name))
    out, todo = [], [pid]
    while todo:
        for c in children.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def _holders(folder: Path) -> list[int]:
    """This user's processes whose working directory, root or an open file is
    inside `folder` -- such as the fakeroot daemon of a build, which detaches
    itself and so is no descendant of the build."""
    root = Path(folder).resolve()
    folder = str(root)
    # A process in its own mount namespace (the container's) shows its root as
    # "/", so match it by inode: its root is a directory near the top of the
    # build's temporary folder (<tmp>/build-temp-*/rootfs).
    inodes = set()
    for d, dirs, _ in os.walk(root):
        depth = len(Path(d).relative_to(root).parts)
        if depth >= 3:
            dirs.clear()
        try:
            st = os.stat(d)
            inodes.add((st.st_dev, st.st_ino))
        except OSError:
            pass
    me, out = os.getuid(), []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            if d.stat().st_uid != me:
                continue
            st = os.stat(d / "root")
            links = [os.readlink(d / "cwd"), os.readlink(d / "root")]
            links += [os.readlink(f) for f in (d / "fd").iterdir()]
        except OSError:
            continue
        if (st.st_dev, st.st_ino) in inodes or any(
                l == folder or l.startswith(folder + "/") for l in links):
            out.append(int(d.name))
    return out


def _stop(pids: list[int]) -> None:
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        time.sleep(3)


def _run_build(cmd: list[str], fh, env: dict, cwd: Path, timeout: int) -> int | str:
    """Run one build; on a timeout stop it with everything it started.
    Killing `apptainer build` alone leaves the container's processes (apt-get,
    the fakeroot daemon) running, holding its temporary files open."""
    proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env, cwd=cwd)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop([proc.pid] + _descendants(proc.pid))
        proc.wait()
        return "timeout"


def recipe_tag(definition: str) -> str:
    """A short fingerprint of a shared image's recipe. It is part of the
    image's name, so changing the recipe (e.g. adding a compiler to sdk.def)
    builds a new image instead of silently reusing the old one."""
    import hashlib
    return hashlib.sha256((TEMPLATES / definition).read_bytes()).hexdigest()[:8]


def python_image(py: str) -> Path:
    return shared_path("images", f"python-{py}-{recipe_tag('python.def')}.sif")


def sdk_image(sdk: str, py: str) -> Path:
    return shared_path("images", f"zephyr-sdk-{sdk}-py{py}-{recipe_tag('python.def')}"
                                 f"{recipe_tag('sdk.def')}.sif")


def host_tag() -> str:
    """Return an architecture suffix for host-specific shared images."""
    import platform
    return "" if platform.machine() == "aarch64" else f"-{platform.machine()}"


def require_host_arch(meta: dict) -> None:
    """Reject an instance on a host architecture it cannot validate on."""
    import platform
    want = meta.get("host_arch")
    if want and want != platform.machine():
        raise RuntimeError(f"{meta.get('instance_id')} must run on {want} hosts "
                           f"(this is {platform.machine()}): it validates there only")


MYNEWT_TEMPLATES = REPO_ROOT / "templates" / "mynewt" / "apptainer"


def mynewt_clone() -> Path | None:
    """Local mynewt-core clone, if configured or available in candidates/."""
    value = os.environ.get("EMBEDEVAL_MYNEWT_CLONE")
    clone = Path(value) if value else REPO_ROOT / "candidates" / "mynewt.git"
    return clone if clone.is_dir() else None


def mynewt_base_image() -> Path:
    """Return the shared Mynewt base image, fingerprinted by its recipe."""
    import hashlib
    h = hashlib.sha256((MYNEWT_TEMPLATES / "base.def").read_bytes())
    return shared_path("images", f"mynewt-sim-{h.hexdigest()[:8]}{host_tag()}.sif")


def ensure_mynewt_base_image(logdir: Path) -> Path:
    """Build the shared Mynewt image if it does not exist yet."""
    img = mynewt_base_image()
    if not img.exists():
        print(f"  building shared image {img.name} ...")
        if not build(img, MYNEWT_TEMPLATES / "base.def", {}, logdir / f"build-{img.stem}.log"):
            raise RuntimeError(f"could not build {img.name}")
    return img


def unpack_sandbox(image: Path, parent: Path | None = None) -> Path:
    """Unpack an image to a writable sandbox for fast Mynewt builds."""
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="embedeval-sbx-",
                                 dir=parent or os.environ.get("EMBEDEVAL_SANDBOX_DIR") or None))
    root = base / "root"
    r = subprocess.run(["apptainer", "build", "--sandbox", str(root), str(image)],
                       capture_output=True, text=True, env=build_env(base))
    if r.returncode:
        remove_sandbox(root)
        raise RuntimeError(f"could not unpack {image.name}: {r.stderr.strip()[-300:]}")
    (root / "embedeval-run").mkdir(exist_ok=True)
    return root


def remove_sandbox(root: Path) -> None:
    """Remove a sandbox created by unpack_sandbox."""
    base = Path(root).parent
    if not base.name.startswith("embedeval-sbx-"):
        raise ValueError(f"refusing to delete {base}: not a sandbox folder")
    subprocess.run(["chmod", "-R", "u+rwX", str(base)], capture_output=True)
    shutil.rmtree(base, ignore_errors=True)


def instance_image(instance_id: str) -> Path:
    return path("images", "instances", f"{instance_id}.sif")


def build(image: Path, definition: Path, build_args: dict, log: Path,
          timeout_min: int = 60, attempts: int = 2,
          binds: dict[Path, str] | None = None) -> bool:
    """apptainer build --fakeroot, with a time limit and a retry.

    The retry is for the network, not for broken recipes: an image build once
    hung for 21 minutes inside `apt-get update`. A recipe that fails the same
    way twice is reported as failed.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    # Absolute paths: the build runs from the recipe's directory, so that the
    # recipe's %files entries resolve next to it.
    image, definition = Path(image).resolve(), Path(definition).resolve()
    # Built under a temporary name and renamed only once complete: Apptainer
    # writes straight to its output, so an interrupted build would otherwise
    # leave a truncated image that every later check takes for a built one.
    partial = image.with_name(f".{image.name}.partial-{os.getpid()}")
    cmd = ["apptainer", "build", "--fakeroot", "--force"]
    for k, v in build_args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    for src, dst in (binds or {}).items():
        cmd += ["--bind", f"{src}:{dst}:ro"]
    cmd += [str(partial), str(definition)]
    for attempt in range(1, attempts + 1):
        start = time.time()
        # A temporary folder of its own, removed whatever happens: parallel
        # builds stay apart, and a failed one leaves nothing behind.
        tmp = path("tmp", f"build-{image.stem}-{os.getpid()}-{attempt}", "x").parent
        with open(log, "a") as fh:
            fh.write(f"\n=== attempt {attempt}: {' '.join(cmd)}\n")
            fh.flush()
            try:
                rc = _run_build(cmd, fh, build_env(tmp), definition.parent, timeout_min * 60)
            finally:
                _stop(_holders(tmp))     # e.g. a detached fakeroot daemon
                try:
                    remove(tmp)
                except OSError as e:
                    fh.write(f"=== could not remove {tmp}: {e}\n")
            fh.write(f"=== attempt {attempt}: rc={rc} after {(time.time() - start) / 60:.1f} min\n")
        if rc == 0 and partial.exists():
            partial.replace(image)
            return True
        if partial.exists():
            partial.unlink()
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


def sandbox_exec_args(sandbox: Path, binds: dict[Path, str] | None = None,
                      env: dict | None = None) -> list[str]:
    """Build exec_args for a writable sandbox."""
    cmd = ["apptainer", "exec", "--containall", "--cleanenv", "--writable"]
    for src, dst in (binds or {}).items():
        cmd += ["--bind", f"{src}:{dst}"]
    for k, v in (env or {}).items():
        cmd += ["--env", f"{k}={v}"]
    return cmd + [str(sandbox)]


# The files an instance image is built from. Their fingerprint is recorded in
# validation.json, so a recipe changed after validation is never promoted.
RECIPE_FILES = ("instance.def", "instance.json", "run_tests.py", "test_patch.diff")
MYNEWT_RECIPE_FILES = ("instance.def", "run_tests.sh", "test_patch.diff", "bench.json")


def mynewt_recipe_files(instance_dir: Path) -> tuple[str, ...]:
    """Recipe files for a Mynewt instance, including optional compatibility patches."""
    return MYNEWT_RECIPE_FILES + tuple(name for name in ("sim_arm64.patch", "simulator_compat.diff")
                                      if (Path(instance_dir) / name).exists())


def recipe_fingerprint(instance_dir: Path, files: tuple[str, ...] = RECIPE_FILES) -> str:
    import hashlib
    h = hashlib.sha256()
    for name in files:
        h.update(name.encode() + b"\0" + (Path(instance_dir) / name).read_bytes() + b"\0")
    return h.hexdigest()


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
