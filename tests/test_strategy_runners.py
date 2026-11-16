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
import json, os, sys, hashlib
from pathlib import Path
sys.stdout.reconfigure(newline="\n")
root = Path(os.environ["RUNNER_MOCK"])
tool, *args = sys.argv[1:]
with (root / "commands.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps([tool, *args]) + "\n")
state_file = root / "state.json"
state = json.loads(state_file.read_text()) if state_file.exists() else {}
scenario = os.environ.get("RUNNER_SCENARIO", "")
def save(): state_file.write_text(json.dumps(state))
def result(code='200', size=0): print(f'\n{code}\t0.010\t{size}\t0.005')
if tool == 'docker':
    if args[0] == 'info': print('linux')
    elif args[:2] == ['image', 'inspect']:
        if '--format' not in args:
            if scenario == 'missing' and not state.get('pulled'): sys.exit(1)
            print('[]')
        elif '{{.Id}}' in args: print('sha256:'+'a'*64)
        else: print('linux/amd64')
    elif args[0] == 'pull': state['pulled']=True; save()
    elif args[0] == 'run':
        if '--help' in args:
            print('old image' if scenario == 'old-image' else '--without-zapret --test-diagnostics')
        else: print('general\nalt\nalt2\nalt3\nalt4\nalt5')
    elif args[:2] == ['volume','create']: state['volume']=args[-1]; save(); print(args[-1])
    elif args[:2] == ['volume','rm']:
        assert args[-1] == state['volume']
        if scenario == 'cleanup-fail': sys.exit(1)
        state.pop('volume'); save()
    elif args[0] == 'create':
        if scenario == 'create-fail': sys.exit(125)
        assert 'active' not in state
        stage = 'warp-control' if '--without-zapret' in args else args[-1]
        state.update(active='cid-'+stage, strategy=stage, stopped=False)
        save(); print(state['active'])
    elif args[0] in ('start','stop'):
        assert args[-1] == state['active']
        state['stopped'] = args[0] == 'stop'; save()
    elif args[0] == 'inspect':
        print('false' if state['strategy']=='alt' or (scenario=='control-startup-fail' and state['strategy']=='warp-control') else 'true')
    elif args[0] == 'exec':
        if '--test-diagnostics' in args:
            if scenario == 'diagnostic-fail': sys.exit(1)
            alive = int(state['strategy'] != 'warp-control')
            packets = 0 if scenario=='no-queue' or not alive else 24
            print(f'{alive}\t{packets}\t0\tUNKNOWN')
        else: sys.exit(int(state['strategy']=='alt5'))
    elif args[0] == 'port': print('127.0.0.1:'+('18888' if args[-1]=='8888/tcp' else '11080'))
    elif args[0] == 'rm':
        assert args[-1] == state['active']
        for key in ('active','strategy','stopped'): state.pop(key)
        save()
    else: raise AssertionError(args)
elif tool == 'curl':
    assert args[0]=='--disable'
    assert '--insecure' not in args and '--head' not in args
    assert args[args.index('--proto')+1]=='=https'
    assert '--proto-redir' in args and '--max-redirs' in args
    direct='--config' in args
    proxy=args[args.index('--proxy')+1]
    if direct:
        assert args[args.index('--config')+1]=='-'
        assert sys.stdin.read().strip()=='noproxy = "*"'
    else:
        assert args[args.index('--noproxy')+1]==','
    if not direct and (proxy.endswith(':1') or state.get('stopped')):
        if scenario=='guard-bypass': result(); sys.exit(0)
        result('000'); sys.exit(7)
    assert direct or proxy in ('http://127.0.0.1:18888','socks5h://127.0.0.1:11080')
    stage='direct' if direct else state['strategy']
    if scenario=='tls-handshake': result('000'); sys.exit(35)
    if scenario=='no-pass' and not direct or stage=='alt3': result('000'); sys.exit(60)
    if stage=='warp-control' and scenario in ('gain','no-queue','diagnostic-fail','direct-warp'):
        result('000'); sys.exit(28)
    if '--tlsv1.3' in args and scenario=='unsupported': result('000'); sys.exit(4)
    if args[-1].endswith('/cdn-cgi/trace'):
        warp='off' if direct or stage=='alt2' else 'on'
        if direct and scenario=='direct-warp': warp='on'
        print('ip=192.0.2.123\nregistration=DO_NOT_SAVE\nwarp='+warp)
        result()
    else:
        if scenario=='body-fail': result(size=20); sys.exit(18)
        sink=args[args.index('--output')+1]
        size=0
        if sink not in ('NUL','/dev/null'):
            data=bytes(65536) if '--data-binary' in args else b'fixture'
            if scenario=='corrupt-fixture': data=b'corrupt'
            Path(sink).write_bytes(data); size=len(data)
        result('503' if stage=='alt4' else '204',size)
else: raise AssertionError(tool)
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

    def run_runner(self, runner, *, strategies=None, scenario="", targets=None, pull="never", rounds=1, connections=1, extra=()):
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
                "--connections", str(connections), "--rounds", str(rounds), "--startup-timeout", "3", "--request-timeout", "1",
                "--targets-file", str(target_file), "--output", str(output),
            ]
            arguments += list(extra)
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
                "function global:curl.exe { if ($args -contains '--config') { $input | " +
                prefix + " curl @args } else { " + prefix + " curl @args } }\n"
                "& " + quote(ROOT / "test-strategies.ps1") +
                f" -Image mock:test -Pull {pull} -Rounds {rounds} -Connections {connections} -StartupTimeout 1 -RequestTimeout 1" +
                " -TargetsFile " + quote(target_file) + " -OutputDirectory " + quote(output)
            )
            if strategies:
                script += " -Strategies @(" + ",".join(map(quote, strategies)) + ")"
            option_names = {'--download-url': '-DownloadUrl', '--download-bytes': '-DownloadBytes',
                            '--download-sha256': '-DownloadSha256', '--upload-url': '-UploadUrl'}
            for i in range(0, len(extra), 2):
                script += ' ' + option_names[extra[i]] + ' ' + quote(extra[i+1])
            wrapper.write_text(script + "\nexit $LASTEXITCODE\n", encoding="utf-8")
            arguments = [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(wrapper)]
        result = subprocess.run(arguments, env=env, capture_output=True, text=True, timeout=180)
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

    def test_references_precede_strategies_and_do_not_claim_effect(self):
        for runner in self.runners:
            with self.subTest(runner=runner):
                result, summary, checks, commands, remaining = self.run_runner(runner)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual([r['strategy'] for r in summary], ['direct', 'warp-control', *CATALOG])
                self.assertEqual([r['status'] for r in summary],
                                 ['REFERENCE', 'AVAILABLE', 'AVAILABLE', 'STARTUP_FAIL', 'PARTIAL', 'PARTIAL', 'PARTIAL', 'STARTUP_FAIL'])
                self.assertEqual(summary[2]['passed'], '8')
                self.assertEqual(summary[0]['passed'], '4')
                self.assertEqual(summary[2]['effect'], 'WARP_WORKS_WITHOUT_ZAPRET')
                self.assertEqual({r['protocol'] for r in checks}, {'DEFAULT','HTTP1.1','TLS1.2','TLS1.3'})
                self.assertEqual(remaining, {})
                curls = [c for c in commands if c[0]=='curl']
                direct = [i for i,c in enumerate(commands) if c[0]=='curl' and '--config' in c]
                creates = [i for i,c in enumerate(commands) if c[:2]==['docker','create']]
                self.assertLess(max(direct),min(creates))
                self.assertIn('--without-zapret',commands[creates[0]])
                self.assertFalse(any('logs' in c or 'compose' in c for c in commands))
                self.assertTrue(all('--head' not in c for c in curls))

    def test_observed_gain_requires_control_failures_and_queue_evidence(self):
        for runner in self.runners:
            for scenario, effect in [('gain','OBSERVED_GAIN'),('no-queue','NOT_ESTABLISHED'),
                                     ('diagnostic-fail','NOT_ESTABLISHED'),('control-startup-fail','NOT_ESTABLISHED'),('direct-warp','NOT_ESTABLISHED')]:
                with self.subTest(runner=runner,scenario=scenario):
                    result, summary, _, _, remaining = self.run_runner(runner,strategies=['general'],scenario=scenario)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    self.assertEqual(summary[-1]['effect'],effect)
                    self.assertEqual(remaining,{})

    def test_body_failures_do_not_pass_and_sites_are_not_skipped(self):
        for runner in self.runners:
            with self.subTest(runner=runner):
                result, summary, checks, _, remaining = self.run_runner(runner,strategies=['general'],scenario='body-fail')
                self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                self.assertEqual(summary[-1]['passed'],'2')
                self.assertEqual(summary[-1]['status'],'PARTIAL')
                sites=[r for r in checks if r['strategy']=='general' and r['target'].startswith('https:')]
                self.assertEqual(len(sites),6)
                self.assertTrue(all(r['status']=='FAIL' and r['curl_exit']=='18' for r in sites))
                self.assertEqual(remaining,{})

    def test_cold_connections_stability_dedup_and_proxy_isolation(self):
        for runner in self.runners:
            with self.subTest(runner=runner):
                result, summary, checks, commands, remaining = self.run_runner(runner,strategies=['general','general'],connections=2,rounds=2)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual(len(summary),3)
                self.assertEqual(summary[-1]['passed'],'32')
                self.assertEqual(summary[-1]['stability'],'PASS')
                creates=[c for c in commands if c[:2]==['docker','create']]
                self.assertEqual(len(creates),4)
                for create in creates:
                    env=[create[i+1] for i,arg in enumerate(create) if arg=='--env']
                    for name in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','FTP_PROXY','NO_PROXY'):
                        for key in (name,name.lower()): self.assertIn(key+'=',env)
                    self.assertIn('127.0.0.1::8888',create)
                    self.assertIn('127.0.0.1::1080',create)
                    self.assertIn('--no-healthcheck',create)
                self.assertEqual(remaining,{})

    def test_guard_failure_aborts_before_resources(self):
        for runner in self.runners:
            result, _, _, commands, remaining = self.run_runner(runner,scenario='guard-bypass',strategies=['general'])
            self.assertEqual(result.returncode,2,result.stdout+result.stderr)
            self.assertFalse(any(c[:2]==['docker','create'] for c in commands))
            self.assertEqual(remaining,{})

    def test_unsupported_tls_is_not_a_strategy_success(self):
        for runner in self.runners:
            for scenario, status in [('unsupported', 'UNSUPPORTED'), ('tls-handshake', 'TLS_ERROR')]:
                with self.subTest(runner=runner, scenario=scenario):
                    result, summary, checks, _, _ = self.run_runner(runner,scenario=scenario,strategies=['general'])
                    self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                    self.assertEqual(summary[-1]['status'],'PARTIAL')
                    self.assertTrue(any(r['status']==status for r in checks))

    def test_invalid_inputs_and_old_image_fail_before_creating_resources(self):
        for runner in self.runners:
            for options in ({'strategies':['../bad']}, {'targets':'https://user:secret@example.com/'},
                            {'targets':'http://example.com/'}, {'targets':'# empty'}, {'scenario':'old-image'}):
                with self.subTest(runner=runner,options=options):
                    result, _, _, commands, remaining = self.run_runner(runner,**options)
                    self.assertEqual(result.returncode,2,result.stdout+result.stderr)
                    self.assertFalse(any(c[:3]==['docker','volume','create'] for c in commands))
                    self.assertEqual(remaining,{})

    def test_create_and_cleanup_failures_are_reported(self):
        for runner in self.runners:
            for scenario,code in [('create-fail',1),('cleanup-fail',2)]:
                result, summary, _, commands, remaining = self.run_runner(runner,strategies=['general'],scenario=scenario)
                self.assertEqual(result.returncode,code,result.stdout+result.stderr)
                if scenario=='create-fail':
                    self.assertFalse(any(c[:2]==['docker','rm'] for c in commands))
                    self.assertEqual(summary[-1]['status'],'STARTUP_FAIL')
                    self.assertEqual(remaining,{})
                else: self.assertIn('volume',remaining)

    def test_pull_modes(self):
        for runner in self.runners:
            for pull,scenario,code in [('never','missing',2),('missing','missing',0),('always','',0)]:
                result, _, _, commands, _ = self.run_runner(runner,strategies=['general'],pull=pull,scenario=scenario)
                self.assertEqual(result.returncode,code,result.stdout+result.stderr)
                self.assertEqual(sum(c[:2]==['docker','pull'] for c in commands),int(pull!='never'))

    def test_exact_download_and_upload_integrity(self):
        import hashlib
        extra=['--download-url','https://example.com/fixture','--download-bytes','7',
               '--download-sha256',hashlib.sha256(b'fixture').hexdigest(),'--upload-url','https://example.com/echo']
        for runner in self.runners:
            for scenario,code in [('',0),('corrupt-fixture',1)]:
                result,summary,checks,_,remaining=self.run_runner(runner,strategies=['general'],extra=extra,scenario=scenario)
                self.assertEqual(result.returncode,code,result.stdout+result.stderr)
                self.assertEqual(summary[-1]['status'],'AVAILABLE' if code==0 else 'PARTIAL')
                self.assertEqual(remaining,{})

    def test_real_direct_request_ignores_proxy_environment(self):
        curl = shutil.which("curl.exe" if os.name == "nt" else "curl")
        if not curl:
            self.skipTest("curl is unavailable")
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
                except OSError:
                    pass

            worker = threading.Thread(target=serve)
            worker.start()
            try:
                result = subprocess.run(
                    [curl, '--disable', '--silent', '--show-error', '--proxy', 'http://127.0.0.1:1',
                     '--config', '-', '--max-time', '3', '--proto', '=https',
                     f'https://127.0.0.1:{listener.getsockname()[1]}/'],
                    env=dict(os.environ, HTTPS_PROXY='http://invalid.invalid:9',
                             ALL_PROXY='socks5h://invalid.invalid:9'),
                    input=b'noproxy = "*"\n', capture_output=True, timeout=6,
                )
                self.assertNotEqual(result.returncode, 0)  # local peer has no TLS certificate
            finally:
                worker.join(timeout=6)
        # Accepting the TCP connection proves the direct path even when the OS
        # TLS backend cannot acquire credentials and sends no ClientHello.
        self.assertTrue(received, 'The direct reference did not reach the local listener')
        self.assertFalse(received[0].startswith(b'CONNECT '), 'Unexpected HTTP proxy request')

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
