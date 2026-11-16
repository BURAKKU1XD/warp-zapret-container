"""Release hygiene checks; never inspect or mount real WARP state."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_general import ROOT, run_in_image


class PublicationTests(unittest.TestCase):
    def test_local_diagnostics_absent_from_tracked_files_and_reachable_history(self):
        for args in (["ls-files", "--", ".planning"],
                     ["rev-list", "--all", "--", ".planning"]):
            with self.subTest(args=args):
                result = subprocess.run(
                    ["git", *args], cwd=ROOT, capture_output=True, check=True, timeout=30,
                )
                self.assertEqual(result.stdout.strip(), b"")

    def test_git_ignores_private_files_but_keeps_public_examples(self):
        private = [
            ".env", ".env.production", ".planning/debug/local.md",
            "warp_state/reg.json", "cloudflare-warp/reg.json",
            "var/lib/cloudflare-warp/settings.json", "backups/state.tar.gz",
            "registration.json", "mdm.xml", "client.key", "client.pem",
            "docker-compose.override.yml",
        ]
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--stdin"], cwd=ROOT,
            input=("\n".join(private) + "\n").encode(),
            capture_output=True, check=True, timeout=15,
        )
        self.assertEqual(set(result.stdout.decode().splitlines()), set(private))
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", ".env.example",
             "config/strategies/general.conf",
             "tests/fixtures/strategies-1.10.4/manifest.json",
             "assets/fake/stun2.bin"],
            cwd=ROOT, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")

    def test_real_docker_context_contains_only_allowed_public_inputs(self):
        public = [
            "Dockerfile", "entrypoint.sh", "healthcheck.sh", "LICENSE",
            "THIRD_PARTY_NOTICES.md", "licenses/upstream.txt",
            "config/strategies/general.conf", "assets/fake/public.bin",
            "assets/lists/public.txt",
        ]
        private = [
            ".env", ".env.example", ".git/config", ".planning/debug/local.md",
            "warp_state/reg.json", "cloudflare-warp/reg.json",
            "tests/test_local.py", "tools/local.py", "README.md",
            "config/client.key", "assets/fake/registration.json",
            "assets/lists/local.env", "assets/fake/cloudflare-warp/reg.json",
        ]
        with tempfile.TemporaryDirectory(prefix="warp-publication-") as directory:
            context, output = Path(directory) / "context", Path(directory) / "output"
            context.mkdir()
            (context / ".dockerignore").write_bytes((ROOT / ".dockerignore").read_bytes())
            for name in public + private:
                path = context / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic fixture only\n", encoding="utf-8")
            subprocess.run(
                ["docker", "build", "--network", "none", "--file", "-",
                 "--output", "type=local,dest=" + str(output), str(context)],
                input=b"FROM scratch\nCOPY . /context/\n",
                capture_output=True, check=True, timeout=60,
            )
            actual = {p.relative_to(output / "context").as_posix()
                      for p in (output / "context").rglob("*") if p.is_file()}
            self.assertEqual(actual, set(public))

    def test_licenses_and_runtime_package_notices_are_in_image(self):
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md",
                     "licenses/zapret-MIT.txt",
                     "licenses/zapret-discord-youtube-1.10.4.txt"):
            with self.subTest(file=name):
                output = run_in_image("cat /usr/share/doc/warp-zapret-container/" + name)
                self.assertEqual(output.decode(), (ROOT / name).read_text(encoding="utf-8"))
        run_in_image(
            "set -eu; for package in bash curl socat busybox-static nfqws; do "
            'test -s "/usr/share/doc/$package/copyright"; done'
        )

    def test_linux_line_endings_and_original_batch_bytes_are_preserved(self):
        paths = ["entrypoint.sh", "healthcheck.sh", "Dockerfile",
                 "docker-compose.yml", ".github/workflows/ci.yml"]
        for name in paths:
            with self.subTest(file=name):
                self.assertNotIn(b"\r", (ROOT / name).read_bytes())
        fixture = ROOT / "tests/fixtures/strategies-1.10.4"
        manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
        for item in manifest["strategies"].values():
            name = "tests/fixtures/strategies-1.10.4/" + item["source"]
            filtered = subprocess.run(
                ["git", "hash-object", "--path=" + name, name],
                cwd=ROOT, capture_output=True, check=True, timeout=10,
            ).stdout
            raw = subprocess.run(
                ["git", "hash-object", "--no-filters", name],
                cwd=ROOT, capture_output=True, check=True, timeout=10,
            ).stdout
            self.assertEqual(filtered, raw, name)


if __name__ == "__main__":
    unittest.main()
