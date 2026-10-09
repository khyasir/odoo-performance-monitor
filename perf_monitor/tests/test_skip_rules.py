# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools.safe_eval import safe_eval

from ..models import perf_issue as pi
from ..models import perf_skip_rule as psr


@tagged('post_install', '-at_install')
class TestSkipAndFilterRules(TransactionCase):
    """Pure rules: skip rule matching, actionable filter, quiet routes, plain explanation."""

    def test_skip_rule_matches(self):
        call = {'issue_key': 'n1:' + 'a' * 40, 'top_sql_fingerprint': 'a' * 40, 'model_name': 'sale.order',
                'route': '/web/dataset/call_kw/sale.order/read'}
        self.assertTrue(psr.skip_rule_matches('issue_key', 'n1:' + 'a' * 40, call))
        self.assertFalse(psr.skip_rule_matches('issue_key', 'sql:' + 'a' * 40, call))
        self.assertTrue(psr.skip_rule_matches('fingerprint', 'a' * 40, call))
        self.assertTrue(psr.skip_rule_matches('model', 'sale.order', call))
        self.assertFalse(psr.skip_rule_matches('model', 'sale.order.line', call))
        self.assertFalse(psr.skip_rule_matches('route', '/web/dataset/call_kw', call))
        self.assertTrue(psr.skip_rule_matches('route', '/web/dataset/*', call))
        self.assertFalse(psr.skip_rule_matches('model', '', call))
        self.assertFalse(psr.skip_rule_matches('route', '/x', {'route': False}))

    def test_actionable_reason_and_quiet_routes(self):
        base = {'duration_ms': 10000.0, 'sql_ms': 1000.0, 'top_sql_ms': 100.0, 'top_sql_calls': 5}
        self.assertIsNone(pi.actionable_reason(base))
        self.assertEqual(pi.actionable_reason(dict(base, sql_ms=5000.0)), 'sql_share')
        self.assertEqual(pi.actionable_reason(dict(base, top_sql_ms=1000.0)), 'slow_query')
        self.assertEqual(pi.actionable_reason(dict(base, top_sql_calls=50)), 'n_plus_1')
        self.assertIsNone(pi.actionable_reason(dict(base, top_sql_calls=49)))
        self.assertEqual(pi.actionable_reason(dict(base, top_sql_calls=5), min_repeats=5), 'n_plus_1')
        for route in ('/', '/web', '/web/login', '/web/assets/debug/web.assets_backend.js', '/web/webclient/qweb/x'):
            self.assertTrue(pi.is_quiet_route(route), route)
        for route in ('/web/dataset/call_kw', '/shop', '/web/image', False, ''):
            self.assertFalse(pi.is_quiet_route(route), route)

    def test_plain_explanation(self):
        text = pi.plain_explanation({
            'issue_type': 'n_plus_1', 'menu': 'Accounting/Closings', 'screen': 'Closing Wags',
            'route': '/web/dataset/call_kw', 'model_name': 'closing.wags', 'method': 'get_closing_date',
            'users': 'ali, admin', 'databases': 'erp', 'max_duration_ms': 240000.0, 'avg_duration_ms': 200000.0,
            'sql_ms': 60000.0, 'top_sql': 'SELECT id\n FROM account_move_line WHERE move_id = %s',
            'top_sql_ms': 25.0, 'max_repeats': 36710, 'callsite': 'closing_wags/models/closing_wags.py:1698',
            'seq_scan_tables': 'account_move_line (5000000 rows)'})
        english, urdu = text.split('\n\nRoman Urdu\n')
        for part in ("menu 'Accounting/Closings'", 'closing.wags.get_closing_date', 'Users: ali, admin',
                     'database (SQL) 60.0 s (25 %)', 'Odoo / Python 180.0 s (75 %)', '36710 times',
                     'SELECT id FROM account_move_line', 'N+1', 'closing_wags.py:1698', 'Index',
                     'On DB erp log in as ali'):
            self.assertIn(part, english)
        for part in ('Kahan se', 'Waqt', 'aik call mein 36710 dafa', 'Mumkina hal', 'login karen', 'Dobara kaise'):
            self.assertIn(part, urdu)
        cron = pi.plain_explanation({'cron_name': 'Mail: queue', 'max_duration_ms': 9000.0})
        self.assertIn("run the scheduled action 'Mail: queue'", cron)
        self.assertIn('database time not reported', cron)
        python = pi.plain_explanation({'model_name': 'x.y', 'method': 'run', 'max_duration_ms': 10000.0,
                                       'sql_ms': 500.0})
        self.assertIn('Python code: most of the time is in Odoo', python)


class _BuildCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Remote = cls.env['perf.agent.slow.call.remote']
        cls.Issue = cls.env['perf.issue']
        cls.Rule = cls.env['perf.skip.rule']
        cls.Remote.search([]).write({'issue_checked': True})
        cls.Rule.search([]).write({'active': False})
        cls.ICP = cls.env['ir.config_parameter']
        cls.ICP.set_param('perf_monitor.ai_enabled', 'False')
        cls.server = cls.env['perf.server'].create({'name': 'agent', 'host': 'agent:skipdb', 'db_name': 'skipdb'})
        cls.next_id = 1

    def _row(self, age_min=5, **kw):
        type(self).next_id += 1
        vals = {'server_id': self.server.id, 'agent_id': self.next_id, 'db_name': 'skipdb', 'kind': 'rpc',
                'ts': fields.Datetime.now() - timedelta(minutes=age_min), 'duration_ms': 9000.0, 'sql_ms': 500.0,
                'model_name': 'stock.picking', 'method': 'button_validate', 'login': 'ali', 'sql_count': 3000,
                'top_sql_calls': 500, 'top_sql_ms': 10.0, 'top_sql_fingerprint': '1' * 40,
                'top_sql': 'SELECT id FROM stock_move WHERE picking_id = %s', 'route': '/web/dataset/call_kw'}
        vals.update(kw)
        return self.Remote.create(vals)

    def _issue(self, key):
        return self.Issue.search([('fingerprint', '=', pi.issue_fingerprint(key))])


@tagged('post_install', '-at_install')
class TestSkipRules(_BuildCase):
    """Skip button / wizard, skip rules at build time, expiry, menu, access."""

    def _skip(self, issue, **vals):
        action = issue.action_skip()
        self.assertEqual((action['res_model'], action['target']), ('perf.issue.skip.wizard', 'new'))
        wizard = self.env['perf.issue.skip.wizard'].with_context(action['context']).create(
            dict({'reason': 'Known month-end closing, fixed in the next release'}, **vals))
        wizard.action_confirm()
        return wizard

    def test_skip_wizard_and_build(self):
        self._row()
        self.Issue._build_issues()
        key = 'n1:' + '1' * 40
        issue = self._issue(key)
        self.assertEqual(len(issue.activity_ids), 1)
        wizard = self._skip(issue)
        self.assertEqual((wizard.match_type, wizard.match_value), ('issue_key', key))
        rule = issue.skip_rule_id
        self.assertEqual((rule.match_type, rule.match_value, rule.user_id), ('issue_key', key, self.env.user))
        self.assertAlmostEqual(rule.until, fields.Datetime.now() + timedelta(days=7), delta=timedelta(minutes=5))
        self.assertEqual(issue.state, 'skipped')
        self.assertFalse(issue.activity_ids, 'skipped: the open activity is dropped')
        # a matching slow call is stored and counted, the issue is untouched
        occurrences, severity = issue.occurrence_count, issue.severity
        row = self._row(age_min=1, duration_ms=120000.0)
        self.Issue._build_issues()
        self.assertTrue(row.exists() and row.issue_checked)
        self.assertFalse(row.issue_id)
        self.assertEqual((row.skip_rule_id, row.issue_key), (rule, key))
        self.assertEqual((rule.hit_count, rule.last_hit), (1, row.ts))
        self.assertEqual((issue.state, issue.occurrence_count, issue.severity), ('skipped', occurrences, severity))
        self.assertFalse(issue.activity_ids)
        # skipped issues are hidden by the default filter of Performance > Issues
        action = self.env['ir.actions.act_window']._for_xml_id('perf_monitor.action_perf_issue')
        self.assertEqual(safe_eval(action['context']).get('search_default_not_closed'), 1)
        arch = self.Issue.get_view(view_type='search')['arch']
        self.assertIn("[('state', 'not in', ('closed', 'skipped'))]", arch)
        self.assertNotIn(issue, self.Issue.search([('state', 'not in', ('closed', 'skipped'))]))

    def test_expired_rule_reopens(self):
        self._row()
        self.Issue._build_issues()
        issue = self._issue('n1:' + '1' * 40)
        self._skip(issue, duration='30d')
        rule = issue.skip_rule_id
        self.assertAlmostEqual(rule.until, fields.Datetime.now() + timedelta(days=30), delta=timedelta(minutes=5))
        # expiry is checked at match time (no cron needed)
        rule.until = fields.Datetime.now() - timedelta(minutes=1)
        self.assertTrue(rule.expired)
        self._row(age_min=1)
        self.Issue._build_issues()
        self.assertEqual((issue.state, issue.reopen_count), ('open', 1))
        self.assertEqual(rule.hit_count, 0)
        self.assertEqual(len(issue.activity_ids), 1)
        self.assertEqual(issue.activity_ids.summary, 'Reopened performance issue')
        self.assertTrue(any('no skip rule matches' in (m.body or '') for m in issue.message_ids))
        # the build cron archives expired rules
        self.assertTrue(rule.active)
        self.Issue._cron_build_issues()
        self.assertFalse(rule.active)

    def test_forever_and_other_match_types(self):
        self._row()
        self.Issue._build_issues()
        issue = self._issue('n1:' + '1' * 40)
        self._skip(issue, match_type='model', duration='forever')
        self.assertEqual(issue.skip_rule_id.match_value, 'stock.picking')
        self.assertFalse(issue.skip_rule_id.until)
        # any issue of this model is kept out, even a new key
        self._row(top_sql_fingerprint='2' * 40)
        self.Rule.create({'name': 'fp', 'match_type': 'fingerprint', 'match_value': '3' * 40, 'reason': 'x'})
        self.Rule.create({'name': 'rt', 'match_type': 'route', 'match_value': '/shop/*', 'reason': 'x'})
        self._row(model_name='sale.order', top_sql_fingerprint='3' * 40)
        self._row(model_name=False, method=False, route='/shop/cart', top_sql_fingerprint='4' * 40)
        self.Issue._build_issues()
        for key in ('n1:' + '2' * 40, 'n1:' + '3' * 40, 'n1:' + '4' * 40):
            self.assertFalse(self._issue(key), key)
        self.assertEqual(self.Rule.search([('name', 'in', ('fp', 'rt'))]).mapped('hit_count'), [1, 1])
        # reopen by hand ends the skip rule
        rule = issue.skip_rule_id
        issue.action_open()
        self.assertEqual(issue.state, 'open')
        self.assertFalse(rule.active)

    def test_wizard_checks_and_access(self):
        self._row(model_name=False, method=False, route='/shop/cart')
        self.Issue._build_issues()
        issue = self._issue('n1:' + '1' * 40)
        wizard = self.env['perf.issue.skip.wizard'].create({'issue_id': issue.id, 'match_type': 'model',
                                                            'reason': 'x'})
        self.assertFalse(wizard.match_value)
        with self.assertRaises(UserError):
            wizard.action_confirm()
        wizard.match_type = 'route'
        self.assertEqual(wizard.match_value, '/shop/cart')
        viewer = self.env['res.users'].create({
            'name': 'viewer', 'login': 'perf_viewer_skip',
            'groups_id': [(6, 0, [self.env.ref('perf_monitor.group_perf_viewer').id])]})
        with self.assertRaises(UserError):
            issue.with_user(viewer).action_skip()
        self.assertEqual(self.env.ref('perf_monitor.menu_perf_skip_rules').action,
                         self.env.ref('perf_monitor.action_perf_skip_rule'))
        manager = self.env.ref('perf_monitor.group_perf_manager')
        self.assertIn(manager, self.env.ref('perf_monitor.menu_perf_skip_rules').groups_id)
        access = self.env['ir.model.access'].search([('model_id.model', '=', 'perf.skip.rule'),
                                                     ('group_id', '=', manager.id)])
        self.assertTrue(access.perm_unlink and access.perm_create)
        self.assertTrue(self.Rule.with_user(viewer).check_access_rights('read'))
        self.assertFalse(self.Rule.with_user(viewer).check_access_rights('create', raise_exception=False))


@tagged('post_install', '-at_install')
class TestActionableFilter(_BuildCase):
    """Only actionable slowness makes an issue; the rest is kept without one."""

    def _quiet(self, **kw):
        # slow in Python, few queries: no reason on its own
        vals = dict(top_sql_calls=3, top_sql_ms=50.0, sql_ms=400.0, model_name='res.partner', method='compute_x',
                    top_sql_fingerprint='8' * 40)
        vals.update(kw)
        return self._row(**vals)

    def test_recurring(self):
        key = 'method:res.partner.compute_x'
        first = self._quiet(age_min=60 * 24 * 8)  # older than 7 days: does not count
        second = self._quiet(age_min=120)
        self.Issue._build_issues()
        self.assertFalse(self._issue(key))
        self.assertTrue(second.issue_checked)
        self.assertEqual((second.issue_key, second.issue_id.id), (key, False))
        self._quiet(age_min=60)
        self.Issue._build_issues()
        self.assertFalse(self._issue(key), '2 occurrences in 7 days')
        third = self._quiet(age_min=1)
        self.Issue._build_issues()
        issue = self._issue(key)
        self.assertEqual(issue.issue_type, 'slow_method')
        self.assertEqual(issue.occurrence_count, 3, 'the earlier calls of the 7 days become occurrences')
        self.assertIn(third, issue.slow_call_ids)
        self.assertNotIn(first, issue.slow_call_ids)

    def test_sql_share_slow_query_and_n_plus_1(self):
        self._quiet(sql_ms=4500.0, method='a')
        self._quiet(top_sql_ms=1000.0, method='b', top_sql_fingerprint='7' * 40, duration_ms=9000.0)
        self._quiet(top_sql_calls=60, method='c')
        self._quiet(method='d')
        self.Issue._build_issues()
        self.assertTrue(self._issue('method:res.partner.a'))
        self.assertTrue(self._issue('method:res.partner.b'))
        self.assertTrue(self._issue('method:res.partner.c'))
        self.assertFalse(self._issue('method:res.partner.d'))

    def test_quiet_routes(self):
        for age in (30, 20, 10):
            self._quiet(age_min=age, model_name=False, method=False, route='/web', top_sql_fingerprint=False)
        self.Issue._build_issues()
        self.assertFalse(self._issue('route:/web'), 'recurring does not count on /web')
        self._quiet(model_name=False, method=False, route='/web/login', top_sql_fingerprint=False, sql_ms=8000.0)
        self.Issue._build_issues()
        self.assertTrue(self._issue('route:/web/login'), 'SQL share still counts on /web/login')

    def test_thresholds_from_settings(self):
        self.ICP.set_param('perf_monitor.issue_min_repeats', '3')
        self.assertEqual(self.Issue._issue_thresholds()['min_repeats'], 3)
        self._quiet(method='e')
        self.Issue._build_issues()
        self.assertTrue(self._issue('method:res.partner.e'))
        # saving 0 in Settings deletes the parameter: the default applies on read
        settings = self.env['res.config.settings'].create({
            'perf_issue_min_repeats': 0, 'perf_issue_min_sql_share': 0.0, 'perf_issue_min_query_ms': 0,
            'perf_issue_min_occ_7d': 0})
        settings.execute()
        for key in ('issue_min_repeats', 'issue_min_sql_share', 'issue_min_query_ms', 'issue_min_occ_7d'):
            self.assertFalse(self.ICP.get_param('perf_monitor.' + key), key)
        self.assertEqual(self.Issue._issue_thresholds(), {
            'min_sql_share': 0.5, 'min_query_ms': 1000, 'min_repeats': 50, 'min_occ_7d': 3})
        self.assertEqual(self.env['res.config.settings'].create({}).perf_issue_min_repeats, 50)
        self.env['res.config.settings'].create({'perf_issue_min_sql_share': 0.8}).execute()
        self.assertEqual(self.Issue._issue_thresholds()['min_sql_share'], 0.8)


@tagged('post_install', '-at_install')
class TestAiSwitchOff(TransactionCase):
    """The AI layer is opt-in and can be switched off again from Settings."""

    def test_ai_can_be_disabled(self):
        Issue, ICP = self.env['perf.issue'], self.env['ir.config_parameter']
        self.assertFalse(self.env.ref('perf_monitor.param_ai_enabled', raise_if_not_found=False),
                         'fresh install: no ai_enabled parameter (opt-in)')
        ICP.set_param('perf_monitor.ai_enabled', False)   # Odoo deletes the parameter
        self.assertFalse(ICP.get_param('perf_monitor.ai_enabled'))
        self.assertFalse(Issue._ai_enabled())
        self.assertFalse(self.env['res.config.settings'].create({}).perf_ai_enabled)
        self.env['res.config.settings'].create({'perf_ai_enabled': True}).execute()
        self.assertTrue(Issue._ai_enabled())
        settings = self.env['res.config.settings'].create({})
        self.assertTrue(settings.perf_ai_enabled)
        settings.perf_ai_enabled = False   # untick + Save
        settings.execute()
        self.assertFalse(ICP.get_param('perf_monitor.ai_enabled'))
        self.assertFalse(Issue._ai_enabled())
        self.assertEqual(Issue._ai_analyse_pending(), 0)


@tagged('post_install', '-at_install')
class TestExplainAndExplanation(_BuildCase):

    def test_failed_explain_keeps_real_error_and_transaction(self):
        dbname = self.env.cr.dbname
        self.server.db_name = dbname
        self._row(db_name=dbname, top_sql_calls=1, top_sql_ms=5000.0, top_sql_fingerprint='6' * 40,
                  top_sql='SELECT id FROM perf_no_such_table_xyz WHERE name = ?')
        self.Issue._build_issues()
        issue = self._issue('sql:' + '6' * 40)
        self.assertEqual(issue.issue_type, 'slow_sql')
        if self.env.cr._cnx.server_version < 160000:
            self.assertFalse(issue.explain_plan)
            return
        self.assertIn('EXPLAIN unavailable', issue.explain_plan)
        self.assertIn('perf_no_such_table_xyz', issue.explain_plan)
        self.assertNotIn('current transaction is aborted', issue.explain_plan)
        # the transaction is still usable, the statement timeout is back
        self.env.cr.execute('SELECT 1')
        self.assertEqual(self.env.cr.fetchone()[0], 1)
        self.env.cr.execute('SHOW statement_timeout')
        self.assertNotEqual(self.env.cr.fetchone()[0], '%sms' % pi.EXPLAIN_TIMEOUT_MS)
        issue.action_refresh_explain()
        self.assertIn('perf_no_such_table_xyz', issue.explain_plan)

    def test_explanation_on_issue(self):
        self._row(sql_ms=2000.0, ui_menu='Inventory/Transfers', ui_action='Receipts')
        self.Issue._build_issues()
        issue = self._issue('n1:' + '1' * 40)
        self.assertEqual(issue.sql_ms, 2000.0)
        text = issue.explanation
        for part in ("menu 'Inventory/Transfers'", "screen 'Receipts'", 'stock.picking.button_validate',
                     'Users: ali', 'database (SQL) 2.0 s (22 %)', 'N+1', 'On DB skipdb log in as ali',
                     'Roman Urdu', 'Mumkina hal'):
            self.assertIn(part, text)
        arch = self.Issue.get_view(view_type='form')['arch']
        # first tab (Overview) of the form
        self.assertLess(arch.index('name="overview"'), arch.index('name="explanation"'))
        self.assertLess(arch.index('name="explanation"'), arch.index('name="evidence"'))
        self.assertIn('name="action_skip"', arch)
