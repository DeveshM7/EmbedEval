"""Contiki-NG packaging and validation using shared EmbedEval receipts/builds."""
from __future__ import annotations
import hashlib, json, os, shutil, subprocess, time
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
    payload['ci_sources']=rec.get('ci_sources',{})
    payload['inventory_note']='Only affected supported runnable units; absent or guarded selections must be held.'
    omissions=[]
    for field in ('test_patch','production_diff'):
        def summarize(match):
            value=match[1]
            digest=hashlib.sha256(value.encode()).hexdigest()
            omissions.append({'field':field,'hex_characters':len(value),'sha256':digest})
            return '"'+value[:128]+f' [PROMPT ONLY: {len(value)-256} hex characters omitted; sha256={digest}] '+value[-128:]+'"'
        payload[field]=re.sub(r'"([0-9a-fA-F]{4096,})"',summarize,payload[field])
        if len(payload[field])>120000:
            text=payload[field]
            omitted=len(text)-100000
            payload[field]=text[:60000]+f'\n[PROMPT ONLY: {omitted} characters omitted; read full source at base/merge]\n'+text[-40000:]
            omissions.append({'field':field,'kind':'diff-preview',
                              'omitted_characters':omitted,
                              'sha256':hashlib.sha256(rec[field].encode()).hexdigest()})
    if omissions:
        payload['prompt_omissions']=omissions
        payload['prompt_omission_note']='Prompt omissions are explicitly listed. Original patches are unchanged; use read_file at base/merge for full upstream data.'
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

# Upstream CI images (docker.io/contiker/contiki-ng) for Cooja, by base commit
# date. Ant era (to 2022-11): the last image of each Java era, i386 before 2022.
# Gradle era: the newest image published on or before the base, since each
# Gradle wrapper runs only on the Java of its time (tags recorded 2026-11-05).
COOJA_ERA_IMAGES=(('0000-00-00','5310e7a'),                                         # Ant, Java 8 (2019-10)
                  ('2020-01-01','66cbc3434'),                                       # Ant, Java 8 (2021-04)
                  ('2022-01-01','d0c6c3ce8ec9aecfdaa95cc5222e19d1ce3eeaadfdd6727b0fa757e10e5c7f17'))  # Ant, Java 11
GRADLE_ERA_START='2022-12-01'
GRADLE_ERA_IMAGES=(('2022-12-02','149f1452f352781d937f1f5792df9c85b7aaeb706aed857d3125333f64d5410f'),
                   ('2022-12-07','fc6fffa82f19351e3de2135d5a1eeff22e4ba1322f53200a8a3d81b40856aeac'),
                   ('2023-06-07','85035503fab4d3b1bc80b7ccf746d3ca1a93a6ee78abe707f8a3347929210595'),
                   ('2023-06-19','a33da7fb057707eda4c3e34d7723d1a5269de3ac2015e2e187b8c7cf4f504679'),
                   ('2023-07-05','5f7b39908d617b353d4e8c1a4e6125d0ce32941244aded3024db91f5f4bd3b8d'),
                   ('2023-07-20','2df41a8f187031d4ed5176bc0f788e71310e58bd3fa4581b8faf7eb6875ca2d9'),
                   ('2023-07-24','f4af71088124d270aa3e935abb56796761c4dd114efcaa5c63cb98e42475102c'),
                   ('2023-08-14','82fdb2217e9649ed4b74bd73de3067001195fc10e980a8cc661644a35fcc925a'),
                   ('2023-08-16','73287ff991dddc4ac06b5c23bf06e54e90e6810b478e86dc8fd2c116e5da98bc'),
                   ('2023-08-30','8f38c24bc0c292a0115b36eb9aad1efa4dddfd5aeecc12693c30cc170fc65a54'),
                   ('2023-09-09','904f00f7dc1c51579f75b449f7bd3252204527e96372dca8b4b37173871c149b'),
                   ('2023-09-20','f29f243e96614a0c7b478ec6d9b9b826f1faba36611233936611f97360e542e0'),
                   ('2023-10-04','0ffd8e618644674352d2ccf3fec8ae5c24de912f24082005a0b4f118f255059f'),
                   ('2023-10-13','d027656a33abf9dc2be7e03d0e85f18442abac8c8136a5874c27781bd9967dc2'),
                   ('2023-11-08','1918f72fa63b70e5fd6c63792b77d596b38ccab28a46c2013b0f88ba4e3faf88'),
                   ('2023-12-22','e6d13e03c458962df9bdeba46547a7aea51c3dff89b966623fd777ce56649e73'),
                   ('2023-12-27','3366352bfc0502a9e69b6b505735ff30682b904cd2d79ad3d5a5686756241654'),
                   ('2024-02-14','006114161f257f5190982b85670c87e5cf4069eb4a4c66ff38fc7f56098f556e'),
                   ('2024-05-06','a957f8bb66c7e7d683a24db1e352a8f2b5280e6d07661e40a42ef5640d101d13'),
                   ('2024-09-02','c9a0e2c8af3f6fa0feb88bb642c471fb72c2ba952f4eae404d993712d603ee44'),
                   ('2024-11-06','9eb427ae8fb01c22b71f5abbce789626ea9f92216c5e245476f48be8ee843a3b'),
                   ('2026-06-22','3f04d86f92db43e3ee1abdb2d3e4e5a5dd63ad5f49f6da2d4d7ced891be67aaf'),
                   ('2026-07-10',au.CONTIKI_NG_CURRENT_IMAGE))

def cooja_era_image(base_date):
    if base_date>=GRADLE_ERA_START:
        return [tag for day,tag in GRADLE_ERA_IMAGES if day<=base_date][-1]
    return [tag for start,tag in COOJA_ERA_IMAGES if start<=base_date][-1]

class NotObservable(ValueError):
    """Deterministic: this PR has nothing to observe under the requested profile."""

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
        if not available:raise NotObservable('no supported affected upstream runnable unit; candidate held')
        profiles={u['profile'] for u in available.values()}
        if profile is None:
            if len(profiles)!=1:raise NotObservable('observation requires an explicit profile for mixed affected units')
            profile=next(iter(profiles))
        if profile not in rec['profiles']:raise NotObservable('unknown observation profile')
        if rec['nested_merge_count']:raise NotObservable('release/merged-PR rollup needs attribution review')
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
    if any(u.get('kind')=='cooja' for u in cfg['units']):
        cfg['ci_image']=cooja_era_image(candidates.git('log','-1','--format=%cs',rec['base_commit']).strip())
        # Before Cooja nested it, MSPSim (Sky/Z1 motes) was its own gitlink; the
        # image initializes it only where it is one at the base.
        cfg['submodules']=sorted({*cfg['submodules'],'tools/mspsim'})
    cfg['after_required']=[name for unit in cfg['units'] for name in candidates.case_names(unit)]
    cfg['enrich_version']=candidates.ENRICH_VERSION
    meta={'instance_id':iid,'project':'contiki-ng','pipeline':'contiki-ng','repo':candidates.REPO,
          'base_commit':rec['base_commit'],'fix_commit':rec['fix_commit'],'test_commit':rec['fix_commit'],
          'problem_statement':statement,'fail_to_pass':cfg['fail_to_pass'],'pass_to_pass':cfg['pass_to_pass'],
          'after_required':cfg['after_required'],
          'files_changed_by_fix':rec['source_files'],'pr_url':rec['url'],'run_command':'run_tests',
          'apptainer_image':iid+'.sif','generated':True,'oracle':cfg['oracle'],'architecture':'x86_64',
          'observation_only':bool(observed and verdict is None)}
    write_recipe(dest,cfg,meta,patch)
    (dest/'metadata.json').write_text(json.dumps(meta,indent=2)+'\n')
    return dest

# What run_tests reads inside the image (bench.json). The expected lists,
# oracle, compile expectations and evidence stay on the host in instance.json.
BENCH_UNIT_FIELDS=('id','kind','directory','binary','make_options','suite_directory','scenario','entry','submodules')

def bench(cfg):
    units=[]
    for u in cfg['units']:
        b={k:u[k] for k in BENCH_UNIT_FIELDS if k in u}
        if 'cases' in u:b['cases']=[{'id':c['id'],'description':c['description']} for c in u['cases']]
        units.append(b)
    return {'units':units,'submodules':cfg.get('submodules',[])}

def write_recipe(dest,cfg,meta,test_patch):
    """The recipe files of an instance: instance.json (host), bench.json
    (image), run_tests.py and instance.def. A compile oracle gets its
    expectations derived from the fix here."""
    if cfg.get('oracle')=='compile':
        cfg['compile_expectations']=compile_expectations(meta['base_commit'],meta['fix_commit'],
                                                         meta['files_changed_by_fix'],test_patch,cfg['units'])
        if not any(e['symbols'] or e['headers'] for e in cfg['compile_expectations'].values()):
            raise ValueError('compile oracle: the tests use nothing the fix introduces')
    (dest/'instance.json').write_text(json.dumps(cfg,indent=2)+'\n')
    (dest/'bench.json').write_text(json.dumps(bench(cfg),indent=2)+'\n')
    shutil.copyfile(TEMPLATES/'run_tests.py',dest/'run_tests.py')
    definition=(TEMPLATES/'instance.def.tmpl').read_text()
    for key,value in {'@@INSTANCE_ID@@':meta['instance_id'],'@@REPO@@':candidates.REPO,'@@BASE_COMMIT@@':meta['base_commit']}.items():definition=definition.replace(key,value)
    (dest/'instance.def').write_text(definition)

def render(source,out_root):
    """An existing (validated or promoted) instance under the current
    templates: its instance.json, metadata.json and test_patch.diff, with the
    recipe written again. A revalidation rebuilds every instance this way."""
    from generate_instance import check_applies
    source=Path(source);cfg=json.loads((source/'instance.json').read_text())
    meta=json.loads((source/'metadata.json').read_text());patch=(source/'test_patch.diff').read_text()
    iid=meta['instance_id']
    if source.name!=iid or meta.get('pipeline')!='contiki-ng':raise ValueError(f'{source}: not a Contiki-NG instance folder')
    if cfg.get('observe'):raise ValueError(f'{iid}: an observation, not an instance')
    dest=Path(out_root)/iid
    if dest.resolve()==source.resolve():raise ValueError('render into a different folder')
    candidates.ensure_commits(meta['base_commit'],meta['fix_commit'])
    dest.mkdir(parents=True,exist_ok=True)
    (dest/'test_patch.diff').write_text(patch)
    check_applies(patch,meta['base_commit'],dest,candidates.clone())
    cfg.pop('compile_expectations',None)
    write_recipe(dest,cfg,meta,patch)
    (dest/'metadata.json').write_text(json.dumps(meta,indent=2)+'\n')
    return dest

C_WORDS={'int','void','char','long','short','unsigned','signed','struct','union','enum','const','volatile',
         'static','return','sizeof','else','for','while','switch','case','break','continue','default','goto',
         'typedef','extern','inline','bool','true','false','NULL','uint8_t','uint16_t','uint32_t','uint64_t',
         'int8_t','int16_t','int32_t','int64_t','size_t'}
STRING=re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
COMMENT=re.compile(r'/\*.*?\*/|//.*$')
INCLUDE=re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]')

def code_words(line):
    """Identifiers on a C line, outside comments and string literals."""
    text=COMMENT.sub(' ',STRING.sub(' ',line))
    return set(re.findall(r'\b[A-Za-z_]\w{2,}\b',text))-C_WORDS

def uncommented(lines):
    """The lines with block comments that span lines removed."""
    out=[];inside=False
    for line in lines:
        text=''
        while line:
            if inside:
                end=line.find('*/')
                if end<0:line='';break
                line=line[end+2:];inside=False
            else:
                start=STRING.sub(lambda m:' '*len(m[0]),line).find('/*')
                if start<0:text+=line;break
                text+=line[:start]+' ';line=line[start+2:];inside=True
        out.append(text)
    return out

def compile_expectations(base,fix,fix_files,test_patch,units):
    """What each unit's before-build must fail on, derived from the fix: the
    identifiers the code lines the tests add use that the fix introduces
    (declared by its added production lines, absent from the whole base
    tree), and the
    headers it creates that the tests include. A unit's build is attested when
    its errors name every such identifier, or report such a header missing
    (GCC stops at the #include). Preprocessor conditions, comments and
    strings are not code, so their names are not expected."""
    rows=candidates.git('diff','--unified=0',base,fix,'--',*fix_files)
    created=[];path=None;new=False;lines=[]
    for line in rows.splitlines():
        if line.startswith('--- '):new=line=='--- /dev/null'
        elif line.startswith('+++ b/'):
            path=line[6:]
            if new and path.endswith('.h'):created.append(path)
        elif line.startswith('+') and not line.startswith('+++') and path and path.endswith(('.c','.h')):
            lines.append(line[1:])
    # only what the fix declares: a name it merely uses (INT64_MAX from the
    # C library) is not missing before it
    added=candidates.declared_identifiers(uncommented(lines))
    present=set()
    if added:
        r=subprocess.run(['git','-C',str(candidates.clone()),'grep','-h','-o','-w','-F',
                          *[a for w in sorted(added) for a in ('-e',w)],base,'--'],
                         capture_output=True,text=True,errors='replace')
        present=set(r.stdout.split())
    introduced=added-present
    tests={};path=None
    for line in test_patch.splitlines():
        if line.startswith('+++ b/'):path=line[6:]
        elif path and line.startswith('+') and not line.startswith('+++'):tests.setdefault(path,[]).append(line[1:])
    out={}
    for unit in units:
        sources=[s for s in unit.get('sources',[]) if s.endswith(('.c','.h')) and s in tests]
        symbols=set();headers=set()
        # a name the tests declare themselves (a local, a helper) is not missing
        added_test=uncommented([l for s in sources for l in tests[s]])
        own=candidates.test_declared(added_test)
        for s in sources:
            for line in uncommented(tests[s]):
                m=INCLUDE.match(line)
                if m:
                    headers|={h for h in created if h==m[1] or h.endswith('/'+m[1])};continue
                if line.lstrip().startswith('#'):continue
                symbols|=(code_words(line)&introduced)-own
        out[unit['id']]={'symbols':sorted(symbols),'headers':sorted(headers)}
    return out

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

MISSING_HEADER=re.compile(r'fatal error: ([\w./+-]+\.h): No such file or directory')

def config_attribution(output,diagnostics):
    failure_lines=[line for line in output.splitlines() if re.search(r'error:|fatal error:|undefined reference|make(?:\[\d+\])?: \*\*\*|failed',line,re.I)]
    return next((d for d in diagnostics if any(d in line for line in failure_lines)),None)

def protected_test_change(patch):
    return bool(re.search(r'^(?:diff --git a/|--- a/|\+\+\+ b/)(?:tests/|regression-tests/|tools/cooja/)',patch,re.M))

# Every before run judged the same way, this many times (the first one
# included): a VALID instance's before state is deterministic.
DETERMINISM_RUNS=5
NETWORK_PROBE=('import socket,sys\n'
               'try:\n socket.getaddrinfo("github.com",443);sys.exit(1)\n'
               'except OSError:pass\n'
               's=socket.socket();s.settimeout(5)\n'
               'sys.exit(1 if s.connect_ex(("140.82.112.3",443))==0 else 0)\n')

def unit_log_name(unit,phase):
    return re.sub(r'[^A-Za-z0-9_.-]','_',unit['id'])+'.'+phase+'.log'

def compile_attestation(cfg,output,details):
    """(per-unit attestation, None) when every native unit's before-build
    fails exactly as the fix-derived compile_expectations say: a unit with
    expectations fails and its errors name every expected identifier, or
    report an expected (fix-created) header missing; a unit without any
    builds. Otherwise (None, why)."""
    from validate_instance import error_text
    expectations=cfg.get('compile_expectations')
    if not expectations:return None,'compile oracle without derived compile_expectations'
    attested={}
    for unit in cfg['units']:
        if unit.get('kind')!='native':return None,'compile oracles are attested on native units only'
        e=expectations.get(unit['id'],{'symbols':[],'headers':[]})
        failed=(unit['id']+': native build failed') in output
        if not (e['symbols'] or e['headers']):
            if failed:return None,f"{unit['id']}: build fails on nothing the fix introduces"
            continue
        if not failed:return None,f"{unit['id']}: builds before the fix"
        log=Path(details)/unit_log_name(unit,'build')
        text=log.read_text(errors='replace') if log.exists() else ''
        missing=[h for h in MISSING_HEADER.findall(text) if any(x==h or x.endswith('/'+h) for x in e['headers'])]
        if missing:
            attested[unit['id']]={'header':missing[0]};continue
        errors=error_text(text)
        absent=[x for x in e['symbols'] if not re.search(r'(?<![\w])'+re.escape(x)+r'(?![\w])',errors)]
        if absent or not e['symbols']:
            return None,f"{unit['id']}: before-build errors do not name {absent or e['headers']}"
        attested[unit['id']]={'symbols':e['symbols']}
    return attested,None

def leak_check(meta,image,patch,test_patch,cmd):
    """contiki_ng_leakcheck on the image's files, and a network probe under
    the isolation every run uses. `patch` is the upstream fix (None: read it
    from the clone, as when another patch is validated)."""
    import contiki_ng_leakcheck as leakcheck
    facts=leakcheck.fix_facts(candidates.clone(),meta,patch if patch is not None else fix_patch(meta),test_patch)
    sandbox=au.extract_local_sandbox(image)
    try:
        record=leakcheck.scan(sandbox,facts)
    finally:
        if not au.is_i386_sif(image):au.remove_local_sandbox(image)
    probe=subprocess.run(cmd+['python3','-c',NETWORK_PROBE],capture_output=True,text=True,timeout=120)
    record['network']='isolated' if probe.returncode==0 else 'open'
    if probe.returncode:
        record['ok']=False;record['findings'].append('network: the container reaches the network')
    record['statement_sha256']=hashlib.sha256((meta.get('problem_statement') or '').encode()).hexdigest()
    return record

def validate(iid,patch_override=None,timeout=1800,verbose=False,determinism=DETERMINISM_RUNS):
    from validate_instance import observe_lists, build_errors, _finish
    folder=instances.instance_dir(iid);meta=instances.load_metadata(iid)
    cfg=json.loads((folder/'instance.json').read_text()); image=au.instance_image(iid)
    report={'instance_id':iid,'image':str(image),'valid':False,'observe':bool(cfg.get('observe')),
            'recipe_sha256':au.recipe_fingerprint(folder),'profile':cfg.get('profile'),'oracle':cfg.get('oracle','runtime'),
            'validated_at':time.strftime('%Y-%m-%dT%H:%M:%S')}
    if not image.exists():report['reason']='image not built';return _finish(iid,report)
    stamp=time.strftime('%Y%m%d-%H%M%S')+'-'+str(os.getpid())
    work=au.path('runs',iid+'-'+stamp,'x').parent
    local_overlay=tempfile.TemporaryDirectory(prefix='embedeval-contiki-',dir='/tmp')
    overlay=Path(local_overlay.name)/'overlay';overlay.mkdir()
    logs=au.path('logs',iid,'validate-'+stamp,'x').parent;logs.mkdir(parents=True,exist_ok=True)
    report['logs']=str(logs)
    # No network for any step: what the tests do offline is what an agent's patch is graded on.
    cmd=au.exec_args(image,binds={work:'/embedeval-run'},overlay=overlay,network=False)
    def step(name,args):
        start=time.monotonic()
        details=logs/(name+'-details') if args==['run_tests'] else None
        step_cmd=cmd
        if details is not None:
            details.mkdir()
            step_cmd=au.exec_args(image,binds={work:'/embedeval-run',details:'/testbed/.embedeval/logs'},overlay=overlay,network=False)
        try:
            r=subprocess.run(step_cmd+args,capture_output=True,text=True,errors='replace',timeout=timeout)
            rc,out=r.returncode,r.stdout+r.stderr
        except subprocess.TimeoutExpired as e:
            rc='timeout';out=(e.stdout or b'');out=out.decode(errors='replace') if isinstance(out,bytes) else out
        (logs/(name+'.log')).write_text(out)
        if verbose:print(out)
        return {'rc':rc,'seconds':round(time.monotonic()-start,3),'cases':parse_results(out),
                'build_errors':build_errors(out),'details':str(details) if details is not None else None},out
    try:
        patch=Path(patch_override).read_text() if patch_override else fix_patch(meta)
        if protected_test_change(patch):report['reason']='production fix overlaps protected upstream tests';return _finish(iid,report)
        if not patch.strip():report['reason']='empty production fix';return _finish(iid,report)
        # Nothing an agent must not see, before any test runs.
        leak=report['leak_check']=leak_check(meta,image,None if patch_override else patch,
                                             (folder/'test_patch.diff').read_text(),cmd)
        if not leak['ok']:
            report['reason']='leak check: '+'; '.join(leak['findings'])[:600]
            print(iid+': '+report['reason']);return _finish(iid,report)
        before,out1=step('before',['run_tests']);report['before']=before
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
            def judge(result,out):
                """(the before state's signature, why it is not the oracle's)"""
                if oracle=='runtime':
                    bad=[n for n in f2p if result['cases'].get(n) not in ('failed','error')]
                    why=None if result['rc']==1 and not bad else f"fail_to_pass did not fail: {bad or result['rc']}"
                    attested=None
                elif oracle=='compile':
                    attested,why=(compile_attestation(cfg,out,result['details']) if result['rc']==2
                                  else (None,f"the before run did not fail to build (rc={result['rc']})"))
                else:
                    attested=config_attribution(out,cfg.get('config_diagnostics',[])) if result['rc']==2 else None
                    why=None if attested else 'no configured diagnostic in the before build'
                not_passing=[n for n in p2p if result['cases'].get(n)!='passed']
                if why is None and not_passing:why=f'pass_to_pass did not pass before: {not_passing}'
                return {'rc':result['rc'],'cases':result['cases'],'attested':attested},why
            first,why=judge(before,out1)
            if oracle=='compile' and first['attested']:report['before']['compile_attested']=first['attested']
            passes=all(after['cases'].get(n)=='passed' for n in cfg.get('after_required',f2p+p2p))
            report['valid']=bool(f2p) and why is None and after['rc']==0 and passes
            report['reason']=('attributable before failure and named after passes' if report['valid'] else
                              why or 'required named cases did not pass with the fix')
            if report['valid']:
                repeat,out=step('before-repeat-reset',['git','-C','/testbed','apply','-R','/embedeval-run/fix.diff'])
                deterministic=repeat['rc']==0; repeats=[]; differences=[]
                # Files the fix created are gone again, but make would keep the
                # binary built from them: rebuild the native units from scratch.
                for unit in cfg['units']:
                    if deterministic and unit.get('kind')=='native':
                        cleaned,_=step('before-repeat-clean',['make','-C','/testbed/'+unit['directory'],'TARGET=native','clean'])
                        deterministic=cleaned['rc']==0
                if deterministic:
                    for i in range(determinism-1):
                        result,repeat_out=step('before-repeat-'+str(i+1),['run_tests']);repeats.append(result)
                        signature,repeat_why=judge(result,repeat_out)
                        if repeat_why or signature!=first:
                            differences.append({'run':i+2,'why':repeat_why or 'results differ from the first before run'})
                deterministic&=not differences
                report['before_repeats']=repeats;report['deterministic_before']=deterministic
                report['determinism']={'runs':1+len(repeats),'ok':deterministic and 1+len(repeats)>=determinism,
                                       'differences':differences}
                report['valid'] &= report['determinism']['ok']
                if not report['valid']:report['reason']='before-state failures are not repeatable'
        if not report['valid'] and (after['rc'] in (2,3,'timeout') or before['rc']=='timeout'):
            report['status']='hold'
            report['reason']='infrastructure/profile hold: '+report['reason']
        print(iid+': '+report['reason'])
        return _finish(iid,report)
    except Exception as e:
        report['reason']=f'{type(e).__name__}: {e}';return _finish(iid,report)
    finally:
        local_overlay.cleanup()
        au.remove(work)
