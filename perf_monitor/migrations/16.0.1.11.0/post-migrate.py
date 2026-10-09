# -*- coding: utf-8 -*-
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """16.0.1.11.0: the stored plain explanation shows the top query time in seconds (was ms): recompute it."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    Issue = env['perf.issue'].with_context(active_test=False)
    issues = Issue.search([])
    env.add_to_compute(Issue._fields['explanation'], issues)
    issues.flush_recordset(['explanation'])
    _logger.info('perf_monitor: plain explanation recomputed on %d issue(s)', len(issues))
