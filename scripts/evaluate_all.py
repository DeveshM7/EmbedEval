import os
import glob
import json
import subprocess
import concurrent.futures

def evaluate_patch(patch_path):
    # patch_path = outputs/riot__riot-20197/claude-opus-4-6/riot__riot-20197.patch
    parts = patch_path.split('/')
    instance = parts[1]
    model = parts[2]
    
    # Read trajectory
    traj_path = patch_path.replace('.patch', '.trajectory.json')
    steps = "N/A"
    agent_status = "Unknown"
    if os.path.exists(traj_path):
        with open(traj_path, 'r') as f:
            try:
                data = json.load(f)
                info = data.get('info', {})
                stats = info.get('model_stats', {})
                steps = stats.get('api_calls', 'N/A')
                
                agent_status = info.get('exit_status', '')
                if not agent_status:
                    submission = info.get('submission', '')
                    if 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' in submission:
                        agent_status = 'Submitted'
                    else:
                        agent_status = 'Unknown/FormatError'
            except Exception as e:
                pass
                
    # Run validation
    cmd = ['./scripts/validate_riot_instance.sh', instance, patch_path]
    print(f"Running validation for {instance} with {model}...")
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
        validation = "PASS" if result.returncode == 0 else "FAIL"
    except subprocess.TimeoutExpired:
        validation = "TIMEOUT"
        
    return {
        'instance': instance,
        'model': model,
        'steps': steps,
        'agent_status': agent_status,
        'validation': validation
    }

def main():
    patch_files = glob.glob('outputs/riot__*/*/*.patch')
    results = []
    
    print(f"Found {len(patch_files)} patches to evaluate.")
    
    # Run sequentially or parallel? Let's use 4 workers to speed it up.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(evaluate_patch, p) for p in patch_files]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            print(f"Finished {res['instance']} {res['model']}: {res['validation']}")
            results.append(res)
            
    # Sort results
    results.sort(key=lambda x: (x['instance'], x['model']))
    
    # Write to RESULTS.md
    with open('outputs/RESULTS.md', 'w') as f:
        f.write("# Benchmark Results (RIOT OS ONLY)\n\n")
        f.write("This document tracks the results and step sizes for all models benchmarked on RIOT OS issues.\n")
        f.write("Every patch has been individually evaluated by the `validate_riot_instance.sh` script.\n\n")
        f.write("| Instance / PR | Model | Steps Taken | Agent Exit Status | Validation Result |\n")
        f.write("| :--- | :--- | :--- | :--- | :--- |\n")
        for r in results:
            f.write(f"| **{r['instance']}** | {r['model']} | {r['steps']} | {r['agent_status']} | **{r['validation']}** |\n")

if __name__ == '__main__':
    main()
