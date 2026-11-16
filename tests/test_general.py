"""Checks against the built Compose image with networking disabled and no WARP startup."""

import hashlib
from pathlib import Path
import shlex
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "warp-zapret:latest"
PAYLOAD_HASHES = {
    "quic_initial_www_google_com.bin": "f4589c57749f956bb30538197a521d7005f8b0a8723b4707e72405e51ddac50a",
    "tls_clienthello_www_google_com.bin": "936c2bee4cfb80aa3c426b2dcbcc834b3fbcd1adb17172959dc569c73a14275c",
    "tls_clienthello_4pda_to.bin": "eefeaf09dde8d69b1f176212541f63c68b314a33a335eced99a8a29f17254da8",
    "ACTIVE_DISCORD_UDP.bin": "2fe18b3bd20807d36704d0b072092ee49ae84edca907a4420ab9a0f0f28fddcf",
}


def run_in_image(script, net_admin=False):
    # nfqws sets its capability set even in --dry-run; Docker must allow NET_ADMIN.
    capabilities = ["--cap-add", "NET_ADMIN"] if net_admin else []
    result = subprocess.run(
        [
            # Individual health-state tests enable their own explicit probes.
            "docker", "run", "--rm", "--network", "none", "--no-healthcheck",
            *capabilities,
            "--entrypoint", "/usr/bin/bash", IMAGE, "-c", script,
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise AssertionError(
            f"Image check failed ({result.returncode}); build with docker build -t {IMAGE} . first.\n"
            + result.stdout.decode(errors="replace")
            + result.stderr.decode(errors="replace")
        )
    return result.stdout


class GeneralTests(unittest.TestCase):
    def test_ordered_arguments_match_upstream_profiles(self):
        fixture = ROOT / "tests/fixtures/general-1.10.4.txt"
        profiles = []
        for line in fixture.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            normalized = line.replace("%LISTS%", "/config/lists/")
            normalized = normalized.replace("%BIN%", "/opt/zapret/files/fake/")
            profiles.append(shlex.split(normalized))
        self.assertEqual(len(profiles), 7)

        expected = []
        for profile in profiles:
            if expected:
                expected.append("--new")
            expected.extend(profile)
        output = run_in_image(
            'set -eu; source /config/zapret1-warp.conf; printf "%s\\0" "${NFQWS_ARGS[@]}"'
        )
        actual = output.decode().removesuffix("\0").split("\0")
        self.assertEqual(actual, expected)

    def test_warp_queue_boundaries_are_preserved(self):
        output = run_in_image(
            'set -eu; source /config/zapret1-warp.conf; '
            'printf "%s\\n" "$NFQUEUE_NUM" "$NFQWS_PORTS_TCP" "$NFQWS_PORTS_UDP" '
            '"$NFQWS_TCP_CONNBYTES" "$NFQWS_UDP_CONNBYTES"'
        )
        self.assertEqual(output.decode().splitlines(), [
            "200", "80,443,2053,2083,2087,2096,8443",
            "443,19294:19344,50000:50100", "1:40", "1:12",
        ])

    def test_source_payloads_match_upstream_hashes(self):
        for name, expected in PAYLOAD_HASHES.items():
            with self.subTest(payload=name):
                payload = ROOT / "assets/fake" / name
                self.assertEqual(hashlib.sha256(payload.read_bytes()).hexdigest(), expected)

    def test_image_payloads_match_upstream_hashes(self):
        paths = " ".join(f"/opt/zapret/files/fake/{name}" for name in PAYLOAD_HASHES)
        output = run_in_image(f"/bin/busybox sha256sum {paths}")
        actual = {
            Path(path).name: digest
            for digest, path in (line.split() for line in output.decode().splitlines())
        }
        self.assertEqual(actual, PAYLOAD_HASHES)

    def test_bash_syntax(self):
        run_in_image("bash -n /entrypoint.sh && bash -n /config/zapret1-warp.conf")

    def test_image_configuration_matches_source(self):
        for name in ("entrypoint.sh", "config/zapret1-warp.conf"):
            with self.subTest(file=name):
                source = (ROOT / name).read_text(encoding="utf-8")
                source = source.replace("#!/usr/bin/env bash", "#!/usr/bin/bash", 1)
                self.assertEqual(run_in_image(f"cat /{name}").decode(), source)

    def test_generated_mark_order_idempotence_and_cleanup(self):
        # Load only definitions; do not start dbus, nfqws or WARP.
        output = run_in_image(
            r'''set -eu
source <(while IFS= read -r line; do
  [[ "$line" == '# Runtime starts here.' ]] && break
  printf '%s\n' "$line"
done < /entrypoint.sh)
iptables -t mangle -A OUTPUT -m mark --mark 0x40000000/0x40000000 -j ACCEPT
for i in 1 2; do
  iptables -t mangle -A OUTPUT -m mark --mark 0x40000000/0x40000000 \
    -m comment --comment warp-zapret1-generated -j ACCEPT
done
setup_rules >/dev/null
setup_rules >/dev/null
echo SETUP
iptables -t mangle -S OUTPUT
cleanup_rules
cleanup_rules
echo CLEANUP
iptables -t mangle -S OUTPUT
''',
            net_admin=True,
        ).decode()
        installed, cleaned = output.split("CLEANUP\n")
        rules = [line for line in installed.splitlines() if line.startswith("-A OUTPUT")]
        self.assertEqual(len(rules), 4)
        self.assertIn("--mark 0x40000000/0x40000000", rules[0])
        self.assertIn("warp-zapret1-generated", rules[0])
        self.assertTrue(rules[0].endswith("-j ACCEPT"))
        self.assertIn("-p udp", rules[2])
        self.assertIn("-p tcp", rules[3])
        self.assertIn("--dports 443,19294:19344,50000:50100", rules[2])
        self.assertIn("--connbytes 1:12", rules[2])
        self.assertIn("--dports 80,443,2053,2083,2087,2096,8443", rules[3])
        self.assertIn("--connbytes 1:40", rules[3])
        for rule in rules[2:]:
            self.assertIn("! -d 127.0.0.0/8", rule)
            self.assertIn("--connbytes-mode packets --connbytes-dir original", rule)
            self.assertIn("-j NFQUEUE --queue-num 200 --queue-bypass", rule)
        remaining = [line for line in cleaned.splitlines() if line.startswith("-A OUTPUT")]
        self.assertEqual(len(remaining), 1)
        self.assertIn("--mark 0x40000000/0x40000000 -j ACCEPT", remaining[0])
        self.assertNotIn("warp-zapret1-generated", remaining[0])

    def test_nfqws_accepts_all_profiles_and_assets(self):
        run_in_image(
            'set -eu; source /config/zapret1-warp.conf; '
            'exec /usr/local/bin/nfqws --dry-run --qnum="$NFQUEUE_NUM" "${NFQWS_ARGS[@]}"',
            net_admin=True,
        )


if __name__ == "__main__":
    unittest.main()
