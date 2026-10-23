# Ideal verdicts

Hand-written triage verdicts that stand in for the model while the twister
pipeline (enrich -> dry run -> generate -> build -> validate) is developed and
tested on its own. Same format as a model verdict (`docs/pr_triage.md`), plus
the twister fields the pipeline needs: `test_path` and `scenario`.

- The eight gold accepts use the board and scenario of the hand-built
  instance in `docker/instances/`, and its problem statement.
- `112110.json` is a deliberately wrong accept: that PR only moves tests, so
  validation must reject it. It checks the pipeline's rejection path.
- 99824 has no verdict: its dry run selects no pair (the suite needs a memory
  controller none of our boards has), so it is rejected before triage.
