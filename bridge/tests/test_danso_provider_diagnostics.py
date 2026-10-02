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

    def test_current_native_schema_preserves_diagnostics(self):
        for reason, status in [('http_status', 429), ('http_status', 401),
                               ('http_status', 503), ('invalid_json', None)]:
            record = {'version': 1, 'reason': reason, 'http_status': status,
                      'output_tokens_max': None}
            event = self.failure('DANSO_PROVIDER=' + json.dumps(record))
            self.assertIn('reason=' + reason, event.message)
            if status is not None:
                self.assertIn('http_status=' + str(status), event.message)

    def test_output_cap_is_bounded_and_reason_specific(self):
        valid = {'version': 1, 'reason': 'max_tokens', 'http_status': None,
                 'output_tokens_max': 8192}
        event = self.failure('DANSO_PROVIDER=' + json.dumps(valid))
        self.assertIn('reason=max_tokens, output_tokens_max=8192', event.message)
        malformed = [{**valid, 'output_tokens_max': cap}
                     for cap in (None, True, 0, -1, 2**32, 'PRIVATE', [])]
        malformed += [{**valid, 'http_status': 429},
                      {**valid, 'reason': 'invalid_json'},
                      {**valid, 'extra': 'PRIVATE'},
                      {k: v for k, v in valid.items() if k != 'output_tokens_max'}]
        for record in malformed:
            with self.subTest(record=record):
                message = self.failure('DANSO_PROVIDER=' + json.dumps(record)).message
                self.assertNotIn('reason=', message)
                self.assertNotIn('PRIVATE', message)

    def test_zai_http_extension_requires_matching_primary(self):
        primary = 'DANSO_PROVIDER=' + json.dumps({
            'version': 1, 'reason': 'http_status', 'http_status': 429,
            'output_tokens_max': None})
        valid = {'version': 1, 'provider': 'zai', 'http_status': 429,
                 'provider_code': 1305, 'retry_after_seconds': 120}
        def message(value, prefix=primary):
            return self.failure(prefix + '\nDANSO_HTTP=' + json.dumps(value)).message
        self.assertIn('zai_code=1305, retry_after_seconds=120', message(valid))
        self.assertNotIn('zai_code=', message(valid, ''))
        bad = [{**valid, 'version': True}, {**valid, 'provider': 'PRIVATE'},
               {**valid, 'http_status': 503}, {**valid, 'provider_code': True},
               {**valid, 'provider_code': '1305'}, {**valid, 'provider_code': 9999},
               {**valid, 'provider_code': 1312}, {**valid, 'retry_after_seconds': -1},
               {**valid, 'retry_after_seconds': 86401}, {**valid, 'retry_after_seconds': True},
               {**valid, 'extra': 'PRIVATE'}]
        for value in bad:
            with self.subTest(value=value):
                result = message(value)
                self.assertIn('http_status=429', result)
                self.assertNotIn('zai_code=', result)
                self.assertNotIn('zai_hint=', result)
                self.assertNotIn('PRIVATE', result)
        duplicate = primary + '\n' + '\n'.join(['DANSO_HTTP=' + json.dumps(valid)] * 2)
        self.assertNotIn('zai_code=', self.failure(duplicate).message)
        for delay in (None, 0, 86400):
            self.assertIn('zai_code=1305', message({**valid, 'retry_after_seconds': delay}))

    def test_zai_quota_code_groups_render_user_hints(self):
        primary = 'DANSO_PROVIDER=' + json.dumps({
            'version': 1, 'reason': 'http_status', 'http_status': 429,
            'output_tokens_max': None})

        def message(code):
            http = {'version': 1, 'provider': 'zai', 'http_status': 429,
                    'provider_code': code, 'retry_after_seconds': None}
            return self.failure(primary + '\nDANSO_HTTP=' + json.dumps(http)).message

        window = {1308: 'usage window limit reached',
                  1310: 'weekly/monthly limit reached',
                  1316: '5-hour window limit reached', 1318: '5-hour window limit reached',
                  1320: '5-hour window limit reached',
                  1317: '7-day window limit reached', 1319: '7-day window limit reached',
                  1321: '7-day window limit reached'}
        account = {1113: 'recharge or buy a resource package',
                   1309: 'renew the expired subscription',
                   1311: 'use a model the plan covers',
                   1313: 'submit the fair-usage restore request',
                   1314: 'contact the enterprise administrator',
                   1315: 'use a key that matches the product type'}
        for code, phrase in window.items():
            with self.subTest(code=code):
                result = message(code)
                self.assertIn(f'zai_code={code}, zai_hint={phrase}', result)
                self.assertIn('wait', result)
        for code, phrase in account.items():
            with self.subTest(code=code):
                result = message(code)
                self.assertIn(f'zai_code={code}, zai_hint=account action required: {phrase}',
                              result)
                self.assertIn('waiting will not clear it', result)
        for code in (None, 1302, 1305):
            with self.subTest(code=code):
                result = message(code)
                self.assertNotIn('zai_hint=', result)
                self.assertIn('No automatic replay.', result)
