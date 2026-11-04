"""Dependency mistakes found in upstream source, without authored benchmark tests."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from contiki_ng_inputs import closure


class InputClosureTests(unittest.TestCase):
    def unit(self, directory, sources):
        return {'id':directory+'/test','directory':directory,
                'suite_directory':str(Path(directory).parent),'sources':sources,'submodules':[]}

    def test_local_vectors_headers_and_unit_makefile_are_inputs(self):
        files={'tests/native/a/test.c':'#include "vectors.c"\n#include "local.h"',
               'tests/native/a/vectors.c':'const int vector=1;',
               'tests/native/a/local.h':'#include "nested.h"',
               'tests/native/a/nested.h':'#define LIMIT 1',
               'tests/native/a/unrelated.dat':'unrelated',
               'tests/native/a/Makefile':'PROJECT_SOURCEFILES += common.c',
               'tests/native/a/common.c':'', 'tests/native/a/project-conf.h':''}
        result=closure(self.unit('tests/native/a',['tests/native/a/test.c']),files,lambda p:files.get(p,''))
        self.assertEqual(set(result['sources']),set(files)-{'tests/native/a/unrelated.dat'})
        self.assertFalse(result['dependency_evidence']['build_verified'])

    def test_native_ecc_nested_module_requires_pinned_submodule(self):
        files={'tests/native/ecc/test.c':'',
               'tests/native/ecc/Makefile':'MODULES += os/services/ecc',
               'os/services/ecc/Makefile.ecc':'MODULES += os/net/security/micro-ecc',
               'os/net/security/micro-ecc':'',
               '.gitmodules':'[submodule "ecc"]\npath = os/net/security/micro-ecc\nurl = https://example.test/ecc'}
        result=closure(self.unit('tests/native/ecc',['tests/native/ecc/test.c']),files,lambda p:files.get(p,''))
        self.assertEqual(result['submodules'],['os/net/security/micro-ecc'])

    def test_sourced_script_resolves_protocol_fixture_without_other_protocol(self):
        files={'tests/packets/02-sicslowpan.sh':'export TEST_PROTOCOL=sicslowpan\nsource packet-injector.sh',
               'tests/packets/packet-injector.sh':'source ../utils.sh\nCODE_DIR=packet-injector\nPACKET_DIR=$CODE_DIR/$TEST_PROTOCOL-data\nfor i in $PACKET_DIR/*',
               'tests/utils.sh':'', 'tests/packets/packet-injector/sicslowpan-data/packet-1':'data',
               'tests/packets/packet-injector/uip-data/packet-1':'other'}
        unit=self.unit('tests/packets/packet-injector',['tests/packets/02-sicslowpan.sh'])
        result=closure(unit,files,lambda p:files.get(p,''))
        self.assertIn('tests/packets/packet-injector/sicslowpan-data/packet-1',result['sources'])
        self.assertIn('tests/utils.sh',result['sources'])
        self.assertNotIn('tests/packets/packet-injector/uip-data/packet-1',result['sources'])

    def test_executed_shell_wrapper_transports_shared_helper(self):
        files={'tests/native/json/Makefile':'',
               'tests/native/json.sh':'./run-one.sh json',
               'tests/native/run-one.sh':'source ../utils.sh', 'tests/utils.sh':''}
        unit=self.unit('tests/native/json',['tests/native/json.sh'])
        result=closure(unit,files,lambda p:files.get(p,''))
        self.assertIn('tests/native/run-one.sh',result['sources'])
        self.assertIn('tests/utils.sh',result['sources'])

    def test_dynamic_module_is_explicitly_unresolved(self):
        files={'tests/native/a/Makefile':'MODULES += $(UNRESOLVED)/ecc',
               'tests/native/a/test.c':''}
        result=closure(self.unit('tests/native/a',['tests/native/a/test.c']),files,lambda p:files.get(p,''))
        self.assertEqual(result['submodules'],[])
        self.assertEqual(result['dependency_evidence']['unresolved'][0]['expression'],'$(UNRESOLVED)/ecc')

    def test_scenario_includes_mote_project_configuration_and_sources(self):
        files={'tests/sim/test.csc':'', 'examples/mote/main.c':'',
               'examples/mote/Makefile':'PROJECT_SOURCEFILES += common.c',
               'examples/mote/common.c':'','examples/mote/project-conf.h':''}
        unit={'id':'tests/sim/test.csc','kind':'cooja','directory':'tests/sim',
              'suite_directory':'tests/sim','sources':['tests/sim/test.csc','examples/mote/main.c'],'submodules':[]}
        result=closure(unit,files,lambda p:files.get(p,''))
        self.assertEqual(set(result['sources']),set(files))
