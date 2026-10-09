# -*- coding: utf-8 -*-
import ast
from datetime import date

from dateutil.relativedelta import relativedelta
from lxml import etree

from odoo.modules.module import get_manifest
from odoo.tests import TransactionCase, tagged
from odoo.tools import file_open
from odoo.tools.safe_eval import safe_eval

# Odoo 16 web client (web/static/src/search/utils/dates.js INTERVAL_OPTIONS).
# Any other interval (e.g. 'hour', added in 17) makes getGroupBy() throw
# "Invalid groupBy description" and the whole action fails to open.
ODOO16_INTERVALS = {'year', 'quarter', 'month', 'week', 'day'}
PERF_MODELS = ('perf.metric', 'perf.sql.snapshot', 'perf.live.query', 'perf.agent.slow.call.remote')


@tagged('post_install', '-at_install')
class TestPerfViews(TransactionCase):

    def _group_bys(self, model):
        """Yield (source, 'field[:interval]') for every group-by the web client will parse."""
        views = self.env[model].get_views([(False, 'list'), (False, 'graph'), (False, 'pivot'),
                                           (False, 'search')])['views']
        for vtype, view in views.items():
            arch = etree.fromstring(view['arch'])
            for node in arch.iter('filter'):
                ctx = node.get('context')
                if ctx and 'group_by' in ctx:
                    gb = ast.literal_eval(ctx)['group_by']
                    for spec in (gb if isinstance(gb, list) else [gb]):
                        yield '%s %s' % (vtype, node.get('name')), spec
            if vtype in ('graph', 'pivot'):
                for node in arch.iter('field'):
                    if node.get('interval'):
                        yield '%s %s' % (vtype, node.get('name')), '%s:%s' % (node.get('name'),
                                                                          node.get('interval'))

    def test_group_by_intervals_supported_by_odoo16_client(self):
        for model in PERF_MODELS:
            fields_info = self.env[model].fields_get()
            for source, spec in self._group_bys(model):
                fname, _sep, interval = spec.partition(':')
                self.assertIn(fname, fields_info, '%s: %s' % (model, source))
                if interval:
                    self.assertIn(interval, ODOO16_INTERVALS,
                                  '%s %s: group-by %r is not supported by the Odoo 16 web client'
                                  % (model, source, spec))

    def test_metric_list_opens_with_default_filter(self):
        """Same calls as the Metrics menu: action -> list view -> web_search_read with last_24h."""
        action = self.env.ref('perf_monitor.action_perf_metric')
        self.assertEqual(action.view_mode.split(',')[0], 'tree')
        views = self.env['perf.metric'].get_views([(False, 'list'), (False, 'search')])['views']
        search = etree.fromstring(views['search']['arch'])
        domain = safe_eval(search.xpath("//filter[@name='last_24h']")[0].get('domain'),
                           {'context_today': date.today, 'relativedelta': relativedelta})
        list_fields = [n.get('name') for n in etree.fromstring(views['list']['arch']).iter('field')]
        self.env['perf.metric'].web_search_read(domain, list_fields, limit=80)
        self.env['perf.metric'].web_read_group(domain, ['cpu_pct', 'ram_pct'], ['ts:day'])

    def test_issue_form_ai_model_next_to_analyse_button(self):
        """The AI model dropdown sits in the AI advice tab next to the Analyse button, once (manager)."""
        self.env.user.groups_id |= self.env.ref('perf_monitor.group_perf_manager')
        arch = etree.fromstring(self.env['perf.issue'].get_views([(False, 'form')])['views']['form']['arch'])
        page = arch.xpath("//page[@name='ai']")[0]
        self.assertEqual(len(page.xpath(".//field[@name='ai_model_id']")), 1)
        self.assertTrue(page.xpath(".//button[@name='action_ai_reanalyse']"))

    def test_issue_form_chat_pane(self):
        """The AI Chat tab loads (manager): composer, round Send object button, chat assets in the manifest."""
        self.env.user.groups_id |= self.env.ref('perf_monitor.group_perf_manager')
        arch = etree.fromstring(self.env['perf.issue'].get_views([(False, 'form')])['views']['form']['arch'])
        page = arch.xpath("//page[@name='ai_chat']")[0]
        self.assertTrue(page.xpath(".//div[contains(@class, 'o_perf_chat_panel')]"
                                   "//div[contains(@class, 'o_perf_chat_scroll')]//field[@name='ai_chat_html']"))
        draft = page.xpath(".//div[contains(@class, 'o_perf_chat_inputbox')]/field[@name='ai_chat_draft']")[0]
        self.assertIn('o_perf_chat_composer', draft.get('class'))
        send = page.xpath(".//div[contains(@class, 'o_perf_chat_inputbox')]/button[@name='action_ai_chat_send']")[0]
        self.assertEqual(send.get('type'), 'object')
        for name in ('action_ai_chat_new', 'ai_chat_session_id', 'ai_chat_model_id', 'ai_chat_use_history',
                     'ai_chat_usage'):
            self.assertTrue(page.xpath(".//*[@name='%s']" % name), name)
        manifest = get_manifest('perf_monitor')
        for path in ('perf_monitor/static/src/scss/perf_chat.scss', 'perf_monitor/static/src/js/perf_chat.js'):
            self.assertIn(path, manifest['assets']['web.assets_backend'])
            with file_open(path) as fh:
                self.assertTrue(fh.read())
