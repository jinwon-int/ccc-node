#!/usr/bin/env bash
# termux-dns-health-watch — detect "Android system DNS broken while raw DNS
# works" on a Termux node and alert the owner once (ccc-node#2937).
#
# Incident 2026-11-28 (recorded in the Family Wiki operating log): after a ~10-minute
# network blip the Tailscale Android app kept its tunnel up but its in-VPN
# resolver (100.100.100.100) stopped answering. Android routes every app's
# getaddrinfo through the VPN DNS, so mail, mbsync and gh all failed with
# "[Errno 7] No address associated with hostname" for ~5 hours while ping and
# raw UDP DNS to a public resolver worked. Reconnecting Wi-Fi did not help;
# toggling the Tailscale connection off/on fixed it at once.
#
# States (one per run):
#   ok             system resolver answers
#   sysdns-broken  system resolver fails, raw query to a public resolver works
#                  -> alert after CCC_DNS_WATCH_THRESHOLD consecutive runs
#   offline        both fail -> logged only (no network, nothing to fix)
# Alerts: one push-spool record when sysdns-broken persists, one on recovery.
# Detect + alert only: this script never reconnects the VPN (owner decision,
# 2026-11-28; a reconnect drops tunnels/ssh and needs its own approval).
#
# Exit codes: 0 ok, 1 sysdns-broken, 3 offline, 2 usage.
set -uo pipefail

CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
SPOOL="${CCC_DNS_WATCH_SPOOL:-${CCC_PUSH_SPOOL:-$STATE_DIR/telegram-spool}}"
STATE_FILE="${CCC_DNS_WATCH_STATE:-$STATE_DIR/dns-health-watch.json}"
LOG="${CCC_DNS_WATCH_LOG:-$STATE_DIR/dns-health-watch.log}"
THRESHOLD="${CCC_DNS_WATCH_THRESHOLD:-2}"
HOSTS="${CCC_DNS_WATCH_HOSTS:-github.com api.anthropic.com}"
PUBLIC_DNS="${CCC_DNS_WATCH_PUBLIC_DNS:-8.8.8.8 1.1.1.1}"
VPN_DNS="${CCC_DNS_WATCH_VPN_DNS:-100.100.100.100}"
NODE="${CCC_NODE:-$(hostname 2>/dev/null || echo node)}"
PYTHON="${CCC_DNS_WATCH_PYTHON:-python3}"

case "${1:-}" in
  -h|--help)
    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
  "") ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac
case "$THRESHOLD" in ''|*[!0-9]*|0) echo "CCC_DNS_WATCH_THRESHOLD must be a positive integer" >&2; exit 2 ;; esac

mkdir -p "$(dirname "$STATE_FILE")" "$(dirname "$LOG")" 2>/dev/null || true

# -I: never import modules from the cwd or PYTHON* env.
exec "$PYTHON" -I - "$STATE_FILE" "$LOG" "$SPOOL" "$THRESHOLD" "$HOSTS" "$PUBLIC_DNS" "$VPN_DNS" "$NODE" <<'PY'
import json, os, random, socket, struct, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

state_path, log_path, spool = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
threshold = int(sys.argv[4])
hosts, public_dns, vpn_dns = sys.argv[5].split(), sys.argv[6].split(), sys.argv[7].strip()
node = sys.argv[8]
# Test seams (hermetic suites have no network): force a probe result.
SYS_OVERRIDE = os.environ.get("CCC_DNS_WATCH_TEST_SYS")   # ok|fail
RAW_OVERRIDE = os.environ.get("CCC_DNS_WATCH_TEST_RAW")   # ok|fail
VPN_OVERRIDE = os.environ.get("CCC_DNS_WATCH_TEST_VPN")   # ok|fail
TS_OVERRIDE = os.environ.get("CCC_DNS_WATCH_TEST_NOW")    # fixed timestamp text

KST = timezone(timedelta(hours=9))
ts = TS_OVERRIDE or datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S%z")


def sys_dns(host):
    """getaddrinfo through the Android resolver, hard-capped at 10 s.

    getaddrinfo cannot be interrupted portably, so it runs in a child.
    """
    pid = os.fork()
    if pid == 0:
        try:
            socket.getaddrinfo(host, 443)
            os._exit(0)
        except Exception:
            os._exit(1)
    deadline = time.time() + 10
    while time.time() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        time.sleep(0.2)
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    return False


def raw_dns(server, host):
    """One A query over UDP straight to *server*, bypassing the system resolver."""
    q = struct.pack(">HHHHHH", random.randint(0, 65535), 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in host.split(".")) + b"\0"
    q += struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(4)
    try:
        s.sendto(q, (server, 53))
        r = s.recv(512)
        return len(r) >= 12 and (r[3] & 15) == 0 and struct.unpack(">H", r[6:8])[0] > 0
    except Exception:
        return False
    finally:
        s.close()


def forced(value, probe):
    if value in ("ok", "fail"):
        return value == "ok"
    return probe()


probe_host = hosts[0] if hosts else "github.com"
sys_ok = forced(SYS_OVERRIDE, lambda: any(sys_dns(h) for h in hosts))
raw_ok = sys_ok or forced(RAW_OVERRIDE, lambda: any(raw_dns(s, probe_host) for s in public_dns))
vpn_ok = None
if not sys_ok and vpn_dns:
    vpn_ok = forced(VPN_OVERRIDE, lambda: raw_dns(vpn_dns, probe_host))
status = "ok" if sys_ok else ("sysdns-broken" if raw_ok else "offline")

try:
    prev = json.loads(state_path.read_text(encoding="utf-8"))
except Exception:
    prev = {}
streak = int(prev.get("streak", 0)) + 1 if status == "sysdns-broken" else 0
alerted = bool(prev.get("alerted"))
since = prev.get("since") if prev.get("status") == status and prev.get("since") else ts


def spool_send(text):
    if not spool.is_dir():
        return "no-spool"
    name = f"dns-health-{time.time_ns()}"
    record = {"event": "dns-health", "node": node, "ts": ts, "text": text,
              "dedup": f"dns-health:{node}:{name}", "tier": "notice"}
    tmp = spool / f".{name}.tmp"  # not *.json: the drain must never see a partial file
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, spool / f"{name}.json")
    return "sent"


action = "-"
if status == "sysdns-broken" and streak >= threshold and not alerted:
    vpn_txt = {True: "응답", False: "무응답", None: "미확인"}[vpn_ok]
    action = spool_send(
        f"[{ts}] ⚠️ **{node} 시스템 DNS 고장** ({since}부터)\n"
        f"시스템 DNS 실패 · 공용 DNS 직접 질의 정상 · VPN DNS({vpn_dns or '-'}) {vpn_txt}\n"
        "영향: 메일·mbsync·gh 등 이름 조회 전부 실패\n"
        "조치: 폰의 VPN(Tailscale) 앱 연결을 껐다 켜기 — Wi-Fi 재연결만으로는 안 풀림 "
        "(2026-11-28 사례, ccc-node#2937)")
    if action == "sent":
        alerted = True
elif status == "ok" and alerted:
    action = spool_send(f"[{ts}] ✅ **{node} 시스템 DNS 복구** — 이름 조회 정상")
    if action == "sent":
        alerted = False

state = {"schema": "ccc.dns-health-watch.v1", "status": status, "since": since,
         "streak": streak, "alerted": alerted, "last": ts}
tmp = state_path.with_name(state_path.name + ".tmp")
tmp.write_text(json.dumps(state), encoding="utf-8")
os.replace(tmp, state_path)
with log_path.open("a", encoding="utf-8") as f:
    f.write(f"[{ts}] status={status} sys={int(sys_ok)} raw={int(raw_ok)} "
            f"vpndns={'-' if vpn_ok is None else int(vpn_ok)} streak={streak} alert={action}\n")
sys.exit({"ok": 0, "sysdns-broken": 1, "offline": 3}[status])
PY
