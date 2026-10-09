# -*- coding: utf-8 -*-
from odoo.tests import BaseCase, tagged

from ..tools import ui_context as U


@tagged('post_install', '-at_install', 'perf_monitor_agent')
class TestUiContext(BaseCase):

    def test_parse_header(self):
        self.assertEqual(U.parse_header('a=412&m=97&mo=sale.order&id=12&v=form&ai=3&d=5'), {
            'action': 412, 'menu_id': 97, 'model': 'sale.order', 'record_id': 12, 'view_type': 'form',
            'active_id': 3, 'spreadsheet_id': 5})
        # xmlid / client tag actions, bad values and unknown keys dropped
        self.assertEqual(U.parse_header('a=owl.sales_dashboard_od&m=x&mo=Bad%20Model&id=-1&zz=1'),
                         {'action': 'owl.sales_dashboard_od'})
        self.assertEqual(U.parse_header('id=99999999999&ai=true'), {})
        for bad in (None, '', 42, '&&=', '%zz'):
            self.assertEqual(U.parse_header(bad), {})
        self.assertLessEqual(len(U.parse_header('a=' + 'x' * 5000).get('action', '')), U.MAX_STR)

    def test_header_wins_rpc_context_fills_gaps(self):
        params = {'model': 'res.partner', 'method': 'web_search_read', 'args': [],
                  'kwargs': {'context': {'params': {'action': 7, 'menu_id': 8, 'model': 'res.partner',
                                                    'view_type': 'list'},
                                         'active_id': 44, 'active_model': 'res.company'}}}
        ui = U.collect('a=412&v=form', params, '/web/dataset/call_kw/res.partner/web_search_read')
        self.assertEqual(ui, {'action': 412, 'view_type': 'form', 'menu_id': 8, 'model': 'res.partner',
                              'active_id': 44, 'active_model': 'res.company'})
        # no header (other client / RPC sent before the patch loaded): the URL state of the context
        ui = U.collect(None, params, '/web/dataset/call_kw/res.partner/web_search_read')
        self.assertEqual((ui['action'], ui['menu_id'], ui['view_type']), (7, 8, 'list'))

    def test_action_load_names_the_action_being_opened(self):
        ui = U.collect('a=1&m=2&mo=res.partner&id=3', {'action_id': 55}, '/web/action/load')
        self.assertEqual(ui, {'action': 55, 'menu_id': 2})
        ui = U.collect(None, {'action_id': 'sale.action_orders'}, '/web/action/load')
        self.assertEqual(ui, {'action': 'sale.action_orders'})

    def test_dashboards(self):
        # odoo_dashboards_saas: id in the RPC args (get_analytics_payload(None, [widgets], dashboard_id))
        params = {'model': 'dashboards.saas', 'method': 'get_analytics_payload',
                  'args': [None, [101, 102], 9, {}, False, []], 'kwargs': {'context': {'lang': 'en_US'}}}
        ui = U.collect('a=1137&m=5', params, '/web/dataset/call_kw/dashboards.saas/get_analytics_payload')
        self.assertEqual((ui['dashboard_model'], ui['dashboard_id'], ui['action']), ('dashboards.saas', 9, 1137))
        params['args'] = [None, 9]
        self.assertEqual(U.collect(None, params, '/web/dataset/call_kw')['dashboard_id'], 9)
        # a context key naming the dashboard wins over the args
        params['kwargs']['context']['context_dashboard_id'] = 4
        self.assertEqual(U.collect(None, params, '/web/dataset/call_kw')['dashboard_id'], 4)
        # the client action's own context string
        self.assertEqual(U.dashboard_from_action_context("{'context_dashboard_id': 31}"), ('dashboards.saas', 31))
        self.assertEqual(U.dashboard_from_action_context('{"context_dashboard_id":  7, "x": 1}'),
                         ('dashboards.saas', 7))
        self.assertEqual(U.dashboard_from_action_context("{'group_by': 'x'}"), (None, None))
        self.assertEqual(U.dashboard_from_action_context(False), (None, None))

    def test_header_dashboard_and_tile(self):
        self.assertEqual(U.parse_header('a=1244&m=5&dh=183&t=4021'),
                         {'action': 1244, 'menu_id': 5, 'saas_dashboard_id': 183, 'tile_id': 4021})
        ui = U.collect('a=1244&m=5&dh=183&t=4021', {}, '/web/dataset/call_kw')
        self.assertEqual(ui, {'action': 1244, 'menu_id': 5, 'dashboard_model': 'dashboards.saas',
                              'dashboard_id': 183, 'tile_id': 4021})
        self.assertEqual(U.parse_header('dh=x&t=-3'), {})

    def test_saas_dashboard_rpcs(self):
        path = '/web/dataset/call_kw'
        # the batch RPC loading every tile of one type: dashboard id is args[3] (or the kwarg)
        params = {'model': 'dashboards.saas', 'method': 'get_all_datasets_saas',
                  'args': [None, 'tile', None, 183, False, None], 'kwargs': {'context': {}}}
        ui = U.collect('a=1244&m=5', params, path)
        self.assertEqual((ui['dashboard_model'], ui['dashboard_id'], ui.get('tile_id')), ('dashboards.saas', 183, None))
        params['args'] = [None]
        params['kwargs'] = {'dashboard_id': '157', 'context': {}}
        self.assertEqual(U.collect(None, params, path)['dashboard_id'], 157)
        # per-tile RPCs: args[1] is a TILE id, never a dashboard id (the dashboard comes from the tile)
        for method in ('get_tile_data_from_da_id', 'get_bar_chart_data_from_da_id', 'get_table_data_from_da_id',
                       'get_pivot_data_from_da_id', 'get_heatmap_data_from_da_id', 'get_map_data_from_da_id'):
            with self.subTest(method=method):
                ui = U.collect('a=1244&dh=183', {'model': 'dashboards.saas', 'method': method,
                                                 'args': [None, 4021, None, None]}, path)
                self.assertEqual((ui['tile_id'], ui['dashboard_id']), (4021, 183))
                self.assertNotIn('record_id', ui)
                ui = U.collect(None, {'model': 'dashboards.saas', 'method': method, 'args': [None, 4021]}, path)
                self.assertEqual(ui, {'tile_id': 4021})
        # the explicit argument beats a stale header tile
        ui = U.collect('t=1', {'model': 'dashboards.saas', 'method': 'get_tile_data_from_da_id',
                               'args': [None, 4021]}, path)
        self.assertEqual(ui['tile_id'], 4021)
        ui = U.collect(None, {'model': 'dashboards.saas', 'method': 'get_dashboard_metadata', 'args': [None, 8]}, path)
        self.assertEqual(ui, {'dashboard_model': 'dashboards.saas', 'dashboard_id': 8})
        # data.analytics.saas record calls name the tile
        ui = U.collect(None, {'model': 'data.analytics.saas', 'method': 'write', 'args': [[4021], {}]}, path)
        self.assertEqual((ui['tile_id'], ui['model'], ui['record_id']), (4021, 'data.analytics.saas', 4021))

    def test_record_from_single_id_rpc(self):
        params = {'model': 'sale.order', 'method': 'action_confirm', 'args': [[12]], 'kwargs': {}}
        self.assertEqual(U.collect(None, params, '/web/dataset/call_button'),
                         {'model': 'sale.order', 'record_id': 12})
        # the screen (header) wins over the call's own ids; multi-id calls name no record
        self.assertEqual(U.collect('mo=sale.order&v=list', params, '/web/dataset/call_button'),
                         {'model': 'sale.order', 'view_type': 'list'})
        params['args'] = [[1, 2]]
        self.assertEqual(U.collect(None, params, '/web/dataset/call_button'), {})
        # non-RPC routes: args are not ids
        self.assertEqual(U.collect(None, {'model': 'x', 'args': [[3]]}, '/my/route'), {})

    def test_never_raises_on_junk(self):
        for params in (None, [], 'x', {'kwargs': 'x'}, {'kwargs': {'context': []}},
                       {'kwargs': {'context': {'params': 'x', 'active_id': {'a': 1}}}},
                       {'model': 5, 'args': 'x'}, {'model': 'dashboards.saas', 'args': [None, [True], 'x']}):
            with self.subTest(params=params):
                self.assertIsInstance(U.collect('a=1', params, '/web/dataset/call_kw'), dict)

    def test_summary(self):
        self.assertEqual(U.summary('2026-09-27 14:05', 3210.0, dashboard='BOD', action='wad BOD Action',
                                   model_name='dashboards.saas', method='get_analytics_payload'),
                         "Dashboard 'BOD' at 2026-09-27 14:05 took 3.2 s "
                         "(dashboards.saas.get_analytics_payload)")
        # the format asked for: dashboard > tile (menu path) by user at .. took ..
        self.assertEqual(U.summary('2026-09-27 16:05', 4200.0, dashboard='Sales', tile='Top Products',
                                   menu='Sales/Dashboards', login='ali', action='wad Sales Action',
                                   model='dashboards.saas', record_id=3, model_name='dashboards.saas',
                                   method='get_all_datasets_saas'),
                         "Dashboard 'Sales' > Tile 'Top Products' (menu Sales/Dashboards) by user ali at "
                         "2026-09-27 16:05 took 4.2 s (dashboards.saas.get_all_datasets_saas)")
        self.assertEqual(U.summary('09:00', 2500.0, tile='KPI', login='bob', route='/x'),
                         "/x called > Tile 'KPI' by user bob at 09:00 took 2.5 s")
        self.assertEqual(U.summary('09:00', 2500.0, action='Quotations', menu='Sales / Orders / Quotations',
                                   model='sale.order', record_id=12, model_name='sale.order', method='read'),
                         "'Quotations' opened (menu 'Sales / Orders / Quotations') on sale.order #12 at 09:00 "
                         "took 2.5 s (sale.order.read)")
        self.assertEqual(U.summary('09:00', 60000.0, cron_name='Mail: queue', login='__system__'),
                         "Cron 'Mail: queue' ran at 09:00 took 60.0 s")
        self.assertEqual(U.summary('09:00', 2000.0, route='/web/login'), '/web/login called at 09:00 took 2.0 s')
