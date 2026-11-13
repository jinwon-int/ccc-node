#!/usr/bin/env bash
# Hermetic tests for scripts/fleet-ssh-mesh.sh (#2687). Stub ssh / ssh-keyscan /
# ssh-keygen / tailscale; each fake node is a temp HOME, and the stub ssh runs
# the "remote" command there with sh. No real host or tailnet is contacted.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/scripts/fleet-ssh-mesh.sh"
TMP_BASE="${TMPDIR:-/tmp}"
T="$(mktemp -d "$TMP_BASE/fleet-ssh-mesh-test.XXXXXX")"
trap 'chmod -R u+rwX "$T" 2>/dev/null; rm -rf "$T"' EXIT

pass=0 fail=0
check() { # check <description> <command...>
  local desc="$1"; shift
  if "$@"; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL: $desc"; fi
}
has() { grep -qF -- "$2" <<< "$1"; }
hasnt() { ! grep -qF -- "$2" <<< "$1"; }
count_lines() { grep -cxF -- "$2" "$1"; }

STUB="$T/stub"; FLEET="$T/fleet"; NODES="$FLEET/nodes"
mkdir -p "$STUB" "$NODES" "$T/ops" "$T/tmp" "$T/cfg"
ALL_OUT="$T/all-output.log"; : > "$ALL_OUT"

# ---------------------------------------------------------------- stubs
cat > "$STUB/ssh" <<'SH'
#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case "$1" in -o|-p|-i|-l|-F) shift 2 ;; --) shift; break ;; -*) shift ;; *) break ;; esac
done
node=$1; shift
printf '%s\n' "$node" >> "$FAKE_FLEET/ssh.calls"
d="$FAKE_FLEET/nodes/$node"
if [ ! -d "$d" ] || [ -e "$d.down" ]; then
  echo "ssh: connect to host $node: unreachable" >&2; exit 255
fi
printf '== %s\n%s\n' "$node" "$*" >> "$FAKE_FLEET/remote.log"
if [ -f "$FAKE_FLEET/reply.$node" ]; then cat "$FAKE_FLEET/reply.$node"; exit 0; fi
cd "$d/home" && HOME="$d/home" PATH="$STUB_DIR:$PATH" sh -c "$*"
SH
cat > "$STUB/ssh-keygen" <<'SH'
#!/bin/sh
f=""; c=""
while [ $# -gt 0 ]; do
  case "$1" in -f) f=$2; shift 2 ;; -C) c=$2; shift 2 ;; -N|-t) shift 2 ;; *) shift ;; esac
done
printf '%s\n' '-----BEGIN OPENSSH PRIVATE KEY-----' "PRIVATE-SENTINEL-$c" '-----END OPENSSH PRIVATE KEY-----' > "$f"
chmod 600 "$f"
printf 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI%s %s\n' "$(printf %s "$c" | cksum | cut -d' ' -f1)" "$c" > "$f.pub"
echo "keygen $c" >> "$FAKE_FLEET/keygen.log"
SH
cat > "$STUB/ssh-keyscan" <<'SH'
#!/bin/sh
port=22; host=""
while [ $# -gt 0 ]; do
  case "$1" in -T|-t) shift 2 ;; -p) port=$2; shift 2 ;; *) host=$1; shift ;; esac
done
echo "$host $port" >> "$FAKE_FLEET/keyscan.calls"
[ -e "$FAKE_FLEET/keyscan-fail.$host" ] && exit 1
echo "# $host:$port SSH-2.0-OpenSSH_fake" >&2
if [ "$port" = 22 ]; then hp=$host; else hp="[$host]:$port"; fi
printf '%s ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHOST%s\n' "$hp" "$(printf %s "$host" | tr -c 'A-Za-z0-9' 'x')"
SH
cat > "$STUB/tailscale" <<'SH'
#!/bin/sh
[ "$1 $2" = "status --json" ] || exit 64
echo status >> "$FAKE_FLEET/tailscale.calls"
[ -f "$FAKE_TS_JSON" ] || exit 1
cat "$FAKE_TS_JSON"
SH
chmod +x "$STUB"/*

# Tailnet snapshot: documentation-range addresses only.
TS_OK="$T/ts-ok.json"
cat > "$TS_OK" <<'JSON'
{"Self": {"ID": "n1", "PublicKey": "nodekey:a", "HostName": "node-a",
          "DNSName": "node-a.example-tailnet.ts.net.",
          "TailscaleIPs": ["fd7a:115c:a1e0::a", "192.0.2.10"]},
 "Peer": {
  "nodekey:b": {"ID": "n2", "PublicKey": "nodekey:b", "HostName": "node-b",
                "DNSName": "node-b.example-tailnet.ts.net.", "TailscaleIPs": ["192.0.2.11"]},
  "nodekey:c": {"ID": "n3", "PublicKey": "nodekey:c", "HostName": "localhost",
                "DNSName": "node-c.example-tailnet.ts.net.", "TailscaleIPs": ["192.0.2.12"]},
  "nodekey:x": {"ID": "n4", "PublicKey": "nodekey:x", "HostName": "other",
                "DNSName": "other.example-tailnet.ts.net.", "TailscaleIPs": ["192.0.2.50"]}}}
JSON
# node-c missing.
TS_MISSING="$T/ts-missing.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); del d["Peer"]["nodekey:c"]; json.dump(d,open(sys.argv[2],"w"))' "$TS_OK" "$TS_MISSING"
# A stale second device that still reports HostName node-b.
TS_AMBIG="$T/ts-ambig.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["Peer"]["nodekey:b2"]={"ID":"n5","PublicKey":"nodekey:b2","HostName":"node-b","DNSName":"node-b-1.example-tailnet.ts.net.","TailscaleIPs":["192.0.2.99"]}; json.dump(d,open(sys.argv[2],"w"))' "$TS_OK" "$TS_AMBIG"

TS_JSON="$TS_OK"
NODES_FILE="$T/cfg/nodes"
cat > "$NODES_FILE" <<'EOF'
# test fleet: name port user [host]
node-a 22 root

node-b 2222 root   # trailing comment
node-c 22 ops
EOF

run() { # run <script args...>  -> OUT, RC (stdout+stderr merged)
  OUT=$(env -u CCC_FLEET_SSH_NODES -u CCC_FLEET_SSH -u CCC_FLEET_SSH_MARKER \
    HOME="$T/ops" CCC_FLEET_SSH_WORK="${WORK:-$T/work}" TMPDIR="$T/tmp" \
    SSH_BIN="$STUB/ssh" SSH_KEYSCAN_BIN="$STUB/ssh-keyscan" TAILSCALE_BIN="$STUB/tailscale" \
    FAKE_FLEET="$FLEET" FAKE_TS_JSON="$TS_JSON" STUB_DIR="$STUB" \
    bash "$SCRIPT" "$@" 2>&1)
  RC=$?
  printf '%s\n' "$OUT" >> "$ALL_OUT"
}
reset_calls() { rm -f "$FLEET"/ssh.calls "$FLEET"/keyscan.calls "$FLEET"/tailscale.calls "$FLEET"/remote.log; }
no_ssh_calls() { [ ! -s "$FLEET/ssh.calls" ]; }
snapshot() { # content + mode + name of every file on every fake node
  (cd "$NODES" && find . -type f | LC_ALL=C sort | while read -r f; do
    printf '%s %s %s\n' "$(stat -c %a "$f")" "$(sha256sum < "$f" | cut -c1-16)" "$f"; done)
}
outside() { awk -v mb="$MB" -v me="$ME" '$0==mb{skip=1} !skip{print} $0==me{skip=0}' "$1"; }
inside() { awk -v mb="$MB" -v me="$ME" '$0==mb{on=1} on{print} $0==me{on=0}' "$1"; }
MB="# >>> SEOYOON-FLEET-MESH >>>"
ME="# <<< SEOYOON-FLEET-MESH <<<"

# Fake nodes. node-a already has a fleet key and pre-existing files carrying
# an old managed block (as deployed by the original script) between lines
# that are not ours.
for n in node-a node-b node-c; do mkdir -p "$NODES/$n/home"; done
A="$NODES/node-a/home/.ssh"; mkdir -p "$A"; chmod 700 "$A"
printf '%s\n' '-----BEGIN OPENSSH PRIVATE KEY-----' 'PRIVATE-SENTINEL-node-a' '-----END OPENSSH PRIVATE KEY-----' > "$A/id_ed25519_fleet"
printf 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIpreexisting node-a-fleet-old\n' > "$A/id_ed25519_fleet.pub"
cat > "$A/authorized_keys" <<EOF
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIoperator operator@laptop
$MB
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIstale stale-fleet
$ME
ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQkeep keep-me
EOF
printf 'legacy.example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIlegacy\n' > "$A/known_hosts"
cat > "$A/config" <<'EOF'
Host legacy
    HostName legacy.example
    User someone
EOF
chmod 600 "$A/authorized_keys" "$A/config"; chmod 644 "$A/known_hosts"
cp -p "$A/authorized_keys" "$T/orig_authorized_keys"
cp -p "$A/config" "$T/orig_config"

# ------------------------------------------------ node table: fail closed
reset_calls
run collect
check "missing default node table exits 2" test "$RC" = 2
check "missing table names the default path" has "$OUT" "node table not found: $T/ops/.config/ccc/fleet-ssh-nodes"
check "missing table: no ssh call" no_ssh_calls

run --nodes "$T/cfg/absent" push
check "missing --nodes file exits 2" test "$RC" = 2
check "missing --nodes names that path" has "$OUT" "$T/cfg/absent"

i=0
while IFS= read -r bad; do
  i=$((i + 1))
  printf 'node-a 22 root\n%b\n' "$bad" > "$T/cfg/bad$i"
  reset_calls
  run --nodes "$T/cfg/bad$i" all
  check "invalid table [$bad] exits 2" test "$RC" = 2
  check "invalid table [$bad] reports line 2" has "$OUT" "bad$i:2:"
  check "invalid table [$bad]: no ssh/tailscale call" test ! -e "$FLEET/ssh.calls" -a ! -e "$FLEET/tailscale.calls"
done <<'EOF'
node-b x root
node-b 70000 root
node-b 0 root
node;b 22 root
$(id) 22 root
node-b 22 root host;rm
node-b 22
node-b 22 root host extra
node-a 22 root
node-b 22 ro$ot
EOF
printf '# only comments\n\n   \n' > "$T/cfg/empty"
run --nodes "$T/cfg/empty" collect
check "empty table exits 2" test "$RC" = 2
check "empty table message" has "$OUT" "node table has no nodes"

run --dry-run collect
check "--dry-run outside push is refused" test "$RC" = 2
run frobnicate
check "unknown phase exits 2" test "$RC" = 2

# ---------------------------------------------- tailscale host resolution
reset_calls
TS_JSON="$TS_MISSING" run --nodes "$NODES_FILE" scan
check "unresolvable node exits 2" test "$RC" = 2
check "not-found message names node-c" has "$OUT" "node 'node-c' not found in tailscale status"
check "not found: no keyscan" test ! -e "$FLEET/keyscan.calls"

reset_calls
TS_JSON="$TS_AMBIG" run --nodes "$NODES_FILE" all
check "ambiguous node exits 2" test "$RC" = 2
check "ambiguous message names node-b" has "$OUT" "node 'node-b' is ambiguous in tailscale status (2 devices match)"
check "ambiguous: all stops before any ssh (collect never ran)" no_ssh_calls
check "ambiguous: no keyscan" test ! -e "$FLEET/keyscan.calls"

reset_calls
TS_JSON="$T/no-such.json" run --nodes "$NODES_FILE" scan
check "tailscale failure exits 2" test "$RC" = 2
check "tailscale failure message" has "$OUT" "status --json' failed"

# Explicit hosts never consult tailscale (which is broken here).
printf 'node-a 22 root 192.0.2.10\nnode-b 2222 root node-b.example\n' > "$T/cfg/explicit"
reset_calls
TS_JSON="$T/no-such.json" WORK="$T/work-explicit" run --nodes "$T/cfg/explicit" scan
check "explicit hosts: scan ok" test "$RC" = 0
check "explicit hosts: tailscale not called" test ! -e "$FLEET/tailscale.calls"
check "explicit hosts: keyscan got the given host/port" grep -qx 'node-b.example 2222' "$FLEET/keyscan.calls"

# ------------------------------------------------------------- collect
priv_sum=$(sha256sum < "$A/id_ed25519_fleet")
[ "$(id -u)" = 0 ] || chmod 000 "$A/id_ed25519_fleet"   # unreadable: collect must not need it
reset_calls; rm -f "$FLEET/keygen.log"
run --nodes "$NODES_FILE" collect
check "collect exits 0" test "$RC" = 0
check "collect OK for all three" test "$(grep -c '^COLLECT-OK ' <<< "$OUT")" = 3
check "existing key reused, not regenerated" grep -qxF 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIpreexisting node-a-fleet-old' "$T/work/fleet_pubkeys"
check "keygen only for nodes without a key" test "$(sort "$FLEET/keygen.log" | cut -d' ' -f2 | sed 's/-fleet-[0-9]*$//' | tr '\n' ' ')" = "node-b node-c "
check "new key comment is <name>-fleet-<date>" grep -qE ' node-b-fleet-[0-9]{8}$' "$T/work/fleet_pubkeys"
check "fleet_pubkeys has one line per node" test "$(wc -l < "$T/work/fleet_pubkeys")" = 3
cat_targets=$(grep -oE 'cat [^;|&]*' "$FLEET/remote.log" | sed 's/[[:space:]]*$//')
check "collect cats exactly one file per node" test "$(wc -l <<< "$cat_targets")" = 3
check "collect only ever cats the .pub" test -z "$(grep -v '\.pub$' <<< "$cat_targets")"
check "private key never in collected output" hasnt "$(cat "$T/work"/* "$ALL_OUT")" "PRIVATE"
[ "$(id -u)" = 0 ] || chmod 600 "$A/id_ed25519_fleet"
check "private key on node untouched" test "$(sha256sum < "$A/id_ed25519_fleet")" = "$priv_sum"
check "private key stays on its node (newly generated too)" test -f "$NODES/node-b/home/.ssh/id_ed25519_fleet"

# ---------------------------------------------------------- scan + build
reset_calls
run --nodes "$NODES_FILE" scan
check "scan exits 0" test "$RC" = 0
check "scan resolved node-a via HostName (IPv4 preferred)" grep -qx '192.0.2.10 22' "$FLEET/keyscan.calls"
check "scan resolved node-c via MagicDNS name" grep -qx '192.0.2.12 22' "$FLEET/keyscan.calls"
check "scan uses the table port" grep -qx '192.0.2.11 2222' "$FLEET/keyscan.calls"
check "known_hosts drops keyscan comments" test "$(wc -l < "$T/work/fleet_known_hosts")" = 3
check "non-22 port host key is bracketed" grep -q '^\[192.0.2.11\]:2222 ssh-ed25519 ' "$T/work/fleet_known_hosts"

run --nodes "$NODES_FILE" build
check "build exits 0" test "$RC" = 0
check "build summary" has "$OUT" "BUILD-OK 3 pubkeys, 3 hostkeys"
for b in authorized_keys known_hosts config; do
  check "block_$b starts with begin marker" test "$(head -1 "$T/work/block_$b")" = "$MB"
  check "block_$b ends with end marker" test "$(tail -1 "$T/work/block_$b")" = "$ME"
done
check "config block: node-b entry" grep -qx 'Host node-b' "$T/work/block_config"
check "config block: resolved HostName" grep -qx '    HostName 192.0.2.12' "$T/work/block_config"
check "config block: port" grep -qx '    Port 2222' "$T/work/block_config"
check "config block: user" grep -qx '    User ops' "$T/work/block_config"
check "config block: fleet identity" test "$(grep -c '    IdentityFile ~/.ssh/id_ed25519_fleet' "$T/work/block_config")" = 3

# -------------------------------------------------------- push --dry-run
before=$(snapshot)
reset_calls
run --nodes "$NODES_FILE" --dry-run push
check "dry-run exits 0" test "$RC" = 0
check "dry-run writes nothing on any node" test "$(snapshot)" = "$before"
check "dry-run reports the change" has "$OUT" "PUSH-DRYRUN-CHANGE node-a ~/.ssh/authorized_keys"
check "dry-run shows removed stale key" has "$OUT" "-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIstale stale-fleet"
check "dry-run lists 9 node/file results" test "$(grep -c '^PUSH-DRYRUN-' <<< "$OUT")" = 9
check "dry-run creates no backup" test -z "$(find "$NODES" -name '*.bak-*')"
check "dry-run leaves its temp dir clean" test -z "$(ls -A "$T/tmp")"

# ---------------------------------------------------------------- push
reset_calls
run --nodes "$NODES_FILE" push
check "push exits 0" test "$RC" = 0
check "push OK for all three" test "$(grep -c '^PUSH-OK ' <<< "$OUT")" = 3
for n in node-a node-b node-c; do
  for f in authorized_keys known_hosts config; do
    check "$n $f: exactly one managed block" test "$(count_lines "$NODES/$n/home/.ssh/$f" "$MB")" = 1
    check "$n $f: block equals the built block" cmp -s <(inside "$NODES/$n/home/.ssh/$f") "$T/work/block_$f"
  done
  check "$n authorized_keys mode 600" test "$(stat -c %a "$NODES/$n/home/.ssh/authorized_keys")" = 600
  check "$n known_hosts mode 644" test "$(stat -c %a "$NODES/$n/home/.ssh/known_hosts")" = 644
  check "$n config mode 600" test "$(stat -c %a "$NODES/$n/home/.ssh/config")" = 600
done
check "stale block replaced, not duplicated" test "$(grep -c 'stale-fleet' "$A/authorized_keys")" = 0
check "lines outside the block preserved byte-for-byte (authorized_keys)" cmp -s <(outside "$A/authorized_keys") <(outside "$T/orig_authorized_keys")
check "lines outside the block preserved byte-for-byte (config)" cmp -s <(outside "$A/config") "$T/orig_config"
check "config block is prepended" test "$(head -1 "$A/config")" = "$MB"
check "authorized_keys block is appended" test "$(tail -1 "$A/authorized_keys")" = "$ME"
bak=$(find "$A" -name 'authorized_keys.bak-*')
check "one timestamped backup per file" test "$(wc -l <<< "$bak")" = 1
check "backup name is .bak-YYYYmmddHHMMSS" grep -qE '/authorized_keys\.bak-[0-9]{14}$' <<< "$bak"
check "backup holds the pre-push content" cmp -s "$bak" "$T/orig_authorized_keys"
check "config backup holds the pre-push content" cmp -s "$(find "$A" -name 'config.bak-*')" "$T/orig_config"
check "no temp files left on node" test -z "$(find "$NODES" -name '*.tmp' -o -name '*.stripped' -o -name '*.blk')"

# ------------------------------------------------------- idempotency
after1=$(cd "$NODES" && for f in */home/.ssh/authorized_keys */home/.ssh/known_hosts */home/.ssh/config; do printf '%s ' "$f"; sha256sum < "$f"; done)
run --nodes "$NODES_FILE" --dry-run push
check "settled state: dry-run reports 9 unchanged" test "$(grep -c '^PUSH-DRYRUN-UNCHANGED ' <<< "$OUT")" = 9
run --nodes "$NODES_FILE" build
run --nodes "$NODES_FILE" push
check "second push exits 0" test "$RC" = 0
after2=$(cd "$NODES" && for f in */home/.ssh/authorized_keys */home/.ssh/known_hosts */home/.ssh/config; do printf '%s ' "$f"; sha256sum < "$f"; done)
check "build+push twice yields identical files" test "$after1" = "$after2"
check "still a single block after re-run" test "$(count_lines "$A/authorized_keys" "$MB")" = 1
check "second push keeps a second backup" test "$(find "$A" -name 'authorized_keys.bak-*' | wc -l)" = 2
check "first backup never overwritten" test -n "$(for b in "$A"/authorized_keys.bak-*; do cmp -s "$b" "$T/orig_authorized_keys" && echo same; done)"

# --------------------------------------------- unbalanced marker refusal
B="$NODES/node-b/home/.ssh"
outside "$B/known_hosts" > "$B/known_hosts.new"; printf '%s\nhalf-written line\n' "$MB" >> "$B/known_hosts.new"
mv "$B/known_hosts.new" "$B/known_hosts"; chmod 644 "$B/known_hosts"
kh_sum=$(sha256sum < "$B/known_hosts"); kh_baks=$(find "$B" -name 'known_hosts.bak-*' | wc -l)
run --nodes "$NODES_FILE" --dry-run push
check "dry-run refuses an unbalanced block" has "$OUT" "PUSH-DRYRUN-REFUSE node-b ~/.ssh/known_hosts"
check "dry-run with refusal exits 1" test "$RC" = 1
run --nodes "$NODES_FILE" push
check "push with unbalanced block exits 1" test "$RC" = 1
check "push reports the node" has "$OUT" "PUSH-FAIL node-b"
check "other nodes still pushed" test "$(grep -c '^PUSH-OK ' <<< "$OUT")" = 2
check "unbalanced file left unchanged" test "$(sha256sum < "$B/known_hosts")" = "$kh_sum"
check "no backup taken for a refused file" test "$(find "$B" -name 'known_hosts.bak-*' | wc -l)" = "$kh_baks"
check "error names the file" has "$OUT" "unbalanced marker block"
printf '%s\n' "$ME" >> "$B/known_hosts"   # repair for the next steps

# -------------------------------------------------------------- verify
reset_calls
run --nodes "$NODES_FILE" verify
check "verify exits 0 when the mesh is complete" test "$RC" = 0
check "verify node-a line" has "$OUT" "VERIFY node-a -> node-b node-c "
check "verify has no failures" hasnt "$OUT" "FAIL:"
touch "$NODES/node-c.down"
run --nodes "$NODES_FILE" verify
check "verify exits 1 with a down node" test "$RC" = 1
check "verify marks the unreachable target" has "$OUT" "VERIFY node-a -> node-b FAIL:node-c "
check "verify marks the unreachable source" has "$OUT" "VERIFY node-c -> FAIL:unreachable"

# --------------------------------------- partial collect / bad pubkey reply
WORK="$T/work-partial"; export WORK
reset_calls
run --nodes "$NODES_FILE" collect
check "collect with a down node exits 1" test "$RC" = 1
check "collect reports the down node" has "$OUT" "COLLECT-FAIL node-c"
rm -f "$NODES/node-c.down"
printf '%s\n%s\n' '-----BEGIN OPENSSH PRIVATE KEY-----' 'b3BlbnNzaC1rZXk' > "$FLEET/reply.node-b"
run --nodes "$NODES_FILE" collect
check "non-pubkey reply is rejected" has "$OUT" "COLLECT-FAIL node-b"
check "rejected reply never stored" hasnt "$(cat "$WORK/fleet_pubkeys")" "PRIVATE"
rm -f "$FLEET/reply.node-b"
run --nodes "$NODES_FILE" scan
run --nodes "$NODES_FILE" build
check "build refuses a partial collect" test "$RC" = 1
check "partial build explains why" has "$OUT" "BUILD-FAIL partial: 2 pubkeys, 3 hostkeys for 3 nodes"
check "partial build writes no blocks" test ! -e "$WORK/block_authorized_keys"
run --nodes "$NODES_FILE" --allow-partial build
check "--allow-partial builds anyway" test "$RC" = 0
check "--allow-partial warns" has "$OUT" "BUILD-WARN partial"
unset WORK

# ------------------------------------------------- env path + overrides
printf 'node-a 22 root 192.0.2.10\n' > "$T/cfg/env-nodes"
OUT=$(env -u CCC_FLEET_SSH -u CCC_FLEET_SSH_MARKER HOME="$T/ops" CCC_FLEET_SSH_NODES="$T/cfg/env-nodes" CCC_FLEET_SSH_WORK="$T/work-env" \
  SSH_BIN="$STUB/ssh" FAKE_FLEET="$FLEET" STUB_DIR="$STUB" bash "$SCRIPT" collect 2>&1); RC=$?
check "CCC_FLEET_SSH_NODES selects the table" test "$RC" = 0
check "env table used (one node)" test "$(grep -c '^COLLECT-OK ' <<< "$OUT")" = 1
OUT=$(env -u CCC_FLEET_SSH -u CCC_FLEET_SSH_MARKER HOME="$T/ops" CCC_FLEET_SSH_NODES="$T/cfg/env-nodes" CCC_FLEET_SSH_WORK="$T/work-env" \
  SSH_BIN="$STUB/ssh" FAKE_FLEET="$FLEET" STUB_DIR="$STUB" bash "$SCRIPT" --nodes "$NODES_FILE" collect 2>&1); RC=$?
check "--nodes overrides the env table" test "$(grep -c '^COLLECT-OK ' <<< "$OUT")" = 3

# ---------------------------------------------------- final key sweep
check "no private key material anywhere off-node" test -z "$(grep -rl 'PRIVATE' "$T/work" "$T/work-partial" "$T/work-env" "$T/ops" "$T/tmp" "$ALL_OUT" 2>/dev/null)"

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
