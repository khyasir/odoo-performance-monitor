# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api

DEFAULTS = {'central_url': 'https://central.example.com', 'central_db': 'demo_db'}


def migrate(cr, version):
    """Default central endpoint for DBs installed before 16.0.1.2.0: only the keys still missing."""
    api.Environment(cr, SUPERUSER_ID, {})['perf.agent.slow.call']._set_default_params(DEFAULTS)
