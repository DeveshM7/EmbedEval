"""
Run mini-swe-agent on a single EmbedEval instance.
Usage:
    python harness/run_instance.py
    python harness/run_instance.py --instance zephyr__zephyr-65697 --model anthropic/claude-sonnet-4-6
"""

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

class ZephyrDockerEnvironment:
    """
    Wraps DockerEnvironment to automatically clean up stuck QEMU processes
    after timeouts.

    When `west build -t run` is issued, QEMU starts inside the container.
    mini-swe-agent enforces a per-command timeout by killing the `docker exec`
    client process. But killing the client does NOT kill the container-side
    process — QEMU keeps running, holding an exclusive lockf() lock on
    qemu.pid. Every subsequent `west build -t run` fails with
    "Cannot lock pid file" until QEMU is explicitly killed.

    This class intercepts every timed-out execute() call and runs a cleanup
    command inside the container: read the PID from qemu.pid, kill it, then
    remove the file. The agent never sees the PID conflict.
    """

    # Defer the actual DockerEnvironment import so missing package gives a
    # clean error at runtime, not at module load.
    _env = None

    def __init__(self, **kwargs):
        from minisweagent.environments.docker import DockerEnvironment
        self._env = DockerEnvironment(**kwargs)

    # Proxy all attribute access to the underlying environment so the agent
    # framework (which calls .execute(), .cleanup(), .serialize(), etc.) works
    # transparently.
    def __getattr__(self, name):
        return getattr(self._env, name)

    def execute(self, action: dict, **kwargs) -> dict:
        result = self._env.execute(action, **kwargs)
        # returncode -1 + "timed out" in exception_info means docker exec hit
        # the timeout. The container-side process (QEMU) is still alive.
        if (
            result.get("returncode") == -1
            and "timed out" in result.get("exception_info", "").lower()
        ):
            self._kill_stuck_qemu()
        return result

    def _kill_stuck_qemu(self):
        """Kill any QEMU process left running after a docker exec timeout."""
        # Kill via the PID file (most precise — avoids matching the calling
        # bash process by name). Then remove the file so the next run starts
        # clean.
        # We do NOT use `pkill -f qemu-system-i386` because that pattern
        # matches the calling bash's own cmdline and kills the shell itself.
        cleanup = (
            "if [ -f /testbed/build/qemu.pid ]; then "
            "  kill -9 $(cat /testbed/build/qemu.pid) 2>/dev/null || true; "
            "fi; "
            "rm -f /testbed/build/qemu.pid /testbed/build/zephyr/qemu.pid"
        )
        import subprocess
        try:
            cmd = [
                self._env.config.executable,
                "exec",
                self._env.container_id,
                "bash", "-c", cleanup,
            ]
            subprocess.run(cmd, timeout=10, capture_output=True)
        except Exception:
            pass  # best-effort; if this fails the agent will hit the error


def make_verbose_agent_class(base_class):
    """Wraps DefaultAgent to print each step live as it happens."""
    import re

    class VerboseAgent(base_class):
        def add_messages(self, *messages):
            for msg in messages:
                role = msg.get("role", "")
                if role == "assistant":
                    actions = msg.get("extra", {}).get("actions", [])
                    if actions:
                        cmd = actions[0].get("command", "")
                        print(f"\n\033[1;34m[CMD]\033[0m {cmd[:300]}")
                    else:
                        content = msg.get("content") or ""
                        if content:
                            print(f"\n\033[1;33m[THOUGHT]\033[0m {content[:300]}")
                elif role in ("user", "tool"):
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        # tool_result format
                        for part in content:
                            if isinstance(part, dict):
                                content = str(part.get("content", ""))
                                break
                    if isinstance(content, str) and content:
                        clean = re.sub(r"<[^>]+>", "", content).strip()
                        if clean:
                            # Show last 1500 chars so build preamble scrolls off
                            # and run_tests results (which appear last) are visible.
                            snippet = clean[-1500:] if len(clean) > 1500 else clean
                            print(f"\033[0;32m[OUT]\033[0m {snippet}")
                elif role == "exit":
                    status = msg.get("extra", {}).get("exit_status", "")
                    print(f"\n\033[1;31m[EXIT]\033[0m {status}")
            return super().add_messages(*messages)

    return VerboseAgent

# ---------------------------------------------------------------------------
# Agent prompt templates
# These follow the format mini-swe-agent expects.
# system_template: sets the agent's persona and response format rules.
# instance_template: the first user message; {{task}} is filled by agent.run().
# ---------------------------------------------------------------------------

SYSTEM_TEMPLATE = """\
You are an expert embedded systems engineer. You can interact with a Linux shell
to navigate codebases, edit source files, build firmware, and run tests.
You are working inside a Zephyr RTOS repository.

Your response must contain exactly ONE bash code block with ONE command
(or commands connected with && or ||).
Include a THOUGHT section before your command explaining your reasoning.

<format_example>
THOUGHT: Your reasoning here.

```mswea_bash_command
your_command_here
```
</format_example>

CRITICAL RULES — responses that break these are rejected:
- Every response MUST include exactly one ```mswea_bash_command``` block.
- NEVER use heredoc syntax (<<'EOF' or <<'PY'). It breaks inside docker exec.
  Use python3 -c "..." with a one-liner instead.
- After a successful build you MUST still run `run_tests` before submitting.
  A passing build does NOT mean tests pass.
"""

INSTANCE_TEMPLATE = """\
Please solve this issue:

{{task}}

## Important Rules

1. Every response must contain exactly one action in triple backticks.
2. Do NOT modify any files under tests/.
3. Environment variable changes and directory changes are NOT persistent between
   commands — every action runs in a new subshell. Use absolute paths or prefix
   commands with `cd /testbed &&`.

## Running Tests

Use the `run_tests` command to run tests. It handles all QEMU setup and cleanup,
waits for results, and exits immediately once pass/fail is known:

    cd /testbed && run_tests

Exit codes: 0 = all tests passed, 1 = tests failed, 2 = timed out.

Do NOT use `west build -t run` directly — QEMU never exits cleanly, so that
command always hangs until the docker timeout kills it, and you will never see
whether your fix worked.

If `run_tests` times out (exit code 2) with no test results, your fix has
introduced an infinite loop — do NOT submit. Investigate the bug further.

Mandatory workflow:
1. Explore the code and understand the bug
2. Edit the source file to fix the bug
3. Build and test: `cd /testbed && west build && run_tests`
   - If build fails: fix the error and repeat
   - If run_tests shows tests FAILING: fix the bug and go back to step 3
   - If run_tests shows all target tests PASSING: go to step 4
4. Submit by running THIS EXACT COMMAND ALONE — nothing before or after it:

```
echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
```

CRITICAL: The submit command must be the ONLY thing in your response's code block.
Do NOT chain it with && or any other command. It must be alone.

Other useful commands:
- Full rebuild from scratch: `cd /testbed && rm -rf build && west build -b qemu_x86 tests/posix/common`
- Find code: `grep -rn "name" --include="*.c" --include="*.h" /testbed`
- ctags index: `grep "function_name" /testbed/tags`

## Zephyr Reference Guide

A reference guide is available at `/testbed/ZEPHYR_GUIDE.md`. Read it if you need
help with the Zephyr repo layout, west build commands, test framework, or common
code patterns. Usage: `cat /testbed/ZEPHYR_GUIDE.md`

<system_information>
{{system}} {{release}} {{machine}}
</system_information>
"""


def load_metadata(instance_id: str) -> dict:
    path = REPO_ROOT / "docker" / "instances" / instance_id / "metadata.json"
    if not path.exists():
        sys.exit(f"ERROR: metadata.json not found at {path}")
    with open(path) as f:
        return json.load(f)


def build_task(meta: dict) -> str:
    """Format the problem statement + test context into the task string."""
    fail_tests = "\n".join(f"  - {t}" for t in meta["fail_to_pass"])
    pass_tests = "\n".join(f"  - {t}" for t in meta["pass_to_pass"])
    fix_files = "\n".join(f"  - {f}" for f in meta["files_changed_by_fix"])

    return f"""\
## Bug Description

{meta["problem_statement"]}

## Failing Tests (must pass after your fix)

{fail_tests}

## Tests That Must Continue Passing

{pass_tests}

## Relevant Source File(s)

{fix_files}

## Build & Test Command

    cd /testbed && west build && run_tests

Full rebuild if needed: cd /testbed && rm -rf build && {meta["build_command"]}

Once all target tests PASS in run_tests output, submit with this command ALONE:
    echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--instance",
        default="zephyr__zephyr-65697",
        help="Instance ID matching a dir under docker/instances/",
    )
    parser.add_argument(
        "--model",
        default="openai/gpt-5.4",
        help="Model in LiteLLM format (e.g. anthropic/claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--step-limit",
        type=int,
        default=50,
        help="Max agent steps (0 = unlimited)",
    )
    parser.add_argument(
        "--cost-limit",
        type=float,
        default=3.0,
        help="Max spend in USD (0 = unlimited)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "outputs"),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print each agent command and output live as it happens",
    )
    args = parser.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ and "OPENAI_API_KEY" not in os.environ:
        sys.exit("ERROR: No API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY.")

    try:
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.environments.docker import DockerEnvironment
        from minisweagent.models import get_model
    except ImportError:
        sys.exit("ERROR: mini-swe-agent not installed. Run: pip install mini-swe-agent")

    meta = load_metadata(args.instance)
    task = build_task(meta)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Instance   : {meta['instance_id']}")
    print(f"Image      : {meta['docker_image']}")
    print(f"Model      : {args.model}")
    print(f"Step limit : {args.step_limit}  Cost limit: ${args.cost_limit}")
    print(f"Output     : {output_dir}")
    print()

    # DockerEnvironment starts its own container from the image.
    # cwd=/testbed so every command lands in the repo root.
    # timeout=180s to give west build enough room (cold builds ~60-120s).
    env = ZephyrDockerEnvironment(
        image=meta["docker_image"],
        cwd="/testbed",
        timeout=180,
    )

    AgentClass = make_verbose_agent_class(DefaultAgent) if args.verbose else DefaultAgent

    # DefaultAgent takes system_template and instance_template as required kwargs.
    # step_limit and cost_limit are optional AgentConfig fields.
    agent = AgentClass(
        get_model(input_model_name=args.model),
        env,
        system_template=SYSTEM_TEMPLATE,
        instance_template=INSTANCE_TEMPLATE,
        step_limit=args.step_limit,
        cost_limit=args.cost_limit,
        output_path=output_dir / f"{meta['instance_id']}.trajectory.json",
    )

    print("Starting agent...")
    print("=" * 60)
    result = agent.run(task)
    print("=" * 60)
    print(f"Exit status : {result.get('exit_status', 'unknown')}")

    # Capture git diff limited to only the files the fix should touch.
    # The test patch is pre-applied in the Dockerfile, so a plain `git diff`
    # would include test file changes that the agent didn't write.
    files = meta.get("files_changed_by_fix", [])
    diff_cmd = "git diff -- " + " ".join(files) if files else "git diff"
    diff_result = env.execute({"command": diff_cmd})
    patch = diff_result.get("output", "")

    patch_path = output_dir / f"{meta['instance_id']}.patch"
    patch_path.write_text(patch)
    print(f"Patch saved : {patch_path}")

    if not patch.strip():
        print("WARNING: agent made no changes (empty diff).")


if __name__ == "__main__":
    main()
