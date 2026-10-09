# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

from odoo.exceptions import UserError, ValidationError

from ..models.perf_common import CRON_PREFIX
from ..models.perf_server import hash_token, status_from_last_seen


@tagged('post_install', '-at_install')
class TestPerfMonitor(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Server = cls.env['perf.server']
        cls.server = cls.Server.create({'name': 'staging', 'host': '10.0.0.1', 'db_name': 'db1'})

    @staticmethod
    def _ts(minutes_ago=5):
        return fields.Datetime.to_string(fields.Datetime.now() - timedelta(minutes=minutes_ago))

    def _payload(self, **kw):
        ts, qs = self._ts(5), self._ts(5 + 12 / 60.0)
        payload = {
            'version': 1, 'collector_version': '1.0.0',
            'host': {'hostname': 'ip-10-0-0-1', 'instance_type': 'm7g.xlarge', 'instance_id': 'i-123',
                     'region': 'eu-west-1', 'az': 'eu-west-1a', 'vcpu': 4, 'ram_mb': 16000,
                     'pg_version': 'PostgreSQL 15.4'},
            'metrics': [{'ts': ts, 'cpu_pct': 12.5, 'ram_pct': 40.0, 'disk_pct': 55.0,
                         'pg_connections': 12, 'db_latency_ms': 1.2}],
            'sql': [{'ts': ts, 'queryid': '-123', 'query': 'SELECT 1', 'calls': 5,
                     'total_ms': 3000, 'mean_ms': 600, 'reason': 'slow_mean'}],
            'live': [{'ts': ts, 'pid': 99, 'category': 'lock_wait', 'state': 'active',
                      'wait_event_type': 'Lock', 'duration_s': 12.0, 'blocking_pids': '42',
                      'query_start': qs, 'query': 'UPDATE x'}],
        }
        payload.update(kw)
        return payload

    def test_cron_prefix_and_active(self):
        crons = self.env['ir.model.data'].search(
            [('module', '=', 'perf_monitor'), ('model', '=', 'ir.cron')])
        self.assertGreaterEqual(len(crons), 4)
        for xmlid in crons:
            cron = self.env['ir.cron'].browse(xmlid.res_id)
            self.assertTrue(cron.name.startswith(CRON_PREFIX), cron.name)
            self.assertTrue(cron.active, cron.name)
        self.assertEqual(CRON_PREFIX, 'Perf Monitor: ')

    def test_status_thresholds(self):
        now = datetime(2026, 9, 25, 12, 0, 0)
        self.assertEqual(status_from_last_seen(False, now), 'unknown')
        self.assertEqual(status_from_last_seen(now - timedelta(minutes=4), now), 'green')
        self.assertEqual(status_from_last_seen(now - timedelta(minutes=7), now), 'yellow')
        self.assertEqual(status_from_last_seen(now - timedelta(minutes=11), now), 'red')

    def test_token_only_hash_stored(self):
        token = self.server._set_new_token()
        self.assertEqual(self.server.token_hash, hash_token(token))
        self.assertEqual(self.server.token_hint, token[-4:])
        self.assertNotIn(token, str(self.server.read()[0]))
        self.assertEqual(self.Server._find_by_token(token), self.server)
        self.assertFalse(self.Server._find_by_token(token + 'x'))
        self.server.action_revoke_token()
        self.assertFalse(self.Server._find_by_token(token))

    def test_wizard_creates_server_and_command(self):
        wizard = self.env['perf.server.add.wizard'].create({
            'name': 'live-1', 'environment': 'live', 'host': '10.0.0.9', 'db_name': 'prod',
            'pg_host': 'db.example', 'central_url': 'https://perf.example.com/'})
        action = wizard.action_confirm()
        self.assertEqual(self.env['ir.config_parameter'].get_param('perf_monitor.central_url'),
                         'https://perf.example.com')
        self.assertEqual(action['res_id'], wizard.id)
        server = wizard.server_id
        self.assertEqual(server.environment, 'live')
        cmd = wizard.install_command
        self.assertIn('/perf_monitor/collector/install.sh?db=', cmd)
        self.assertIn("'X-Perf-Token: ", cmd)
        self.assertIn('PERF_PG_DSN=', cmd)
        self.assertIn('user=perf_monitor_ro password=CHANGE_ME sslmode=require', cmd)
        self.assertEqual(cmd.count("curl --proto '=https' --tlsv1.2 "), 2)
        self.assertNotIn(' -L', cmd.replace('-fsS', ''))  # no redirect following with the token header
        self.assertNotIn('-fsSL', cmd)
        token = cmd.split('X-Perf-Token: ')[1].split("'")[0]
        self.assertEqual(self.Server._find_by_token(token), server)
        wizard.action_close()
        self.assertFalse(wizard.install_command)
        # regenerate revokes the old token
        server.action_regenerate_token()
        self.assertFalse(self.Server._find_by_token(token))

    def test_central_url_https_only(self):
        vals = {'name': 'x', 'environment': 'live', 'host': 'h', 'db_name': 'd'}
        Wizard = self.env['perf.server.add.wizard']
        for url in ('http://perf.example.com', 'http://10.0.0.1:8069', 'ftp://x', 'https://a@b'):
            with self.assertRaises(ValidationError):
                Wizard.create(dict(vals, central_url=url))
        for url in ('https://perf.example.com', 'http://127.0.0.1:8069', 'http://localhost', 'http://[::1]:8069'):
            Wizard.create(dict(vals, central_url=url))
        local = self.Server._build_install_command('http://127.0.0.1:8069', 't' * 43)
        self.assertNotIn('--proto', local)
        with self.assertRaises(UserError):
            self.Server._build_install_command('http://perf.example.com', 't' * 43)
        self.env['ir.config_parameter'].set_param('perf_monitor.central_url', 'http://evil.example')
        self.env['ir.config_parameter'].set_param('web.base.url', 'https://base.example')
        self.assertEqual(Wizard._default_central_url(), 'https://base.example')

    def test_wizard_central_url_param_not_downgraded(self):
        # N4: a loopback http URL never replaces an https central URL (install.sh of remote servers)
        params = self.env['ir.config_parameter']
        vals = {'name': 'x', 'environment': 'live', 'host': 'h', 'db_name': 'd'}
        Wizard = self.env['perf.server.add.wizard']
        params.set_param('perf_monitor.central_url', False)
        Wizard.create(dict(vals, central_url='http://127.0.0.1:8069')).action_confirm()
        self.assertEqual(params.get_param('perf_monitor.central_url'), 'http://127.0.0.1:8069')
        Wizard.create(dict(vals, name='x2', host='h2', central_url='https://perf.example.com/')).action_confirm()
        self.assertEqual(params.get_param('perf_monitor.central_url'), 'https://perf.example.com')
        wizard = Wizard.create(dict(vals, name='x3', host='h3', central_url='http://localhost:8069'))
        wizard.action_confirm()
        self.assertEqual(params.get_param('perf_monitor.central_url'), 'https://perf.example.com')
        self.assertIn('http://localhost:8069/perf_monitor/collector/install.sh', wizard.install_command)
        Wizard.create(dict(vals, name='x4', host='h4', central_url='https://other.example.com')).action_confirm()
        self.assertEqual(params.get_param('perf_monitor.central_url'), 'https://other.example.com')

    def test_ingest_payload_and_dedup(self):
        accepted = self.server._ingest_payload(self._payload())
        self.assertEqual(accepted, {'accepted': 3, 'rejected': 0})
        self.assertEqual(self.server.instance_type, 'm7g.xlarge')
        self.assertEqual(self.server.vcpu, 4)
        self.assertEqual(self.server.cpu_pct, 12.5)
        self.assertEqual(self.server.status, 'green')
        self.assertEqual(self.server.collector_version, '1.0.0')
        live = self.env['perf.live.query'].search([('server_id', '=', self.server.id)])
        self.assertEqual(live.category, 'lock_wait')
        # re-sent spooled payload: nothing duplicated
        self.assertEqual(self.server._ingest_payload(self._payload()), {'accepted': 0, 'rejected': 0})
        with self.assertRaises(ValueError):
            self.server._ingest_payload({'version': 2})
        with self.assertRaises(ValueError):
            self.server._ingest_payload(self._payload(metrics='nope'))

    def test_ingest_pgss_status(self):
        # old agent / collector: no "pgss" key -> ingested as before, status unknown
        self.assertEqual(self.server.pgss_status, 'unknown')
        self.assertEqual(self.server._ingest_payload(self._payload())['rejected'], 0)
        self.assertEqual((self.server.pgss_status, self.server.pgss_checked_at), ('unknown', False))
        for status in ('missing', 'unreadable', 'ok'):
            self.server._ingest_payload(self._payload(metrics=[], sql=[], live=[], pgss=status))
            self.assertEqual(self.server.pgss_status, status)
            self.assertTrue(self.server.pgss_checked_at)
        self.assertEqual(dict(self.server._fields['pgss_status']._description_selection(self.env))['missing'],
                         'Top SQL OFF - pg_stat_statements missing')
        # garbage value: not an error, unknown
        self.assertEqual(self.server._ingest_payload(self._payload(metrics=[], sql=[], live=[], pgss=['x']))
                         ['accepted'], 0)
        self.assertEqual(self.server.pgss_status, 'unknown')
        # the HTTP form view shows it
        arch = self.server.get_views([(False, 'form')])['views']['form']['arch']
        self.assertIn('pgss_status', arch)

    def test_ingest_redacts_query_text(self):
        payload = self._payload()
        payload['sql'][0]['query'] = "ALTER ROLE perf_monitor_ro PASSWORD 'hunter2'"
        payload['live'][0]['query'] = ("SELECT id FROM res_partner WHERE name ilike '%acme%' AND id IN (1, 2, 3) "
                                       "AND credit > 1500.5")
        self.server._ingest_payload(payload)
        sql = self.env['perf.sql.snapshot'].search([('server_id', '=', self.server.id)])
        live = self.env['perf.live.query'].search([('server_id', '=', self.server.id)])
        self.assertEqual(sql.query, 'ALTER ROLE perf_monitor_ro PASSWORD ?')
        self.assertEqual(live.query, 'SELECT id FROM res_partner WHERE name ilike ? AND id IN (?) AND credit > ?')

    def test_ingest_typing(self):
        payload = self._payload()
        payload['metrics'][0].update(cpu_pct=float('nan'), ram_pct=float('inf'))
        payload['sql'][0].update(calls=1e12, shared_blks_hit=5e12, rows=float('inf'), temp_blks_read=-3)
        self.assertEqual(self.server._ingest_payload(payload)['accepted'], 3)
        metric = self.server.latest_metric_id
        self.assertFalse(metric.cpu_pct)
        self.assertFalse(metric.ram_pct)
        self.assertEqual(metric.disk_pct, 55.0)
        sql = self.env['perf.sql.snapshot'].search([('server_id', '=', self.server.id)])
        self.assertEqual((sql.calls, sql.shared_blks_hit, sql.rows, sql.temp_blks_read), (1e12, 5e12, 0.0, 0.0))
        # host-level int out of range: whole payload refused
        for bad in ({'host': {'vcpu': float('nan')}}, {'host': {'ram_mb': 2 ** 40}}):
            with self.assertRaises(ValueError):
                self.server._ingest_payload(self._payload(**bad))
        # row-level int out of range (N9): only that row is refused, the rest is stored
        live_ok = dict(self._payload()['live'][0], pid=7, ts=self._ts(3))
        payload = self._payload(
            metrics=[dict(self._payload()['metrics'][0], ts=self._ts(2), pg_connections=1e12),
                     dict(self._payload()['metrics'][0], ts=self._ts(1))],
            sql=[],
            live=[dict(live_ok, pid=float('inf')), dict(live_ok, pid=2 ** 40, ts=self._ts(4)), live_ok])
        self.assertEqual(self.server._ingest_payload(payload), {'accepted': 2, 'rejected': 3})
        self.assertEqual(self.env['perf.live.query'].search_count(
            [('server_id', '=', self.server.id), ('pid', '=', 7)]), 1)

    def test_ingest_strings_and_timestamps(self):
        from ..models.perf_server import _to_dt, _to_str
        # N5: NUL characters would make PostgreSQL refuse the whole payload
        self.assertEqual(_to_str('a\x00b', 10), 'ab')
        payload = self._payload(host={'hostname': 'ip\x00-1', 'instance_type': 'm\x00x'})
        payload['live'][0]['query'] = "SELECT '\x00'"
        payload['live'][0]['usename'] = 'u\x00'
        self.assertEqual(self.server._ingest_payload(payload)['accepted'], 3)
        self.assertEqual((self.server.hostname, self.server.instance_type), ('ip-1', 'mx'))
        # N8: offsets / Z are converted to naive UTC
        expected = datetime(2026, 9, 25, 12, 31, 0)
        for value in ('2026-09-25T15:31:00+03:00', '2026-09-25 15:31:00.123456+03:00', '2026-09-25T12:31:00Z',
                      '2026-09-25 12:31:00', '2026-09-25T12:31:00.5', '2026-09-25T09:31:00-03:00'):
            self.assertEqual(_to_dt(value), expected, value)
        for value in ('garbage', '', None, 12, '2026-13-01 00:00:00', 'x' * 100):
            self.assertIsNone(_to_dt(value), value)
        ts = fields.Datetime.now().replace(microsecond=0) - timedelta(minutes=2)
        local = (ts + timedelta(hours=3)).isoformat() + '+03:00'
        self.server._ingest_payload(self._payload(metrics=[{'ts': local, 'cpu_pct': 3.0}], sql=[], live=[]))
        self.assertEqual(self.server.metric_ts, ts)

    def test_all_rejected_does_not_refresh_last_seen(self):
        # N7: a collector whose clock is off has every row rejected: the card must not turn green
        future = fields.Datetime.to_string(fields.Datetime.now() + timedelta(hours=2))
        self.assertFalse(self.server.last_seen)
        with self.assertLogs('odoo.addons.perf_monitor.models.perf_server', 'WARNING') as logs:
            result = self.server._ingest_payload(self._payload(
                metrics=[{'ts': future, 'cpu_pct': 1.0}], sql=[], live=[]))
        self.assertIn('last_seen not updated', logs.output[0])
        self.assertEqual(result, {'accepted': 0, 'rejected': 1})
        self.assertFalse(self.server.last_seen)
        self.assertEqual(self.server.status, 'unknown')
        self.assertEqual(self.server.hostname, 'ip-10-0-0-1')  # host info is still refreshed
        # an empty payload (nothing to send) is still a heartbeat
        self.server._ingest_payload({'version': 1})
        self.assertTrue(self.server.last_seen)
        self.assertEqual(self.server.status, 'green')

    def test_ingest_time_window(self):
        before = self.server.latest_metric_id
        future = fields.Datetime.to_string(fields.Datetime.now() + timedelta(minutes=30))
        too_old = fields.Datetime.to_string(fields.Datetime.now() - timedelta(days=36))
        live_old = fields.Datetime.to_string(fields.Datetime.now() - timedelta(days=4))  # live retention 3 d
        payload = self._payload(metrics=[{'ts': future, 'cpu_pct': 99.0}, {'ts': too_old, 'cpu_pct': 98.0},
                                         {'ts': '2099-01-01 00:00:00', 'cpu_pct': 97.0}, {'ts': 'garbage'}])
        payload['live'][0]['ts'] = live_old
        self.assertEqual(self.server._ingest_payload(payload), {'accepted': 1, 'rejected': 5})
        self.assertEqual(self.env['perf.metric'].search_count([('server_id', '=', self.server.id)]), 0)
        self.assertEqual(self.server.latest_metric_id, before)
        # a legacy future row already in the table never becomes the latest metric
        self.env['perf.metric'].create({'server_id': self.server.id, 'ts': '2099-01-01 00:00:00', 'cpu_pct': 1.0})
        self.server._ingest_payload(self._payload())
        self.assertEqual(self.server.cpu_pct, 12.5)
        self.assertLess(self.server.metric_ts, fields.Datetime.now() + timedelta(minutes=1))

    def test_rightsize_stub(self):
        self.server.action_compute_rightsize()
        self.assertEqual(self.server.rightsize_recommendation, 'insufficient data: 0/14 days')
        now = fields.Datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        vals = []
        for day in range(14):
            for i, cpu in enumerate((10.0, 20.0, 90.0)):
                vals.append({'server_id': self.server.id, 'ts': now - timedelta(days=day, minutes=i),
                             'cpu_pct': cpu, 'ram_pct': 50.0})
        self.env['perf.metric'].create(vals[:3 * 5])
        self.server.invalidate_recordset(['metric_days'])
        self.server.action_compute_rightsize()
        self.assertEqual(self.server.rightsize_recommendation, 'insufficient data: 5/14 days')
        self.env['perf.metric'].create(vals[3 * 5:])
        self.server.invalidate_recordset(['metric_days'])
        self.assertEqual(self.server.metric_days, 14)
        self.env['perf.server']._cron_refresh()
        text = self.server.rightsize_recommendation
        self.assertTrue(text.startswith('p95 CPU 90.0%, peak 90.0%, p95 RAM 50.0%, peak 50.0%'), text)
        self.assertIn('collector only, CloudWatch card not computed', text)

    def test_purge_batches(self):
        old = fields.Datetime.now() - timedelta(days=40)
        self.env['perf.metric'].create([{'server_id': self.server.id, 'ts': old - timedelta(minutes=i)}
                                        for i in range(5)])
        self.env['perf.metric'].create({'server_id': self.server.id, 'ts': fields.Datetime.now()})
        # N6: rows dated more than one day ahead never age out: purged too; 1 hour ahead is kept
        self.env['perf.metric'].create([{'server_id': self.server.id, 'ts': '2099-01-01 00:00:00'},
                                        {'server_id': self.server.id,
                                         'ts': fields.Datetime.now() + timedelta(hours=1)}])
        from ..models.perf_common import purge_older_than
        self.assertEqual(purge_older_than(self.env['perf.metric'], 35, batch=2), 6)
        self.assertEqual(self.env['perf.metric'].search_count([('server_id', '=', self.server.id)]), 2)
        self.env['perf.sql.snapshot']._cron_purge()
        self.env['perf.live.query']._cron_purge()

    def test_indexes_created(self):
        self.env.cr.execute("SELECT indexname FROM pg_indexes WHERE indexname IN "
                            "('perf_metric_server_ts_idx', 'perf_sql_snapshot_server_ts_idx', "
                            "'perf_live_query_server_ts_idx')")
        self.assertEqual(len(self.env.cr.fetchall()), 3)

    def test_deploy_register_local(self):
        token = self.Server._deploy_register_local('staging-local', 'staging-host', 'db-x')
        server = self.Server._find_by_token(token)
        self.assertEqual(server.host, 'staging-host')
        token2 = self.Server._deploy_register_local('staging-local', 'staging-host', 'db-x')
        self.assertEqual(self.Server._find_by_token(token2), server)
        self.assertFalse(self.Server._find_by_token(token))
