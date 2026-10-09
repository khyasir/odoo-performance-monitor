# -*- coding: utf-8 -*-
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tools.safe_eval import safe_eval

from ..models import perf_issue as pi


@tagged('post_install', '-at_install')
class TestIssueRules(TransactionCase):
    """Pure rules: classification, fingerprint, severity, EXPLAIN text."""

    def test_classify(self):
        n1 = {'kind': 'rpc', 'duration_ms': 243525, 'model_name': 'closing.wags', 'method': 'get_closing_date',
              'top_sql_fingerprint': 'a' * 40, 'top_sql_calls': 36710, 'top_sql_ms': 20,
              'top_sql_callsite': 'closing_wags/models/closing_wags.py:1698 (detrmine_levels)'}
        itype, key, title = pi.classify_call(n1)
        self.assertEqual((itype, key), ('n_plus_1', 'n1:' + 'a' * 40))
        self.assertIn('closing_wags.py:1698', title)
        self.assertIn('36710', title)
        # wags_logger's cursor patch is never named as the call site
        wl = dict(n1, top_sql_callsite='wags_logger/models/wags_logger.py:525 (_patched_cursor_execute)')
        self.assertNotIn('wags_logger', pi.classify_call(wl)[2])
        tile = {'kind': 'tile', 'duration_ms': 100, 'tile_ms': 192169, 'ui_dashboard': 'BOD OD',
                'ui_tile': 'Retail Monthly Sales', 'model_name': 'dashboards.saas', 'method': 'get_all_datasets_saas',
                'top_sql_fingerprint': 'b' * 40, 'top_sql_calls': 1}
        self.assertEqual(pi.classify_call(tile)[:2], ('slow_tile', 'tile:BOD OD|Retail Monthly Sales'))
        # the call row of a multi-tile call is covered by its tile rows
        call = dict(tile, kind='rpc', duration_ms=802042, tile_count=12)
        self.assertIsNone(pi.classify_call(call))
        # a single-tile refresh call row names its tile
        self.assertEqual(pi.classify_call(dict(call, tile_count=1))[0], 'slow_tile')
        slow_sql = {'kind': 'rpc', 'duration_ms': 5000, 'top_sql_ms': 4000, 'top_sql_fingerprint': 'c' * 40,
                    'model_name': 'sale.order', 'method': 'read'}
        self.assertEqual(pi.classify_call(slow_sql)[:2], ('slow_sql', 'sql:' + 'c' * 40))
        method = dict(slow_sql, top_sql_ms=100)
        self.assertEqual(pi.classify_call(method)[:2], ('slow_method', 'method:sale.order.read'))
        cron = {'kind': 'cron', 'duration_ms': 9000, 'cron_name': 'Mail: queue'}
        self.assertEqual(pi.classify_call(cron)[:2], ('slow_cron', 'cron:Mail: queue'))
        route = {'kind': 'http', 'duration_ms': 9000, 'route': '/web/image'}
        self.assertEqual(pi.classify_call(route)[:2], ('slow_route', 'route:/web/image'))
        # under the threshold / perf_monitor's own calls: no issue
        self.assertIsNone(pi.classify_call(dict(method, duration_ms=1999)))
        self.assertIsNone(pi.classify_call(dict(method, model_name='perf.server')))
        self.assertIsNone(pi.classify_call({'kind': 'http', 'duration_ms': 9000, 'route': '/perf_monitor/api/v1/push'}))
        self.assertIsNone(pi.classify_call({'kind': 'http', 'duration_ms': 9000}))
        self.assertEqual(pi.issue_fingerprint('x'), pi.issue_fingerprint('x'))
        self.assertNotEqual(pi.issue_fingerprint('x'), pi.issue_fingerprint('y'))

    def test_severity(self):
        sev, score, reasons = pi.severity_for(243525, 1, 140132, 36710, 0)
        self.assertEqual((sev, score), ('critical', 8))
        self.assertTrue(any('N+1' in r for r in reasons))
        self.assertEqual(pi.severity_for(2500)[0], 'low')
        self.assertEqual(pi.severity_for(12000, 12)[0], 'medium')          # 2 + 1
        self.assertEqual(pi.severity_for(192169, 1, 4, 1, 2_000_000)[0], 'high')  # 3 + 2
        self.assertEqual(pi.severity_for(61000, 60, 0, 0, 0)[1], 5)
        self.assertEqual(pi.severity_for(5000, 0, 0, 150, 150000)[1], 1 + 2 + 1)

    def test_explainable_sql(self):
        self.assertEqual(pi.explainable_sql('SELECT id FROM t WHERE a = ? AND b IN (?) AND c = %s'),
                         'SELECT id FROM t WHERE a = $1 AND b IN ($2) AND c = $3')
        self.assertEqual(pi.explainable_sql('WITH x AS (SELECT 1) SELECT * FROM x WHERE y = $1;'),
                         'WITH x AS (SELECT 1) SELECT * FROM x WHERE y = $1')
        self.assertIsNone(pi.explainable_sql('UPDATE t SET a = ?'))
        self.assertIsNone(pi.explainable_sql('WITH d AS (DELETE FROM t RETURNING id) SELECT * FROM d'))
        self.assertIsNone(pi.explainable_sql(''))
        self.assertEqual(pi.explainable_sql('select create_date from t'), 'select create_date from t')

    def test_plan_summary(self):
        plan = [{'Plan': {'Node Type': 'Hash Join', 'Plan Rows': 10, 'Total Cost': 99.5, 'Hash Cond': '(a.id = b.x)',
                          'Plans': [{'Node Type': 'Seq Scan', 'Relation Name': 'pos_order', 'Plan Rows': 5,
                                     'Total Cost': 50, 'Filter': "(state = 'paid'::text)"},
                                    {'Node Type': 'Index Scan', 'Relation Name': 'res_partner',
                                     'Index Name': 'res_partner_pkey', 'Plan Rows': 1, 'Total Cost': 1}]}}]
        text = pi.plan_summary(plan)
        self.assertIn('Seq Scan on pos_order', text)
        self.assertIn('using res_partner_pkey', text)
        self.assertNotIn('paid', text)  # literals stripped
        self.assertEqual(len(list(pi.plan_nodes(plan))), 3)
        self.assertEqual(pi._merge_text('a, b', ['b', 'c', False]), 'a, b, c')


@tagged('post_install', '-at_install')
class TestIssueBuild(TransactionCase):
    """perf.issue built from perf.agent.slow.call.remote rows."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = cls.env['perf.server'].create({'name': 'agent', 'host': 'agent:db1', 'db_name': 'db1'})
        cls.Remote = cls.env['perf.agent.slow.call.remote']
        cls.Issue = cls.env['perf.issue']
        # rows from earlier tests / the DB must not interfere
        cls.Remote.search([]).write({'issue_checked': True})
        cls.env['ir.config_parameter'].set_param('perf_monitor.ai_enabled', 'False')
        cls.next_id = 1

    def _row(self, age_min=5, db='db1', **kw):
        type(self).next_id += 1
        vals = {'server_id': self.server.id, 'agent_id': self.next_id, 'db_name': db, 'kind': 'rpc',
                'ts': fields.Datetime.now() - timedelta(minutes=age_min), 'duration_ms': 243525.0,
                'model_name': 'closing.wags', 'method': 'get_closing_date', 'login': 'admin', 'sql_count': 140132,
                'top_sql_calls': 36710, 'top_sql_ms': 25.0, 'top_sql_fingerprint': 'e' * 40,
                'top_sql': 'SELECT id FROM account_move_line WHERE move_id = %s',
                'top_sql_callsite': 'closing_wags/models/closing_wags.py:1698 (detrmine_levels)',
                'ui_action': 'Closing Wags', 'ui_menu': 'Accounting/Closings'}
        vals.update(kw)
        return self.Remote.create(vals)

    def _issue(self, key):
        return self.Issue.search([('fingerprint', '=', pi.issue_fingerprint(key))])

    def test_build_dedupe_tags_severity(self):
        rows = self._row() | self._row(db='db2') | self._row(age_min=1, db='db2', login='ali')
        tiles = (self._row(kind='tile', ui_dashboard='BOD OD', ui_tile='Retail Monthly Sales', tile_ms=192169.0,
                           duration_ms=802042.0, top_sql_calls=1, sql_count=4, top_sql_ms=190000.0,
                           top_sql_fingerprint='f' * 40, model_name='dashboards.saas', method='get_all_datasets_saas')
                 | self._row(kind='rpc', ui_dashboard='BOD OD', ui_tile='Retail Monthly Sales', tile_count=20,
                             model_name='dashboards.saas', method='get_all_datasets_saas', top_sql_calls=1,
                             duration_ms=802042.0))
        fast = self._row(duration_ms=500.0)
        handled = self.Issue._build_issues()
        self.assertEqual(handled, 6)
        n1 = self._issue('n1:' + 'e' * 40)
        self.assertEqual(len(n1), 1, 'one task for the three N+1 calls')
        self.assertEqual(n1.issue_type, 'n_plus_1')
        self.assertEqual(sorted(n1.tag_ids.mapped('name')), ['db1', 'db2'])
        self.assertEqual(self.env['perf.issue.tag'].search_count([('name', '=', 'db2')]), 1)
        self.assertEqual((n1.occurrence_count, n1.max_repeats, n1.max_sql_count), (3, 36710, 140132))
        self.assertEqual(n1.severity, 'critical')
        self.assertIn('N+1', n1.severity_reasons)
        self.assertEqual(n1.slow_call_ids, rows)
        self.assertEqual(n1.users, 'admin, ali')
        self.assertEqual((n1.state, n1.ai_state), ('new', 'pending'))
        self.assertIn('closing_wags.py:1698', n1.callsite)
        tile = self._issue('tile:BOD OD|Retail Monthly Sales')
        self.assertEqual((tile.issue_type, tile.occurrence_count, tile.dashboard), ('slow_tile', 1, 'BOD OD'))
        self.assertEqual(tile.max_duration_ms, 192169.0)
        self.assertFalse(fast.issue_id)
        self.assertTrue(fast.issue_checked)
        # a second build does nothing; new rows only update the same issue
        self.assertEqual(self.Issue._build_issues(), 0)
        self._row(db='db3')
        self.Issue._build_issues()
        self.assertEqual(n1.occurrence_count, 4)
        self.assertEqual(sorted(n1.tag_ids.mapped('name')), ['db1', 'db2', 'db3'])
        self.assertEqual(self.Issue.search_count([('fingerprint', '=', n1.fingerprint)]), 1)

    def test_reopen_and_never_auto_close(self):
        self._row(age_min=60 * 24 * 8)  # 8 days ago
        self.Issue._build_issues()
        issue = self._issue('n1:' + 'e' * 40)
        self.assertEqual(issue.last_seen_hint, 'Last seen 8 days ago')
        self.Issue._build_issues()
        self.assertEqual(issue.state, 'new', 'never closed automatically')
        issue.action_close()
        self.assertEqual(issue.state, 'closed')
        self.assertTrue(issue.closed_date)
        # an older row pushed late does not reopen it
        issue.closed_date = fields.Datetime.now() - timedelta(minutes=30)
        self._row(age_min=60)
        self.Issue._build_issues()
        self.assertEqual(issue.state, 'closed')
        # it happens again after the close: same task reopens, new DB tag added
        self._row(age_min=1, db='db_new')
        self.Issue._build_issues()
        self.assertEqual(issue.state, 'open')
        self.assertEqual(issue.reopen_count, 1)
        self.assertIn('db_new', issue.tag_ids.mapped('name'))
        self.assertFalse(issue.last_seen_hint)
        self.assertTrue(any('Reopened' in (m.body or '') for m in issue.message_ids))
        self.assertEqual(self.Issue.search_count([('key', '=', issue.key)]), 1)

    def test_wags_logger_callsite_is_skipped(self):
        wl = 'wags_logger/models/wags_logger.py:525 (_patched_cursor_execute)'
        real = 'closing_wags/models/closing_wags.py:1698 (detrmine_levels)'
        # the slowest call only names the cursor patch; another call of the issue has the real frame
        self._row(top_sql_callsite=wl, duration_ms=300000.0)
        self._row(top_sql_callsite='%s <- %s' % (wl, real), duration_ms=250000.0)
        self.Issue._build_issues()
        issue = self._issue('n1:' + 'e' * 40)
        self.assertEqual(issue.callsite, real)
        self.assertNotIn('wags_logger', issue.name)
        self.assertIn(real, issue._ai_context()['where'])
        # only the cursor patch anywhere: no call site (never a wrong one), no wrong code for the AI
        self._row(top_sql_callsite=wl, top_sql_fingerprint='9' * 40)
        self.Issue._build_issues()
        only = self._issue('n1:' + '9' * 40)
        self.assertFalse(only.callsite)
        self.assertNotIn('wags_logger', only.name)
        # an issue stored before 16.0.1.12.0 with the cursor patch as call site: no code is read for it
        only.callsite = wl
        self.assertEqual(only._ai_code(), '')
        self.assertNotIn('wags_logger', only._ai_involved_modules())
        # a later call with a real frame fixes the call site of the issue
        self._row(top_sql_callsite=real, top_sql_fingerprint='9' * 40, duration_ms=3000.0)
        self.Issue._build_issues()
        self.assertEqual(only.callsite, real)

    def test_history_and_age(self):
        self._row()
        self.Issue._build_issues()
        issue = self._issue('n1:' + 'e' * 40)
        self.env.flush_all()
        self.env.cr.precommit.run()   # tracking of the creation
        self.assertTrue(issue.message_ids.filtered(lambda m: 'Issue created by the rules' in (m.body or '')))
        before = issue.message_ids
        # a recurring occurrence is not logged: at most a tracking note when it changes a tracked field (the
        # severity rules: here the score stays critical, count_7d 2 < 10), never a message of its own
        self._row(age_min=1)
        self.Issue._build_issues()
        self.env.flush_all()
        self.env.cr.precommit.run()
        self.assertEqual(issue.occurrence_count, 2)
        added = issue.message_ids - before
        self.assertFalse(added.filtered(lambda m: not m.tracking_value_ids), 'occurrences are not logged')
        self.assertFalse(added.tracking_value_ids.filtered(lambda t: t.field.name != 'severity'),
                         'only a severity change may be tracked')
        # state / assignee changes: tracked (old -> new, author), notes only
        other = self.env['res.users'].create({'name': 'Other perf', 'login': 'perf_history_other'})
        issue.action_close()
        issue.user_id = other
        self.env.flush_all()
        self.env.cr.precommit.run()
        tracked = issue.message_ids.tracking_value_ids
        state = tracked.filtered(lambda t: t.field.name == 'state')
        self.assertEqual((state.old_value_char, state.new_value_char), ('New', 'Closed'))
        self.assertTrue(tracked.filtered(lambda t: t.field.name == 'user_id'))
        self.assertFalse(issue.message_ids.filtered(lambda m: m.message_type == 'email'))
        self.assertFalse(self.env['mail.notification'].sudo().search([
            ('mail_message_id', 'in', issue.message_ids.ids), ('notification_type', '=', 'email')]))
        # age in days (list: "Created" sortable + "Age (days)")
        self.assertEqual(issue.age_days, 0)
        self.env.cr.execute("UPDATE perf_issue SET create_date = now() at time zone 'UTC' - interval '10 days' "
                            "WHERE id = %s", (issue.id,))
        issue.invalidate_recordset(['create_date', 'age_days'])
        self.assertEqual(issue.age_days, 10)

    def test_viewer_cannot_close(self):
        self._row()
        self.Issue._build_issues()
        issue = self._issue('n1:' + 'e' * 40)
        viewer = self.env['res.users'].create({
            'name': 'viewer', 'login': 'perf_viewer_issue',
            'groups_id': [(6, 0, [self.env.ref('perf_monitor.group_perf_viewer').id])]})
        with self.assertRaises(UserError):
            issue.with_user(viewer).action_close()
        self.assertTrue(issue.with_user(viewer).read(['name', 'severity']))

    def test_explain_seq_scan_on_local_db(self):
        """EXPLAIN (GENERIC_PLAN) runs only for this DB; a big seq-scanned table raises the severity."""
        dbname = self.env.cr.dbname
        self._row(db=dbname, top_sql_calls=1, top_sql_ms=200000.0, top_sql_fingerprint='d' * 40,
                  top_sql='SELECT id FROM res_partner WHERE name = ?')
        with patch.object(type(self.Issue), '_table_rows', lambda self, tables: {t: 5_000_000 for t in tables}):
            self.Issue._build_issues()
        issue = self._issue('sql:' + 'd' * 40)
        self.assertEqual(issue.issue_type, 'slow_sql')
        if self.env.cr._cnx.server_version >= 160000:
            self.assertIn('res_partner', issue.explain_plan)
            if 'Seq Scan on res_partner' in issue.explain_plan:
                self.assertIn('res_partner', issue.seq_scan_tables)
                self.assertIn('seq scan', issue.severity_reasons)
        else:
            self.assertFalse(issue.explain_plan)
        # another DB: never EXPLAINed here
        self._row(db='elsewhere', top_sql_calls=1, top_sql_ms=200000.0, top_sql_fingerprint='9' * 40)
        self.Issue._build_issues()
        self.assertFalse(self._issue('sql:' + '9' * 40).explain_plan)

    def test_build_safe_never_raises(self):
        self._row()
        with patch.object(type(self.Issue), '_upsert_from_calls', side_effect=RuntimeError('boom')):
            self.assertEqual(self.Issue._build_issues_safe(), 0)
        self.assertEqual(self.Issue._build_issues(), 1)  # rows kept for the next build


@tagged('post_install', '-at_install')
class TestIssueAssignee(TransactionCase):
    """Issues assigned to the Settings "Issue assignee" (default Yasir, else Administrator) with an
    Odoo To-Do activity, never an e-mail; "My Issues" shows only the user's issues."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['perf.agent.slow.call.remote'].search([]).write({'issue_checked': True})
        cls.ICP = cls.env['ir.config_parameter']
        cls.ICP.set_param('perf_monitor.ai_enabled', 'False')
        cls.ICP.set_param(pi.ASSIGNEE_PARAM, '')
        viewer = cls.env.ref('perf_monitor.group_perf_viewer')
        cls.yasir = cls.env['res.users'].search([('login', 'ilike', 'yasir'), ('share', '=', False)],
                                                order='id', limit=1)
        if not cls.yasir:
            cls.yasir = cls.env['res.users'].create({'name': 'Yasir Rauf', 'login': 'yasir.perf.test'})
        cls.yasir.groups_id = [(4, viewer.id)]
        cls.other = cls.env['res.users'].create({'name': 'Other Dev', 'login': 'perf_other_dev',
                                                 'groups_id': [(6, 0, [viewer.id])]})
        cls.server = cls.env['perf.server'].create({'name': 'agent', 'host': 'agent:dba', 'db_name': 'dba'})

    def _row(self, age_min=5, fp='5' * 40):
        return self.env['perf.agent.slow.call.remote'].create({
            'server_id': self.server.id, 'agent_id': 1, 'db_name': 'dba', 'kind': 'rpc',
            'ts': fields.Datetime.now() - timedelta(minutes=age_min), 'duration_ms': 9000.0,
            'model_name': 'closing.wags', 'method': 'get_closing_date', 'sql_count': 3000,
            'top_sql_calls': 500, 'top_sql_ms': 10.0, 'top_sql_fingerprint': fp})

    def _mails(self):
        return self.env['mail.mail'].sudo().search_count([])

    def test_default_assignee_is_stored(self):
        self.assertEqual(self.env['perf.issue']._issue_assignee(), self.yasir)
        self.assertEqual(self.ICP.get_param(pi.ASSIGNEE_PARAM), str(self.yasir.id))
        settings = self.env['res.config.settings'].create({})
        self.assertEqual(settings.perf_issue_assignee_id, self.yasir)
        # nobody named like the hint: Administrator
        self.ICP.set_param(pi.ASSIGNEE_PARAM, '')
        with patch.object(pi, 'DEFAULT_ASSIGNEE_HINT', 'zz-no-such-user-zz'):
            self.assertEqual(self.env['perf.issue']._issue_assignee(), self.env.ref('base.user_admin'))
        # an archived assignee falls back to the default again
        self.ICP.set_param(pi.ASSIGNEE_PARAM, str(self.other.id))
        self.assertEqual(self.env['perf.issue']._issue_assignee(), self.other)
        self.other.active = False
        self.assertEqual(self.env['perf.issue']._issue_assignee(), self.yasir)

    def test_new_and_reopened_issue_activity_no_mail(self):
        mails = self._mails()
        self._row()
        self.env['perf.issue']._build_issues()
        issue = self.env['perf.issue'].search([('fingerprint', '=', pi.issue_fingerprint('n1:' + '5' * 40))])
        self.assertEqual(issue.user_id, self.yasir)
        todo = self.env.ref('mail.mail_activity_data_todo')
        self.assertEqual(len(issue.activity_ids), 1)
        self.assertEqual((issue.activity_ids.user_id, issue.activity_ids.activity_type_id), (self.yasir, todo))
        self.assertIn(issue.name, issue.activity_ids.note)
        self._row(age_min=1)
        self.env['perf.issue']._build_issues()
        self.assertEqual(len(issue.activity_ids), 1, 'no second activity while one is open')
        issue.action_close()
        self.assertFalse(issue.activity_ids)
        # reopened: the configured assignee is kept, a new activity is scheduled
        issue.closed_date = fields.Datetime.now() - timedelta(minutes=30)
        self._row(age_min=1)
        self.env['perf.issue']._build_issues()
        self.assertEqual((issue.state, issue.user_id), ('open', self.yasir))
        self.assertEqual(issue.activity_ids.summary, 'Reopened performance issue')
        self.assertEqual(self._mails(), mails, 'activities only: never an e-mail')
        self.assertFalse(self.env['mail.notification'].sudo().search([
            ('mail_message_id', 'in', issue.message_ids.ids), ('notification_type', '=', 'email')]))
        self.assertFalse(issue.message_ids.filtered(lambda m: m.message_type == 'email'))

    def test_configured_assignee_and_backfill(self):
        self.ICP.set_param(pi.ASSIGNEE_PARAM, str(self.other.id))
        self._row(fp='6' * 40)
        self.env['perf.issue']._build_issues()
        issue = self.env['perf.issue'].search([('fingerprint', '=', pi.issue_fingerprint('n1:' + '6' * 40))])
        self.assertEqual(issue.user_id, self.other)
        # issues built before 16.0.1.7.0 have no assignee: the build cron assigns them
        issue.activity_ids.unlink()
        issue.user_id = False
        self.env['perf.issue']._cron_build_issues()
        self.assertEqual(issue.user_id, self.other)
        self.assertEqual(issue.activity_ids.user_id, self.other)

    def test_my_issues(self):
        self.ICP.set_param(pi.ASSIGNEE_PARAM, str(self.other.id))
        self._row(fp='3' * 40)
        self.env['perf.issue']._build_issues()
        self.ICP.set_param(pi.ASSIGNEE_PARAM, str(self.yasir.id))
        self._row(fp='4' * 40)
        self.env['perf.issue']._build_issues()
        action = self.env['ir.actions.act_window']._for_xml_id('perf_monitor.action_perf_issue_my')
        ctx = safe_eval(action['context'])
        self.assertEqual((ctx.get('search_default_my_issues'), ctx.get('search_default_not_closed')), (1, 1))
        self.assertEqual(self.env.ref('perf_monitor.menu_perf_my_issues').action, self.env.ref(
            'perf_monitor.action_perf_issue_my'))
        self.assertTrue(self.env.ref('perf_monitor.menu_perf_issues').action, 'full Issues list kept')
        arch = self.env['perf.issue'].get_view(view_type='search')['arch']
        self.assertIn('name="my_issues"', arch)
        self.assertIn("[('user_id', '=', uid)]", arch)
        Issue = self.env['perf.issue'].with_user(self.yasir)
        mine = Issue.search([('user_id', '=', self.yasir.id), ('tag_ids.name', '=', 'dba')])
        self.assertEqual(mine.mapped('key'), ['n1:' + '4' * 40])
        self.assertEqual(len(Issue.search([('tag_ids.name', '=', 'dba')])), 2, 'all issues still readable')


@tagged('post_install', '-at_install')
class TestIssueOnPush(HttpCase):
    def test_push_builds_issues(self):
        server = self.env['perf.server'].create({'name': 'a', 'host': 'agent:x', 'db_name': 'x'})
        token = server._set_new_token()
        self.env['ir.config_parameter'].set_param('perf_monitor.max_pushes_per_minute', 0)
        self.env['perf.agent.slow.call.remote'].search([]).write({'issue_checked': True})
        call = {'id': 1, 'ts': fields.Datetime.to_string(fields.Datetime.now()), 'duration_ms': 9000, 'kind': 'rpc',
                'model_name': 'stock.picking', 'method': 'button_validate', 'sql_count': 3000, 'top_sql_calls': 500,
                'top_sql_ms': 10, 'top_sql': 'SELECT 1 FROM stock_move WHERE id = %s', 'top_sql_fingerprint': '7' * 40}
        resp = self.url_open('/perf_monitor/api/v1/push_slow_calls', headers={
            'Content-Type': 'application/json', 'X-Perf-Token': token},
            data=json.dumps({'version': 1, 'db_name': 'x', 'calls': [call]}))
        self.assertEqual(resp.json()['accepted'], 1)
        issue = self.env['perf.issue'].search([('fingerprint', '=', pi.issue_fingerprint('n1:' + '7' * 40))])
        self.assertEqual(issue.issue_type, 'n_plus_1')
        self.assertEqual(issue.tag_ids.mapped('name'), ['x'])
