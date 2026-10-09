# -*- coding: utf-8 -*-
import logging

from odoo import models
from odoo.http import request

from .. import runtime

_logger = logging.getLogger(__name__)


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    @classmethod
    def _dispatch(cls, endpoint):
        """Tag the DB connection with the request and record it when slow. Instrumentation errors
        are logged and swallowed; the endpoint's own exceptions propagate unchanged."""
        try:
            probe = runtime.begin_request(request, endpoint)
        except Exception:
            _logger.warning('perf_monitor_agent: begin_request failed', exc_info=True)
            probe = None
        if probe is None:
            return super()._dispatch(endpoint)
        error = None
        try:
            return super()._dispatch(endpoint)
        except BaseException as exc:
            error = exc
            raise
        finally:
            runtime.end_request(request, probe, error)
