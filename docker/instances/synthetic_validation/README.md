# Validation code and outputs for all 15 synthetic instances

`code/reproduce.py` contains the shared validation checks. `code/manifest.json` supplies commands and exact expected results for all 15 cases. `code/instances/` contains each case's test patches, production injection and repair patches, test runners, build contexts, and reports; `code/docker/` supplies base-image definitions. This code directory is self-contained relative to the exported benchmark bundle: Python, Docker, network downloads, and upstream pinned repositories are still required.

Run from this validation folder:

```sh
python code/reproduce.py --list
python code/reproduce.py --all --build --validate
python code/reproduce.py --instance openthread-13494-late-order --build --validate
```

New execution outputs go to `runs/<instance-id>/`: correct, synthetic, and restored build/test logs plus `results.json`. The validator requires the correct and restored suites to pass, exactly the expected synthetic failures, all controls passing, unchanged test/runner hashes, and matching correct/restored source hashes. Actual test implementations are in the bundled patches, not in the shared Python validator.

Every instance has historical outputs in `<instance-id>/saved-evidence/`. These are copied verbatim from its existing `evidence/` directory. `provenance.json` records their origin and SHA256 hashes. The root `index.json` lists all 15. Saved evidence includes all three test-phase logs and original result JSONs. Build logs are included where previously recorded; missing historical build logs are not fabricated. Original result schemas vary between the earlier validators.

Collection on 2026-11-01 did not execute container tests: Docker's Linux engine was unavailable. See `docker-availability-2026-11-01.log`. `code-list-check.log` and `code-dry-run-check.log` verify discovery and command generation only; they are not test passes. The older logs directly under some instance directories predate this collection and remain preserved. Fresh local Python measurements from 2026-10-31 are preserved within the ordering case's saved evidence and are explicitly local, not Docker runs.

Gold repairs are included for reproducibility and review. Exclude them and the reports from agent-visible evaluation inputs. The original bundle-root `reproduce.py` remains usable; this copy writes new outputs into this folder's `runs/` directory.
