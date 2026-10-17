"""
Run an agent on EmbedEval instances.

    python harness/run.py --instance zephyr__zephyr-65697 --model gpt-5.4
    python harness/run.py --repo nuttx --model gpt-5.4
    python harness/run.py --all --model gpt-5.4 gemini-2.5-pro

Twister-pipeline Zephyr instances (generated/, built as Apptainer images) run in
an Apptainer environment instead of Docker, e.g. with the local Qwen server:

    EMBEDEVAL_INSTANCES_DIR=generated python harness/run.py \
        --instance zephyr__zephyr-65697 --model qwen38-nvfp4

So do NuttX instances whose metadata says "runtime": "apptainer".

Replaces run_instance.py, run_nuttx_instance.py, run_riot_instance.py and
run_all.py, which were ~50% duplicated and disagreed on prompt format, .env
handling and the API-key check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths
import projects

try:
    from dotenv import load_dotenv

    load_dotenv(paths.REPO_ROOT / ".env")
except ImportError:
    pass


# --------------------------------------------------------------------------
# Prompts. One pair of templates for every project; the differences arrive
# through the {slots}, which come from projects.py and metadata.json.
# --------------------------------------------------------------------------

SYSTEM_TEMPLATE = """\
You are an expert embedded systems engineer. You can interact with a Linux shell
to navigate codebases, edit source files, build firmware, and run tests.
You are working inside a {label} repository.

{orientation}

You interact with the shell by calling the `bash` tool. Every response must
include exactly one `bash` tool call. Put your reasoning in the message content,
then call the tool.

CRITICAL RULES:
- Never reply with text alone -- every response must call the `bash` tool.
- NEVER use heredoc syntax (<<'EOF' or <<'PY'). It breaks inside docker exec.
  Use python3 -c "..." with a one-liner instead.
- After a successful build you MUST still run `run_tests` before submitting.
  A passing build does NOT mean tests pass.
"""

INSTANCE_TEMPLATE = """\
Please solve this issue:

{{{{task}}}}

## Important Rules

1. Every response must contain exactly one `bash` tool call.
2. Do NOT modify any files under {protected}.
3. Environment variable and directory changes are NOT persistent between
   commands -- every action runs in a new subshell. Use absolute paths or
   prefix commands with `cd /testbed &&`.

## Running Tests

{test_help}
{extra_warnings}
Mandatory workflow:
1. Explore the code and understand the bug
2. Edit the source file(s) to fix it
3. Build and test: `{build_and_test}`
   - If the build fails: fix the error and repeat
   - If run_tests shows tests FAILING: fix the bug and go back to step 3
   - If run_tests shows all target tests PASSING: go to step 4
4. Submit by calling the `bash` tool with exactly this command:

       echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT

   Do NOT chain it with && or any other command.

Other useful commands:
- Find code:   grep -rn "name" --include="*.c" --include="*.h" /testbed
- ctags index: grep "function_name" /testbed/tags

<system_information>
{{{{system}}}} {{{{release}}}} {{{{machine}}}}
</system_information>
"""

FORMAT_ERROR_TEMPLATE = (
    "ERROR: No tool call found in your response. Do not apologize or explain "
    "yourself -- execute actions by calling the `bash` tool. If you are finished, "
    "call the `bash` tool with the command: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
)

# Which environment variable each provider needs, for a pre-flight check that
# fails at launch instead of 40 steps in.
PROVIDER_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "together_ai": ("TOGETHER_API_KEY", "TOGETHERAI_API_KEY"),
}


# Models served locally by scripts/serve_triage_model.sh: vLLM's
# OpenAI-compatible API, so LiteLLM's openai/ route with an api_base. The
# sampling settings come from the same profile triage uses (triage/models.json).
# EMBEDEVAL_LOCAL_API_BASE overrides the address; the server listens on
# 127.0.0.1, so the harness must run on the server's node.
LOCAL_MODELS = {"openai/qwen38-nvfp4": "qwen38-nvfp4"}
REQUEST_TIMEOUT = 3600  # long generations; LiteLLM's 600 s default silently retried them


def model_config(full_model: str) -> dict:
    """mini-swe-agent model config for a local model; {} for hosted ones."""
    profile = LOCAL_MODELS.get(full_model)
    if not profile:
        return {}
    prof = json.loads((paths.REPO_ROOT / "triage" / "models.json").read_text())[profile]
    sampling = dict(prof.get("sampling", {}))
    extra = {k: sampling.pop(k) for k in ("top_k", "min_p") if k in sampling}
    kwargs = dict(api_base=os.environ.get("EMBEDEVAL_LOCAL_API_BASE", "http://127.0.0.1:8000/v1"),
                  api_key="local", timeout=REQUEST_TIMEOUT, num_retries=0,
                  max_tokens=prof.get("max_tokens", 32768), **sampling)
    if extra:
        kwargs["extra_body"] = extra
    # A local model has no price; without this LiteLLM's cost lookup fails.
    return {"model_kwargs": kwargs, "cost_tracking": "ignore_errors"}


def check_api_key(model_name: str) -> None:
    if paths.resolve_model(model_name) in LOCAL_MODELS:
        return
    provider = paths.resolve_model(model_name).split("/")[0]
    names = PROVIDER_KEYS.get(provider)
    if not names:
        return
    if not any(n in os.environ for n in names):
        sys.exit(
            f"ERROR: no API key for provider {provider!r} (model {model_name}). "
            f"Set one of: {', '.join(names)}"
        )
    # litellm reads ANTHROPIC_API_KEY; accept CLAUDE_API_KEY as an alias.
    if provider == "anthropic" and "ANTHROPIC_API_KEY" not in os.environ:
        os.environ["ANTHROPIC_API_KEY"] = os.environ["CLAUDE_API_KEY"]


def build_prompts(meta: dict) -> tuple[str, str]:
    cfg = projects.config_for(meta)
    protected = " or ".join(f"`{p}`" for p in cfg["protected_paths"])
    warn = cfg["extra_warnings"]
    system = SYSTEM_TEMPLATE.format(
        label=cfg["label"], orientation=cfg["orientation"]
    )
    instance = INSTANCE_TEMPLATE.format(
        protected=protected,
        extra_warnings=f"\n{warn}\n" if warn else "",
        build_and_test=_build_and_test(meta),
        test_help=TEST_HELP.get(meta["project"], TEST_HELP["default"]),
    )
    return system, instance


_HANG = ("If `run_tests` times out{code} with no test results, your change has\n"
         "introduced a hang or an infinite loop. Do NOT submit -- investigate further.")

# What run_tests' exit codes mean, per project; they must match the runner.
# Zephyr's codes are in its extra warning (projects.py), and a Zephyr timeout
# is the environment's command limit, not an exit code.
TEST_HELP = {
    "nuttx": (
        "Use `run_tests` after building. It runs the whole ostest suite and "
        "waits for results. Exit codes: 0 = ostest finished cleanly and every "
        "changed test ran, 1 = any other completed run (a test failed, or a "
        "changed test did not run), 2 = timed out.\n\n" + _HANG.format(code=" (exit code 2)")
    ),
    "zephyr": (
        "Use the `run_tests` command to build and run the tests. It handles "
        "all setup and teardown and waits for results.\n\n" + _HANG.format(code="")
    ),
    "default": (
        "Use the `run_tests` command to build and run the tests. It handles "
        "all setup and teardown, waits for results, and exits as soon as "
        "pass/fail is known. Exit codes: 0 = all tests passed, "
        "1 = tests failed, 2 = timed out.\n\n" + _HANG.format(code=" (exit code 2)")
    ),
}


def _build_and_test(meta: dict) -> str:
    """
    The incremental build+test command shown to the agent.

    Deliberately NOT metadata's build_command: that is the cold build used by
    the evaluator after wiping the build directory. Re-specifying -b and a test
    path is what produced invented board names in the existing trajectories.
    """
    rebuild = projects.config_for(meta)["rebuild_command"]
    return f"cd /testbed && {rebuild} && run_tests" if rebuild else "cd /testbed && run_tests"


def build_task(meta: dict) -> str:
    """The first user message: the problem plus its test context."""
    def bullets(items):
        return "\n".join(f"  - {i}" for i in items) or "  (none)"

    return f"""\
## Bug Description

{meta["problem_statement"]}

## Failing Tests (must pass after your fix)

{bullets(meta.get("fail_to_pass", []))}

## Tests That Must Continue Passing

{bullets(meta.get("pass_to_pass", []))}

## Relevant Source File(s)

{bullets(meta.get("files_changed_by_fix", []))}
"""


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


def make_environment(meta: dict):
    cfg = projects.config_for(meta)
    if projects.setting(meta, "runtime") == "apptainer":
        # Zephyr, and NuttX instances rendered for Apptainer: an Apptainer
        # image built by scripts/build_instances.py.
        sys.path.insert(0, str(paths.REPO_ROOT / "scripts"))
        import apptainer_util
        from apptainer_env import ApptainerEnvironment

        apptainer_util.require_host_arch(meta)

        # The image carries the PR's test changes as uncommitted edits.
        # Committing them in this run's overlay before the agent starts makes
        # `git diff` show the agent's changes alone, so capture_patch's check
        # for edits to protected paths means what it says. Graded patches still
        # apply to the image as built: the same files, the same content.
        # NuttX needs no such commit: its test changes are in apps/, a
        # repository of its own that the image keeps out of the kernel's git.
        commit_tests = ("cd /testbed && git add -A -- tests/ && git -c user.name=EmbedEval "
                        "-c user.email=embedeval@localhost commit -q -m "
                        "'The PR test changes, without its fix'")
        return ApptainerEnvironment(image=apptainer_util.instance_image(meta["instance_id"]),
                                    name=meta["instance_id"], cwd="/testbed",
                                    timeout=cfg.get("command_timeout", 900),
                                    setup_commands=[commit_tests] if meta["project"] == "zephyr"
                                    else [])

    # NuttX, RIOT: Docker.
    kwargs = dict(image=meta["docker_image"], cwd="/testbed",
                  timeout=projects.setting(meta, "command_timeout", 180))
    platform = projects.setting(meta, "docker_platform")
    if platform:
        kwargs["run_args"] = ["--rm", "--platform", platform]
    from minisweagent.environments.docker import DockerEnvironment

    return DockerEnvironment(**kwargs)


def make_verbose_agent_class(base_class):
    """Print each step live as it happens."""

    class VerboseAgent(base_class):
        def add_messages(self, *messages):
            for msg in messages:
                role = msg.get("role", "")
                if role == "assistant":
                    actions = msg.get("extra", {}).get("actions", [])
                    if actions:
                        print(f"\n\033[1;34m[CMD]\033[0m {actions[0].get('command','')[:300]}")
                    elif msg.get("content"):
                        print(f"\n\033[1;33m[THOUGHT]\033[0m {str(msg['content'])[:300]}")
                elif role in ("user", "tool"):
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict):
                                content = str(part.get("content", ""))
                                break
                    if isinstance(content, str) and content.strip():
                        clean = re.sub(r"<[^>]+>", "", content).strip()
                        if clean:
                            print(f"\033[0;32m[OUT]\033[0m {clean[-1500:]}")
                elif role == "exit":
                    print(f"\n\033[1;31m[EXIT]\033[0m {msg.get('extra',{}).get('exit_status','')}")
            return super().add_messages(*messages)

    return VerboseAgent


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------


def start_commit(env) -> str:
    """The commit the agent starts from (after any setup commit), so the patch
    is everything since -- whatever the agent did with git along the way."""
    out = env.execute({"command": "cd /testbed && git rev-parse HEAD"})
    sha = out.get("output", "").strip()
    return sha if out.get("returncode") == 0 and len(sha) == 40 else "HEAD"


def capture_patch(env, meta: dict, start: str = "HEAD") -> tuple[str, list[str]]:
    """
    Return (patch, protected_files_touched).

    Everything the agent changed since `start`: edits to tracked files, new
    files, and changes it staged or committed itself (`git add -A`, then a diff
    of the index against `start`; ignored paths such as build directories stay
    out). A plain `git diff` saw only unstaged edits to tracked files and lost
    the rest -- a new source file, or files the agent staged with `git
    checkout <rev> -- <path>`.

    The full diff is captured, minus the project's protected paths, so a correct
    fix outside the gold patch's file set is preserved -- the old behaviour
    filtered to files_changed_by_fix and silently discarded anything else.
    Protected paths are excluded from the patch but reported, so test tampering
    is visible rather than quietly dropped.
    """
    protected = projects.config_for(meta)["protected_paths"]
    excludes = " ".join(f"':(exclude){p}'" for p in protected)
    patch = env.execute({"command": f"cd /testbed && git add -A && "
                                    f"git diff --cached --binary {start} -- . {excludes}"}
                        ).get("output", "")
    touched = env.execute(
        {"command": f"cd /testbed && git diff --cached --name-only {start} -- "
                    + " ".join(f"'{p}'" for p in protected)}
    ).get("output", "")
    return patch, [t for t in touched.splitlines() if t.strip()]


# The run in progress, for the SIGTERM handler: {"save": callable, "env": env}.
_CURRENT: dict = {}


def _stop_on_sigterm(signum, frame):
    """`timeout`, scancel and Slurm stop a run with SIGTERM, then SIGKILL a
    little later. Save what the agent did and remove the overlay right here,
    then exit. Raising an exception instead is not enough: SIGTERM usually
    lands while the agent waits for the model, and the model client's retry
    wrapper catches the exception and retries the call."""
    print(f"\n  stopped by signal {signum}: saving the patch", flush=True)
    try:
        if _CURRENT.get("save"):
            _CURRENT["save"]()
            print("  patch saved", flush=True)
    except BaseException as e:
        print(f"  could not save the patch: {type(e).__name__}: {e}", flush=True)
    try:
        if _CURRENT.get("env") is not None:
            _CURRENT["env"].cleanup()
    except BaseException:
        pass
    os._exit(128 + signum)


def run_one(instance_id: str, model_name: str, args) -> dict:
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.models import get_model

    meta = paths.load_metadata(instance_id)
    full_model = paths.resolve_model(model_name)
    out_dir = paths.run_dir(instance_id, full_model)
    out_dir.mkdir(parents=True, exist_ok=True)

    system_template, instance_template = build_prompts(meta)
    task = build_task(meta)

    print(f"  instance : {instance_id}")
    print(f"  model    : {full_model}")
    print(f"  image    : {meta.get('docker_image') or meta.get('apptainer_image')}")
    print(f"  output   : {paths.show(out_dir)}")

    result = {
        "instance_id": instance_id,
        "model": full_model,
        "status": "error",
        "exit_status": None,
        "steps": None,
        "cost": None,
        "patch_empty": True,
        "protected_touched": [],
        "elapsed_seconds": 0.0,
        "error": None,
    }
    start = time.time()
    env, captured, base = None, False, "HEAD"

    def save_patch() -> None:
        patch, touched = capture_patch(env, meta, base)
        paths.patch_path(instance_id, full_model).write_text(patch)
        result["patch_empty"] = not patch.strip()
        result["protected_touched"] = touched

    try:
        env = make_environment(meta)
        base = start_commit(env)
        _CURRENT.update(save=save_patch, env=env)
        model = get_model(input_model_name=full_model, config=model_config(full_model))
        model.config.format_error_template = FORMAT_ERROR_TEMPLATE

        agent_class = make_verbose_agent_class(DefaultAgent) if args.verbose else DefaultAgent
        agent = agent_class(
            model,
            env,
            system_template=system_template,
            instance_template=instance_template,
            step_limit=args.step_limit,
            cost_limit=args.cost_limit,
            wall_time_limit_seconds=int(args.time_limit * 60),
            output_path=paths.trajectory_path(instance_id, full_model),
        )
        run_info = agent.run(task)
        result["exit_status"] = run_info.get("exit_status")

        save_patch()
        captured = True
        touched = result["protected_touched"]
        result["status"] = "success"
        if touched:
            print(f"  WARNING: agent modified protected paths: {', '.join(touched)}")
        if result["patch_empty"]:
            print("  WARNING: agent made no changes (empty diff)")
    except KeyboardInterrupt:
        raise
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        print(f"  ERROR: {result['error']}")
    finally:
        _CURRENT.clear()
        if env is not None and not captured:
            # Stopped early by an error:
            # keep whatever the agent had done, so it can still be graded.
            try:
                save_patch()
                result["patch_captured"] = "after interruption"
            except BaseException as e:
                print(f"  could not save the patch: {type(e).__name__}: {e}")
        result["elapsed_seconds"] = round(time.time() - start, 1)
        traj = paths.trajectory_path(instance_id, full_model)
        if traj.exists():
            try:
                stats = json.loads(traj.read_text())["info"]["model_stats"]
                result["steps"] = stats.get("api_calls")
                result["cost"] = round(stats.get("instance_cost", 0), 4)
            except Exception:
                pass
        if env is not None:
            try:
                env.cleanup()
            except Exception:
                pass
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def select_instances(args) -> list[str]:
    if args.instance:
        known = set(paths.discover_instances())
        for i in args.instance:
            if i not in known:
                sys.exit(f"ERROR: unknown instance {i!r}")
        return list(args.instance)
    if args.repo:
        found = paths.discover_instances(args.repo)
        if not found:
            sys.exit(f"ERROR: no instances for repo {args.repo!r}")
        return found
    return paths.discover_instances()


def main() -> None:
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    p = argparse.ArgumentParser(description="Run an agent on EmbedEval instances.")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--instance", nargs="+", help="one or more instance ids")
    sel.add_argument("--repo", choices=paths.PROJECTS, help="every instance for one project")
    sel.add_argument("--all", action="store_true", help="every instance, all projects")
    p.add_argument("--model", nargs="+", required=True,
                   help="one or more models (full LiteLLM name or shorthand)")
    p.add_argument("--step-limit", type=int, default=50)
    p.add_argument("--cost-limit", type=float, default=3.0)
    p.add_argument("--time-limit", type=float, default=0,
                   help="minutes of wall-clock time per run, checked before each model "
                        "call; the patch is captured as usual (default 0: no limit)")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="print the planned runs and exit without starting containers")
    args = p.parse_args()

    try:
        models = [paths.resolve_model(m) for m in args.model]
    except paths.UnknownModelError as e:
        sys.exit(f"ERROR: {e}")
    instances = select_instances(args)

    print(f"Instances : {len(instances)}")
    print(f"Models    : {', '.join(models)}")
    print(f"Total runs: {len(instances) * len(models)}")
    print(f"Limits    : {args.step_limit} steps, ${args.cost_limit}\n")

    if args.dry_run:
        for m in models:
            for i in instances:
                print(f"  {m:46s} {i:24s} -> {paths.show(paths.run_dir(i, m))}")
        return

    for m in models:
        check_api_key(m)

    results = []
    n = 0
    total = len(instances) * len(models)
    for model_name in models:
        for instance_id in instances:
            n += 1
            print(f"\n{'=' * 70}\n[{n}/{total}] {instance_id}  x  {model_name}\n{'=' * 70}")
            results.append(run_one(instance_id, model_name, args))

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    for r in results:
        flag = "ok  " if r["status"] == "success" and not r["patch_empty"] else "WARN"
        print(
            f"  [{flag}] {r['model']:34s} {r['instance_id']:24s} "
            f"{str(r['exit_status'] or r['error'] or ''):22.22s} "
            f"steps={str(r['steps']):>4s} ${str(r['cost']):>7s} {r['elapsed_seconds']:>7.1f}s"
        )
    ok = sum(1 for r in results if r["status"] == "success" and not r["patch_empty"])
    print(f"\n  {ok}/{len(results)} runs produced a non-empty patch")
    tampered = [r for r in results if r["protected_touched"]]
    if tampered:
        print(f"  {len(tampered)} run(s) modified protected paths -- see warnings above")


if __name__ == "__main__":
    main()
