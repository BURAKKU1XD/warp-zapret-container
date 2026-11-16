"""Exercise the actual control entrypoint offline, without WARP registration."""

import unittest

from test_general import run_in_image


class StrategyControlTests(unittest.TestCase):
    def test_control_does_not_start_nfqws_or_install_queue_rules(self):
        output = run_in_image(r'''
set -euo pipefail
mkdir /tmp/control-mocks
for command in dbus-daemon iptables ss warp-cli warp-svc socat; do
  cat > "/tmp/control-mocks/$command" <<'MOCK'
#!/usr/bin/bash
case "${0##*/}" in
  dbus-daemon) exit 0 ;;
  iptables) touch /tmp/queue-touched; exit 1 ;;
  ss) echo 'LISTEN :40000 ' ;;
  warp-cli) echo 'Status update: Connected' ;;
  warp-svc|socat) exec sleep 30 ;;
esac
MOCK
done
cat > /usr/local/bin/nfqws <<'MOCK'
#!/usr/bin/bash
[[ "$*" == *--dry-run* ]] && exit 0
touch /tmp/nfqws-started
exit 99
MOCK
chmod +x /tmp/control-mocks/* /usr/local/bin/nfqws
export PATH="/tmp/control-mocks:$PATH"
/entrypoint.sh --without-zapret >/tmp/control-output 2>&1 &
child=$!
for i in $(seq 1 50); do
  [[ ! -f /run/warp-zapret.ready ]] || break
  sleep 0.1
done
test -f /run/warp-zapret.ready
test ! -f /run/warp-zapret.nfqws.pid
test ! -f /tmp/nfqws-started
kill -TERM "$child"
wait "$child"
test ! -f /tmp/queue-touched
echo CONTROL_VERIFIED
''').decode()
        self.assertIn('CONTROL_VERIFIED', output)

    def test_diagnostics_sum_queue_counters_without_exposing_trace(self):
        output = run_in_image(r'''
set -euo pipefail
source /runtime.sh
printf '%s\n' "$$" > /run/warp-zapret.nfqws.pid
iptables() {
  printf '%s\n' 'pkts bytes target prot opt in out source destination' \
    '3 100 NFQUEUE tcp -- * * 0.0.0.0/0 0.0.0.0/0' \
    '4 100 NFQUEUE tcp -- * * 0.0.0.0/0 0.0.0.0/0' \
    '9 100 NFQUEUE udp -- * * 0.0.0.0/0 0.0.0.0/0'
}
strategy_diagnostics
iptables() { return 1; }
if strategy_diagnostics; then exit 99; fi
''').decode()
        self.assertEqual(output, '1\t7\t9\tUNKNOWN\n')
