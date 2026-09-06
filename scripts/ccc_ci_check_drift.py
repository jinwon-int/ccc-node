#!/usr/bin/env python3
"""Read-only comparison of desired checks and effective GitHub protections."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import quote


class EvidenceError(ValueError):
    """Protection evidence is incomplete or not understood."""


def object_value(value, label):
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    return value


def list_value(value, label):
    if not isinstance(value, list):
        raise EvidenceError(f"{label} must be an array")
    return value


def bool_value(value, label):
    if not isinstance(value, bool):
        raise EvidenceError(f"{label} must be boolean")
    return value


def check_pairs(checks, app_key):
    result = set()
    for item in list_value(checks, "checks"):
        item = object_value(item, "check")
        name = item.get("context")
        app = item.get(app_key)
        if not isinstance(name, str) or not name.strip():
            raise EvidenceError("check context must be nonempty text")
        if app is not None and (type(app) is not int or app < -1):
            raise EvidenceError("invalid check app binding")
        if app_key not in item:
            raise EvidenceError("missing check app binding")
        result.add((name, app))
    return result


def desired_state(manifest):
    manifest = object_value(manifest, "manifest")
    branch = manifest.get("branch")
    if not isinstance(branch, str) or not branch:
        raise EvidenceError("manifest branch is missing")
    app = manifest.get("app_id")
    if type(app) is not int or app <= 0:
        raise EvidenceError("manifest app_id must be positive")
    strict = bool_value(manifest.get("strict"), "manifest strict")
    checks = list_value(manifest.get("checks"), "manifest checks")
    desired = check_pairs([dict(object_value(c, "manifest check"), app_id=app)
                           for c in checks], "app_id")
    if not desired or len(desired) != len(checks):
        raise EvidenceError("manifest checks must be nonempty and unique")
    return branch, strict, desired


def effective_state(legacy, rules):
    legacy = object_value(legacy, "legacy protection")
    strict = bool_value(legacy.get("strict"), "legacy strict")
    actual = check_pairs(legacy.get("checks"), "app_id")
    contexts = list_value(legacy.get("contexts"), "legacy contexts")
    if any(not isinstance(c, str) for c in contexts):
        raise EvidenceError("legacy contexts must contain text")
    if set(contexts) != {name for name, _ in actual}:
        raise EvidenceError("legacy contexts/checks disagree")
    for rule in list_value(rules, "effective branch rules"):
        rule = object_value(rule, "rule")
        if not isinstance(rule.get("type"), str):
            raise EvidenceError("rule type is missing")
        if rule["type"] != "required_status_checks":
            # Other rules (reviews, deletion, etc.) are outside this checker.
            continue
        params = object_value(rule.get("parameters"), "status-check parameters")
        rule_strict = bool_value(params.get("strict_required_status_checks_policy"),
                                 "ruleset strict")
        actual |= check_pairs(params.get("required_status_checks"), "integration_id")
        strict = strict or rule_strict
    return strict, actual


def describe(pairs):
    return [{"context": name, "app_id": app}
            for name, app in sorted(pairs, key=lambda pair: (pair[0], str(pair[1])))]


def compare(manifest, legacy, rules):
    branch, wanted_strict, desired = desired_state(manifest)
    strict, actual = effective_state(legacy, rules)
    missing = desired - actual
    additional = actual - desired
    drift = bool(missing or additional or strict != wanted_strict)
    return {"status": "drift" if drift else "ok", "branch": branch,
            "missing": describe(missing), "additional": describe(additional),
            "strict": {"desired": wanted_strict, "effective": strict},
            "scope": "required_status_checks_only"}


def query(endpoint, *, pages=False):
    argv = ['gh', 'api', endpoint]
    if pages:
        argv += ['--paginate', '--slurp']
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvidenceError("GitHub read unavailable or timed out") from exc
    if result.returncode:
        # Never echo arbitrary stderr/API bodies into the diagnostic output.
        raise EvidenceError(f"GitHub read failed (exit {result.returncode}); verify gh authentication and read permission")
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise EvidenceError("GitHub returned invalid JSON") from exc
    if pages:
        return [rule for page in list_value(data, "rule pages")
                for rule in list_value(page, "rule page")]
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path,
                        default=Path(__file__).resolve().parents[1] / '.github/required-checks.json')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--repo', help='owner/repo; uses authenticated gh, GET only')
    source.add_argument('--legacy-json', type=Path, help='offline legacy required_status_checks snapshot')
    parser.add_argument('--rules-json', type=Path, help='offline effective branch-rules array (required with --legacy-json)')
    args = parser.parse_args(argv)
    if bool(args.legacy_json) != bool(args.rules_json):
        parser.error('--legacy-json and --rules-json must be supplied together')
    try:
        manifest = json.loads(args.manifest.read_text())
        branch, _, _ = desired_state(manifest)
        if args.repo:
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
                raise EvidenceError("repo must be owner/repo")
            encoded = quote(branch, safe='')
            legacy = query(f'repos/{args.repo}/branches/{encoded}/protection/required_status_checks')
            # This endpoint returns ONLY active rules applying to this branch,
            # including organization rules. Do not infer applicability from a
            # repository-wide list or accidentally count evaluate/disabled rules.
            rules = query(f'repos/{args.repo}/rules/branches/{encoded}', pages=True)
        else:
            legacy = json.loads(args.legacy_json.read_text())
            rules = json.loads(args.rules_json.read_text())
        report = compare(manifest, legacy, rules)
    except (OSError, ValueError) as exc:
        report = {"status": "error", "reason": str(exc),
                  "scope": "required_status_checks_only"}
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return {"ok": 0, "drift": 1, "error": 2}[report['status']]


if __name__ == '__main__':
    raise SystemExit(main())
