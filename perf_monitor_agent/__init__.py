# -*- coding: utf-8 -*-
from . import models


def uninstall_hook(cr, registry):
    # parameters written by code / the deploy script (data-file parameters are removed by the ORM)
    cr.execute("DELETE FROM ir_config_parameter WHERE key IN %s",
               [('perf_monitor_agent.central_url', 'perf_monitor_agent.central_token',
                 'perf_monitor_agent.central_db', 'perf_monitor_agent.enabled',
                 'perf_monitor_agent.collect_enabled', 'perf_monitor_agent.send_host_metrics')])
