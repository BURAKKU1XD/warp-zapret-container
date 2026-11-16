"""Exercise DNS setup offline against temporary files; never alter host DNS."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BASH = str(GIT_BASH) if os.name == "nt" and GIT_BASH.exists() else shutil.which("bash")


@unittest.skipUnless(BASH, "Bash is required")
class DnsTests(unittest.TestCase):
    def configure(self, directory, servers=None):
        env = dict(os.environ)
        env.pop("DNS_SERVERS", None)
        if servers is not None:
            env["DNS_SERVERS"] = servers
        return subprocess.run(
            [BASH, "-c", 'set -euo pipefail; source "$1"; configure_dns "$2" "$3"',
             "dns-test", (ROOT / "runtime.sh").as_posix(),
             (directory / "resolv.conf").as_posix(), (directory / "original").as_posix()],
            env=env, capture_output=True, text=True, timeout=10,
        )

    def test_default_empty_and_overrides_replace_inherited_resolvers(self):
        for value, expected in (
            (None, ["1.1.1.1", "8.8.8.8"]), ("", ["1.1.1.1", "8.8.8.8"]),
            ("9.9.9.9", ["9.9.9.9"]),
            (" 10.0.0.53\t1.0.0.1\n8.8.4.4 ", ["10.0.0.53", "1.0.0.1", "8.8.4.4"]),
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                original = "nameserver 127.0.0.11\nsearch cluster.local\noptions ndots:5\n"
                (directory / "resolv.conf").write_text(original)
                result = self.configure(directory, value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((directory / "resolv.conf").read_text(),
                                 "".join(f"nameserver {ip}\n" for ip in expected))
                self.assertEqual((directory / "original").read_text(), original)
                result = self.configure(directory, "8.8.4.4")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((directory / "original").read_text(), original)

    def test_invalid_input_does_not_modify_resolver_or_create_backup(self):
        for value in (" ", "256.1.1.1", "1.2.3", "dns.example.test", "01.1.1.1",
                      "1.1.1.1,8.8.8.8", "1.1.1.1 " * 4, "1.1.1.1; echo bad"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                (directory / "resolv.conf").write_text("nameserver 127.0.0.11\n")
                result = self.configure(directory, value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("DNS_SERVERS", result.stderr)
                self.assertEqual((directory / "resolv.conf").read_text(),
                                 "nameserver 127.0.0.11\n")
                self.assertFalse((directory / "original").exists())

    def test_write_failure_stops_setup(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "resolv.conf").mkdir()
            (directory / "original").write_text("preserved\n")
            result = self.configure(directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Cannot configure DNS", result.stderr)

    def test_entrypoint_configures_dns_before_starting_services(self):
        entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertLess(entrypoint.index("configure_dns\n"), entrypoint.index("# Runtime starts here."))
        # --dry-run must remain side-effect free.
        self.assertLess(entrypoint.index('if [ "$DRY_RUN" = 1 ]'), entrypoint.index("configure_dns\n"))


class ComposeDnsTests(unittest.TestCase):
    def test_default_and_environment_override(self):
        for value in (None, "", "9.9.9.9 1.0.0.1"):
            with self.subTest(value=value):
                env = dict(os.environ)
                env.pop("DNS_SERVERS", None)
                if value is not None:
                    env["DNS_SERVERS"] = value
                with tempfile.TemporaryDirectory() as temp:
                    empty_env = Path(temp) / "empty.env"
                    empty_env.write_text("")
                    result = subprocess.run(
                        ["docker", "compose", "--env-file", str(empty_env),
                         "config", "--format", "json"], cwd=ROOT, env=env,
                        capture_output=True, text=True, timeout=30,
                    )
                self.assertEqual(result.returncode, 0, result.stderr)
                service = json.loads(result.stdout)["services"]["warp-zapret"]
                self.assertEqual(service["environment"]["DNS_SERVERS"],
                                 value or "1.1.1.1 8.8.8.8")


if __name__ == "__main__":
    unittest.main()
