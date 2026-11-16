"""Exercise actual workflow Bash offline; Docker login/push are mocked."""

import json
import os
import shlex
import subprocess
import unittest

from test_general import ROOT, run_in_image


WORKFLOW = ROOT / ".github/workflows/ci.yml"
SHA = "1234567890abcdef1234567890abcdef12345678"
IMAGE = "ghcr.io/burakku1xd/warp-zapret-container"


def shell_block(step_id):
    """Read only our workflow's literal run block, without a YAML dependency."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index("        id: " + step_id)
    run = next(i for i in range(start + 1, len(lines)) if lines[i] == "        run: |")
    block = []
    for line in lines[run + 1:]:
        if line and not line.startswith("          "):
            break
        block.append(line[10:] if line else "")
    return "\n".join(block)


def run_block(step_id, environment=None):
    values = {
        "GITHUB_REPOSITORY": "BURAKKU1XD/warp-zapret-container",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_SHA": SHA, "GITHUB_REF": "refs/heads/main", "GITHUB_REF_NAME": "main",
        "GITHUB_OUTPUT": "/tmp/step-output", "GITHUB_STEP_SUMMARY": "/tmp/step-summary",
        "RUNNER_TEMP": "/tmp", "LOCAL_IMAGE": "local-fixture:latest",
        "TESTED_IMAGE_ID": "sha256:fixture", "GITHUB_ACTOR": "fixture",
        "GHCR_TOKEN": "synthetic-test-token", "MOCK_IMAGE_ID": "sha256:fixture",
        "MOCK_PLATFORM": "linux/amd64",
    }
    values.update(environment or {})
    setup = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in values.items())
    script = setup + r"""
: > /tmp/step-output
: > /tmp/step-summary
: > /tmp/docker-calls
docker() {
  printf 'DOCKER' >> /tmp/docker-calls
  printf ' <%s>' "$@" >> /tmp/docker-calls
  printf '\n' >> /tmp/docker-calls
  case "$1 $2" in
    'image inspect')
      if [[ "$*" == *Architecture* ]]; then
        echo "$MOCK_PLATFORM"
      else
        echo "$MOCK_IMAGE_ID"
      fi
      ;;
    'login ghcr.io')
      received="$(cat)"
      test "$received" = "$GHCR_TOKEN"
      ;;
  esac
  return 0
}
set +e
(
set -euo pipefail
""" + shell_block(step_id) + r"""
)
code=$?
printf 'CHECK_EXIT=%s\n' "$code"
cat /tmp/step-output /tmp/docker-calls /tmp/step-summary
"""
    return run_in_image(script).decode()


class GhcrTests(unittest.TestCase):
    def test_build_selects_amd64_and_labels_public_source_revision(self):
        output = run_block("build")
        self.assertIn("CHECK_EXIT=0", output)
        self.assertIn("<build> <--platform> <linux/amd64>", output)
        self.assertIn("<org.opencontainers.image.source=https://github.com/BURAKKU1XD/warp-zapret-container>", output)
        self.assertIn("<org.opencontainers.image.revision=" + SHA + ">", output)
        self.assertNotIn("synthetic-test-token", output)

    def test_main_tags_lowercase_repository_and_full_commit_sha(self):
        output = run_block("tags")
        self.assertIn("CHECK_EXIT=0", output)
        self.assertIn(IMAGE + ":sha-" + SHA, output)
        self.assertIn(IMAGE + ":latest", output)
        self.assertNotIn("ghcr.io/BURAKKU1XD", output)

    def test_version_and_prerelease_never_update_latest(self):
        for version in ("1.2.3", "1.2.3-rc.1"):
            with self.subTest(version=version):
                output = run_block("tags", {
                    "GITHUB_REF": "refs/tags/v" + version, "GITHUB_REF_NAME": "v" + version,
                })
                self.assertIn("CHECK_EXIT=0", output)
                self.assertIn(IMAGE + ":" + version, output)
                self.assertIn(IMAGE + ":sha-" + SHA, output)
                self.assertNotIn(IMAGE + ":latest", output)

    def test_unsupported_refs_invalid_versions_or_sha_fail_before_login(self):
        cases = [
            {"GITHUB_REF": "refs/heads/feature"},
            {"GITHUB_REF": "refs/tags/vbad", "GITHUB_REF_NAME": "vbad"},
            {"GITHUB_REF": "refs/tags/v1.2.3+metadata", "GITHUB_REF_NAME": "v1.2.3+metadata"},
            {"GITHUB_REF": "refs/tags/v1.2.3", "GITHUB_REF_NAME": "v1.2.3;echo injected"},
            {"GITHUB_SHA": "invalid"},
        ]
        for environment in cases:
            with self.subTest(environment=environment):
                output = run_block("tags", environment)
                self.assertIn("CHECK_EXIT=1", output)
                self.assertNotIn("DOCKER", output)
                self.assertNotIn("tags<<EOF", output)

    def test_loaded_image_must_match_tested_id_and_platform(self):
        output = run_block("load")
        self.assertIn("CHECK_EXIT=0", output)
        self.assertIn("<load> <--input> </tmp/warp-image.tar>", output)
        for environment in ({"MOCK_IMAGE_ID": "sha256:different"},
                            {"MOCK_PLATFORM": "linux/arm64"}, {"TESTED_IMAGE_ID": ""}):
            with self.subTest(environment=environment):
                self.assertIn("CHECK_EXIT=1", run_block("load", environment))

    def test_export_saves_tested_image_without_rebuild(self):
        output = run_block("export")
        self.assertIn("CHECK_EXIT=0", output)
        self.assertIn("image_id=sha256:fixture", output)
        self.assertIn("<save> <--output> </tmp/warp-image.tar> <local-fixture:latest>", output)
        self.assertNotIn("<build>", output)

    def test_login_uses_stdin_and_push_only_tags_the_loaded_image(self):
        output = run_block("login")
        self.assertIn("CHECK_EXIT=0", output)
        self.assertIn("<login> <ghcr.io> <--username> <fixture> <--password-stdin>", output)
        self.assertNotIn("synthetic-test-token", output)
        tags = [IMAGE + ":sha-" + SHA, IMAGE + ":latest"]
        output = run_block("push", {"IMAGE_TAGS": "\n".join(tags)})
        self.assertIn("CHECK_EXIT=0", output)
        for tag in tags:
            self.assertIn("<tag> <local-fixture:latest> <" + tag + ">", output)
            self.assertIn("<push> <" + tag + ">", output)
        self.assertNotIn("<build>", output)

    def test_publish_permissions_and_artifact_transfer_are_separate_from_tests(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        tests, publish = workflow.split("\n  publish:\n", 1)
        guard = ("github.event_name != 'pull_request' && "
                 "(github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/v'))")
        self.assertNotIn("packages: write", tests)
        self.assertNotIn("secrets.GITHUB_TOKEN", tests)
        self.assertIn("needs: test", publish)
        self.assertIn("if: " + guard, publish)
        self.assertIn("packages: write", publish)
        self.assertNotIn("docker build", publish)
        self.assertNotIn("actions/checkout", publish)
        self.assertIn("path: ${{ runner.temp }}/warp-image.tar", tests)
        self.assertIn("retention-days: 1", tests)
        self.assertEqual(tests.count("if: " + guard), 2)
        self.assertIn("if: always()", publish)
        self.assertIn("docker logout ghcr.io", publish)

    def test_compose_registry_override_keeps_loopback_state_and_limits(self):
        env = dict(os.environ, WARP_ZAPRET_IMAGE=IMAGE + ":1.2.3")
        service = json.loads(subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=ROOT, env=env, capture_output=True, check=True, timeout=20,
        ).stdout)["services"]["warp-zapret1"]
        self.assertEqual(service["image"], IMAGE + ":1.2.3")
        self.assertEqual(int(service["mem_limit"]), 268435456)
        self.assertEqual(service["mem_limit"], service["memswap_limit"])
        self.assertTrue(all(port["host_ip"] == "127.0.0.1" for port in service["ports"]))
        self.assertEqual(service["volumes"][0]["target"], "/var/lib/cloudflare-warp")


if __name__ == "__main__":
    unittest.main()
