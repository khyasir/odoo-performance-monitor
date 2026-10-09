# -*- coding: utf-8 -*-
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import requests

from odoo.tests import TransactionCase, tagged

from ..models import perf_agent_collector as mod
from ..tools import collect
from .common import set_agent_params

TOKEN = 'Tok_0123456789abcdefghijklmnopqrstuvwxyzABCD'


def _resp(status=200, body=None):
    resp = MagicMock(status_code=status, text=json.dumps(body or {}))
    resp.json.return_value = body if body is not None else {}
    return resp


@tagged('post_install', '-at_install', 'perf_monitor_agent')
class TestAgentCollect(TransactionCase):
    """"Perf Monitor: agent collect": metrics / Top SQL / live queries pushed with the agent token."""

    def setUp(self):
        super().setUp()
        self.Collector = self.env['perf.agent.collector']
        self.Collector.search([]).unlink()
        set_agent_params(self.env, central_url='https://perf.example.com/', central_token=TOKEN, central_db='',
                         enabled='1', collect_enabled='1', send_host_metrics='1')
        # no EC2 metadata lookups in tests
        self.patch(collect, 'fetch_imds', lambda *a, **k: {})

    def _session(self, *responses):
        session = MagicMock()
        session.__enter__.return_value = session
        session.headers = {}
        session.post.side_effect = list(responses)
        return session

    def _run(self, *responses):
        session = self._session(*responses)
        with patch.object(mod.requests, 'Session', return_value=session):
            result = self.Collector._cron_collect_and_push()
        return result, session

    def test_cron_record(self):
        cron = self.env.ref('perf_monitor_agent.ir_cron_agent_collect')
        self.assertEqual(cron.name, 'Perf Monitor: agent collect')
        self.assertTrue(cron.active)
        self.assertEqual((cron.interval_number, cron.interval_type), (5, 'minutes'))
        self.assertEqual(cron.model_id.model, 'perf.agent.collector')
        self.assertIn('_cron_collect_and_push', cron.code)
        self.assertTrue(hasattr(self.Collector, '_cron_collect_and_push'))

    def test_noop_without_token(self):
        for params in ({'central_token': ''}, {'central_url': ''}, {'collect_enabled': '0'}, {'enabled': '0'},
                       {'central_url': 'http://perf.example.com'}):
            set_agent_params(self.env, central_url='https://perf.example.com', central_token=TOKEN,
                             collect_enabled='1', enabled='1')
            set_agent_params(self.env, **params)
            with self.subTest(params=params), patch.object(mod.requests, 'Session') as session_cls:
                self.assertEqual(self.Collector._cron_collect_and_push(), {})
            session_cls.assert_not_called()
        self.assertFalse(self.Collector.search([]), 'no state saved when skipped')

    def test_push_payload(self):
        result, session = self._run(_resp(200, {'ok': True, 'accepted': 1, 'rejected': 0}))
        self.assertEqual(result, {'accepted': 1, 'rejected': 0})
        url = session.post.call_args[0][0]
        kwargs = session.post.call_args[1]
        self.assertEqual(url, 'https://perf.example.com/perf_monitor/api/v1/push')
        self.assertEqual(kwargs['headers']['X-Perf-Token'], TOKEN)
        self.assertFalse(kwargs['allow_redirects'])
        body = json.loads(kwargs['data'])
        self.assertEqual((body['version'], body['db_name']), (1, self.env.cr.dbname))
        self.assertTrue(body['collector_version'].startswith('perf_monitor_agent/'))
        self.assertLessEqual(len(body['collector_version']), 32)
        metric = body['metrics'][0]
        self.assertIn('ts', metric)
        self.assertIn('db_latency_ms', metric)
        self.assertIn('pg_connections', metric)
        self.assertIn('ram_pct', metric)  # send_host_metrics on
        self.assertIn('hostname', body['host'])
        self.assertIn('PostgreSQL', body['host']['pg_version'])
        self.assertIsInstance(body['live'], list)
        self.assertEqual(body['sql'], [])  # first run: pg_stat_statements baseline only (or not installed)
        self.assertTrue(self.Collector.search([('key', '=', 'host')]), 'host counters saved after the push')
        session.get.assert_not_called()  # no central_db: no pinning

    def test_db_only_without_host_metrics(self):
        set_agent_params(self.env, send_host_metrics='0')
        _result, session = self._run(_resp(200, {'ok': True, 'accepted': 1}))
        body = json.loads(session.post.call_args[1]['data'])
        metric = body['metrics'][0]
        self.assertIn('db_latency_ms', metric)
        for key in ('cpu_pct', 'ram_pct', 'disk_pct', 'net_rx_kbps', 'load1'):
            self.assertNotIn(key, metric)
        self.assertNotIn('vcpu', body['host'])
        self.assertIn('live', body)
        self.assertIn('sql', body)

    def test_central_db_pinned(self):
        set_agent_params(self.env, central_db='central_db')
        _result, session = self._run(_resp(200, {'ok': True}))
        self.assertEqual(session.get.call_args[0][0], 'https://perf.example.com/web?db=central_db')
        self.assertEqual(session.post.call_args[0][0],
                         'https://perf.example.com/perf_monitor/api/v1/push?db=central_db')

    def test_failures_fail_soft(self):
        for failure in (requests.ConnectionError('down'), _resp(401, {'ok': False, 'error': 'invalid token'}),
                        _resp(429, {'ok': False}), _resp(200, {'ok': False})):
            with self.subTest(failure=failure), self.assertLogs(mod._logger.name, 'WARNING') as logs:
                result, _session = self._run(failure)
            self.assertEqual(result, {})
            self.assertNotIn(TOKEN, '\n'.join(logs.output))
        self.assertFalse(self.Collector.search([]), 'counters are kept for the next run after a failed push')

    def test_pg_stat_statements_missing(self):
        """Missing / unreadable: Top SQL skipped, reported as payload "pgss", ONE info line per state change
        (the cron runs every 5 minutes: no repeated WARNING in the Odoo log)."""
        th, Model = dict(collect.DEFAULT_THRESHOLDS), type(self.Collector)
        mod._PGSS_LOGGED.clear()
        with patch.object(Model, '_statements_meta', return_value=None), \
                patch.object(mod._logger, 'info') as info, patch.object(mod._logger, 'warning') as warning:
            for _run in range(3):
                status = {}
                self.assertEqual(self.Collector._top_sql(th, '2026-01-01 00:00:00', {}, status), [])
                self.assertEqual(status, {'pgss': 'missing'})
            payload = self.Collector._build_payload(False, {})
        self.assertEqual(payload['pgss'], 'missing')
        self.assertEqual(info.call_count, 1, 'logged once, not on every run')
        self.assertIn('pg_stat_statements', info.call_args[0][0])
        warning.assert_not_called()
        with patch.object(Model, '_statements_meta', side_effect=Exception('permission denied')), \
                patch.object(mod._logger, 'info') as info, patch.object(mod._logger, 'warning') as warning:
            for _run in range(2):
                status = {}
                self.assertEqual(self.Collector._top_sql(th, '2026-01-01 00:00:00', {}, status), [])
                self.assertEqual(status, {'pgss': 'unreadable'})
        self.assertEqual(info.call_count, 1, 'state change logged once')
        self.assertIn('permission denied', str(info.call_args))
        warning.assert_not_called()
        # back to ok: one line, then quiet
        meta = ('"public"', {'total': 'total_exec_time', 'max': 'max_exec_time'}, True)
        with patch.object(Model, '_statements_meta', return_value=meta), \
                patch.object(Model, '_statements', return_value={}), patch.object(mod._logger, 'info') as info:
            for _run in range(2):
                status = {}
                self.Collector._top_sql(th, '2026-01-01 00:00:00', {}, status)
                self.assertEqual(status, {'pgss': 'ok'})
        self.assertEqual(info.call_count, 1)
        # the cron transaction is still usable
        self.env.cr.execute('SELECT 1')

    def test_payload_reports_pgss(self):
        payload = self.Collector._build_payload(False, {})
        self.assertIn(payload['pgss'], ('ok', 'missing', 'unreadable'))
        self.env.cr.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'")
        if not self.env.cr.fetchone():
            self.assertEqual(payload['pgss'], 'missing')

    def test_failing_stat_query_does_not_break_transaction(self):
        with self.assertRaises(Exception):
            with self.Collector._stat_cursor() as cr:
                cr.execute('SELECT * FROM perf_monitor_agent_no_such_view')
        self.env.cr.execute('SHOW statement_timeout')
        self.assertNotEqual(self.env.cr.fetchone()[0], mod.STATEMENT_TIMEOUT)
        with self.Collector._stat_cursor() as cr:
            cr.execute('SHOW statement_timeout')
            self.assertEqual(cr.fetchone()[0], mod.STATEMENT_TIMEOUT)
        self.env.cr.execute('SHOW statement_timeout')
        self.assertNotEqual(self.env.cr.fetchone()[0], mod.STATEMENT_TIMEOUT)

    def test_top_sql_second_run_redacted(self):
        meta = ('"public"', {'total': 'total_exec_time', 'max': 'max_exec_time'}, True)
        runs = [
            {'1:10': {'queryid': '1', 'userid': 10, 'calls': 10, 'total_ms': 100.0, 'max_ms': 50}},
            {'1:10': {'queryid': '1', 'userid': 10, 'calls': 20, 'total_ms': 20100.0, 'max_ms': 3000}},
        ]
        th = dict(collect.DEFAULT_THRESHOLDS)
        Model = type(self.Collector)
        with patch.object(Model, '_statements_meta', return_value=meta), \
                patch.object(Model, '_statements', side_effect=runs), \
                patch.object(Model, '_statement_texts',
                             return_value={'1': "SELECT * FROM res_partner WHERE email = 'a@b.c' AND id = 7"}):
            states = {}
            self.assertEqual(self.Collector._top_sql(th, 'ts1', states), [])  # baseline
            self.Collector._set_state('sql', states['sql'])
            rows = self.Collector._top_sql(th, 'ts2', states)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['calls'], rows[0]['total_ms'], rows[0]['reason']), (10, 20000.0, 'slow_mean'))
        self.assertEqual(rows[0]['query'], 'SELECT * FROM res_partner WHERE email = ? AND id = ?')

    def test_live_queries_redacted(self):
        rows = self.Collector._live_queries(dict(collect.DEFAULT_THRESHOLDS, long_running_s=0), 'ts')
        for row in rows:
            self.assertIn(row['category'], ('lock_wait', 'idle_in_tx', 'long_running'))
            self.assertIsInstance(row['blocking_pids'], str)
        self.assertEqual(collect.redact_sql("UPDATE t SET name = 'secret' WHERE id = 5"),
                         'UPDATE t SET name = ? WHERE id = ?')

    def test_settings(self):
        self.env['res.config.settings'].create({
            'perf_agent_collect_enabled': False, 'perf_agent_send_host_metrics': False}).execute()
        params = self.env['ir.config_parameter'].sudo()
        self.assertEqual(params.get_param('perf_monitor_agent.collect_enabled'), '0')
        self.assertEqual(params.get_param('perf_monitor_agent.send_host_metrics'), '0')
        settings = self.env['res.config.settings'].create({})
        self.assertFalse(settings.perf_agent_collect_enabled or settings.perf_agent_send_host_metrics)
        params.set_param('perf_monitor_agent.collect_enabled', False)
        params.set_param('perf_monitor_agent.send_host_metrics', False)
        settings = self.env['res.config.settings'].create({})
        self.assertTrue(settings.perf_agent_collect_enabled and settings.perf_agent_send_host_metrics)  # default on
        help_text = self.env['res.config.settings']._fields['perf_agent_send_host_metrics'].help
        self.assertIn('ONLY ONE database per host', help_text)


@tagged('post_install', '-at_install', 'perf_monitor_agent')
class TestHostSampler(TransactionCase):
    """tools/collect.py: /proc parsing and rates between two cron runs (pure python)."""

    def _proc(self, stat, net):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, 'net'))
        files = {'stat': 'cpu  %s\ncpu0 1 1 1 1\n' % stat, 'net/dev': (
            'Inter-|   Receive\n face |bytes packets\n    lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0\n'
            '  eth0: %s 0 0 0 0 0 0 0 %s 0 0 0 0 0 0 0\n' % net),
            'meminfo': 'MemTotal: 2048000 kB\nMemAvailable: 1024000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n',
            'loadavg': '0.50 0.40 0.30 1/100 42\n'}
        for name, text in files.items():
            with open(os.path.join(root, name), 'w') as fh:
                fh.write(text)
        return root

    def test_rates_from_previous_state(self):
        clock = iter([1000.0, 1300.0]).__next__
        sleeps = []
        root = self._proc('100 0 100 700 100 0 0 0', ('1000', '2000'))
        sampler = collect.HostSampler(proc_root=root, use_psutil=False, clock=clock, sleep=sleeps.append)
        prev = sampler.counters()
        self.assertEqual(prev['cpu'], [1000, 700, 100])
        self.assertEqual(prev['net'], [1000, 2000])
        with open(os.path.join(root, 'stat'), 'w') as fh:
            fh.write('cpu  300 0 300 2100 300 0 0 0\n')  # +2000 jiffies: 400 busy, 200 iowait
        with open(os.path.join(root, 'net', 'dev'), 'w') as fh:
            fh.write('  eth0: 38500 0 0 0 0 0 0 0 77000 0 0 0 0 0 0 0\n')
        row, state = sampler.sample(prev)
        self.assertEqual(sleeps, [])  # previous counters usable: no short sample
        self.assertEqual((row['cpu_pct'], row['iowait_pct']), (20.0, 10.0))
        self.assertEqual((row['net_rx_kbps'], row['net_tx_kbps']), (1.0, 2.0))  # bytes over 300 s
        self.assertEqual(row['ram_pct'], 50.0)
        self.assertIn('load1', row)
        self.assertIn('disk_pct', row)
        self.assertEqual(state['t'], 1300.0)

    def test_short_sample_without_usable_state(self):
        clock = iter([5000.0, 5001.0]).__next__
        sleeps = []
        root = self._proc('100 0 100 700 100 0 0 0', ('1000', '2000'))
        sampler = collect.HostSampler(proc_root=root, use_psutil=False, clock=clock, sleep=sleeps.append)
        _row, state = sampler.sample({'t': 1.0, 'cpu': [1, 1, 1], 'net': [1, 1]})  # far too old
        self.assertEqual(sleeps, [collect.SHORT_SAMPLE_S])
        self.assertEqual(state['t'], 5001.0)
        self.assertEqual(collect.HostSampler.rates({'t': 1, 'cpu': [10, 5, 0]}, {'t': 2, 'cpu': [5, 1, 0]}), {})

    def test_compute_top_sql(self):
        th = dict(collect.DEFAULT_THRESHOLDS)
        prev = {'1:1': [10, 100.0, 0, 0, 0, 0, 0]}
        cur = {'1:1': {'queryid': '1', 'calls': 20, 'total_ms': 10100.0, 'query': "SELECT 'x'"},
               '2:1': {'queryid': '2', 'calls': 5, 'total_ms': 5.0},  # new, fast: not selected
               '3:1': {'queryid': '3', 'calls': 2000, 'total_ms': 20.0}}  # new, many calls
        rows, state = collect.compute_top_sql(prev, cur, th, 'ts')
        self.assertEqual([r['queryid'] for r in rows], ['1', '3'])
        self.assertEqual(rows[0]['query'], 'SELECT ?')
        self.assertEqual(rows[1]['reason'], 'high_calls')
        self.assertEqual(sorted(state), ['1:1', '2:1', '3:1'])
        json.dumps(state)  # stored as JSON
