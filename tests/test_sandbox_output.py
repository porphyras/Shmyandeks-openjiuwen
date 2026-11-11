"""Offline tests for the sandbox's captured output and environment (no network).

Run with: python3 -m unittest discover -s tests -v

Output over MAX_OUT keeps its first and last halves: the end carries the answer line and
unittest's summary. Programs run with one BLAS/OpenMP thread.
"""
import os
import tempfile
import unittest

from harness import sandbox


def run_py(src):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "p.py"), "w") as f:
        f.write(src)
    return sandbox.run([sandbox.PYTHON, "p.py"], d, timeout=30)


class OutputTests(unittest.TestCase):
    def test_small_output_is_whole(self):
        rc, out, err = run_py("print('a'); print(42)")
        self.assertEqual((rc, out), (0, "a\n42\n"))

    def test_long_output_keeps_the_end(self):
        rc, out, err = run_py("for i in range(20000):\n    print('line', i)\nprint('ANSWER 42')")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("line 0\n"))
        self.assertEqual(out.strip().splitlines()[-1], "ANSWER 42")
        self.assertIn("bytes skipped", out)
        self.assertLessEqual(len(out.encode()), sandbox.MAX_OUT + 100)

    def test_unittest_summary_survives_noisy_tests(self):
        src = ("import sys, unittest\n"
               "class T(unittest.TestCase):\n"
               "    def test_a(self):\n"
               "        sys.stderr.write('x' * 200000 + '\\n')\n"
               "        self.assertTrue(True)\n"
               "unittest.main()\n")
        rc, out, err = run_py(src)
        self.assertEqual(rc, 0)
        self.assertIn("Ran 1 test", err)
        self.assertTrue(err.rstrip().endswith("OK"))

    def test_one_blas_thread(self):
        rc, out, err = run_py("import os\nprint(os.environ.get('OPENBLAS_NUM_THREADS'), os.environ.get('OMP_NUM_THREADS'))")
        self.assertEqual(out.strip(), "1 1")


if __name__ == "__main__":
    unittest.main()
