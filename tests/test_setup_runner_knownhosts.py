from __future__ import annotations

import getpass
import hashlib
import subprocess
import tempfile
import unittest
import warnings
from pathlib import Path

from qatlib_env.trash import TrashError, trash


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/qatlib_env/setup_runner.sh"
ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
ECDSA = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg="


class SetupRunnerKnownHostsTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="setup-runner-knownhosts-"))
        self.addCleanup(self.cleanup_fixture)

    def cleanup_fixture(self):
        try:
            trash(self.directory)
        except (OSError, TrashError) as error:
            warnings.warn(f"Test fixture retained at {self.directory}: {error}", RuntimeWarning)

    def run_helper(self):
        subprocess.run(["bash", str(SCRIPT), "known-hosts", str(self.directory), getpass.getuser()],
                       cwd=ROOT, check=True)

    def lookup(self, host):
        return subprocess.run(["ssh-keygen", "-F", host, "-f", str(self.directory / ".ssh/known_hosts")],
                              check=True, capture_output=True, text=True).stdout

    def test_hashed_entries_permissions_and_third_party_entries_are_preserved(self):
        ssh_dir = self.directory / ".ssh"
        ssh_dir.mkdir()
        known_hosts = ssh_dir / "known_hosts"
        known_hosts.write_text(f"github.com {ED25519}\n[ssh.github.com]:443 {ECDSA}\n", encoding="utf-8")
        subprocess.run(["ssh-keygen", "-H", "-f", str(known_hosts)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with known_hosts.open("a", encoding="utf-8") as stream:
            stream.write(f"other.example {ED25519}\n")
        self.run_helper()
        first_digest = hashlib.sha256((self.directory / ".ssh/known_hosts").read_bytes()).hexdigest()
        self.run_helper()
        self.assertEqual(first_digest, hashlib.sha256((self.directory / ".ssh/known_hosts").read_bytes()).hexdigest())
        for host in ("github.com", "[ssh.github.com]:443"):
            output = self.lookup(host)
            self.assertEqual(sum("ssh-ed25519" in line or "ecdsa-sha2" in line or "ssh-rsa" in line
                                 for line in output.splitlines()), 3)
        self.assertIn(ED25519, self.lookup("other.example"))
        self.assertEqual((self.directory / ".ssh").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.directory / ".ssh/known_hosts").stat().st_mode & 0o777, 0o600)
