# -*- coding: utf-8 -*-
from lxml import etree

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, new_test_user, tagged

from ..models.perf_common import COLLECTOR_CONF_PATH, COLLECTOR_RESTART_COMMAND


@tagged('post_install', '-at_install')
class TestTokenSafety(TransactionCase):
    """Regenerate / Revoke token: Perf Monitor Manager only, strong confirm, new token + collector.conf steps."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Server = cls.env['perf.server']
        # throwaway record: never the live collector
        cls.server = cls.Server.create({'name': 'token-test', 'host': '10.9.9.9', 'db_name': 'token_test_db'})
        cls.token = cls.server._set_new_token()
        cls.viewer = new_test_user(cls.env, login='perf_tok_viewer', groups='base.group_user,perf_monitor.group_perf_viewer')
        cls.settings_admin = new_test_user(cls.env, login='perf_tok_admin',
                                           groups='base.group_user,base.group_system,perf_monitor.group_perf_viewer')
        cls.manager = new_test_user(cls.env, login='perf_tok_manager', groups='base.group_user,perf_monitor.group_perf_manager')

    def test_group_name(self):
        self.assertEqual(self.env.ref('perf_monitor.group_perf_manager').name, 'Perf Monitor Manager')

    def test_non_manager_user_error(self):
        for user in (self.viewer, self.settings_admin):
            server = self.server.with_user(user)
            with self.assertRaises(UserError):
                server.action_regenerate_token()
            with self.assertRaises(UserError):
                server.action_revoke_token()
        # the token still works
        self.assertEqual(self.Server._find_by_token(self.token), self.server)

    def test_settings_admin_without_group_is_not_manager(self):
        self.assertTrue(self.settings_admin.has_group('base.group_system'))
        self.assertFalse(self.settings_admin.has_group('perf_monitor.group_perf_manager'))
        with self.assertRaises(UserError):  # AccessError (no wizard ACL) is a UserError
            self.env['perf.server.add.wizard'].with_user(self.settings_admin).create({
                'name': 'x', 'environment': 'staging', 'host': 'h', 'db_name': 'd',
                'central_url': 'http://127.0.0.1:8069'}).action_confirm()

    def test_regenerate_opens_wizard_with_conf_path_and_restart(self):
        action = self.server.with_user(self.manager).action_regenerate_token()
        self.assertEqual(action['res_model'], 'perf.server.add.wizard')
        self.assertEqual(action['target'], 'new')
        wizard = self.env['perf.server.add.wizard'].browse(action['res_id'])
        self.assertEqual((wizard.state, wizard.mode, wizard.server_id), ('done', 'regenerate', self.server))
        self.assertEqual(wizard.conf_path, '/etc/perf-monitor/collector.conf')
        self.assertEqual(wizard.restart_command, 'sudo systemctl restart perf-monitor-collector')
        new_token = wizard.token
        self.assertTrue(new_token)
        self.assertEqual(self.Server._find_by_token(new_token), self.server)
        self.assertFalse(self.Server._find_by_token(self.token), 'the old token must be revoked')
        self.assertIn('token = %s' % new_token, wizard.conf_command)
        self.assertIn(COLLECTOR_CONF_PATH, wizard.conf_command)
        self.assertTrue(wizard.conf_command.endswith(COLLECTOR_RESTART_COMMAND))
        self.assertIn(COLLECTOR_CONF_PATH, self.server.message_ids[0].body)
        wizard.action_close()
        self.assertFalse(wizard.token or wizard.conf_command or wizard.install_command)

    def test_conf_path_matches_collector_files(self):
        from odoo.modules.module import get_module_resource
        with open(get_module_resource('perf_monitor', 'collector', 'perf-monitor-collector.service')) as f:
            self.assertIn('--config %s' % COLLECTOR_CONF_PATH, f.read())
        with open(get_module_resource('perf_monitor', 'collector', 'install.sh.tmpl')) as f:
            tmpl = f.read()
        self.assertIn('/etc/perf-monitor', tmpl)
        self.assertIn('token = $TOKEN', tmpl)
        self.assertIn('perf-monitor-collector.service', tmpl)

    def test_wizard_view_done_state(self):
        arch = etree.fromstring(self.env['perf.server.add.wizard'].get_views([(False, 'form')])['views']['form']['arch'])
        block = etree.tostring(arch.xpath("//div[@name='regenerate_collector']")[0]).decode()
        for text in ('/etc/perf-monitor/collector.conf', 'sudo systemctl restart perf-monitor-collector',
                     'name="token"', 'name="conf_command"', 'name="restart_command"', '200', '401'):
            self.assertIn(text, block)

    def test_confirm_strings(self):
        arch = etree.fromstring(self.Server.with_user(self.manager).get_views(
            [(False, 'form')])['views']['form']['arch'])
        for name in ('action_regenerate_token', 'action_revoke_token'):
            buttons = arch.xpath("//button[@name='%s']" % name)
            self.assertEqual(len(buttons), 2, name)  # collector / DB agent variants
            confirms = [b.get('confirm') or '' for b in buttons]
            for confirm in confirms:
                self.assertIn('401', confirm)
                self.assertIn('STOP', confirm)
            self.assertTrue(any('/etc/perf-monitor/collector.conf' in c and 'perf-monitor-collector' in c
                                for c in confirms), name)
            self.assertTrue(any('perf_monitor_agent.central_token' in c for c in confirms), name)
        # buttons carry the manager group in the source arch
        source = etree.fromstring(self.env.ref('perf_monitor.perf_server_view_form').arch_db)
        for button in source.xpath("//button[@name='action_regenerate_token' or @name='action_revoke_token']"):
            self.assertEqual(button.get('groups'), 'perf_monitor.group_perf_manager')

    def test_buttons_hidden_for_non_manager(self):
        for user in (self.viewer, self.settings_admin):
            arch = etree.fromstring(self.Server.with_user(user).get_views([(False, 'form')])['views']['form']['arch'])
            self.assertFalse(arch.xpath("//button[@name='action_regenerate_token' or @name='action_revoke_token']"),
                             user.login)
