# EmbedEval

A benchmark for evaluating LLM coding agents on **embedded RTOS bug fixes**.

Each task is a real merged pull request from Zephyr, NuttX, or RIOT. The agent
gets the repository at the commit *before* the fix, plus the tests the PR added.
It has to make those tests pass. Everything runs inside Docker with a real
cross-compiler toolchain, so a patch only counts if the firmware actually builds
and the tests actually run.

**17 instances:** 8 Zephyr, 6 NuttX, 3 RIOT.

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
--all                              # all 17
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

Adding a fourth RTOS means adding an entry to each, not writing a new runner.
