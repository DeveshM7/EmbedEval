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


class HeaderAttributionTests(unittest.TestCase):
    LOG='test-cbor.c:38:10: fatal error: lib/cbor.h: No such file or directory\ncompilation terminated.\n'
    def patch(self,old='/dev/null',body='+void cbor_write_unsigned(int v);'):
        return f'diff --git a/os/lib/cbor.h b/os/lib/cbor.h\n--- {old}\n+++ b/os/lib/cbor.h\n@@ -0,0 +1 @@\n{body}\n'

    def test_missing_header_created_by_fix_and_declaring_symbol(self):
        from contiki_ng_pipeline import header_attribution
        self.assertEqual(header_attribution(self.LOG,['cbor_write_unsigned'],self.patch()),'lib/cbor.h')

    def test_existing_header_or_undeclared_symbol_is_not_attributed(self):
        from contiki_ng_pipeline import header_attribution
        self.assertIsNone(header_attribution(self.LOG,['cbor_write_unsigned'],self.patch(old='a/os/lib/cbor.h')))
        self.assertIsNone(header_attribution(self.LOG,['cbor_read_signed'],self.patch()))
        self.assertIsNone(header_attribution('no error\n',['cbor_write_unsigned'],self.patch()))


class CoojaEraImageTests(unittest.TestCase):
    def test_base_dates_pick_their_era_image(self):
        from contiki_ng_pipeline import cooja_era_image
        import apptainer_util as au
        self.assertEqual(cooja_era_image('2017-12-05'),'5310e7a')
        self.assertEqual(cooja_era_image('2021-05-26'),'66cbc3434')
        self.assertTrue(cooja_era_image('2022-08-17').startswith('d0c6c3ce'))
        self.assertTrue(cooja_era_image('2022-12-20').startswith('fc6fffa82'))   # newest on or before
        self.assertTrue(cooja_era_image('2023-09-15').startswith('904f00f7'))
        self.assertTrue(cooja_era_image('2025-11-12').startswith('9eb427ae'))
        self.assertTrue(cooja_era_image('2026-06-29').startswith('3f04d86f'))
        self.assertEqual(cooja_era_image('2026-09-27'),au.CONTIKI_NG_CURRENT_IMAGE)


class EraImageRepackTests(unittest.TestCase):
    def run_with(self, listing):
        import io, apptainer_util as au
        from unittest.mock import patch, MagicMock
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout=listing if cmd[1] == 'sif' else '')
        with patch.object(au.subprocess, 'run', side_effect=fake_run), \
             patch.object(au, 'build_env', return_value={'APPTAINER_TMPDIR': '/tmp'}), \
             patch.object(au.shutil, 'move') as move:
            au._amd64_sif(Path('/x/old.sif'), io.StringIO())
        return [c[1:3] for c in calls], move.called

    def test_amd64_image_is_left_alone(self):
        calls, moved = self.run_with('4 |1 |NONE |40960-1 |FS (Squashfs/*System/amd64)')
        self.assertEqual(calls, [['sif', 'list']])
        self.assertFalse(moved)

    def test_i386_image_is_repacked_through_a_sandbox(self):
        calls, moved = self.run_with('4 |1 |NONE |40960-1 |FS (Squashfs/*System/386)')
        self.assertEqual(calls, [['sif', 'list'], ['build', '--sandbox'], ['build', calls[2][1]]])
        self.assertTrue(moved)


class SandboxBuildTests(unittest.TestCase):
    def test_contiki_template_parses_into_sandbox_steps(self):
        import apptainer_util as au
        text = (Path(__file__).resolve().parents[2] / 'templates/contiki-ng/apptainer/instance.def.tmpl').read_text()
        spec = au.parse_definition(text, {'BASE_IMAGE': '/img/base.sif'})
        self.assertEqual(spec['base'], '/img/base.sif')
        self.assertIn(('run_tests.py', '/usr/local/bin/run_tests'), spec['files'])
        self.assertIn(('instance.json', '/opt/embedeval/instance.json'), spec['files'])
        self.assertIn('git apply /opt/embedeval/test_patch.diff', spec['post'])
        self.assertIn('export LANG=C.UTF-8', spec['environment'])
        self.assertIn('embedeval.instance', spec['labels'])
        self.assertNotIn('%', spec['post'].splitlines()[0])

    def test_i386_refusal_falls_back_to_the_sandbox_build(self):
        import tempfile, apptainer_util as au
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / 'r.def').write_text('Bootstrap: localimage\nFrom: base.sif\n%post\n    true\n')
            def first(cmd, fh, *a):
                fh.write(au.I386_REFUSAL + '\n'); fh.flush(); return 255
            def sandbox(partial, *a):
                Path(partial).write_text('sif'); return 0
            with patch.object(au, '_run_build', side_effect=first), \
                 patch.object(au, '_build_via_sandbox', side_effect=sandbox) as fallback, \
                 patch.object(au, 'path', side_effect=lambda *p: d.joinpath(*p)), \
                 patch.object(au, 'remove'), patch.object(au, '_stop'), patch.object(au, '_holders', return_value=[]), \
                 patch('builtins.print'):
                self.assertTrue(au.build(d / 'out.sif', d / 'r.def', {}, d / 'build.log', attempts=1))
            self.assertTrue(fallback.called)
            self.assertTrue((d / 'out.sif').exists())


class LocalSandboxTests(unittest.TestCase):
    def test_amd64_images_run_from_their_sif(self):
        import apptainer_util as au
        from unittest.mock import patch
        with patch.object(au, 'is_i386_sif', return_value=False):
            self.assertEqual(au.exec_args(Path('/x/a.sif'))[-1], '/x/a.sif')

    def test_i386_image_runs_from_one_local_extraction(self):
        import tempfile, apptainer_util as au
        from unittest.mock import patch, MagicMock
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / 'inst.sif'; img.write_text('sif')
            builds = []
            def fake_run(cmd, **kw):
                builds.append(cmd); Path(cmd[3]).mkdir(parents=True); return MagicMock(returncode=0)
            with patch.dict(au.os.environ, {'EMBEDEVAL_LOCAL_SANDBOXES': str(Path(tmp) / 'local')}), \
                 patch.object(au, 'is_i386_sif', return_value=True), \
                 patch.object(au.subprocess, 'run', side_effect=fake_run):
                first = au.exec_args(img)[-1]
                second = au.exec_args(img)[-1]
                self.assertEqual(first, second)
                self.assertEqual(len(builds), 1)
                self.assertTrue(first.startswith(str(Path(tmp) / 'local')))
                self.assertEqual(au.remove_local_sandbox(img), [Path(first)])
                self.assertFalse(Path(first).exists())

