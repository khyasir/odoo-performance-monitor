# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo import api
from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged

from .. import runtime
from ..tools.probe import build_app_name, parse_app_name
from .common import json_rpc, probe_app_name, reset_limiter, set_agent_params


@tagged('post_install', '-at_install', 'perf_monitor_agent')
class TestAgentHttp(HttpCase):

    def setUp(self):
        super().setUp()
        reset_limiter()
        self.addCleanup(runtime.clear_config_cache)
        self.Call = self.env['perf.agent.slow.call']
        self.Call.search([]).unlink()
        self.authenticate('admin', 'admin')
        patcher = patch.object(type(self.env['res.partner']), 'perf_agent_probe', probe_app_name, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _search_read(self):
        return json_rpc(self, '/web/dataset/call_kw/res.partner/search_read', {
            'model': 'res.partner', 'method': 'search_read', 'args': [[]],
            'kwargs': {'fields': ['name'], 'limit': 5}})

    def test_app_name_tagged_during_request(self):
        set_agent_params(self.env, slow_ms=60000)
        result = json_rpc(self, '/web/dataset/call_kw/res.partner/perf_agent_probe', {
            'model': 'res.partner', 'method': 'perf_agent_probe', 'args': [], 'kwargs': {}})['result']
        uid = self.env.ref('base.user_admin').id
        self.assertEqual(result, build_app_name(self.env.cr.dbname, uid, 'admin', 'res.partner.perf_agent_probe',
                                                '/web/dataset/call_kw'))
        self.assertTrue(result.startswith('odoo|%s|%s|admin|res.partner.perf_agent_probe|/web/'
                                          % (self.env.cr.dbname, uid)), result)
        self.assertLessEqual(len(result.encode()), 63)
        self.assertFalse(self.Call.search([]), 'fast call must not be recorded')

    def test_http_route_tagged_with_endpoint(self):
        set_agent_params(self.env, slow_ms=0)
        resp = self.url_open('/web/login')
        self.assertEqual(resp.status_code, 200)
        call = self.Call.search([('route', '=', '/web/login')], limit=1)
        self.assertEqual(call.kind, 'http')
        self.assertTrue(call.method.endswith('Home.web_login'), call.method)  # auth_signup overrides it
        self.assertEqual(parse_app_name(call.app_name), {'db': self.env.cr.dbname, 'uid': str(call.uid or 0),
                                                         'login': call.login or '-', 'model_method': call.method,
                                                         'where': '/web/login'})

    def test_slow_call_recorded(self):
        set_agent_params(self.env, slow_ms=0)
        self.assertIn('result', self._search_read())
        call = self.Call.search([('method', '=', 'search_read')])
        self.assertEqual(len(call), 1)
        self.assertEqual((call.kind, call.model_name, call.login, call.route),
                         ('rpc', 'res.partner', 'admin', '/web/dataset/call_kw/res.partner/search_read'))
        self.assertEqual(call.uid, self.env.ref('base.user_admin').id)
        self.assertGreater(call.sql_count, 0)
        self.assertGreaterEqual(call.duration_ms, call.sql_ms)
        self.assertTrue(call.top_sql and call.top_sql_calls >= 1 and call.top_sql_callsite, call.read()[0])
        self.assertEqual(len(call.top_sql_fingerprint), 40)
        self.assertFalse(call.pushed)
        self.assertFalse(call.error)

    def test_threshold(self):
        set_agent_params(self.env, slow_ms=60000)
        self._search_read()
        self.assertFalse(self.Call.search([]))

    def test_excluded_routes(self):
        set_agent_params(self.env, slow_ms=0)
        json_rpc(self, '/web/webclient/version_info', {})
        self.assertFalse(self.Call.search([('route', 'like', 'version_info')]))
        self._search_read()
        self.assertTrue(self.Call.search([('method', '=', 'search_read')]))

    def test_disabled(self):
        set_agent_params(self.env, slow_ms=0, enabled='0')
        result = json_rpc(self, '/web/dataset/call_kw/res.partner/perf_agent_probe', {
            'model': 'res.partner', 'method': 'perf_agent_probe', 'args': [], 'kwargs': {}})['result']
        self.assertFalse(result.startswith('odoo|'), result)
        self.assertFalse(self.Call.search([]))

    def test_agent_failures_never_break_the_request(self):
        set_agent_params(self.env, slow_ms=0)
        expected = len(self._search_read()['result'])
        boom = RuntimeError('agent bug')
        for target in ('begin_request', 'record_slow_call', '_set_app_name', 'slow_call_vals', 'get_config'):
            with self.subTest(target=target), patch.object(runtime, target, side_effect=boom), \
                    self.assertLogs('odoo.addons.perf_monitor_agent', 'WARNING'):
                self.assertEqual(len(self._search_read()['result']), expected)
        # the hook was always removed and nothing leaked into the thread-local state
        self.assertIsNone(getattr(runtime._local, 'probe', None))

    def test_endpoint_exception_propagates(self):
        set_agent_params(self.env, slow_ms=0)
        def fail(self):
            raise UserError('business error')

        with patch.object(runtime, 'record_slow_call') as record, \
                patch.object(type(self.env['res.partner']), 'perf_agent_fail', api.model(fail), create=True):
            result = json_rpc(self, '/web/dataset/call_kw/res.partner/perf_agent_fail', {
                'model': 'res.partner', 'method': 'perf_agent_fail', 'args': [], 'kwargs': {}})
        self.assertEqual(result['error']['data']['message'], 'business error')
        # recorded with the exception class (the row itself is written on a separate cursor)
        self.assertTrue(record.called)
        self.assertIsInstance(record.call_args[0][3], UserError)

    def test_error_row(self):
        probe = runtime.CallProbe('rpc', 0, {'uid': 2, 'login': 'admin', 'model_name': 'res.partner',
                                             'method': 'write', 'route': '/web/dataset/call_kw', 'cron_name': False},
                                  'odoo|x')
        probe(None, "UPDATE res_partner SET name = 'x' WHERE id IN %s", [(1,)], 0, 0.5)
        self.assertTrue(runtime.record_slow_call(self.registry, probe, 2500.0, UserError('x')))
        call = self.Call.search([])
        self.assertEqual((call.error, call.duration_ms, call.top_sql_ms, call.sql_count),
                         ('UserError', 2500.0, 500.0, 1))
        self.assertEqual(call.top_sql, 'UPDATE res_partner SET name = ? WHERE id IN %s')

    def test_rate_limit(self):
        set_agent_params(self.env, slow_ms=0)
        runtime._limiter.limit = 2
        for _i in range(4):
            self._search_read()
        self.assertEqual(self.Call.search_count([('method', '=', 'search_read')]), 2)
