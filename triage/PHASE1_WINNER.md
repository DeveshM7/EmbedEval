# NuttX Phase 1 GA winner

- Individual: `68660ee811` (generation 0, frozen code commit `3c21afd32f060f1b5774f1474aec4f8af688da87`).
- Prompt SHA-256: `69e201aa67261cb242971012e4aec34fa83bf5cff104473d95ac6299873e6148`.
- Pass bar: repeats 2 and 3 were each 13/13 on the normal partial gold with the same individual SHA.
- Fitness at stop: mean normal 12.67/13, worst normal 12/13, mean completed blind 8/9.
- All five completed runs below have worker exit 0 and evaluation exit 0.
- Blind runs are diagnostic and do not count toward the pass bar.

| Repeat | Normal | Normal miss | Blind | Blind miss |
|---|---:|---|---:|---|
| 1 | 12/13 | apps-2137 | 8/9 | apps-2137 |
| 2 | 13/13 | none | 8/9 | apps-2137 |
| 3 | 13/13 | none | not completed | — |

- Source of scorecards and traces: `/scratch/scholar/zhan5136/phase1-results/ga/gen00/68660ee811/rep<k>/{normal,blind}/`.
- Sampling and limits: temperature 1.0, top_p 0.95, top_k 20, 40 tool calls, 30 minutes per apps PR, four cases in parallel; `followup_review=false`.
- Prompt additions were general rules about follow-up kernel changes, config branches, apps fixes, and final review. The added rules contain no gold-case identifiers.

## Prompt diff from generation 0 base

```diff
--- triage/ga/base_prompt.md
+++ triage/ga/individuals/68660ee811/nuttx_apps_triage.md
@@ -30,6 +30,10 @@
 - `qemu_only`: a `kernel_test` whose changed test can only run on a QEMU
   board, not on native `sim:nsh` (for instance it needs SMP, an
   architecture-specific hook, or hardware the simulator lacks).
+
+When a kernel change only made existing apps code stop compiling or running
+and the apps PR repairs that apps code, the kind is `apps_fix`, and the kernel
+PR that exposed it is `low`.
 
 ## 2. Find the kernel PRs
 
@@ -101,6 +105,21 @@
 `behavior_change` when it deliberately changes or adds behavior (including
 a new interface) that the changed test now expects.
 
+A later kernel PR that edits code the primary PR added or changed, merged
+before the apps PR, goes into `also_needs` unless its diff clearly cannot
+affect any path the changed test reaches. Reading cannot show that the test
+passes without it, and validation drops a follow-up that is not needed, so
+when unsure list it with the reason. Never dismiss a follow-up as unrelated
+from its subject line or its size alone.
+
+If the primary PR adds a function or a source file, every later commit that
+edits that function or file is a follow-up candidate: open it and judge it
+against the changed test's inputs and timing.
+
+A kernel PR whose diff only moves, renames or re-guards Kconfig symbols or
+build plumbing, and adds no behavior the changed test asserts, is `low` even
+when it is linked or merged near the apps PR.
+
 ## 3. Check the runnable target
 
 The generator and runner use native `sim:nsh` with `ostest`. A kernel
@@ -129,6 +148,15 @@
 The runner judges the whole `ostest` run: it passes only when the suite
 completes with status 0, prints no `ERROR` line, and prints the
 `user_main: ... test` banner of every changed test.
+
+Your input ends with the options the changed tests need, computed from the
+sources: put the listed required options (those not already on in the board's
+defaults) in `config_overrides`, and one option of each "one of" group.
+
+When the changed test has alternative `#if` branches, decide from the kernel
+diff which branch exercises the fixed code, say so in the evidence, and set
+the options that compile that branch; `check_config` reports which branches
+are compiled.
 
 ## 4. Tools
 
@@ -147,6 +175,10 @@
 explicit references, then symbols and dates. Decide from the evidence you
 gathered and say what remains uncertain. Do not use outside knowledge about
 specific PRs.
+
+Once you have confirmed the pairs, made the follow-up check and run
+`check_config`, answer. Do not spend the rest of the budget on further
+exploration.
 
 ## 5. Output
 
@@ -205,3 +237,7 @@
   `needs_reviewed_recipe` when you cannot tell. Use `needs_fixture` for a
   QEMU platform, another test program, or `runtime_other`.
 - Do not invent links, paths, target support, config settings or results.
+
+Before replying, re-read each pair against the rules above: its confidence,
+its `also_needs`, its `config_overrides` and its `readiness` must follow from
+the evidence you listed.
```
