"""Strict body-free native provider diagnostic contract."""
import json
import unittest

from telegram_bot.core.danso_worker import _failure


class ProviderDiagnostics(unittest.TestCase):
    def failure(self, record, category='provider', code=3):
        base = 'DANSO_ERROR=' + json.dumps({
            'version': 1, 'category': category, 'exit_code': code}) + '\n'
        return _failure((base + record + '\nPRIVATE_BODY https://private.invalid').encode(), code)

    def test_valid_closed_reasons_and_http_status(self):
        for reason in ('invalid_json', 'response_too_large', 'stream_ended',
                       'invalid_stream', 'unsupported_stream_event', 'response_failed',
                       'response_incomplete', 'response_error'):
            record = 'DANSO_PROVIDER=' + json.dumps({
                'version': 1, 'reason': reason, 'http_status': None})
            event = self.failure(record)
            self.assertIn('reason=' + reason, event.message)
            self.assertNotIn('http_status=', event.message)
            self.assertNotIn('PRIVATE', event.message)
        for status in (101, 302, 401, 403, 429, 500, 999):
            event = self.failure('DANSO_PROVIDER=' + json.dumps({
                'version': 1, 'reason': 'http_status', 'http_status': status}))
            self.assertIn(f'reason=http_status, http_status={status}', event.message)
            self.assertIn('No automatic replay.', event.message)

    def test_malformed_records_and_wrong_categories_do_not_leak(self):
        valid = {'version': 1, 'reason': 'http_status', 'http_status': 429}
        malformed = [None, [], {}, {**valid, 'version': True},
                     {**valid, 'reason': []}, {**valid, 'reason': 'PRIVATE'},
                     {**valid, 'extra': 'PRIVATE'}, {**valid, 'http_status': True},
                     {**valid, 'http_status': 'PRIVATE'}, {**valid, 'http_status': 99},
                     {**valid, 'http_status': 1000}, {**valid, 'http_status': 200},
                     {**valid, 'http_status': 299}, {**valid, 'http_status': None},
                     {**valid, 'reason': 'invalid_stream'}]
        records = ['DANSO_PROVIDER=' + json.dumps(value) for value in malformed]
        line = 'DANSO_PROVIDER=' + json.dumps(valid)
        records += [line + '\n' + line, 'DANSO_PROVIDER={',
                    'DANSO_PROVIDER={"version":1,"reason":"http_status","http_status":401,"http_status":429}']
        for record in records:
            with self.subTest(record=record):
                event = self.failure(record)
                self.assertEqual(event.code, 'danso_provider')
                self.assertNotIn('reason=', event.message)
                self.assertNotIn('PRIVATE', event.message)
        for category, code in [('provider', 2), ('provider_timeout', 3), ('session', 3),
                               ('run_timeout', 124), ('UNKNOWN', 3)]:
            self.assertNotIn('reason=', self.failure(line, category, code).message)
        self.assertNotIn('reason=', self.failure('').message)
