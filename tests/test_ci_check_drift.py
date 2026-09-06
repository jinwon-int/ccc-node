"""Synthetic API evidence for the read-only required-check drift report."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location(
    'ccc_ci_check_drift', Path(__file__).resolve().parents[1] / 'scripts/ccc_ci_check_drift.py')
drift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drift)


@pytest.fixture
def evidence():
    return ({'branch': 'main', 'strict': True, 'app_id': 15368,
             'checks': [{'context': 'tests'}, {'context': 'wheel-smoke'}]},
            {'strict': True, 'contexts': ['tests', 'wheel-smoke'],
             'checks': [{'context': 'tests', 'app_id': 15368},
                        {'context': 'wheel-smoke', 'app_id': 15368}]}, [])


def test_exact_match(evidence):
    assert drift.compare(*evidence)['status'] == 'ok'


def test_missing_check_and_wrong_app_are_not_success(evidence):
    manifest, legacy, rules = evidence
    legacy['checks'][1]['app_id'] = None
    report = drift.compare(manifest, legacy, rules)
    assert report['status'] == 'drift'
    assert report['missing'] == [{'context': 'wheel-smoke', 'app_id': 15368}]
    assert report['additional'] == [{'context': 'wheel-smoke', 'app_id': None}]


def test_ruleset_union_supplies_missing_legacy_check_and_strictness(evidence):
    manifest, legacy, rules = evidence
    legacy['checks'].pop()
    legacy['contexts'].pop()
    legacy['strict'] = False
    rules.append({'type': 'required_status_checks', 'parameters': {
        'strict_required_status_checks_policy': True,
        'required_status_checks': [{'context': 'wheel-smoke', 'integration_id': 15368}]}})
    assert drift.compare(manifest, legacy, rules)['status'] == 'ok'


def test_additional_context_is_reported_without_removal(evidence):
    manifest, legacy, rules = evidence
    legacy['contexts'].append('operator-check')
    legacy['checks'].append({'context': 'operator-check', 'app_id': 42})
    report = drift.compare(manifest, legacy, rules)
    assert report['additional'] == [{'context': 'operator-check', 'app_id': 42}]
    assert report['status'] == 'drift'


def test_non_strict_is_drift(evidence):
    evidence[1]['strict'] = False
    assert drift.compare(*evidence)['status'] == 'drift'


@pytest.mark.parametrize('mutation', [
    lambda m, legacy, r: legacy.pop('checks'),
    lambda m, legacy, r: legacy.update(strict='true'),
    lambda m, legacy, r: legacy['contexts'].pop(),
    lambda m, legacy, r: legacy['checks'][0].pop('app_id'),
    lambda m, legacy, r: legacy['checks'][0].update(app_id=True),
    lambda m, legacy, r: m.update(checks=[]),
    lambda m, legacy, r: m['checks'].append(copy.deepcopy(m['checks'][0])),
    lambda m, legacy, r: r.append({'type': 'required_status_checks', 'parameters': {}}),
    lambda m, legacy, r: r.append({}),
])
def test_incomplete_or_malformed_evidence_is_not_ok(evidence, mutation):
    mutation(*evidence)
    with pytest.raises(drift.EvidenceError):
        drift.compare(*evidence)


def test_unrelated_rules_are_outside_scope(evidence):
    evidence[2].append({'type': 'deletion'})
    assert drift.compare(*evidence)['scope'] == 'required_status_checks_only'


def test_query_pagination_and_read_only_transport():
    response = subprocess.CompletedProcess([], 0, '[[{"type":"deletion"}],[]]', '')
    with patch.object(drift.subprocess, 'run', return_value=response) as run:
        assert drift.query('repos/example/test/rules/branches/main', pages=True) == [{'type': 'deletion'}]
    assert run.call_args.args[0] == ['gh', 'api', 'repos/example/test/rules/branches/main', '--paginate', '--slurp']
    assert run.call_args.kwargs['timeout'] == 30


@pytest.mark.parametrize('response', [
    subprocess.CompletedProcess([], 1, '', 'PRIVATE-ERROR-BODY'),
    subprocess.CompletedProcess([], 0, 'not json', ''),
    subprocess.CompletedProcess([], 0, '[{}]', ''),
])
def test_query_failure_is_explicit_and_does_not_echo_body(response):
    with patch.object(drift.subprocess, 'run', return_value=response):
        with pytest.raises(drift.EvidenceError) as exc:
            drift.query('fixture', pages=True)
    assert 'PRIVATE-ERROR-BODY' not in str(exc.value)


def test_offline_cli_exit_codes(evidence, tmp_path, capsys):
    paths = [tmp_path / name for name in ('manifest.json', 'legacy.json', 'rules.json')]
    for path, value in zip(paths, evidence):
        path.write_text(json.dumps(value))
    argv = ['--manifest', str(paths[0]), '--legacy-json', str(paths[1]), '--rules-json', str(paths[2])]
    assert drift.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'ok'
    evidence[1]['strict'] = False
    paths[1].write_text(json.dumps(evidence[1]))
    assert drift.main(argv) == 1
    capsys.readouterr()
    paths[2].write_text('{}')
    assert drift.main(argv) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'error'


def test_bridge_matrix_covers_python_314():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_text()
    import re
    matrix = re.search(r'python-version: \[(.*?)\]', workflow)
    assert matrix and '"3.14"' in matrix.group(1)
