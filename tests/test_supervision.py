"""Offline watchdog failures and a real Docker restart; never register WARP."""

import json
import subprocess
import time
import unittest
import uuid

from test_general import IMAGE, ROOT, run_in_image


class SupervisionTests(unittest.TestCase):
    def test_runtime_matches_source_and_parses(self):
        self.assertEqual(run_in_image('cat /runtime.sh').decode(),
                         (ROOT / 'runtime.sh').read_text(encoding='utf-8'))
        run_in_image('bash -n /runtime.sh && bash -n /entrypoint.sh')

    def watchdog_case(self, outcomes, ready=True):
        setup = "\n".join(map(str, outcomes)) + "\n"
        return run_in_image(r'''
set -euo pipefail
source /runtime.sh
sleep() { :; }
kill() { echo "SIGNAL:$*"; }
cat > /tmp/outcomes <<'DATA'
''' + setup + r'''DATA
echo 0 > /tmp/count
cat > /healthcheck.sh <<'CHECK'
#!/usr/bin/bash
read -r count < /tmp/count
count=$((count + 1))
echo "$count" > /tmp/count
result=$(/bin/busybox sed -n "${count}p" /tmp/outcomes)
if [ "$result" = hang ]; then
  trap '' TERM
  while true; do sleep 1; done
fi
exit "${result:-1}"
CHECK
chmod +x /healthcheck.sh
''' + ('touch /tmp/ready\n' if ready else '') + r'''
watchdog 999 /tmp/ready 0 0
echo "CHECKS:$(cat /tmp/count)"
''').decode()

    def test_fifth_consecutive_failure_requests_restart(self):
        output = self.watchdog_case([1] * 5)
        self.assertIn('CHECKS:5', output)
        self.assertEqual(output.count('SIGNAL:-USR1 999'), 1)
        self.assertIn('(5/5)', output)

    def test_success_resets_failure_counter(self):
        output = self.watchdog_case([1] * 4 + [0] + [1] * 5)
        self.assertIn('CHECKS:10', output)
        self.assertEqual(output.count('(1/5)'), 2)
        self.assertEqual(output.count('SIGNAL:-USR1 999'), 1)

    def test_startup_deadline_does_not_wait_for_healthcheck(self):
        output = self.watchdog_case([], ready=False)
        self.assertIn('startup exceeded', output)
        self.assertIn('CHECKS:0', output)
        self.assertIn('SIGNAL:-USR1 999', output)

    def test_hung_healthcheck_is_bounded_and_counts_as_failure(self):
        began = time.monotonic()
        output = self.watchdog_case(['hang', 1, 1, 1, 1])
        self.assertIn('CHECKS:5', output)
        self.assertIn('SIGNAL:-USR1 999', output)
        self.assertLess(time.monotonic() - began, 18)

    def test_cli_timeout_kills_a_command_ignoring_term(self):
        began = time.monotonic()
        output = run_in_image(r'''
set -euo pipefail
source /runtime.sh
mkdir /tmp/mocks
cat > /tmp/mocks/warp-cli <<'MOCK'
#!/usr/bin/bash
trap '' TERM
while true; do sleep 1; done
MOCK
chmod +x /tmp/mocks/warp-cli
export PATH="/tmp/mocks:$PATH"
if warp_cli status; then exit 99; else echo TIMED_OUT; fi
''').decode()
        self.assertIn('TIMED_OUT', output)
        self.assertLess(time.monotonic() - began, 18)

    def test_log_filter_hides_sensitive_fields_and_keeps_diagnostics(self):
        output = run_in_image(r'''
set -euo pipefail
source /runtime.sh
printf '%s\n' 'Status update: Connected' 'registration=PRIVATE_MARKER' \
  'PrivateKey=PRIVATE_MARKER' 'TOKEN=PRIVATE_MARKER' \
  'account_id=PRIVATE_MARKER' 'License: PRIVATE_MARKER' \
  'connection lost; retrying' | filter_warp_log
''').decode()
        self.assertNotIn('PRIVATE_MARKER', output)
        self.assertIn('Connected', output)
        self.assertIn('retrying', output)

    def test_entrypoint_restarts_after_five_failures_and_stops_cleanly(self):
        # Replace executables only inside a disposable, network-isolated container.
        script = r'''
set -euo pipefail
mkdir -p /tmp/mocks
for cmd in dbus-daemon ss socat iptables warp-cli warp-svc; do
  cat > "/tmp/mocks/$cmd" <<'MOCK'
#!/usr/bin/bash
case "${0##*/}" in
  dbus-daemon) exit 0 ;;
  iptables) [[ "$*" != *' -D '* ]]; exit $? ;;
  ss) echo 'LISTEN :40000 ' ;;
  socat|warp-svc) exec sleep 3600 ;;
  warp-cli) echo 'Status update: Connected' ;;
esac
MOCK
done
cat > /usr/local/bin/nfqws <<'MOCK'
#!/usr/bin/bash
[[ "$*" == *--dry-run* ]] && exit 0
exec sleep 3600
MOCK
cat > /healthcheck.sh <<'MOCK'
#!/usr/bin/bash
[[ -f /tmp/recovered ]] && exit 0
count=0
[[ ! -f /tmp/check-count ]] || read -r count < /tmp/check-count
count=$((count + 1))
echo "$count" > /tmp/check-count
[[ "$count" != 5 ]] || touch /tmp/recovered
exit 1
MOCK
chmod +x /tmp/mocks/* /usr/local/bin/nfqws /healthcheck.sh
# Accelerate only the watchdog clock; exercise the actual entrypoint and cleanup.
/bin/busybox sed -i 's|watchdog "$$" /run/warp-zapret.ready &|watchdog "$$" /run/warp-zapret.ready 2 0.05 \&|' /entrypoint.sh
export PATH="/tmp/mocks:$PATH"
exec /entrypoint.sh
'''
        name = 'warp-supervision-test-' + uuid.uuid4().hex[:12]
        subprocess.run(['docker', 'run', '-d', '--name', name, '--init',
                        '--network', 'none', '--restart', 'on-failure:1',
                        '--no-healthcheck', '--entrypoint', '/usr/bin/bash',
                        IMAGE, '-c', script], capture_output=True, check=True, timeout=20)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                state = json.loads(subprocess.run(
                    ['docker', 'inspect', name], capture_output=True,
                    check=True, timeout=10).stdout)[0]
                if state['RestartCount'] == 1 and state['State']['Running']:
                    count = subprocess.run(['docker', 'exec', name, 'cat', '/tmp/check-count'],
                                           capture_output=True, timeout=10)
                    if count.stdout.strip() == b'5':
                        logs = subprocess.run(['docker', 'logs', name], capture_output=True,
                                              check=True, timeout=10).stdout.decode()
                        if logs.count('[+] ready') >= 2:
                            break
                time.sleep(0.2)
            else:
                self.fail('Container did not restart and recover after five failures')
            self.assertIn('[+] cleanup', logs)
            self.assertIn('five consecutive', logs)
            subprocess.run(['docker', 'stop', '--time', '5', name], capture_output=True,
                           check=True, timeout=15)
            state = json.loads(subprocess.run(['docker', 'inspect', name],
                               capture_output=True, check=True, timeout=10).stdout)[0]
            self.assertEqual(state['State']['ExitCode'], 0)
            self.assertEqual(state['RestartCount'], 1)
        finally:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True,
                           check=True, timeout=20)


if __name__ == '__main__':
    unittest.main()
