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

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import layout

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates" / "zephyr" / "apptainer"
OPENTHREAD_TEMPLATES = REPO_ROOT / "templates" / "openthread" / "apptainer"

# Instance pipelines built, validated and run as Apptainer images.
APPTAINER_PIPELINES = ("twister", "cmake", "contiki-ng")


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


def recipe_tag(definition: str, templates: Path = TEMPLATES) -> str:
    """A short fingerprint of a shared image's recipe. It is part of the
    image's name, so changing the recipe (e.g. adding a compiler to sdk.def)
    builds a new image instead of silently reusing the old one."""
    import hashlib
    return hashlib.sha256((templates / definition).read_bytes()).hexdigest()[:8]


def python_image(py: str) -> Path:
    return shared_path("images", f"python-{py}-{recipe_tag('python.def')}.sif")


def sdk_image(sdk: str, py: str) -> Path:
    return shared_path("images", f"zephyr-sdk-{sdk}-py{py}-{recipe_tag('python.def')}"
                                 f"{recipe_tag('sdk.def')}.sif")


def openthread_image() -> Path:
    return shared_path("images", f"openthread-base-{recipe_tag('base.def', OPENTHREAD_TEMPLATES)}.sif")


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
    # Built under a temporary name and renamed only once complete: Apptainer
    # writes straight to its output, so an interrupted build would otherwise
    # leave a truncated image that every later check takes for a built one.
    partial = image.with_name(f".{image.name}.partial-{os.getpid()}")
    cmd = ["apptainer", "build", "--fakeroot", "--force"]
    for k, v in build_args.items():
        cmd += ["--build-arg", f"{k}={v}"]
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
                if rc and I386_REFUSAL in Path(log).read_text(errors="replace"):
                    fh.write("=== the base is a 32-bit userland: building through a sandbox\n")
                    fh.flush()
                    rc = _build_via_sandbox(partial, definition, build_args, fh, build_env(tmp),
                                            tmp, timeout_min * 60)
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


I386_REFUSAL = "image targets '386', cannot run on 'amd64'"


def parse_definition(text: str, build_args: dict) -> dict:
    """The parts of a localimage recipe that _build_via_sandbox needs: the base
    image, %files pairs and the %post, %environment and %labels bodies."""
    header, sections, current = {}, {}, None
    for line in text.splitlines():
        if line.startswith("%"):
            current = line[1:].split()[0]
            sections[current] = []
        elif current is None:
            if ":" in line:
                key, value = line.split(":", 1)
                header[key.strip()] = value.strip()
        else:
            sections[current].append(line)
    base = header.get("From", "")
    for key, value in build_args.items():
        base = base.replace("{{ " + key + " }}", str(value))
    files = [tuple(l.split()) for l in sections.get("files", []) if l.split()]
    labels = dict(l.split(None, 1) for l in sections.get("labels", []) if len(l.split(None, 1)) == 2)
    return {"base": base, "files": files, "post": "\n".join(sections.get("post", [])),
            "environment": "\n".join(sections.get("environment", [])), "labels": labels}


def _build_via_sandbox(partial: Path, definition: Path, build_args: dict, fh, env: dict,
                       tmp: Path, timeout: int) -> int | str:
    """What `apptainer build` does for a localimage recipe, for a 32-bit base:
    Apptainer refuses to run %post (even under fakeroot) in an i386 userland
    on amd64, but runs it as the user in a writable sandbox. %post needs no
    root here: it only writes the checkout and files the sandbox's owner owns."""
    spec = parse_definition(definition.read_text(), build_args)
    rootfs = tmp / "rootfs"
    steps = [["apptainer", "build", "--sandbox", str(rootfs), spec["base"]]]
    rc = _run_build(steps[0], fh, env, definition.parent, timeout)
    if rc:
        return rc
    for src, dst in spec["files"]:
        target = rootfs / dst.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(definition.parent / src, target)
    script = rootfs / "opt" / "embedeval" / ".post.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(spec["post"] + "\n")
    rc = _run_build(["apptainer", "exec", "--writable", "--pwd", "/", str(rootfs),
                     "/bin/sh", "-e", "/opt/embedeval/.post.sh"], fh, env, definition.parent, timeout)
    script.unlink(missing_ok=True)
    if rc:
        return rc
    meta = rootfs / ".singularity.d"
    (meta / "env").mkdir(parents=True, exist_ok=True)
    (meta / "env" / "90-environment.sh").write_text("#!/bin/sh\n" + spec["environment"] + "\n")
    labels = meta / "labels.json"
    known = json.loads(labels.read_text()) if labels.exists() else {}
    labels.write_text(json.dumps({**known, **spec["labels"]}, indent=2) + "\n")
    return _run_build(["apptainer", "build", "--force", str(partial), str(rootfs)],
                      fh, env, definition.parent, timeout)


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


def ensure_openthread_base(logdir: Path) -> Path:
    """Build the shared OpenThread toolchain image if it does not exist yet."""
    img = openthread_image()
    if not img.exists():
        print(f"  building shared image {img.name} ...")
        if not build(img, OPENTHREAD_TEMPLATES / "base.def", {}, logdir / f"build-{img.stem}.log"):
            raise RuntimeError(f"could not build {img.name}")
    return img


_I386: dict[Path, bool] = {}


def is_i386_sif(image: Path) -> bool:
    """Whether a SIF holds a 32-bit (i386) userland (older Contiki CI eras)."""
    image = Path(image)
    if image.is_dir() or image.suffix != ".sif" or not image.exists():
        return False
    if image not in _I386:
        listing = subprocess.run(["apptainer", "sif", "list", str(image)], capture_output=True,
                                 text=True).stdout
        _I386[image] = "/386)" in listing
    return _I386[image]


def local_sandbox_root() -> Path:
    """Node-local, never NFS: see runnable_image."""
    import getpass, tempfile
    return Path(os.environ.get("EMBEDEVAL_LOCAL_SANDBOXES")
                or Path(tempfile.gettempdir()) / f"embedeval-sandboxes-{getpass.getuser()}")


def local_sandbox(image: Path) -> Path:
    import hashlib
    st = Path(image).stat()
    key = hashlib.sha256(f"{Path(image).resolve()}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:12]
    return local_sandbox_root() / f"{Path(image).stem}-{key}"


def runnable_image(image: Path) -> Path:
    """The image to exec. A 32-bit userland cannot run from its SIF here: the
    SIF is FUSE-mounted with 64-bit inode numbers, so its 32-bit stat() calls
    fail with EOVERFLOW (the i386 JVM reports its own libjvm.so missing). Such
    an image runs from a sandbox extracted once on node-local disk."""
    if not is_i386_sif(image):
        return Path(image)
    target = local_sandbox(image)
    if (target / ".embedeval-complete").exists():
        return target
    import fcntl
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target.with_suffix(".lock"), "w") as lock:
        fcntl.lockf(lock, fcntl.LOCK_EX)
        if not (target / ".embedeval-complete").exists():
            partial = target.with_name(target.name + ".partial")
            shutil.rmtree(partial, ignore_errors=True)
            env = {**os.environ, "APPTAINER_TMPDIR": str(target.parent)}
            r = subprocess.run(["apptainer", "build", "--sandbox", str(partial), str(image)],
                               capture_output=True, text=True, env=env, timeout=3600)
            if r.returncode:
                raise RuntimeError(f"could not extract {image} to {partial}: {r.stderr[-500:]}")
            shutil.rmtree(target, ignore_errors=True)
            partial.rename(target)
            (target / ".embedeval-complete").touch()
    return target


def remove_local_sandbox(image: Path) -> list[Path]:
    """Delete an image's node-local sandboxes (only under local_sandbox_root)."""
    root = local_sandbox_root().resolve()
    gone = []
    for d in root.glob(f"{Path(image).stem}-*") if root.is_dir() else []:
        if d.resolve().parent == root and not d.name.endswith(".lock"):
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d)
    return gone


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
    return cmd + [str(runnable_image(image))]


# The files an instance image is built from. Their fingerprint is recorded in
# validation.json, so a recipe changed after validation is never promoted.
RECIPE_FILES = ("instance.def", "instance.json", "run_tests.py", "test_patch.diff")


def recipe_fingerprint(instance_dir: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    for name in RECIPE_FILES:
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


CONTIKI_NG_CURRENT_IMAGE = "dcf5962b018eabbc69bbbc3d67bd74e586c605b9b367d92adc51906c0c90ff05"


def ensure_contiki_ng_base(logdir: Path | None = None, tag: str | None = None) -> Path:
    """The upstream CI image: the current one (or its EMBEDEVAL_CONTIKI_NG_IMAGE
    override), or an older era image named by tag, pulled once into shared/."""
    import platform
    if platform.machine() not in ("x86_64", "amd64"):
        raise RuntimeError("Contiki-NG CI image requires an x86_64 build worker")
    tag = tag or CONTIKI_NG_CURRENT_IMAGE
    override = os.environ.get("EMBEDEVAL_CONTIKI_NG_IMAGE") if tag == CONTIKI_NG_CURRENT_IMAGE else None
    image = Path(override).expanduser().resolve() if override else shared_path("images", "contiki-ng-ci-dcf5962b-x86_64.sif" if tag == CONTIKI_NG_CURRENT_IMAGE
                                                        else f"contiki-ng-ci-{tag[:9]}-x86_64.sif")
    if image.exists():
        return image
    if override:
        raise RuntimeError(f"Contiki-NG image override does not exist: {image}")
    image.parent.mkdir(parents=True, exist_ok=True)
    partial = image.with_suffix(".partial.sif")
    source = f"docker://contiker/contiki-ng:{tag}"
    logs = logdir or path("logs", "bases", "x").parent
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "contiki-ng-pull.log").open("w") as fh:
        r = subprocess.run(["apptainer", "pull", "--force", str(partial), source],
                           stdout=fh, stderr=subprocess.STDOUT, env=build_env(), timeout=2400)
    if r.returncode:
        raise RuntimeError("Contiki-NG CI image pull failed; inspect contiki-ng-pull.log")
    with (logs / "contiki-ng-pull.log").open("a") as fh:
        _amd64_sif(partial, fh)
    partial.replace(image)
    return image


def _amd64_sif(sif: Path, log) -> None:
    """Older CI images are 32-bit (i386) userlands. They run on x86_64, but
    Apptainer refuses to build on an image whose SIF says '386'; repack such
    an image through a sandbox so its SIF records amd64. Files are unchanged."""
    listing = subprocess.run(["apptainer", "sif", "list", str(sif)], capture_output=True, text=True).stdout
    if "/386)" not in listing:
        return
    import tempfile
    env = build_env()
    with tempfile.TemporaryDirectory(dir=env["APPTAINER_TMPDIR"]) as tmp:
        sandbox, repacked = Path(tmp) / "rootfs", Path(tmp) / "repacked.sif"
        for cmd in (["apptainer", "build", "--sandbox", str(sandbox), str(sif)],
                    ["apptainer", "build", str(repacked), str(sandbox)]):
            if subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=3600).returncode:
                raise RuntimeError(f"repacking the i386 image {sif.name} failed; see the pull log")
        shutil.move(str(repacked), str(sif))
