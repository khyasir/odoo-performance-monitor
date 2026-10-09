# -*- coding: utf-8 -*-
import logging

from odoo import models

from .. import runtime

_logger = logging.getLogger(__name__)


class IrCron(models.Model):
    _inherit = 'ir.cron'

    def _callback(self, cron_name, server_action_id, job_id):
        """Tag the job cursor with the cron (application_name reset afterwards) and record the run
        when slow. Never lets the instrumentation fail the job."""
        try:
            probe = runtime.begin_cron(self, cron_name, server_action_id)
        except Exception:
            _logger.warning('perf_monitor_agent: begin_cron failed', exc_info=True)
            probe = None
        try:
            return super()._callback(cron_name, server_action_id, job_id)
        finally:
            if probe is not None:
                runtime.end_cron(self, probe)
