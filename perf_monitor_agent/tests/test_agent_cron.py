# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo import api
from odoo.tests import TransactionCase, tagged

from .. import runtime
from ..tools.probe import build_app_name
from .common import reset_limiter, set_agent_params

SEEN = []


@api.model
def cron_probe(self):
    self.env.cr.execute('SHOW application_name')
    SEEN.append(self.env.cr.fetchone()[0])


@tagged('post_install', '-at_install', 'perf_monitor_agent')
class TestAgentCron(TransactionCase):

    def setUp(self):
        super().setUp()
        # record_slow_call opens registry.cursor(): keep it inside the test transaction
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)
        self.addCleanup(runtime.clear_config_cache)
        reset_limiter()
        SEEN.clear()
        patcher = patch.object(type(self.env['res.partner']), '_perf_agent_cron_probe', cron_probe, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cron = self.env['ir.cron'].create({
            'name': 'Perf Monitor: test probe', 'model_id': self.env.ref('base.model_res_partner').id,
            'state': 'code', 'code': 'model._perf_agent_cron_probe()', 'interval_number': 1,
            'interval_type': 'days', 'numbercall': -1, 'user_id': self.env.ref('base.user_root').id})
        self.Call = self.env['perf.agent.slow.call']

    def _run(self):
        cron = self.cron.with_user(self.cron.user_id)
        cron._callback(self.cron.name, self.cron.ir_actions_server_id.id, self.cron.id)

    def _app_name(self):
        self.env.cr.execute('SHOW application_name')
        return self.env.cr.fetchone()[0]

    def test_cron_tagged_and_reset(self):
        set_agent_params(self.env, slow_ms=60000)
        before = self._app_name()
        self._run()
        self.assertEqual(SEEN, [build_app_name(self.env.cr.dbname, 1, '__system__',
                                               'res.partner._perf_agent_cron_probe', 'cron:Perf Monitor: test probe')])
        self.assertTrue(SEEN[0].startswith('odoo|%s|1|__system__|res.partner._perf_agent_cron_probe|c'
                                           % self.env.cr.dbname), SEEN[0])
        self.assertEqual(self._app_name(), before, 'application_name must be reset after the cron')
        self.assertFalse(self.Call.search([]))

    def test_slow_cron_recorded(self):
        set_agent_params(self.env, slow_ms=0)
        self._run()
        call = self.Call.search([])
        self.assertEqual(len(call), 1)
        self.assertEqual((call.kind, call.cron_name, call.model_name, call.method, call.login),
                         ('cron', 'Perf Monitor: test probe', 'res.partner', '_perf_agent_cron_probe', '__system__'))
        self.assertGreaterEqual(call.sql_count, 1)

    def test_cron_instrumentation_failure_does_not_fail_the_job(self):
        set_agent_params(self.env, slow_ms=0)
        with patch.object(runtime, 'begin_cron', side_effect=RuntimeError('boom')), \
                self.assertLogs('odoo.addons.perf_monitor_agent', 'WARNING'):
            self._run()
        self.assertEqual(len(SEEN), 1)
        with patch.object(runtime, 'record_slow_call', side_effect=RuntimeError('boom')), \
                self.assertLogs('odoo.addons.perf_monitor_agent', 'WARNING'):
            self._run()
        self.assertEqual(len(SEEN), 2)
