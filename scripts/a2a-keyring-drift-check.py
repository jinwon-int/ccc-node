#!/usr/bin/env python3
"""a2a-keyring-drift-check.py — detect worker signing-key drift across the
a2a key surfaces before it breaks receipt verification.

Root cause class (fleet-skills#130, 2026-09-04 / jingun 2026-09-05): a worker
g2 key rotation updates the broker http-signature registry (and the worker
env) but not the fleet keyring, so receipts signed by that worker fail
keyring verification ('worker signature is invalid') while the broker still
accepts and countersigns them.

Surfaces compared (only sha256 fingerprints are printed; private key material
is parsed in memory to derive the public key and never stored/logged):

  keyring    refs/a2a-public-keyring.json (repo, fetched via gh api)
  registry   broker http-signature registries:
               T1 (seoseo) local file
               T2 (gwakga) via `ssh gwakga cat ...`
  local      configured worker key ID and JWK over fleet ssh (systemd workers; Termux workers
             have no readable local key and rely on registry coverage)

Per-worker status:
  match          keyring agrees with at least one verifying surface
  DRIFT:*        keyring disagrees with a verifying surface (exit 1)
  retained-key   another local key ID is selected, with no registry for this ID
  unverifiable   no surface could compare against the keyring
  local-unreadable / unreachable / no-local-key   info rows for the local probe
  not-in-keyring registry identity absent from the keyring (info; e.g. canary)

Local comparisons require an exact key-ID match, never just a worker name.
Retained keys stay in the keyring for historical receipt verification. Missing
or malformed local key IDs do not establish that any key is historical. This
probe reads configured env, not the running process; it does not attest activation.

Env overrides:
  CCC_KEYRING_REPO           repo with refs/a2a-public-keyring.json
                             (default jinwon-int/fleet-skills)
  A2A_KEYRING_T1_REGISTRY    T1 registry path (default
                             /var/lib/a2a-broker/keys/http-signature-registry.json)
  A2A_KEYRING_T2_SSH         `host:path` for the T2 registry
                             (default gwakga:/var/lib/a2a-broker/keys/http-signature-registry.json)
  A2A_KEYRING_LOCAL_NODES    comma-separated ssh hosts for the local probe
                             (default fleet systemd workers)
  A2A_KEYRING_SSH_TIMEOUT    per-host ssh timeout seconds (default 12)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys

DEFAULT_REPO = "jinwon-int/fleet-skills"
DEFAULT_T1 = "/var/lib/a2a-broker/keys/http-signature-registry.json"
DEFAULT_T2 = "gwakga:/var/lib/a2a-broker/keys/http-signature-registry.json"
DEFAULT_LOCAL_NODES = "gongmyoung,dungae,soonwook,jingun,nosuk,sogyo,bangtong,yukson"
WORKER_ENV_PATH = "/etc/default/a2a-hermes-worker"

SPKI_PREFIX = base64.b64decode("MCowBQYDK2VwAyEA")


def fp(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout)  # noqa: S603


def jwk_x_to_raw(x: str) -> bytes:
    return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))


def pem_to_raw(pem: str) -> bytes:
    body = "".join(
        line for line in pem.splitlines() if "BEGIN" not in line and "END" not in line
    )
    der = base64.b64decode(body)
    if not der.startswith(SPKI_PREFIX):
        raise ValueError("keyring entry is not an Ed25519 SPKI PEM")
    return der[len(SPKI_PREFIX):]


def load_keyring(repo: str) -> dict[str, bytes]:
    proc = _run(
        ["gh", "api", f"repos/{repo}/contents/refs/a2a-public-keyring.json"],
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"keyring fetch failed: {proc.stderr.decode()[:120]}")
    payload = json.loads(proc.stdout.decode("utf-8"))
    keyring = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
    out = {}
    for keyid, entry in keyring.get("keys", {}).items():
        if keyid.startswith("worker:"):
            out[keyid] = pem_to_raw(entry)
    return out


def parse_registry(text: str) -> dict[str, bytes]:
    registry = json.loads(text)
    out = {}
    for keyid, entry in registry.items():
        if keyid.startswith("worker:"):
            x = (entry.get("publicKeyJwk") or {}).get("x", "")
            if x:
                out[keyid] = jwk_x_to_raw(x)
    return out


def load_registry_t1(path: str) -> dict[str, bytes]:
    with open(path, encoding="utf-8") as handle:
        return parse_registry(handle.read())


def load_registry_t2(spec: str) -> dict[str, bytes]:
    host, _, path = spec.partition(":")
    proc = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 host, "cat", path], timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(f"T2 registry fetch failed: {proc.stderr.decode()[:120]}")
    return parse_registry(proc.stdout.decode("utf-8"))


def read_worker_env(node: str, timeout: int) -> tuple[str, str | None]:
    """Return (status, jwk_text). status in
    ok / no-local-key / local-unreadable / unreachable."""
    cmds = [
        f"cat {WORKER_ENV_PATH}",
        f"sudo -n cat {WORKER_ENV_PATH}",
    ]
    last_rc = 255
    for cmd in cmds:
        try:
            proc = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                         node, f"{cmd} 2>/dev/null"], timeout=timeout)
        except subprocess.TimeoutExpired:
            return "unreachable", None
        last_rc = proc.returncode
        env_text = proc.stdout.decode("utf-8", errors="replace")
        if "PRIVATE_KEY_JWK=" in env_text:
            return "ok", env_text
        if proc.returncode != 0 and "not found" in proc.stderr.decode():
            return "no-local-key", None
    if last_rc == 0:
        return "no-local-key", None
    return "local-unreadable", None


def env_worker_raw(env_text: str) -> bytes | None:
    import re
    match = re.search(
        r"^A2A_HTTP_SIGNATURE_WORKER_PRIVATE_KEY_JWK=(.+)$", env_text, re.MULTILINE
    )
    if not match:
        return None
    jwk = json.loads(match.group(1).strip().strip('"').strip("'"))
    return jwk_x_to_raw(jwk["x"])



def env_worker_keyid(env_text: str, node: str) -> str | None:
    values = re.findall(
        r"^A2A_HTTP_SIGNATURE_WORKER_KEY_ID=(.*)$", env_text, re.MULTILINE
    )
    if len(values) != 1:
        return None
    value = values[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if not re.fullmatch(r"worker:" + re.escape(node) + r":[A-Za-z0-9_.:-]+", value):
        return None
    return value


def probe_worker(node: str, timeout: int) -> tuple[str, str | None, bytes | None]:
    status, env_text = read_worker_env(node, timeout)
    if status != "ok":
        return status, None, None
    keyid = env_worker_keyid(env_text or "", node)
    if keyid is None:
        return "local-keyid-unavailable", None, None
    try:
        raw = env_worker_raw(env_text or "")
    except (ValueError, TypeError, KeyError):
        raw = None
    if raw is None or len(raw) != 32:
        return "local-parse-failed", None, None
    return "ok", keyid, raw


def compare_key(
    keyid: str,
    keyring: dict[str, bytes],
    registries: dict[str, dict[str, bytes]],
    local: tuple[str, str | None, bytes | None] | None,
) -> dict[str, object]:
    node = keyid.split(":")[1]
    row: dict[str, object] = {"worker": node, "keyid": keyid}
    keyring_raw = keyring.get(keyid)
    status, local_id, local_raw = local or ("not-probed", None, None)
    if local_id:
        row["local_keyid"] = local_id
    if keyring_raw is None:
        if status == "ok" and local_id == keyid:
            row.update(status="DRIFT:active-not-in-keyring")
        else:
            row.update(status="not-in-keyring", note="registry-only identity (canary?)")
        return row

    surfaces = {label: fp(reg[keyid]) for label, reg in registries.items() if keyid in reg}
    keyring_fp = fp(keyring_raw)
    row.update(keyring=keyring_fp, registries=surfaces)
    conflicts = [f"{label}:{value}" for label, value in surfaces.items() if value != keyring_fp]
    if conflicts:
        row.update(status=f"DRIFT:keyring-vs-{'/'.join(s.split(':')[0] for s in conflicts)}",
                   conflicting=conflicts)
    elif status == "ok" and local_id == keyid:
        if local_raw == keyring_raw:
            row["status"] = "match"
        else:
            row.update(status="DRIFT:keyring-vs-local", local=fp(local_raw))
    elif surfaces:
        row.update(status="match", local_probe=status if local_id is None else "different-keyid")
    elif status == "ok":
        row.update(status="retained-key", note="not selected by local worker; no registry entry")
    else:
        row.update(status="unverifiable", local_probe=status)
    return row

def main() -> int:
    repo = os.environ.get("CCC_KEYRING_REPO", DEFAULT_REPO)
    t1_path = os.environ.get("A2A_KEYRING_T1_REGISTRY", DEFAULT_T1)
    t2_spec = os.environ.get("A2A_KEYRING_T2_SSH", DEFAULT_T2)
    local_nodes = [
        n.strip()
        for n in os.environ.get("A2A_KEYRING_LOCAL_NODES", DEFAULT_LOCAL_NODES).split(",")
        if n.strip()
    ]
    ssh_timeout = int(os.environ.get("A2A_KEYRING_SSH_TIMEOUT", "12"))

    keyring = load_keyring(repo)
    registries: dict[str, dict[str, bytes]] = {}
    errors = []
    try:
        registries["t1"] = load_registry_t1(t1_path)
    except Exception as error:  # noqa: BLE001
        errors.append(f"t1: {error}")
    try:
        registries["t2"] = load_registry_t2(t2_spec)
    except Exception as error:  # noqa: BLE001
        errors.append(f"t2: {error}")

    # Probe once per worker so every key version uses the same local snapshot.
    local = {node: probe_worker(node, ssh_timeout) for node in dict.fromkeys(local_nodes)}
    all_ids = sorted(set(keyring)
                     | set(registries.get("t1", {}))
                     | set(registries.get("t2", {}))
                     | {keyid for status, keyid, _ in local.values() if status == "ok" and keyid})
    rows = [compare_key(keyid, keyring, registries, local.get(keyid.split(":")[1]))
            for keyid in all_ids]
    drift = sum(str(row["status"]).startswith("DRIFT:") for row in rows)

    print(json.dumps(
        {
            "repo": repo,
            "checked": len(rows),
            "drift": drift,
            "surface_errors": errors,
            "rows": rows,
            "remedy": (
                "DRIFT: re-sync refs/a2a-public-keyring.json to the key that the "
                "worker config selects, matching the exact key ID; preserve historical entries "
                "unless separately retired (broker registry / worker env — "
                "fleet-skills#130 pattern), then re-run the a2a-receipts workflow "
                "for affected PRs."
            ) if drift else "",
        },
        ensure_ascii=False, indent=1,
    ))
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
