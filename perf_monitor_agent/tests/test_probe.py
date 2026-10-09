# -*- coding: utf-8 -*-
from odoo.tests import BaseCase, tagged

from ..tools import probe as P
from ..tools.sql_fingerprint import normalize_sql, redact_literals, sql_fingerprint


@tagged('post_install', '-at_install', 'perf_monitor_agent')
class TestProbe(BaseCase):

    def test_app_name_format(self):
        name = P.build_app_name('db1', 7, 'bob', 'res.partner.read', '/web/dataset/call_kw')
        self.assertEqual(name, 'odoo|db1|7|bob|res.partner.read|/web/dataset/call_kw')
        self.assertEqual(P.parse_app_name(name), {'db': 'db1', 'uid': '7', 'login': 'bob',
                                                  'model_method': 'res.partner.read',
                                                  'where': '/web/dataset/call_kw'})
        self.assertEqual(P.build_app_name('db', None, None, None, None), 'odoo|db|0|-|-|-')

    def test_app_name_truncated_ascii(self):
        name = P.build_app_name('d' * 80, 1, 'x', 'y', 'z')
        self.assertEqual(len(name.encode()), 63)
        # non-ASCII / control chars -> '?', the separator inside a field -> '/'
        name = P.build_app_name('db', 1, 'jürgen|x\n中', 'm.m', 'cron:A|B')
        self.assertEqual(name, 'odoo|db|1|j?rgen/x??|m.m|cron:A/B')
        self.assertTrue(name.isascii())
        self.assertLessEqual(len(P.build_app_name('中' * 100, 1, 'a', 'b', 'c').encode()), 63)

    def test_short_route(self):
        self.assertEqual(P.short_route('/web/dataset/call_kw/sale.order/read'), '/web/dataset/call_kw')
        self.assertEqual(P.short_route('/web/dataset/call_button'), '/web/dataset/call_button')
        self.assertEqual(P.short_route('/shop/cart'), '/shop/cart')
        self.assertEqual(P.short_route(''), '-')

    def test_probe_aggregates_by_template(self):
        probe = P.CallProbe('http', 0, {}, 'x')
        # business code of another addon (this test file is inside the agent: skipped as framework)
        ns = {}
        exec(compile("def confirm(probe):\n"
                     "    for _i in range(10):\n"
                     "        probe(None, 'SELECT name FROM res_partner WHERE id = %s', [1], 0, 0.004)\n"
                     "    probe(None, 'SELECT 1', None, 0, 0.02)\n",
                     '/srv/addons/sale_custom/models/sale_order.py', 'exec'), ns)
        probe.install()
        try:
            ns['confirm'](probe)  # N+1: many cheap executions of one template win over one slower query
        finally:
            probe.uninstall()
        self.assertEqual(probe.sql_count, 11)
        self.assertAlmostEqual(probe.sql_s, 0.06)
        query, calls, total_ms, callsite = probe.top()
        self.assertEqual((query, calls), ('SELECT name FROM res_partner WHERE id = %s', 10))
        self.assertAlmostEqual(total_ms, 40.0)
        self.assertEqual(callsite, 'sale_custom/models/sale_order.py:3 (confirm)')
        self.assertNotIn(probe, P.threading.current_thread().query_hooks)

    def test_callsite_skips_framework(self):
        import odoo
        odoo_dir = P._odoo_dir()
        self.assertEqual(odoo_dir, P.os.path.dirname(P.os.path.abspath(odoo.__file__)) + P.os.sep)
        for path, framework in ((odoo_dir + 'models.py', True), (odoo_dir + 'sql_db.py', True),
                                (odoo_dir + 'addons/base/models/res_partner.py', False),
                                ('/x/addons/web/controllers/dataset.py', True), ('/x/odoo-bin', True),
                                ('/x/addons/sale/models/sale_order.py', False), (P._OWN_DIR + 'runtime.py', True),
                                (P._STDLIB_DIR + 'threading.py', True), ('<string>', True),
                                ('/opt/odoo/custom/addons/wags_logger/models/wags_logger.py', True),
                                ('/opt/odoo/custom/addons/wags_logger_ext/models/x.py', False)):
            self.assertEqual(P._is_framework(path, odoo_dir), framework, path)

    def test_callsite_skips_wags_logger(self):
        # wags_logger's cursor patch -> sql_db -> the real caller: the caller is named, never wags_logger
        ns = {}
        exec(compile('def patched(f):\n    return f()\n', '/opt/odoo/custom/addons/wags_logger/models/wags_logger.py',
                     'exec'), ns)
        exec(compile('def business(p, f):\n    return p(f)\n', '/opt/odoo/custom/addons/closing_wags/models/c.py',
                     'exec'), ns)
        site = ns['business'](ns['patched'], P.find_callsite)
        self.assertTrue(site.startswith('closing_wags/models/c.py:2 (business)'), site)
        # only wags_logger frames (and this module): no fallback to wags_logger either
        only = ns['patched'](lambda: P.find_callsite(P.sys._getframe(0)))
        self.assertNotIn('wags_logger', only)

    def test_probe_never_raises(self):
        from psycopg2 import sql
        probe = P.CallProbe('http', 0, {}, 'x')
        probe(None, sql.SQL('SELECT {}').format(sql.Identifier('a')), None, 0, 0.001)  # unhashable
        probe(None, b'SELECT 1', None, 0, 0.001)
        self.assertEqual(probe.sql_count, 2)
        probe.uninstall()  # not installed: no-op

    def test_templates_bounded(self):
        probe = P.CallProbe('http', 0, {}, 'x')
        for i in range(P.MAX_TEMPLATES + 50):
            probe(None, 'SELECT %d' % i, None, 0, 0.0001)
        self.assertEqual(len(probe.stats), P.MAX_TEMPLATES)
        self.assertEqual(probe.sql_count, P.MAX_TEMPLATES + 50)
        self.assertLessEqual(probe.walks, P.MAX_CALLSITE_WALKS)

    def test_rate_limiter(self):
        limiter = P.RateLimiter(2)
        self.assertEqual([limiter.allow(60.0) for _i in range(3)], [True, True, False])
        self.assertTrue(limiter.allow(125.0))

    def test_fingerprint_placeholder_styles_agree(self):
        # Odoo template (agent) == pg_stat_statements text == redacted text
        variants = [
            "SELECT id FROM res_partner WHERE id IN %s AND name = %(n)s",
            "select id from res_partner where id in ($1, $2, $3) and name = $4",
            "SELECT id FROM res_partner WHERE id IN (?) AND name = ?",
            "SELECT id FROM res_partner WHERE id IN (1, 2) AND name = 'x' -- c",
        ]
        self.assertEqual(len({sql_fingerprint(v) for v in variants}), 1)
        self.assertEqual(normalize_sql(variants[0]), 'select id from res_partner where id in(...)and name = ?')
        self.assertEqual(redact_literals("SELECT 'secret', 42 FROM t WHERE a = %s"), 'SELECT ?, ? FROM t WHERE a = %s')

    def test_time_tile(self):
        import time
        hooks_before = len(getattr(P.threading.current_thread(), 'query_hooks', None) or [])
        call = P.CallProbe('rpc', 0, {}, 'x')
        call.install()
        self.addCleanup(call.uninstall)

        def tile(ms, queries, nested=None):
            for _i in range(queries):  # a query hook call per statement, like sql_db.Cursor.execute
                for hook in list(P.threading.current_thread().query_hooks):
                    hook(None, 'SELECT %s' % ms, None, 0, 0.001)
            if nested:
                P.time_tile(call, nested, tile, (0, 1), {})
            time.sleep(ms / 1000.0)
            return ms

        self.assertEqual(P.time_tile(call, 7, tile, (30, 2), {}), 30)
        self.assertEqual(P.time_tile(call, 8, tile, (5, 1), {'nested': 9}), 5)  # nested: counted once, in 8
        P.time_tile(call, 7, tile, (10, 1), {})  # a second section of tile 7 adds up
        self.assertEqual(set(call.tiles), {7, 8})
        slowest = P.slowest_tiles(call)
        self.assertEqual([t.tile_id for t in slowest], [7, 8])
        self.assertGreaterEqual(slowest[0].ms, 40)
        self.assertEqual((slowest[0].sections, slowest[0].sql_count, slowest[1].sql_count), (2, 3, 2))
        self.assertAlmostEqual(slowest[0].sql_ms, 3.0)
        self.assertEqual(slowest[0].probe.top()[0], 'SELECT 30')
        self.assertEqual(call.sql_count, 5)  # the call's own probe still sees every statement
        self.assertFalse(call.in_tile)
        # errors propagate, the timer is reset; bad ids / no probe: plain call
        with self.assertRaises(ZeroDivisionError):
            P.time_tile(call, 7, lambda: 1 / 0, (), {})
        self.assertFalse(call.in_tile)
        self.assertEqual(len(P.threading.current_thread().query_hooks), hooks_before + 1)
        self.assertEqual(P.time_tile(call, None, tile, (0, 0), {}), 0)
        self.assertEqual(P.time_tile(call, True, tile, (0, 0), {}), 0)
        self.assertEqual(P.time_tile(None, 3, tile, (0, 0), {}), 0)
        self.assertEqual(set(call.tiles), {7, 8})
        self.assertEqual(P.slowest_tiles(P.CallProbe('rpc', 0, {}, 'y')), [])

    def test_time_tile_bounded(self):
        call = P.CallProbe('rpc', 0, {}, 'x')
        for tile_id in range(1, P.MAX_TILES + 20):
            P.time_tile(call, tile_id, int, (), {})
        self.assertEqual(len(call.tiles), P.MAX_TILES)
