#!/usr/bin/env python3
"""
A Zephyr instance's git layer: a small read-only layer, stacked on the image,
whose /testbed/.git is a fresh repository with one commit -- the PR's base -- in
place of the image's full clone.

    python scripts/zephyr_gitlayer.py build <instance>...        # build and check
    python scripts/zephyr_gitlayer.py build --push --delete-image <instance>...
    python scripts/zephyr_gitlayer.py build --push --delete-image --shard I/N
    python scripts/zephyr_gitlayer.py check <instance>...        # check a built layer
    python scripts/zephyr_gitlayer.py pull <instance>...         # what evaluation runs

Why: every Zephyr image clones Zephyr's whole history (instance.def: a blobless
clone, then a checkout of the base). Commits made after the base are in it, the
PR's merged fix among them: `git log --all` shows the fix's messages and files,
and `git show` fetches its code. The image is a sealed file, so the history is
hidden instead, by a layer above it.

How the layer is made, in the instance's own container, on a throw-away overlay:
the PR's tests are taken out again (git apply -R of the instance's
test_patch.diff), so the tree is the base; a new repository commits exactly the
files the base commit tracks (some are tracked although .gitignore matches
them); its tree must equal the base commit's tree, or the layer is refused. The
new .git goes into a squashfs marked opaque (.wh..wh..opq): stacked on the
image it replaces the old .git rather than merging with it. The tree in
/testbed is unchanged, PR tests included, so the instance's validation holds.

Each layer is checked from inside the container, as an agent sees it: one
commit, one commit object, no remote, the tree of the base, no file of the
image's old .git, and the PR's tests as the only uncommitted changes.

--push sends it to EMBEDEVAL_GITLAYER_REGISTRY (default
oras://ghcr.io/deveshm7/embedeval-zephyr-gitlayer), tag = instance id, and
records it in the instance's validation.json ("gitlayer": ref, sha256, base
tree). A pull checks the file against that sha256. --delete-image removes the
instance image afterwards (evaluation pulls it again); --shard I/N takes every
Nth published Zephyr instance, for one node of a job.
"""

from __future__ import annotations

import argparse
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
import registry  # noqa: E402

REGISTRY = os.environ.get("EMBEDEVAL_GITLAYER_REGISTRY",
                          "oras://ghcr.io/deveshm7/embedeval-zephyr-gitlayer").rstrip("/")
MKSQUASHFS = shutil.which("mksquashfs") or "/usr/libexec/apptainer/bin/mksquashfs"
OPAQUE = ".wh..wh..opq"     # overlay whiteout: this folder replaces the one below

# Runs in the instance's container, /testbed on a throw-away overlay, the new
# repository's folder bound at /newgit. Prints the two trees for the check.
MAKE_GIT = r'''
set -e
B=$1; cd /testbed
OLD="git -c safe.directory=*"
BASE_TREE=$($OLD rev-parse "$B^{tree}")
$OLD apply -R /opt/embedeval/test_patch.diff
$OLD ls-tree -r --name-only -z "$B" > /tmp/base-files
export GIT_DIR=/newgit/git GIT_WORK_TREE=/testbed
N="git -c safe.directory=* -c user.name=EmbedEval -c user.email=embedeval@localhost
   -c init.defaultBranch=main -c gc.auto=0 -c gc.autoDetach=false -c maintenance.auto=false"
$N init -q
cp /testbed/.git/info/exclude $GIT_DIR/info/exclude
$N add -f --pathspec-from-file=/tmp/base-files --pathspec-file-nul
GIT_AUTHOR_DATE=2000-01-01T00:00:00Z GIT_COMMITTER_DATE=2000-01-01T00:00:00Z \
    $N commit -q -m "The starting point of this task"
$N gc -q
rm -rf $GIT_DIR/logs $GIT_DIR/hooks $GIT_DIR/COMMIT_EDITMSG
echo "base_tree=$BASE_TREE"
echo "new_tree=$($N rev-parse HEAD^{tree})"
'''

# Runs in the container with the layer stacked, as an agent would see it.
CHECK = r'''
cd /testbed; G="git -c safe.directory=*"
echo "commits=$($G rev-list --all | wc -l)"
echo "commit_objects=$($G cat-file --batch-all-objects --batch-check='%(objecttype)' | grep -c '^commit$')"
echo "refs=$($G for-each-ref --format='%(refname)' | tr '\n' ' ')"
echo "remotes=$($G remote | wc -l)"
echo "tree=$($G rev-parse HEAD^{tree})"
echo "base_commit_present=$($G cat-file -e $1^{commit} 2>/dev/null && echo yes || echo no)"
echo "promisor_packs=$(ls .git/objects/pack/ 2>/dev/null | grep -c promisor)"
echo "changed_outside_tests=$($G status --porcelain | grep -vc '^.. tests/')"
echo "changed_in_tests=$($G status --porcelain | grep -c '^.. tests/')"
'''


def layer_path(instance_id: str) -> Path:
    return au.path("images", "gitlayers", f"{instance_id}.sif")


def _kv(text: str) -> dict:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    r.stdout = "\n".join(line for line in r.stdout.splitlines() if not line.startswith("INFO:"))
    return r


def check(instance_id: str, layer: Path, base_commit: str, base_tree: str) -> dict:
    """The layer as an agent sees it; raises unless the history is gone."""
    image = registry.image_for(instance_id)
    work = au.path("runs", f"gitlayer-check-{instance_id}-{os.getpid()}", "x").parent
    (work / "overlay").mkdir(parents=True)
    try:
        cmd = au.exec_args(image, overlay=work / "overlay", layer=layer, network=False)
        r = _run(cmd + ["bash", "-c", CHECK, "check", base_commit], 600)
    finally:
        au.remove(work)
    got = _kv(r.stdout)
    want = {"commits": "1", "commit_objects": "1", "refs": "refs/heads/main ", "remotes": "0",
            "tree": base_tree, "base_commit_present": "no", "promisor_packs": "0",
            "changed_outside_tests": "0"}
    bad = {k: got.get(k) for k, v in want.items() if got.get(k) != v}
    if bad or got.get("changed_in_tests", "0") == "0":
        raise RuntimeError(f"layer check failed: {bad or 'no PR test changes'}; "
                           f"{(r.stderr or '').strip()[-200:]}")
    return got


def build(instance_id: str) -> dict:
    """Make and check the instance's git layer in the work root; return its record."""
    meta = json.loads((instances.instance_dir(instance_id) / "metadata.json").read_text())
    base = meta["base_commit"]
    image = registry.image_for(instance_id)        # pulled if published, checksum checked
    work = au.path("runs", f"gitlayer-{instance_id}-{os.getpid()}", "x").parent
    for d in ("overlay", "newgit", "layer/testbed"):
        (work / d).mkdir(parents=True)
    (work / "make_git.sh").write_text(MAKE_GIT)
    t = time.time()
    try:
        cmd = au.exec_args(image, overlay=work / "overlay", network=False,
                           binds={work / "newgit": "/newgit", work / "make_git.sh": "/make_git.sh"})
        r = _run(cmd + ["bash", "/make_git.sh", base], 3600)
        got = _kv(r.stdout)
        if r.returncode or not got.get("base_tree"):
            raise RuntimeError(f"new repository failed: {(r.stderr or r.stdout).strip()[-300:]}")
        if got["base_tree"] != got.get("new_tree"):
            raise RuntimeError(f"tree differs from the base: {got.get('new_tree')} "
                               f"!= {got['base_tree']}")
        git = work / "layer" / "testbed" / ".git"
        (work / "newgit" / "git").rename(git)
        (git / OPAQUE).touch()
        subprocess.run(["chmod", "-R", "a+rwX", str(work / "layer")], check=True)
        squash = work / "layer.squashfs"
        r = subprocess.run([MKSQUASHFS, str(work / "layer"), str(squash), "-all-root",
                            "-no-progress", "-noappend", "-processors", "8"],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"mksquashfs: {r.stderr[:300]}")
        out = layer_path(instance_id)
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_name(f".{out.name}.part-{os.getpid()}")
        # datatype 4 partition, fs squash, type overlay, arch arm64 (Spark)
        for args in (["new", str(part)],
                     ["add", "--datatype", "4", "--partfs", "1", "--parttype", "4",
                      "--partarch", "4", "--groupid", "1", str(part), str(squash)]):
            r = subprocess.run(["apptainer", "sif", *args], capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(f"apptainer sif {args[0]}: {r.stderr[:300]}")
        part.replace(out)
    finally:
        au.remove(work)
    seen = check(instance_id, out, base, got["base_tree"])
    return {"base_tree": got["base_tree"], "sha256": registry.sha256(out),
            "bytes": out.stat().st_size, "seconds": round(time.time() - t),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "pr_test_files_changed": int(seen["changed_in_tests"])}


def push(instance_id: str, rec: dict) -> dict:
    """Push the built layer and record it in validation.json."""
    out = layer_path(instance_id)
    if registry.sha256(out) != rec["sha256"]:
        raise RuntimeError(f"{out} is not the layer just checked")
    ref = f"{REGISTRY}:{instance_id}"
    t = time.time()
    r = subprocess.run(["apptainer", "push", str(out), ref], env=registry._push_env(),
                       capture_output=True, text=True, timeout=1800)
    if r.returncode:
        raise RuntimeError(f"apptainer push failed: {(r.stderr or r.stdout).strip()[-300:]}")
    rec = {**rec, "ref": ref, "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "push_seconds": round(time.time() - t)}
    report_file, report = registry._report(instance_id)
    report["gitlayer"] = rec
    tmp = report_file.with_name(f".{report_file.name}.tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n")
    tmp.replace(report_file)
    return rec


def pull(instance_id: str, timeout: int = 1800) -> Path:
    """The published git layer, in the work root, checked against its sha256."""
    rec = registry._report(instance_id)[1].get("gitlayer")
    if not rec or not rec.get("ref"):
        raise RuntimeError(f"{instance_id} has no published git layer "
                           "(scripts/zephyr_gitlayer.py build --push)")
    out = layer_path(instance_id)
    if out.exists() and registry.sha256(out) == rec["sha256"]:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(f".{out.name}.pull-{os.getpid()}")
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPTAINER_DOCKER_")}
    try:
        r = subprocess.run(["apptainer", "pull", "--force", str(part), rec["ref"]], env=env,
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError(f"apptainer pull {rec['ref']} failed: "
                               f"{(r.stderr or r.stdout).strip()[-300:]}")
        if registry.sha256(part) != rec["sha256"]:
            raise RuntimeError(f"{rec['ref']} is not the checked git layer")
        part.replace(out)
    finally:
        if part.exists():
            au.remove(part)
    return out


def published_zephyr() -> list[str]:
    return [i for i in instances.discover_instances("zephyr") if registry.published(i)]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["build", "check", "pull"])
    p.add_argument("instance", nargs="*")
    p.add_argument("--shard", help="I/N: every Nth published Zephyr instance, from the Ith")
    p.add_argument("--push", action="store_true", help="push and record each layer built")
    p.add_argument("--delete-image", action="store_true",
                   help="remove the instance image once its layer is done")
    p.add_argument("--redo", action="store_true", help="build again a layer already published")
    args = p.parse_args()
    ids = list(args.instance)
    if args.shard:
        i, n = map(int, args.shard.split("/"))
        ids += published_zephyr()[i::n]
    if not ids:
        sys.exit("no instances given")
    bad = 0
    for iid in ids:
        t = time.time()
        try:
            rec = registry._report(iid)[1].get("gitlayer")
            if args.command == "pull":
                msg = f"ready {pull(iid)}"
            elif args.command == "check":
                meta = json.loads((instances.instance_dir(iid) / "metadata.json").read_text())
                layer = layer_path(iid) if layer_path(iid).exists() else pull(iid)
                check(iid, layer, meta["base_commit"], rec["base_tree"])
                msg = "ok: one commit, base tree, no old history"
            elif rec and rec.get("ref") and not args.redo:
                msg = f"already published {rec['ref']}"
            else:
                rec = build(iid)
                msg = f"built and checked ({rec['bytes'] / 2**20:.0f} MB, {rec['seconds']} s)"
                if args.push:
                    rec = push(iid, rec)
                    msg += f", pushed {rec['ref']} ({rec['push_seconds']} s)"
            if args.delete_image and args.command == "build" and au.instance_image(iid).exists():
                au.remove(au.instance_image(iid))
                msg += "; image removed"
        except (RuntimeError, OSError, subprocess.TimeoutExpired, KeyError) as e:
            msg, bad = f"FAILED: {e}", bad + 1
        print(f"  {iid}: {msg} [{time.time() - t:.0f} s]", flush=True)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
