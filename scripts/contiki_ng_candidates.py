#!/usr/bin/env python3
"""Source-derived Contiki-NG PR discovery and runnable inventory."""
from __future__ import annotations
import argparse, ast, json, posixpath, re, subprocess
from pathlib import Path
import layout
from functools import lru_cache
from contiki_ng_inputs import closure

REPO='https://github.com/contiki-ng/contiki-ng'
ENRICH_VERSION=7
TEST_ROOTS=('tests/','regression-tests/')
SUPPORTED_SIM_SUITES=('07-simulation-base','09-ipv6','13-ieee802154','21-security-protocols')

def clone(): return layout.contiki_ng_clone()
def candidates_dir(): return layout.candidates()/'contiki-ng'
def enriched_dir(): return layout.enriched() if layout.run_dir() else candidates_dir()/'enriched'
def git(*args, check=True):
    r=subprocess.run(['git','-C',str(clone()),*args],capture_output=True,text=True,errors='replace')
    if check and r.returncode: raise RuntimeError(r.stderr.strip())
    return r.stdout

def show(rev,path):
    r=subprocess.run(['git','-C',str(clone()),'show',f'{rev}:{path}'],capture_output=True,text=True,errors='replace')
    return r.stdout if r.returncode==0 else None

def ensure_clone():
    if not clone().exists():
        clone().parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(['git','clone','--bare',REPO+'.git',str(clone())],check=True)

def classify(rec):
    if not rec.get('merged_at'): return False, 'not merged'
    if rec.get('title','').lower().startswith('revert'): return False, 'revert'
    ensure_commits(rec['base_commit'],rec['fix_commit'])
    paths=git('diff','--name-only',rec['base_commit'],rec['fix_commit']).splitlines()
    return (True, '') if any(p.startswith(TEST_ROOTS) for p in paths) else (False, 'no upstream test changes')

def ensure_commits(*commits):
    ensure_clone()
    for sha in commits:
        if not re.fullmatch('[0-9a-f]{40}',sha): raise ValueError('full commit SHA required')
        if subprocess.run(['git','-C',str(clone()),'cat-file','-e',sha+'^{commit}'],capture_output=True).returncode:
            git('fetch','origin',sha)

def api(path):
    r=subprocess.run(['gh','api',path],capture_output=True,text=True)
    if r.returncode: raise RuntimeError(r.stderr.strip())
    return json.loads(r.stdout)

def compact(pr):
    return {'number':pr['number'],'title':pr['title'],'body':pr.get('body'),
            'merged_at':pr.get('merged_at'),'merge_commit_sha':pr.get('merge_commit_sha'),
            'url':pr.get('html_url',f"{REPO}/pull/{pr['number']}")}

def find_pr(pr):
    cache=candidates_dir()/'fetched.json'
    rows=json.loads(cache.read_text()) if cache.exists() else []
    found=next((r for r in rows if r['number']==pr),None)
    if found is None:
        found=compact(api(f'repos/contiki-ng/contiki-ng/pulls/{pr}'))
    if not found.get('merged_at'): raise ValueError(f'PR {pr} is not merged')
    ensure_commits(found['merge_commit_sha'])
    parents=git('show','-s','--format=%P',found['merge_commit_sha']).split()
    if len(parents)!=2: raise ValueError('PR merge shape needs explicit mapping')
    return {'body':None,**found,'base_commit':parents[0],'fix_commit':found['merge_commit_sha']}

def files_at(rev): return git('ls-tree','-r','--name-only',rev).splitlines()
C_TOKEN=re.compile(r'/\*[\s\S]*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|\S')

def c_tokens(source):
    return [m[0] for m in C_TOKEN.finditer(source) if not m[0].startswith(('/*','//'))]

def balanced_end(tokens,start,left,right):
    level=1
    for j in range(start+1,len(tokens)):
        if tokens[j]==left:level+=1
        elif tokens[j]==right:level-=1
        if level==0:return j+1
    return None

def native_body_evidence(source,path,entries):
    """Unconditional definitions/calls, not an assertion that the target builds."""
    matches=[m for m in C_TOKEN.finditer(source) if not m[0].startswith(('/*','//'))]
    tokens=[m[0] for m in matches];stack=[];branches=[];ranges=[];defined=set()
    names={case['id'] for case in entries};malformed=False
    for i,token in enumerate(tokens):
        if token=='#' and i+1<len(tokens):
            directive=tokens[i+1]
            if directive in ('if','ifdef','ifndef','elif','else'):
                if directive in ('elif','else'):
                    if not stack:malformed=True;continue
                    branches[stack.pop()]['end']=i
                start=matches[i].start();end=source.find('\n',start)
                branches.append({'start':i,'end':len(tokens),'source':path,
                                 'line':source.count('\n',0,start)+1,
                                 'directive':source[start:end if end>=0 else None]})
                stack.append(len(branches)-1)
            elif directive=='endif':
                if stack:branches[stack.pop()]['end']=i
                else:malformed=True
        if token not in ('UNIT_TEST','UNIT_TEST_REGISTER','UNIT_TEST_RUN') or tokens[i+1:i+2]!=['(']:continue
        if i+2>=len(tokens) or tokens[i+2] not in names:continue
        end=balanced_end(tokens,i+1,'(',')')
        if end is None:malformed=True;continue
        if token=='UNIT_TEST':
            if tokens[end:end+1]!=['{']:continue
            end=balanced_end(tokens,end,'{','}')
            if end is None:malformed=True;continue
            defined.add(tokens[i+2])
        ranges.append((i,end))
    relevant=[b for b in branches if any(start<b['end'] and end>b['start'] for start,end in ranges)]
    def describe(branch):return {k:branch[k] for k in ('source','line','directive')}
    unresolved=sorted(names-defined)
    enabled=True if names and not relevant and not unresolved and not stack and not malformed else None
    return {'enabled':enabled,'method':'source-unconditional-test-bodies' if enabled else 'compiler-proof-pending',
            'sources':[path],'guards':[describe(b) for b in relevant],
            'configuration_guards':[describe(b) for b in branches if b not in relevant],
            'unresolved_definitions':unresolved}

def cases(source):
    tokens=c_tokens(source);registered={};invoked=[]
    for i,token in enumerate(tokens):
        if tokens[i+1:i+2]!=['('] or i+3>=len(tokens):continue
        name=tokens[i+2]
        if not re.fullmatch(r'[A-Za-z_]\w*',name):continue
        if token=='UNIT_TEST_RUN' and tokens[i+3]==')':invoked.append(name)
        if token!='UNIT_TEST_REGISTER' or tokens[i+3]!=',':continue
        j=i+4;parts=[]
        while j<len(tokens) and tokens[j].startswith('"'):
            parts.append(ast.literal_eval(tokens[j]));j+=1
        if parts and j<len(tokens) and tokens[j]==')':registered[name]=''.join(parts)
    return [{'id':n,'description':registered[n]} for n in dict.fromkeys(invoked) if n in registered]

def source_functions(source):
    """Recognize direct C calls and definitions; do not interpret macro expansion."""
    tokens=c_tokens(source);calls=set();provided=set();depth=0
    for i,token in enumerate(tokens):
        if token=='{':depth+=1
        elif token=='}':depth=max(0,depth-1)
        if not re.fullmatch(r'[A-Za-z_]\w*',token) or tokens[i+1:i+2]!=['(']:continue
        j=balanced_end(tokens,i+1,'(',')')
        if j is None:continue
        if tokens[j:j+1]==['{']:provided.add(token)
        elif depth and (i==0 or tokens[i-1] not in ('.','>')):calls.add(token)
    return calls,provided

def before_compile_evidence(unit,requirements,read):
    evidence={'method':'project-source-absence-and-direct-test-call',
              'status':'unknown','required_interfaces':[],'requirements':[]}
    # External providers and unresolved source guards are outside this strict
    # static check. Unknown does not mean that the target compiles or runs.
    if (unit['kind']!='native' or unit.get('submodules') or
        unit['profile_evidence'].get('enabled') is not True):return evidence
    relevant=[r for r in requirements if r['kind']=='function' and r['at_base'] is False
              and set(r['test_files']).intersection(unit['sources'])]
    if not relevant:return evidence
    calls=set();provided=set();support=[]
    for path in unit['sources']:
        if not path.startswith(TEST_ROOTS) or not path.endswith(('.c','.h','/Makefile')):continue
        text=read(path);support.append(text)
        if path.endswith(('.c','.h')):
            used,defined=source_functions(text);calls.update(used);provided.update(defined)
    support='\n'.join(support+unit.get('make_options',[]))
    for requirement in relevant:
        symbol=requirement['symbol']
        aliases=re.search(r'^\s*#\s*define\s+'+re.escape(symbol)+r'\b|\b'+re.escape(symbol)+r'(?:\([^)]*\))?\s*=(?!=)|\(\s*\*\s*'+re.escape(symbol)+r'\s*\)',support,re.M)
        if symbol in calls and symbol not in provided and not aliases:
            evidence['requirements'].append(requirement)
    evidence['required_interfaces']=sorted({r['symbol'] for r in evidence['requirements']})
    if evidence['required_interfaces']:evidence['status']='source_blocked'
    return evidence

def logical_lines(text): return re.sub(r'\\\n',' ',text).splitlines()
def suite_entries(text):
    entries=[]
    for line in logical_lines(text):
        m=re.match(r'^\s*EXAMPLES\s*[:?+]?=\s*(.*)',line)
        if m:entries.extend(m[1].split())
    return entries

def inventory(rev):
    paths=files_at(rev); units=[]; holds=[]
    @lru_cache(maxsize=None)
    def read(path):return show(rev,path) or ''
    # Native unit Makefiles are authoritative, including old units later run by Cooja.
    for path in paths:
        if not path.startswith(TEST_ROOTS) or not path.endswith('/Makefile'): continue
        directory=posixpath.dirname(path); make=read(path)
        sources=[p for p in paths if posixpath.dirname(p)==directory and p.endswith('.c')]
        native=[]
        for source in sources:
            entries=cases(read(source))
            if entries:native.append((source,entries))
        if not native: continue
        projects=re.search(r'^\s*CONTIKI_PROJECT\s*[:?+]?=\s*(.*)',make,re.M)
        if projects: targets=projects[1].split()
        else:
            rule=re.search(r'^all:\s*(.*)',make,re.M); targets=rule[1].split() if rule else []
        options=[[]]
        suite=posixpath.dirname(directory)
        suite_make=read(suite+'/Makefile')
        matching=[entry for entry in suite_entries(suite_make) if entry.startswith(directory+'/native:')]
        if matching:
            options=[]
            for entry in matching:
                assignments=[v for v in entry.split(':')[2:] if re.match(r'^\w+=',v)]
                if assignments not in options: options.append(assignments)
        for source, entries in native:
            binary=Path(source).stem
            if binary not in targets:
                if len(targets)==1 and len(native)==1: binary=targets[0]
                else:
                    holds.append({'source':source,'reason':'ambiguous upstream binary target'});continue
            if any('$' in t for t in (binary,*sum(options,[]))):
                holds.append({'source':source,'reason':'dynamic Make target/options'});continue
            for opts in options:
                suffix=':'+','.join(opts) if opts else ''
                runners=[posixpath.normpath(suite+'/'+e.split(':')[1]) for e in matching if e.split(':')[1].startswith('./')]
                consumers=[source,*runners]
                if matching:consumers.append(suite+'/Makefile')
                consumer_text='\n'.join([make,suite_make,*(read(p) for p in runners)])
                for helper in ('tests/utils.sh','tests/Makefile.compile-test','regression-tests/Makefile.compile-test'):
                    if helper.split('/')[-1] in consumer_text:
                        consumers.append(helper)
                units.append({'id':directory+'/'+binary+suffix,'kind':'native','profile':'native',
                              'case_source':source,
                              'directory':directory,'binary':binary,'suite_directory':suite,
                              'make_options':opts,'sources':list(dict.fromkeys(consumers)), 'cases':entries,'submodules':[]})
    native_directories={u['directory'] for u in units}
    for path in paths:
        if not path.startswith(TEST_ROOTS) or not path.endswith('/Makefile'): continue
        suite=posixpath.dirname(path);text=read(path)
        if not re.search(r'^\s*RUN_FILE\s*[:?+]?=\s*1',text,re.M):continue
        root_match=re.search(r'^\s*EXAMPLESDIR\s*[:?+]?=\s*(.*)',text,re.M)
        example_root=root_match[1].strip() if root_match else '.'
        if '$' in example_root:
            holds.append({'source':path,'reason':'dynamic example directory'});continue
        for entry in suite_entries(text):
            fields=entry.split(':')
            if len(fields)<2 or not fields[0].endswith('/native'):continue
            directory=posixpath.normpath(suite+'/'+example_root+'/'+fields[0].rsplit('/',1)[0])
            if directory in native_directories:continue
            runner=posixpath.normpath(suite+'/'+fields[1])
            source_paths=[p for p in paths if posixpath.dirname(p)==directory and
                          (p.endswith(('.c','.h')) or Path(p).name.startswith('Makefile'))]+[runner,path]
            supported=Path(suite).name in ('06-script-base','10-ipv6-nbr','20-packet-parsing')
            units.append({'id':suite+'::'+entry,'kind':'script','profile':'script',
                          'directory':directory,'suite_directory':suite,'entry':entry,
                          'make_options':fields[2:],'sources':source_paths,
                          'cases':[{'id':'script','description':runner}], 'submodules':[],
                          'supported':supported,'hold_reason':None if supported else 'network/service fixture needs worker control'})
    import xml.etree.ElementTree as ET
    for path in paths:
        if not path.startswith(TEST_ROOTS) or not path.endswith('.csc'):continue
        suite=posixpath.dirname(path)
        if '/code' in suite: continue
        try: tree=ET.fromstring(read(path))
        except ET.ParseError:
            holds.append({'source':path,'reason':'unparseable scenario'});continue
        refs=[]
        for field in ('source','scriptfile'):
            for node in tree.findall('.//'+field):
                value=node.text or ''
                if value.startswith('[CONTIKI_DIR]/'): refs.append(value[14:])
                elif value.startswith('[CONFIG_DIR]/'): refs.append(posixpath.normpath(suite+'/'+value[13:]))
                else: refs.append(posixpath.normpath(suite+'/'+value))
        supported=Path(suite).name in SUPPORTED_SIM_SUITES
        units.append({'id':path,'kind':'cooja','profile':'cooja','directory':suite,
                      'suite_directory':suite,'scenario':Path(path).name,'make_options':[],
                      'sources':[path,*refs], 'cases':[{'id':'scenario','description':tree.findtext('.//title') or path}],
                      'submodules':['tools/cooja','os/net/security/mbedtls','os/net/security/micro-ecc'],
                      'supported':supported,'hold_reason':None if supported else 'profile needs worker control'})
    for unit in units:
        if unit['kind']=='native':
            suite_make=read(unit['suite_directory']+'/Makefile')
            unit['execution_evidence']={
                'method':'upstream-make-registration-and-scenario-references',
                'native_entries':[entry for entry in suite_entries(suite_make)
                                  if entry.startswith(unit['directory']+'/native:')],
                'simulation_consumers':[other['id'] for other in units
                                        if other['kind']=='cooja' and unit['case_source'] in other['sources']],
                'build_verified':False}
        unit.update(closure(unit,paths,read))
        sources=[p for p in unit['sources'] if p.endswith('.c') and p.startswith(TEST_ROOTS)]
        guards=[{'source':p,'line':i,'directive':line.strip()} for p in sources
                for i,line in enumerate(read(p).splitlines(),1)
                if re.match(r'\s*#\s*(?:if|ifdef|ifndef|elif)\b',line)]
        unit['profile_evidence']={'enabled':True if unit['kind']=='native' and sources and not guards else None,
                                  'method':'source-unconditional' if unit['kind']=='native' and sources and not guards else 'compiler-proof-pending',
                                  'sources':sources,'guards':guards}
        if unit['kind']=='native':
            unit['profile_evidence']=native_body_evidence(read(unit['case_source']),unit['case_source'],unit['cases'])
        descriptions=[case['description'] for case in unit['cases']]
        if unit['kind']=='native' and len(set(descriptions))!=len(descriptions):
            unit['supported']=False
            unit['hold_reason']='duplicate runtime descriptions cannot identify individual cases'
            unit['profile_evidence']['enabled']=None
            holds.append({'source':unit['id'],'reason':unit['hold_reason']})
    return units,holds

def select_tests(rec,profile=None):
    changed=set(rec['test_files']); chosen=[]
    # Adding an EXAMPLES registration does not change every consumer of the
    # suite Makefile. Restrict those edits to the entries actually added.
    registration_entries={}
    for path in list(changed):
        if not path.endswith('/Makefile'):continue
        edits=[];current=None
        for line in rec.get('test_patch','').splitlines():
            if line.startswith('+++ b/'):current=line[6:]
            elif current==path and line[:1] in ('+','-') and not line.startswith(('+++','---')):
                value=line[1:].strip().rstrip('\\').strip()
                if value and not value.startswith('#'):edits.append(value)
        if edits and all(re.match(r'[^\s:]+/native:',e) for e in edits):
            changed.remove(path)
            registration_entries[path]=edits

    for u in rec['test_inventory']:
        if profile and u['profile']!=profile:continue
        registration=any(path in u['sources'] and any(e.startswith(u['directory']+'/native:') for e in entries)
                         for path,entries in registration_entries.items())
        if u.get('supported',True) and (changed.intersection(u['sources']) or registration):chosen.append(u)
    return list({u['id']:u for u in chosen}.values())

def case_names(unit):
    return [unit['id']+'::'+c['id'] for c in unit['cases']] if unit['kind']=='native' else [unit['id']]

def added_lines(patch):
    path=None
    for line in patch.splitlines():
        if line.startswith('+++ b/'):
            path=line[6:]
        elif path and line.startswith('+') and not line.startswith('+++'):
            yield path,line[1:]

def added_blocks(patch):
    """Contiguous added lines; context and hunk boundaries cannot join definitions."""
    path=None;block=[]
    for line in patch.splitlines():
        if line.startswith('+++ b/'):
            if block:yield path,block
            path=line[6:];block=[]
        elif path and line.startswith('+') and not line.startswith('+++'):
            block.append(line[1:])
        elif block:
            yield path,block;block=[]
    if block:yield path,block

def compile_requirements(base,production_diff,test_patch):
    """Interfaces introduced by the fix and used on newly added test lines."""
    tests=list(added_lines(test_patch)); found={}
    for path,block in added_blocks(production_diff):
        interfaces=[]
        for line in block:
            macro=re.match(r'\s*#\s*define\s+([A-Za-z_]\w*)\b',line)
            if macro:interfaces.append((macro[1],'macro'))
            inline=re.match(r'\s*(?:[A-Za-z_]\w*|\*)[\w\s*]*\b([A-Za-z_]\w*)\s*\([^;]*\)\s*\{',line)
            if inline:interfaces.append((inline[1],'function'))
        joined='\n'.join(block)
        for match in re.finditer(r'(?m)^([A-Za-z_]\w*)[ \t]*\([^;{}\n]*\)[ \t]*\n[ \t]*\{',joined):
            interfaces.append((match[1],'function'))
        for symbol,kind in interfaces:
            used=sorted({p for p,text in tests if re.search(r'\b'+re.escape(symbol)+r'\b',text)})
            if not used or symbol in found:continue
            # Any occurrence at the base makes absence unproved. This is conservative
            # for common names and declarations, which must be reviewed manually.
            if git('grep','-n','-w','-e',symbol,base,'--',check=False):continue
            found[symbol]={'symbol':symbol,'kind':kind,'at_base':False,
                           'production_file':path,'test_files':used}
    return list(found.values())

def stale_record(rec):
    required=('number','title','body','url','base_commit','fix_commit','test_inventory',
              'test_files','source_files','test_patch','production_diff','profiles',
              'inventory_holds','nested_merge_count','compile_requirements','ci_sources')
    if not isinstance(rec,dict) or rec.get('enrich_version')!=ENRICH_VERSION or any(k not in rec for k in required):return True
    if not isinstance(rec['test_inventory'],list) or not isinstance(rec['test_files'],list) or not isinstance(rec['source_files'],list):return True
    if any(not isinstance(p,str) for p in rec['test_files']+rec['source_files']):return True
    if not isinstance(rec['profiles'],dict) or not isinstance(rec['compile_requirements'],list) or not isinstance(rec['ci_sources'],dict):return True
    return any(not isinstance(u,dict) or not all(k in u for k in ('id','kind','profile','sources','cases','profile_evidence','dependency_evidence','before_compile','before_dependencies'))
               or not isinstance(u['profile_evidence'],dict) or not isinstance(u['sources'],list)
               or not isinstance(u['dependency_evidence'],dict)
               or not all(k in u['dependency_evidence'] for k in ('method','edges','unresolved','build_verified'))
               or not isinstance(u['before_compile'],dict)
               or not isinstance(u['before_compile'].get('required_interfaces'),list)
               or not isinstance(u['before_dependencies'],dict)
               or not isinstance(u['before_dependencies'].get('missing_submodules'),list)
               or (u['kind']=='native' and (not isinstance(u.get('execution_evidence'),dict)
                   or not all(k in u['execution_evidence'] for k in ('method','native_entries','simulation_consumers','build_verified'))))
               or any(not isinstance(p,str) for p in u['sources'])
               or not isinstance(u['cases'],list) for u in rec['test_inventory'])

def enrich(pr):
    r=find_pr(pr);base,fix=r['base_commit'],r['fix_commit']
    files=git('diff','--name-only','--no-renames',base,fix).splitlines()
    tests=[p for p in files if p.startswith(TEST_ROOTS)]
    source=[p for p in files if not p.startswith(TEST_ROOTS)]
    units,holds=inventory(fix)
    nested=[line for line in git('log','--format=%s',base+'..'+fix).splitlines()
            if line.startswith('Merge pull request #')]
    test_patch=git('diff','--binary',base,fix,'--',*TEST_ROOTS)
    production_diff=git('diff','--binary',base,fix,'--','.',':(exclude)tests/',':(exclude)regression-tests/')
    requirements=compile_requirements(base,production_diff,test_patch)
    base_submodules={line.partition('\t')[2] for line in git('ls-tree','-r',base).splitlines()
                     if line.startswith('160000 ')}
    declared_submodules=set(re.findall(r'^\s*path\s*=\s*(.*?)\s*$',show(base,'.gitmodules') or '',re.M))
    @lru_cache(maxsize=None)
    def read(path):return show(fix,path) or ''
    for unit in units:
        unit['before_compile']=before_compile_evidence(unit,requirements,read)
        unit['before_dependencies']={'method':'base-gitlink-and-base-gitmodules',
            'missing_submodules':[p for p in unit.get('submodules',[])
                                  if p not in declared_submodules or p not in base_submodules]}
    return {**r,'repo':REPO,'enrich_version':ENRICH_VERSION,'test_files':tests,'source_files':source,
            'test_inventory':units,'inventory_holds':holds,
            'profiles':{'native':{'architecture':'x86_64','target':'native'},
                        'cooja':{'architecture':'x86_64','seed':1,'run_count':1},
                        'script':{'architecture':'x86_64','target':'native'}},
            'test_patch':test_patch,
            'production_diff':production_diff,
            'compile_requirements':requirements,
            'nested_merge_count':max(0,len(nested)-1),
            'ci_sources':{p:show(base,p) for p in ('.github/workflows/build.yml','.travis.yml') if show(base,p)}}

def check_verdict(v,rec):
    if not isinstance(v,dict):return ['verdict must be an object']
    if stale_record(rec):return ['stale enriched record; re-enrich']
    if v.get('pr')!=rec['number']:return ['PR mismatch']
    if v.get('verdict') not in ('accept','reject','hold'):return ['unknown verdict']
    if v.get('verdict')!='accept':return [] if isinstance(v.get('reason'),str) and v['reason'].strip() else ['reason required']
    if not rec['source_files']:return ['no separate production/configuration fix available']
    if any(p.startswith(TEST_ROOTS) for p in rec['source_files']):return ['production fix overlaps upstream test roots']
    profile=v.get('profile')
    if not isinstance(profile,str) or profile not in rec['profiles']:return ['profile must name a supported execution profile']
    if not isinstance(rec['test_patch'],str) or not rec['test_patch'].strip():return ['no upstream test patch']
    errors=[]; available={u['id']:u for u in select_tests(rec,v.get('profile'))}
    chosen=v.get('units',[])
    if not isinstance(chosen,list) or not chosen or any(not isinstance(x,str) or x not in available for x in chosen):
        return ['units must name runnable affected inventory entries']
    if any(available[name]['profile']!=profile for name in chosen):errors.append('units must match selected profile')
    if any(available[name]['profile_evidence'].get('enabled') is not True for name in chosen):
        errors.append('selected test body needs compiler-backed profile evidence')
    if any(available[name]['before_dependencies']['missing_submodules'] for name in chosen):
        errors.append('selected dependencies cannot be initialized in the before snapshot; state-dependent dependency setup is unsupported')
    names={n for name in chosen for n in case_names(available[name])}
    f2p=v.get('fail_to_pass',[]);p2p=v.get('pass_to_pass',[])
    if not isinstance(f2p,list) or not f2p or not all(isinstance(x,str) and x in names for x in f2p):errors.append('fail_to_pass must name inventory cases')
    if not isinstance(p2p,list) or not all(isinstance(x,str) and x in names for x in p2p):errors.append('pass_to_pass must name inventory cases')
    if errors:return errors
    if set(f2p)&set(p2p):errors.append('F2P and P2P overlap')
    oracle=v.get('oracle','runtime')
    if oracle not in ('runtime','compile','config'):errors.append('unknown oracle')
    blocked={name:available[name]['before_compile']['required_interfaces'] for name in chosen
             if available[name]['before_compile'].get('status')=='source_blocked'}
    if oracle=='runtime' and blocked:
        errors.append('runtime oracle contradicts required interfaces absent from the base: '+
                      ', '.join(sorted({s for symbols in blocked.values() for s in symbols})))
    if any(case in p2p for name in blocked for case in case_names(available[name])):
        errors.append('P2P cases cannot run before a required-interface build failure; omit P2P for blocked units')
    if oracle=='compile':
        symbols=v.get('compile_symbols')
        selected_sources={p for name in chosen for p in available[name]['sources']}
        known={r['symbol'] for r in rec['compile_requirements'] if isinstance(r,dict) and r.get('at_base') is False
               and isinstance(r.get('symbol'),str) and isinstance(r.get('test_files'),list)
               and selected_sources.intersection(r['test_files'])}
        if not isinstance(symbols,list) or not symbols or any(not isinstance(x,str) or x not in known for x in symbols):
            errors.append('compile_symbols must name verified absent added interfaces')
    if oracle=='config':
        diagnostics=v.get('config_diagnostics')
        if not isinstance(diagnostics,list) or not diagnostics or any(not isinstance(x,str) or not x.strip() for x in diagnostics):
            errors.append('config_diagnostics must name expected build diagnostics')
    if oracle in ('compile','config') and (not isinstance(v.get('oracle_attribution'),str) or not v['oracle_attribution'].strip()):
        errors.append('pre-runtime oracle requires causal attribution')
    for field in ('problem_statement','causal_chain','change_type'):
        if not isinstance(v.get(field),str) or not v[field].strip():errors.append(field+' required')
    return errors

def dispatch(args):
    cmd=args.cmd; candidates_dir().mkdir(parents=True,exist_ok=True)
    if cmd=='fetch':
        prs=getattr(args,'pr',None);rows=[]
        if prs: rows=[compact(api(f'repos/contiki-ng/contiki-ng/pulls/{n}')) for n in prs]
        else:
            page=1
            while True:
                data=api(f'repos/contiki-ng/contiki-ng/pulls?state=closed&per_page=100&page={page}')
                rows.extend(compact(p) for p in data if p.get('merged_at'))
                if len(data)<100:break
                page+=1
        since=getattr(args,'since',None);until=getattr(args,'until',None)
        rows=[p for p in rows if p.get('merged_at') and (prs or
              (not since or p['merged_at'][:10]>=since) and (not until or p['merged_at'][:10]<=until))]
        (candidates_dir()/'fetched.json').write_text(json.dumps(rows,indent=2)+'\n')
        print(f'{len(rows)} merged PRs cached');return
    if cmd=='filter':
        rows=json.loads((candidates_dir()/'fetched.json').read_text());kept=[]
        for r in rows:
            rec=find_pr(r['number']); paths=git('diff','--name-only',rec['base_commit'],rec['fix_commit']).splitlines()
            if any(p.startswith(TEST_ROOTS) for p in paths) and not r['title'].lower().startswith('revert'):
                kept.append(rec)
        (candidates_dir()/'candidates.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in kept))
        print(f'{len(kept)} test-touching PRs; runnability is not implied');return
    if cmd=='enrich':
        prs=getattr(args,'pr',None)
        if not prs:prs=[json.loads(l)['number'] for l in (candidates_dir()/'candidates.jsonl').read_text().splitlines()]
        if getattr(args,'limit',None):prs=prs[:args.limit]
        enriched_dir().mkdir(parents=True,exist_ok=True)
        for pr in prs:
            rec=enrich(pr);(enriched_dir()/f'{pr}.json').write_text(json.dumps(rec,indent=2)+'\n')
            print(f'{pr}: {len(select_tests(rec))} affected runnable units')
        return
    if cmd=='selftest':
        assert case_names({'id':'unit','kind':'native','cases':[{'id':'case'}]})==['unit::case']
        print('Contiki-NG identity helper passed');return
    raise ValueError('unsupported command '+cmd)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('cmd',choices=('fetch','filter','enrich','selftest'))
    p.add_argument('--pr',type=int,nargs='+');p.add_argument('--since');p.add_argument('--until');p.add_argument('--limit',type=int)
    dispatch(p.parse_args())
