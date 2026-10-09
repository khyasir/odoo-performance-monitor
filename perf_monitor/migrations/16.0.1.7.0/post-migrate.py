# -*- coding: utf-8 -*-
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Issue assignee (new in 16.0.1.7.0): store the default in Settings (Yasir, else Administrator) and
    assign the issues built before, with a To-Do activity on the open ones (no e-mail)."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    assignee = env['perf.issue']._issue_assignee()
    issues = env['perf.issue']._assign_unassigned()
    _logger.info('perf_monitor: issue assignee %s, %d issue(s) assigned', assignee.login, len(issues))
