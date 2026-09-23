# Apache Mynewt Support Design

## Objective

Add Apache Mynewt as the fourth supported RTOS in EmbedEval alongside Zephyr,
NuttX, and RIOT. Port the previously validated Mynewt implementation into the
current unified build, validation, agent-run, and patch-evaluation commands.

The branch will include three benchmark tasks:

- `mynewt__mynewt-2809`
- `mynewt__mynewt-3299`
- `mynewt__mynewt-3680`

The integration will preserve existing behavior for Zephyr, NuttX, and RIOT.
Generated model outputs, patches, trajectories, and validation logs are outside
the branch scope.

## Existing validated behavior

The source implementation in `../.EmbedEval-old` already demonstrated all
three Mynewt tasks in Docker using Mynewt's native BSP and simulator compiler:

- PR #2809: baseline 11/11 pass, before 11 pass and 1 fail, after 12/12 pass.
- PR #3299: baseline 29/29 pass, before 30 pass and 3 fail, after 33/33 pass.
- PR #3680: baseline 2/2 pass, before 2 pass and 1 fail, after 3/3 pass.

This work ports that implementation to the current repository architecture.
The port must be validated again because the shared build and harness code has
changed.

## Architecture

Mynewt will use the same public commands as the existing projects:

```bash
python scripts/build_bases.py --repo mynewt
python scripts/build_instances.py --repo mynewt
python scripts/validate_instance.py --repo mynewt --verbose
python harness/run.py --repo mynewt --model <model>
python harness/evaluate_patches.py --repo mynewt --model <model>
```

Project-specific behavior will remain in the existing configuration boundaries:

- `scripts/build_config.py` describes the Mynewt base image, build context,
  Docker platform, build arguments, and cleanup behavior.
- `harness/projects.py` describes the agent's Mynewt orientation, protected
  test paths, rebuild behavior, and Docker platform.
- Mynewt's native test runner owns Newt invocation, direct ELF execution, exact
  testcase parsing, and structured result generation.
- The shared scripts continue to own selection, orchestration, output paths,
  agent execution, and fresh-container evaluation.

## Mynewt Docker environment

Add `docker/bases/mynewt.Dockerfile` based on the previously validated image.
It will install the host compiler dependencies needed by `compiler/sim`, build
the pinned Newt tool revision, and install the shared Mynewt setup and test
runner scripts.

The Docker base build needs repository-root context so the Dockerfile can copy
files from `docker/shared/`. `scripts/build_config.py` will carry that context
as Mynewt-specific configuration, and `scripts/build_bases.py` will honor the
configured context. Existing projects retain their current contexts.

`docker/shared/setup_mynewt.py` will create an external Newt project under
`/project` without modifying the checked-out upstream repository. The target
will use:

```text
@apache-mynewt-core/hw/bsp/native
@apache-mynewt-core/compiler/sim
```

The setup will retain only runtime-safe test metadata inside the agent image so
the solution commits and upstream fix remain hidden.

`docker/shared/mynewt_runner.py` will preserve the validated behavior:

1. Delete `/project/bin` before every build.
2. Run `newt test` for the instance's pinned selftest package.
3. Locate exactly one generated ELF.
4. Execute the ELF from a clean temporary runtime directory.
5. Parse every `[pass]` and `[FAIL]` testcase line.
6. Reject missing tests, unexpected tests, abnormal process exits, incomplete
   builds, and timeouts.
7. Write structured results to `/tmp/mynewt-result.json`.
8. Return 0 for a complete passing suite, 1 for a complete suite with test
   failures, 2 for timeout, and 3 for invalid or incomplete execution.

## Instance definitions

Each instance directory will follow the current required four-file layout:

```text
docker/instances/mynewt__mynewt-<PR>/
    Dockerfile
    metadata.json
    run_tests.sh
    test_patch.diff
```

The Dockerfile will:

1. Start from the shared Mynewt base image.
2. Clone `apache/mynewt-core` at the pre-fix commit.
3. Apply only `test_patch.diff`.
4. Copy the instance metadata into the image.
5. Run the external-project setup.
6. Install the instance's `run_tests.sh` command.

The task metadata will preserve the validated fields from the prior
implementation:

- repository, issue, and PR URLs
- base and fix commits
- test package path
- `fail_to_pass`, `pass_to_pass`, and `baseline_tests`
- production files changed by the fix
- Docker platform
- compatibility compiler flags
- complete problem statement and scope notes

PR #2809 retains `-Wno-error=stringop-overflow`, applied consistently to the
container configuration, because it bypasses an unrelated GCC 13 warning in
the native socket code.

PR #3680 retains both requirements from the validated task statement: safe
termination of bounded decoded strings and the `JSON_ATTR_MAX` attribute-name
boundary behavior covered by the merged PR tests.

The old stored `fix_patch.diff` files will not be copied. The current unified
validator generates the upstream production patch from the recorded base and
fix commits, restricted to `files_changed_by_fix`.

## Unified project registration

Add `mynewt` to the supported project lists in the build and harness layers.
The new instance names follow the current `<project>__<project>-<PR>` rule, so
the existing instance parser remains unchanged.

The Mynewt project configuration will specify:

- label: Apache Mynewt
- platform: `linux/amd64`
- no QEMU cleanup
- no separate incremental rebuild command because `run_tests` builds and runs
  the selected native selftest
- protected paths covering the three selftest directories
- orientation explaining `/testbed`, `/project`, Newt, the native BSP, and the
  requirement to use `run_tests`

## Validation and evaluation

The shared validator retains its current before-fix and after-fix phases. No
new baseline phase or behavior change will be added for existing projects.

For Mynewt, the before-fix phase will additionally read
`/tmp/mynewt-result.json` and require:

- a successful build and native test execution
- exactly the metadata `fail_to_pass` set in `failed`
- exactly the metadata `pass_to_pass` set in `passed`
- no missing or unexpected testcases

The after-fix phase will require every `fail_to_pass` and `pass_to_pass`
testcase to pass, with no missing, failed, or unexpected testcases. This
preserves the old implementation's exact-test protection without adding a new
validation phase or changing other projects.

Fresh-container patch evaluation will continue through
`harness/evaluate_patches.py`. The Mynewt `run_tests` command itself rejects an
incomplete test inventory, so a zero exit status means all expected named tests
ran and passed.

## Documentation

Update the main README to:

- describe EmbedEval as supporting four RTOS projects
- update the instance count from 17 to 20
- list Mynewt in the project-selection examples
- document its native simulator rather than describing it as QEMU
- provide the build, validation, model-run, and evaluation commands

Add focused Mynewt notes only where needed to explain the native target,
compiler compatibility flag, and exact testcase reporting.

## Verification

The implementation is complete when:

1. Project discovery reports all three Mynewt instances using `--repo mynewt`.
2. Dry-run base and instance builds resolve the correct images, contexts,
   platforms, and build arguments.
3. The Mynewt base image builds.
4. All three instance images build.
5. `scripts/validate_instance.py --repo mynewt --verbose` validates all three
   tasks with the exact expected before and after testcase inventories.
6. Existing project discovery and dry-run commands continue to work unchanged.
7. `harness/run.py` accepts each Mynewt instance through `--dry-run` without an
   API call.
8. If a working provider API key is available, one model is run against
   `mynewt__mynewt-3680` and its patch is graded through the unified
   fresh-container evaluator.
9. No generated model outputs, patches, trajectories, or validation logs are
   added to Git.

The model and provider are selected at run time based on the credentials that
are available. A missing API key does not invalidate the Docker, task, or
unified harness integration; it only prevents the optional live demonstration
run. Generated model artifacts remain local and uncommitted.

## Excluded work

- Committing generated outputs or validation logs
- Committing API keys or other credentials
- Adding new benchmark candidates
- Changing PR filtering
- Adding validation phases to existing RTOS projects
- Refactoring unrelated shared code
- Removing or modifying existing Zephyr, NuttX, or RIOT instances
