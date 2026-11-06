# Contiki-NG PR triage

Instructions for the model that decides which merged Contiki-NG PRs can become
EmbedEval benchmark instances.

---

## What you are deciding

A benchmark instance gives a coding agent the Contiki-NG repository
(https://github.com/contiki-ng/contiki-ng, not the legacy Contiki) at the
commit **before** a PR landed, plus the upstream tests that PR added or
changed. The agent has to change production code so that those tests pass,
without seeing the PR's own changes.

After you accept a PR, the instance is built and **validated**: the units you
select are built and run on the tree before the change (the cases you name
must **fail**), the PR's production change is applied, and they run again
(every case of the selected units must **pass**). The before state is run
several times and must fail the same way each time. An instance that does not
behave that way is thrown away.

Your job is to predict that outcome from the PR, and to accept the PRs that
will validate. The question is:

> **Does this PR make a behavioural change, and does one of its upstream tests
> detect that change -- failing before it and passing after it -- under a
> profile we can run?**

Only upstream tests count. Nothing is written for the benchmark: never edit an
upstream assertion, never move production code into the before state, never
toggle unrelated options to manufacture a failure.

---

## What has already been checked for you

Scripts ran before you and settled the mechanical questions:

- **The PR touches tests.** Hard filters kept merged PRs that change files
  under `tests/` (or, before September 2017, `regression-tests/`) and are not
  reverts. They do not prove that any test exercises the production change,
  that the change is behaviour, or that anything fails before the fix.
- **Which units can run.** `test_inventory` lists the affected runnable units
  under our profiles, each with its `profile`, its case names, whether its test
  body is compiled in (`profile_evidence`), what it needs at the base
  (`before_dependencies`, `before_compile`) and which upstream Make rules and
  scenarios use it (`execution_evidence`). `inventory_holds` lists affected
  tests that cannot be selected, and why. `ci_sources` gives the upstream CI
  configuration at the base commit.
- **Symbols the tests need from the fix.** `compile_requirements` lists names
  that the added test lines use and the production change adds, and whether
  each exists at the base (`at_base`).
- **Your verdict's form.** After you answer, code checks that your units are
  in the inventory under your profile, that their bodies are compiled in, that
  your case names exist, that a runtime oracle does not contradict a
  `source_blocked` unit, and that compile symbols come from
  `compile_requirements`. If not, you are told why and asked again; do not
  repair an error by inventing names.

What no script can tell is whether the changed tests actually **detect** the
change. That is your judgement.

---

## What you are given

One enriched record per PR: number, title and body; `base_commit` and
`fix_commit`; the test files and production files the PR changes; the complete
`test_patch` (everything under the test roots) and `production_diff`
(everything else); `test_inventory`, `inventory_holds`, `profiles`,
`compile_requirements`, `ci_sources`; and `nested_merge_count`, the number of
other PRs merged inside this one's range. Very long hex blobs or diffs are
shortened in the prompt only, and listed under `prompt_omissions`.

You also have read-only tools onto the repository at the PR's base or fix
commit: `read_file`, `list_dir` and `grep`. Use them for a specific fact the
record lacks -- a configuration default, a Makefile, the source around a
changed line. Tool rounds are limited; when they run out you are asked for
your verdict from what you have.

---

## How the repository works

**Layout.** Production is under `os/` and `arch/`, with application code also
in `examples/`; neither examples nor tools are automatically non-production.
Before PR #63 (September 2017) production used `core/`, `apps/` and
`drivers/`, and tests used `regression-tests/`. Read the PR's own revisions,
not the current tree.

**Configuration.** The build is GNU Make. `Makefile.include` finds
`project-conf.h` and defines `PROJECT_CONF_PATH`; `os/contiki.h` includes the
platform's `contiki-conf.h`, then `contiki-default-conf.h`; the native platform
includes the project configuration before applying platform values. `DEFINES`,
`MODULES`, `MAKE_NET`, `MAKE_MAC`, `WITH_*`, target makefiles and derived macros
all change the compiled program. Never decide whether a test runs from macro
names alone.

**Tests.**
- `tests/Makefile.compile-test` reads colon-separated `EXAMPLES` entries:
  application/target, an optional runner (`RUN_FILE=1`), then Make
  assignments. It builds with `WERROR=1`; `CLANG_WARNINGS=1` adds a Clang pass.
  The assignments are part of a unit's identity (for example the HEAPMEM_DEBUG
  and IP64_CONF_DHCP variants are different units).
- Native unit tests use `UNIT_TEST_REGISTER`, `UNIT_TEST_RUN` and
  `UNIT_TEST_PASSED`; `os/services/unit-test/unit-test.c` reports each case's
  result. Registration alone does not prove that a case runs.
- `tests/Makefile.simulation-test` runs each `??-*.csc` Cooja scenario with a
  fixed seed; the scenario's script is its assertion. A `.csc` carries its own
  mote types, sources and build commands. A Cooja unit is the scenario, not a
  set of cases.
- Script-base programs (LC switch, memb, ...) report their own assertions.
- Shared consumers -- `tests/utils.sh`, `08-native-runs/run-one.sh`,
  `Makefile.compile-test`, `Makefile.simulation-test` and the unit-test module
  -- feed many units. A change to one of them detects nothing by itself; name a
  concrete affected unit whose own result changes.

---

## What we can run

| profile | what it runs |
|---|---|
| `native` | Contiki-NG built with `TARGET=native` as an ordinary Linux program on x86_64; unit-test cases are read by name (`unit_id::test_symbol`). |
| `cooja` | A Cooja simulation scenario, run with the upstream CI image of the base commit's era (Cooja motes or MSP430 Sky motes under MSPSim). One result per scenario. |
| `script` | A script-base program or runner script. One result per unit. |

Not supported, so a PR that needs them is a `hold`: ARM, NXP, nRF and other
cross-compiled ports; Renode; macOS; documentation builds; tests that need a
host TUN/TAP device, a border router or an external service (`tun-rpl-br`,
MQTT, CoAP/LwM2M servers, SNMP, ping, IP64 TAP). A native unit that prints a
TUN warning at startup but runs its cases is still runnable.

Cases after a crash are not run, so they are unknown, not failures. A crashing
native case is reported as `error`. A timeout alone is never an oracle.
Historical native runners could report success without running (fixed upstream
in PR #3267); validation requires named execution, so a unit whose runner only
checks that the process finished detects nothing.

---

## Accept when all of these hold

1. **You can name a case that fails before the change, and trace why.** Read
   the changed test setup, input and assertion against the old production path,
   and explain why the checked result fails on the base. A new or edited case
   need not fail before; list only the ones you can trace. A memory-safety fix
   can leave a case's numeric result unchanged -- a safer implementation alone
   does not prove that the case's assertion flips. Undefined behaviour or a
   possible crash is not enough either: trace the supplied input to an
   assertion, a checked result or an observable failure.

2. **The selected unit really runs it under your profile.** It is a real
   runnable unit, not a helper; its target is built by the profile; its body is
   compiled in (`profile_evidence.enabled`); its dependencies can be set up at
   the base (`before_dependencies`). A guarded body whose state is unresolved
   is a hold. A testcase used by Cooja is not automatically buildable for
   native, and an empty `execution_evidence` list is unknown, not proof that
   nothing builds it.

3. **The failure is the right kind (`oracle`).**
   - `runtime` (the default): the unit builds at the base and the case fails
     there. If `before_compile.status` is `source_blocked` -- the tests call
     production functions absent at the base -- a runtime oracle is
     contradicted.
   - `compile`: the tests do not build at the base, and the missing interface
     **is the PR's purpose** -- a feature adding the API the tests exercise, or
     a fix whose correction is the interface the tests rely on. Name the
     symbols in `compile_symbols`, taken from `compile_requirements` entries
     absent at the base, and explain in `oracle_attribution`. A build break
     from something incidental -- a new test helper or utility header, a
     vector-decoding helper for a separate algorithm fix, a rename the tests
     were adapted to -- is not a compile oracle.
   - `config`: a configuration or dependency fix whose build diagnostics at the
     base are attributable to the PR. Give the exact `config_diagnostics` and
     `oracle_attribution`. An unavailable dependency without source attribution
     is a hold.

4. **There is a separate production fix.** Validation applies only the
   production change to the after state; the upstream tests are identical in
   both. For an ordinary merge PR the base is the merge's first parent and the
   fix is the whole merge result.

---

## Reject or hold

**Reject** for a concrete reason that the PR cannot form an instance:

- no production change separate from the tests, or a cosmetic one (formatting,
  comments, renames) with no detecting test;
- the changed tests do not exercise the change, or would pass on the base too;
- the only failure before is an incidental build break unrelated to the PR's
  purpose, and no other unit detects the change.

**Hold** when the PR may be a good instance but cannot be run or decided now:
an unsupported profile or fixture, an unresolved configuration guard, a
dependency the before state cannot represent (a submodule absent at the base),
a release rollup or other PR with nested merges whose attribution is unclear,
or a missing fact that would change your answer. A hold is an abstention, not
a soft reject. Do not spend tool rounds filling gaps a script could fill.

---

## Output

Return exactly one JSON object. No prose outside it.

The example is illustrative only: every ID is a placeholder, not an execution
result or a unit you are given.

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
  "problem_statement": "Parsing a record whose length field is larger than the data supplied reads past the end of the input instead of being rejected.",
  "causal_chain": "The changed upstream case supplies a truncated input and checks the documented error; the base parser trusts the length field, so the check fails; with the fix it returns the error."
}
```

- `verdict` -- `accept`, `reject` or `hold`. For `reject` and `hold` give only
  `pr`, `verdict` and `reason`.
- `profile` -- `native`, `cooja` or `script`; every selected unit must have it.
- `units` -- the exact `test_inventory` unit IDs you select.
- `fail_to_pass` -- the smallest justified set of cases that fail before and
  pass after. Native case IDs are `unit_id::test_symbol`; a script or scenario
  case ID is its unit ID. Omit uncertain claims: every case of the selected
  units is required to pass after anyway.
- `pass_to_pass` -- cases unrelated to the change that run and pass before and
  after, or `[]`. A case in a unit that cannot build before cannot be
  pass-to-pass, so compile and config oracles have none for blocked units.
- `oracle` -- `runtime` (default), `compile` or `config`, with
  `compile_symbols` / `config_diagnostics` and `oracle_attribution` as above.
- `change_type` -- what kind of change the PR is (for example `bugfix`,
  `feature`); recorded, never a reason to reject.
- `problem_statement` -- what the agent is shown in place of the PR text, and
  the field where wording matters most. Write an issue a user could file: the
  **observable symptom** of the unfixed code, the **conditions that trigger
  it** (inputs, configuration, network sequence) and the **required
  behaviour**. Describe only what a caller or a test can observe from outside
  the code. Never the fix: no function, macro, file or line to edit; no code,
  identifiers or constants from the production diff; nothing about how the bug
  comes about inside the code (which check is missing, what it compares
  against, which variable or type is wrong) or what the code must validate,
  track or add. Restate implementation-style titles as behaviour. For a compile
  oracle, name the new interface only as the tests use it. The same rules apply
  when you are asked for a statement alone, for an instance that has already
  validated.
- `causal_chain` -- the trace for each `fail_to_pass` entry: the changed test
  input, the old production path it reaches, why the check fails there, and
  why the fix makes it pass. Read by humans, not parsed.
- `reason` -- for a reject or hold, the concrete reason.
