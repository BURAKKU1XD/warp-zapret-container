"""Checks against the built Compose image with networking disabled and no WARP startup."""

import hashlib
from pathlib import Path
import shlex
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "warp-zapret1-container-warp-zapret1:latest"
PAYLOAD_HASHES = {
    "quic_initial_www_google_com.bin": "f4589c57749f956bb30538197a521d7005f8b0a8723b4707e72405e51ddac50a",
    "tls_clienthello_www_google_com.bin": "936c2bee4cfb80aa3c426b2dcbcc834b3fbcd1adb17172959dc569c73a14275c",
    "stun.bin": "9cd5469309780ca56c0bd97266524a48c7ee529d02c3179cfecb20b260a59641",
    "tls_clienthello_www_sferum_ru.bin": "e6dd8d724eecc6a7340bf64e2ebec81c60bc4e078c6f702b4b6ba9bf184180ad",
    "ACTIVE_DISCORD_UDP.bin": "2fe18b3bd20807d36704d0b072092ee49ae84edca907a4420ab9a0f0f28fddcf",
}


def run_in_image(script, net_admin=False):
    # nfqws sets its capability set even in --dry-run; Docker must allow NET_ADMIN.
    capabilities = ["--cap-add", "NET_ADMIN"] if net_admin else []
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none",
            *capabilities,
            "--entrypoint", "/usr/bin/bash", IMAGE, "-c", script,
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise AssertionError(
            f"Image check failed ({result.returncode}); build with docker compose build first.\n"
            + result.stdout.decode(errors="replace")
            + result.stderr.decode(errors="replace")
        )
    return result.stdout


class Alt12Tests(unittest.TestCase):
    def test_ordered_arguments_match_upstream_profiles(self):
        fixture = ROOT / "tests/fixtures/alt12-1.10.4.txt"
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
        run_in_image("bash -n /entrypoint.sh /config/zapret1-warp.conf")

    def test_nfqws_accepts_all_profiles_and_assets(self):
        run_in_image(
            'set -eu; source /config/zapret1-warp.conf; '
            'exec /usr/local/bin/nfqws --dry-run --qnum="$NFQUEUE_NUM" "${NFQWS_ARGS[@]}"',
            net_admin=True,
        )


if __name__ == "__main__":
    unittest.main()
