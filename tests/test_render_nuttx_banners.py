"""ostest_banners when a test is called more than once in ostest_main.c."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import render_nuttx_instance as render  # noqa: E402


class RepeatedCallTests(unittest.TestCase):
    def test_a_bannerless_second_call_keeps_the_bannered_one(self):
        # Since nuttx-apps #3759: `ostest wqueue` runs wqueue_test() alone,
        # without a banner, besides the bannered call in the main sequence.
        with tempfile.TemporaryDirectory() as tmp:
            ostest = Path(tmp)
            (ostest / "ostest_main.c").write_text(
                '  if (argc == 2 && strcmp(argv[1], "wqueue") == 0)\n    {\n'
                '      wqueue_test();\n      return 0;\n    }\n'
                '  printf("\\nuser_main: wqueue test\\n");\n  wqueue_test();\n')
            (ostest / "wqueue.c").write_text("void wqueue_test(void)\n{\n}\n")
            self.assertEqual(render.ostest_banners(ostest, ["testing/ostest/wqueue.c"]),
                             ["user_main: wqueue test"])

    def test_a_test_with_no_bannered_call_is_still_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ostest = Path(tmp)
            (ostest / "ostest_main.c").write_text(
                '  wqueue_test();\n  printf("\\nuser_main: other\\n");\n'
                '#ifdef CONFIG_X\n  wqueue_test();\n#endif\n')
            (ostest / "wqueue.c").write_text("void wqueue_test(void)\n{\n}\n")
            with self.assertRaisesRegex(ValueError, "wqueue_test"):
                render.ostest_banners(ostest, ["testing/ostest/wqueue.c"])

    def test_a_compile_case_may_have_no_banner(self):
        # apps #237: tls_test() has never had a banner; the test is that tls.c compiles.
        with tempfile.TemporaryDirectory() as tmp:
            ostest = Path(tmp)
            (ostest / "ostest_main.c").write_text("  /* Test TLS */\n  tls_test();\n")
            (ostest / "tls.c").write_text("void tls_test(void)\n{\n}\n")
            self.assertEqual(render.ostest_banners(ostest, ["testing/ostest/tls.c"], False), [])
            with self.assertRaises(ValueError):
                render.ostest_banners(ostest, ["testing/ostest/tls.c"])


if __name__ == "__main__":
    unittest.main()
