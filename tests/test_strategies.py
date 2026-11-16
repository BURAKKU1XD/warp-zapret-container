"""Offline parity, import reproducibility and real entrypoint strategy selection."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from test_general import IMAGE, ROOT, run_in_image
from tools.import_strategies import import_release, parse_batch, strategy_name


FIXTURES = ROOT / "tests/fixtures/strategies-1.10.4"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
PREFIX = r'''source <(while IFS= read -r line; do
  [[ "$line" == '# Runtime starts here.' ]] && break
  printf '%s\n' "$line"
done < /entrypoint.sh)
printf '%s\0' "${NFQWS_ARGS[@]}"
'''


def expected_arguments(name):
    # Independent normalization of the archived Windows command; do not use
    # the importer under test to calculate the expected Linux argument array.
    path = FIXTURES / MANIFEST["strategies"][name]["source"]
    profiles = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.startswith("--") or line.startswith(
            ("--filter-tcp=%GameFilterTCP%", "--filter-udp=%GameFilterUDP%")
        ):
            continue
        line = line.removesuffix("^").strip().replace("^!", "!")
        line = line.replace("%LISTS%", "/config/lists/").replace("%BIN%", "/opt/zapret/files/fake/")
        args = shlex.split(line)
        if args[-1] == "--new":
            args.pop()
        profiles.append(args)
    result = []
    for profile in profiles:
        if result:
            result.append("--new")
        result.extend(profile)
    return result


def run_entrypoint(*arguments, environment=None, net_admin=False):
    command = ["docker", "run", "--rm", "--network", "none"]
    if net_admin:
        command.extend(["--cap-add", "NET_ADMIN"])
    for name, value in (environment or {}).items():
        command.extend(["--env", name + "=" + value])
    command.extend([IMAGE, *arguments])
    return subprocess.run(command, capture_output=True, timeout=30, check=False)


class StrategyTests(unittest.TestCase):
    def test_complete_source_catalog_and_hashes(self):
        self.assertEqual(MANIFEST["version"], "1.10.4")
        self.assertFalse(MANIFEST["game_filter"])
        self.assertEqual(len(MANIFEST["strategies"]), 22)
        self.assertEqual(
            set(MANIFEST["strategies"]),
            {"general", "alt", "exp", *("alt" + str(i) for i in range(2, 14)),
             "fake-tls-auto", "fake-tls-auto-alt", "fake-tls-auto-alt2", "fake-tls-auto-alt3",
             "simple-fake", "simple-fake-alt", "simple-fake-alt2"},
        )
        self.assertEqual(
            {p.name for p in FIXTURES.glob("*.bat")},
            {item["source"] for item in MANIFEST["strategies"].values()},
        )
        for name, item in MANIFEST["strategies"].items():
            with self.subTest(strategy=name):
                self.assertEqual(
                    hashlib.sha256((FIXTURES / item["source"]).read_bytes()).hexdigest(),
                    item["sha256"],
                )
                self.assertEqual(expected_arguments(name).count("--new") + 1, item["profiles"])

    def test_all_ordered_arguments_through_real_selector(self):
        for name in MANIFEST["strategies"]:
            with self.subTest(strategy=name):
                config = f"config/strategies/{name}.conf"
                self.assertEqual(
                    run_in_image(f"cat /{config}").decode(),
                    (ROOT / config).read_text(encoding="utf-8"),
                )
                output = run_in_image(f"set -- --strategy {name};\n" + PREFIX)
                actual = output.decode().removesuffix("\0").split("\0")
                self.assertEqual(actual, expected_arguments(name))

    def test_all_config_syntax_queue_boundaries_and_dry_runs(self):
        for name in MANIFEST["strategies"]:
            with self.subTest(strategy=name):
                run_in_image(
                    f'set -eu; bash -n /config/strategies/{name}.conf; '
                    f'source /config/strategies/{name}.conf; '
                    'test "$NFQUEUE_NUM" = 200 && '
                    'test "$NFQWS_PORTS_TCP" = "80,443,2053,2083,2087,2096,8443" && '
                    'test "$NFQWS_PORTS_UDP" = "443,19294:19344,50000:50100" && '
                    'test "$NFQWS_TCP_CONNBYTES" = "1:40" && test "$NFQWS_UDP_CONNBYTES" = "1:12"',
                )
                result = run_entrypoint("--strategy", name, "--dry-run", net_admin=True)
                self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode())
                self.assertNotIn(b"starting dbus", result.stdout)

    def test_payloads_present_and_identical_in_source_and_image(self):
        for name, digest in MANIFEST["payloads"].items():
            with self.subTest(payload=name):
                self.assertEqual(hashlib.sha256((ROOT / "assets/fake" / name).read_bytes()).hexdigest(), digest)
        paths = " ".join("/opt/zapret/files/fake/" + name for name in MANIFEST["payloads"])
        output = run_in_image("/bin/busybox sha256sum " + paths)
        actual = {Path(path).name: digest for digest, path in
                  (line.split() for line in output.decode().splitlines())}
        self.assertEqual(actual, MANIFEST["payloads"])

    def test_selector_defaults_environment_and_legacy_config(self):
        cases = [
            ("general", "set --;"),
            ("alt2", "export ZAPRET_STRATEGY=alt2; set --;"),
            ("alt12", "export ZAPRET_STRATEGY=alt2 CONFIG=/config/strategies/alt12.conf; set --;"),
            ("general", "export ZAPRET_STRATEGY=alt2 CONFIG=/missing; set -- --strategy general;"),
            ("general", "export CONFIG=/config/zapret1-warp.conf; set --;"),
        ]
        for name, setup in cases:
            with self.subTest(setup=setup):
                output = run_in_image(setup + "\n" + PREFIX)
                self.assertEqual(output.decode().removesuffix("\0").split("\0"), expected_arguments(name))

    def test_help_and_catalog_need_no_capabilities_or_warp(self):
        result = run_entrypoint("--list-strategies", environment={"CONFIG": "/missing"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode().splitlines(), sorted(MANIFEST["strategies"]))
        result = run_entrypoint("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"--strategy", result.stdout)

    def test_invalid_selection_exits_before_startup(self):
        cases = [
            (("--strategy",), {}),
            (("--strategy", "--dry-run"), {}),
            (("--strategy", ""), {}),
            (("--strategy", "missing"), {}),
            (("--strategy", "../../entrypoint"), {}),
            (("--strategy", "/config/zapret1-warp.conf"), {}),
            (("--strategy", "general;echo bad"), {}),
            (("--typo",), {}),
            ((), {"ZAPRET_STRATEGY": "../general"}),
            ((), {"CONFIG": "/missing"}),
        ]
        for arguments, environment in cases:
            with self.subTest(arguments=arguments, environment=environment):
                result = run_entrypoint(*arguments, environment=environment)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"[!]", result.stderr)

    def test_compose_strategy_interpolation_and_defaults(self):
        for name in ("general", "alt12"):
            env = dict(os.environ, ZAPRET_STRATEGY=name)
            result = subprocess.run(
                ["docker", "compose", "config", "--format", "json"], cwd=ROOT,
                env=env, capture_output=True, check=True, timeout=30,
            )
            service = json.loads(result.stdout)["services"]["warp-zapret"]
            self.assertEqual(service["environment"]["ZAPRET_STRATEGY"], name)
            self.assertNotIn("CONFIG", service["environment"])
            self.assertEqual(int(service["mem_limit"]), 268435456)
            self.assertEqual(service["mem_limit"], service["memswap_limit"])


class ImportTests(unittest.TestCase):
    def test_parser_handles_literal_bang_and_duplicate_payload_arguments(self):
        text = (FIXTURES / "general (FAKE TLS AUTO).bat").read_text(encoding="utf-8-sig")
        args = parse_batch(text)
        self.assertIn("--dpi-desync-fake-tls=!", args)
        self.assertGreater(args.count("--dpi-desync-fake-tls=0x00000000"), 1)
        self.assertNotIn("^", " ".join(args))
        self.assertNotIn("%", " ".join(args))

    def test_parser_rejects_untranslated_variables_and_changed_boundaries(self):
        text = (FIXTURES / "general.bat").read_text(encoding="utf-8-sig")
        for broken in (
            text.replace("--wf-tcp=80,443,", "--wf-tcp=8080,443,"),
            text.replace("--dpi-desync-repeats=6", "--dpi-desync-repeats=%UNKNOWN%"),
        ):
            with self.subTest(text=broken[:20]):
                with self.assertRaises(ValueError):
                    parse_batch(broken)
        with self.assertRaises(ValueError):
            strategy_name("../general.bat")

    def test_reimport_reproduces_checked_in_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / "release", Path(directory) / "repo"
            source.mkdir()
            shutil.copytree(ROOT / "assets/fake", source / "bin")
            shutil.copytree(ROOT / "assets/lists", destination / "assets/lists")
            (destination / "assets/fake").mkdir()
            for batch in FIXTURES.glob("*.bat"):
                shutil.copyfile(batch, source / batch.name)
            result = import_release(source, destination)
            self.assertEqual(result, MANIFEST)
            for name in MANIFEST["strategies"]:
                relative = Path("config/strategies") / (name + ".conf")
                self.assertEqual((destination / relative).read_bytes(), (ROOT / relative).read_bytes())
            before = {str(p.relative_to(destination)): p.read_bytes()
                      for p in destination.rglob("*") if p.is_file()}
            import_release(source, destination)
            after = {str(p.relative_to(destination)): p.read_bytes()
                     for p in destination.rglob("*") if p.is_file()}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
