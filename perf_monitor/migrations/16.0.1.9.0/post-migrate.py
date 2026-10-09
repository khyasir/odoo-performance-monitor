# -*- coding: utf-8 -*-
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """16.0.1.9.0: the slow calls already linked to an issue get its key (they count as recurring
    occurrences), and the issues get the SQL time of their slowest call (plain-language explanation).
    perf_monitor.ai_enabled keeps its current value: only fresh installs start with the AI layer off."""
    cr.execute("""
        UPDATE perf_agent_slow_call_remote c SET issue_key = i.key
          FROM perf_issue i
         WHERE c.issue_id = i.id AND c.issue_key IS NULL AND i.key IS NOT NULL
    """)
    _logger.info('perf_monitor: issue key set on %d slow call(s)', cr.rowcount)
    cr.execute("""
        SELECT DISTINCT ON (issue_id) issue_id, sql_ms
          FROM perf_agent_slow_call_remote
         WHERE issue_id IS NOT NULL AND COALESCE(sql_ms, 0) > 0
         ORDER BY issue_id, GREATEST(COALESCE(tile_ms, 0), COALESCE(duration_ms, 0)) DESC
    """)
    env = api.Environment(cr, SUPERUSER_ID, {})
    Issue = env['perf.issue'].with_context(active_test=False)
    for issue_id, sql_ms in cr.fetchall():
        Issue.browse(issue_id).write({'sql_ms': sql_ms})  # recomputes the explanation
    _logger.info('perf_monitor: SQL time set on the issues, explanations recomputed')
