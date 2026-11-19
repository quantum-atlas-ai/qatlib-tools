"""Regression tests for the host prerequisites used by setup_runner.sh."""
import tests._bootstrap as _bootstrap  # noqa: F401
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[3] / "src/qatlib_env/setup_runner.sh"


class SetupRunnerPrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls"
        (self.bin / "cat").symlink_to("/bin/cat")
        self.env = {
            **os.environ,
            "PATH": str(self.bin),
            "CALL_LOG": str(self.log),
            "SETUP_SCRIPT_DIR": str(SCRIPT.parent),
            "QATLIB_CI_ROOT": str(self.root / "lanes"),
            "QATLIB_SYSTEMD_DIR": str(self.root / "systemd"),
            "QATLIB_LANES": "dev",
            "MISSING_PACKAGES": "",
            "CC_STATUS": "0",
            "APT_UPDATE_STATUS": "0",
            "APT_INSTALL_STATUS": "0",
        }
        self.stub("id", "printf '0\\n'")
        self.stub("dirname", 'printf "%s\\n" "$SETUP_SCRIPT_DIR"')
        self.stub("dpkg-query", '''
case " $MISSING_PACKAGES " in *" ${!#} "*) exit 1;; esac
printf installed
''')
        self.stub("apt-get", '''
printf 'apt-get %s\\n' "$*" >> "$CALL_LOG"
if [ "$1" = update ]; then exit "$APT_UPDATE_STATUS"; fi
exit "$APT_INSTALL_STATUS"
''')
        self.stub("cc", '''
printf 'cc %s\\n' "$*" >> "$CALL_LOG"
while IFS= read -r line; do printf '%s\\n' "$line" >> "$CALL_LOG"; done
exit "$CC_STATUS"
''')
        # Stop at the first lane mutation; successful preflight must reach here.
        self.stub("install", 'printf "lane-install\\n" >> "$CALL_LOG"; exit 97')
        for tool in ("curl", "git", "python3", "runuser", "systemctl", "tar", "useradd", "zstd"):
            self.stub(tool, "exit 98")

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -eu\n" + body + "\n")
        path.chmod(0o755)

    def run_setup(self, *arguments):
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), *arguments], env=self.env,
            capture_output=True, text=True, timeout=10,
        )
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return result, calls

    def test_fresh_host_installs_build_essential_before_c_probe_and_lane_setup(self):
        self.env["MISSING_PACKAGES"] = "build-essential curl git python3 zstd"
        result, calls = self.run_setup("init")
        self.assertEqual(result.returncode, 97, result.stderr)
        self.assertEqual(calls[0], "apt-get update")
        self.assertEqual(set(calls[1].split()[2:]), {
            "-y", "--no-install-recommends", "build-essential", "curl", "git", "python3", "zstd",
        })
        self.assertTrue(calls[2].startswith("cc "))
        self.assertEqual(set(calls[3:-1]), {
            "#include <stdio.h>", "#include <stdlib.h>", "#include <stdint.h>",
        })
        self.assertEqual(calls[-1], "lane-install")

    def test_provisioned_host_skips_apt_and_still_probes_c_headers(self):
        result, calls = self.run_setup()
        self.assertEqual(result.returncode, 97, result.stderr)
        self.assertTrue(calls[0].startswith("cc "))
        self.assertFalse(any(call.startswith("apt-get") for call in calls))
        self.assertEqual(calls[-1], "lane-install")

    def test_failed_package_install_stops_before_probe_or_lane_setup(self):
        self.env.update(MISSING_PACKAGES="build-essential", APT_INSTALL_STATUS="42")
        result, calls = self.run_setup("init")
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call.startswith("apt-get") for call in calls))

    def test_missing_cc_stops_before_lane_setup_with_actionable_error(self):
        # Omit cc from the isolated PATH even when the test host has a compiler.
        self.env["PATH"] = str(self.root / "without-cc")
        without_cc = Path(self.env["PATH"])
        without_cc.mkdir()
        for path in self.bin.iterdir():
            if path.name != "cc":
                (without_cc / path.name).symlink_to(path)
        result, calls = self.run_setup("init")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Host prerequisite is unavailable after installation: cc", result.stderr)
        self.assertEqual(calls, [])

    def test_missing_c_headers_stop_init_and_registration(self):
        self.env["CC_STATUS"] = "1"
        (self.root / "lanes/dev/runners").mkdir(parents=True)
        for arguments in (
            ("init",),
            ("register", "dev", "build-01", "--url", "https://github.com/example/repo"),
        ):
            with self.subTest(arguments=arguments):
                result, calls = self.run_setup(*arguments)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("Host C standard headers are unavailable", result.stderr)
                self.assertIn("build-essential", result.stderr)
                self.assertNotIn("lane-install", calls)

    def test_help_does_not_install_packages_or_probe_compiler(self):
        result, calls = self.run_setup("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)
        self.assertEqual(calls, [])

    def test_init_copies_one_complete_distribution_for_the_runner_account(self):
        # Exercise real file copies without root or network access. Stop at the
        # package install boundary, before elan/runner/systemd provisioning.
        pip = self.root / "lanes/dev/venv/bin/pip"
        pip.parent.mkdir(parents=True)
        pip.write_text("#!/bin/sh\n")
        pip.chmod(0o755)
        self.stub("install", '''
arguments=()
while [ "$#" -gt 0 ]; do
  case "$1" in -o|-g) shift 2;; *) arguments+=("$1"); shift;; esac
done
exec /usr/bin/install "${arguments[@]}"
''')
        self.stub("runuser", '''
printf 'runuser %s\\n' "$*" >> "$CALL_LOG"
case "${!#}" in */tmp/qatlas-python-install.*) exit 97;; esac
exit 0
''')
        (self.bin / "mktemp").symlink_to("/usr/bin/mktemp")
        (self.bin / "cp").symlink_to("/bin/cp")
        (self.bin / "chmod").symlink_to("/usr/bin/chmod")
        self.stub("chown", "exit 0")
        # A prior setup used this fixed staging directory for separate packages.
        # A new init must not ship stale child distribution metadata or modules.
        stale = self.root / "lanes/dev/tmp/qatlas-python-install/qatlib_env/pyproject.toml"
        stale.parent.mkdir(parents=True)
        stale.write_text("old child distribution\n")
        result, calls = self.run_setup("init")
        self.assertEqual(result.returncode, 97, result.stderr)
        source, = (self.root / "lanes/dev/tmp").glob("qatlas-python-install.*")
        host_slice = (self.root / "systemd/qatlas-ci.slice").read_text()
        self.assertIn("MemoryHigh=90%", host_slice)
        self.assertIn("MemoryMax=95%", host_slice)
        self.assertTrue((source / "pyproject.toml").is_file())
        self.assertEqual(stale.read_text(), "old child distribution\n")
        for package in ("qatlib_env", "qatlib_forge", "qatlib_search"):
            self.assertTrue((source / "src" / package / "__init__.py").is_file(), package)
            self.assertFalse((source / "src" / package / "pyproject.toml").exists(), package)
        self.assertEqual((source / "README.md").read_bytes(), (SCRIPT.parents[2] / "README.md").read_bytes())
        for resource in ("qatlib_env/setup_runner.sh", "qatlib_search/retrieve.py",
                         "qatlib_search/lean_source.py"):
            self.assertEqual((source / "src" / resource).read_bytes(),
                             (SCRIPT.parent.parent / resource).read_bytes())
        self.assertIn("--no-deps --no-build-isolation " + str(source), calls[-1])
