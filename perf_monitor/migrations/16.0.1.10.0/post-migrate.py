# -*- coding: utf-8 -*-
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.perf_monitor.models.perf_server import base_server_name, name_key

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """16.0.1.10.0: an agent card named like a server card (" (agent)" suffix ignored, case and spaces
    ignored) is linked to it; every other agent card keeps the link kept by pre-migrate."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    Server = env['perf.server'].with_context(active_test=False)
    collectors = {}
    for server in Server.search([('source_kind', '=', 'collector'), ('active', '=', True)]):
        collectors.setdefault(name_key(server.name), server)
    changed = 0
    for agent in Server.search([('source_kind', '=', 'agent')]):
        match = collectors.get(name_key(base_server_name(agent.name)))
        if match and agent.host_server_id != match:
            agent.host_server_id = match
            changed += 1
    dups = {}
    for server in Server.search([]):
        dups.setdefault(name_key(server.name), []).append(server.name)
    for key, names in dups.items():
        if len(names) > 1:
            _logger.warning('perf_monitor: server names only differ by case / spaces, rename them: %r', names)
    _logger.info('perf_monitor: %d agent card(s) linked by name', changed)
