"""Real subprocess trees must disappear after cancellation and timeouts."""
import tests._bootstrap as _bootstrap  # noqa: F401
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from qatlib_env import supervisor


class StageDeadlineTests(unittest.TestCase):
    def test_two_commands_share_one_stage_deadline(self):
        environment = {**os.environ, "QATLIB_STAGE_DEADLINE": str(time.monotonic() + 0.6)}
        supervisor.run([sys.executable, "-c", "import time; time.sleep(0.25)"], env=environment, check=True)
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            supervisor.run([sys.executable, "-c", "import time; time.sleep(10)"], env=environment, timeout=10, grace=0.05)
        self.assertLess(time.monotonic() - started, 0.8)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process-tree ownership")
class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pids = self.root / "pids"
        self.driver = self.root / "tree.py"
        self.driver.write_text('''import os, signal, subprocess, sys, time
with open(sys.argv[1], "a") as f:
    f.write(str(os.getpid()) + "\\n"); f.flush()
depth = int(sys.argv[2])
if depth:
    subprocess.Popen([sys.executable, __file__, sys.argv[1], str(depth-1)], start_new_session=(depth == 2))
else:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(60)
''')

    def assert_gone(self):
        pids = [int(value) for value in self.pids.read_text().splitlines()]
        self.assertEqual(len(pids), 4)
        self.assertFalse([pid for pid in pids if Path(f"/proc/{pid}").exists()], pids)

    def test_timeout_reaps_nested_process_groups_and_term_ignoring_leaf(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            supervisor.run(["sh", "-c", '"$1" "$2" "$3" 3', "tree", sys.executable,
                            str(self.driver), str(self.pids)], timeout=0.8, grace=0.1)
        self.assert_gone()

    def test_sigterm_reaps_tree_before_returning_cancellation(self):
        script = ("from qatlib_env import supervisor\nimport sys\n"
                  "try: supervisor.run(sys.argv[1:], grace=0.1)\n"
                  "except supervisor.ProcessCancelled as e: raise SystemExit(e.returncode)\n")
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
        process = subprocess.Popen([sys.executable, "-c", script, sys.executable,
                                   str(self.driver), str(self.pids), "3"], env=env)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.pids.exists() and len(self.pids.read_text().splitlines()) == 4:
                break
            time.sleep(0.02)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=5), 128 + signal.SIGTERM)
        self.assert_gone()

    def test_failure_reaps_detached_child(self):
        # Parent returns an error before its detached child finishes. A subreaper
        # must collect the child even though communicate already reaped the parent.
        script = ("import subprocess,sys,time\n"
                  "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], start_new_session=True)\n"
                  "open(sys.argv[1],'w').write(str(p.pid))\n"
                  "time.sleep(.1)\nraise SystemExit(7)\n")
        with self.assertRaises(subprocess.CalledProcessError) as context:
            supervisor.run([sys.executable, "-c", script, str(self.pids)], check=True, grace=0.1)
        self.assertEqual(context.exception.returncode, 7)
        self.assertFalse(Path(f"/proc/{self.pids.read_text()}").exists())

    def test_failed_parent_does_not_wait_for_descendant_held_capture_pipe(self):
        script = ("import subprocess,sys\n"
                  "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'], start_new_session=True)\n"
                  "open(sys.argv[1],'w').write(str(p.pid))\n"
                  "print('failed build', flush=True)\nraise SystemExit(7)\n")
        started = time.monotonic()
        with self.assertRaises(subprocess.CalledProcessError) as context:
            supervisor.run([sys.executable, "-c", script, str(self.pids)], check=True,
                           text=True, capture_output=True, grace=0.1, timeout=10)
        self.assertEqual(context.exception.returncode, 7)
        self.assertEqual(context.exception.stdout, "failed build\n")
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(Path(f"/proc/{self.pids.read_text()}").exists())

    def test_module_timeout_reports_real_lean_process(self):
        lean = self.root / "lean"
        lean.symlink_to(sys.executable)
        source = self.root / "Slow.lean"
        source.write_text("import time\ntime.sleep(60)\n")
        with self.assertRaises(subprocess.TimeoutExpired) as context:
            supervisor.run([str(lean), str(source)], timeout=5, module_timeout=0.15, grace=0.1)
        self.assertEqual(context.exception.timeout, 0.15)

    def test_input_output_and_check_are_subprocess_compatible(self):
        result = supervisor.run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                                input="probe", text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout, "PROBE\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
