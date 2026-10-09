# -*- coding: utf-8 -*-
import importlib.util
import json
import os
from datetime import timedelta
from unittest.mock import patch

from lxml import etree

from odoo import fields
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models import perf_server as perf_server_module

MIGRATION = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'migrations', '16.0.1.5.0', 'post-migrate.py')


def _load_migration():
    spec = importlib.util.spec_from_file_location('perf_monitor_post_migrate_16_0_1_5_0', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@tagged('post_install', '-at_install')
class TestAgentSources(TransactionCase):
    """perf.server records registered for perf_monitor_agent (host 'agent:<db>', slow calls only)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Server = cls.env['perf.server']
        cls.collector = Server.create({'name': 'erp-host', 'host': '10.0.0.5', 'db_name': 'erp_db'})
        cls.agent = Server.create({'name': 'erp_db (perf_monitor_agent)', 'host': 'agent:erp_db',
                                   'db_name': 'erp_db'})
        cls.Remote = cls.env['perf.agent.slow.call.remote']

    def _call(self, server, agent_id, age, kind='http'):
        return self.Remote.create({'server_id': server.id, 'agent_id': agent_id, 'kind': kind,
                                   'ts': fields.Datetime.now() - age, 'duration_ms': 3000.0})

    def test_source_kind_assignment(self):
        self.assertEqual(self.collector.source_kind, 'collector')
        self.assertEqual(self.agent.source_kind, 'agent')
        # deploy_perf_monitor_agent.sh path
        token = self.env['perf.server']._deploy_register_local('x (perf_monitor_agent)', 'agent:x', db_name='x')
        self.assertTrue(token)
        registered = self.env['perf.server'].search([('host', '=', 'agent:x')])
        self.assertEqual(registered.source_kind, 'agent')
        # recomputed when the host changes, and still overridable by hand
        registered.host = 'db-host-1'
        self.assertEqual(registered.source_kind, 'collector')
        registered.source_kind = 'agent'
        self.assertEqual(registered.source_kind, 'agent')
        explicit = self.env['perf.server'].create({'name': 'e', 'host': 'agent:e', 'source_kind': 'collector'})
        self.assertEqual(explicit.source_kind, 'collector')
        # collectors are listed first
        servers = self.env['perf.server'].search([('id', 'in', (self.collector | self.agent).ids)])
        self.assertEqual(servers.mapped('source_kind'), ['collector', 'agent'])

    def test_migration_marks_agent_hosts(self):
        self.env['perf.server'].flush_model()
        self.env.cr.execute("UPDATE perf_server SET source_kind = 'collector', status = 'green' WHERE id IN %s",
                            ((self.collector.id, self.agent.id),))
        _load_migration().migrate(self.env.cr, '16.0.1.4.0')
        self.env['perf.server'].invalidate_model()
        self.assertEqual(self.agent.source_kind, 'agent')
        self.assertEqual(self.agent.status, 'unknown')  # never pushed: no leftover collector status
        self.assertEqual(self.collector.source_kind, 'collector')
        self.assertEqual(self.collector.status, 'green')

    def test_slow_call_counts_and_last_slow_call(self):
        self._call(self.agent, 1, timedelta(hours=1))
        self._call(self.agent, 2, timedelta(hours=2))
        self._call(self.agent, 3, timedelta(hours=2), kind='tile')  # tile rows are not calls
        self._call(self.agent, 4, timedelta(days=3))
        last = self._call(self.agent, 5, timedelta(days=10))
        latest = self._call(self.agent, 6, timedelta(minutes=5), kind='tile')
        servers = self.collector | self.agent
        self.env.flush_all()
        servers.invalidate_recordset()
        with self.assertQueryCount(1):  # one grouped query for the whole recordset
            stats = [(s.slow_calls_24h, s.slow_calls_7d, s.last_slow_call) for s in servers]
        self.assertEqual(stats[0], (0, 0, False))
        self.assertEqual(stats[1], (2, 3, latest.ts))
        self.assertTrue(last.ts < stats[1][2])

    def test_host_server(self):
        # same db_name, different names: no link (matched by Name only, db_name may repeat)
        self.assertFalse(self.agent.host_server_id)
        self.assertFalse(self.collector.host_server_id)
        # renamed to the server card name (+ " (agent)"): linked
        self.agent.name = 'erp-host (agent)'
        self.assertEqual(self.agent.host_server_id, self.collector)
        # an archived server card is not matched
        other = self.env['perf.server'].create({'name': 'erp-other (agent)', 'host': 'agent:erp_db',
                                                'db_name': 'erp_db'})
        self.collector.active = False
        self.env['perf.server'].create({'name': 'erp-archived', 'host': '10.0.0.8', 'db_name': 'erp_db',
                                        'active': False})
        other.name = 'erp-archived (agent)'
        self.assertFalse(other.host_server_id)

    def test_status_for_agent_sources(self):
        now = fields.Datetime.now()
        for age, expected in ((None, 'unknown'), (timedelta(hours=1), 'green'), (timedelta(days=2), 'yellow'),
                              (timedelta(days=8), 'unknown')):
            self.agent.last_push = now - age if age else False
            self.agent.last_seen = now  # ignored for an agent source
            self.agent._refresh_status()
            self.assertEqual(self.agent.status, expected, age)
        self.collector.last_seen = now - timedelta(minutes=20)
        self.collector.last_push = now  # ignored for a collector
        self.collector._refresh_status()
        self.assertEqual(self.collector.status, 'red')

    def test_touch_last_push(self):
        self.assertTrue(self.agent._touch_last_push())
        self.assertEqual(self.agent.status, 'green')
        first = self.agent.last_push
        self.assertFalse(self.agent._touch_last_push())  # < 60 s: no write
        later = first + timedelta(seconds=61)
        with patch.object(perf_server_module.fields.Datetime, 'now', return_value=later):
            self.assertTrue(self.agent._touch_last_push())
        self.assertEqual(self.agent.last_push, later)
        self.collector._touch_last_push()
        self.assertEqual((self.collector.status, self.collector.last_seen), ('unknown', False))

    def test_cron_skips_rightsize_for_agents(self):
        self.agent.rightsize_recommendation = False
        self.env['perf.server']._cron_refresh()
        self.assertFalse(self.agent.rightsize_recommendation)
        self.assertTrue(self.collector.rightsize_recommendation)

    def test_sql_row_without_text(self):
        ts = fields.Datetime.to_string(fields.Datetime.now() - timedelta(minutes=1))
        self.collector._ingest_payload({'version': 1, 'sql': [
            {'ts': ts, 'queryid': '1', 'query': '', 'query_unavailable': True, 'calls': 5},
            {'ts': ts, 'queryid': '2', 'query': 'SELECT 1', 'calls': 5}]})
        rows = self.env['perf.sql.snapshot'].search([('server_id', '=', self.collector.id)], order='queryid')
        self.assertEqual(rows.mapped('query'), ['(query text unavailable)', 'SELECT ?'])

    def test_open_slow_calls(self):
        action = self.agent.action_open_slow_calls()
        self.assertEqual(action['res_model'], 'perf.agent.slow.call.remote')
        self.assertEqual(action['domain'], [('server_id', '=', self.agent.id)])
        search = self.env['perf.agent.slow.call.remote'].get_views([(False, 'search')])['views']['search']
        self.assertTrue(etree.fromstring(search['arch']).xpath("//filter[@name='calls']"))

    def test_views(self):
        views = self.env['perf.server'].get_views([(False, 'kanban'), (False, 'list'), (False, 'form'),
                                                   (False, 'search')])['views']
        kanban = etree.fromstring(views['kanban']['arch'])
        declared = {n.get('name') for n in kanban.xpath('/kanban/field')}
        used = set()
        for node in kanban.iter():
            for attr in ('t-if', 't-elif', 't-esc', 't-out', 't-attf-class', 't-att-title'):
                expr = node.get(attr) or ''
                used.update(part.split('.')[0] for part in expr.split('record.')[1:])
        used |= {n.get('name') for n in kanban.xpath('//templates//field')}
        self.assertFalse(used - declared, 'kanban fields used but not declared')
        self.assertTrue(kanban.xpath("//t[@t-if=\"record.source_kind.raw_value == 'agent'\"]"))
        agent_card = kanban.xpath("//div[contains(@t-attf-class, 'o_perf_agent_card')]")[0]
        agent_text = etree.tostring(agent_card).decode()
        for name in ('slow_calls_24h', 'slow_calls_7d', 'last_slow_call', 'last_push', 'host_server_id'):
            self.assertIn(name, agent_text)
        for name in ('cpu_pct', 'ram_pct', 'disk_pct', 'db_latency_ms', 'rightsize_recommendation',
                     'instance_type'):
            self.assertNotIn(name, agent_text)
        collector_card = kanban.xpath("//t[@t-if=\"record.source_kind.raw_value == 'agent'\"]"
                                      "/following-sibling::t[@t-else][1]")[0]
        self.assertIn('cpu_pct', etree.tostring(collector_card).decode())
        tree = etree.fromstring(views['list']['arch'])
        self.assertTrue(tree.xpath("//field[@name='source_kind']"))
        self.assertIn('agent', tree.xpath("//field[@name='cpu_pct']")[0].get('modifiers'))
        form = etree.fromstring(views['form']['arch'])
        self.assertTrue(form.xpath("//button[@name='action_open_slow_calls']"))
        self.assertTrue(form.xpath("//button[@name='action_regenerate_token']"))
        search = etree.fromstring(views['search']['arch'])
        self.assertTrue(search.xpath("//filter[@name='group_source_kind']"))
        self.assertTrue(search.xpath("//filter[@name='agents']"))
        # what the kanban reads
        fields_list = sorted(declared)
        rows = self.env['perf.server'].web_search_read([('id', 'in', (self.collector | self.agent).ids)],
                                                       fields_list)['records']
        self.assertEqual([r['source_kind'] for r in rows], ['collector', 'agent'])


@tagged('post_install', '-at_install')
class TestAgentPushLastPush(HttpCase):

    def setUp(self):
        super().setUp()
        self.collector = self.env['perf.server'].create({'name': 'c', 'host': 'h', 'db_name': 'src'})
        self.agent = self.env['perf.server'].create({'name': 'a', 'host': 'agent:src', 'db_name': 'src'})
        self.token = self.agent._set_new_token()
        self.env['ir.config_parameter'].set_param('perf_monitor.max_pushes_per_minute', 0)
        self.authenticate(None, None)

    def _push(self, body):
        return self.url_open('/perf_monitor/api/v1/push_slow_calls', data=json.dumps(body),
                             headers={'Content-Type': 'application/json', 'X-Perf-Token': self.token})

    def test_push_updates_last_push_and_status(self):
        self.assertEqual(self.agent.status, 'unknown')
        resp = self._push({'version': 1, 'db_name': 'src', 'calls': []})  # 0 rows still counts
        self.assertEqual(resp.status_code, 200, resp.text)
        self.agent.invalidate_recordset()
        self.assertTrue(self.agent.last_push)
        self.assertEqual(self.agent.status, 'green')
        self.assertFalse(self.agent.last_seen)
        first = self.agent.last_push
        self._push({'version': 1, 'db_name': 'src', 'calls': []})
        self.agent.invalidate_recordset()
        self.assertEqual(self.agent.last_push, first)  # rewritten at most once a minute
        self.collector.invalidate_recordset()
        self.assertEqual((self.collector.last_seen, self.collector.status, self.collector.last_push),
                         (False, 'unknown', False))
        # a refused payload does not count as a push
        self.agent.sudo().write({'last_push': False, 'status': 'unknown'})
        self.env.flush_all()
        self.assertEqual(self._push({'version': 2}).status_code, 400)
        self.agent.invalidate_recordset()
        self.assertFalse(self.agent.last_push)
