# -*- coding: utf-8 -*-
import json
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged

from ..controllers import main as controller


@tagged('post_install', '-at_install')
class TestPushController(HttpCase):

    def setUp(self):
        super().setUp()
        self.server = self.env['perf.server'].create({'name': 'http-test', 'host': 'h', 'db_name': 'd'})
        self.token = self.server._set_new_token()
        self.env['ir.config_parameter'].set_param('perf_monitor.max_pushes_per_minute', 0)  # off unless tested
        self.authenticate(None, None)  # pins the test DB in the session, like the collector's /web?db=

    def _push(self, body, token=None):
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        return self.url_open('/perf_monitor/api/v1/push', data=data,
                             headers={'Content-Type': 'application/json', 'X-Perf-Token': token or self.token})

    @staticmethod
    def _ts(**delta):
        return fields.Datetime.to_string(fields.Datetime.now() - timedelta(**(delta or {'minutes': 1})))

    def test_push(self):
        payload = {'version': 1, 'host': {'instance_type': 'unknown'},
                   'metrics': [{'ts': self._ts(), 'cpu_pct': 5.0}], 'sql': [], 'live': []}
        resp = self._push(payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'ok': True, 'accepted': 1, 'rejected': 0})
        self.assertTrue(self.server.last_seen)
        self.assertEqual(self._push(payload, token='x' * 43).status_code, 401)
        self.assertEqual(self._push(b'{not json').status_code, 400)
        self.assertEqual(self._push({'version': 1, 'metrics': [{}] * 6000}).status_code, 400)

    def test_push_bad_numbers_400_nothing_written(self):
        ok_metric = {'ts': self._ts(), 'cpu_pct': 5.0}
        bodies = [
            {'version': 1, 'metrics': [ok_metric], 'host': {'vcpu': 1e300}},
            {'version': 1, 'metrics': [ok_metric], 'host': {'ram_mb': -5}},
            '{"version": 1, "metrics": [%s], "host": {"vcpu": NaN}}' % json.dumps(ok_metric),
            '[' * 100000,  # deeply nested JSON: RecursionError -> 400, not 500 (N5)
        ]
        for body in bodies:
            resp = self._push(body)
            self.assertEqual(resp.status_code, 400, body)
            self.assertFalse(resp.json()['ok'])
        self.assertEqual(self.env['perf.metric'].search_count([('server_id', '=', self.server.id)]), 0)
        self.assertEqual(self.env['perf.live.query'].search_count([('server_id', '=', self.server.id)]), 0)
        self.server.invalidate_recordset()
        self.assertFalse(self.server.last_seen)
        # huge cumulative counters and non-finite floats are stored, not refused
        body = ('{"version": 1, "metrics": [{"ts": "%s", "cpu_pct": NaN, "ram_pct": Infinity}], '
                '"sql": [{"ts": "%s", "queryid": "7", "calls": 1e12, "shared_blks_hit": 4e12, "total_ms": 1e300}]}'
                % (ok_metric['ts'], ok_metric['ts']))
        resp = self._push(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {'ok': True, 'accepted': 2, 'rejected': 0})
        sql = self.env['perf.sql.snapshot'].search([('server_id', '=', self.server.id)])
        self.assertEqual((sql.calls, sql.shared_blks_hit), (1e12, 4e12))

    def test_push_bad_row_ints_skip_only_the_row(self):
        # N9: an out-of-range pid / pg_connections refuses that row only, the good rows are stored
        ts = [self._ts(minutes=m) for m in (1, 2, 3, 4)]
        body = ('{"version": 1, "metrics": [{"ts": "%s", "cpu_pct": 5.0}, {"ts": "%s", "pg_connections": 1e12}], '
                '"live": [{"ts": "%s", "pid": Infinity}, {"ts": "%s", "pid": NaN}, {"ts": "%s", "pid": 1e15}, '
                '{"ts": "%s", "pid": 12, "query": "SELECT 1"}]}' % (ts[0], ts[1], ts[0], ts[1], ts[2], ts[3]))
        resp = self._push(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {'ok': True, 'accepted': 2, 'rejected': 4})
        live = self.env['perf.live.query'].search([('server_id', '=', self.server.id)])
        self.assertEqual(live.pid, 12)

    def test_push_nul_in_strings(self):
        # N5: "\u0000" used to fail at the deferred flush, after the try block (HTTP 500)
        payload = {'version': 1, 'host': {'hostname': 'a\u0000b'}, 'collector_version': '1\u0000',
                   'metrics': [{'ts': self._ts(), 'cpu_pct': 1.0}]}
        resp = self._push(payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.server.invalidate_recordset()
        self.assertEqual(self.server.hostname, 'ab')

    def test_push_future_rejected(self):
        payload = {'version': 1, 'metrics': [{'ts': '2099-01-01 00:00:00', 'cpu_pct': 1.0},
                                             {'ts': self._ts(), 'cpu_pct': 2.0}]}
        resp = self._push(payload)
        self.assertEqual(resp.json(), {'ok': True, 'accepted': 1, 'rejected': 1})
        self.server.invalidate_recordset()
        self.assertEqual(self.server.cpu_pct, 2.0)
        self.assertLess(self.server.metric_ts, fields.Datetime.now() + timedelta(minutes=1))

    def test_push_throttle(self):
        self.env['ir.config_parameter'].set_param('perf_monitor.max_pushes_per_minute', 3)
        payload = {'version': 1, 'metrics': []}
        codes = [self._push(payload).status_code for _ in range(4)]
        if codes[-1] != 429:  # the minute rolled over between two pushes: one more window
            codes = [self._push(payload).status_code for _ in range(4)]
        self.assertEqual(codes[-1], 429, codes)
        self.assertIn(200, codes)
        # per server: another server is not throttled
        other = self.env['perf.server'].create({'name': 'other', 'host': 'h2', 'db_name': 'd'})
        self.assertEqual(self._push(payload, token=other._set_new_token()).status_code, 200)

    def test_throttle_window(self):
        key_now = 1000 * 60.0
        self.assertFalse(controller._throttled('db', -1, 2, now=key_now))
        self.assertFalse(controller._throttled('db', -1, 2, now=key_now + 1))
        self.assertTrue(controller._throttled('db', -1, 2, now=key_now + 2))
        self.assertFalse(controller._throttled('db', -1, 2, now=key_now + 60))  # next minute
        self.assertFalse(controller._throttled('db', -2, 0, now=key_now))      # 0 disables

    def test_install_script(self):
        params = self.env['ir.config_parameter']
        params.set_param('perf_monitor.central_url', 'https://perf.example.com')
        resp = self.url_open('/perf_monitor/collector/install.sh', headers={'X-Perf-Token': self.token})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("TOKEN='%s'" % self.token, resp.text)
        self.assertIn("CENTRAL_URL='https://perf.example.com'", resp.text)
        self.assertIn("--proto '=https' --tlsv1.2", resp.text)
        self.assertIn('[Service]', resp.text)
        self.assertNotIn('@@', resp.text)
        # a forged Host header never changes CENTRAL_URL
        resp = self.url_open('/perf_monitor/collector/install.sh',
                             headers={'X-Perf-Token': self.token, 'Host': 'attacker.example'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("CENTRAL_URL='https://perf.example.com'", resp.text)
        self.assertNotIn('attacker', resp.text)
        # a plain-http non-loopback URL is never rendered, and there is no web.base.url fallback
        # (Odoo rewrites web.base.url from the Host header of admin logins): 400 until it is set (N4)
        params.set_param('web.base.url', 'https://base.example')
        for value in ('http://perf.example.com', False):
            params.set_param('perf_monitor.central_url', value)
            resp = self.url_open('/perf_monitor/collector/install.sh', headers={'X-Perf-Token': self.token})
            self.assertEqual(resp.status_code, 400, value)
            self.assertIn('perf_monitor.central_url', resp.text)
            self.assertNotIn('base.example', resp.text)
        # token only in the header: ?token= is ignored (it would land in access logs)
        self.assertEqual(self.url_open('/perf_monitor/collector/install.sh?token=%s' % self.token).status_code, 401)
        self.assertEqual(self.url_open('/perf_monitor/collector/install.sh?token=bad').status_code, 401)
        self.assertEqual(self.url_open('/perf_monitor/collector/perf_collector.py?token=%s' % self.token).status_code,
                         401)
        resp = self.url_open('/perf_monitor/collector/perf_collector.py', headers={'X-Perf-Token': self.token})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('class Collector', resp.text)
        self.assertIn('def redact_sql', resp.text)
