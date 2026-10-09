# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo import api
from odoo.tests import HttpCase, tagged

from .. import runtime
from ..tools import ui_context
from .common import json_rpc, reset_limiter, set_agent_params


def _tile_compute(self, tile_id, sleep_ms):
    """Stands in for dataset.saas.get_tile_data: a tile's own SQL."""
    self.env.cr.execute('SELECT pg_sleep(%s)', [sleep_ms / 1000.0])
    self.env.cr.execute('SELECT count(*) FROM res_partner WHERE id = %s', [tile_id])
    return tile_id


@api.model
def _tiles_batch(self, dashboard_id, plan):
    """Stands in for dashboards.saas.get_all_datasets_saas: every tile of a dashboard in one RPC."""
    for tile_id, sleep_ms in plan:
        self.perf_test_tile_compute(tile_id, sleep_ms)
    return len(plan)


@tagged('post_install', '-at_install', 'perf_monitor_agent')
class TestAgentTiles(HttpCase):
    """Per-tile timing of a call computing many dashboard tiles. odoo_dashboards_saas is not a dependency:
    res.partner plays both the dashboard (dashboards.saas) and the tile (data.analytics.saas) models, and
    a test method on it the per-tile method (dataset.saas.get_tile_data)."""

    def setUp(self):
        super().setUp()
        reset_limiter()
        self.addCleanup(runtime.clear_config_cache)
        self.Call = self.env['perf.agent.slow.call']
        self.Call.search([]).unlink()
        set_agent_params(self.env, slow_ms=0)
        Partner = self.env['res.partner']
        self.dashboard = Partner.create({'name': 'Sales Dash'})
        self.fast, self.slow, self.mid = (Partner.create({'name': n}) for n in ('Tile Fast', 'Tile Slow', 'Tile Mid'))
        self.action = self.env['ir.actions.client'].create({
            'name': 'wad Sales Dash Action', 'tag': 'owl.sales_dashboard_od',
            'context': "{'context_dashboard_id': %d}" % self.dashboard.id})
        self.menu_app = self.env['ir.ui.menu'].create({'name': 'Sales App'})
        self.menu = self.env['ir.ui.menu'].create({'name': 'Sales Dash', 'parent_id': self.menu_app.id,
                                                   'action': 'ir.actions.client,%d' % self.action.id})
        cls = type(Partner)
        for name, func in (('perf_test_tile_compute', _tile_compute), ('perf_test_tiles_batch', _tiles_batch)):
            setattr(cls, name, func)
            self.addCleanup(delattr, cls, name)
        patches = [patch.object(runtime, 'TILE_METHODS', {'res.partner': {
                       'perf_test_tile_compute': ('id', 0, 'tile_id')}}),
                   patch.object(ui_context, 'TILE_MODEL', 'res.partner'),
                   patch.object(ui_context, 'SAAS_DASHBOARD_MODEL', 'res.partner')]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(runtime.install_tile_timers(self.env), 1)
        self.assertEqual(runtime.install_tile_timers(self.env), 0)  # idempotent (one wrapper)
        self.authenticate('admin', 'admin')

    def _batch(self, plan, header=None):
        header = header or 'a=%d&m=%d&dh=%d' % (self.action.id, self.menu_app.id, self.dashboard.id)
        return json_rpc(self, '/web/dataset/call_kw/res.partner/perf_test_tiles_batch', {
            'model': 'res.partner', 'method': 'perf_test_tiles_batch',
            'args': [self.dashboard.id, plan], 'kwargs': {}}, headers={ui_context.HEADER: header})

    def test_slow_batch_names_its_slowest_tile_and_times_each(self):
        plan = [(self.fast.id, 5), (self.slow.id, 250), (self.mid.id, 60)]
        self.assertEqual(self._batch(plan)['result'], 3)
        call = self.Call.search([('method', '=', 'perf_test_tiles_batch'), ('kind', '=', 'rpc')])
        self.assertEqual(len(call), 1)
        admin = self.env.ref('base.user_admin')
        self.assertEqual((call.ui_dashboard, call.ui_tile, call.ui_tile_id, call.tile_count),
                         ('Sales Dash', 'Tile Slow', self.slow.id, 3))
        self.assertEqual((call.ui_menu_id, call.ui_menu), (self.menu.id, 'Sales App/Sales Dash'))
        self.assertEqual((call.uid, call.login, call.user_name), (admin.id, 'admin', admin.name))
        self.assertGreaterEqual(call.tile_ms, 250)
        self.assertTrue(call.ui_summary.startswith(
            "Dashboard 'Sales Dash' > Tile 'Tile Slow' (menu Sales App/Sales Dash) by user admin at "),
            call.ui_summary)
        self.assertTrue(call.ui_summary.endswith('(res.partner.perf_test_tiles_batch)'), call.ui_summary)
        tiles = call.tile_ids.sorted('duration_ms', reverse=True)
        self.assertEqual(tiles.mapped('ui_tile'), ['Tile Slow', 'Tile Mid', 'Tile Fast'])
        self.assertEqual(set(tiles.mapped('kind')), {'tile'})
        self.assertEqual(set(tiles.mapped('ui_dashboard')), {'Sales Dash'})
        self.assertEqual(set(tiles.mapped('login')), {'admin'})
        self.assertEqual(tiles.mapped('sql_count'), [2, 2, 2])  # each tile's own SQL only
        self.assertIn('pg_sleep', tiles[0].top_sql)
        self.assertGreaterEqual(tiles[0].duration_ms, 250)
        self.assertLess(tiles[2].duration_ms, 250)
        self.assertLessEqual(sum(tiles.mapped('duration_ms')), call.duration_ms)
        self.assertEqual(tiles[0].top_sql_fingerprint and len(tiles[0].top_sql_fingerprint), 40)
        self.assertTrue(tiles[1].ui_summary.startswith("Dashboard 'Sales Dash' > Tile 'Tile Mid' "), tiles[1].ui_summary)
        # group by tile (list view group-by)
        groups = self.Call.read_group([('kind', '=', 'tile')], ['duration_ms:max'], ['ui_tile'])
        self.assertEqual({g['ui_tile'] for g in groups}, {'Tile Slow', 'Tile Mid', 'Tile Fast'})
        # push payload carries the tile / user / parent
        payload = {p['id']: p for p in (call | tiles)._push_payload()}
        self.assertEqual((payload[call.id]['ui_tile'], payload[call.id]['user_name'], payload[call.id]['parent_id']),
                         ('Tile Slow', admin.name, None))
        self.assertEqual((payload[tiles[0].id]['kind'], payload[tiles[0].id]['parent_id']), ('tile', call.id))

    def test_single_tile_call_has_no_tile_rows(self):
        self._batch([(self.mid.id, 20)])
        call = self.Call.search([])
        self.assertEqual(len(call), 1)
        self.assertEqual((call.ui_tile, call.tile_count), ('Tile Mid', 1))

    def test_fast_call_records_nothing(self):
        set_agent_params(self.env, slow_ms=60000)
        self.assertEqual(self._batch([(self.fast.id, 1), (self.slow.id, 1)])['result'], 2)
        self.assertFalse(self.Call.search([]))

    def test_outside_a_request_the_wrapper_is_transparent(self):
        self.assertEqual(self.env['res.partner'].perf_test_tile_compute(self.fast.id, 0), self.fast.id)
        self.assertFalse(self.Call.search([]))

    def test_tile_errors_propagate_unchanged(self):
        # (in test mode the recording cursor is the test cursor, rolled back with the failed request: look at
        # what is recorded rather than at the row)
        with patch.object(runtime, 'record_slow_call', wraps=runtime.record_slow_call) as record:
            res = self._batch([(self.fast.id, 30), (self.slow.id, None)])  # the 2nd tile raises inside its timer
        self.assertEqual(res['error']['data']['name'], 'builtins.TypeError')
        probe, _duration, error = record.call_args[0][1:4]
        self.assertIsInstance(error, TypeError)
        self.assertEqual(set(probe.tiles), {self.fast.id, self.slow.id})
        self.assertFalse(probe.in_tile)
        vals = runtime.slow_call_vals(probe, 100.0, error)
        self.assertEqual(vals['error'], 'TypeError')

    def test_rate_limit_caps_tile_rows(self):
        with patch.object(runtime, '_limiter', runtime.RateLimiter(2)):
            self._batch([(self.fast.id, 1), (self.slow.id, 1), (self.mid.id, 1)])
        self.assertEqual(self.Call.search_count([]), 2)  # the call + 1 tile row
