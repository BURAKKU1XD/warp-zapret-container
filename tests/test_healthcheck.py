"""Offline failure injection and real Docker health-state transitions."""

import json
import subprocess
import time
import unittest
import uuid

from test_general import IMAGE, ROOT, run_in_image


MOCK_SETUP = r"""
set -eu
mkdir -p /tmp/health-mocks
cat > /tmp/health-mocks/warp-cli <<'MOCK'
#!/usr/bin/bash
if [ "${DAEMON:-connected}" = hang ]; then sleep 5; fi
if [ "${DAEMON:-connected}" = disconnected ]; then
  echo 'Status update: Disconnected'
else
  echo 'Status update: Connected'
fi
MOCK
cat > /tmp/health-mocks/curl <<'MOCK'
#!/usr/bin/bash
set -eu
printf '%s\n' "$@" > "/tmp/curl-args-${BASHPID}"
route=HTTP
for arg in "$@"; do
  [[ "$arg" == socks5h://* ]] && route=SOCKS
done
mode="${HTTP_MODE:-on}"
[ "$route" = SOCKS ] && mode="${SOCKS_MODE:-on}"
[ -f /tmp/health-failure ] && mode=timeout
case "$mode" in
  timeout) exit 28 ;;
  tls) exit 60 ;;
  http-error) exit 22 ;;
  off) printf 'ip=private-marker\nwarp=off\n\n200' ;;
  missing) printf 'ip=private-marker\n\n200' ;;
  substring) printf 'notwarp=on\n\n200' ;;
  redirect) printf 'warp=on\n\n302' ;;
  crlf) printf 'ip=private-marker\r\nwarp=on\r\n\n200' ;;
  *) printf 'ip=private-marker\nwarp=on\n\n200' ;;
esac
MOCK
chmod +x /tmp/health-mocks/*
export PATH="/tmp/health-mocks:$PATH"
"""


def run_case(environment=None, real_curl=False):
    script = MOCK_SETUP
    if real_curl:
        script += "\nrm /tmp/health-mocks/curl\n"
    script += r"""
set +e
/healthcheck.sh
code=$?
echo "CHECK_EXIT=$code"
for file in /tmp/curl-args-*; do
  [ -f "$file" ] || continue
  echo CURL_ARGS
  cat "$file"
done
exit 0
"""
    args = ["docker", "run", "--rm", "-i", "--network", "none"]
    for key, value in (environment or {}).items():
        args += ["--env", key + "=" + value]
    result = subprocess.run(
        [*args, "--entrypoint", "/usr/bin/bash", IMAGE, "-s"],
        input=script.encode(), capture_output=True, timeout=20, check=True,
    )
    return result.stdout.decode()


class HealthcheckTests(unittest.TestCase):
    def test_image_script_curl_and_docker_compose_contract(self):
        self.assertEqual(
            run_in_image("cat /healthcheck.sh").decode(),
            (ROOT / "healthcheck.sh").read_text(encoding="utf-8"),
        )
        run_in_image("bash -n /healthcheck.sh; curl --version; /bin/busybox timeout 1 true")
        image = json.loads(subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, check=True, timeout=20,
        ).stdout)[0]
        self.assertEqual(image["Config"]["Healthcheck"], {
            "Test": ["CMD", "/healthcheck.sh"], "Interval": 30000000000,
            "Timeout": 12000000000, "StartPeriod": 120000000000, "Retries": 3,
        })
        service = json.loads(subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=ROOT, capture_output=True, check=True, timeout=20,
        ).stdout)["services"]["warp-zapret1"]
        self.assertEqual(service["healthcheck"], {
            "test": ["CMD", "/healthcheck.sh"], "interval": "30s",
            "timeout": "12s", "start_period": "2m0s", "retries": 3,
        })

    def test_success_uses_both_explicit_proxies_and_bounded_verified_https(self):
        output = run_case({
            "PUBLIC_HTTP_PROXY_PORT": "18888", "PUBLIC_SOCKS_PROXY_PORT": "11080",
            "HTTP_PROXY": "http://invalid:1", "HTTPS_PROXY": "http://invalid:1", "NO_PROXY": "*",
        })
        self.assertIn("CHECK_EXIT=0", output)
        calls = [part.splitlines() for part in output.split("CURL_ARGS\n")[1:]]
        self.assertEqual(len(calls), 2)
        self.assertEqual({args[args.index("--proxy") + 1] for args in calls}, {
            "http://127.0.0.1:18888", "socks5h://127.0.0.1:11080",
        })
        for args in calls:
            self.assertEqual(args[0], "--disable")
            self.assertEqual(args[args.index("--noproxy") + 1], "")
            for flag, value in (("--connect-timeout", "3"), ("--max-time", "8"),
                                ("--max-filesize", "4096"), ("--proto", "=https")):
                self.assertEqual(args[args.index(flag) + 1], value)
            self.assertNotIn("--insecure", args)
            self.assertNotIn("--retry", args)
            self.assertNotIn("--location", args)
        self.assertNotIn("private-marker", output)

    def test_either_proxy_failure_is_unhealthy_without_sensitive_output(self):
        for route in ("HTTP", "SOCKS"):
            for mode in ("timeout", "tls", "http-error", "off", "missing", "substring", "redirect"):
                with self.subTest(route=route, mode=mode):
                    output = run_case({route + "_MODE": mode})
                    self.assertIn("CHECK_EXIT=1", output)
                    self.assertIn("unhealthy: " + route + " proxy", output)
                    self.assertEqual(output.count("CURL_ARGS\n"), 2)
                    self.assertNotIn("private-marker", output)

    def test_crlf_trace_and_legacy_http_port(self):
        output = run_case({"HTTP_MODE": "crlf", "SOCKS_MODE": "crlf", "PUBLIC_PROXY_PORT": "18889"})
        self.assertIn("CHECK_EXIT=0", output)
        self.assertIn("http://127.0.0.1:18889", output)

    def test_disconnected_or_hung_cli_fails_before_network_probes(self):
        for mode in ("disconnected", "hang"):
            with self.subTest(mode=mode):
                began = time.monotonic()
                output = run_case({"DAEMON": mode})
                self.assertIn("CHECK_EXIT=1", output)
                self.assertIn("WARP is not Connected", output)
                self.assertNotIn("CURL_ARGS\n", output)
                self.assertLess(time.monotonic() - began, 10)

    def test_real_curl_cannot_bypass_missing_proxies(self):
        output = run_case({"NO_PROXY": "*"}, real_curl=True)
        self.assertIn("CHECK_EXIT=1", output)
        self.assertIn("HTTP proxy HTTPS request failed", output)
        self.assertIn("SOCKS proxy HTTPS request failed", output)

    def test_docker_transitions_healthy_unhealthy_healthy_without_restart(self):
        name = "warp-healthcheck-test-" + uuid.uuid4().hex[:12]
        command = ["docker", "run", "-d", "--name", name, "--network", "none",
                   "--health-cmd", 'PATH="/tmp/health-mocks:$PATH" /healthcheck.sh',
                   "--health-interval", "1s", "--health-start-period", "0s",
                   "--health-retries", "2", "--entrypoint", "/usr/bin/bash",
                   IMAGE, "-c", MOCK_SETUP + "\nexec sleep 120"]
        subprocess.run(command, capture_output=True, check=True, timeout=20)
        try:
            def await_health(expected):
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    state = json.loads(subprocess.run(
                        ["docker", "inspect", name], capture_output=True, check=True, timeout=10,
                    ).stdout)[0]
                    if state["State"]["Health"]["Status"] == expected:
                        self.assertEqual(state["RestartCount"], 0)
                        return state["State"]["StartedAt"]
                    time.sleep(0.5)
                self.fail("Docker did not reach " + expected)

            started_at = await_health("healthy")
            subprocess.run(["docker", "exec", name, "touch", "/tmp/health-failure"],
                           capture_output=True, check=True, timeout=10)
            self.assertEqual(await_health("unhealthy"), started_at)
            subprocess.run(["docker", "exec", name, "rm", "/tmp/health-failure"],
                           capture_output=True, check=True, timeout=10)
            self.assertEqual(await_health("healthy"), started_at)
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=True, timeout=20)


if __name__ == "__main__":
    unittest.main()
