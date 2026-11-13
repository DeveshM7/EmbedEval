#!/bin/bash
#SBATCH -p spark-batch
#SBATCH --gres=gpu:1
#SBATCH -c 20
#SBATCH --mem=120500M
#SBATCH -t 04:00:00
#SBATCH -A scholar
#SBATCH -J phase1-round
#SBATCH -o /scratch/scholar/zhan5136/phase1-results/slurm-%j.out

set -u -o pipefail
ROUND="${1:?round number required}"
CODE="${2:?frozen code directory required}"
RESULTS=/scratch/scholar/zhan5136/phase1-results
OUT="$RESULTS/round$ROUND"
PYTHON="$HOME/EmbedEval/.venv/bin/python"
mkdir -p "$OUT"
cd "$CODE" || exit 1
export PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '^/apps/cent7' | paste -sd: -)"
export SLURM_SUBMIT_DIR="$CODE"
unset CONDA_PREFIX CONDA_DEFAULT_ENV
date -Is > "$OUT/started.txt"

# Job 477004 owns round 2. Its clients finish before round 3 starts.
if [ "$ROUND" = 3 ]; then
    "$PYTHON" - "$CODE" "$RESULTS/round2" <<'PY' > "$OUT/round2-copy.log" 2>&1
import json, shutil, subprocess, sys
from pathlib import Path
code, dest = Path(sys.argv[1]), Path(sys.argv[2])
source = Path.home() / 'EmbedEval-phase1'
runs = source / 'triage/nuttx_runs'
dest.mkdir(parents=True, exist_ok=True)
normal = runs / '2026-10-16_184035_qwen38-nvfp4_apps'
blind = sorted(runs.glob('2026-10-16_*_qwen38-nvfp4_apps_blind'))
blind = [p for p in blind if p.name > normal.name]
for label, run in [('normal', normal), ('blind', blind[-1] if blind else None)]:
    if run is None or not run.exists():
        print(f'{label}: run directory unavailable', flush=True)
        continue
    target = dest / label
    shutil.copytree(run, target, dirs_exist_ok=True)
    with (dest / f'{label}-eval.log').open('w') as log:
        result = subprocess.run([str(Path.home() / 'EmbedEval/.venv/bin/python'),
                                 str(code / 'scripts/triage_nuttx_prs.py'), 'eval', str(target)],
                                cwd=code, stdout=log, stderr=subprocess.STDOUT)
    print(f'{label}: copied {run.name}; eval exit {result.returncode}', flush=True)
for log in ('round-r2-driver.log', 'round-r2-normal.log', 'round-r2-blind.log'):
    path = source / log
    if path.exists(): shutil.copy2(path, dest / log)
lines = ['# Round 2', '']
for label in ('normal', 'blind'):
    manifest = dest / label / 'manifest.json'
    score = dest / label / 'scorecard.json'
    if manifest.exists():
        m = json.loads(manifest.read_text())
        lines.append(f"- {label}: prompt `{m.get('prompt_sha256')}`, started {m.get('started')}, finished {m.get('finished', 'incomplete')}")
    if score.exists():
        s = json.loads(score.read_text())
        lines.append(f"  Score {s['correct']}/{s['scored']}; misses: " + ', '.join(r['case'] for r in s['rows'] if not r['correct']))
(dest / 'summary.md').write_text('\n'.join(lines) + '\n')
PY
fi

server_pid=
stop_server() {
    if [ -n "$server_pid" ]; then
        kill "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
    fi
}
trap stop_server EXIT
bash "$CODE/scripts/serve_triage_model.sh" qwen38-nvfp4 > "$OUT/server.log" 2>&1 &
server_pid=$!
ready=0
for ((i=0; i<180; i++)); do
    if curl -fsS --max-time 5 http://127.0.0.1:8000/v1/models > "$OUT/models.json" 2>/dev/null; then
        ready=1
        break
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then break; fi
    sleep 10
done
if [ "$ready" -ne 1 ]; then
    echo 'Model server did not become ready within 30 minutes' > "$OUT/error.txt"
    exit 1
fi
date -Is > "$OUT/server-ready.txt"

client_failed=0
for mode in normal blind; do
    args=(apps --profile qwen38-nvfp4 --gold)
    if [ "$mode" = blind ]; then args+=(--blind); fi
    if ! "$PYTHON" scripts/triage_nuttx_prs.py "${args[@]}" > "$OUT/$mode.log" 2>&1; then
        client_failed=1
    fi
    pattern="*_qwen38-nvfp4_apps"
    if [ "$mode" = blind ]; then pattern="*_qwen38-nvfp4_apps_blind"; fi
    run=$(ls -dt triage/nuttx_runs/$pattern 2>/dev/null | head -1)
    if [ -z "$run" ]; then
        client_failed=1
        continue
    fi
    if ! "$PYTHON" scripts/triage_nuttx_prs.py eval "$run" > "$OUT/$mode-eval.log" 2>&1; then
        client_failed=1
    fi
    cp -a "$run" "$OUT/$mode"
done
date -Is > "$OUT/finished.txt"

"$PYTHON" - "$OUT" <<'PY'
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
lines = [f'# {out.name}', '', f"- Job: {__import__('os').environ.get('SLURM_JOB_ID', '?')}",
         f"- Started: {out.joinpath('started.txt').read_text().strip()}",
         f"- Server ready: {out.joinpath('server-ready.txt').read_text().strip()}",
         f"- Finished: {out.joinpath('finished.txt').read_text().strip()}"]
for mode in ('normal', 'blind'):
    manifest, score = out / mode / 'manifest.json', out / mode / 'scorecard.json'
    if not manifest.exists():
        lines.append(f'- {mode}: no run manifest')
        continue
    m = json.loads(manifest.read_text())
    lines += ['', f'## {mode}', '', f"- Prompt SHA-256: `{m['prompt_sha256']}`",
              f"- Run: {out.joinpath(mode).name} ({m.get('started')} to {m.get('finished', 'incomplete')})"]
    if score.exists():
        s = json.loads(score.read_text())
        lines += [f"- Score: {s['correct']}/{s['scored']}",
                  f"- Must-find: {s['must_find_found']}/{s['must_find_total']}",
                  f"- Forbidden proposals: {s['violations']}",
                  '- Misses: ' + (', '.join(r['case'] for r in s['rows'] if not r['correct']) or 'none')]
    else:
        lines.append('- Scorecard unavailable')
(out / 'summary.md').write_text('\n'.join(lines) + '\n')
PY
exit "$client_failed"
