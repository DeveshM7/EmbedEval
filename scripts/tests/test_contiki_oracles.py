"""Source contradictions must fail before expensive candidate validation."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


class CompileExpectationTests(unittest.TestCase):
    """Compile oracles: what a before-build must fail on is derived from the
    fix, and every unit's failure must be attested."""
    FIX=('diff --git a/os/lib/cbor.h b/os/lib/cbor.h\n--- a/os/lib/cbor.h\n+++ b/os/lib/cbor.h\n'
         '@@ -1 +1,3 @@\n+int cbor_read_signed(int v);\n+#define CBOR_LIMIT_NEW 4\n+int base_thing(void);\n'
         'diff --git a/os/lib/new.h b/os/lib/new.h\n--- /dev/null\n+++ b/os/lib/new.h\n@@ -0,0 +1 @@\n+int brand_new_api(void);\n')
    TESTS=('diff --git a/tests/u/test-u.c b/tests/u/test-u.c\n--- a/tests/u/test-u.c\n+++ b/tests/u/test-u.c\n'
           '@@ -1 +1,9 @@\n+#include "lib/new.h"\n+  x = cbor_read_signed(1); /* cbor_comment_only */\n'
           '+  puts("CBOR_STRING_ONLY");\n+#if CBOR_LIMIT_NEW\n+  /* spans\n+   * cbor_hidden_name\n+   */\n'
           '+  y = CBOR_LIMIT_NEW + base_thing();\n')
    UNIT={'id':'tests/u/test-u','kind':'native','sources':['tests/u/test-u.c','tests/u/Makefile']}

    def derive(self):
        import contiki_ng_pipeline as pipeline
        def git(*args,**kw):
            return self.FIX if args[0]=='diff' else ''
        present='base_thing\n'
        with patch.object(pipeline.candidates,'git',side_effect=git), \
             patch.object(pipeline.subprocess,'run',return_value=SimpleNamespace(stdout=present,returncode=0)), \
             patch.object(pipeline.candidates,'clone',return_value=Path('/clone')):
            return pipeline.compile_expectations('b'*40,'f'*40,['os/lib/cbor.h','os/lib/new.h'],self.TESTS,[self.UNIT])

    def test_derives_fix_introduced_names_and_created_headers(self):
        e=self.derive()[self.UNIT['id']]
        # not comments, strings, #if conditions, or names the base already has
        self.assertEqual(e,{'symbols':['CBOR_LIMIT_NEW','cbor_read_signed'],'headers':['os/lib/new.h']})

    def test_names_the_fix_only_uses_or_the_tests_declare_are_not_expected(self):
        import contiki_ng_pipeline as pipeline
        fix=('diff --git a/os/lib/heapmem.c b/os/lib/heapmem.c\n--- a/os/lib/heapmem.c\n+++ b/os/lib/heapmem.c\n'
             '@@ -1 +1,4 @@\n+void *heapmem_calloc(size_t nmemb, size_t size)\n+{ if (nmemb > INT64_MAX) return 0; }\n'
             '+static int zones;\n')
        tests=('diff --git a/tests/u/test-u.c b/tests/u/test-u.c\n--- a/tests/u/test-u.c\n+++ b/tests/u/test-u.c\n'
               '@@ -1 +1,4 @@\n+UNIT_TEST_REGISTER(zones, "Zones");\n+  size_t nmemb = INT64_MAX;\n'
               '+  UNIT_TEST_ASSERT(heapmem_calloc(nmemb, 1));\n')
        with patch.object(pipeline.candidates,'git',return_value=fix), \
             patch.object(pipeline.subprocess,'run',return_value=SimpleNamespace(stdout='',returncode=1)), \
             patch.object(pipeline.candidates,'clone',return_value=Path('/clone')):
            e=pipeline.compile_expectations('b'*40,'f'*40,['os/lib/heapmem.c'],tests,[self.UNIT])
        self.assertEqual(e[self.UNIT['id']]['symbols'],['heapmem_calloc'])

    def attest(self,log,failed=True,expectations=None):
        import contiki_ng_pipeline as pipeline
        cfg={'units':[self.UNIT],'compile_expectations':expectations or
             {self.UNIT['id']:{'symbols':['CBOR_LIMIT_NEW','cbor_read_signed'],'headers':['os/lib/new.h']}}}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/pipeline.unit_log_name(self.UNIT,'build')).write_text(log)
            out=(self.UNIT['id']+': native build failed\n') if failed else ''
            return pipeline.compile_attestation(cfg,out,tmp)

    def test_missing_created_header_attests_the_unit(self):
        attested,why=self.attest('test-u.c:1:10: fatal error: lib/new.h: No such file or directory\n')
        self.assertEqual(attested,{self.UNIT['id']:{'header':'lib/new.h'}})

    def test_every_expected_name_must_be_named(self):
        log=("test-u.c:2:7: error: implicit declaration of function 'cbor_read_signed' [-Werror]\n"
             "test-u.c:8:7: error: 'CBOR_LIMIT_NEW' undeclared (first use in this function)\n")
        self.assertEqual(self.attest(log)[0],{self.UNIT['id']:{'symbols':['CBOR_LIMIT_NEW','cbor_read_signed']}})
        attested,why=self.attest(log.splitlines()[0]+'\n')
        self.assertIsNone(attested)
        self.assertIn('CBOR_LIMIT_NEW',why)

    def test_unit_that_builds_or_fails_unexplained_is_not_attested(self):
        self.assertIsNone(self.attest('',failed=False)[0])
        none={self.UNIT['id']:{'symbols':[],'headers':[]}}
        self.assertIsNone(self.attest("error: something else\n",expectations=none)[0])
        self.assertEqual(self.attest('',failed=False,expectations=none)[0],{})


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
        self.assertIn(('bench.json', '/opt/embedeval/bench.json'), spec['files'])
        self.assertNotIn('instance.json', ' '.join(f for pair in spec['files'] for f in pair))
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


class SandboxPostTests(unittest.TestCase):
    def test_post_runs_without_the_host_home(self):
        """With the host's home mounted, /home is a session overlay and %post's
        changes under it (the CI image's /home/user repositories) are lost."""
        import io, tempfile, apptainer_util as au
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / 'r.def').write_text('Bootstrap: localimage\nFrom: base.sif\n%post\n    true\n')
            calls = []
            def run(cmd, *a):
                calls.append(cmd)
                return 0
            with patch.object(au, '_run_build', side_effect=run):
                au._build_via_sandbox(d / 'out.sif', d / 'r.def', {}, io.StringIO(), {}, d, 60)
        post = next(c for c in calls if c[:2] == ['apptainer', 'exec'])
        self.assertIn('--no-home', post)
        self.assertIn('--writable', post)


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

