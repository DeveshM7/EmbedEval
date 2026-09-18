"""
Run mini-swe-agent on a single NuttX EmbedEval instance.
Usage:
    python harness/run_nuttx_instance.py
    python harness/run_nuttx_instance.py --instance nuttx__nuttx-8885 --model anthropic/claude-sonnet-4-6
"""

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# Load .env from repo root if present (requires python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass


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
# ---------------------------------------------------------------------------

SYSTEM_TEMPLATE = """\
You are an expert embedded systems engineer. You can interact with a Linux shell
to navigate codebases, edit source files, build firmware, and run tests.
You are working inside a NuttX RTOS repository.

NuttX uses two repos: the kernel at /testbed and the apps at /testbed/apps.
Builds use GNU Make (not CMake/west). The sim target produces a native Linux
binary at /testbed/nuttx — no QEMU or cross-compiler needed.

Call the bash tool to run shell commands. Each call must contain exactly one
command (or commands connected with && or ||).

If the bash tool is unavailable, wrap your command in a ```mswea_bash_command```
code block — use no other tag.

Rules:
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
2. Do NOT modify any files under apps/testing/ or apps/examples/ (test files).
3. Environment variable changes and directory changes are NOT persistent between
   commands — every action runs in a new subshell. Use absolute paths or prefix
   commands with `cd /testbed &&`.

## Running Tests

Use the `run_tests` command to run tests. It pipes NSH commands into the sim
binary, watches for the ostest completion string, and exits cleanly:

    cd /testbed && run_tests

Exit codes: 0 = all tests passed, 1 = tests failed, 2 = timed out.

Do NOT pipe directly into ./nuttx yourself — the run_tests wrapper handles
process management and cleanup correctly.

If `run_tests` times out (exit code 2) with no test results, your fix has
introduced an infinite loop or hang — do NOT submit. Investigate further.

Mandatory workflow:
1. Explore the code and understand the bug
2. Edit the source file(s) in /testbed (kernel side) to fix the bug
3. Build and test: `cd /testbed && make -j$(nproc) && run_tests`
   - If build fails: fix the error and repeat
   - If run_tests shows tests FAILING: fix the bug and go back to step 3
   - If run_tests prints "PASSED" at the end: ALL target tests have passed.
     STOP immediately and go to step 4. Do NOT run any more commands.
4. Submit using this EXACT block — it must be the only thing in your response:

```mswea_bash_command
echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
```

CRITICAL: Use the mswea_bash_command tag, not ```bash or ```. Do NOT chain
with && or any other command. The submit block must be alone.

Other useful commands:
- Full rebuild from scratch: `cd /testbed && make distclean && ./tools/configure.sh -a ./apps sim:nsh && make olddefconfig && make -j$(nproc)`
- Find code: `grep -rn "symbol_name" --include="*.c" --include="*.h" /testbed`
- Search ctags index: `grep "function_name" /testbed/tags`
- View a file: `cat /testbed/include/nuttx/signal.h`

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

    cd /testbed && make -j$(nproc) && run_tests

Full rebuild if needed: cd /testbed && make distclean && ./tools/configure.sh -a ./apps sim:nsh && make olddefconfig && make -j$(nproc)

Once all target tests PASS in run_tests output, submit with this command ALONE:
    echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--instance",
        default="nuttx__nuttx-8885",
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

    # Support CLAUDE_API_KEY as an alias for ANTHROPIC_API_KEY (used in this repo's .env)
    if "CLAUDE_API_KEY" in os.environ and "ANTHROPIC_API_KEY" not in os.environ:
        os.environ["ANTHROPIC_API_KEY"] = os.environ["CLAUDE_API_KEY"]

    has_key = any(k in os.environ for k in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"
    ))
    if not has_key:
        sys.exit("ERROR: No API key found. Set OPENAI_API_KEY, ANTHROPIC_API_KEY / CLAUDE_API_KEY, or GEMINI_API_KEY.")

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
    # cwd=/testbed so every command lands in the kernel repo root.
    # timeout=360s to give make enough room (cold NuttX sim builds ~60-180s).
    env = DockerEnvironment(
        image=meta["docker_image"],
        cwd="/testbed",
        timeout=360,
    )

    AgentClass = make_verbose_agent_class(DefaultAgent) if args.verbose else DefaultAgent

    # Thinking models (Gemini 2.5, o1, o3) consume tokens on internal reasoning
    # Thinking models (Gemini 2.5, o1, o3) consume tokens on internal reasoning
    # before producing output. A low max_tokens causes them to exhaust the budget
    # while thinking and return nothing. 32 000 is safe for all models.
    # cost_tracking=ignore_errors prevents a RuntimeError crash when LiteLLM
    # doesn't have cost data for a model (common with Gemini preview versions).
    model = get_model(
        input_model_name=args.model,
        config={
            "model_kwargs": {"max_tokens": 32000},
            "cost_tracking": "ignore_errors",
        },
    )

    agent = AgentClass(
        model,
        env,
        system_template=SYSTEM_TEMPLATE,
        instance_template=INSTANCE_TEMPLATE,
        step_limit=args.step_limit,
        cost_limit=args.cost_limit,
        output_path=output_dir / f"{meta['instance_id']}.trajectory.json",
    )

    print("Starting agent...")
    print("=" * 60)
    try:
        result = agent.run(task)
    except (IndexError, KeyError, Exception) as e:
        print(f"\n[HARNESS ERROR] Agent crashed: {type(e).__name__}: {e}")
        result = {"exit_status": "HarnessError"}
    print("=" * 60)
    print(f"Exit status : {result.get('exit_status', 'unknown')}")

    # Capture git diff limited to only the kernel files the fix should touch.
    # The test patch is pre-applied in the Dockerfile, so a plain `git diff`
    # would also include test file changes. We scope to fix files only.
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
