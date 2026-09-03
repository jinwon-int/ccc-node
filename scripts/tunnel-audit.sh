#!/usr/bin/env bash
# tunnel-audit — read-only, node-local inventory of the exposure surface
# (ccc-node#1366: periodic public-tunnel audit).
#
# Collects, on THIS node only, everything that can carry traffic in or out of
# it beyond plain SSH, and prints one JSON document (default) or a short
# markdown summary (--markdown). It never starts, stops, or edits anything and
# never prints tokens, secrets, or file contents — only unit names, ExecStart
# shapes with the token/secret arguments masked, listener sockets, and the
# tailscale serve/funnel status lines.
#
# What it looks at (same axes as the 2026-08-30 fleet survey that produced the
# Family Wiki tunnel registry [DOC-3283]):
#   1. systemd units (system + the runtime user's --user manager when
#      reachable) whose name or ExecStart mentions a tunnel-class program:
#      tunnel|ngrok|cloudflared|frp|bore|autossh|`ssh -R`|`ssh -L`
#   2. crontab / cron.d lines of the same shape (Termux keeps tunnels here)
#   3. non-loopback TCP listeners (`ss -tlnp`), tagged public (0.0.0.0/::)
#      vs tailnet (100.64.0.0/10 bind) vs other
#   4. tailscale serve / funnel status (funnel = public exposure)
#   5. removed-unit residue (`*.removed-*`, `*.disabled`) so orphans stay
#      visible until an operator deletes them
#   6. host firewall (ufw) status, default incoming policy, normalized rules
#      and their sha256 (#1431) — gongmyoung accepted 0.0.0.0 binds as private
#      on "ufw default-deny + allowlist" grounds (Wiki [TNL-10]); a dropped or
#      widened ruleset must be as visible as a new listener
#
# The registry comparison (what is NEW vs the Wiki page) is deliberately not
# done here: this script has no Wiki access and must stay meaningful on a node
# with nothing but bash, ss, and systemctl. Compare its JSON against the
# registry in the fleet wrapper / Wiki runbook.
#
# Exit codes: 0 = collected; 2 = usage error. Missing tools are reported inside
# the JSON (`"tools"` block), never as a failure — an absent `ss` on Termux is
# a fact about the node, not a broken audit.
set -uo pipefail

MODE=json
for arg in "$@"; do
  case "$arg" in
    --json) MODE=json ;;
    --markdown) MODE=markdown ;;
    -h|--help)
      cat <<'EOF'
Usage: tunnel-audit.sh [--json|--markdown]
Read-only inventory of this node's tunnel units, cron tunnel lines, non-loopback
listeners, tailscale serve/funnel state, and removed-unit residue. Prints JSON
(default) or a markdown summary. Never mutates anything; masks token/secret
arguments. Env: CCC_TUNNEL_AUDIT_SYSTEMD_DIR (default /etc/systemd/system),
CCC_TUNNEL_AUDIT_CRONTAB_CMD (default crontab), CCC_TUNNEL_AUDIT_SS_CMD (default ss),
CCC_TUNNEL_AUDIT_TAILSCALE_CMD (default tailscale), CCC_TUNNEL_AUDIT_CROND_DIR
(default /etc/cron.d), CCC_TUNNEL_AUDIT_UFW_CMD (default ufw).
EOF
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

export CCC_TUNNEL_AUDIT_SYSTEMD_DIR="${CCC_TUNNEL_AUDIT_SYSTEMD_DIR:-/etc/systemd/system}"
export CCC_TUNNEL_AUDIT_CRONTAB_CMD="${CCC_TUNNEL_AUDIT_CRONTAB_CMD:-crontab}"
export CCC_TUNNEL_AUDIT_SS_CMD="${CCC_TUNNEL_AUDIT_SS_CMD:-ss}"
export CCC_TUNNEL_AUDIT_TAILSCALE_CMD="${CCC_TUNNEL_AUDIT_TAILSCALE_CMD:-tailscale}"
export CCC_TUNNEL_AUDIT_CROND_DIR="${CCC_TUNNEL_AUDIT_CROND_DIR:-/etc/cron.d}"
export CCC_TUNNEL_AUDIT_UFW_CMD="${CCC_TUNNEL_AUDIT_UFW_CMD:-ufw}"
export CCC_TUNNEL_AUDIT_MODE="$MODE"

python3 - <<'PY'
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

PATTERN = re.compile(r"tunnel|ngrok|cloudflared|frp\b|frpc|frps|\bbore\b|autossh|ssh\s+[^\n]*-(R|L)\s", re.I)
# Mask anything that looks like a credential argument, keep the shape.
MASK = re.compile(r"(--token(?:-file)?[= ]|token[= ]|--secret[= ]|--auth[= ]|password[= ])\S+", re.I)
SYSTEMD_DIR = Path(os.environ["CCC_TUNNEL_AUDIT_SYSTEMD_DIR"])
CROND_DIR = Path(os.environ["CCC_TUNNEL_AUDIT_CROND_DIR"])
CRONTAB = os.environ["CCC_TUNNEL_AUDIT_CRONTAB_CMD"]
SS = os.environ["CCC_TUNNEL_AUDIT_SS_CMD"]
TAILSCALE = os.environ["CCC_TUNNEL_AUDIT_TAILSCALE_CMD"]
UFW = os.environ["CCC_TUNNEL_AUDIT_UFW_CMD"]
MODE = os.environ["CCC_TUNNEL_AUDIT_MODE"]


def mask(text: str) -> str:
    return MASK.sub(lambda m: m.group(1) + "<masked>", text)


def run(cmd, timeout=8):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "missing"
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError as exc:
        return None, f"error:{exc.__class__.__name__}"
    return p.stdout, f"rc={p.returncode}"


def unit_field(text: str, key: str) -> str:
    # systemd allows a trailing backslash to continue a value on the next
    # line; the fleet's reverse tunnels are written that way, so a first-line
    # read saw only "ssh -N -T \\" and classified them as "other".
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            value = line[len(key) + 1:]
            while value.rstrip().endswith("\\") and i + 1 < len(lines):
                value = value.rstrip()[:-1] + " " + lines[i + 1]
                i += 1
            return " ".join(value.split())
    return ""


def unit_state(name: str) -> str:
    out, status = run(["systemctl", "is-active", name])
    if out is None:
        return status
    return out.strip() or status


def scan_units():
    units, residue = [], []
    if not SYSTEMD_DIR.is_dir():
        return units, residue, "missing-dir"
    for f in sorted(SYSTEMD_DIR.iterdir()):
        n = f.name
        if not f.is_file():
            continue
        if ".removed-" in n or n.endswith(".disabled"):
            if PATTERN.search(n):
                residue.append({"file": n, "mtime": int(f.stat().st_mtime)})
            continue
        if not n.endswith(".service"):
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        exec_start = unit_field(text, "ExecStart")
        if not (PATTERN.search(n) or PATTERN.search(exec_start)):
            continue
        shape = mask(exec_start)
        kind = "other"
        if "cloudflared" in shape:
            kind = "cloudflared"
        elif re.search(r"ssh\s.*-R\s", shape):
            kind = "ssh-reverse"
        elif re.search(r"ssh\s.*-L\s", shape):
            kind = "ssh-forward"
        elif re.search(r"ngrok|frp|bore", shape, re.I):
            kind = "public-tunnel"
        units.append({
            "unit": n,
            "kind": kind,
            "active": unit_state(n),
            "description": unit_field(text, "Description"),
            "exec_start": shape[:300],
        })
    return units, residue, "ok"


def scan_cron():
    lines = []
    out, status = run([CRONTAB, "-l"])
    if out:
        for line in out.splitlines():
            if line.strip().startswith("#") or not line.strip():
                continue
            if PATTERN.search(line):
                lines.append({"source": "crontab", "line": mask(line)[:300]})
    if CROND_DIR.is_dir():
        for f in sorted(CROND_DIR.iterdir()):
            try:
                for line in f.read_text(errors="replace").splitlines():
                    if line.strip().startswith("#"):
                        continue
                    if PATTERN.search(line):
                        lines.append({"source": f"cron.d/{f.name}", "line": mask(line)[:300]})
            except OSError:
                continue
    return lines, status


def classify_bind(addr: str) -> str:
    host = addr.rsplit(":", 1)[0].strip("[]")
    if host in ("0.0.0.0", "*", "::", ""):
        return "public"
    if host.startswith("127.") or host == "::1":
        return "loopback"
    if host.startswith("100.") and 64 <= int(host.split(".")[1]) <= 127:
        return "tailnet"
    if host.lower().startswith("fd7a:115c:a1e0:"):  # Tailscale ULA range
        return "tailnet"
    return "other"


def scan_listeners():
    out, status = run([SS, "-tlnpH"])
    if out is None:
        out, status = run([SS, "-tlnp"])
    if out is None:
        return [], status
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] == "State":
            continue
        local = parts[3]
        bind = classify_bind(local)
        if bind == "loopback":
            continue
        proc = ""
        m = re.search(r'users:\(\("([^"]+)"', line)
        if m:
            proc = m.group(1)
        rows.append({"local": local, "bind": bind, "process": proc})
    return rows, status


def scan_tailscale():
    result = {}
    for sub in ("serve", "funnel"):
        out, status = run([TAILSCALE, sub, "status"])
        if out is None:
            result[sub] = {"status": status, "lines": []}
            continue
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        if any("panic" in l or "goroutine" in l for l in lines):
            result[sub] = {"status": "crashed", "lines": lines[:2]}
            continue
        if sub == "funnel":
            # `tailscale funnel status` prints the whole serve config and
            # tags tailnet-only entries "(tailnet only)"; funnel is on only
            # for entries marked "(Funnel on)". Treating any output as
            # "configured" flagged seoseo and dungae (serve-only) as public.
            configured = any("(Funnel on)" in l or "Funnel on" in l for l in lines)
        else:
            configured = bool(lines) and not any(
                "No serve config" in l or "not running" in l or "Funnel is not" in l for l in lines
            )
        result[sub] = {"status": status, "configured": configured, "lines": lines[:6]}
    return result


def scan_ufw():
    """Host firewall posture (#1431). Every outcome is a fact, never an error:
    status is active / inactive / missing (no binary) / unavailable (present
    but rc != 0 — non-root, broken backend) / unknown (unparsable output).
    `ufw status verbose` prints metadata, a "To Action From" header, a dashed
    separator, then the rule table — so everything AFTER the first dashed
    separator row is a rule. Rule order is meaningful in ufw (first match
    wins): the normalized list keeps order, only whitespace collapses, and
    "(v6)" markers stay so v4/v6 pairs remain distinguishable. The hash covers
    the ordered normalized ruleset, so ANY widening, narrowing, or reordering
    changes it."""
    out, status = run([UFW, "status", "verbose"])
    if out is None:
        # `status verbose` missing on old ufw → retry plain `status`.
        out, status = run([UFW, "status"])
    if out is None:
        fact = "missing" if status == "missing" else "unavailable"
        return {"status": fact, "default_incoming": None, "rules": [], "rules_sha256": None}
    if status != "rc=0":
        return {"status": "unavailable", "default_incoming": None, "rules": [], "rules_sha256": None}
    lines = out.splitlines()
    m = re.search(r"^Status:\s*(\S+)", out, re.M)
    state = m.group(1).lower() if m else "unknown"
    default_incoming = None
    dm = re.search(r"^Default:\s*(\S+)\s*\(incoming\)", out, re.M | re.I)
    if dm:
        default_incoming = dm.group(1).lower()
    sep = None
    for i, line in enumerate(lines):
        t = line.strip().replace(" ", "")
        if len(t) >= 4 and set(t) == {"-"}:
            sep = i
            break
    rules = []
    if sep is not None:
        for line in lines[sep + 1:]:
            t = " ".join(line.split())
            if t:
                rules.append(t)
    rules_sha256 = None
    if rules:
        import hashlib
        rules_sha256 = hashlib.sha256("\n".join(rules).encode("utf-8", "replace")).hexdigest()
    return {"status": state, "default_incoming": default_incoming, "rules": rules, "rules_sha256": rules_sha256}


units, residue, units_status = scan_units()
cron_lines, cron_status = scan_cron()
listeners, ss_status = scan_listeners()
tailscale = scan_tailscale()
ufw = scan_ufw()

public_listeners = [l for l in listeners if l["bind"] == "public"]
funnel = tailscale.get("funnel", {})
# firewall_default_deny: true only when the host firewall is enforcing a deny
# (or reject) default on incoming — the property gongmyoung's baseline rests
# on. inactive/allow → false; missing/unavailable/unknown → null (unknown,
# not safe).
firewall_default_deny = None
if ufw["status"] == "active":
    firewall_default_deny = ufw["default_incoming"] in ("deny", "reject")
elif ufw["status"] == "inactive":
    firewall_default_deny = False
exposure = {
    "cloudflared_units": sum(1 for u in units if u["kind"] == "cloudflared"),
    "reverse_ssh_units": sum(1 for u in units if u["kind"] == "ssh-reverse"),
    "public_tunnel_units": sum(1 for u in units if u["kind"] == "public-tunnel"),
    "public_listeners": len(public_listeners),
    "funnel_configured": bool(funnel.get("configured")),
    "residue_files": len(residue),
    "firewall_default_deny": firewall_default_deny,
}

doc = {
    "schema": "ccc.tunnel-audit.v1",
    "node": os.environ.get("CCC_NODE") or socket.gethostname(),
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "tools": {
        "systemctl": shutil.which("systemctl") is not None,
        "ss": shutil.which(SS) is not None,
        "tailscale": shutil.which(TAILSCALE) is not None,
        "units": units_status,
        "crontab": cron_status,
        "ss_status": ss_status,
    },
    "exposure": exposure,
    "units": units,
    "cron": cron_lines,
    "listeners": listeners,
    "tailscale": tailscale,
    "firewall": {"ufw": ufw},
    "residue": residue,
}

if MODE == "json":
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    sys.exit(0)

print(f"# tunnel audit — {doc['node']} ({doc['generated_at']})")
print("")
print(f"- cloudflared units: {exposure['cloudflared_units']} · reverse ssh: {exposure['reverse_ssh_units']}"
      f" · public tunnel units: {exposure['public_tunnel_units']} · public listeners: {exposure['public_listeners']}"
      f" · funnel: {'YES' if exposure['funnel_configured'] else 'no'} · residue: {exposure['residue_files']}")
ufw_brief = ufw["status"]
if ufw["status"] == "active":
    ufw_brief += f" (default-in {ufw['default_incoming'] or '?'}, {len(ufw['rules'])} rules)"
print(f"- ufw: {ufw_brief}")
if units:
    print("")
    print("| unit | kind | active | exec |")
    print("|---|---|---|---|")
    for u in units:
        print(f"| `{u['unit']}` | {u['kind']} | {u['active']} | `{u['exec_start'][:80]}` |")
if cron_lines:
    print("")
    for c in cron_lines:
        print(f"- cron[{c['source']}]: `{c['line'][:120]}`")
if listeners:
    print("")
    print("| listener | bind | process |")
    print("|---|---|---|")
    for l in listeners:
        print(f"| `{l['local']}` | {l['bind']} | {l['process'] or '-'} |")
if residue:
    print("")
    for r in residue:
        print(f"- residue: `{r['file']}`")
PY
