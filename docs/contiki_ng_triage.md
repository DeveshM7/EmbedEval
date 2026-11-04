# Contiki-NG triage contract

Status: integrated adapter undergoing bounded Scholar pilot verification.
A full campaign requires the remaining profile/era controls below. Supported
selections pass the inventory checker before generation and named-case validation.
Detailed research and logs live in `triage/runs/contiki-research/` (ignored).

## Run contract

- Repository: https://github.com/contiki-ng/contiki-ng, default branch `develop`.
  Research pin: `c02eedae8b187123069891f81a7111b833701f9c` (2026-10-25).
  This is Contiki-NG, not the legacy Contiki repository.
- Work only on `contiki-ng-port` in the EmbedEval-contiki worktree. Starting
  EmbedEval revision: `52744b392e3f9bf212938076f69ad906b7b055f3`.
  Origin refreshed before comparison; checkout was clean and ten commits ahead
  of origin/main. Reuse generic observe, after-fix, profile and compile-oracle
  infrastructure; do not transplant OpenThread discovery or CMake assumptions.
- Goal: maximum valid-instance yield. Validation is the gate; do not tune triage
  accuracy. Use upstream tests only, never authored regression substitutes.
- Runtime and semantic compile oracles are allowed. A compile failure qualifies
  only when the PR supplies the very interface/build fix that its tests need;
  incidental incompatibilities do not qualify. Attributable configuration/dependency-only
  fixes are also allowed (user decision, 2026-11-03).
- Research history: official merged Contiki-NG PRs through the research pin,
  including merges outside the `develop` first-parent chain and archived merge
  objects no longer reachable from `develop`. Verify each PR against GitHub
  metadata; imported legacy Contiki PR numbers must not be assigned NG PR URLs.
  Full NG history is the confirmed campaign window (user decision, 2026-11-03).
- Execution: Scholar, Apptainer, workspace
  `/scratch/scholar/zhan5136/embedeval-contiki`. Do not access the OpenThread
  workspace or `~/EmbedEval`. Shared generic images may be read without mutation.
  One Contiki job at a time while OpenThread owns the other slot; total limit two.
  Spark jobs require `--gres=gpu:1`, `--export=NONE`, a clean `env -i`, and
  `/usr/sbin` in PATH. Maximum job wall time four hours.
- Triage: Scholar vLLM profile `qwen38-nvfp4` from `triage/models.json`.
  No paid API calls without asking. No push, publication or promotion without
  asking. Commit coherent steps as `zhan5136 <zhan5136@purdue.edu>`.
- No pool-wide observation, triage, or finalization before the user has reviewed
  research and the readiness gate passes. Bounded research controls are allowed.

## Repository, configuration and test model

Modern production is under `os/` and `arch/`, with application code also in
`examples/`; neither examples nor tools are universally non-production.
Before PR #63 (September 2017), production uses `core/`, `apps/`, and `drivers/`,
and tests use `regression-tests/`. Inspect the candidate's own revisions.

The build is GNU Make. `Makefile.include` finds `project-conf.h` and defines
`PROJECT_CONF_PATH`. `os/contiki.h` includes platform `contiki-conf.h` and then
`contiki-default-conf.h`; the native platform includes project configuration
before applying platform values. `DEFINES`, `MODULES`, `MAKE_NET`, `MAKE_MAC`,
`WITH_*`, target makefiles and derived macros also affect the compiled program.
Never decide test eligibility by macro names alone.

`tests/Makefile.compile-test` interprets colon-separated `EXAMPLES` as
application/target, optional runner (`RUN_FILE=1`), then Make assignments. It
builds with `WERROR=1`; `CLANG_WARNINGS=1` adds a Clang pass. Keep assignments in
the profile identity, especially both HEAPMEM_DEBUG and IP64_CONF_DHCP variants.
`tests/Makefile.simulation-test` discovers `??-*.csc`, runs Cooja with fixed
BASESEED/RUNCOUNT, and uses scenario scripts as assertions. A `.csc` carries its
own mote types, source/firmware paths and build commands. Resolve those instead
of assuming every simulation uses the same architecture.

Native unit cases use `UNIT_TEST_REGISTER`, `UNIT_TEST_RUN`, and
`UNIT_TEST_PASSED`. `os/services/unit-test/unit-test.c` reports description,
result, source exit point and assertion count. Registration alone does not prove
execution. Source inventory at the pin finds 34 C files with 391 registrations;
these are not 391 independently runnable or validated cases. Map descriptions
to symbols, application Makefiles, invoked cases and profile guards.

Shared consumers include `tests/utils.sh`, `08-native-runs/run-one.sh`,
`Makefile.compile-test`, `Makefile.simulation-test`, and the production unit-test
module. Helper-only changes need a concrete affected consumer, not an invented
standalone executable. Script-base LC/memb programs report their own assertions.
Cooja units are scenarios, not gtest cases.

Use the compiler's actual preprocessing command (upstream `build/<target>/obj/*.e`
rule) to resolve bodies. Preserve include provenance and macro dumps, and mark
unresolved expressions unknown. Research verified that IP64_CONF_DHCP=0 removes
`IP64_TAP_MODE dhcp` and keeps `static`, while =1 reverses them. Compiler line/definition evidence is recorded by the research resolver. Guarded
native selections remain holds until their own profile has compiler-backed proof.
The TAP runtime agreement control was blocked by device access permissions.

## CI profiles and worker status

Source: `.github/workflows/build.yml`, each `tests/??-*/Makefile`, and shared
runners at the pin. Linux executes `cimake -C tests/??-${test}` and then
`tests/check-test.sh`; cimake sets RELSTR and CFLAGS_DATE. macOS uses make after
installing its own dependencies. The matrix has 17 suites across Linux/macOS,
with seven macOS exclusions (27 combinations). MacOS is out of worker scope.

| CI suite | Execution model | Research status on Scholar |
| --- | --- | --- |
| documentation | Doxygen/Sphinx | Not a runtime profile; oracle scope pending |
| compile-base | example/tool target matrix | Compile profile; toolchain controls pending |
| compile-arm-ports | board matrix and out-of-tree builds | Cross-toolchain controls pending |
| renode-simulation | upstream Renode scripts | Uncontrolled; upstream image installs amd64 Renode |
| script-base | native LC switch/address-labels, memb, visualization | Full current suite completed on x86 control, including visualization Cooja scenario; historical versions untested |
| simulation-base | Cooja scenario matrix | Native-mote ringbufindex and Sky hello-world passed on current x86 control; remaining scenarios/eras untested |
| native-runs | native applications and shell runners | Current pure-native subset: 20 runner logs, 158 named cases passed; network/service cases untested |
| ipv6 | Cooja scenarios | Current single native-mote scenario passed; older eras need controls |
| ipv6-nbr | native neighbor test | Current x86 control passed with named unit result; historical versions untested |
| ieee802154 | Cooja scenarios | Current PAN-ID native-mote scenario passed |
| rpl-lite | Cooja scenarios | Current route scenario passed on node-local storage; NFS clean fails with loaded libraries |
| rpl-classic | Cooja scenarios | Current route scenario passed on node-local storage; NFS clean fails with loaded libraries |
| tun-rpl-br | Cooja plus host TUN/router scripts | Pending simulator and network privilege fixture |
| coap-lwm2m | simulations/standalone native and queue mode | External submodule/services; fixture unverified |
| packet-parsing | native packet injector, four shell consumers | Current x86 suite completed; historical profile coverage bounded |
| security-protocols | Cooja and crypto dependencies | Current EDHOC method-0 scenario passed with pinned crypto trees |
| nat64-standalone | native application plus host echo fixture | Current x86 UDP echo round trip passed; historical versions untested |

Within native-runs, MQTT, ping, CoAP, SNMP and IP64 TAP scripts have network,
service or privilege requirements that the pure native units do not. Native
startup can warn that TUN is unavailable yet successfully execute pure units;
that does not establish support for network cases. Never blanket-exclude all
native tests for this warning, or treat it as a regression oracle.

Historical CI, scenario and Makefile inputs are recorded at all 341 test-touching
PR bases. Normalized CI blobs and extracted scenario build commands are research
inputs, not an executable historical profile selector. The current Dockerfile
uses Ubuntu 22.04 and hard-coded x86 binaries for Java, MSP430, ARM tools and
Renode. It cannot be assumed to run on Spark ARM64. Current Cooja submodule is
`fabff3ee41eeeb6f3de7bd11f7c9cdf0a7d7dfc4` with Java toolchain 25; historical
Ant/Gradle and submodule versions must be resolved per base. No whole-history
image/toolchain compatibility claim is established. A separate x86_64 control
used an Apptainer SIF converted from the current upstream Dockerfile image;
the local SIF file has SHA256
`8fb5b6fea745e0aba4c69a860c476f51549a164036a7a3f88c0ccb20e22702c6`
ran Java 25.0.3, GCC 11.4.0 and MSP430 GCC 4.7.4. The two current Cooja
scenarios passed, and a user-network-namespace probe exited zero; neither
establishes support for other simulator, network, or historical recipes.

Research native controls used an existing read-only Ubuntu 24.04 ARM64 image,
SHA256 `9f5a1ffa170250cd051e5adc54115917efd6a3780373c66c199f64efc977f287`,
GCC 13.3.0 and Make 4.3. This is a smoke environment, not a frozen campaign recipe.
Source archives omit `.git`, causing version-query warnings; the campaign must
preserve revision metadata or use the pinned upstream CI version variables.

Profile choice must use CI at the candidate base, supplemented by the upstream
PR's newly introduced test recipe where necessary. Select a real built consumer
and an enabled test body; mixed configurations need additional observations.
Unsupported profiles remain holds, not invalid PRs.

## PR boundaries, selection and separation

The official merged-PR census contains 1,843 PRs with recovered merge objects;
341 touch `tests/` or historical `regression-tests/`. Of these, 102 merges and
23 test-touching merges are outside the old first-parent census. Seven recovered
merge objects are no longer reachable from `develop`; preserve their GitHub
identity and verify archived base/fix availability before observation. The
first-parent chain separately contains 1,758 commits: 1,744 merges (1,743
numbered PRs plus one release merge) and 14 unassociated direct commits. Its
rename, deletion, and revert flags are mechanical, not hard-filter acceptance
counts. Across the full PR census, 24 PRs rename test paths, 26 delete them,
and 18 contain other official PR merges in their base..fix ancestry. Those 18
need rollup review and deduplication; nesting alone does not imply exclusion.

For ordinary merge PRs, base = merge first parent; fix = complete merge result.
Check the second-parent history and complete diff for merge-resolution changes.
Do not pick only the last topic commit or the GitHub current base branch tip.
Unassociated commits and ambiguous release rollups need explicit mapping/hold.
Reverts, renames and deletions do not inherently supply a detecting assertion.
If squash/rebase styles appear in a future window, map full PR commit ranges
before enabling them; never apply OpenThread's title regex as a universal rule.

Read the changed test setup/input/assertion against the old production path and
record a causal chain. Hold unclear attribution. A newly added vector-decoding helper or utility header
is an incidental compile dependency when the PR targets a separate algorithm
fix; its absence must not be mislabeled as the intended compile oracle. New APIs can supply semantic
compile oracles; symbols and diagnostic notes must identify the intended API.
Protect upstream tests/support and transport them identically to both states;
apply only the selected production patch to the after state. Do not copy gold
production into the before state, or expose it in the problem statement.

## Evidence, output and readiness contract

Always collect before/after build output, named case results, assertion/exit
locations, elapsed time, selected profile and recipe hash. Build and run after
when before does not compile. Unexecuted cases after a crash are unknown, not
failures; isolate where the upstream runner supports it. A timeout alone is not
an oracle. Keep baseline failures distinct from candidate detection.

Historical native runners can falsely pass without execution (fixed upstream in
PR #3267). Require fresh logs, positive completion markers and named execution.
Do not transplant that fix into old benchmark tests without separate fixture
analysis. Repeat the before state to check determinism.

The implemented decision schema uses `pr`, `verdict` (`accept`, `reject`,
`hold`), and `reason` for rejects/holds. Accepts require:

- `units`: nonempty list of exact affected `test_inventory` unit IDs.
- `profile`: optional `native`, `cooja`, or `script`; selected units must match.
- `fail_to_pass`: exact case IDs; native IDs are `unit_id::test_symbol`,
  while script/scenario IDs equal their unit ID.
- `pass_to_pass`: relevant unaffected case IDs, or an empty list.
- `oracle`: `runtime` (default), `compile`, or `config`.
- `problem_statement`, `causal_chain`, and `change_type`: nonempty strings.
- Compile oracles also require `compile_symbols`, drawn from the record's
  verified absent-at-base `compile_requirements`, plus `oracle_attribution`.
  Missing test utilities remain incidental unless they implement the PR purpose.
- Config oracles require exact `config_diagnostics` and `oracle_attribution`.
  An unavailable dependency without source attribution is an infrastructure hold.

Native accept selections require enabled-body evidence; unresolved guarded
bodies remain holds until compiler evidence establishes the selected profile.
Source-unconditional bodies are recorded explicitly. Observations can investigate
unknown bodies but do not turn missing execution into failure. Records carry an
enrichment version and required fields; stale records must be re-enriched.

Illustrative schema only; every ID below is a placeholder, never an execution
claim or an allowed substitute for actual inventory values:

```json
{
  "pr": 123,
  "verdict": "accept",
  "profile": "native",
  "units": ["tests/example/test-example"],
  "fail_to_pass": ["tests/example/test-example::bounds"],
  "pass_to_pass": [],
  "oracle": "runtime",
  "change_type": "bugfix",
  "problem_statement": "The parser must respect the supplied input length.",
  "causal_chain": "The changed upstream case supplies a truncated input and checks the documented error."
}
```

High confidence requires source attribution plus compatible observed evidence;
uncertain dependencies/configuration are holds. Do not spend model calls filling
mechanically obtainable inventory gaps. Research controls are intentionally
selected, not held-out triage-accuracy evidence, and their expected answers must
stay out of future prompts.

The native adapter, schema, generator, validator and cluster integration are
implemented and bounded CPU controls pass. Full-campaign readiness remains
closed: historical profile resolution, remaining profile/era controls, complete
baseline inventories, guarded-body execution agreement and model handoff proof
are outstanding. No full pass has been launched.


## Cluster execution

Set `project: contiki-ng` and `slurm.build_partition: cpu` in the cluster config.
Use a separate scratch root and optional `paths.contiki_ng_image` override for
the verified x86 CI SIF. `cluster/setup.py` verifies the clone, image and tools;
`cluster/prepare.py` fetches/enriches the selected PRs without Twister or Zephyr
SDK setup. Full history needs an explicit 2017-to-pin preparation date window.

Submit prepared runs with `cluster/submit.py observe --run NAME` for x86 runtime
observation, `triage` for Spark model inference, and `validate` for x86 accepted
recipes. Contiki does not use the combined Zephyr `pipeline` mode. CPU resources
use `slurm.build_cpus_per_node`, `build_mem`, and `build_python`; Spark retains
`cpus_per_node`, `mem`, and `triage_python`. Set architecture-appropriate Python
executables when system Python lacks dependencies. The model is qwen38-nvfp4.

Jobs use `--export=NONE`, a clean environment, absolute shell paths, and PATH
including `/usr/sbin`. Submissions cap wall time at four hours. Run folders,
claims, project-scoped ledgers, recipes, observed/validation receipts and logs
support resuming incomplete work. A VALID receipt certifies only its matching
recipe fingerprint; promotion/publication still requires user authorization.


Cooja writable overlays use node-local temporary storage during validation and
fresh agent grading. On Scholar NFS, a loaded native mote library can prevent
`make clean` from removing its build directory; node-local reruns of the unchanged
RPL controls pass. Logs/receipts remain in the shared run directory. TUN/TAP
fixtures remain unsupported on this worker because opening the bound device from
the user namespace returns permission denied; those selections are infrastructure
holds. Current automatic accepts require source-unconditional native bodies;
Cooja/script observations are supported, while guarded/scenario accepts need
additional compiled-body evidence. No full-history campaign is certified yet.

## Integration proof (2026-11-04)

Scholar CPU job 484669 ran the final two controls from a frozen source archive
(`5b4c9e9`, archive SHA256
`0c4551e4122d44136778a3dc31355b2232ce967b7a75d70f3614406733563637`).
These use explicitly labelled, source-informed manual verdicts; they prove
packaging and validation, not unattended model selection or triage accuracy.

| PR | Profile/oracle | Before | After | Repeated before |
| --- | --- | --- | --- | --- |
| #3246 | native/runtime | Four named JSON failures; two named passes | All six pass | Three additional runs agree |
| #3020 | native/semantic compile | Missing `cbor_write_signed` / `cbor_read_signed`, attributed to the interface added by the PR | Named signed-integer case passes | Three additional builds agree |

Finalization took about 4.7 minutes per instance including image creation.
Receipts and logs live under
`/scratch/scholar/zhan5136/embedeval-contiki/runs/port-pilot/`.
The earlier observation pass retains its own receipts; the CBOR observation
correctly held for semantic attribution rather than counting an unexecuted case
as a runtime failure. Infrastructure/profile holds remain outside the permanent
invalid-instance ledger. Resubmitting observation retries failed/capped work
and preserves completed observations.

Spark's `SparkRelocation` maintenance reservation covers November 4 through
November 11, 2026. The bounded inference submission was canceled after its
forecast moved to November 11; no inference occurred and no paid fallback was
used. Actual qwen38-nvfp4 triage-to-validation handoff remains unverified. No
instances have been promoted or published.

Fresh-grader control job 484672 rebuilt the JSON image independently and applied
only the upstream production fix on a fresh node-local overlay: all six cases
passed. A separate protected-test patch was rejected before container execution.
Results are under `research/gold-grader-control/` in the Scholar workspace. This
is an upstream-gold control, not an agent attempt. The final local verification
has 24 pipeline/runner/grading checks and nine cluster checks passing.
