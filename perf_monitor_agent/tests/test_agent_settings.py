# -*- coding: utf-8 -*-
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged

from .. import runtime

KEY = 'perf_monitor_agent.%s'


@tagged('post_install', '-at_install', 'perf_monitor_agent')
class TestAgentSettings(TransactionCase):
    """Settings > Perf Monitor Agent and the default central endpoint of the module data."""

    def setUp(self):
        super().setUp()
        self.addCleanup(runtime.clear_config_cache)
        self.params = self.env['ir.config_parameter'].sudo()
        self.params.set_param(KEY % 'central_token', 'Secret-Token-0123456789-abcd')

    def _settings(self, **vals):
        return self.env['res.config.settings'].create(vals)

    def test_install_defaults_endpoint(self):
        # module data (function run at install): the staging central, overridable
        self.assertEqual(self.params.get_param(KEY % 'central_url'), 'https://central.example.com')
        self.assertEqual(self.params.get_param(KEY % 'central_db'), 'demo_db')
        Call = self.env['perf.agent.slow.call']
        self.params.set_param(KEY % 'central_url', 'https://other.example.com')
        self.params.set_param(KEY % 'central_db', False)
        done = Call._set_default_params({'central_url': 'https://x.example.com', 'central_db': 'db2',
                                         'central_token': 'nope', 'slow_ms': '1'})
        self.assertEqual(done, ['central_db'])  # only the missing, known keys (never the token)
        self.assertEqual(self.params.get_param(KEY % 'central_url'), 'https://other.example.com')
        self.assertEqual(self.params.get_param(KEY % 'central_db'), 'db2')
        self.assertEqual(self.params.get_param(KEY % 'central_token'), 'Secret-Token-0123456789-abcd')

    def test_token_never_sent_to_the_form(self):
        settings = self._settings()
        self.assertFalse(settings.perf_agent_central_token)
        self.assertEqual(settings.perf_agent_token_hint, 'set (ends with ...abcd)')
        self.assertNotIn('Secret-Token', str(self.env['res.config.settings'].default_get(
            list(self.env['res.config.settings']._fields))))
        for fname in ('perf_agent_enabled', 'perf_agent_slow_ms', 'perf_agent_central_url', 'perf_agent_central_db',
                      'perf_agent_central_token', 'perf_agent_token_hint'):
            self.assertEqual(self.env['res.config.settings']._fields[fname].groups, 'base.group_system', fname)
        arch = self.env['res.config.settings'].get_view(self.env.ref('base.res_config_settings_view_form').id)['arch']
        self.assertIn('name="perf_agent_central_token"', arch)
        self.assertRegex(arch, r'<field name="perf_agent_central_token"[^>]*password="True"')

    def test_save(self):
        self._settings(perf_agent_enabled=False, perf_agent_slow_ms=750,
                       perf_agent_central_url='https://central.example.com/', perf_agent_central_db='perf_db',
                       perf_agent_central_token=False).execute()
        self.assertEqual([self.params.get_param(KEY % k) for k in ('enabled', 'slow_ms', 'central_url', 'central_db',
                                                                     'central_token')],
                         ['0', '750', 'https://central.example.com', 'perf_db', 'Secret-Token-0123456789-abcd'])
        self.assertEqual(runtime.get_config(self.env), (False, 750))
        settings = self._settings()
        self.assertEqual((settings.perf_agent_enabled, settings.perf_agent_slow_ms), (False, 750))
        self._settings(perf_agent_enabled=True, perf_agent_slow_ms=2000,
                       perf_agent_central_url='http://127.0.0.1:8069', perf_agent_central_db='perf_db',
                       perf_agent_central_token='  New-Token-9876543210-wxyz ').execute()
        self.assertEqual(self.params.get_param(KEY % 'central_token'), 'New-Token-9876543210-wxyz')
        self.assertEqual(self.params.get_param(KEY % 'enabled'), '1')
        self.assertEqual(self.params.get_param(KEY % 'central_url'), 'http://127.0.0.1:8069')

    def test_validation(self):
        good = dict(perf_agent_enabled=True, perf_agent_slow_ms=2000, perf_agent_central_url='https://c.example.com',
                    perf_agent_central_db='db')
        for bad in ({'perf_agent_central_url': 'http://c.example.com'},  # https only (http only to loopback)
                    {'perf_agent_central_url': 'ftp://c.example.com'},
                    {'perf_agent_central_url': 'https://c.example.com/?token=x'},
                    {'perf_agent_central_db': 'db; drop'},
                    {'perf_agent_slow_ms': -1},
                    {'perf_agent_central_token': 'short'},
                    {'perf_agent_central_token': 'has spaces in the token value'}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self._settings(**dict(good, **bad)).execute()
        self.assertEqual(self.params.get_param(KEY % 'central_token'), 'Secret-Token-0123456789-abcd')

    def test_non_admin_cannot_change_it(self):
        user = self.env['res.users'].create({
            'name': 'Settings Only', 'login': 'perf_settings_only',
            'groups_id': [(6, 0, [self.env.ref('base.group_erp_manager').id])]})
        with self.assertRaises(AccessError):
            self.env['res.config.settings'].with_user(user).create({'perf_agent_central_url': 'https://evil.example.com'})
        self.assertEqual(self.params.get_param(KEY % 'central_token'), 'Secret-Token-0123456789-abcd')
        self.assertEqual(self.params.get_param(KEY % 'central_url'), 'https://central.example.com')
