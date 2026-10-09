# -*- coding: utf-8 -*-
from datetime import timedelta

from lxml import etree

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

from ..models.perf_server import base_server_name


@tagged('post_install', '-at_install')
class TestServerIdentity(TransactionCase):
    """Server cards are identified by their unique Name; db_name may repeat across hosts."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Server = cls.env['perf.server']
        # two hosts serving a database with the same name
        cls.host_a = cls.Server.create({'name': 'step-a', 'host': '10.0.1.1', 'db_name': 'demo_db'})
        cls.host_b = cls.Server.create({'name': 'step-b', 'host': '10.0.1.2', 'db_name': 'demo_db'})

    def test_name_unique(self):
        with self.assertRaises(ValidationError):
            self.Server.create({'name': 'step-a', 'host': '10.0.1.3', 'db_name': 'other'})
        with self.assertRaises(ValidationError):
            self.host_b.name = 'step-a'
        archived = self.Server.create({'name': 'old', 'host': 'h', 'db_name': 'd', 'active': False})
        with self.assertRaises(ValidationError):  # archived cards keep their name
            self.Server.create({'name': archived.name, 'host': 'h2', 'db_name': 'd'})
        # same db_name, different names: fine
        self.Server.create({'name': 'step-c', 'host': '10.0.1.3', 'db_name': 'demo_db'})

    def test_base_server_name(self):
        self.assertEqual(base_server_name('step-a (agent)'), 'step-a')
        self.assertEqual(base_server_name('db (perf_monitor_agent)'), 'db')
        self.assertEqual(base_server_name(' step-a '), 'step-a')

    def test_host_link_by_name_not_db_name(self):
        agent_a = self.Server.create({'name': 'step-a (agent)', 'host': 'agent:demo_db',
                                      'db_name': 'demo_db'})
        agent_b = self.Server.create({'name': 'step-b (agent)', 'host': 'agent:demo_db',
                                      'db_name': 'demo_db'})
        self.assertEqual(agent_a.host_server_id, self.host_a)
        self.assertEqual(agent_b.host_server_id, self.host_b)  # not stolen by host_a (same db_name)
        # a name that matches nothing: no link, even with the same db_name
        lone = self.Server.create({'name': 'lone (agent)', 'host': 'agent:demo_db',
                                   'db_name': 'demo_db'})
        self.assertFalse(lone.host_server_id)
        # set by hand (managers), kept when the name matches nothing
        lone.host_server_id = self.host_b
        lone.name = 'still-lone (agent)'
        self.assertEqual(lone.host_server_id, self.host_b)
        # a server card created after its agent card links it
        late = self.Server.create({'name': 'late (agent)', 'host': 'agent:x', 'db_name': 'x'})
        self.assertFalse(late.host_server_id)
        late_server = self.Server.create({'name': 'late', 'host': '10.0.1.9', 'db_name': 'x'})
        self.assertEqual(late.host_server_id, late_server)
        self.assertFalse(self.host_a.host_server_id)  # server cards have no host link

    def test_deploy_register_local_prefers_name(self):
        token = self.Server._deploy_register_local('step-b', '10.0.1.2', 'demo_db')
        self.assertEqual(self.Server._find_by_token(token), self.host_b)
        # host + db_name of host_a but name of host_b: the name wins, the card is moved
        token = self.Server._deploy_register_local('step-b', '10.0.1.1', 'demo_db')
        self.assertEqual(self.Server._find_by_token(token), self.host_b)
        self.assertEqual(self.host_b.host, '10.0.1.1')
        self.assertEqual(self.host_a.host, '10.0.1.1')
        # unknown name: host + db_name fallback, else a new card
        token = self.Server._deploy_register_local('new-card', '10.7.7.7', 'db7')
        self.assertEqual(self.Server._find_by_token(token).name, 'new-card')

    def test_deploy_register_local_ignores_agent_cards(self):
        """A DB agent card with the same name is never turned into a server card: the deploy fails
        loudly (unique name) and the agent card is untouched."""
        agent = self.Server.create({'name': 'step_live', 'host': 'agent:step_live', 'db_name': 'step_live'})
        self.assertEqual(agent.source_kind, 'agent')
        with self.assertRaises(ValidationError):
            self.Server._deploy_register_local('step_live', '10.0.1.5', 'step_live')
        self.assertEqual(agent.host, 'agent:step_live')
        self.assertEqual(agent.source_kind, 'agent')
        # a different name works and creates a normal server card
        token = self.Server._deploy_register_local('step_live_srv', '10.0.1.5', 'step_live')
        server = self.Server._find_by_token(token)
        self.assertNotEqual(server, agent)
        self.assertEqual(server.source_kind, 'collector')

    def test_name_unique_ignores_case_and_spaces(self):
        for dup in ('STEP-A', ' step-a ', 'step-a  '):
            with self.assertRaises(ValidationError):
                self.Server.create({'name': dup, 'host': '10.0.1.9', 'db_name': 'x'})
        wizard = self.env['perf.server.add.wizard']
        with self.assertRaises(ValidationError):
            wizard.create({'name': 'Step-A', 'environment': 'live', 'host': '10.0.1.9', 'db_name': 'x'})

    def test_agent_link_ignores_case(self):
        agent = self.Server.create({'name': 'STEP-A (agent)', 'host': 'agent:step_a', 'db_name': 'demo_db'})
        self.assertEqual(agent.host_server_id, self.host_a)

    def test_add_wizard_shows_token(self):
        wizard = self.env['perf.server.add.wizard'].create({
            'name': 'step-new', 'environment': 'live', 'host': '10.0.1.5', 'db_name': 'demo_db',
            'central_url': 'https://perf.example.com/'})
        wizard.action_confirm()
        self.assertEqual(wizard.state, 'done')
        self.assertTrue(wizard.token)
        self.assertEqual(self.Server._find_by_token(wizard.token), wizard.server_id)
        self.assertEqual(wizard.central_db, self.env.cr.dbname)
        self.assertEqual(wizard.central_url, 'https://perf.example.com')
        self.assertIn(wizard.token, wizard.install_command)  # legacy installer still offered
        self.assertEqual(wizard.server_id.source_kind, 'collector')
        wizard.action_close()
        self.assertFalse(wizard.token or wizard.install_command)
        with self.assertRaises(ValidationError):  # duplicate name refused before any token is issued
            self.env['perf.server.add.wizard'].create({
                'name': 'step-a', 'environment': 'live', 'host': 'h', 'db_name': 'd',
                'central_url': 'https://perf.example.com'})

    def test_add_wizard_view(self):
        arch = etree.fromstring(self.env['perf.server.add.wizard'].get_views([(False, 'form')])['views']['form']['arch'])
        block = arch.xpath("//div[@name='add_done']")[0]
        token = block.xpath(".//field[@name='token']")
        self.assertEqual(token[0].get('widget'), 'CopyClipboardChar')
        text = etree.tostring(block).decode()
        self.assertIn('Settings &gt; Perf Monitor Agent', text)
        for name in ('central_url', 'central_db'):
            self.assertTrue(block.xpath(".//field[@name='%s']" % name), name)
        # the legacy root installer is collapsed, after the agent steps
        self.assertTrue(block.xpath(".//details//field[@name='install_command']"))
        self.assertLess(text.index('name="token"'), text.index('<details'))
        # regenerate paths keep their copy button
        for name in ('regenerate_collector', 'regenerate_agent'):
            field = arch.xpath("//div[@name='%s']//field[@name='token']" % name)[0]
            self.assertEqual(field.get('widget'), 'CopyClipboardChar')

    def test_slow_calls_on_collector_card(self):
        call = self.env['perf.agent.slow.call.remote'].create({
            'server_id': self.host_a.id, 'agent_id': 1, 'kind': 'http',
            'ts': fields.Datetime.now() - timedelta(hours=1), 'duration_ms': 3000.0})
        action = self.host_a.action_open_slow_calls()
        self.assertEqual(action['res_model'], 'perf.agent.slow.call.remote')
        self.assertEqual(action['domain'], [('server_id', '=', self.host_a.id)])
        self.assertEqual(self.env[action['res_model']].search(action['domain']), call)
        self.host_a.invalidate_recordset()
        self.assertEqual(self.host_a.slow_calls_7d, 1)
        form = etree.fromstring(self.Server.get_views([(False, 'form')])['views']['form']['arch'])
        button = form.xpath("//button[@name='action_open_slow_calls']")[0]
        self.assertNotIn('invisible', button.get('modifiers') or '')

    def test_agent_collected_status_windows(self):
        now = fields.Datetime.now()
        self.host_a.write({'collector_version': 'perf_monitor_agent/16.0.1.3.0',
                           'last_seen': now - timedelta(minutes=7)})
        self.host_a._refresh_status()
        self.assertEqual(self.host_a.status, 'green')  # pushes every 5 minutes
        self.host_a.last_seen = now - timedelta(minutes=15)
        self.host_a._refresh_status()
        self.assertEqual(self.host_a.status, 'yellow')
        self.host_b.write({'collector_version': '1.0.1', 'last_seen': now - timedelta(minutes=7)})
        self.host_b._refresh_status()
        self.assertEqual(self.host_b.status, 'yellow')  # root collector: every minute

    def test_agent_payload_on_server_card(self):
        """Payload of the in-agent collector (db_name + DB-only metric row) on a normal server card."""
        ts = fields.Datetime.to_string(fields.Datetime.now() - timedelta(minutes=1))
        result = self.host_a._ingest_payload({
            'version': 1, 'db_name': 'demo_db', 'collector_version': 'perf_monitor_agent/16.0.1.3.0',
            'host': {'odoo_version': '16.0', 'pg_version': 'PostgreSQL 14'},
            'metrics': [{'ts': ts, 'db_latency_ms': 0.4, 'pg_connections': 12}],
            'sql': [{'ts': ts, 'queryid': '42', 'query': "SELECT * FROM t WHERE a = 'x'", 'calls': 3}],
            'live': [{'ts': ts, 'pid': 77, 'category': 'lock_wait', 'query': 'UPDATE t SET a = 1'}]})
        self.assertEqual(result, {'accepted': 3, 'rejected': 0})
        self.assertEqual(self.host_a.status, 'green')
        self.assertEqual(self.host_a.db_latency_ms, 0.4)
        self.assertEqual(self.host_a.metric_count, 1)
