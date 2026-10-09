# -*- coding: utf-8 -*-
import re
from unittest.mock import patch

from odoo.tests import HttpCase, tagged

from .. import runtime
from ..tools import ui_context
from .common import json_rpc, reset_limiter, set_agent_params


@tagged('post_install', '-at_install', 'perf_monitor_agent')
class TestAgentUiContext(HttpCase):
    """UI context (dashboard / menu / action / model / record) of slow calls."""

    def setUp(self):
        super().setUp()
        reset_limiter()
        self.addCleanup(runtime.clear_config_cache)
        self.Call = self.env['perf.agent.slow.call']
        self.Call.search([]).unlink()
        set_agent_params(self.env, slow_ms=0)
        self.partner = self.env['res.partner'].create({'name': 'Board Of Directors'})
        self.act_partners = self.env['ir.actions.act_window'].create({
            'name': 'Perf Partners', 'res_model': 'res.partner', 'view_mode': 'tree,form'})
        self.act_countries = self.env['ir.actions.act_window'].create({
            'name': 'Perf Countries', 'res_model': 'res.country', 'view_mode': 'tree,form'})
        self.menu_app = self.env['ir.ui.menu'].create({'name': 'Perf App', 'sequence': 1})
        self.menu_partners = self.env['ir.ui.menu'].create({
            'name': 'Partners', 'parent_id': self.menu_app.id, 'action': 'ir.actions.act_window,%d' % self.act_partners.id})
        self.menu_countries = self.env['ir.ui.menu'].create({
            'name': 'Countries', 'parent_id': self.menu_app.id,
            'action': 'ir.actions.act_window,%d' % self.act_countries.id})
        self.menu_app.action = 'ir.actions.act_window,%d' % self.act_partners.id
        self.authenticate('admin', 'admin')

    def _rpc(self, model, method, args, header=None, context=None):
        return json_rpc(self, '/web/dataset/call_kw/%s/%s' % (model, method), {
            'model': model, 'method': method, 'args': args, 'kwargs': {'context': context or {}}},
            headers={ui_context.HEADER: header} if header else None)

    def _dashboard_is_partner(self):
        """Make res.partner play the dashboards.saas role (odoo_dashboards_saas is not a dependency)."""
        rx = [(re.compile(r'''['"]context_dashboard_id['"]\s*:\s*(\d+)'''), 'res.partner')]
        return patch.object(ui_context, '_RX_CTX_KEYS', rx)

    def test_header_resolved_to_names(self):
        header = 'a=%d&m=%d&mo=res.partner&id=%d&v=form' % (self.act_partners.id, self.menu_partners.id,
                                                            self.partner.id)
        self._rpc('res.partner', 'read', [[self.partner.id], ['name']], header=header)
        call = self.Call.search([('method', '=', 'read')])
        self.assertEqual(len(call), 1)
        self.assertEqual((call.ui_action_id, call.ui_action, call.ui_menu_id, call.ui_menu),
                         (self.act_partners.id, 'Perf Partners', self.menu_partners.id, 'Perf App/Partners'))
        self.assertEqual((call.ui_model, call.ui_record_id, call.ui_view_type), ('res.partner', self.partner.id, 'form'))
        self.assertFalse(call.ui_dashboard)
        self.assertIn("'Perf Partners' opened (menu 'Perf App/Partners') on res.partner #%d by user admin at "
                      % self.partner.id, call.ui_summary)
        self.assertEqual(call.user_name, self.env.ref('base.user_admin').name)
        self.assertIn(' (res.partner.read)', call.ui_summary)

    def test_dashboard_client_action(self):
        dashboard_action = self.env['ir.actions.client'].create({
            'name': 'wad BOD Action', 'tag': 'owl.sales_dashboard_od',
            'context': "{'context_dashboard_id': %d}" % self.partner.id})
        with self._dashboard_is_partner():
            self._rpc('res.partner', 'search_count', [[]], header='a=%d&m=%d' % (dashboard_action.id,
                                                                              self.menu_app.id))
        call = self.Call.search([('method', '=', 'search_count')])
        self.assertEqual((call.ui_dashboard, call.ui_dashboard_model, call.ui_dashboard_id, call.ui_action),
                         ('Board Of Directors', 'res.partner', self.partner.id, 'wad BOD Action'))
        self.assertTrue(call.ui_summary.startswith("Dashboard 'Board Of Directors' (menu Perf App) by user admin "
                                                   "at "), call.ui_summary)
        self.assertEqual(self.Call.search([('ui_dashboard', '!=', False)]), call)  # "Dashboards" filter

    def test_unknown_dashboard_model_named_after_its_action(self):
        # dashboards.saas not installed here: the dashboard is still reported, with its action's name
        dashboard_action = self.env['ir.actions.client'].create({
            'name': 'wad Cash Position Dashboard Action', 'tag': 'owl.sales_dashboard_od',
            'context': "{'context_dashboard_id': 77}"})
        self._rpc('res.partner', 'search_count', [[]], header='a=%d' % dashboard_action.id)
        call = self.Call.search([('method', '=', 'search_count')])
        self.assertEqual((call.ui_dashboard, call.ui_dashboard_model, call.ui_dashboard_id),
                         ('wad Cash Position Dashboard Action', 'dashboards.saas', 77))

    def test_menu_refined_to_the_leaf_opening_the_action(self):
        # Odoo 16 puts the app's ROOT menu in the URL: the menu path is the descendant opening the action
        self._rpc('res.partner', 'search_count', [[]], header='a=%d&m=%d' % (self.act_countries.id, self.menu_app.id))
        call = self.Call.search([('method', '=', 'search_count')])
        self.assertEqual((call.ui_menu_id, call.ui_menu), (self.menu_countries.id, 'Perf App/Countries'))
        call.unlink()
        # no menu sent at all (URL without menu_id): the menu opening the action
        self._rpc('res.partner', 'search_count', [[]], header='a=%d' % self.act_countries.id)
        call = self.Call.search([('method', '=', 'search_count')])
        self.assertEqual((call.ui_menu_id, call.ui_menu), (self.menu_countries.id, 'Perf App/Countries'))

    def test_without_header_rpc_context_used(self):
        ctx = {'params': {'action': self.act_partners.id, 'menu_id': self.menu_partners.id, 'model': 'res.partner',
                          'view_type': 'list'}}
        self._rpc('res.partner', 'search_count', [[]], context=ctx)
        call = self.Call.search([('method', '=', 'search_count')])
        self.assertEqual((call.ui_action, call.ui_menu, call.ui_model, call.ui_view_type),
                         ('Perf Partners', 'Perf App/Partners', 'res.partner', 'list'))

    def test_action_load_and_xmlid(self):
        json_rpc(self, '/web/action/load', {'action_id': 'base.action_partner_form'},
                 headers={ui_context.HEADER: 'a=%d&mo=res.partner&id=1' % self.act_countries.id})
        call = self.Call.search([('route', '=', '/web/action/load')])
        action = self.env.ref('base.action_partner_form')
        self.assertEqual((call.ui_action_id, call.ui_action, call.ui_model, call.ui_record_id),
                         (action.id, action.name, 'res.partner', 0))

    def test_unresolvable_values_kept_raw(self):
        self._rpc('res.partner', 'search_count', [[]], header='a=owl.no_such_tag&m=999999999&mo=x.y&id=5')
        call = self.Call.search([('method', '=', 'search_count')])
        self.assertEqual((call.ui_action, call.ui_action_id, call.ui_menu_id, call.ui_menu, call.ui_model,
                          call.ui_record_id), ('owl.no_such_tag', 0, 999999999, False, 'x.y', 5))

    def test_ui_failures_never_lose_the_row_nor_break_the_request(self):
        header = 'a=%d&m=%d' % (self.act_partners.id, self.menu_partners.id)
        with patch.object(runtime, 'resolve_ui', side_effect=RuntimeError('boom')), \
                self.assertLogs('odoo.addons.perf_monitor_agent', 'WARNING'):
            self.assertIn('result', self._rpc('res.partner', 'search_count', [[]], header=header))
        call = self.Call.search([('method', '=', 'search_count')])
        # recorded with the raw ids, without the names
        self.assertEqual((call.ui_action_id, call.ui_menu_id, call.ui_action, call.ui_menu),
                         (self.act_partners.id, self.menu_partners.id, False, False))
        call.unlink()
        with patch.object(ui_context, 'collect', side_effect=RuntimeError('boom')), \
                self.assertLogs('odoo.addons.perf_monitor_agent', 'WARNING'):
            self.assertIn('result', self._rpc('res.partner', 'search_count', [[]], header=header))
        call = self.Call.search([('method', '=', 'search_count')])
        self.assertEqual(len(call), 1)
        self.assertFalse(call.ui_action_id)

    def test_fast_calls_never_read_the_ui_context(self):
        set_agent_params(self.env, slow_ms=60000)
        with patch.object(ui_context, 'collect') as collect:
            self._rpc('res.partner', 'search_count', [[]], header='a=1')
        collect.assert_not_called()
        self.assertFalse(self.Call.search([]))

    def test_web_client_sends_the_screen(self):
        """Real web client: the URL hash on load, then the action being opened from a menu (the hash
        still names the previous action while the new one loads its data)."""
        self.browser_js(
            '/web#action=%d&menu_id=%d' % (self.act_partners.id, self.menu_app.id),
            "odoo.__WOWL_DEBUG__.root.env.services.menu.selectMenu(%d)"
            ".then(() => console.log('test successful'));" % self.menu_countries.id,
            ready="odoo.isReady === true && !!document.querySelector('.o_list_renderer .o_data_row')", login='admin')
        partners = self.Call.search([('model_name', '=', 'res.partner'), ('method', '=', 'web_search_read')])
        self.assertTrue(partners, self.Call.search([]).mapped('route'))
        # the URL names the action and menu only (the model comes from the action)
        self.assertEqual({(c.ui_action_id, c.ui_action, c.ui_menu_id, c.ui_model) for c in partners},
                         {(self.act_partners.id, 'Perf Partners', self.menu_app.id, 'res.partner')})
        countries = self.Call.search([('model_name', '=', 'res.country'), ('method', '=', 'web_search_read')])
        self.assertTrue(countries)
        self.assertEqual({(c.ui_action, c.ui_menu) for c in countries}, {('Perf Countries', 'Perf App/Countries')})
        load = self.Call.search([('route', '=', '/web/action/load')], order='id desc', limit=1)
        self.assertEqual(load.ui_action_id, self.act_countries.id)
