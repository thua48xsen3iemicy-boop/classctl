"""Exercise per-host convergence against a local Git branch and a stub host installer."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("update", Path(__file__).parent / "classctl/files/update.py")
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class Deployment(unittest.TestCase):
    def test_independent_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()

            def git(*args):
                return subprocess.check_output(["git", "-C", repo, *args], text=True).strip()

            git("init", "-b", "main")
            host = repo / "ops/ssh/host.sh"
            host.parent.mkdir(parents=True)
            host.write_text('''set -eu
echo "$1" >> "$TEST_STATE/calls"
case "$1" in
  release) touch "$TEST_BASE/$2/.ready" ;;
  student|server) test ! -f "$TEST_STATE/fail" ;;
esac
''')
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "first")

            def run(role, action):
                base, state, config = (root / role / name for name in ("base", "state", "config"))
                state.mkdir(parents=True, exist_ok=True)
                identity = "node" if role == "student" else "server"
                folder = config / identity
                folder.mkdir(parents=True, exist_ok=True)
                with patch.multiple(update, BASE=base, CONFIG=config, STATE=state), patch.dict(
                    os.environ, TEST_BASE=str(base), TEST_STATE=str(state)
                ):
                    if not (folder / "cert.pem").exists():
                        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                                        "-subj", "/CN=" + identity, "-keyout", folder / "key.pem",
                                        "-days", "1", "-out", folder / "cert.pem"], check=True,
                                       stderr=subprocess.DEVNULL)
                        (folder / "ca.pem").write_bytes((folder / "cert.pem").read_bytes())
                        if role == "server":
                            (config / "room.json").write_text('{"roomId":"lab"}')
                            (config / "policy.json").write_text("{}")
                    args = ("pc-01", "https://teacher.test:8767") if role == "student" else ("8767", "8768")
                    return update.update(action, repo.as_uri(), "main", role, *args)

            first = git("rev-parse", "HEAD")
            for role in ("server", "student"):
                self.assertFalse(run(role, "current"))
                self.assertTrue(run(role, "apply"))
                calls = root / role / "state/calls"
                previous = calls.read_text()
                self.assertTrue(run(role, "current"))
                self.assertTrue(run(role, "apply"))
                self.assertEqual(calls.read_text(), previous)

                key = root / role / "config" / ("server" if role == "server" else "node") / "key.pem"
                saved_key = key.read_bytes()
                key.unlink()
                for action in ("current", "apply"):
                    with self.assertRaisesRegex(ValueError, "Missing: .*key.pem"):
                        run(role, action)
                self.assertFalse(key.exists())
                key.write_bytes(saved_key)
                self.assertEqual(calls.read_text(), previous)

            student_key = root / "student/config/node/key.pem"
            saved_key = student_key.read_bytes()
            student_key.write_bytes((root / "server/config/server/key.pem").read_bytes())
            with self.assertRaisesRegex(ValueError, "does not match"):
                run("student", "current")
            student_key.write_bytes(saved_key)

            git("-c", "user.name=Test", "-c", "user.email=test@example.test",
                "commit", "--allow-empty", "-m", "second")
            self.assertTrue(run("server", "apply"))
            marker = root / "student/state/puppet-student.json"
            self.assertEqual(json.loads(marker.read_text())[0], first)
            fail = marker.with_name("fail")
            fail.touch()
            with self.assertRaises(subprocess.CalledProcessError):
                run("student", "apply")
            self.assertFalse(run("student", "current"))
            fail.unlink()
            self.assertTrue(run("student", "apply"))
            self.assertEqual(json.loads(marker.read_text())[0], git("rev-parse", "HEAD"))
            (root / "server/config/policy.json").write_text('{"nodes":{}}')
            self.assertFalse(run("server", "current"))
            self.assertTrue(run("server", "apply"))


if __name__ == "__main__":
    unittest.main()
