# -*- coding: utf-8 -*-
"""collector/perf_collector.py (stand-alone root collector, no Odoo): pg_stat_statements status reported as payload
"pgss" and logged once per change (INFO), never a warning every cycle. Pure python."""
import importlib.util
import os
import tempfile
from unittest.mock import patch

from odoo.tests import BaseCase, tagged

COLLECTOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'collector', 'perf_collector.py')


def _load():
    spec = importlib.util.spec_from_file_location('perf_monitor_collector_under_test', COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePg:
    def __init__(self, results):
        self.results = list(results)

    def statements(self, qmax):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@tagged('post_install', '-at_install')
class TestCollectorPgss(BaseCase):

    def _collector(self, mod, results):
        tmp = tempfile.mkdtemp()
        cfg = {'thresholds': dict(mod.DEFAULTS['thresholds']),
               'state_path': os.path.join(tmp, 'state.json'), 'central_url': 'https://x', 'token': 't',
               'http_timeout': 1}
        return mod.Collector(cfg, pg=FakePg(results), central=object(), sampler=object(), spool=object(),
                             imds_fn=dict)

    def test_pgss_status_and_single_log(self):
        mod = _load()
        col = self._collector(mod, [None, None, None, RuntimeError('must be loaded via shared_preload_libraries'),
                                    RuntimeError('again'), [], []])
        with patch.object(col, 'host_info', return_value={}):
            self.assertNotIn('pgss', col.build_payload(), 'no DB read yet: key absent (central: unknown)')
            with patch.object(mod._logger, 'info') as info, patch.object(mod._logger, 'warning') as warning:
                for _cycle in range(3):
                    col.collect_topsql()
                self.assertEqual(col.build_payload()['pgss'], 'missing')
                self.assertEqual(info.call_count, 1, 'missing: logged once')
                col.collect_topsql()
                col.collect_topsql()
                self.assertEqual(col.build_payload()['pgss'], 'unreadable')
                self.assertEqual(info.call_count, 2, 'unreadable: one more line')
                self.assertIn('shared_preload_libraries', str(info.call_args))
                col.collect_topsql()
                col.collect_topsql()
                self.assertEqual(col.build_payload()['pgss'], 'ok')
                self.assertEqual(info.call_count, 3, 'back to ok: one line')
            warning.assert_not_called()
