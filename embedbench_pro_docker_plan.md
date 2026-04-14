# EmbedBench-Pro: Docker Infrastructure & Instance Construction Plan

## Overview

EmbedBench-Pro is a benchmark for evaluating LLMs on embedded software engineering tasks (bug repair) in RTOS projects like Zephyr, FreeRTOS, etc. Each benchmark instance is a Docker image containing:

- A repo checked out to a pre-fix commit (the "broken" state)
- Test(s) cherry-picked from the fix PR (so they fail on the broken code)
- A full build toolchain + QEMU so an agent can build, run, and test changes

An external agent harness (mini-SWE-agent) runs on the host, sends bash commands into the container via `docker exec`, reads output, and iterates with an LLM.

---

## Architecture

```
Host Machine                         Docker Container
────────────                         ────────────────
mini-swe-agent (Python)              /testbed/ (Zephyr repo @ pre-fix commit)
  ↕ LLM API calls                   Zephyr SDK (cross-compilers)
  ↕ docker exec <cid> bash -c "…"   QEMU (for running tests)
                                     west, cmake, ninja
                                     ctags index
```

The agent process lives on the host. It never enters the container. Each bash command the LLM produces is executed via `docker exec <container_id> bash -c "<command>"` and stdout/stderr are returned to the agent. The LLM does not know Docker is involved — it just sees a Linux shell.

---

## Repo Structure

```
embedbench-pro/
├── docker/
│   ├── bases/
│   │   ├── zephyr.Dockerfile          # Shared base: Ubuntu + SDK + QEMU + west
│   │   └── freertos.Dockerfile        # (future) Base for FreeRTOS instances
│   └── instances/
│       └── zephyr__zephyr-65697/      # One dir per benchmark instance
│           ├── Dockerfile             # FROM embedbench-zephyr-base, adds repo + tests
│           ├── test_patch.diff        # Test-only changes from the PR
│           └── metadata.json          # Instance metadata (commits, platform, commands)
├── dataset/
│   └── embedbench_pro.json            # Full dataset: array of all instance records
├── harness/
│   ├── config/
│   │   ├── embedbench.yaml            # Mini-SWE-agent config (system + instance templates)
│   │   └── project_guides/
│   │       └── zephyr.md              # Zephyr-specific context for LLM prompt
│   ├── run_instance.py                # Run agent on one instance
│   ├── run_batch.py                   # Parallel batch runner
│   └── evaluate.py                    # Apply patch, run tests, report pass/fail
├── scripts/
│   ├── build_base_images.sh           # Build the Zephyr base image
│   ├── build_riot_base_images.sh      # Build the RIOT base image
│   ├── build_instance_images.sh       # Build Zephyr per-instance Docker images
│   ├── build_riot_instance_images.sh  # Build RIOT per-instance Docker images
│   ├── generate_instance.py           # Given a PR, generate instance dir
│   └── validate_instance.py           # Verify fail-then-pass behavior
└── paper/
```

---

## Step 1: Build the Zephyr Base Image

File: `docker/bases/zephyr.Dockerfile`

This image is built ONCE and shared by all Zephyr instances. It contains everything except the repo itself.

```dockerfile
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

# System packages — matches official Zephyr getting started guide (Ubuntu)
# NOTE: No QEMU here. The Zephyr SDK bundles its own custom QEMU build
# with Zephyr-specific board/machine definitions.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake ninja-build gperf ccache dfu-util device-tree-compiler wget \
    python3-dev python3-pip python3-venv python3-tk \
    xz-utils file make gcc gcc-multilib g++-multilib libsdl2-dev libmagic1 \
    ctags cscope \
    && rm -rf /var/lib/apt/lists/*

# Create a Python virtual environment (matches official docs)
RUN python3 -m venv /opt/zephyr-venv
ENV PATH="/opt/zephyr-venv/bin:$PATH"

# Install west inside the venv
RUN pip install west

# We install the Zephyr SDK in the per-instance stage (after the repo is cloned)
# because `west sdk install` needs to run inside a west workspace.
# However, we CAN pre-download the SDK here to cache it.
# The per-instance Dockerfile will run `west sdk install` after `west init`.
```

Build command:
```bash
docker build -f docker/bases/zephyr.Dockerfile -t embedbench-zephyr-base:latest docker/bases/
```

### What each section does:

- `FROM ubuntu:24.04` — Start from bare Ubuntu (matching Zephyr's official docs which target 24.04+)
- `apt-get install` — Install C build tools (cmake, ninja, gcc, device-tree-compiler) and code navigation tools (ctags, cscope) that the agent can use via bash. **No QEMU** — the Zephyr SDK bundles its own custom QEMU build with Zephyr-specific board definitions
- `python3 -m venv` + `pip install west` — Creates a virtual environment and installs west, matching the official Zephyr getting started guide
- The SDK is installed per-instance (via `west sdk install`) because it needs a west workspace to exist first. This means the SDK layer lives in the per-instance image, but Docker caching still helps since the `west sdk install` command is deterministic for a given SDK version

---

## Step 2: Build Per-Instance Images

File: `docker/instances/zephyr__zephyr-65697/Dockerfile`

Each instance adds: specific repo commit, west modules, test patch, pre-built CMake cache.

```dockerfile
FROM embedbench-zephyr-base:latest

WORKDIR /testbed

# Clone repo and checkout the pre-fix commit
# For PR #65697, the fix is commit 330c820. We want its parent (the broken state).
ARG BASE_COMMIT
RUN git clone --depth=500 https://github.com/zephyrproject-rtos/zephyr.git . \
    && git checkout ${BASE_COMMIT}

# Initialize west workspace, fetch external modules, and export CMake package
# --narrow and --depth=1 minimize download size for modules
RUN west init -l . \
    && west update --narrow -o=--depth=1 \
    && west zephyr-export

# Install Zephyr SDK (includes cross-compilers AND custom QEMU)
# This uses west's built-in SDK installer which downloads the right version
# and sets up toolchains + host tools (QEMU, OpenOCD, etc.)
RUN west sdk install

# Install Python deps for this Zephyr version (uses west packages, per official docs)
RUN west packages pip --install

# Apply the test-only patch (adds failing tests, NOT the fix)
COPY test_patch.diff /tmp/
RUN git apply /tmp/test_patch.diff

# Pre-build to populate CMake cache (makes agent's incremental builds fast)
# The || true prevents Docker build failure if tests fail (they will — that's the point)
ARG PLATFORM=qemu_x86
ARG TEST_PATH=tests/posix/common
RUN west build -b ${PLATFORM} ${TEST_PATH} || true

# Index codebase for agent's code navigation
RUN ctags -R --languages=C,C++ --exclude=build .
```

Build command:
```bash
cd docker/instances/zephyr__zephyr-65697/
docker build \
    --build-arg BASE_COMMIT=<parent-of-330c820> \
    --build-arg PLATFORM=qemu_x86 \
    --build-arg TEST_PATH=tests/posix/common \
    -t embedbench:zephyr-65697 .
```

### What each section does:

- `FROM embedbench-zephyr-base:latest` — Starts from the pre-built base (system deps, venv, west already installed)
- `git clone + checkout` — Gets the Zephyr source at the exact broken commit. `--depth=500` limits git history to keep the clone manageable while still having enough history for `git log` to be useful to the agent
- `west init -l . && west update` — Fetches all external modules that Zephyr depends on. This is the slow step (~5-15 min). The `-l .` flag says "this is already a Zephyr repo, don't clone it again"
- `west zephyr-export` — Registers the Zephyr CMake package so `west build` can find Zephyr's build system. This is required by the official docs
- `west sdk install` — Downloads and installs the Zephyr SDK, which includes cross-compilers for all architectures AND custom QEMU builds with Zephyr-specific board/machine definitions. This replaces the manual SDK download from the old plan. The SDK is ~1-2GB
- `west packages pip --install` — Installs Zephyr's Python dependencies (devicetree processing scripts, etc.) using the official method instead of `pip install -r scripts/requirements.txt`
- `COPY test_patch.diff` — Copies the pre-generated test diff from the host into the image. This file must exist next to the Dockerfile
- `git apply` — Applies the test changes. Now the repo has the NEW tests but the OLD (buggy) source code
- `west build ... || true` — Pre-compiles the test. This populates CMake's cache so that when the agent later changes one .c file, only that file needs recompiling (seconds instead of minutes). `|| true` prevents the Docker build from failing if compilation or tests fail
- `ctags -R` — Generates a tags file mapping every C function/variable to file:line. Agent can grep this for instant code navigation

---

## Step 3: Generate test_patch.diff

File: `scripts/generate_instance.py`

For PR #65697, this script would:

1. Identify the two commits:
   - Fix commit: `330c820` (changes `lib/posix/key.c`)
   - Test commit: `ba72388` (changes `tests/posix/common/src/key.c`)

2. Extract ONLY the test changes:
```bash
# Clone the repo (or use existing clone)
git clone https://github.com/zephyrproject-rtos/zephyr.git /tmp/zephyr
cd /tmp/zephyr

# The base commit is the parent of the fix
BASE_COMMIT=$(git rev-parse 330c820~1)

# Generate test-only diff: changes in tests/ between base and the test commit
git diff ${BASE_COMMIT}..ba72388 -- tests/ > test_patch.diff
```

3. Write metadata.json:
```json
{
    "instance_id": "zephyr__zephyr-65697",
    "project": "zephyr",
    "repo": "https://github.com/zephyrproject-rtos/zephyr",
    "base_commit": "<SHA of 330c820~1>",
    "fix_commit": "330c820b9dad6a417431b98a20c97baa32d7dc43",
    "test_commit": "ba723889f47a5685b564052ed7ab7754f4e80fc6",
    "problem_statement": "pthread_key_delete() always calls sys_bitarray_free() with offset 0 instead of the actual bit offset of the key being deleted. This causes a resource leak where creating and deleting pthread keys in sequence eventually exhausts the key pool, even though keys were properly deleted.",
    "platform": "qemu_x86",
    "test_path": "tests/posix/common",
    "test_scenario": "portability.posix.common",
    "build_command": "west build -b qemu_x86 tests/posix/common",
    "run_command": "west build -t run",
    "docker_image": "embedbench:zephyr-65697",
    "fail_to_pass": [
        "test_key_resource_leak",
        "test_correct_key_is_deleted"
    ],
    "pass_to_pass": [
        "test_key_1to1_thread",
        "test_key_Nto1_thread"
    ],
    "files_changed_by_fix": [
        "lib/posix/key.c"
    ]
}
```

4. Place both files in `docker/instances/zephyr__zephyr-65697/`

---

## Step 4: Validate the Instance

File: `scripts/validate_instance.py`

Before the instance is usable, verify the fail-then-pass behavior:

```bash
# 1. Build the instance image
docker build -t embedbench:zephyr-65697 docker/instances/zephyr__zephyr-65697/

# 2. Start a container
CID=$(docker run -d embedbench:zephyr-65697 sleep infinity)

# 3. Verify tests FAIL on the broken code
docker exec $CID bash -c "cd /testbed && west build -b qemu_x86 tests/posix/common && west build -t run"
# Expected: test_key_resource_leak and test_correct_key_is_deleted FAIL

# 4. Apply the fix patch and verify tests PASS
docker exec $CID bash -c "cd /testbed && git apply /tmp/fix_patch.diff"
docker exec $CID bash -c "cd /testbed && west build -b qemu_x86 tests/posix/common && west build -t run"
# Expected: ALL tests pass

# 5. Cleanup
docker stop $CID && docker rm $CID
```

If both checks pass, the instance is valid.

---

## Step 5: Mini-SWE-Agent Configuration

File: `harness/config/embedbench.yaml`

```yaml
agent:
  system_template: |
    You are an expert embedded systems engineer. You can interact with a
    Linux shell to navigate codebases, edit source files, build firmware,
    and run tests. You are working inside an RTOS repository.

    Your response must contain exactly ONE bash code block with ONE command
    (or commands connected with && or ||). Include a THOUGHT section before
    your command explaining your reasoning.

    <format_example>
    THOUGHT: Your reasoning here
    ```bash
    your_command_here
    ```
    </format_example>

  instance_template: |
    <issue_description>
    {{problem_statement}}
    </issue_description>

    <project_context>
    Project: {{project}}
    {{project_guide}}
    </project_context>

    <test_info>
    The following tests are currently FAILING and should pass after your fix:
    {{fail_to_pass}}

    Build command: {{build_command}}
    Run tests: {{run_command}}
    Test directory: {{test_path}}
    Target platform: {{platform}}
    </test_info>

    <instructions>
    Fix the source code so the failing tests pass. Do NOT modify test files.
    The repository is at /testbed.

    Recommended workflow:
    1. Read the failing test code to understand what behavior is expected
    2. Use grep/ctags to find the relevant source code
    3. Understand the bug
    4. Edit the source code to fix it
    5. Rebuild and run tests to verify
    6. Submit: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
    </instructions>

  # Budget
  step_limit: 100
  cost_limit: 5.00
  timeout: 180           # seconds per command (builds can be slow)
```

File: `harness/config/project_guides/zephyr.md` (injected as `{{project_guide}}`)

```markdown
## Zephyr RTOS Developer Guide

**Build system**: Zephyr uses `west` + CMake + Ninja.
- Build: `west build -b <board> <test_path>` (e.g., `west build -b qemu_x86 tests/posix/common`)
- Run: `west build -t run` (runs on QEMU, must build first)
- Clean rebuild: `rm -rf build && west build -b <board> <test_path>`
- Incremental rebuild after editing a .c file: just run `west build` again (fast)

**Directory structure**:
- `kernel/` — Core kernel (scheduler, threads, sync primitives)
- `lib/posix/` — POSIX API implementation
- `subsys/` — Subsystems (networking, Bluetooth, logging, etc.)
- `drivers/` — Device drivers
- `arch/` — Architecture-specific code (ARM, x86, RISC-V)
- `boards/` — Board definitions
- `tests/` — Test suites (each has testcase.yaml + src/)
- `include/` — Public headers

**Test framework**: Zephyr uses `ztest`. Tests are defined with `ZTEST(suite, test_name)`.
Test assertions: `zassert_ok()`, `zassert_equal()`, `zassert_true()`, etc.

**Configuration**: Zephyr uses Kconfig. Project config is in `prj.conf` files.
Board/platform selection is via `-b <board>` flag to `west build`.

**Code navigation shortcuts**:
- `grep -rn "function_name" --include="*.c" --include="*.h" .`
- `grep "function_name" tags` (ctags index is pre-built at /testbed/tags)

**Common QEMU targets**: qemu_x86, qemu_cortex_m3, native_sim
```

---

## Step 6: Agent Execution Flow

File: `harness/run_instance.py`

Pseudocode:
```python
def run_instance(instance_id, model_name, output_dir):
    # 1. Load instance metadata
    meta = load_json(f"docker/instances/{instance_id}/metadata.json")

    # 2. Start Docker container from pre-built image
    cid = docker_run(meta["docker_image"])

    # 3. Load project guide
    project_guide = read_file(f"harness/config/project_guides/{meta['project']}.md")

    # 4. Configure mini-SWE-agent
    agent = DefaultAgent(
        model=LitellmModel(model_name=model_name),
        environment=DockerEnvironment(container_id=cid),
        config="harness/config/embedbench.yaml"
    )

    # 5. Run agent with instance-specific template variables
    result = agent.run(
        task=meta["problem_statement"],
        project=meta["project"],
        project_guide=project_guide,
        platform=meta["platform"],
        test_path=meta["test_path"],
        build_command=meta["build_command"],
        run_command=meta["run_command"],
        fail_to_pass=", ".join(meta["fail_to_pass"]),
    )

    # 6. Capture patch
    patch = docker_exec(cid, "cd /testbed && git diff")
    save(f"{output_dir}/{instance_id}.patch", patch)

    # 7. Cleanup
    docker_stop(cid)
```

---

## Step 7: Evaluation

File: `harness/evaluate.py`

For each instance:
1. Start a FRESH container from the same image (clean state)
2. Apply the agent's patch: `git apply agent_patch.diff`
3. Build: `west build -b <platform> <test_path>`
4. Run: `west build -t run`
5. Parse test output for pass/fail status of each test
6. Score:
   - **fail_to_pass**: Did all previously-failing tests now pass?
   - **pass_to_pass**: Did all previously-passing tests stay passing?
   - **resolved**: Both conditions met = instance resolved

---

## Key Decisions & Notes

1. **SDK includes QEMU**: The Zephyr SDK bundles custom QEMU builds with Zephyr-specific board/machine definitions. Do NOT install QEMU separately via apt — use the SDK's version. The `west sdk install` command handles downloading and setting up both the cross-compilers and QEMU.

2. **SDK version**: `west sdk install` automatically picks the right SDK version for the Zephyr commit being built. Different Zephyr versions may pull different SDK versions. This is handled automatically — no need to pin a version manually.

3. **Python virtual environment**: The official Zephyr docs use `python3 -m venv`. Our base image creates a venv at `/opt/zephyr-venv` and adds it to PATH so all subsequent `pip install` and `west` commands run inside it.

4. **west packages pip --install**: This is the official method for installing Zephyr's Python dependencies (replaces the older `pip install -r scripts/requirements.txt`). It may downgrade or upgrade west itself, which is fine.

5. **west zephyr-export**: Required step after `west init` + `west update`. Registers the Zephyr CMake package so that `west build` can locate Zephyr's build system files. Without this, builds will fail with CMake errors.

6. **west update is slow**: This is the bottleneck (~5-15 min per instance). Consider caching the west modules in a Docker volume or intermediate layer if building many instances from nearby commits.

7. **Build timeout**: `west build` on a cold cache takes 60-120 seconds. After pre-building in the Dockerfile, incremental rebuilds after a single file change take 5-15 seconds. Set the agent's per-command timeout to 180 seconds to be safe.

8. **Platform selection**: From the testcase.yaml, pick the simplest QEMU target that the test supports. Usually `qemu_x86` for POSIX tests, `qemu_cortex_m3` for ARM kernel tests. The `integration_platforms` field in testcase.yaml lists the preferred test platforms.

9. **Image size**: Base image ~2GB (system packages + venv + west), per-instance layer ~4-6GB (repo + west modules + SDK + pre-built cache). Docker layer sharing means all Zephyr instances share the base layer.

10. **Parallelism**: Each instance runs in its own container. You can run N agents in parallel limited only by CPU cores and API rate limits. Mini-SWE-agent supports parallel batch runs via workers.
