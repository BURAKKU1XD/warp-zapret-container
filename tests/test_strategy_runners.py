"""Execute both host runners offline with fake Docker/curl; no WARP registration."""

import csv
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
if os.name == "nt":
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    BASH = str(git_bash) if git_bash.exists() else None
# Prefer the actual Windows PowerShell executable over a Store execution alias.
POWERSHELL = (shutil.which("powershell") or shutil.which("pwsh")) if os.name == "nt" else shutil.which("pwsh")
CATALOG = ["general", "alt", "alt2", "alt3", "alt4", "alt5"]

MOCK = r'''
import json
import os
from pathlib import Path
import sys

sys.stdout.reconfigure(newline="\n")
root = Path(os.environ["RUNNER_MOCK"])
tool, *args = sys.argv[1:]
with (root / "commands.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps([tool, *args]) + "\n")
state_file = root / "state.json"
state = json.loads(state_file.read_text()) if state_file.exists() else {}
scenario = os.environ.get("RUNNER_SCENARIO", "")

def save():
    state_file.write_text(json.dumps(state))

if tool == "docker":
    if args[0] == "info":
        print("linux")
    elif args[:2] == ["image", "inspect"]:
        if "--format" not in args:
            if scenario == "missing" and not state.get("pulled"):
                print("Error: no such image", file=sys.stderr)
                sys.exit(1)
            print("[]")
        elif "{{.Id}}" in args:
            print("sha256:" + "a" * 64)
        else:
            print("linux/amd64")
    elif args[0] == "pull":
        state["pulled"] = True
        save()
        print("pulled")
    elif args[0] == "run":
        print("general\nalt\nalt2\nalt3\nalt4\nalt5")
    elif args[:2] == ["volume", "create"]:
        state["volume"] = args[-1]
        save()
        print(args[-1])
    elif args[:2] == ["volume", "rm"]:
        assert args[-1] == state["volume"], "attempt to delete an unowned volume"
        if scenario == "cleanup-fail":
            sys.exit(1)
        state.pop("volume")
        save()
    elif args[0] == "create":
        if scenario == "create-fail":
            sys.exit(125)
        strategy = args[-1]
        assert "active" not in state, "strategies overlapped"
        state["active"] = "cid-" + strategy
        state["strategy"] = strategy
        save()
        print(state["active"])
    elif args[0] in ("start", "stop"):
        assert args[-1] == state["active"]
        print(args[-1])
    elif args[0] == "inspect":
        assert args[-1] == state["active"]
        print("false" if state["strategy"] == "alt" else "true")
    elif args[0] == "exec":
        sys.exit(1 if state["strategy"] == "alt5" else 0)
    elif args[0] == "port":
        assert args[1] == state["active"]
        print("127.0.0.1:" + ("18888" if args[-1] == "8888/tcp" else "11080"))
    elif args[0] == "rm":
        assert args[-1] == state["active"], "attempt to delete an unowned container"
        state.pop("active")
        state.pop("strategy")
        save()
    else:
        raise AssertionError(args)
elif tool == "curl":
    assert args[0] == "--disable"
    assert args[args.index("--noproxy") + 1] == ","
    assert args[args.index("--proto") + 1] == "=https"
    assert "--insecure" not in args
    proxy = args[args.index("--proxy") + 1]
    assert proxy in ("http://127.0.0.1:18888", "socks5h://127.0.0.1:11080")
    strategy = state["strategy"]
    trace = args[-1].endswith("/cdn-cgi/trace")
    if scenario == "no-pass" or strategy == "alt3":
        print("\n000\t0.010")
        sys.exit(60)
    if trace:
        assert "--head" not in args
        print("ip=192.0.2.123\nregistration=DO_NOT_SAVE\nwarp=" +
              ("off" if strategy == "alt2" else "on"))
        print("\n200\t0.010")
    else:
        if scenario == "body-fail" and "--head" not in args:
            print("\n200\t0.020")
            sys.exit(18)  # headers succeeded, response body was truncated
        print("\n" + ("503" if strategy == "alt4" else "204") + "\t0.020")
else:
    raise AssertionError(tool)
'''


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runners = []
        if BASH:
            cls.runners.append("bash")
        if POWERSHELL:
            cls.runners.append("powershell")
        if not cls.runners:
            raise unittest.SkipTest("Neither Bash nor PowerShell is available")

    def run_runner(self, runner, *, strategies=None, scenario="", targets=None, pull="never", rounds=1):
        preferred = Path(os.environ.get("LOCALAPPDATA", "")) / "Temp/opencode"
        temporary = tempfile.TemporaryDirectory(
            prefix="strategy-runners-", dir=preferred if preferred.is_dir() else None,
        )
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        mock = directory / "mock.py"
        mock.write_text(MOCK, encoding="utf-8")
        target_file = directory / "targets.txt"
        target_file.write_text(targets if targets is not None else
                               "# public test endpoint\n\nhttps://example.com/health\n", encoding="utf-8")
        env = dict(os.environ, RUNNER_MOCK=str(directory), RUNNER_SCENARIO=scenario,
                   MSYS_NO_PATHCONV="1", NO_PROXY="*", HTTPS_PROXY="http://invalid.invalid:9")
        output = directory / "output with spaces"
        if runner == "bash":
            for tool in ("docker", "curl"):
                shim = directory / tool
                shim.write_text(
                    "#!/usr/bin/env bash\nexec " +
                    shlex.quote(Path(sys.executable).as_posix()) + " " +
                    shlex.quote(mock.as_posix()) + " " + tool + ' "$@"\n',
                    encoding="utf-8",
                )
                shim.chmod(0o755)
            env["PATH"] = str(directory) + os.pathsep + env["PATH"]
            # Git Bash prepends its own curl directory when it starts. Override
            # PATH inside the shell so that no test can invoke the real curl.
            shim_path = '$(cygpath -u "$1")' if os.name == "nt" else '$1'
            arguments = [
                BASH, "-c", f'export PATH="{shim_path}:$PATH"; exec bash "$2" "${{@:3}}"',
                "runner-test", str(directory), str(ROOT / "test-strategies.sh"),
                "--image", "mock:test", "--pull", pull,
                "--rounds", str(rounds), "--startup-timeout", "3", "--request-timeout", "1",
                "--targets-file", str(target_file), "--output", str(output),
            ]
            if strategies:
                arguments += ["--strategies", ",".join(strategies)]
        else:
            def quote(value):
                return "'" + str(value).replace("'", "''") + "'"

            # Functions shadow native commands only inside this test process.
            wrapper = directory / "invoke.ps1"
            prefix = "& " + quote(sys.executable) + " " + quote(mock)
            script = (
                "function global:docker { " + prefix + " docker @args }\n"
                "function global:curl.exe { " + prefix + " curl @args }\n"
                "& " + quote(ROOT / "test-strategies.ps1") +
                f" -Image mock:test -Pull {pull} -Rounds {rounds} -StartupTimeout 1 -RequestTimeout 1" +
                " -TargetsFile " + quote(target_file) + " -OutputDirectory " + quote(output)
            )
            if strategies:
                script += " -Strategies @(" + ",".join(map(quote, strategies)) + ")"
            wrapper.write_text(script + "\nexit $LASTEXITCODE\n", encoding="utf-8")
            arguments = [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(wrapper)]
        result = subprocess.run(arguments, env=env, capture_output=True, text=True, timeout=90)
        self.assertNotIn("192.0.2.123", result.stdout + result.stderr)
        self.assertNotIn("DO_NOT_SAVE", result.stdout + result.stderr)
        reports = list(output.glob("*/summary.tsv"))
        summary, checks = [], []
        if reports:
            with reports[0].open(encoding="utf-8-sig", newline="") as stream:
                summary = list(csv.DictReader(stream, delimiter="\t"))
            with (reports[0].parent / "checks.tsv").open(encoding="utf-8-sig", newline="") as stream:
                checks = list(csv.DictReader(stream, delimiter="\t"))
            for path in reports[0].parent.iterdir():
                text = path.read_text(encoding="utf-8-sig")
                self.assertNotIn("192.0.2.123", text)
                self.assertNotIn("DO_NOT_SAVE", text)
        log = directory / "commands.jsonl"
        commands = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        state = directory / "state.json"
        remaining = json.loads(state.read_text()) if state.exists() else {}
        return result, summary, checks, commands, remaining

    def test_all_strategies_outcomes_and_isolation(self):
        for runner in self.runners:
            with self.subTest(runner=runner):
                result, summary, checks, commands, remaining = self.run_runner(runner)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual([row["strategy"] for row in summary], CATALOG)
                self.assertEqual([row["status"] for row in summary],
                                 ["PASS", "STARTUP_FAIL", "PARTIAL", "PARTIAL", "PARTIAL", "STARTUP_FAIL"])
                self.assertEqual([row["passed"] for row in summary], ["4", "0", "0", "0", "2", "0"])
                self.assertTrue(all(row["total"] == "4" for row in summary))
                self.assertEqual(len(checks), 16)
                self.assertEqual(sum(row["status"] == "SKIP" for row in checks), 4)
                self.assertEqual(remaining, {})
                creates = [c for c in commands if c[:2] == ["docker", "create"]]
                self.assertEqual(len(creates), 6)
                for command in creates:
                    self.assertIn("127.0.0.1::8888", command)
                    self.assertIn("127.0.0.1::1080", command)
                    self.assertIn("--no-healthcheck", command)
                    self.assertIn("MALLOC_TRIM_THRESHOLD_=1048576", command)
                    self.assertIn("MALLOC_MMAP_THRESHOLD_=1048576", command)
                    self.assertTrue(command[-3].startswith("sha256:"))
                    mount = command[command.index("--mount") + 1]
                    self.assertIn("src=warp-zapret-test-", mount)
                    self.assertNotIn("warp_zapret1_state", mount)
                self.assertFalse(any(c[:2] == ["docker", "pull"] for c in commands))
                self.assertFalse(any("logs" in c or "compose" in c for c in commands))

    def test_selected_strategies_are_deduplicated(self):
        for runner in self.runners:
            with self.subTest(runner=runner):
                result, summary, _, _, remaining = self.run_runner(runner, strategies=["general", "general"])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual([row["strategy"] for row in summary], ["general"])
                self.assertEqual(remaining, {})

    def test_container_cannot_inherit_docker_proxy_settings(self):
        for runner in self.runners:
            with self.subTest(runner=runner):
                result, _, _, commands, _ = self.run_runner(runner, strategies=["general"])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                create = next(c for c in commands if c[:2] == ["docker", "create"])
                environment = [create[i + 1] for i, arg in enumerate(create) if arg == "--env"]
                for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "NO_PROXY"):
                    for key in (name, name.lower()):
                        self.assertIn(key + "=", environment)

    def test_successful_headers_with_failed_body_do_not_pass(self):
        for runner in self.runners:
            with self.subTest(runner=runner):
                result, summary, checks, _, remaining = self.run_runner(
                    runner, strategies=["general"], scenario="body-fail",
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(summary[0]["status"], "PARTIAL")
                self.assertEqual(summary[0]["passed"], "2")  # only WARP trace passed
                sites = [row for row in checks if row["target"] != "WARP"]
                self.assertTrue(all(row["status"] == "FAIL" and row["curl_exit"] == "18" for row in sites))
                self.assertEqual(remaining, {})

    def test_no_pass_does_not_name_a_winner(self):
        for runner in self.runners:
            with self.subTest(runner=runner):
                result, summary, checks, _, remaining = self.run_runner(
                    runner, strategies=["general"], scenario="no-pass",
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(summary[0]["status"], "PARTIAL")
                self.assertNotIn("Fully passing strategies:", result.stdout)
                self.assertEqual(sum(row["status"] == "SKIP" for row in checks), 2)
                self.assertEqual(remaining, {})

    def test_invalid_strategy_fails_before_creating_resources(self):
        for runner in self.runners:
            with self.subTest(runner=runner):
                result, summary, _, commands, remaining = self.run_runner(runner, strategies=["../bad"])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse(summary)
                self.assertFalse(any(c[1:3] == ["volume", "create"] for c in commands))
                self.assertEqual(remaining, {})

    def test_invalid_targets_do_not_contact_docker(self):
        for runner in self.runners:
            for targets in ("https://user:secret@example.com/\n", "http://example.com/\n", "# empty\n"):
                with self.subTest(runner=runner, targets=targets):
                    result, _, _, commands, _ = self.run_runner(runner, targets=targets)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertFalse(commands)

    def test_create_failure_never_removes_an_unowned_container(self):
        for runner in self.runners:
            with self.subTest(runner=runner):
                result, summary, _, commands, remaining = self.run_runner(
                    runner, strategies=["general"], scenario="create-fail",
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(summary[0]["status"], "STARTUP_FAIL")
                self.assertFalse(any(c[:2] == ["docker", "rm"] for c in commands))
                self.assertEqual(remaining, {})

    def test_cleanup_error_is_not_reported_as_success(self):
        for runner in self.runners:
            with self.subTest(runner=runner):
                result, _, _, _, remaining = self.run_runner(
                    runner, strategies=["general"], scenario="cleanup-fail",
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("volume", remaining)
                self.assertNotIn("active", remaining)

    def test_missing_local_image_does_not_pull_with_never(self):
        for runner in self.runners:
            with self.subTest(runner=runner):
                result, summary, _, commands, _ = self.run_runner(
                    runner, strategies=["general"], scenario="missing",
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse(summary)
                self.assertFalse(any(c[:2] == ["docker", "pull"] for c in commands))

    def test_pull_modes_and_repeated_rounds(self):
        for runner in self.runners:
            for pull, scenario in (("missing", "missing"), ("always", "")):
                with self.subTest(runner=runner, pull=pull):
                    result, summary, checks, commands, remaining = self.run_runner(
                        runner, strategies=["general"], pull=pull, scenario=scenario, rounds=2,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(summary[0]["passed"], "8")
                    self.assertEqual({row["round"] for row in checks}, {"1", "2"})
                    self.assertEqual(sum(c[:2] == ["docker", "pull"] for c in commands), 1)
                    self.assertNotIn("active", remaining)
                    self.assertNotIn("volume", remaining)

    def test_real_curl_ignores_ambient_no_proxy(self):
        curl = shutil.which("curl.exe" if os.name == "nt" else "curl")
        if not curl:
            self.skipTest("curl is unavailable")
        # A local fake CONNECT proxy proves requests don't bypass --proxy even
        # under NO_PROXY=*. No DNS lookup or external connection is needed.
        received = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(5)

            def serve():
                try:
                    connection, _ = listener.accept()
                    with connection:
                        received.append(connection.recv(4096))
                        connection.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass

            worker = threading.Thread(target=serve)
            worker.start()
            try:
                result = subprocess.run(
                    [curl, "--disable", "--silent", "--proxy",
                     f"http://127.0.0.1:{listener.getsockname()[1]}", "--noproxy", ",",
                     "--max-time", "3", "--proto", "=https", "https://example.invalid/"],
                    env=dict(os.environ, NO_PROXY="*", no_proxy="*"),
                    capture_output=True, timeout=6,
                )
                self.assertNotEqual(result.returncode, 0)
            finally:
                worker.join(timeout=6)
        self.assertTrue(received)
        self.assertTrue(received[0].startswith(b"CONNECT example.invalid:443 "))


if __name__ == "__main__":
    unittest.main()
