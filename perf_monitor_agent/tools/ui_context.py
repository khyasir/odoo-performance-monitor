# -*- coding: utf-8 -*-
"""UI context of a slow call: the screen the user was on (menu, action, dashboard, model, record).

The web client patch (static/src/js/perf_ui.js) sends it with every RPC in the ``X-Perf-UI`` header,
a query string built from the URL hash plus the action being opened and the last clicked menu:
``a=<action id | xmlid | client tag>&m=<menu id>&mo=<model>&id=<record id>&v=<view type>&ai=<active id>
&d=<spreadsheet dashboard id>&dh=<dashboards.saas id>&t=<dashboard tile id>`` (``dh``: the
``context_dashboard_id`` of the current client action, ``t``: the tile a per-tile RPC is about). The
server parses it ONLY for slow calls (never on the fast path).
Without the header (other clients, the few RPCs sent before the patch is loaded) the RPC's own params /
context are used: ``context.params`` (set by the web client when an action is restored from the URL),
``active_id`` / ``active_model``, the ``action_id`` of /web/action/load, dashboard ids in the context,
and the dashboard / tile ids in the arguments of the odoo_dashboards_saas RPCs (see DASHBOARD_RPCS).

Pure python (no Odoo import): unit-testable, and nothing here may raise.
"""
import re
from urllib.parse import parse_qsl

HEADER = 'X-Perf-UI'
MAX_HEADER = 512
MAX_ID = 2147483647  # int4 columns
MAX_STR = 128
# context keys carrying the id of a dashboard record -> the dashboard model (odoo_dashboards_saas client
# actions carry {'context_dashboard_id': <id>} in their ir.actions.client context)
DASHBOARD_CONTEXT_KEYS = {'context_dashboard_id': 'dashboards.saas', 'active_dashboard_id': 'dashboards.saas'}
# models whose records ARE dashboards: an RPC on them names the dashboard in its arguments
DASHBOARD_MODELS = ('dashboards.saas', 'spreadsheet.dashboard')
SPREADSHEET_MODEL = 'spreadsheet.dashboard'
SPREADSHEET_TAG = 'action_spreadsheet_dashboard'
BOARD_MODEL = 'board.board'
# odoo_dashboards_saas: a dashboard (dashboards.saas) shows tiles / widgets (data.analytics.saas)
SAAS_DASHBOARD_MODEL = 'dashboards.saas'
TILE_MODEL = 'data.analytics.saas'
# dashboards.saas RPCs (all @api.model style, called with args[0] = None) -> (what, index of the id in
# args, kwarg name). Anything else on dashboards.saas falls back to the generic record-id guess.
DASHBOARD_RPCS = {
    'get_all_datasets_saas': ('dashboard', 3, 'dashboard_id'),
    'get_dashboard_metadata': ('dashboard', 1, 'dashboard_id'),
    'get_analytics_condition_lines_for_global_filters': ('dashboard', 1, 'dashboard_id'),
    'get_tile_data_from_da_id': ('tile', 1, 'tile_id'),
    'get_bar_chart_data_from_da_id': ('tile', 1, 'data_analytics_obj_id'),
    'get_table_data_from_da_id': ('tile', 1, 'data_analytics_obj_id'),
    'get_pivot_data_from_da_id': ('tile', 1, 'data_analytics_obj_id'),
    'get_heatmap_data_from_da_id': ('tile', 1, 'data_analytics_obj_id'),
    'get_map_data_from_da_id': ('tile', 1, 'data_analytics_obj_id'),
    'get_report_data_from_da_id': ('tile', 1, 'data_analytics_obj_id'),
}
_HEADER_INTS = {'m': 'menu_id', 'id': 'record_id', 'ai': 'active_id', 'd': 'spreadsheet_id', 't': 'tile_id',
                'dh': 'saas_dashboard_id'}
_HEADER_STRS = {'mo': 'model', 'v': 'view_type', 'am': 'active_model'}
_RX_MODEL = re.compile(r'^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$')
_RX_CTX_KEYS = [(re.compile(r'''['"]%s['"]\s*:\s*(\d+)''' % re.escape(key)), model)
                for key, model in DASHBOARD_CONTEXT_KEYS.items()]


def to_id(value):
    """Positive int4 id from an int or a digit string, else None (bools are not ids)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, int) and 0 < value <= MAX_ID:
        return value
    return None


def to_model(value):
    return value if isinstance(value, str) and len(value) <= MAX_STR and _RX_MODEL.match(value) else None


def to_str(value):
    if not isinstance(value, str):
        return None
    value = value.strip().replace('\x00', '')[:MAX_STR]
    return value or None


def action_ref(value):
    """An action id (int) or an xmlid / client action tag (str)."""
    return to_id(value) or to_str(value)


def parse_header(value):
    """dict of the X-Perf-UI header (unknown keys and bad values dropped)."""
    ui = {}
    if not value or not isinstance(value, str):
        return ui
    for key, val in parse_qsl(value[:MAX_HEADER], keep_blank_values=False):
        if key == 'a':
            ui['action'] = action_ref(val)
        elif key in _HEADER_INTS:
            ui[_HEADER_INTS[key]] = to_id(val)
        elif key in ('mo', 'am'):
            ui[_HEADER_STRS[key]] = to_model(val)
        elif key in _HEADER_STRS:
            ui[_HEADER_STRS[key]] = to_str(val)
    return {k: v for k, v in ui.items() if v is not None}


def _record_ids(args):
    """Ids named by RPC args: args[0] (a recordset method), else the first id among args[1:4]
    (@api.model methods such as dashboards.saas.get_analytics_payload(None, [widgets], dashboard_id))."""
    if not isinstance(args, (list, tuple)) or not args:
        return []
    first = args[0]
    if isinstance(first, (list, tuple)):
        ids = [to_id(i) for i in first[:10]]
        if ids and all(ids):
            return ids
    elif to_id(first):
        return [to_id(first)]
    for arg in args[1:4]:
        if to_id(arg):
            return [to_id(arg)]
    return []


def _arg(args, kwargs, index, name):
    """Positional arg ``index`` or keyword ``name`` of an RPC, as an id (None when absent / invalid)."""
    if isinstance(args, (list, tuple)) and len(args) > index:
        found = to_id(args[index])
        if found:
            return found
    return to_id(kwargs.get(name)) if isinstance(kwargs, dict) else None


def dashboard_from_action_context(context):
    """(model, id) of a dashboard named in an action's context (a python dict literal string)."""
    if isinstance(context, str):
        for rx, model in _RX_CTX_KEYS:
            match = rx.search(context)
            if match and to_id(match.group(1)):
                return model, to_id(match.group(1))
    return None, None


def collect(header, params, path):
    """Raw UI context of a request (ids / names as sent, nothing resolved): keys action, menu_id,
    model, record_id, view_type, active_id, active_model, spreadsheet_id, dashboard_model,
    dashboard_id, tile_id. The header wins, the RPC params / context fill the gaps."""
    ui = parse_header(header)
    params = params if isinstance(params, dict) else {}
    kwargs = params.get('kwargs')
    ctx = kwargs.get('context') if isinstance(kwargs, dict) else None
    if not isinstance(ctx, dict):
        ctx = params.get('context') if isinstance(params.get('context'), dict) else {}
    url_state = ctx.get('params') if isinstance(ctx.get('params'), dict) else {}
    for key, value in (('action', action_ref(url_state.get('action'))),
                       ('menu_id', to_id(url_state.get('menu_id'))),
                       ('model', to_model(url_state.get('model'))),
                       ('record_id', to_id(url_state.get('id'))),
                       ('view_type', to_str(url_state.get('view_type'))),
                       ('active_id', to_id(ctx.get('active_id'))),
                       ('active_model', to_model(ctx.get('active_model')))):
        if value is not None and key not in ui:
            ui[key] = value
    path = path or ''
    if path.startswith(('/web/action/load', '/web/action/run')):
        # the call IS the opening of this action (the header still names the previous screen)
        action = action_ref(params.get('action_id'))
        if action is not None:
            ui = {'action': action, 'menu_id': ui['menu_id']} if 'menu_id' in ui else {'action': action}
    saas_dashboard_id = ui.pop('saas_dashboard_id', None)
    if saas_dashboard_id:
        ui['dashboard_model'], ui['dashboard_id'] = SAAS_DASHBOARD_MODEL, saas_dashboard_id
    for key, model in DASHBOARD_CONTEXT_KEYS.items():
        dashboard_id = to_id(ctx.get(key))
        if dashboard_id and 'dashboard_id' not in ui:
            ui['dashboard_model'], ui['dashboard_id'] = model, dashboard_id
            break
    rpc_model = params.get('model')
    if path.startswith('/web/dataset/call_') and isinstance(rpc_model, str):
        args, kwargs = params.get('args'), params.get('kwargs')
        spec = DASHBOARD_RPCS.get(params.get('method')) if rpc_model == SAAS_DASHBOARD_MODEL else None
        if spec:
            # the ids are explicit arguments: they beat the header (the RPC is about THIS dashboard / tile)
            found = _arg(args, kwargs, spec[1], spec[2])
            if found and spec[0] == 'tile':
                ui['tile_id'] = found
            elif found:
                ui['dashboard_model'], ui['dashboard_id'] = SAAS_DASHBOARD_MODEL, found
            ids = []
        else:
            ids = _record_ids(args)
        if rpc_model == TILE_MODEL and ids and 'tile_id' not in ui:
            ui['tile_id'] = ids[0]
        if rpc_model in DASHBOARD_MODELS and ids and 'dashboard_id' not in ui:
            ui['dashboard_model'], ui['dashboard_id'] = rpc_model, ids[0]
        if len(ids) == 1 and 'record_id' not in ui and 'active_id' not in ui and 'model' not in ui:
            ui['model'], ui['record_id'] = to_model(rpc_model), ids[0]
    return {k: v for k, v in ui.items() if v is not None}


def summary(when, duration_ms, dashboard=None, action=None, menu=None, model=None, record_id=None,
            cron_name=None, model_name=None, method=None, route=None, tile=None, login=None):
    """One line for a task, e.g. "Dashboard 'Sales' > Tile 'Top Products' (menu Sales/Dashboards) by user
    ali at 2026-09-27 16:05 took 4.2 s (dashboards.saas.get_all_datasets_saas)"."""
    took = '%.1f s' % ((duration_ms or 0.0) / 1000.0)
    call = '.'.join(p for p in (model_name, method) if p) or route or ''
    if dashboard:
        text = "Dashboard '%s'" % dashboard + (" > Tile '%s'" % tile if tile else '')
        text += ' (menu %s)' % menu if menu else ''
    elif action:
        text = "'%s' opened" % action + (" (menu '%s')" % menu if menu else '')
    elif menu:
        text = "Menu '%s' opened" % menu
    elif cron_name:
        text, call = "Cron '%s' ran" % cron_name, ''
    else:
        text, call = '%s called' % (call or 'request'), ''
    if tile and not dashboard:
        text += " > Tile '%s'" % tile
    if model and record_id and not dashboard:
        text += ' on %s #%s' % (model, record_id)
    if login and not cron_name:
        text += ' by user %s' % login
    text += ' at %s took %s' % (when, took)
    return text + (' (%s)' % call if call else '')
