# -*- coding: utf-8 -*-
import json
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged

from ..models.sql_fingerprint import sql_fingerprint

URL = '/perf_monitor/api/v1/push_slow_calls'


@tagged('post_install', '-at_install')
class TestSlowCallsEndpoint(HttpCase):
    """POST /perf_monitor/api/v1/push_slow_calls (perf_monitor_agent -> central)."""

    def setUp(self):
        super().setUp()
        self.server = self.env['perf.server'].create({'name': 'agent-test', 'host': 'agent:h', 'db_name': 'src'})
        self.token = self.server._set_new_token()
        self.env['ir.config_parameter'].set_param('perf_monitor.max_pushes_per_minute', 0)
        self.authenticate(None, None)
        self.Remote = self.env['perf.agent.slow.call.remote']

    def _push(self, body, token=None):
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        return self.url_open(URL, data=data, headers={'Content-Type': 'application/json',
                                                      'X-Perf-Token': token or self.token})

    def _call(self, agent_id, **kw):
        ts = fields.Datetime.to_string(fields.Datetime.now() - timedelta(minutes=agent_id))
        return dict({
            'id': agent_id, 'ts': ts, 'duration_ms': 2500.5, 'kind': 'rpc', 'uid': 2, 'login': 'admin',
            'model_name': 'sale.order', 'method': 'action_confirm', 'route': '/web/dataset/call_button',
            'cron_name': '', 'app_name': 'odoo|src|2|admin|sale.order.action_confirm|/web/dataset/call_button',
            'sql_count': 120, 'sql_ms': 1800.0, 'error': '', 'top_sql_ms': 1500.0, 'top_sql_calls': 100,
            'top_sql': "SELECT id FROM stock_move WHERE picking_id = %s AND name = 'x'",
            'top_sql_fingerprint': 'b' * 40, 'top_sql_callsite': 'stock/models/stock_move.py:10 (f)',
        }, **kw)

    def test_push_stores_tagged_by_source_db(self):
        resp = self._push({'version': 1, 'db_name': 'demo_db', 'calls': [self._call(1), self._call(2)]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {'ok': True, 'accepted': 2, 'rejected': 0, 'duplicates': 0})
        rows = self.Remote.search([('server_id', '=', self.server.id)], order='agent_id')
        self.assertEqual(rows.mapped('db_name'), ['demo_db'] * 2)
        row = rows[0]
        self.assertEqual((row.kind, row.login, row.model_name, row.method, row.sql_count, row.top_sql_calls),
                         ('rpc', 'admin', 'sale.order', 'action_confirm', 120, 100))
        self.assertEqual(row.top_sql_fingerprint, 'b' * 40)
        self.assertNotIn("'x'", row.top_sql)  # literals redacted again centrally
        self.assertTrue(row.app_name.startswith('odoo|src|2|admin|'))
        # re-sent batch (push answered but not marked on the agent side): duplicates, nothing stored twice
        resp = self._push({'version': 1, 'db_name': 'demo_db', 'calls': [self._call(1), self._call(3)]})
        self.assertEqual(resp.json(), {'ok': True, 'accepted': 1, 'rejected': 0, 'duplicates': 1})
        # same agent ids from another source DB are other rows
        resp = self._push({'version': 1, 'db_name': 'other_db', 'calls': [self._call(1)]})
        self.assertEqual(resp.json()['accepted'], 1)
        self.assertEqual(self.Remote.search_count([('server_id', '=', self.server.id)]), 4)

    def test_ui_context_stored_and_summarized(self):
        ui = {'ui_dashboard': 'BOD', 'ui_dashboard_model': 'dashboards.saas', 'ui_dashboard_id': 9,
              'ui_action': 'wad BOD Action', 'ui_action_id': 768, 'ui_menu': 'Dashboards/BOD', 'ui_menu_id': 5,
              'ui_model': 'dashboards.saas', 'ui_record_id': None, 'ui_view_type': ''}
        calls = [self._call(1, method='get_analytics_payload', model_name='dashboards.saas', duration_ms=3210.0, **ui),
                 self._call(2),  # agent 16.0.1.0.0: no ui_* keys at all
                 self._call(3, ui_menu_id=-4),  # out-of-range id: row refused
                 self._call(4, ui_dashboard='x' * 1000, ui_action_id='12')]
        resp = self._push({'version': 1, 'db_name': 'demo_db', 'calls': calls})
        self.assertEqual(resp.json(), {'ok': True, 'accepted': 3, 'rejected': 1, 'duplicates': 0})
        row = self.Remote.search([('server_id', '=', self.server.id), ('agent_id', '=', 1)])
        self.assertEqual((row.ui_dashboard, row.ui_dashboard_model, row.ui_dashboard_id, row.ui_action,
                          row.ui_action_id, row.ui_menu, row.ui_menu_id, row.ui_model, row.ui_record_id),
                         ('BOD', 'dashboards.saas', 9, 'wad BOD Action', 768, 'Dashboards/BOD', 5,
                          'dashboards.saas', 0))
        when = fields.Datetime.context_timestamp(row, row.ts).strftime('%Y-%m-%d %H:%M')
        self.assertEqual(row.ui_summary, "Dashboard 'BOD' (menu Dashboards/BOD) by user admin at %s took 3.2 s "
                                         "(dashboards.saas.get_analytics_payload) [demo_db]" % when)
        # the summary is in the reader's time zone
        riyadh = row.with_context(tz='Asia/Riyadh').ui_summary
        self.assertIn((row.ts + timedelta(hours=3)).strftime('%H:%M'), riyadh)
        old = self.Remote.search([('server_id', '=', self.server.id), ('agent_id', '=', 2)])
        self.assertFalse(old.ui_dashboard or old.ui_action or old.ui_menu_id)
        self.assertEqual(old.ui_summary.split(' at ')[0], "sale.order.action_confirm called by user admin")
        big = self.Remote.search([('server_id', '=', self.server.id), ('agent_id', '=', 4)])
        self.assertEqual((len(big.ui_dashboard), big.ui_action_id), (256, 12))
        self.assertEqual(self.Remote.search([('server_id', '=', self.server.id), ('ui_dashboard', '!=', False)]),
                         row | big)

    def test_tiles_stored_linked_and_summarized(self):
        ui = {'ui_dashboard': 'Sales', 'ui_dashboard_model': 'dashboards.saas', 'ui_dashboard_id': 3,
              'ui_menu': 'Sales/Dashboards', 'ui_menu_id': 5, 'ui_action': 'wad Sales Action', 'ui_action_id': 70,
              'user_name': 'Ali Hassan', 'login': 'ali', 'model_name': 'dashboards.saas',
              'method': 'get_all_datasets_saas'}
        call = self._call(10, duration_ms=4200.0, ui_tile='Top Products', ui_tile_id=41, ui_tile_type='table',
                          tile_ms=3900.0, tile_count=2, **ui)
        tiles = [self._call(11, kind='tile', duration_ms=3900.0, ui_tile='Top Products', ui_tile_id=41,
                            ui_tile_type='table', tile_ms=3900.0, parent_id=10, **ui),
                 self._call(12, kind='tile', duration_ms=200.0, ui_tile='Revenue', ui_tile_id=42,
                            ui_tile_type='tile', tile_ms=200.0, parent_id=10, **ui)]
        # tiles pushed in the same batch as their call, and in a later one
        resp = self._push({'version': 1, 'db_name': 'demo_db', 'calls': [call, tiles[0]]})
        self.assertEqual(resp.json(), {'ok': True, 'accepted': 2, 'rejected': 0, 'duplicates': 0})
        resp = self._push({'version': 1, 'db_name': 'demo_db',
                           'calls': [tiles[1], self._call(13, kind='tile', parent_id=-1),
                                     self._call(14, kind='tile', parent_id=999)]})
        self.assertEqual(resp.json(), {'ok': True, 'accepted': 2, 'rejected': 1, 'duplicates': 0})
        row = self.Remote.search([('server_id', '=', self.server.id), ('agent_id', '=', 10)])
        self.assertEqual((row.kind, row.ui_tile, row.ui_tile_id, row.ui_tile_type, row.tile_ms, row.tile_count,
                          row.user_name, row.parent_id.id),
                         ('rpc', 'Top Products', 41, 'table', 3900.0, 2, 'Ali Hassan', False))
        self.assertEqual(row.tile_ids.sorted('agent_id').mapped('ui_tile'), ['Top Products', 'Revenue'])
        self.assertEqual(set(row.tile_ids.mapped('kind')), {'tile'})
        orphan = self.Remote.search([('server_id', '=', self.server.id), ('agent_id', '=', 14)])
        self.assertEqual((orphan.parent_agent_id, orphan.parent_id.id), (999, False))
        when = fields.Datetime.context_timestamp(row, row.ts).strftime('%Y-%m-%d %H:%M')
        self.assertEqual(row.ui_summary, "Dashboard 'Sales' > Tile 'Top Products' (menu Sales/Dashboards) by user ali "
                                         "at %s took 4.2 s (dashboards.saas.get_all_datasets_saas) [demo_db]"
                         % when)
        groups = self.Remote.read_group([('server_id', '=', self.server.id), ('kind', '=', 'tile')],
                                        ['duration_ms:max'], ['ui_tile'])
        self.assertEqual({g['ui_tile'] for g in groups}, {'Top Products', 'Revenue', False})
        # the call row is deleted with its tiles
        row.unlink()
        self.assertEqual(self.Remote.search_count([('server_id', '=', self.server.id), ('agent_id', 'in', [11, 12])]), 0)

    def test_auth_and_payload_validation(self):
        body = {'version': 1, 'db_name': 'd', 'calls': [self._call(1)]}
        self.assertEqual(self._push(body, token='x' * 43).status_code, 401)
        self.assertEqual(self.url_open(URL, data=json.dumps(body),
                                       headers={'Content-Type': 'application/json'}).status_code, 401)
        for bad in (b'{nope', {'version': 2, 'calls': []}, {'version': 1, 'calls': 'x'},
                    {'version': 1, 'calls': [self._call(i) for i in range(501)]}):
            resp = self._push(bad)
            self.assertEqual(resp.status_code, 400, bad if not isinstance(bad, dict) else list(bad))
            self.assertFalse(resp.json()['ok'])
        self.assertFalse(self.Remote.search([('server_id', '=', self.server.id)]))

    def test_bad_rows_rejected_individually(self):
        old = fields.Datetime.to_string(fields.Datetime.now() - timedelta(days=30))
        calls = [self._call(1), self._call(2, ts=old), self._call(3, id=None), self._call(4, uid=1e15),
                 self._call(5, kind='weird', top_sql_fingerprint='not-a-sha1', login='x' * 500)]
        resp = self._push({'version': 1, 'db_name': 'd', 'calls': calls})
        self.assertEqual(resp.json(), {'ok': True, 'accepted': 2, 'rejected': 3, 'duplicates': 0})
        row = self.Remote.search([('agent_id', '=', 5)])
        self.assertEqual(row.kind, 'http')
        self.assertEqual(len(row.login), 128)
        self.assertEqual(row.top_sql_fingerprint, sql_fingerprint(row.top_sql))

    def test_real_callsite_found_before_clipping(self):
        frames = ' <- '.join(['/opt/odoo/custom/addons/wags_logger/models/wags_logger.py:525 (_patched_cursor_execute)',
                              '/opt/odoo/odoo/odoo/sql_db.py:321 (execute)',
                              '/opt/odoo/odoo/odoo/models.py:4997 (_search)',
                              '/opt/odoo/odoo/odoo/models.py:1650 (search)',
                              '/opt/odoo/custom/addons/closing_wags/models/closing.py:88 (get_closing_date)'])
        self.assertGreater(frames.index('closing_wags'), 256)
        resp = self._push({'version': 1, 'db_name': 'd', 'calls': [self._call(1, top_sql_callsite=frames),
                                                                    self._call(2, top_sql_callsite=None)]})
        self.assertEqual(resp.json()['accepted'], 2)
        rows = self.Remote.search([('server_id', '=', self.server.id)], order='agent_id')
        self.assertEqual(rows[0].top_sql_callsite,
                         '/opt/odoo/custom/addons/closing_wags/models/closing.py:88 (get_closing_date)')
        self.assertFalse(rows[1].top_sql_callsite)

    def test_throttle(self):
        self.env['ir.config_parameter'].set_param('perf_monitor.max_pushes_per_minute', 1)
        body = {'version': 1, 'db_name': 'd', 'calls': []}
        self.assertEqual(self._push(body).status_code, 200)
        resp = self._push(body)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.headers.get('Retry-After'), '60')

    def test_purge(self):
        self.Remote.create({'server_id': self.server.id, 'agent_id': 1, 'kind': 'http',
                            'ts': fields.Datetime.now() - timedelta(days=20)})
        keep = self.Remote.create({'server_id': self.server.id, 'agent_id': 2, 'kind': 'http',
                                   'ts': fields.Datetime.now()})
        self.Remote._cron_purge()
        self.assertEqual(self.Remote.search([('server_id', '=', self.server.id)]), keep)
