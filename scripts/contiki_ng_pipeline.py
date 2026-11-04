"""Contiki-NG packaging and validation using shared EmbedEval receipts/builds."""
from __future__ import annotations
import json, os, shutil, subprocess, time
import re
import tempfile
from pathlib import Path
import apptainer_util as au
import contiki_ng_candidates as candidates
import instances

TEMPLATES=Path(__file__).resolve().parent.parent/'templates/contiki-ng/apptainer'

def render_record(rec):
    if candidates.stale_record(rec):raise ValueError('stale Contiki-NG record')
    payload={k:rec[k] for k in ('number','title','body','base_commit','fix_commit','test_files',
        'source_files','profiles','inventory_holds','test_patch','production_diff','nested_merge_count','compile_requirements')}
    payload['test_inventory']=candidates.select_tests(rec)
    payload['inventory_note']='Only affected supported runnable units; absent or guarded selections must be held.'
    return json.dumps(payload,indent=2)

def checked_observation(prior,receipt,rec,available):
    if not receipt.get('valid') or receipt.get('observe') is not True or receipt.get('recipe_sha256')!=au.recipe_fingerprint(prior):
        raise ValueError('observed receipt is invalid or stale')
    cfg=json.loads((prior/'instance.json').read_text())
    meta=json.loads((prior/'metadata.json').read_text())
    if cfg.get('observe') is not True or cfg.get('enrich_version')!=candidates.ENRICH_VERSION or cfg.get('oracle')!='runtime':
        raise ValueError('observation configuration is invalid or stale')
    if (meta.get('base_commit')!=rec['base_commit'] or meta.get('fix_commit')!=rec['fix_commit'] or
        meta.get('files_changed_by_fix')!=rec['source_files'] or receipt.get('instance_id')!=meta.get('instance_id') or
        receipt.get('profile')!=cfg.get('profile')):
        raise ValueError('observation belongs to a different PR revision')
    units=cfg.get('units')
    if not isinstance(units,list) or not units or any(not isinstance(u,dict) or u.get('id') not in available or u!=available[u['id']] for u in units):
        raise ValueError('observation selected stale or unsupported units')
    f2p=receipt.get('fail_to_pass');p2p=receipt.get('pass_to_pass')
    names={n for u in units for n in candidates.case_names(u)}
    before=receipt.get('before',{});after=receipt.get('after',{})
    if not isinstance(before,dict) or not isinstance(after,dict) or not isinstance(before.get('cases'),dict) or not isinstance(after.get('cases'),dict):
        raise ValueError('observation lacks named case results')
    if (not isinstance(f2p,list) or not f2p or not isinstance(p2p,list) or
        any(not isinstance(n,str) or n not in names for n in f2p+p2p) or set(f2p)&set(p2p) or
        before.get('rc')!=1 or after.get('rc')!=0 or
        any(before.get('cases',{}).get(n) not in ('failed','error') or after.get('cases',{}).get(n)!='passed' for n in f2p) or
        any(before.get('cases',{}).get(n)!='passed' or after.get('cases',{}).get(n)!='passed' for n in p2p) or
        receipt.get('regressed')):
        raise ValueError('observation lacks named runtime fail-before/pass-after evidence')
    return cfg,meta

def generate(pr,verdict,out_root,observe=False,observed=None,profile=None):
    from generate_instance import check_applies
    rec=json.loads((candidates.enriched_dir()/f'{pr}.json').read_text())
    if candidates.stale_record(rec):raise ValueError('stale Contiki-NG record; re-enrich')
    available={u['id']:u for u in candidates.select_tests(rec,profile)}
    if observed:
        prior=Path(observed);receipt=json.loads((prior/'observed.json').read_text())
        cfg,prior_meta=checked_observation(prior,receipt,rec,available)
        cfg['observe']=False
        cfg.update({k:receipt[k] for k in ('fail_to_pass','pass_to_pass')})
        if verdict is not None:
            errors=candidates.check_verdict(verdict,rec)
            if errors:raise ValueError('; '.join(errors))
            if verdict['verdict']!='accept' or set(verdict['units'])!={u['id'] for u in cfg['units']} or verdict['profile']!=cfg.get('profile') or set(verdict['fail_to_pass'])!=set(cfg['fail_to_pass']) or set(verdict.get('pass_to_pass',[]))!=set(cfg['pass_to_pass']) or verdict.get('oracle','runtime')!='runtime':
                raise ValueError('accepted verdict does not match observed selection and case evidence')
            statement=verdict['problem_statement']
        else:
            statement=None
    elif observe:
        if not available:raise ValueError('no supported affected upstream runnable unit; candidate held')
        profiles={u['profile'] for u in available.values()}
        if profile is None:
            if len(profiles)!=1:raise ValueError('observation requires an explicit profile for mixed affected units')
            profile=next(iter(profiles))
        if profile not in rec['profiles']:raise ValueError('unknown observation profile')
        if rec['nested_merge_count']:raise ValueError('release/merged-PR rollup needs attribution review')
        cfg={'observe':True,'units':list(available.values()),'profile':profile,
             'fail_to_pass':[],'pass_to_pass':[],'oracle':'runtime'}
        statement=None
    else:
        errors=candidates.check_verdict(verdict,rec)
        if errors:raise ValueError('; '.join(errors))
        if verdict['verdict']!='accept':raise ValueError('only accepted verdicts can generate')
        available={u['id']:u for u in candidates.select_tests(rec,verdict.get('profile'))}
        cfg={'observe':False,'units':[available[u] for u in verdict['units']],
             'profile':verdict.get('profile'),'fail_to_pass':verdict['fail_to_pass'],
             'pass_to_pass':verdict.get('pass_to_pass',[]),'oracle':verdict.get('oracle','runtime')}
        if cfg['oracle']=='compile':
            cfg.update({k:verdict[k] for k in ('compile_symbols','oracle_attribution')})
        elif cfg['oracle']=='config':
            cfg.update({k:verdict[k] for k in ('config_diagnostics','oracle_attribution')})
        statement=verdict['problem_statement']
    iid=f'contiki-ng__contiki-ng-{pr}'; dest=Path(out_root)/iid
    if observed and dest.resolve()==Path(observed).resolve():raise ValueError('finalization must preserve observation in a different directory')
    dest.mkdir(parents=True,exist_ok=True)
    patch=rec['test_patch']
    if not patch.strip():raise ValueError('no upstream test patch')
    (dest/'test_patch.diff').write_text(patch)
    check_applies(patch,rec['base_commit'],dest,candidates.clone())
    cfg['submodules']=sorted({p for u in cfg['units'] for p in u.get('submodules',[])})
    cfg['enrich_version']=candidates.ENRICH_VERSION
    (dest/'instance.json').write_text(json.dumps(cfg,indent=2)+'\n')
    shutil.copyfile(TEMPLATES/'run_tests.py',dest/'run_tests.py')
    definition=(TEMPLATES/'instance.def.tmpl').read_text()
    for key,value in {'@@INSTANCE_ID@@':iid,'@@REPO@@':candidates.REPO,'@@BASE_COMMIT@@':rec['base_commit']}.items():definition=definition.replace(key,value)
    (dest/'instance.def').write_text(definition)
    meta={'instance_id':iid,'project':'contiki-ng','pipeline':'contiki-ng','repo':candidates.REPO,
          'base_commit':rec['base_commit'],'fix_commit':rec['fix_commit'],'test_commit':rec['fix_commit'],
          'problem_statement':statement,'fail_to_pass':cfg['fail_to_pass'],'pass_to_pass':cfg['pass_to_pass'],
          'files_changed_by_fix':rec['source_files'],'pr_url':rec['url'],'run_command':'run_tests',
          'apptainer_image':iid+'.sif','generated':True,'oracle':cfg['oracle'],'architecture':'x86_64',
          'observation_only':bool(observed and verdict is None)}
    (dest/'metadata.json').write_text(json.dumps(meta,indent=2)+'\n')
    return dest

def parse_results(text):
    cases={};on=False
    for line in text.splitlines():
        if line.startswith('--- contiki-ng: results'):on=True
        elif on and line.startswith('--- end'):on=False
        elif on and line.startswith('  '):
            status,sep,name=line.strip().partition(' ')
            if sep and status in ('passed','failed','error','timeout','not_run'):cases[name]=status
    return cases

def fix_patch(meta):
    candidates.ensure_commits(meta['base_commit'],meta['fix_commit'])
    files=meta.get('files_changed_by_fix',[])
    if not files or any(p.startswith(candidates.TEST_ROOTS) for p in files):
        raise ValueError('no disjoint production fix')
    return candidates.git('diff','--binary',meta['base_commit'],meta['fix_commit'],'--',*files)

def config_attribution(output,diagnostics):
    failure_lines=[line for line in output.splitlines() if re.search(r'error:|fatal error:|undefined reference|make(?:\[\d+\])?: \*\*\*|failed',line,re.I)]
    return next((d for d in diagnostics if any(d in line for line in failure_lines)),None)

def protected_test_change(patch):
    return bool(re.search(r'^(?:diff --git a/|--- a/|\+\+\+ b/)(?:tests/|regression-tests/)',patch,re.M))

def validate(iid,patch_override=None,timeout=1800,verbose=False):
    from validate_instance import observe_lists, compile_attribution, build_errors, _finish
    folder=instances.instance_dir(iid);meta=instances.load_metadata(iid)
    cfg=json.loads((folder/'instance.json').read_text()); image=au.instance_image(iid)
    report={'instance_id':iid,'image':str(image),'valid':False,'observe':bool(cfg.get('observe')),
            'recipe_sha256':au.recipe_fingerprint(folder),'profile':cfg.get('profile'),
            'validated_at':time.strftime('%Y-%m-%dT%H:%M:%S')}
    if not image.exists():report['reason']='image not built';return _finish(iid,report)
    stamp=time.strftime('%Y%m%d-%H%M%S')+'-'+str(os.getpid())
    work=au.path('runs',iid+'-'+stamp,'x').parent
    local_overlay=tempfile.TemporaryDirectory(prefix='embedeval-contiki-')
    overlay=Path(local_overlay.name)/'overlay';overlay.mkdir()
    logs=au.path('logs',iid,'validate-'+stamp,'x').parent;logs.mkdir(parents=True,exist_ok=True)
    report['logs']=str(logs)
    cmd=au.exec_args(image,binds={work:'/embedeval-run'},overlay=overlay)
    def step(name,args):
        start=time.monotonic()
        try:
            r=subprocess.run(cmd+args,capture_output=True,text=True,errors='replace',timeout=timeout)
            rc,out=r.returncode,r.stdout+r.stderr
        except subprocess.TimeoutExpired as e:
            rc='timeout';out=(e.stdout or b'');out=out.decode(errors='replace') if isinstance(out,bytes) else out
        (logs/(name+'.log')).write_text(out)
        if verbose:print(out)
        return {'rc':rc,'seconds':round(time.monotonic()-start,3),'cases':parse_results(out),
                'build_errors':build_errors(out)},out
    try:
        before,out1=step('before',['run_tests']);report['before']=before
        patch=Path(patch_override).read_text() if patch_override else fix_patch(meta)
        if protected_test_change(patch):report['reason']='production fix overlaps protected upstream tests';return _finish(iid,report)
        if not patch.strip():report['reason']='empty production fix';return _finish(iid,report)
        (work/'fix.diff').write_text(patch)
        applied,outa=step('apply',['git','-C','/testbed','apply','/embedeval-run/fix.diff'])
        if applied['rc']!=0:report['reason']='production fix does not apply';return _finish(iid,report)
        # Always attempt after: missing interfaces before must not hide broken setup.
        after,out2=step('after',['run_tests']);report['after']=after
        lists=observe_lists(before['cases'],after['cases']);report.update(lists)
        if cfg.get('observe'):
            report['valid']=before['rc']==1 and bool(lists['fail_to_pass']) and after['rc']==0 and not lists['regressed']
            report['reason']='observed named fail-before/pass-after' if report['valid'] else (
                'base build failed; semantic compile/config attribution required' if before['rc']==2 else 'no attributable runtime F2P or after-state failure')
        else:
            f2p=cfg['fail_to_pass'];p2p=cfg['pass_to_pass'];oracle=cfg.get('oracle','runtime')
            if oracle=='runtime':signal=before['rc']==1 and all(before['cases'].get(n) in ('failed','error') for n in f2p)
            elif oracle=='compile':
                signal=before['rc']==2 and bool(compile_attribution(out1,cfg.get('compile_symbols',[])))
            else:
                signal=before['rc']==2 and bool(config_attribution(out1,cfg.get('config_diagnostics',[])))
            passes=all(after['cases'].get(n)=='passed' for n in f2p+p2p)
            report['valid']=bool(f2p) and signal and after['rc']==0 and passes
            report['reason']='attributable before failure and named after passes' if report['valid'] else 'oracle or required named case evidence missing'
            if report['valid']:
                repeat,out=step('before-repeat-reset',['git','-C','/testbed','apply','-R','/embedeval-run/fix.diff'])
                deterministic=repeat['rc']==0; repeats=[]
                if deterministic:
                    for i in range(3):
                        result,repeat_out=step('before-repeat-'+str(i+1),['run_tests']);repeats.append(result)
                        if oracle=='runtime':
                            deterministic &= result['rc']==1 and all(result['cases'].get(n) in ('failed','error') for n in f2p)
                        elif oracle=='compile':
                            deterministic &= result['rc']==2 and bool(compile_attribution(repeat_out,cfg['compile_symbols']))
                        else:
                            deterministic &= result['rc']==2 and bool(config_attribution(repeat_out,cfg['config_diagnostics']))
                report['before_repeats']=repeats;report['deterministic_before']=deterministic
                report['valid'] &= deterministic
                if not deterministic:report['reason']='before-state failures are not repeatable'
        print(iid+': '+report['reason'])
        return _finish(iid,report)
    except Exception as e:
        report['reason']=f'{type(e).__name__}: {e}';return _finish(iid,report)
    finally:
        local_overlay.cleanup()
        au.remove(work)
