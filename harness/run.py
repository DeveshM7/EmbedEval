"""
Run an agent on EmbedEval instances.

    python harness/run.py --instance zephyr__zephyr-65697 --model gpt-5.4
    python harness/run.py --repo nuttx --model gpt-5.4
    python harness/run.py --all --model gpt-5.4 gemini-2.5-pro

Replaces run_instance.py, run_nuttx_instance.py, run_riot_instance.py and
run_all.py, which were ~50% duplicated and disagreed on prompt format, .env
handling and the API-key check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
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

Use the `run_tests` command to build and run the tests. It handles all setup and
teardown, waits for results, and exits as soon as pass/fail is known.
Exit codes: 0 = all tests passed, 1 = tests failed, 2 = timed out.

If `run_tests` times out (exit code 2) with no test results, your change has
introduced a hang or an infinite loop. Do NOT submit -- investigate further.
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


def check_api_key(model_name: str) -> None:
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
    cfg = projects.config(meta["project"])
    protected = " or ".join(f"`{p}`" for p in cfg["protected_paths"])
    warn = cfg["extra_warnings"]
    system = SYSTEM_TEMPLATE.format(
        label=cfg["label"], orientation=cfg["orientation"]
    )
    instance = INSTANCE_TEMPLATE.format(
        protected=protected,
        extra_warnings=f"\n{warn}\n" if warn else "",
        build_and_test=_build_and_test(meta),
    )
    return system, instance


def _build_and_test(meta: dict) -> str:
    """
    The incremental build+test command shown to the agent.

    Deliberately NOT metadata's build_command: that is the cold build used by
    the evaluator after wiping the build directory. Re-specifying -b and a test
    path is what produced invented board names in the existing trajectories.
    """
    rebuild = projects.config(meta["project"])["rebuild_command"]
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


class QemuCleanupEnvironment:
    """
    DockerEnvironment plus orphaned-QEMU cleanup, for projects that run tests
    under QEMU (Zephyr).

    A per-command timeout kills the `docker exec` client on the host, but the
    container-side QEMU keeps running and holds an exclusive lock on qemu.pid.
    Every later `run_tests` then fails with "Cannot lock pid file". This kills
    the orphan by PID file -- not by process name, which would match the calling
    shell and kill the session.
    """

    _CLEANUP = (
        "if [ -f /testbed/build/qemu.pid ]; then "
        "  kill -9 $(cat /testbed/build/qemu.pid) 2>/dev/null || true; "
        "fi; "
        "rm -f /testbed/build/qemu.pid /testbed/build/zephyr/qemu.pid"
    )

    def __init__(self, **kwargs):
        from minisweagent.environments.docker import DockerEnvironment

        self._env = DockerEnvironment(**kwargs)

    def __getattr__(self, name):
        return getattr(self._env, name)

    def execute(self, action: dict, **kwargs) -> dict:
        result = self._env.execute(action, **kwargs)
        if (
            result.get("returncode") == -1
            and "timed out" in (result.get("exception_info") or "").lower()
        ):
            try:
                subprocess.run(
                    [
                        self._env.config.executable,
                        "exec",
                        self._env.container_id,
                        "bash",
                        "-c",
                        self._CLEANUP,
                    ],
                    timeout=10,
                    capture_output=True,
                )
            except Exception:
                pass  # best effort; the agent will see the lock error otherwise
        return result


def make_environment(meta: dict):
    cfg = projects.config(meta["project"])
    kwargs = dict(image=meta["docker_image"], cwd="/testbed", timeout=180)
    platform = projects.setting(meta, "docker_platform")
    if platform:
        kwargs["run_args"] = ["--rm", "--platform", platform]
    if cfg["needs_qemu_cleanup"]:
        return QemuCleanupEnvironment(**kwargs)
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


def capture_patch(env, meta: dict) -> tuple[str, list[str]]:
    """
    Return (patch, protected_files_touched).

    The full diff is captured, minus the project's protected paths, so a correct
    fix outside the gold patch's file set is preserved -- the old behaviour
    filtered to files_changed_by_fix and silently discarded anything else.
    Protected paths are excluded from the patch but reported, so test tampering
    is visible rather than quietly dropped.
    """
    protected = projects.config(meta["project"])["protected_paths"]
    excludes = " ".join(f"':(exclude){p}'" for p in protected)
    patch = env.execute({"command": f"cd /testbed && git diff -- . {excludes}"}).get("output", "")
    touched = env.execute(
        {"command": "cd /testbed && git diff --name-only -- " + " ".join(f"'{p}'" for p in protected)}
    ).get("output", "")
    return patch, [t for t in touched.splitlines() if t.strip()]


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
    print(f"  image    : {meta['docker_image']}")
    print(f"  output   : {out_dir.relative_to(paths.REPO_ROOT)}")

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
    env = None
    try:
        env = make_environment(meta)
        model = get_model(input_model_name=full_model)
        model.config.format_error_template = FORMAT_ERROR_TEMPLATE

        agent_class = make_verbose_agent_class(DefaultAgent) if args.verbose else DefaultAgent
        agent = agent_class(
            model,
            env,
            system_template=system_template,
            instance_template=instance_template,
            step_limit=args.step_limit,
            cost_limit=args.cost_limit,
            output_path=paths.trajectory_path(instance_id, full_model),
        )
        run_info = agent.run(task)
        result["exit_status"] = run_info.get("exit_status")

        patch, touched = capture_patch(env, meta)
        paths.patch_path(instance_id, full_model).write_text(patch)
        result["patch_empty"] = not patch.strip()
        result["protected_touched"] = touched
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
    p = argparse.ArgumentParser(description="Run an agent on EmbedEval instances.")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--instance", nargs="+", help="one or more instance ids")
    sel.add_argument("--repo", choices=paths.PROJECTS, help="every instance for one project")
    sel.add_argument("--all", action="store_true", help="every instance, all projects")
    p.add_argument("--model", nargs="+", required=True,
                   help="one or more models (full LiteLLM name or shorthand)")
    p.add_argument("--step-limit", type=int, default=50)
    p.add_argument("--cost-limit", type=float, default=3.0)
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
                print(f"  {m:46s} {i:24s} -> {paths.run_dir(i, m).relative_to(paths.REPO_ROOT)}")
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
