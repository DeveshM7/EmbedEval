# EmbedEval

A benchmark for evaluating LLM coding agents on **embedded RTOS bug fixes**.

Each task is a real merged pull request from Zephyr, NuttX, RIOT, or Apache
Mynewt. The agent gets the repository at the commit *before* the fix, plus the
tests the PR added. It has to make those tests pass. Everything runs inside
Docker with a real compiler toolchain, so a patch only counts if the software
actually builds and the tests actually run.

**20 instances:** 8 Zephyr, 6 NuttX, 3 RIOT, 3 Apache Mynewt.

---

## Requirements

| | |
|---|---|
| Python | 3.11 (tested) |
| Docker | running, ~40 GB free for images |
| git | for cloning upstream repos during validation |
| API key | for whichever model you run |

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Put your API key in a `.env` file at the repo root:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Recognised keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`TOGETHER_API_KEY`. The runner checks for the right one before starting and
exits early if it is missing, rather than failing partway through a run.

Note that mini-swe-agent *also* loads a global config of its own from outside
the repo — on macOS, `~/Library/Application Support/mini-swe-agent/.env`. It
prints the path at startup. If a key seems to work without being in your `.env`,
or a stale key keeps being used, that file is why.

Activate the venv before running anything. The commands below assume it is
active; a system Python will not have `minisweagent` and will fail only once a
run actually starts, not at `--dry-run`.

Images are large — a NuttX instance is ~8.7 GB — and each project needs its base
image built once before any of its instances.

---

## Two halves of the project

These are deliberately separate and share no code:

- **`scripts/`** — *building* instances. Turns a PR into a Docker image and
  proves the image behaves correctly.
- **`harness/`** — *running* agents. Points a model at an image and grades what
  it produces.

You only need `scripts/` when adding or repairing instances. Day to day, you use
`harness/`.

---

## Quick start

Run one agent on one instance and grade the result:

```bash
# 1. build the images (once per project, slow)
python scripts/build_bases.py --repo zephyr
python scripts/build_instances.py --instance zephyr__zephyr-65697

# 2. run the agent
python harness/run.py --instance zephyr__zephyr-65697 --model claude-opus-4-6 -v

# 3. grade the patch it produced
python harness/evaluate_patches.py --instance zephyr__zephyr-65697 --model claude-opus-4-6
```

`-v` streams the agent's reasoning, commands, and output as it works. Without it
the run is silent until it finishes.

---

## Selecting instances

Every command takes the same three selection flags:

```bash
--instance zephyr__zephyr-65697    # one or more, space separated
--repo nuttx                       # every instance in one project
--all                              # all 20
```

Instance ids are `<project>__<project>-<PR>`, matching the directory names under
`docker/instances/`. Use lowercase.

## Selecting models

Use the short name or the full LiteLLM name:

| short | full |
|---|---|
| `claude-opus-4-6` | `anthropic/claude-opus-4-6` |
| `gpt-5.4` | `openai/gpt-5.4` |
| `gemini-2.5-pro` | `gemini/gemini-2.5-pro` |
| `Qwen-3-coder` | `together_ai/Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8` |
| `DeepSeek-V3` | `together_ai/deepseek-ai/DeepSeek-V3` |

Add `--dry-run` to `run.py`, `evaluate_patches.py`, `build_instances.py`, or
`build_bases.py` to see what each *would* do without starting containers or
spending money.

---

## Where results go

```
outputs/<project>/<model>/<PR>/
    patch.diff          the agent's changes
    trajectory.json     every message, command, and result
outputs/results.json    grades from the last evaluate run
```

---

## How an instance works

Each directory under `docker/instances/` holds four things:

| file | purpose |
|---|---|
| `Dockerfile` | clones the repo at the pre-fix commit, configures, pre-builds |
| `test_patch.diff` | the tests the PR added, applied without the fix |
| `run_tests.sh` | installed as `run_tests`; exits 0 pass, 1 fail, 2 timeout |
| `metadata.json` | commits, problem statement, expected failing tests |

The agent is given the problem statement and a shell. It never sees the real
fix. Test files are protected — the runner diffs against `protected_paths` and
warns if the agent edited them, so it cannot pass by rewriting the test.

Grading happens in a **fresh container**, not the one the agent worked in. Only
`patch.diff` crosses over, so nothing the agent left behind — a stale build
artifact, a running process, a modified test — can influence the result.

---

## Validating an instance

An instance is only useful if it **fails before the fix and passes after**.
`validate_instance.py` checks exactly that: it runs the tests on the base
commit, expects failure, applies the real upstream fix, and expects a pass.

```bash
python scripts/validate_instance.py nuttx__nuttx-11889 --verbose
```

Run this on any instance you build or change. An instance that passes step 1 is
broken — it means the test cannot detect the bug, and every agent will score a
free pass on it. This is not theoretical; see the `CONFIG_NDEBUG` note in
`docker/instances/nuttx__nuttx-11889/Dockerfile` for a case where it happened.

### Apache Mynewt

Build and validate all three Mynewt instances through the same commands:

```bash
python scripts/build_bases.py --repo mynewt
python scripts/build_instances.py --repo mynewt
python scripts/validate_instance.py --repo mynewt --verbose
```

Run and grade an individual task through the unified harness:

```bash
python harness/run.py --instance mynewt__mynewt-3680 --model <configured-model> -v
python harness/evaluate_patches.py --instance mynewt__mynewt-3680 --model <configured-model>
```

Mynewt tests compile to host-native executables using `hw/bsp/native` and
`compiler/sim`. They run on Docker's `linux/amd64` platform and do not use
QEMU.

Mynewt validation first runs the unpatched baseline in a separate container
at the parent of the benchmark test snapshot. Its exact `baseline_tests`
inventory must pass with exit `0`. The test-patched snapshot must then fail
with exit `1`, and the production fix must make the complete expected test
inventory pass with exit `0`.

The default `failure_mode: "runtime"` requires a successful build and the
exact named `fail_to_pass` failures before the fix. For
`failure_mode: "compile"`, declare `compile_test_source` (a changed selftest
C/C++ source in the test patch) and `missing_api` (the API token used there).
Every pre-fix compiler error must name that source and API, except GCC
int-to-pointer assignment cascades following an implicit-function declaration:
each cascade must include a matching source line and column showing a direct
assignment from the missing API call. An unrelated error, compiler crash, or
timeout is not regression evidence. After the fix, compile
mode still requires every `fail_to_pass` and `pass_to_pass` testcase to execute
and pass, with no missing or unexpected cases. Build/runtime timeouts return
`2`; other unqualified build or inventory failures return `3`.

Both modes retain the same eligibility requirements: an originating issue,
a merged PR that changes an executable native selftest, and an exact test-only
patch. If test support lives outside `selftest`, inspect both
`base_commit..test_commit` and `test_commit..head_commit`, declare
`test_support_paths`, and verify that the support does not contain the
production fix. Reject candidates whose separation cannot be established.

No additional compile-mode benchmark candidate has been verified. The three
listed Mynewt instances remain runtime tasks; compile-mode support alone does
not establish a new eligible benchmark instance.

### Finding and generating PR instances

The candidate workflow currently supports Zephyr and Apache Mynewt. Zephyr is
the default; pass `--repo mynewt` for the native-only Mynewt path:

```bash
# Confirm the hard filters retain every hand-validated instance.
python scripts/filter_candidates.py selftest --repo mynewt

# Fetch a date range, apply free hard filters, then collect full context only
# for the surviving PRs.
python scripts/filter_candidates.py fetch --repo mynewt --since 2024-01-01 --until 2024-12-31
python scripts/filter_candidates.py filter --repo mynewt
python scripts/filter_candidates.py enrich --repo mynewt
```

Give each JSON record under `candidates/mynewt/enriched/` to the triage model
with [`docs/mynewt_pr_triage.md`](docs/mynewt_pr_triage.md). Save an accepted
verdict as JSON, then generate the four instance files:

```bash
python scripts/generate_instance.py \
    --repo mynewt \
    --pr 3299 \
    --triage triage/mynewt-3299.json \
    --tag-suffix=-gen
```

Generated instances go under `generated/`, never directly into
`docker/instances/`. Build and validate one in isolation before promoting it:

```bash
EMBEDEVAL_INSTANCES_DIR="$PWD/generated" \
    python scripts/build_instances.py --instance mynewt__mynewt-3299
EMBEDEVAL_INSTANCES_DIR="$PWD/generated" \
    python scripts/validate_instance.py mynewt__mynewt-3299 --verbose
```

---

## Layout

```
harness/        run.py, evaluate_patches.py, paths.py, projects.py
scripts/        build_bases.py, build_instances.py, validate_instance.py, ...
docker/bases/   one base image per project (toolchain + SDK)
docker/instances/   one directory per benchmark task
outputs/        agent runs and grades
archive/        superseded work, kept for provenance
```

Two files carry the per-project differences, and they are the first place to
look when something behaves differently across RTOSes:

- **`harness/projects.py`** — prompts, build and test commands, protected paths,
  cleanup behaviour. Instance `metadata.json` overrides these per instance.
- **`scripts/build_config.py`** — base images, build arguments, platform.

Adding another RTOS means adding an entry to each, not writing a new harness.
