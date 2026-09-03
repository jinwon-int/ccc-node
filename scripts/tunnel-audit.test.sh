#!/usr/bin/env bash
# Hermetic tests for tunnel-audit.sh: stub systemd dir, crontab, ss and
# tailscale so the suite is platform-independent and touches nothing real.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AUDIT="$ROOT/scripts/tunnel-audit.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# --- fixtures ---------------------------------------------------------------
SYSD="$TMP/systemd"; mkdir -p "$SYSD" "$TMP/cron.d" "$TMP/bin"
cat > "$SYSD/cloudflared.service" <<'EOF'
[Unit]
Description=cloudflared public tunnel
[Service]
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate run --token-file /etc/cloudflared/token
EOF
cat > "$SYSD/gwakga-broker-public-tunnel.service" <<'EOF'
[Unit]
Description=reverse tunnel to seoseo
[Service]
ExecStart=/usr/bin/ssh -N -T \
  -o ExitOnForwardFailure=yes \
  -R 127.0.0.1:8799:127.0.0.1:8787 seoseo
EOF
cat > "$SYSD/seoseo-broker-tunnel.service" <<'EOF'
[Unit]
Description=private forward
[Service]
ExecStart=/usr/bin/ssh -N -L 18787:127.0.0.1:8787 seoseo
EOF
cat > "$SYSD/ngrok-web.service" <<'EOF'
[Unit]
Description=ngrok
[Service]
ExecStart=/usr/local/bin/ngrok http 8080 --authtoken SECRETVALUE123
EOF
cat > "$SYSD/ccc-telegram-bridge.service" <<'EOF'
[Unit]
Description=bridge (not a tunnel)
[Service]
ExecStart=/opt/ccc-node/bridge/venv/bin/python -m telegram_bot --path /root
EOF
: > "$SYSD/soonwook-webhook-tunnel.service.removed-20260626T075347Z"
printf '*/5 * * * * pgrep -f searxng-tunnel.sh || ~/bin/searxng-tunnel.sh --token abc123\n0 4 * * * echo unrelated\n' > "$TMP/crontab.txt"
printf '# comment tunnel\n17 * * * * root /usr/bin/autossh -M 0 -N -L 9000:127.0.0.1:9000 hub\n' > "$TMP/cron.d/hub"

write_exec_stub "$TMP/bin/crontab" <<EOF
[ "\${1:-}" = "-l" ] && cat "$TMP/crontab.txt"
EOF
write_exec_stub "$TMP/bin/ss" <<EOF
cat <<'SS'
LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))
LISTEN 0 128 127.0.0.1:8787 0.0.0.0:* users:(("node",pid=2,fd=4))
LISTEN 0 128 100.127.171.124:8000 0.0.0.0:* users:(("python3",pid=3,fd=5))
LISTEN 0 128 [::]:443 [::]:* users:(("caddy",pid=4,fd=6))
LISTEN 0 128 [fd7a:115c:a1e0::2f35:ab7d]:38602 [::]:* users:(("tailscaled",pid=5,fd=7))
SS
EOF
write_exec_stub "$TMP/bin/systemctl" <<EOF
case "\$2" in cloudflared.service) echo active ;; ngrok-web.service) echo failed ;; *) echo inactive ;; esac
EOF
# ufw fixture: active, default-deny, mixed v4/v6/interface/deny rules. The stub
# cats the fixture so sub-cases can rewrite it and re-run.
UFW_FIX="$TMP/ufw-verbose.txt"
cat > "$UFW_FIX" <<'UFWEOF'
Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere
22/tcp (v6)                ALLOW IN    Anywhere (v6)
Anywhere on tailscale0     ALLOW IN    Anywhere
203.0.113.5                DENY IN     Anywhere
UFWEOF
write_exec_stub "$TMP/bin/ufw" <<UFWEOF
[ "\$1" = "status" ] && cat "$UFW_FIX"
UFWEOF
write_exec_stub "$TMP/bin/tailscale" <<EOF
case "\$1" in
  serve) printf 'https://node.tailnet.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8888\n' ;;
  funnel) printf 'https://node.tailnet.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8888\n' ;;
esac
EOF
export PATH="$TMP/bin:$PATH"
export CCC_TUNNEL_AUDIT_SYSTEMD_DIR="$SYSD" CCC_TUNNEL_AUDIT_CROND_DIR="$TMP/cron.d"
export CCC_TUNNEL_AUDIT_CRONTAB_CMD="$TMP/bin/crontab" CCC_TUNNEL_AUDIT_SS_CMD="$TMP/bin/ss" CCC_TUNNEL_AUDIT_TAILSCALE_CMD="$TMP/bin/tailscale" CCC_TUNNEL_AUDIT_UFW_CMD="$TMP/bin/ufw"
export CCC_NODE=test-node

out="$(bash "$AUDIT" 2>"$TMP/err")"; rc=$?
ok "json mode exits 0" '[ "$rc" = 0 ]'
ok "json parses with schema" 'jq -e ".schema == \"ccc.tunnel-audit.v1\" and .node == \"test-node\"" <<<"$out" >/dev/null'
ok "four tunnel-class units found, bridge excluded" '[ "$(jq ".units | length" <<<"$out")" = 4 ] && ! jq -e ".units[] | select(.unit == \"ccc-telegram-bridge.service\")" <<<"$out" >/dev/null'
ok "cloudflared classified" 'jq -e ".units[] | select(.unit == \"cloudflared.service\") | .kind == \"cloudflared\" and .active == \"active\"" <<<"$out" >/dev/null'
ok "ssh -R classified as reverse, -L as forward" 'jq -e "[.units[] | select(.kind == \"ssh-reverse\")] | length == 1" <<<"$out" >/dev/null && jq -e "[.units[] | select(.kind == \"ssh-forward\")] | length == 1" <<<"$out" >/dev/null'
ok "ngrok classified as public tunnel" 'jq -e ".units[] | select(.unit == \"ngrok-web.service\") | .kind == \"public-tunnel\" and .active == \"failed\"" <<<"$out" >/dev/null'
ok "token argument masked in exec shape" '! grep -q "SECRETVALUE123" <<<"$out" && grep -q "authtoken <masked>" <<<"$out"'
ok "token-file path masked too" 'jq -r ".units[] | select(.kind == \"cloudflared\") | .exec_start" <<<"$out" | grep -q -- "--token-file <masked>"'
ok "removed-unit residue listed" 'jq -e "(.residue | length) == 1 and (.residue[0].file | test(\"removed-20260626\"))" <<<"$out" >/dev/null'
ok "crontab tunnel line found, unrelated line skipped, cron token masked" '[ "$(jq "[.cron[] | select(.source == \"crontab\")] | length" <<<"$out")" = 1 ] && ! grep -q "abc123" <<<"$out"'
ok "cron.d autossh -L line found, comment skipped" 'jq -e ".cron[] | select(.source == \"cron.d/hub\") | .line | test(\"autossh\")" <<<"$out" >/dev/null && [ "$(jq ".cron | length" <<<"$out")" = 2 ]'
ok "loopback listener excluded; public/tailnet (v4 + fd7a ULA v6) classified" '[ "$(jq ".listeners | length" <<<"$out")" = 4 ] && jq -e "[.listeners[] | select(.bind == \"public\")] | length == 2" <<<"$out" >/dev/null && jq -e "[.listeners[] | select(.bind == \"tailnet\")] | length == 2" <<<"$out" >/dev/null'
ok "listener process names captured" 'jq -e ".listeners[] | select(.local == \"[::]:443\") | .process == \"caddy\"" <<<"$out" >/dev/null'
ok "tailscale serve configured; funnel status echoing a tailnet-only serve is NOT funnel" 'jq -e ".tailscale.serve.configured == true and .tailscale.funnel.configured == false" <<<"$out" >/dev/null'
ok "ufw active + default-deny parsed, rules normalized in order" 'jq -e ".firewall.ufw.status == \"active\" and .firewall.ufw.default_incoming == \"deny\" and (.firewall.ufw.rules | length) == 4 and .firewall.ufw.rules[0] == \"22/tcp ALLOW IN Anywhere\" and .firewall.ufw.rules[2] == \"Anywhere on tailscale0 ALLOW IN Anywhere\"" <<<"$out" >/dev/null'
ok "rules sha256 covers the ordered normalized ruleset" '[ "$(jq -j ".firewall.ufw.rules | join(\"\n\")" <<<"$out" | sha256sum | cut -c1-64)" = "$(jq -r .firewall.ufw.rules_sha256 <<<"$out")" ]'
ok "firewall_default_deny true under active+deny" 'jq -e ".exposure.firewall_default_deny == true" <<<"$out" >/dev/null'
ok "multi-line ExecStart (backslash continuation) is joined and classified as reverse" 'jq -e ".units[] | select(.unit == \"gwakga-broker-public-tunnel.service\") | .kind == \"ssh-reverse\" and (.exec_start | test(\"-R 127.0.0.1:8799\"))" <<<"$out" >/dev/null'
# Real funnel: an entry tagged "(Funnel on)" flips the flag.
write_exec_stub "$TMP/bin/tailscale" <<EOF
case "\$1" in
  serve) printf 'https://node.tailnet.ts.net (tailnet only)\n' ;;
  funnel) printf 'https://node.tailnet.ts.net (Funnel on)\n|-- / proxy http://127.0.0.1:8888\n' ;;
esac
EOF
fun_out="$(bash "$AUDIT" 2>/dev/null)"
ok "an entry tagged (Funnel on) is reported as funnel" 'jq -e ".tailscale.funnel.configured == true and .exposure.funnel_configured == true" <<<"$fun_out" >/dev/null'
write_exec_stub "$TMP/bin/tailscale" <<EOF
case "\$1" in
  serve) printf 'https://node.tailnet.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8888\n' ;;
  funnel) printf 'https://node.tailnet.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8888\n' ;;
esac
EOF
out="$(bash "$AUDIT" 2>/dev/null)"
ok "exposure summary counts" 'jq -e ".exposure == {cloudflared_units:1, reverse_ssh_units:1, public_tunnel_units:1, public_listeners:2, funnel_configured:false, residue_files:1, firewall_default_deny:true}" <<<"$out" >/dev/null'
ok "stderr is empty" '[ ! -s "$TMP/err" ]'

md="$(bash "$AUDIT" --markdown 2>/dev/null)"; rc=$?
ok "markdown mode exits 0" '[ "$rc" = 0 ]'
ok "markdown headline carries counts" 'grep -q "cloudflared units: 1 · reverse ssh: 1 · public tunnel units: 1 · public listeners: 2 · funnel: no · residue: 1" <<<"$md"'
ok "markdown carries the ufw line" 'grep -q -- "- ufw: active (default-in deny, 4 rules)" <<<"$md"'
ok "markdown lists units and residue" 'grep -q "gwakga-broker-public-tunnel.service" <<<"$md" && grep -q "removed-20260626" <<<"$md"'
ok "markdown never leaks the token" '! grep -q "SECRETVALUE123" <<<"$md"'

# Missing tools (Termux-like) are facts, not failures.
out="$(CCC_TUNNEL_AUDIT_SS_CMD=/nonexistent/ss CCC_TUNNEL_AUDIT_TAILSCALE_CMD=/nonexistent/tailscale CCC_TUNNEL_AUDIT_UFW_CMD=/nonexistent/ufw CCC_TUNNEL_AUDIT_SYSTEMD_DIR="$TMP/no-such-dir" bash "$AUDIT" 2>/dev/null)"; rc=$?
ok "missing ss/tailscale/systemd dir still exit 0" '[ "$rc" = 0 ]'
ok "missing tools reported in tools block" 'jq -e ".tools.units == \"missing-dir\" and .tools.ss_status == \"missing\" and .tailscale.serve.status == \"missing\"" <<<"$out" >/dev/null'
ok "missing ufw is a fact, not a failure" 'jq -e ".firewall.ufw.status == \"missing\" and .exposure.firewall_default_deny == null" <<<"$out" >/dev/null'
ok "no units or listeners when tools are missing" 'jq -e "(.units | length == 0) and (.listeners | length == 0)" <<<"$out" >/dev/null'

# Tailscale CLI crash (gongyung 2026-08-30 observation) is surfaced, not parsed as config.
write_exec_stub "$TMP/bin/tailscale" <<EOF
printf 'panic: runtime error\ngoroutine 1 [running]:\n'
EOF
out="$(bash "$AUDIT" 2>/dev/null)"
ok "tailscale panic classified as crashed" 'jq -e ".tailscale.serve.status == \"crashed\" and .tailscale.funnel.status == \"crashed\" and .exposure.funnel_configured == false" <<<"$out" >/dev/null'

# --- ufw posture variations (#1431) -----------------------------------------
printf '8080/tcp                   ALLOW IN    Anywhere\n' >> "$UFW_FIX"
out="$(bash "$AUDIT" 2>/dev/null)"
ok "widened ruleset grows the list, order kept" 'jq -e ".firewall.ufw.status == \"active\" and (.firewall.ufw.rules | length) == 5 and .firewall.ufw.rules[4] == \"8080/tcp ALLOW IN Anywhere\"" <<<"$out" >/dev/null'
sed -i 's/^Default: deny (incoming)/Default: allow (incoming)/' "$UFW_FIX"
out="$(bash "$AUDIT" 2>/dev/null)"
ok "active + default allow is NOT default-deny" 'jq -e ".firewall.ufw.default_incoming == \"allow\" and .exposure.firewall_default_deny == false" <<<"$out" >/dev/null'
printf 'Status: inactive\n' > "$UFW_FIX"
out="$(bash "$AUDIT" 2>/dev/null)"
ok "ufw inactive: no rules, not default-deny" 'jq -e ".firewall.ufw.status == \"inactive\" and (.firewall.ufw.rules | length) == 0 and .firewall.ufw.rules_sha256 == null and .exposure.firewall_default_deny == false" <<<"$out" >/dev/null'
write_exec_stub "$TMP/bin/ufw" <<'UFWEOF'
echo "ERROR: You need to be root to run this script" >&2
exit 1
UFWEOF
out="$(bash "$AUDIT" 2>/dev/null)"; rc=$?
ok "failing ufw is unavailable, exit stays 0" '[ "$rc" = 0 ] && jq -e ".firewall.ufw.status == \"unavailable\" and .exposure.firewall_default_deny == null" <<<"$out" >/dev/null'

out="$(bash "$AUDIT" --bogus 2>&1)"; rc=$?
ok "unknown flag exits 2" '[ "$rc" = 2 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
