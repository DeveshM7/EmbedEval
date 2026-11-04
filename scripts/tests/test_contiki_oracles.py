"""Source contradictions must fail before expensive candidate validation."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import contiki_ng_candidates as c
from test_contiki_candidates import record,verdict


class OracleFactsTests(unittest.TestCase):
    def body(self,source):
        return c.native_body_evidence(source,'tests/test.c',[{'id':'sample','description':'Sample'}])

    def test_configuration_guards_outside_test_bodies_do_not_disable_them(self):
        evidence=self.body('#if DEBUG\n#define VALUE 2\n#else\n#define VALUE 1\n#endif\n'
            'UNIT_TEST(sample) { check(VALUE); }\n'
            'UNIT_TEST_REGISTER(sample, "Sample"); UNIT_TEST_RUN(sample);')
        self.assertTrue(evidence['enabled'])
        self.assertEqual(evidence['guards'],[])
        self.assertEqual(len(evidence['configuration_guards']),2)

    def test_guards_on_definitions_assertions_registration_or_calls_are_unknown(self):
        definition='UNIT_TEST(sample) { check(); }\n'
        registration='UNIT_TEST_REGISTER(sample, "Sample");\n'
        run='UNIT_TEST_RUN(sample);\n'
        sources=['#if FLAG\n'+definition+'#endif\n'+registration+run,
                 'UNIT_TEST(sample) {\n#if FLAG\ncheck();\n#endif\n}\n'+registration+run,
                 definition+'#if FLAG\n'+registration+'#endif\n'+run,
                 definition+registration+'#if FLAG\n'+run+'#endif\n']
        for source in sources:
            with self.subTest(source=source):
                self.assertIsNone(self.body(source)['enabled'])
                self.assertTrue(self.body(source)['guards'])

    def test_comments_strings_and_missing_definitions_do_not_prove_body_enabled(self):
        self.assertTrue(self.body('/* #if OMIT */ UNIT_TEST(sample) { puts("#if OMIT"); }')['enabled'])
        self.assertIsNone(self.body('/* UNIT_TEST(sample) {} */ UNIT_TEST_RUN(sample);')['enabled'])
        self.assertIsNone(self.body('#if BAD\nUNIT_TEST(sample) {}')['enabled'])

    def evidence(self,source,extra=None):
        rec=record();unit=rec['test_inventory'][0]
        files={unit['sources'][0]:source,**(extra or {})}
        unit['sources']+=list(extra or {})
        return c.before_compile_evidence(unit,rec['compile_requirements'],lambda p:files.get(p,''))

    def test_direct_new_project_api_blocks_runtime(self):
        evidence=self.evidence('UNIT_TEST(json_test) { json_parse_new(); }')
        self.assertEqual(evidence['status'],'source_blocked')
        self.assertEqual(evidence['required_interfaces'],['json_parse_new'])
        rec=record();rec['test_inventory'][0]['before_compile']=evidence
        self.assertTrue(any('runtime oracle contradicts' in error for error in c.check_verdict(verdict(),rec)))
        v=verdict('compile');v.update(compile_symbols=['json_parse_new'],oracle_attribution='The intended new interface')
        self.assertEqual(c.check_verdict(v,rec),[])

    def test_compile_p2p_cannot_execute_in_the_blocked_unit(self):
        rec=record();unit=rec['test_inventory'][0]
        unit['before_compile']=self.evidence('UNIT_TEST(json_test) { json_parse_new(); }')
        unit['cases'].append({'id':'existing','description':'Existing'})
        v=verdict('compile');v.update(compile_symbols=['json_parse_new'],oracle_attribution='New API',
                                     pass_to_pass=[unit['id']+'::existing'])
        self.assertTrue(any('P2P cases cannot run' in error for error in c.check_verdict(v,rec)))

    def test_comments_strings_and_prototypes_are_not_executed_calls(self):
        evidence=self.evidence('int json_parse_new(void); /* json_parse_new(); */ '
                               'UNIT_TEST(json_test) { printf("json_parse_new()"); }')
        self.assertEqual(evidence['status'],'unknown')

    def test_compile_oracle_can_keep_p2p_from_a_separate_unblocked_unit(self):
        rec=record();unit=rec['test_inventory'][0]
        unit['before_compile']=self.evidence('UNIT_TEST(json_test) { json_parse_new(); }')
        other={**unit,'id':'tests/native/stable/test','sources':['tests/native/stable/test.c'],
               'cases':[{'id':'stable','description':'Stable'}],
               'before_compile':{'status':'unknown','required_interfaces':[]}}
        rec['test_inventory'].append(other);rec['test_files']+=other['sources']
        v=verdict('compile');v.update(units=[unit['id'],other['id']],compile_symbols=['json_parse_new'],
            oracle_attribution='New API',pass_to_pass=[other['id']+'::stable'])
        self.assertEqual(c.check_verdict(v,rec),[])

    def test_test_snapshot_provider_is_not_missing_production_api(self):
        evidence=self.evidence('UNIT_TEST(json_test) { json_parse_new(); }',
            {'tests/helper.h':'static inline int json_parse_new(void) { return 0; }'})
        self.assertEqual(evidence['required_interfaces'],[])

    def test_macro_alias_and_local_callback_do_not_force_compile_oracle(self):
        for setup in ('#define json_parse_new() old_api()\n', 'int (*json_parse_new)(void);\n'):
            with self.subTest(setup=setup):
                self.assertEqual(self.evidence(setup+'UNIT_TEST(json_test) { json_parse_new(); }')['status'],'unknown')

    def test_function_macro_in_make_flags_is_not_a_missing_interface(self):
        evidence=self.evidence('UNIT_TEST(json_test) { json_parse_new(); }',
            {'tests/native/Makefile':'DEFINES += json_parse_new(x)=existing_api(x)'})
        self.assertEqual(evidence['status'],'unknown')

    def test_comparing_a_function_result_is_still_a_required_call(self):
        evidence=self.evidence('UNIT_TEST(json_test) { UNIT_TEST_ASSERT(json_parse_new() == 0); }')
        self.assertEqual(evidence['status'],'source_blocked')

    def test_missing_before_submodule_is_a_setup_hold_not_an_accept(self):
        rec=record();rec['test_inventory'][0]['before_dependencies']['missing_submodules']=['os/new-sdk']
        self.assertTrue(any('before snapshot' in error for error in c.check_verdict(verdict(),rec)))
