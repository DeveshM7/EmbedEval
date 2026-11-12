"""
Run an agent on EmbedEval instances.

    python harness/run.py --instance zephyr__zephyr-65697 --model gpt-5.4
    python harness/run.py --repo nuttx --model gpt-5.4
    python harness/run.py --all --model gpt-5.4 gemini-2.5-pro

Twister-pipeline Zephyr instances (generated/, built as Apptainer images) run in
an Apptainer environment instead of Docker, e.g. with the local Qwen server:

    EMBEDEVAL_INSTANCES_DIR=generated python harness/run.py \
        --instance zephyr__zephyr-65697 --model qwen38-nvfp4

Each run goes to its own folder, <eval dir>/<model>/<project>/<PR>/ (see
harness/paths.py): patch.diff, trajectory.json, run.json (how the run ended),
and grade.json, written as soon as the run ends. As on SWE-bench, only a run
the agent submitted is tested; any other ending is a fail. A pair with a
grade.json is skipped, so the same command picks up where a stopped job left
off. An infrastructure error is retried once (harness/run.py, run_and_grade).

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
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hidden_tests
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
- After a successful build you MUST still run `run_tests` before submitting.
  A passing build does NOT mean tests pass.
"""

INSTANCE_TEMPLATE = """\
Please solve this task:

{{{{task}}}}

## Important Rules

1. Every response must contain exactly one `bash` tool call.
2. Do NOT modify any files under {protected}: changes there are discarded
   before your work is graded.
3. Environment variable and directory changes are NOT persistent between
   commands -- every action runs in a new subshell. Use absolute paths or
   prefix commands with `cd /testbed &&`.
4. There is no network access. Nothing can be downloaded (no pip install,
   git fetch, package downloads); everything the build needs is already here.
5. The git history holds only the starting point of this task. Use
   `git status` and `git diff` to see your own changes.
6. Your work is graded only if you submit it (step 4 below).

## Running Tests

Use the `run_tests` command to build and run the tests. It handles all setup and
teardown, waits for results, and exits as soon as pass/fail is known. A test
that hangs is stopped and reported as failed.
{extra_warnings}
Mandatory workflow:
1. Explore the code and understand the task
2. Edit the source file(s) to make the change
3. Build and test: `{build_and_test}`
   - If the build fails: fix the error and repeat
   - If run_tests shows tests FAILING: fix your change and go back to step 3
   - If run_tests shows all target tests PASSING: go to step 4
4. Submit by calling the `bash` tool with exactly this command:

       echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT

   Do NOT chain it with && or any other command.

Other useful commands:
- Find code:   grep -rn "name" --include="*.c" --include="*.h" /testbed
{tags_line}
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
# EMBEDEVAL_LOCAL_API_BASE overrides the address, and EMBEDEVAL_LOCAL_API_KEY the key,
# for a server on another node. By default the server listens on 127.0.0.1, so the
# harness must run on its node.
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
                  api_key=os.environ.get("EMBEDEVAL_LOCAL_API_KEY", "local"),
                  timeout=REQUEST_TIMEOUT, num_retries=0,
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
    hide = hidden_tests.ENABLED
    if hide:      # harness/hidden_tests.py: the PR's tests are not given
        project = hidden_tests.project_of(meta)
        cfg = {**cfg, "orientation": hidden_tests.ORIENTATION[project],
               "extra_warnings": hidden_tests.WARNINGS[project]}
    protected = " or ".join(f"`{p}`" for p in cfg["protected_paths"])
    warn = cfg["extra_warnings"]
    system = SYSTEM_TEMPLATE.format(
        label=cfg["label"], orientation=cfg["orientation"]
    )
    # Only images built with a ctags index have /testbed/tags (Zephyr's twister
    # pipeline); RIOT's do not.
    tags = ('- ctags index: grep "function_name" /testbed/tags\n'
            if cfg.get("ctags") else "")
    instance = INSTANCE_TEMPLATE.format(
        protected=protected,
        extra_warnings=f"\n{warn}\n" if warn else "",
        build_and_test=_build_and_test(meta),
        tags_line=tags,
    )
    if hide:
        system = _swap(system, _SYSTEM_RULE, hidden_tests.SYSTEM_RULE)
        instance = _swap(instance, _PROTECTED_RULE.format(protected=protected),
                         hidden_tests.PROTECTED_RULE.format(protected=protected))
        instance = _swap(instance, _RUNNING_TESTS, hidden_tests.RUNNING_TESTS[project])
        instance = _swap(instance, _WORKFLOW_STEP3.format(build_and_test=_build_and_test(meta)),
                         hidden_tests.WORKFLOW)
    return system, instance


# The parts of the prompt that assume the PR's tests are there; replaced when
# they are hidden.
_SYSTEM_RULE = """\
- After a successful build you MUST still run `run_tests` before submitting.
  A passing build does NOT mean tests pass.
"""
_PROTECTED_RULE = """\
Do NOT modify any files under {protected}: changes there are discarded
   before your work is graded."""
_RUNNING_TESTS = """\
Use the `run_tests` command to build and run the tests. It handles all setup and
teardown, waits for results, and exits as soon as pass/fail is known. A test
that hangs is stopped and reported as failed."""
_WORKFLOW_STEP3 = """\
3. Build and test: `{build_and_test}`
   - If the build fails: fix the error and repeat
   - If run_tests shows tests FAILING: fix your change and go back to step 3
   - If run_tests shows all target tests PASSING: go to step 4
"""


def _swap(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"prompt: expected one copy of {old[:60]!r}")
    return text.replace(old, new)


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
    """The first user message: the problem plus its test context.

    Not the files the PR's fix changed: SWE-bench does not give them, and
    knowing where to change the code is part of the task."""
    def bullets(items):
        return "\n".join(f"  - {i}" for i in items)

    if hidden_tests.ENABLED:      # the problem statement only, as in SWE-bench
        return f"## Task\n\n{meta['problem_statement']}\n"
    task = f"""\
## Task

{meta["problem_statement"]}

## Failing Tests (must pass after your change)

{bullets(meta.get("fail_to_pass", [])) or "  (none)"}
"""
    if meta.get("pass_to_pass"):
        task += f"""
## Tests That Must Continue Passing

{bullets(meta["pass_to_pass"])}
"""
    return task


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


def make_environment(meta: dict):
    cfg = projects.config_for(meta)
    if cfg.get("runtime") == "apptainer":
        # Zephyr: an Apptainer image built by scripts/build_instances.py.
        sys.path.insert(0, str(paths.REPO_ROOT / "scripts"))
        import apptainer_util
        from apptainer_env import ApptainerEnvironment

        # The image carries the PR's test changes as uncommitted edits.
        # Committing them in this run's overlay before the agent starts makes
        # `git diff` show the agent's changes alone, so capture_patch's check
        # for edits to protected paths means what it says. Graded patches still
        # apply to the image as built: the same files, the same content.
        commit_tests = ("cd /testbed && git add -A -- tests/ && git -c user.name=EmbedEval "
                        "-c user.email=embedeval@localhost commit -q -m "
                        "'The PR test changes, without its fix'")
        # No history beyond the starting point, or the agent could read the fix
        # in later commits: checked before the tests commit above adds one. A
        # failure stops the run as an infrastructure error, never a silent leak.
        one_commit = ('cd /testbed && n=$(git rev-list --all | wc -l) && [ "$n" = 1 ] '
                      '|| { echo "git history has $n commits, expected 1"; exit 1; }')
        import registry   # the validated image: pulled from the registry if published
        image, layer = apptainer_util.instance_container(
            meta, registry.image_for(meta["instance_id"]))
        if meta.get("pipeline") == "twister":
            # Zephyr's image holds the whole Zephyr history; its git layer hides
            # it behind a one-commit .git (scripts/zephyr_gitlayer.py).
            import zephyr_gitlayer
            layer = zephyr_gitlayer.pull(meta["instance_id"])
        return ApptainerEnvironment(image=image, layer=layer,
                                    name=meta["instance_id"], cwd="/testbed",
                                    timeout=cfg.get("command_timeout", 900),
                                    setup_commands=[one_commit, hidden_tests.setup_command(meta)]
                                    if hidden_tests.ENABLED else [one_commit, commit_tests])

    # NuttX, RIOT: Docker.
    kwargs = dict(image=meta["docker_image"], cwd="/testbed", timeout=180)
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


# The run in progress, for the SIGTERM handler:
# {"save": callable, "env": env, "record": callable writing run.json}.
_CURRENT: dict = {}

# An infrastructure error (our side, not the model's) is retried once: two
# attempts in all, counted across jobs through run.json. After that the run is
# recorded as "not_run", not as a fail.
MAX_ATTEMPTS = 2

# How a run may end that is the model's doing, not ours: graded as a fail.
# Submitted is the only ending that is tested (evaluate_patches.grade_run).
MODEL_ENDINGS = {"Submitted", "LimitsExceeded", "TimeExceeded", "RepeatedFormatError",
                 "ContextWindowExceededError"}


def _context_overflow(e: Exception) -> bool:
    """The conversation outgrew the model's context: the model's doing, a fail,
    as on SWE-bench. LiteLLM usually raises ContextWindowExceededError; a bare
    400 from vLLM that says so counts too."""
    if type(e).__name__ == "ContextWindowExceededError":
        return True
    text = str(e).lower()
    return type(e).__name__ == "BadRequestError" and (
        "maximum context length" in text or "context window" in text)


def _stop_on_sigterm(signum, frame):
    """`timeout`, scancel and Slurm (a job at its time limit) stop a run with
    SIGTERM, then SIGKILL a little later. Save what the agent did, record the
    run as stopped -- an infrastructure error, so a later job retries it -- and
    remove the overlay right here, then exit. Raising an exception instead is
    not enough: SIGTERM usually lands while the agent waits for the model, and
    the model client's retry wrapper catches the exception and retries the call."""
    print(f"\n  stopped by signal {signum}: saving the patch", flush=True)
    try:
        if _CURRENT.get("save"):
            _CURRENT["save"]()
            print("  patch saved", flush=True)
    except BaseException as e:
        print(f"  could not save the patch: {type(e).__name__}: {e}", flush=True)
    try:
        if _CURRENT.get("record"):
            _CURRENT["record"](f"stopped by signal {signum}")
    except BaseException as e:
        print(f"  could not write run.json: {type(e).__name__}: {e}", flush=True)
    try:
        if _CURRENT.get("env") is not None:
            _CURRENT["env"].cleanup()
    except BaseException:
        pass
    os._exit(128 + signum)


def run_one(instance_id: str, model_name: str, args, attempt: int = 1) -> dict:
    """One attempt at one (instance, model) run. Writes patch.diff,
    trajectory.json and run.json to the run's folder and returns run.json.

    run.json says how the run ended. `exit_status` is the agent's ending
    (Submitted, LimitsExceeded, TimeExceeded, RepeatedFormatError,
    ContextWindowExceededError): the model's doing, graded. `infra_error` is set
    instead when our side failed -- the container or the model server, or the
    job stopping the run -- and the run is retried, not graded."""
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
    print(f"  attempt  : {attempt} of {MAX_ATTEMPTS}")

    record = {
        "instance_id": instance_id,
        "model": full_model,
        "attempt": attempt,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "node": socket.gethostname(),
        "limits": {"steps": args.step_limit, "minutes": args.time_limit,
                   "command_seconds": projects.config_for(meta).get("command_timeout", 900)},
        "exit_status": None,
        "steps": None,
        "elapsed_seconds": 0.0,
        "hit_time_cap": False,
        "hit_step_limit": False,
        "infra_error": None,
        "error": None,
        "patch_empty": True,
        "protected_touched": [],
        "tests_hidden": hidden_tests.ENABLED,
    }
    start = time.time()
    env, agent, captured, base = None, None, False, "HEAD"

    def save_patch() -> None:
        patch, touched = capture_patch(env, meta, base)
        paths.patch_path(instance_id, full_model).write_text(patch)
        record["patch_empty"] = not patch.strip()
        record["protected_touched"] = touched

    def write_record(infra_error: str | None = None) -> None:
        if infra_error:
            record["infra_error"] = infra_error
        record["elapsed_seconds"] = round(time.time() - start, 1)
        if agent is not None:
            record["steps"] = agent.n_calls
        paths.write_json(paths.run_record_path(instance_id, full_model), record)

    try:
        _CURRENT.update(record=write_record)
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
        record["exit_status"] = run_info.get("exit_status")
        save_patch()
        captured = True
    except KeyboardInterrupt:
        raise
    except Exception as e:
        record["error"] = f"{type(e).__name__}: {e}"
        if _context_overflow(e):
            record["exit_status"] = "ContextWindowExceededError"
        else:
            record["infra_error"] = record["error"][:500]
        print(f"  ERROR: {record['error'][:300]}")
    finally:
        _CURRENT.clear()
        if env is not None and not captured:
            # Stopped early: keep whatever the agent had done, for reading.
            try:
                save_patch()
            except BaseException as e:
                print(f"  could not save the patch: {type(e).__name__}: {e}")
        if env is not None:
            try:
                env.cleanup()
            except Exception:
                pass
    if record["exit_status"] is not None and record["exit_status"] not in MODEL_ENDINGS:
        # An ending this list does not know (mini-swe-agent's own exception
        # names, say): ours to look at, not the model's fail.
        record["infra_error"] = record["infra_error"] or f"unexpected ending {record['exit_status']}"
    record["hit_time_cap"] = record["exit_status"] == "TimeExceeded"
    record["hit_step_limit"] = (record["exit_status"] == "LimitsExceeded"
                                and agent is not None and agent.n_calls >= args.step_limit)
    write_record()
    if record["protected_touched"]:
        print(f"  WARNING: agent modified protected paths: {', '.join(record['protected_touched'])}")
    if record["patch_empty"] and not record["infra_error"]:
        print("  WARNING: agent made no changes (empty diff)")
    print(f"  ended    : {record['infra_error'] and 'INFRA: ' + record['infra_error'][:200] or record['exit_status']}"
          f" after {record['steps']} steps, {record['elapsed_seconds']:.0f} s")
    return record


def run_and_grade(instance_id: str, model_name: str, args) -> dict:
    """Run one (instance, model) pair to the end and grade it; return grade.json.

    A pair with a grade.json is finished and skipped (--force runs it again).
    A run that ended in an infrastructure error is tried again, up to
    MAX_ATTEMPTS attempts in all, also across jobs: the count is kept in run.json.
    After that the pair is recorded as "not_run"."""
    import evaluate_patches

    full_model = paths.resolve_model(model_name)
    grade_file = paths.grade_path(instance_id, full_model)
    rec_file = paths.run_record_path(instance_id, full_model)
    if grade_file.exists() and not args.force:
        print("  already graded: skipped")
        return json.loads(grade_file.read_text())
    prev = json.loads(rec_file.read_text()) if rec_file.exists() and not args.force else None
    if prev and not prev.get("infra_error"):
        print("  run finished earlier, not graded yet: grading")
        return evaluate_patches.grade_run(instance_id, full_model)
    attempt = prev["attempt"] if prev else 0
    record = prev
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        record = run_one(instance_id, full_model, args, attempt)
        if not record["infra_error"]:
            print("  grading ...")
            return evaluate_patches.grade_run(instance_id, full_model)
    grade = {"instance_id": instance_id, "model": full_model,
             "graded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "grade": "not_run", "tests_run": False,
             "error": f"infrastructure error in {attempt} attempts: {record['infra_error']}"}
    paths.write_json(grade_file, grade)
    print(f"  NOT RUN: {grade['error'][:300]}")
    return grade


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
    p = argparse.ArgumentParser(description="Run an agent on EmbedEval instances and grade each run.")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--instance", nargs="+", help="one or more instance ids")
    sel.add_argument("--repo", choices=paths.PROJECTS, help="every instance for one project")
    sel.add_argument("--all", action="store_true", help="every instance, all projects")
    p.add_argument("--model", nargs="+", required=True,
                   help="one or more models (full LiteLLM name or shorthand)")
    # The limits: 250 steps as in mini-swe-agent's SWE-bench config, a 1-hour cap
    # per run (a job lasts 4 hours here), 900 s per command (projects.py). The
    # agent is not told them, as on SWE-bench.
    p.add_argument("--step-limit", type=int, default=250)
    p.add_argument("--cost-limit", type=float, default=3.0,
                   help="dollars per run, for hosted models (local models cost nothing)")
    p.add_argument("--time-limit", type=float, default=60,
                   help="minutes of wall-clock time per run, checked before each model "
                        "call (default 60; 0: no limit)")
    p.add_argument("--force", action="store_true",
                   help="run again pairs that already have a run or a grade")
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
    print(f"Limits    : {args.step_limit} steps, {args.time_limit:g} min per run")
    print(f"Output    : {paths.OUTPUTS_DIR}\n")

    if args.dry_run:
        for m in models:
            for i in instances:
                done = " (graded)" if paths.grade_path(i, m).exists() else ""
                print(f"  {m:46s} {i:24s} -> {paths.show(paths.run_dir(i, m))}{done}")
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
            results.append(run_and_grade(instance_id, model_name, args))

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    for g in results:
        icon = {"pass": "PASS", "not_run": "----"}.get(g["grade"], "FAIL")
        err = f"  ({g['error'][:80]})" if g.get("error") else ""
        print(f"  [{icon}] {g['model']:34s} {g['instance_id']:24s}{err}")
    graded = [g for g in results if g["grade"] != "not_run"]
    passed = sum(1 for g in graded if g["grade"] == "pass")
    print(f"\n  {passed}/{len(graded)} pass"
          + (f"   ({len(results) - len(graded)} not run)" if len(graded) < len(results) else ""))


if __name__ == "__main__":
    main()
