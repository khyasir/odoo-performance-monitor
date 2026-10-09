# -*- coding: utf-8 -*-
from . import models
from . import controllers
from . import wizard


def uninstall_hook(cr, registry):
    # parameters written by code or by Settings (not by data files, which the ORM removes itself); the
    # 'True' / 'False' on-by-default switches (ai_tool_use_enabled, ai_chat_enabled, ai_chat_tools_enabled) and
    # every other AI setting are covered by the perf_monitor.ai_% pattern
    cr.execute("""
        DELETE FROM ir_config_parameter p
         WHERE (p.key IN ('perf_monitor.central_url', 'perf_monitor.ai_enabled', 'perf_monitor.issue_assignee_id')
                OR p.key LIKE 'perf\\_monitor.ai\\_%%')
           AND NOT EXISTS (SELECT 1 FROM ir_model_data d WHERE d.model = 'ir.config_parameter' AND d.res_id = p.id)
    """)
