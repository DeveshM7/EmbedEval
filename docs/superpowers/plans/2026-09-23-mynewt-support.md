# Apache Mynewt Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Apache Mynewt as a fourth project in the current EmbedEval interface with the three previously validated repair tasks.

**Architecture:** Keep the current EmbedEval build, validation, agent, evaluation, naming, and output flows as the source of truth. Add Mynewt through the existing per-project configuration boundaries, port the proven native-simulator runner and task data, and gate the one structured-result validation hook behind metadata so existing projects follow their current paths unchanged.

**Tech Stack:** Python 3.11+, standard-library `unittest`, Docker, Apache Mynewt Newt, GCC multilib native simulator, mini-swe-agent.

**Spec:** `docs/superpowers/specs/2026-09-23-mynewt-support-design.md`

## Global Constraints

- Branch from `origin/main` as `feature/mynewt-support`.
- Preserve existing Zephyr, NuttX, and RIOT behavior and instance files.
- Use instance IDs `mynewt__mynewt-2809`, `mynewt__mynewt-3299`, and `mynewt__mynewt-3680`.
- Preserve the old Mynewt runner, setup script, metadata semantics, and test patches unless the current EmbedEval interface requires a naming or path change.
- Use Mynewt's `@apache-mynewt-core/hw/bsp/native` BSP and `@apache-mynewt-core/compiler/sim` compiler.
- Keep `linux/amd64` for Mynewt Docker builds and runs.
- Do not add generated model outputs, generated patches, trajectories, validation logs, API keys, or credentials.
- Do not select or require a paid model during implementation; a live #3680 run is conditional on an available key.
- Do not import deleted Zephyr files, old duplicated harness entry points, or unrelated `.EmbedEval-old` working-tree changes.

---

### Task 1: Lock the Mynewt project contract with unit tests

**Files:**
- Create: `tests/test_mynewt_integration.py`
- Create: `tests/test_mynewt_results.py`

**Interfaces:**
- Consumes: current `scripts/instances.py`, `scripts/build_config.py`, `harness/paths.py`, `harness/projects.py`.
- Produces: regression tests for Mynewt discovery/configuration and `mynewt_runner.classify(output, returncode, expected) -> tuple[int, dict]`.

- [ ] **Step 1: Create the runner result tests from the proven implementation**

Copy `../.EmbedEval-old/tests/test_mynewt_results.py` to `tests/test_mynewt_results.py` unchanged. These cases require missing tests, process crashes, repeated assertions for one testcase, complete success, and failure text with exit zero to be classified correctly.

- [ ] **Step 2: Add failing configuration tests**

Create `tests/test_mynewt_integration.py` using `unittest` and `importlib.util.spec_from_file_location` so the independently named `scripts` and `harness` modules can be loaded without import-name collisions. Assert:

```python
self.assertEqual(scripts_instances.split_instance("mynewt__mynewt-3680"), ("mynewt", "3680"))
self.assertEqual(harness_paths.split_instance("mynewt__mynewt-3680"), ("mynewt", "3680"))
self.assertIn("mynewt", scripts_instances.PROJECTS)
self.assertIn("mynewt", harness_paths.PROJECTS)
self.assertEqual(build_config.config("mynewt")["platform"], "linux/amd64")
self.assertEqual(projects.config("mynewt")["docker_platform"], "linux/amd64")
self.assertFalse(projects.config("mynewt")["needs_qemu_cleanup"])
```

Also snapshot existing project tuples before implementation:

```python
self.assertEqual(scripts_instances.PROJECTS[:3], ("zephyr", "nuttx", "riot"))
self.assertEqual(harness_paths.PROJECTS[:3], ("zephyr", "nuttx", "riot"))
```

- [ ] **Step 3: Run the tests and confirm the expected failures**

Run:

```bash
python -m unittest discover -s tests -v
```

Expected: configuration tests fail because `mynewt` is unknown, and the result tests fail to import because `docker/shared/mynewt_runner.py` has not been ported.

- [ ] **Step 4: Commit the failing contract tests**

```bash
git add tests/test_mynewt_integration.py tests/test_mynewt_results.py
git commit -m "Test Apache Mynewt project integration"
```

### Task 2: Port the proven Mynewt native runtime

**Files:**
- Create: `docker/shared/mynewt_runner.py`
- Create: `docker/shared/setup_mynewt.py`
- Create: `docker/bases/mynewt.Dockerfile`
- Modify: `scripts/build_config.py`
- Modify: `scripts/build_bases.py`
- Modify: `scripts/instances.py`

**Interfaces:**
- Consumes: instance metadata at `/opt/benchmark/metadata.json`.
- Produces: `/usr/local/bin/mynewt_run_tests`, `/opt/benchmark/setup.py`, `/tmp/mynewt-result.json`, and Docker image `embedeval-mynewt-base:latest`.

- [ ] **Step 1: Copy the proven Python runtime files byte for byte**

```bash
cp ../.EmbedEval-old/docker/shared/mynewt_runner.py docker/shared/mynewt_runner.py
cp ../.EmbedEval-old/docker/shared/setup_mynewt.py docker/shared/setup_mynewt.py
```

Verify the source and destination hashes match:

```bash
shasum -a 256 ../.EmbedEval-old/docker/shared/mynewt_runner.py docker/shared/mynewt_runner.py
shasum -a 256 ../.EmbedEval-old/docker/shared/setup_mynewt.py docker/shared/setup_mynewt.py
```

Expected pairs:

```text
mynewt_runner.py  736079cd25bf3cfbbefb58ed9e4e0b6460b23c959bd80e1202de26edba5533aa
setup_mynewt.py   641e9a1d8d6f07081d3b281e4202293508570c5ab6c56771b0e4f8dca1d681c3
```

- [ ] **Step 2: Run the runner result tests**

```bash
python -m unittest tests.test_mynewt_results -v
```

Expected: five tests pass.

- [ ] **Step 3: Add the Mynewt base Dockerfile**

Create `docker/bases/mynewt.Dockerfile` from the proven Dockerfile with these current-repository adaptations:

```dockerfile
FROM ubuntu:24.04@sha256:224a1869083a311ef3f13648a154ba79832fbef6364d31493642ca03082da254
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git golang-go gcc-multilib g++-multilib make file python3 && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/apache/mynewt-newt.git /opt/newt-src && cd /opt/newt-src && git checkout c36db0f3df0a2787dfa19914d79a687d6a6cb53a && go build -o /usr/local/bin/newt ./newt
COPY docker/shared/mynewt_runner.py /usr/local/bin/mynewt_run_tests
COPY docker/shared/setup_mynewt.py /opt/benchmark/setup.py
RUN chmod +x /usr/local/bin/mynewt_run_tests
WORKDIR /testbed
```

- [ ] **Step 4: Register the Mynewt base build without changing existing contexts**

Add a `mynewt` entry to `scripts/build_config.py`:

```python
"mynewt": {
    "base_image": "embedeval-mynewt-base:latest",
    "base_dockerfile": "docker/bases/mynewt.Dockerfile",
    "base_context": ".",
    "build_args": {"BASE_COMMIT": lambda m: m["base_commit"]},
    "platform": "linux/amd64",
    "clean_paths": [],
},
```

Update `scripts/build_bases.py` so `base_context` defaults to `docker/bases` and only Mynewt selects repository root:

```python
context = instances.REPO_ROOT / cfg.get("base_context", "docker/bases")
...
cmd.append(str(context))
```

Append `"mynewt"` to `PROJECTS` in `scripts/instances.py`, keeping the existing three entries in their current order. This makes `--repo mynewt` available to the base-build command.

- [ ] **Step 5: Verify build command generation**

```bash
python scripts/build_bases.py --repo mynewt --dry-run
python scripts/build_bases.py --repo zephyr --dry-run
```

Expected: Mynewt uses `--platform linux/amd64`, the new Dockerfile, and repository root context; Zephyr prints the same command and `docker/bases` context it printed before the change.

- [ ] **Step 6: Commit the runtime and base build support**

```bash
git add docker/shared/mynewt_runner.py docker/shared/setup_mynewt.py docker/bases/mynewt.Dockerfile scripts/build_config.py scripts/build_bases.py scripts/instances.py
git commit -m "Add Apache Mynewt native test environment"
```

### Task 3: Register Mynewt in the unified agent configuration

**Files:**
- Modify: `harness/paths.py`
- Modify: `harness/projects.py`
- Test: `tests/test_mynewt_integration.py`

**Interfaces:**
- Consumes: instance IDs matching `mynewt__mynewt-<PR>` and metadata field `project: "mynewt"`.
- Produces: `--repo mynewt` selection in scripts and harness commands, plus Mynewt prompt and environment configuration.

- [ ] **Step 1: Add Mynewt to project discovery**

Append `"mynewt"` to `PROJECTS` in `harness/paths.py`. Keep the existing three entries in their current order. The scripts-side project list was extended in Task 2 so the Mynewt base-build command could already run there.

- [ ] **Step 2: Add the Mynewt agent configuration**

Add this orientation in `harness/projects.py`:

```python
"mynewt": (
    "The Apache Mynewt source tree is at /testbed. Tests use Newt with the "
    "native BSP and simulator compiler through an external project at /project. "
    "Use `run_tests` to perform a clean native build and execute the complete "
    "pinned selftest suite; do not construct a separate newt target."
),
```

Add this `PROJECTS` entry:

```python
"mynewt": {
    "label": "Apache Mynewt",
    "orientation": _ORIENTATION["mynewt"],
    "extra_warnings": "",
    "protected_paths": [
        "fs/fcb2/selftest/",
        "kernel/os/selftest/",
        "encoding/json/selftest/",
    ],
    "needs_qemu_cleanup": False,
    "clean_paths": [],
    "docker_platform": "linux/amd64",
    "build_command": "true",
    "rebuild_command": "",
    "run_command": "run_tests",
},
```

- [ ] **Step 3: Run configuration tests**

```bash
python -m unittest tests.test_mynewt_integration -v
```

Expected: project parsing and project configuration assertions pass. Runner tests continue passing.

- [ ] **Step 4: Commit unified project registration**

```bash
git add harness/paths.py harness/projects.py tests/test_mynewt_integration.py
git commit -m "Register Apache Mynewt with unified commands"
```

### Task 4: Port the three validated task definitions

**Files:**
- Create: `docker/instances/mynewt__mynewt-2809/{Dockerfile,metadata.json,run_tests.sh,test_patch.diff}`
- Create: `docker/instances/mynewt__mynewt-3299/{Dockerfile,metadata.json,run_tests.sh,test_patch.diff}`
- Create: `docker/instances/mynewt__mynewt-3680/{Dockerfile,metadata.json,run_tests.sh,test_patch.diff}`

**Interfaces:**
- Consumes: `embedeval-mynewt-base:latest`, `BASE_COMMIT`, `/opt/benchmark/setup.py`, and `/usr/local/bin/mynewt_run_tests`.
- Produces: images `embedeval:mynewt-2809`, `embedeval:mynewt-3299`, and `embedeval:mynewt-3680`, each containing base code plus the exact PR test patch.

- [ ] **Step 1: Copy all three test patches unchanged**

```bash
mkdir -p docker/instances/mynewt__mynewt-{2809,3299,3680}
cp ../.EmbedEval-old/docker/instances/apache__mynewt-core-2809/test_patch.diff docker/instances/mynewt__mynewt-2809/test_patch.diff
cp ../.EmbedEval-old/docker/instances/apache__mynewt-core-3299/test_patch.diff docker/instances/mynewt__mynewt-3299/test_patch.diff
cp ../.EmbedEval-old/docker/instances/apache__mynewt-core-3680/test_patch.diff docker/instances/mynewt__mynewt-3680/test_patch.diff
```

Verify source and destination hashes match the recorded values:

```text
#2809  348411aac23fe9893d4cf06766917fd507baa9184b5feaa607198d0cff7149fa
#3299  9154f26875c760d4668ab8208e55a10713bc68be97c8db4c12e5a545f7cf3908
#3680  cef6eb91ecfead6df37fcb82ea777bc3050ea412d39fbd2c16fe8f94efd3ca5d
```

- [ ] **Step 2: Create the common instance Dockerfile in each directory**

Use this content for all three instances:

```dockerfile
FROM embedeval-mynewt-base:latest
ARG BASE_COMMIT
RUN git init /testbed \
    && cd /testbed \
    && git remote add origin https://github.com/apache/mynewt-core.git \
    && git fetch --depth 1 origin ${BASE_COMMIT} \
    && git checkout --detach FETCH_HEAD
COPY metadata.json test_patch.diff /opt/benchmark/
RUN cd /testbed \
    && git apply /opt/benchmark/test_patch.diff \
    && git add -A \
    && git -c user.name=EmbedEval -c user.email=benchmark@localhost commit -m "Benchmark starting snapshot with regression tests" \
    && python3 /opt/benchmark/setup.py
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
WORKDIR /testbed
```

- [ ] **Step 3: Create the common run_tests wrapper in each directory**

```bash
#!/usr/bin/env bash
set -euo pipefail
exec /usr/local/bin/mynewt_run_tests "$@"
```

- [ ] **Step 4: Port and rename metadata**

Copy each old `metadata.json`, then change only these integration fields:

```text
instance_id: apache__mynewt-core-<PR> -> mynewt__mynewt-<PR>
docker_image: embedbench:mynewt-<PR> -> embedeval:mynewt-<PR>
build_command: run_tests -> true
```

Keep `project: "mynewt"`, URLs, commits, problem statements, test lists, source-file lists, `linux/amd64`, test paths, compatibility flags, and scope notes unchanged.

- [ ] **Step 5: Add instance integrity assertions**

Extend `tests/test_mynewt_integration.py` to load all three metadata files and assert:

```python
self.assertEqual(meta["project"], "mynewt")
self.assertEqual(meta["instance_id"], directory.name)
self.assertEqual(meta["docker_platform"], "linux/amd64")
self.assertTrue(meta["fail_to_pass"])
self.assertTrue(meta["pass_to_pass"])
self.assertTrue(meta["files_changed_by_fix"])
self.assertEqual(meta["build_command"], "true")
```

Assert #2809 contains `-Wno-error=stringop-overflow`, and assert #3680's problem statement contains `JSON_ATTR_MAX` and `JSON_ERR_STRLONG`.

- [ ] **Step 6: Run discovery and dry-run build checks**

```bash
python -m unittest discover -s tests -v
python scripts/build_instances.py --repo mynewt --dry-run
python harness/run.py --repo mynewt --model gpt-5.4 --dry-run
```

Expected: exactly three Mynewt instances are selected in numeric order, three instance Docker build commands use `linux/amd64`, and three agent runs resolve under `outputs/mynewt/gpt-5.4/<PR>/` without requiring an API key.

- [ ] **Step 7: Commit the task definitions**

```bash
git add docker/instances/mynewt__mynewt-* tests/test_mynewt_integration.py
git commit -m "Add three Apache Mynewt benchmark tasks"
```

### Task 5: Preserve exact Mynewt testcase verification in the unified validator

**Files:**
- Modify: `scripts/validate_instance.py`
- Modify: `docker/instances/mynewt__mynewt-2809/metadata.json`
- Modify: `docker/instances/mynewt__mynewt-3299/metadata.json`
- Modify: `docker/instances/mynewt__mynewt-3680/metadata.json`
- Test: `tests/test_mynewt_integration.py`

**Interfaces:**
- Consumes: metadata key `structured_test_results: "/tmp/mynewt-result.json"` and the runner JSON object `{build_ok, passed, failed, missing, unexpected, error, exit_code}`.
- Produces: `verify_structured_results(meta: dict, report: dict, phase: str) -> tuple[bool, str]` and validation failure when the before/after named testcase inventory is wrong.

- [ ] **Step 1: Write failing unit tests for exact before and after inventories**

Add tests that import `scripts/validate_instance.py` and call `verify_structured_results` with:

```python
meta = {
    "fail_to_pass": ["suite/regression"],
    "pass_to_pass": ["suite/existing"],
}
```

Required cases:

```text
before: existing passes and regression fails -> valid
before: build_ok false -> invalid
before: wrong testcase fails -> invalid
before: missing testcase -> invalid
after: both tests pass -> valid
after: regression remains failed -> invalid
after: unexpected testcase -> invalid
```

- [ ] **Step 2: Run the focused tests and confirm failure**

```bash
python -m unittest tests.test_mynewt_integration -v
```

Expected: failure because `verify_structured_results` does not exist.

- [ ] **Step 3: Implement the pure verification helper**

Add this behavior to `scripts/validate_instance.py`:

```python
def verify_structured_results(meta: dict, report: dict, phase: str) -> tuple[bool, str]:
    expected_fail = set(meta["fail_to_pass"]) if phase == "before" else set()
    expected_pass = set(meta["pass_to_pass"])
    if phase == "after":
        expected_pass |= set(meta["fail_to_pass"])
    actual_pass = set(report.get("passed", []))
    actual_fail = set(report.get("failed", []))
    valid = (
        report.get("build_ok") is True
        and actual_pass == expected_pass
        and actual_fail == expected_fail
        and not report.get("missing")
        and not report.get("unexpected")
        and not report.get("error")
    )
    detail = (
        f"expected pass={sorted(expected_pass)}, fail={sorted(expected_fail)}; "
        f"got pass={sorted(actual_pass)}, fail={sorted(actual_fail)}, "
        f"missing={report.get('missing', [])}, unexpected={report.get('unexpected', [])}, "
        f"error={report.get('error')}"
    )
    return valid, detail
```

- [ ] **Step 4: Read structured results only when metadata requests them**

After each `run_tests` call in `validate`, check `meta.get("structured_test_results")`. If present, capture that JSON from the container with a separate quiet `docker exec cat`, parse it, and call `verify_structured_results` for `before` or `after`. Return validation failure with the helper's detail when it does not match. If the metadata key is absent, preserve the existing return-code-only logic exactly.

- [ ] **Step 5: Enable the hook for all three Mynewt tasks**

Add to each Mynewt metadata file:

```json
"structured_test_results": "/tmp/mynewt-result.json"
```

- [ ] **Step 6: Run unit and existing dry-run regression checks**

```bash
python -m unittest discover -s tests -v
python scripts/build_bases.py --repo zephyr --dry-run
python scripts/build_instances.py --repo riot --dry-run
python harness/run.py --instance nuttx__nuttx-11889 --model gpt-5.4 --dry-run
python harness/evaluate_patches.py --instance zephyr__zephyr-65697 --model gpt-5.4 --dry-run
```

Expected: all unit tests pass; existing project commands select the same instances, images, platforms, and output paths as before.

- [ ] **Step 7: Commit structured Mynewt validation**

```bash
git add scripts/validate_instance.py docker/instances/mynewt__mynewt-*/metadata.json tests/test_mynewt_integration.py
git commit -m "Validate exact Apache Mynewt testcase results"
```

### Task 6: Document Mynewt as the fourth supported project

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: unified commands and the three new instance IDs.
- Produces: user-facing setup and execution instructions consistent with the implemented CLI.

- [ ] **Step 1: Update benchmark scope and counts**

Change the overview from three RTOSes and 17 instances to four projects and 20 instances:

```text
Zephyr, NuttX, RIOT, or Apache Mynewt
20 instances: 8 Zephyr, 6 NuttX, 3 RIOT, 3 Apache Mynewt
```

- [ ] **Step 2: Add Mynewt commands**

Document:

```bash
python scripts/build_bases.py --repo mynewt
python scripts/build_instances.py --repo mynewt
python scripts/validate_instance.py --repo mynewt --verbose
python harness/run.py --instance mynewt__mynewt-3680 --model <configured-model> -v
python harness/evaluate_patches.py --instance mynewt__mynewt-3680 --model <configured-model>
```

Explain that Mynewt uses a host-native simulator through `hw/bsp/native` and `compiler/sim`; it does not use QEMU.

- [ ] **Step 3: Run documentation consistency checks**

```bash
rg -n "17 instances|Zephyr, NuttX, or RIOT|three projects" README.md
python scripts/build_instances.py --repo mynewt --dry-run
```

Expected: no stale three-project/count wording, and documented commands are accepted.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md
git commit -m "Document Apache Mynewt benchmark support"
```

### Task 7: Build and validate all three Mynewt tasks

**Files:**
- Verify only; do not add generated logs or outputs.

**Interfaces:**
- Consumes: Mynewt base image and all three task definitions.
- Produces: local Docker images and terminal evidence that all instances fail before the fix with the exact expected tests and pass after the fix.

- [ ] **Step 1: Build the Mynewt base image**

```bash
python scripts/build_bases.py --repo mynewt
```

Expected: `embedeval-mynewt-base:latest` builds successfully for `linux/amd64`.

- [ ] **Step 2: Build all three instance images**

```bash
python scripts/build_instances.py --repo mynewt
```

Expected: 3/3 images build successfully.

- [ ] **Step 3: Validate all three instances**

```bash
python scripts/validate_instance.py --repo mynewt --verbose
```

Expected:

```text
mynewt__mynewt-2809: before regression fails, after 12/12 pass
mynewt__mynewt-3299: before three regressions fail, after 33/33 pass
mynewt__mynewt-3680: before regression fails, after 3/3 pass
SUMMARY: 3/3 valid
```

- [ ] **Step 4: Verify the working tree contains no generated artifacts**

```bash
git status --short
git diff --check
```

Expected: no outputs, patches, trajectories, validation logs, Python caches, or unrelated files appear.

### Task 8: Final regression verification and optional model demonstration

**Files:**
- Verify only. Model artifacts remain untracked and uncommitted.

**Interfaces:**
- Consumes: completed Mynewt integration and any already configured model credential.
- Produces: final regression evidence and, only when credentials exist, one locally evaluated #3680 agent patch.

- [ ] **Step 1: Run all unit tests and syntax checks**

```bash
python -m unittest discover -s tests -v
python -m py_compile scripts/*.py harness/*.py docker/shared/*.py
git diff --check origin/main...HEAD
```

Expected: all tests and compilation checks pass with no whitespace errors.

- [ ] **Step 2: Verify every project remains discoverable**

```bash
python scripts/build_instances.py --all --dry-run
python harness/run.py --all --model gpt-5.4 --dry-run
```

Expected: 20 instances total: 8 Zephyr, 6 NuttX, 3 RIOT, and 3 Mynewt.

- [ ] **Step 3: Inspect the final branch diff**

```bash
git diff --stat origin/main...HEAD
git diff --name-status origin/main...HEAD
git status --short --branch
```

Expected: only the design/plan, additive Mynewt files, required project registrations, the metadata-gated validation hook, tests, and README changes.

- [ ] **Step 4: Run one model only if a supported provider key is configured**

First check key presence without printing values. If no key for a model listed by `harness/paths.py` is present, record the live run as skipped. Otherwise run:

```bash
python harness/run.py --instance mynewt__mynewt-3680 --model <configured-model> -v
python harness/evaluate_patches.py --instance mynewt__mynewt-3680 --model <configured-model>
```

Expected: the run produces a non-empty local patch and the fresh-container evaluator reports pass. Regardless of outcome, leave generated files uncommitted.
