"""
Run mini-swe-agent on a single EmbedBench-Pro RIOT instance.
Usage:
    python harness/run_riot_instance.py
    python harness/run_riot_instance.py --instance riot__riot-5323 --model anthropic/claude-sonnet-4-6
"""

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


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
You are working inside a RIOT OS repository.

You MUST use the `bash` function/tool provided to you to execute commands.
Include your reasoning in the content/thought of your response, and then call the `bash` tool.

CRITICAL RULES — responses that break these are rejected:
- Every action must be executed by calling the `bash` tool.
- Do NOT output commands in markdown ````bash```` blocks; you must use the `bash` tool.
- DO NOT provide text-only responses. If you are finished, you MUST call the `bash` tool with the submission command.
- NEVER use heredoc syntax (<<'EOF' or <<'PY'). It breaks inside docker exec.
  Use python3 -c "..." with a one-liner instead.
- After a successful build you MUST still run `run_tests` before submitting.
  A passing build does NOT mean tests pass.
- If you receive an error about missing tool calls, just call the `bash` tool.
"""

INSTANCE_TEMPLATE = """\
Please solve this issue:

{{task}}

## Important Rules

1. Every action must be a `bash` tool call. Do not use markdown blocks.
2. Do NOT modify any files under tests/.
3. Environment variable changes and directory changes are NOT persistent between
   commands — every action runs in a new subshell. Use absolute paths or prefix
   commands with `cd /testbed &&`.

## Running Tests

Use the `run_tests` command to compile and run tests. It handles all necessary flags,
waits for results, and exits immediately once pass/fail is known:

    run_tests

Exit codes: 0 = all tests passed, 1 = tests failed, 2 = timed out.

If `run_tests` times out (exit code 2) with no test results, your fix has
introduced an infinite loop or severe compiler error — do NOT submit. Investigate the bug further.

Mandatory workflow:
1. Explore the code and understand the bug
2. Edit the source file to fix the bug
3. Build and test: `run_tests`
   - If build fails: fix the error and repeat
   - If run_tests shows tests FAILING: fix the bug and go back to step 3
   - If run_tests shows all target tests PASSING: go to step 4
4. Submit by calling the `bash` tool with the following command:

echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT

CRITICAL: Do NOT chain the submission command with any other command.

Other useful commands:
- Find code: `grep -rn "name" --include="*.c" --include="*.h" /testbed`
- ctags index: `grep "function_name" /testbed/tags`

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
    fail_tests = "\n".join(f"  - {t}" for t in meta.get("fail_to_pass", []))
    pass_tests = "\n".join(f"  - {t}" for t in meta.get("pass_to_pass", []))
    fix_files = "\n".join(f"  - {f}" for f in meta.get("files_changed_by_fix", []))

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

    run_tests

Once all target tests PASS in run_tests output, submit with this command ALONE:
    echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--instance",
        default="riot__riot-5323",
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

    # If users utilize a .env file, load it
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if "ANTHROPIC_API_KEY" not in os.environ and "OPENAI_API_KEY" not in os.environ and "GEMINI_API_KEY" not in os.environ:
        sys.exit("ERROR: No API key found. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY.")

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
    env = DockerEnvironment(
        image=meta["docker_image"],
        cwd="/testbed",
        timeout=180,
    )

    AgentClass = make_verbose_agent_class(DefaultAgent) if args.verbose else DefaultAgent

    # DefaultAgent takes system_template and instance_template as required kwargs.
    # step_limit and cost_limit are optional AgentConfig fields.
    model = get_model(input_model_name=args.model)
    
    # Customize the format error message to aggressively guide the model back on track.
    model.config.format_error_template = (
        "ERROR: No tool call found in your response. "
        "You MUST NOT apologize or explain yourself. You must execute actions using the `bash` tool. "
        "If you are finished, you MUST call the `bash` tool with the command: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
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
    # The user asked to initially run the PR tests to validate the riot PRs first
    # This executes `run_tests` once at the beginning of the trajectory automatically 
    # to show the LLM unequivocally that it is starting from a broken state!
    initial_test_output = env.execute({"command": "run_tests"}).get("output", "Test failed to run")
    initial_context = f"I immediately ran `run_tests` for you so you can observe the baseline failure state:\n\n{initial_test_output}\n\nWhat is your first plan of action?"
    task = task + f"\n\n{initial_context}"

    result = agent.run(task)
    print("=" * 60)
    print(f"Exit status : {result.get('exit_status', 'unknown')}")

    # Capture git diff limited to only the files the fix should touch.
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
