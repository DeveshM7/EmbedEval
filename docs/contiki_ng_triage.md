# Contiki-NG triage contract

Status: research checkpoint; **not ready for automated triage or a full pass**.
There is no Contiki adapter in the generic pipeline yet. This document must not
be used to generate automatic accepts until that adapter and its checks exist.
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
`IP64_TAP_MODE dhcp` and keeps `static`, while =1 reverses them. Full automated
guard/provenance resolution and runtime agreement remain readiness work.

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
| ipv6 | Cooja scenarios | Pending simulator control |
| ipv6-nbr | native neighbor test | Current x86 control passed with named unit result; historical versions untested |
| ieee802154 | Cooja scenarios | Pending simulator control |
| rpl-lite | Cooja scenarios | Pending simulator control |
| rpl-classic | Cooja scenarios | Pending simulator control |
| tun-rpl-br | Cooja plus host TUN/router scripts | Pending simulator and network privilege fixture |
| coap-lwm2m | simulations/standalone native and queue mode | External submodule/services; fixture unverified |
| packet-parsing | native packet injector, four shell consumers | Current x86 suite completed; historical profile coverage bounded |
| security-protocols | Cooja and crypto dependencies | Pending simulator/submodule controls |
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

The existing generic convention uses `pr`, `verdict` (`accept`, `reject`, `hold`)
and `reason`; accepts require project-specific selection checks. **Contiki's
accept schema, inventory checker and dispatch are not implemented.** Do not
invent OpenThread `test_targets` or CMake fields for Contiki. A neutral research
hold can be recorded as follows; this is not a launchable verdict:

```json
{"pr": 123, "verdict": "hold", "reason": "Selected upstream scenario has no smoke-controlled worker profile"}
```

123 is an illustrative placeholder. Runtime/compile distinctions, profile,
compile-symbol provenance, named F2P/P2P and patch separation must round-trip
through the actual adapter before accepts are permitted. Configuration-only
oracles are authorized, but remain holds until the adapter schema supports them.

High confidence requires source attribution plus compatible observed evidence;
uncertain dependencies/configuration are holds. Do not spend model calls filling
mechanically obtainable inventory gaps. Research controls are intentionally
selected, not held-out triage-accuracy evidence, and their expected answers must
stay out of future prompts.

Readiness remains closed: historical profile resolution, remaining profile/era
controls, complete baseline inventories, guard execution agreement,
adapter/schema integration, and measured model and finalization costs are
outstanding. No full pass has been launched.
