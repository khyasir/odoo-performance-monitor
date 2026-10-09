# -*- coding: utf-8 -*-
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.tests import TransactionCase, tagged

from ..models import perf_agent_slow_call as mod
from .common import set_agent_params

TOKEN = 'Tok_0123456789abcdefghijklmnopqrstuvwxyzABCD'


def _resp(status=200, body=None):
    resp = MagicMock(status_code=status, text=json.dumps(body or {}))
    resp.json.return_value = body if body is not None else {}
    return resp


@tagged('post_install', '-at_install', 'perf_monitor_agent')
class TestAgentPush(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Call = self.env['perf.agent.slow.call']
        self.Call.search([]).unlink()
        set_agent_params(self.env, central_url='https://perf.example.com/', central_token=TOKEN, central_db='')
        now = fields.Datetime.now()
        self.calls = self.Call.create([{
            'ts': now - timedelta(minutes=i), 'duration_ms': 2500.0 + i, 'kind': 'rpc', 'uid': 2, 'login': 'admin',
            'model_name': 'sale.order', 'method': 'action_confirm', 'route': '/web/dataset/call_button',
            'sql_count': 40, 'sql_ms': 900.0, 'top_sql': 'SELECT 1', 'top_sql_ms': 800.0, 'top_sql_calls': 3,
            'top_sql_fingerprint': 'a' * 40, 'top_sql_callsite': 'sale/models/sale_order.py:10 (x)',
            'ui_dashboard': 'BOD', 'ui_dashboard_model': 'dashboards.saas', 'ui_dashboard_id': 9,
            'ui_action': 'wad BOD Action', 'ui_action_id': 768, 'ui_menu': 'Dashboards / BOD', 'ui_menu_id': 5,
        } for i in range(3)])

    def _session(self, *responses):
        session = MagicMock()
        session.__enter__.return_value = session
        session.headers = {}
        session.post.side_effect = list(responses)
        return session

    def test_push_marks_rows_and_sends_payload(self):
        session = self._session(_resp(200, {'ok': True, 'accepted': 3, 'rejected': 0, 'duplicates': 0}))
        with patch.object(mod.requests, 'Session', return_value=session):
            self.assertEqual(self.Call._cron_push(), 3)
        self.assertTrue(all(self.calls.mapped('pushed')))
        url = session.post.call_args[0][0]
        kwargs = session.post.call_args[1]
        self.assertEqual(url, 'https://perf.example.com/perf_monitor/api/v1/push_slow_calls')
        self.assertEqual(kwargs['headers']['X-Perf-Token'], TOKEN)
        self.assertFalse(kwargs['allow_redirects'])
        body = json.loads(kwargs['data'])
        self.assertEqual((body['version'], body['db_name']), (1, self.env.cr.dbname))
        self.assertEqual(sorted(c['id'] for c in body['calls']), sorted(self.calls.ids))
        first = body['calls'][0]
        for key in ('ts', 'duration_ms', 'kind', 'uid', 'login', 'model_name', 'method', 'route', 'sql_count',
                    'top_sql', 'top_sql_fingerprint', 'top_sql_callsite', 'top_sql_calls', 'ui_model',
                    'ui_record_id', 'ui_view_type'):
            self.assertIn(key, first)
        self.assertEqual((first['ui_dashboard'], first['ui_dashboard_model'], first['ui_dashboard_id'],
                          first['ui_action'], first['ui_action_id'], first['ui_menu'], first['ui_menu_id']),
                         ('BOD', 'dashboards.saas', 9, 'wad BOD Action', 768, 'Dashboards / BOD', 5))
        session.get.assert_not_called()  # no central_db: no pinning
        # nothing left to push: no request at all
        session2 = self._session()
        with patch.object(mod.requests, 'Session', return_value=session2):
            self.assertEqual(self.Call._cron_push(), 0)
        session2.post.assert_not_called()

    def test_central_db_pinned(self):
        set_agent_params(self.env, central_db='central_db')
        session = self._session(_resp(200, {'ok': True}))
        with patch.object(mod.requests, 'Session', return_value=session):
            self.Call._cron_push()
        self.assertEqual(session.get.call_args[0][0], 'https://perf.example.com/web?db=central_db')

    def test_failures_keep_rows_unpushed(self):
        for failure in (requests.ConnectionError('down'), _resp(401, {'ok': False, 'error': 'invalid token'}),
                        _resp(500), _resp(200, {'ok': False})):
            session = self._session(failure)
            with self.subTest(failure=failure), patch.object(mod.requests, 'Session', return_value=session), \
                    self.assertLogs(mod._logger.name, 'WARNING') as logs:
                self.assertEqual(self.Call._cron_push(), 0)
            self.assertFalse(any(self.calls.mapped('pushed')))
            self.assertNotIn(TOKEN, '\n'.join(logs.output))

    def test_config_guards(self):
        for params in ({'central_url': ''}, {'central_token': ''}, {'central_url': 'http://perf.example.com'},
                       {'central_db': "x'; drop"}):
            set_agent_params(self.env, central_url='https://perf.example.com', central_token=TOKEN, central_db='')
            set_agent_params(self.env, **params)
            with self.subTest(params=params), patch.object(mod.requests, 'Session') as session_cls:
                self.assertEqual(self.Call._cron_push(), 0)
            session_cls.assert_not_called()
        self.assertTrue(mod.is_allowed_central_url('http://127.0.0.1:8069'))
        self.assertTrue(mod.is_allowed_central_url('https://perf.example.com'))
        self.assertFalse(mod.is_allowed_central_url('http://10.0.0.1:8069'))
        self.assertFalse(mod.is_allowed_central_url('https://x/?a=b'))

    def test_batches(self):
        self.patch(mod, 'PUSH_BATCH', 2)
        ok = {'ok': True, 'accepted': 2}
        session = self._session(_resp(200, ok), _resp(200, ok))
        with patch.object(mod.requests, 'Session', return_value=session):
            self.assertEqual(self.Call._cron_push(), 3)
        self.assertEqual(session.post.call_count, 2)

    def test_cleanup(self):
        old = self.Call.create({'ts': fields.Datetime.now() - timedelta(days=8), 'kind': 'http'})
        future = self.Call.create({'ts': fields.Datetime.now() + timedelta(days=3), 'kind': 'http'})
        self.assertEqual(self.Call._cron_cleanup(), 2)
        self.assertFalse((old | future).exists())
        self.assertEqual(len(self.calls.exists()), 3)
        self.patch(mod, 'MAX_ROWS', 1)
        self.assertEqual(self.Call._cron_cleanup(), 2)
        self.assertEqual(self.Call.search([]), self.calls.sorted('id')[-1:])

    def test_crons_named_for_staging_hygiene(self):
        for xmlid in ('perf_monitor_agent.ir_cron_agent_push', 'perf_monitor_agent.ir_cron_agent_cleanup'):
            cron = self.env.ref(xmlid)
            self.assertTrue(cron.name.startswith('Perf Monitor: '), cron.name)
            self.assertTrue(cron.active)
        self.assertEqual(self.env.ref('perf_monitor_agent.ir_cron_agent_push').name, 'Perf Monitor: agent push')
        self.assertEqual(self.env.ref('perf_monitor_agent.ir_cron_agent_cleanup').name, 'Perf Monitor: agent cleanup')
