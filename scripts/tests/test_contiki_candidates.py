"""Regression checks for actual upstream configuration and selection mistakes."""
import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import contiki_ng_candidates as c
import contiki_ng_pipeline as pipeline
import instances

def record():
    unit={'id':'tests/08-native-runs/test-json/test-json','kind':'native','profile':'native',
          'sources':['tests/08-native-runs/test-json/test-json.c'],
          'cases':[{'id':'json_test','description':'JSON test'}],
          'dependency_evidence':{'method':'static-source-closure','edges':[],'unresolved':[],'build_verified':False},
          'before_compile':{'status':'unknown','required_interfaces':[]},
          'before_dependencies':{'missing_submodules':[]},
          'profile_evidence':{'enabled':True,'method':'source-unconditional',
                              'sources':['tests/08-native-runs/test-json/test-json.c'],'guards':[]}}
    return {'number':1,'title':'JSON fix','body':'','url':'https://example.test/1',
            'base_commit':'a'*40,'fix_commit':'b'*40,'enrich_version':c.ENRICH_VERSION,
            'test_inventory':[unit],'test_files':unit['sources'],'source_files':['os/lib/json.c'],
            'test_patch':'diff --git a/tests/x b/tests/x\n+assert(x);',
            'production_diff':'diff --git a/os/lib/json.c b/os/lib/json.c',
            'profiles':{'native':{}},'inventory_holds':[],'nested_merge_count':0,
            'compile_requirements':[{'symbol':'json_parse_new','kind':'function','at_base':False,
                                     'production_file':'os/lib/json.c','test_files':unit['sources']}]}

def verdict(oracle='runtime'):
    return {'pr':1,'verdict':'accept','profile':'native','units':['tests/08-native-runs/test-json/test-json'],
            'fail_to_pass':['tests/08-native-runs/test-json/test-json::json_test'],
            'pass_to_pass':[],'oracle':oracle,'problem_statement':'JSON parser rejects valid input',
            'causal_chain':'New vector exercises the parser','change_type':'bugfix'}

class CandidatesTests(unittest.TestCase):
    def test_prompt_summarizes_large_hex_data_without_changing_patch(self):
        rec=record()
        original='+  "'+'ab'*5000+'",\n+assert(check_result());\n'
        rec['test_patch']=original
        rendered=json.loads(pipeline.render_record(rec))
        self.assertEqual(rec['test_patch'],original)
        self.assertIn('+assert(check_result());',rendered['test_patch'])
        self.assertIn('PROMPT ONLY:',rendered['test_patch'])
        self.assertEqual(rendered['prompt_omissions'][0]['hex_characters'],10000)
        self.assertLess(len(rendered['test_patch']),1000)

    def test_ordinary_prompt_patch_is_unchanged(self):
        rec=record()
        rendered=json.loads(pipeline.render_record(rec))
        self.assertEqual(rendered['test_patch'],rec['test_patch'])
        self.assertNotIn('prompt_omissions',rendered)

    def test_large_production_preview_does_not_destroy_source_record(self):
        rec=record();original='begin\n'+'source line;\n'*11000+'end\n'
        rec['production_diff']=original
        rendered=json.loads(pipeline.render_record(rec))
        self.assertEqual(rec['production_diff'],original)
        self.assertTrue(rendered['production_diff'].startswith('begin\n'))
        self.assertTrue(rendered['production_diff'].endswith('end\n'))
        self.assertEqual(rendered['prompt_omissions'][0]['kind'],'diff-preview')
        self.assertIn('read full source',rendered['production_diff'])

    def test_large_test_diff_has_an_explicit_preview_without_losing_original(self):
        rec=record();original='start\n'+'+ assert(value);\n'*12000+'end\n'
        rec['test_patch']=original
        rendered=json.loads(pipeline.render_record(rec))
        self.assertEqual(rec['test_patch'],original)
        self.assertLess(len(rendered['test_patch']),101000)
        self.assertEqual(rendered['prompt_omissions'][0]['field'],'test_patch')
        self.assertTrue(rendered['test_patch'].startswith('start\n'))
        self.assertTrue(rendered['test_patch'].endswith('end\n'))

    def test_multiline_suite_keeps_options_on_their_own_entry(self):
        text='EXAMPLES = \\\nexamples/mqtt/native:./mqtt.sh:DEFINES=MQTT=1 \\\ntests/json/native:./json.sh \\\ntests/heap/native:./heap.sh:DEFINES=HEAPMEM_DEBUG=0 \\\ntests/heap/native:./heap.sh:DEFINES=HEAPMEM_DEBUG=1\n'
        entries=c.suite_entries(text)
        self.assertEqual([e for e in entries if e.startswith('tests/json/')],['tests/json/native:./json.sh'])
        self.assertEqual([e.split(':')[2:] for e in entries if e.startswith('tests/heap/')],
                         [['DEFINES=HEAPMEM_DEBUG=0'],['DEFINES=HEAPMEM_DEBUG=1']])

    def test_new_registration_does_not_select_unrelated_suite_consumers(self):
        shared='tests/08-native-runs/Makefile'
        units=[{'id':name,'kind':'native','profile':'native','directory':'tests/08-native-runs/'+name,
                'sources':[shared,'tests/08-native-runs/'+name+'/test.c']} for name in ('new','unrelated')]
        rec={'test_files':[shared,'tests/08-native-runs/new/test.c'],'test_inventory':units,
             'test_patch':'+++ b/'+shared+'\n+tests/08-native-runs/new/native:./new.sh \\\n'}
        self.assertEqual([u['id'] for u in c.select_tests(rec)],['new'])

    def test_only_invoked_registered_cases_enter_inventory(self):
        source='UNIT_TEST_REGISTER(real, "Real"); UNIT_TEST_REGISTER(dead, "Dead"); UNIT_TEST_RUN(real);'
        self.assertEqual(c.cases(source),[{'id':'real','description':'Real'}])

    def test_adjacent_c_string_literals_keep_distinct_descriptions(self):
        source=('UNIT_TEST_REGISTER(first,"prefix " "first"); '
                'UNIT_TEST_REGISTER(second,"prefix " "second"); '
                'UNIT_TEST_RUN(first); UNIT_TEST_RUN(second);')
        self.assertEqual(c.cases(source),[{'id':'first','description':'prefix first'},
                                         {'id':'second','description':'prefix second'}])

    def test_comments_and_string_contents_do_not_invoke_cases(self):
        source=('UNIT_TEST_REGISTER(real,"Real"); UNIT_TEST_REGISTER(dead,"Dead"); '
                '/* UNIT_TEST_RUN(dead); */ printf("UNIT_TEST_RUN(dead)"); UNIT_TEST_RUN(real);')
        self.assertEqual(c.cases(source),[{'id':'real','description':'Real'}])

    def test_ambiguous_runtime_descriptions_are_not_automatic_native_units(self):
        path='tests/native/unit/test.c'
        files={path:'UNIT_TEST_REGISTER(a,"same"); UNIT_TEST_REGISTER(b,"same"); UNIT_TEST_RUN(a); UNIT_TEST_RUN(b);',
               'tests/native/unit/Makefile':'CONTIKI_PROJECT = test'}
        with (patch.object(c,'files_at',return_value=list(files)),
              patch.object(c,'show',side_effect=lambda rev,p:files.get(p))):
            units,holds=c.inventory('a'*40)
        self.assertFalse(units[0]['supported'])
        self.assertEqual(c.select_tests({'test_files':[path],'test_inventory':units}),[])
        self.assertIn('duplicate runtime descriptions',holds[0]['reason'])

    def test_hyphenated_identity_round_trip(self):
        self.assertEqual(instances.split_instance('contiki-ng__contiki-ng-3246'),('contiki-ng','3246'))
        with self.assertRaises(ValueError):instances.split_instance('contiki-ng__contiki-3246')

    def test_unsupported_scenario_does_not_launch(self):
        record={'test_files':['tests/14-rpl-lite/x.csc'],'test_inventory':[
            {'id':'x','profile':'cooja','kind':'cooja','sources':['tests/14-rpl-lite/x.csc'],'supported':False}]}
        self.assertEqual(c.select_tests(record),[])

    def test_shared_helper_needs_recorded_consumer(self):
        rec=record();rec['test_files']=['tests/utils.sh']
        self.assertEqual(c.select_tests(rec),[])
        rec['test_inventory'][0]['sources'].append('tests/utils.sh')
        self.assertEqual([u['id'] for u in c.select_tests(rec)],['tests/08-native-runs/test-json/test-json'])

    def test_stale_record_rejected_before_selection(self):
        self.assertEqual(c.check_verdict({'pr':1,'verdict':'accept'},{'number':1}),['stale enriched record; re-enrich'])
        rec=record();del rec['production_diff']
        self.assertTrue(c.stale_record(rec))

    def test_accept_requires_enabled_body_evidence_and_explicit_profile(self):
        rec=record();v=verdict()
        self.assertEqual(c.check_verdict(v,rec),[])
        rec['test_inventory'][0]['profile_evidence']={'enabled':None,'guards':[{'directive':'#if WITH_JSON'}]}
        self.assertIn('selected test body needs compiler-backed profile evidence',c.check_verdict(v,rec))
        v['profile']=None
        self.assertEqual(c.check_verdict(v,rec),['profile must name a supported execution profile'])

    def test_malformed_verdict_arrays_return_errors(self):
        rec=record()
        for field,bad in [('units',[None]),('fail_to_pass',[None]),('pass_to_pass',[None]),
                          ('fail_to_pass',{}),('pass_to_pass',42)]:
            v=verdict();v[field]=bad
            self.assertTrue(c.check_verdict(v,rec),field)

    def test_compile_oracle_rejects_incidental_or_base_present_symbols(self):
        rec=record();v=verdict('compile');v.update(compile_symbols=['json_parse_new'],oracle_attribution='New API')
        self.assertEqual(c.check_verdict(v,rec),[])
        v['compile_symbols']=['helper_from_test_patch']
        self.assertIn('compile_symbols must name verified absent added interfaces',c.check_verdict(v,rec))
        rec['compile_requirements'][0]['at_base']=True
        v['compile_symbols']=['json_parse_new']
        self.assertIn('compile_symbols must name verified absent added interfaces',c.check_verdict(v,rec))

    def test_config_oracle_uses_diagnostics_not_interface_names(self):
        rec=record();v=verdict('config');v.update(config_diagnostics=['No rule to make target foo'],oracle_attribution='Fix adds target')
        self.assertEqual(c.check_verdict(v,rec),[])
        self.assertEqual(pipeline.config_attribution('make: *** No rule to make target foo. Stop.',v['config_diagnostics']),
                         'No rule to make target foo')
        self.assertIsNone(pipeline.config_attribution('notice: No rule to make target foo',v['config_diagnostics']))

    def test_override_cannot_modify_protected_upstream_test(self):
        self.assertTrue(pipeline.protected_test_change('diff --git a/tests/08-native-runs/x.c b/tests/08-native-runs/x.c'))
        self.assertTrue(pipeline.protected_test_change('+++ b/regression-tests/x.c'))
        self.assertTrue(pipeline.protected_test_change('diff --git a/tools/cooja/x b/tools/cooja/x'))
        self.assertFalse(pipeline.protected_test_change('diff --git a/os/lib/json.c b/os/lib/json.c'))

    def test_compile_requirements_only_new_production_interface_absent_at_base(self):
        production=('+++ b/os/lib/cbor.c\n void\n+cbor_write_signed(cbor_writer_state_t *state, int64_t value)\n+{\n'
                    '+  return;\n+}\n@@ -1,0 +1,2 @@\n+cbor_size_t\n+cbor_read_signed(cbor_reader_state_t *state, int64_t *value)\n+{\n'
                    '+  return CBOR_SIZE_NONE;\n+}\n+#define CBOR_NEW_LIMIT 12\n+int existing_api(void) {\n')
        tests=('+++ b/tests/08-native-runs/16-cbor/test-cbor.c\n'
               '+cbor_write_signed(&writer, -1);\n+cbor_read_signed(&reader, &value);\n'
               '+assert(CBOR_NEW_LIMIT > 0);\n+existing_api();\n')
        with patch.object(c,'git',side_effect=lambda *args,**kwargs: 'old declaration' if 'existing_api' in args else ''):
            found=c.compile_requirements('a'*40,production,tests)
        self.assertEqual({r['symbol'] for r in found},{'cbor_write_signed','cbor_read_signed','CBOR_NEW_LIMIT'})
        self.assertTrue(all(r['at_base'] is False for r in found))

    def test_observed_finalization_requires_same_revision_and_named_runtime_flip(self):
        rec=record();unit=rec['test_inventory'][0];case=unit['id']+'::json_test'
        receipt={'valid':True,'observe':True,'recipe_sha256':'hash','instance_id':'contiki-ng__contiki-ng-1',
                 'profile':'native','before':{'rc':1,'cases':{case:'failed'}},
                 'after':{'rc':0,'cases':{case:'passed'}},'fail_to_pass':[case],
                 'pass_to_pass':[],'regressed':[]}
        with tempfile.TemporaryDirectory() as temp:
            prior=Path(temp)
            (prior/'instance.json').write_text(json.dumps({'observe':True,'enrich_version':c.ENRICH_VERSION,
                'oracle':'runtime','profile':'native','units':[unit]}))
            (prior/'metadata.json').write_text(json.dumps({'instance_id':'contiki-ng__contiki-ng-1',
                'base_commit':rec['base_commit'],'fix_commit':rec['fix_commit'],
                'files_changed_by_fix':rec['source_files']}))
            with patch.object(pipeline.au,'recipe_fingerprint',return_value='hash'):
                pipeline.checked_observation(prior,receipt,rec,{unit['id']:unit})
                receipt['before']['rc']=2
                with self.assertRaisesRegex(ValueError,'named runtime'):
                    pipeline.checked_observation(prior,receipt,rec,{unit['id']:unit})
                receipt['before']['rc']=1
                receipt['before']['cases'][case]='not_run'
                with self.assertRaisesRegex(ValueError,'named runtime'):
                    pipeline.checked_observation(prior,receipt,rec,{unit['id']:unit})
                receipt['before']['cases'][case]='failed'
                rec['fix_commit']='c'*40
                with self.assertRaisesRegex(ValueError,'different PR revision'):
                    pipeline.checked_observation(prior,receipt,rec,{unit['id']:unit})

if __name__=='__main__':unittest.main()
