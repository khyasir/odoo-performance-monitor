# -*- coding: utf-8 -*-
"""Request / cron instrumentation used by the ir.http and ir.cron overrides.

Fast path (a call below the threshold), per HTTP request:
* 1 statement: ``SET LOCAL application_name`` (the tag itself; transaction-scoped, so PostgreSQL
  resets it at commit / rollback: no reset statement, nothing leaks to the next pooled use). It is
  sent before the endpoint runs so that the endpoint's first (often slowest) query is tagged too.
* no read: the config is cached per process for CONFIG_TTL seconds (and ir.config_parameter reads
  are ormcached by Odoo anyway); the login comes from the session, not from res.users.
* one query hook call per statement (a dict update).
Slow path: one INSERT on a separate cursor (so a failed request is recorded too), rate limited. Only
then is the UI context (X-Perf-UI header / RPC params, see tools/ui_context.py) parsed and its action /
menu / dashboard / tile / user names read (a few indexed reads, under a savepoint: a failure only loses
the names).
Dashboard tiles (odoo_dashboards_saas, when installed): the methods computing ONE tile are wrapped
(install_tile_timers) so that a call computing many tiles (get_all_datasets_saas) knows the time / SQL of
each tile. Outside an instrumented request the wrapper is one thread-local read; inside, two
perf_counter() calls and a nested query hook for the tile's own SQL. Per-tile rows (kind 'tile', linked to
the call row) are only written when the call itself was slow.
Every entry point swallows its own exceptions: the agent must never break a request or a cron.
"""
import functools
import logging
import re
import sys
import threading
import time
from datetime import datetime

from odoo import SUPERUSER_ID, api

from .tools.probe import CallProbe, RateLimiter, build_app_name, short_route, slowest_tiles, time_tile
from .tools.sql_fingerprint import redact_literals, sql_fingerprint
from .tools import ui_context

_logger = logging.getLogger(__name__)

PARAM_PREFIX = 'perf_monitor_agent.'
DEFAULT_SLOW_MS = 10000
CONFIG_TTL = 30.0
MAX_ROWS_PER_MINUTE = 120  # per process
TOP_SQL_MAX = 2000
MAX_TILE_ROWS = 50  # per-tile rows written for one slow call (slowest first)
# long-lived / streaming endpoints (always "slow", never interesting) and trivial endpoints that run
# no SQL: on those the SET LOCAL would open a transaction on its own (BEGIN + SET + COMMIT = 3 round
# trips for nothing); every other request already has / opens one, so tagging costs 1 round trip
EXCLUDED_PREFIXES = ('/websocket', '/longpolling', '/bus/', '/web/static/', '/web/assets/',
                     '/web/webclient/version_info', '/web/webclient/translations', '/web/webclient/locale',
                     '/web/service-worker.js', '/web/manifest.webmanifest')
_RX_CRON_METHOD = re.compile(r'\bmodel\s*\.\s*([A-Za-z_]\w*)\s*\(')

_config = {}  # dbname -> (expires, enabled, slow_ms)
_limiter = RateLimiter(MAX_ROWS_PER_MINUTE)
_local = threading.local()


def get_config(env):
    """(enabled, slow_ms) for env's DB, cached CONFIG_TTL seconds per process."""
    dbname = env.cr.dbname
    now = time.monotonic()
    cached = _config.get(dbname)
    if cached and cached[0] > now:
        return cached[1], cached[2]
    params = env['ir.config_parameter'].sudo()
    enabled = (params.get_param(PARAM_PREFIX + 'enabled') or '1').strip().lower() not in ('0', 'false', 'no', 'off')
    try:
        slow_ms = max(0, int(params.get_param(PARAM_PREFIX + 'slow_ms') or DEFAULT_SLOW_MS))
    except (TypeError, ValueError):
        slow_ms = DEFAULT_SLOW_MS
    _config[dbname] = (now + CONFIG_TTL, enabled, slow_ms)
    return enabled, slow_ms


def clear_config_cache():
    _config.clear()


def _set_app_name(cr, name, local):
    cr.execute('SET LOCAL application_name = %s' if local else 'SET application_name = %s', [name],
               log_exceptions=False)


def _tx_in_error(cr):
    try:
        import psycopg2.extensions as ext
        return cr._cnx.info.transaction_status == ext.TRANSACTION_STATUS_INERROR
    except Exception:
        return False


def _tag(cr, name, local):
    """Set the tag; if that statement failed, give the request a usable transaction back."""
    try:
        _set_app_name(cr, name, local)
        return True
    except Exception as exc:
        _logger.warning('perf_monitor_agent: cannot set application_name: %s', exc)
        if _tx_in_error(cr):
            cr.rollback()  # only undoes the pre-dispatch work of this request (auth / routing reads)
        return False


# ------------------------------------------------------------------------------------ HTTP
def begin_request(request, endpoint):
    """CallProbe for this request (tag set, query hook installed) or None when not instrumented."""
    if getattr(_local, 'probe', None) is not None:
        return None  # nested dispatch (e.g. a rerouted request): the outer probe covers it
    httprequest = request.httprequest
    path = httprequest.path or ''
    if path.startswith(EXCLUDED_PREFIXES):
        return None
    env = request.env
    enabled, slow_ms = get_config(env)
    if not enabled:
        return None
    params = request.params or {}
    model_name = params.get('model')
    method = params.get('method')
    if isinstance(model_name, str) and isinstance(method, str) and path.startswith('/web/dataset/call_'):
        kind = 'rpc'
        model_method = '%s.%s' % (model_name, method)
    else:
        kind = 'http'
        func = getattr(endpoint, 'original_endpoint', endpoint)
        qualname = getattr(func, '__qualname__', None) or getattr(func, '__name__', '') or ''
        model_name, method = False, qualname or False
        model_method = qualname
    session = getattr(request, 'session', None)
    login = session.get('login') if session is not None else None
    uid = env.uid or 0
    route = short_route(path)
    name = build_app_name(env.cr.dbname, uid, login, model_method, route)
    probe = CallProbe(kind, slow_ms, {'uid': uid, 'login': login or False, 'model_name': model_name or False,
                                      'method': method or False, 'route': path[:256], 'cron_name': False}, name)
    _tag(env.cr, name, local=True)
    probe.install()
    _local.probe = probe
    return probe


def end_request(request, probe, error=None):
    """Remove the hook; record the call if it was slow. Never raises."""
    try:
        _local.probe = None
        probe.uninstall()
        duration_ms = probe.elapsed_ms()
        if duration_ms < probe.slow_ms:
            return
        record_slow_call(request.env.registry, probe, duration_ms, error, collect_ui(request))
    except Exception:
        _logger.warning('perf_monitor_agent: end_request failed', exc_info=True)


def collect_ui(request):
    """Raw UI context of a slow request ({} when it cannot be read). Slow path only."""
    try:
        httprequest = request.httprequest
        return ui_context.collect(httprequest.headers.get(ui_context.HEADER), request.params,
                                  httprequest.path)
    except Exception:
        _logger.warning('perf_monitor_agent: cannot read the UI context', exc_info=True)
        return {}


# ------------------------------------------------------------------------------------ cron
def begin_cron(cron, cron_name, server_action_id):
    env = cron.env
    enabled, slow_ms = get_config(env)
    if not enabled:
        return None
    action = env['ir.actions.server'].sudo().browse(server_action_id)
    model_name = action.model_id.model or False
    match = _RX_CRON_METHOD.search(action.code or '') if action.state == 'code' else None
    method = match.group(1) if match else (action.state or False)
    login = env.user.sudo().login
    name = build_app_name(env.cr.dbname, env.uid, login, '%s.%s' % (model_name or '-', method or '-'),
                          'cron:%s' % (cron_name or ''))
    probe = CallProbe('cron', slow_ms, {'uid': env.uid, 'login': login, 'model_name': model_name,
                                        'method': method, 'route': False, 'cron_name': cron_name}, name)
    # session level: cron code commits in batches (SET LOCAL would be lost at the first commit)
    probe.app_name = name if _tag(env.cr, name, local=False) else None
    probe.install()
    return probe


def end_cron(cron, probe):
    try:
        probe.uninstall()
        cr = cron.env.cr
        if probe.app_name and not cr.closed and not _tx_in_error(cr):
            # committed with the job cursor (ir.cron._process_job commits it after _callback)
            cr.execute('RESET application_name', log_exceptions=False)
        duration_ms = probe.elapsed_ms()
        if duration_ms >= probe.slow_ms:
            record_slow_call(cron.env.registry, probe, duration_ms, None)
    except Exception:
        _logger.warning('perf_monitor_agent: end_cron failed', exc_info=True)


# ------------------------------------------------------------------------------------ tiles
TILE_MARK = '_perf_monitor_agent_tile'
# odoo_dashboards_saas: model -> methods computing ONE tile, with where the tile is in their arguments
# ('obj', i, kw): a data.analytics.saas record, ('id', i, kw): its id (i = index after self)
TILE_METHODS = {
    'dataset.saas': {name: ('obj', 0, 'data_analytics_obj') for name in (
        'get_tile_data', 'get_bar_chart_data', 'get_heatmap_data', 'get_map_data', 'get_table_data')},
    'dashboards.saas': {'_get_available_filters': ('id', 0, 'data_analytics_obj_id')},
}
# plain functions of odoo_dashboards_saas imported by name into a models module: (module, name, spec)
TILE_FUNCTIONS = (('odoo.addons.odoo_dashboards_saas.models.dashboards', 'get_customer_or_vendor_ageing_report_data',
                   ('obj', 1, 'data_analytics_obj')),)


def _tile_id(spec, args, kwargs):
    how, index, name = spec
    value = kwargs.get(name) if name in kwargs else (args[index] if len(args) > index else None)
    if how == 'obj':
        value = getattr(value, 'id', None) if getattr(value, '_name', None) == ui_context.TILE_MODEL else None
    return ui_context.to_id(value)


def _tile_wrapper(func, spec, is_method):
    if is_method:
        def wrapper(self, *args, **kwargs):
            probe = getattr(_local, 'probe', None)
            if probe is None:
                return func(self, *args, **kwargs)
            try:
                tile_id = _tile_id(spec, args, kwargs)
            except Exception:
                tile_id = None
            return time_tile(probe, tile_id, func, (self,) + args, kwargs)
    else:
        def wrapper(*args, **kwargs):
            probe = getattr(_local, 'probe', None)
            if probe is None:
                return func(*args, **kwargs)
            try:
                tile_id = _tile_id(spec, args, kwargs)
            except Exception:
                tile_id = None
            return time_tile(probe, tile_id, func, args, kwargs)
    functools.update_wrapper(wrapper, func)
    setattr(wrapper, TILE_MARK, True)
    return wrapper


def install_tile_timers(env):
    """Wrap the per-tile methods of odoo_dashboards_saas in this registry (its model classes are rebuilt,
    and this called again, on every registry load). No-op when the module is not installed. Returns the
    number of callables wrapped now."""
    wrapped = 0
    for model, methods in TILE_METHODS.items():
        if model not in env:
            continue
        cls = type(env[model])
        for name, spec in methods.items():
            func = getattr(cls, name, None)
            if callable(func) and not getattr(func, TILE_MARK, False):
                setattr(cls, name, _tile_wrapper(func, spec, True))
                wrapped += 1
    if ui_context.TILE_MODEL in env:
        for module_name, name, spec in TILE_FUNCTIONS:
            module = sys.modules.get(module_name)
            func = getattr(module, name, None)
            if callable(func) and not getattr(func, TILE_MARK, False):
                setattr(module, name, _tile_wrapper(func, spec, False))
                wrapped += 1
    return wrapped


# ------------------------------------------------------------------------------------ record
UI_COLS = ('ui_action_id', 'ui_action', 'ui_menu_id', 'ui_menu', 'ui_model', 'ui_record_id', 'ui_view_type',
           'ui_dashboard_model', 'ui_dashboard_id', 'ui_dashboard', 'ui_tile_id', 'ui_tile', 'ui_tile_type',
           'tile_ms', 'tile_count', 'user_name')


def ui_raw_vals(ui):
    """ui_* columns from the raw UI context (ids as sent; names are filled by resolve_ui)."""
    ui = ui or {}
    action = ui.get('action')
    if ui.get('record_id'):
        model, record_id = ui.get('model'), ui['record_id']
    elif ui.get('active_id') and ui.get('active_model'):
        model, record_id = ui['active_model'], ui['active_id']
    else:
        model, record_id = ui.get('model') or ui.get('active_model'), None
    return {
        'ui_action_id': action if isinstance(action, int) else None,
        'ui_action': action[:256] if isinstance(action, str) else None,  # xmlid / client tag until resolved
        'ui_menu_id': ui.get('menu_id'), 'ui_menu': None,
        'ui_model': model, 'ui_record_id': record_id, 'ui_view_type': (ui.get('view_type') or '')[:32] or None,
        'ui_dashboard_model': ui.get('dashboard_model'), 'ui_dashboard_id': ui.get('dashboard_id'),
        'ui_dashboard': None,
        'ui_tile_id': ui.get('tile_id'), 'ui_tile': None, 'ui_tile_type': None, 'tile_ms': None, 'tile_count': None,
        'user_name': None,
    }


def _name(value):
    return (value or '')[:256] or None


def _menu_of(env, act, menu_id):
    """Menu of the screen: the menu clicked (header / URL), refined to its descendant opening ``act`` (the
    URL hash of Odoo 16 names the app's root menu), or the first menu opening ``act`` when none was sent."""
    menus = env['ir.ui.menu'].with_context(**{'ir.ui.menu.full_list': True})
    menu = menus.browse(menu_id).exists() if menu_id else menus
    opens = menu.action if menu else None  # Reference field: a record or False
    if act and (not opens or (opens._name, opens.id) != (act._name, act.id)):
        domain = [('action', '=', '%s,%d' % (act._name, act.id))]
        leaf = menus.search(domain + ([('id', 'child_of', menu.id)] if menu else []), order='id', limit=1)
        if not leaf and not menu:
            leaf = menus.search(domain, order='id', limit=1)
        menu = leaf or menu
    return menu


def resolve_ui(env, vals, ui):
    """Fill the action / menu / dashboard / tile / user names of ``vals`` (superuser env of the
    recording cursor)."""
    ref = ui.get('action')
    act = env['ir.actions.actions']
    if isinstance(ref, int):
        act = act.browse(ref).exists()
    elif ref and '.' in ref:
        rec = env.ref(ref, raise_if_not_found=False)
        act = act.browse(rec.id) if rec is not None and rec._name.startswith('ir.actions.') else act
    if vals.get('ui_tile_id') and ui_context.TILE_MODEL in env:
        tile = env[ui_context.TILE_MODEL].browse(vals['ui_tile_id']).exists()
        if tile:
            vals['ui_tile'], vals['ui_tile_type'] = _tile_name_type(tile)
            dashboard = tile['dashboard_id'] if 'dashboard_id' in tile._fields else None
            if dashboard:  # the tile's own dashboard: exact, whatever the screen said
                vals['ui_dashboard_model'], vals['ui_dashboard_id'] = dashboard._name, dashboard.id
    if not act and vals['ui_dashboard_model'] == ui_context.SAAS_DASHBOARD_MODEL and vals['ui_dashboard_id'] \
            and ui_context.SAAS_DASHBOARD_MODEL in env:
        dashboard = env[ui_context.SAAS_DASHBOARD_MODEL].browse(vals['ui_dashboard_id']).exists()
        opens = dashboard['action_id'] if dashboard and 'action_id' in dashboard._fields else None
        act = env['ir.actions.actions'].browse(opens.id) if opens else act
    res_model = tag = None
    if act:
        act = env[act.type].browse(act.id) if act.type in env else act
        vals['ui_action_id'], vals['ui_action'] = act.id, _name(act.name)
        res_model = act.res_model if act._name == 'ir.actions.act_window' else None
        tag = act.tag if act._name == 'ir.actions.client' else None
        vals['ui_model'] = vals['ui_model'] or res_model or None
    menu = _menu_of(env, act, vals['ui_menu_id'])
    vals['ui_menu_id'], vals['ui_menu'] = (menu.id, _name(menu.complete_name)) if menu else (vals['ui_menu_id'], None)
    dmodel, did, dname = vals['ui_dashboard_model'], vals['ui_dashboard_id'], None
    if not dmodel and act:
        dmodel, did = ui_context.dashboard_from_action_context(act.context if 'context' in act._fields else None)
        if not dmodel and res_model == ui_context.BOARD_MODEL:
            dmodel, dname = ui_context.BOARD_MODEL, act.name  # "My Dashboard": one board per user
        elif not dmodel and tag == ui_context.SPREADSHEET_TAG:
            dmodel, did = ui_context.SPREADSHEET_MODEL, ui.get('spreadsheet_id')
    if not dmodel and ui.get('spreadsheet_id'):
        dmodel, did = ui_context.SPREADSHEET_MODEL, ui['spreadsheet_id']
    if dmodel and did and dmodel in env:
        dashboard = env[dmodel].browse(did).exists()
        dname = dashboard.display_name if dashboard else None
    if dmodel and not dname and act:
        dname = act.name
    vals.update(ui_dashboard_model=dmodel or None, ui_dashboard_id=did or None, ui_dashboard=_name(dname))
    return vals


def _tile_name_type(tile):
    kind = tile['analytical_type'] if 'analytical_type' in tile._fields else None
    return _name(tile.display_name), (kind or '')[:32] or None


def resolve_user(env, vals):
    if vals.get('uid'):
        user = env['res.users'].browse(vals['uid']).exists()
        vals['user_name'] = _name(user.name) if user else None
    return vals


def tile_names(env, tile_ids):
    """{tile id: (name, type)} of data.analytics.saas records ({} when the model is not installed)."""
    if not tile_ids or ui_context.TILE_MODEL not in env:
        return {}
    tiles = env[ui_context.TILE_MODEL].browse(list(tile_ids)).exists()
    return {t.id: _tile_name_type(t) for t in tiles}


def slow_call_vals(probe, duration_ms, error=None, ui=None):
    info = probe.info
    vals = {
        'ts': datetime.utcfromtimestamp(probe.start_wall).replace(microsecond=0),
        'duration_ms': round(duration_ms, 1),
        'kind': probe.kind,
        'uid': info.get('uid') or None,
        'login': (info.get('login') or '')[:128] or None,
        'model_name': (info.get('model_name') or '')[:128] or None,
        'method': (info.get('method') or '')[:128] or None,
        'route': (info.get('route') or '')[:256] or None,
        'cron_name': (info.get('cron_name') or '')[:256] or None,
        'app_name': probe.app_name or None,
        'sql_count': probe.sql_count,
        'sql_ms': round(probe.sql_s * 1000.0, 1),
        'error': (type(error).__name__ if error is not None else '')[:128] or None,
        'parent_id': None,
    }
    vals.update(ui_raw_vals(ui))
    vals.update(_top_vals(probe))
    return vals


_INSERT_COLS = ('ts', 'duration_ms', 'kind', 'uid', 'login', 'model_name', 'method', 'route', 'cron_name',
                'app_name', 'sql_count', 'sql_ms', 'error', 'top_sql', 'top_sql_ms', 'top_sql_calls',
                'top_sql_fingerprint', 'top_sql_callsite', 'parent_id') + UI_COLS


def _top_vals(probe):
    top = probe.top()
    if not top:
        return {'top_sql': None, 'top_sql_ms': 0.0, 'top_sql_calls': 0, 'top_sql_fingerprint': None,
                'top_sql_callsite': None}
    query, calls, total_ms, callsite = top
    if isinstance(query, bytes):
        query = query.decode('utf-8', 'replace')
    return {
        'top_sql': redact_literals(query)[:TOP_SQL_MAX],
        'top_sql_ms': round(total_ms, 1),
        'top_sql_calls': calls,
        'top_sql_fingerprint': sql_fingerprint(query) or None,
        'top_sql_callsite': (callsite or '')[:256] or None,
    }


def tile_row_vals(parent_vals, stat, names):
    """Row of one tile of a slow call: the call's context, the tile's own time / SQL."""
    vals = dict(parent_vals, kind='tile', duration_ms=round(stat.ms, 1), sql_count=stat.sql_count,
                sql_ms=round(stat.sql_ms, 1), error=None, ui_tile_id=stat.tile_id, tile_ms=round(stat.ms, 1),
                tile_count=None)
    vals['ui_tile'], vals['ui_tile_type'] = names.get(stat.tile_id, (None, None))
    vals.update(_top_vals(stat.probe))
    return vals


def _insert(cr, vals):
    cr.execute(
        'INSERT INTO perf_agent_slow_call (%s, pushed, create_uid, create_date, write_uid, write_date) '
        'VALUES (%s, false, %%s, (now() at time zone \'UTC\'), %%s, (now() at time zone \'UTC\')) RETURNING id'
        % (', '.join(_INSERT_COLS), ', '.join(['%s'] * len(_INSERT_COLS))),
        [vals.get(c) for c in _INSERT_COLS] + [SUPERUSER_ID, SUPERUSER_ID], log_exceptions=False)
    return cr.fetchone()[0]


def record_slow_call(registry, probe, duration_ms, error=None, ui=None):
    """INSERT the call's row (and one row per tile it computed, when more than one) on a separate cursor
    (committed on its own, not tagged / counted)."""
    if not _limiter.allow():
        return False
    vals = slow_call_vals(probe, duration_ms, error, ui)
    tiles = slowest_tiles(probe, MAX_TILE_ROWS)
    if tiles:  # the call's tile is its slowest one (the only one of a per-tile RPC)
        vals.update(ui_tile_id=tiles[0].tile_id, tile_ms=round(tiles[0].ms, 1), tile_count=len(probe.tiles))
    names = {}
    with registry.cursor() as cr:
        try:
            with cr.savepoint(flush=False):
                env = api.Environment(cr, SUPERUSER_ID, {})
                resolve_user(env, vals)
                if ui or vals['ui_tile_id']:
                    resolve_ui(env, vals, ui or {})
                if len(tiles) > 1:
                    names = tile_names(env, [t.tile_id for t in tiles])
        except Exception:
            _logger.warning('perf_monitor_agent: cannot resolve the UI context', exc_info=True)
        parent_id = _insert(cr, vals)
        if len(tiles) > 1:
            parent = dict(vals, parent_id=parent_id)
            for stat in tiles:
                if not _limiter.allow():
                    break
                _insert(cr, tile_row_vals(parent, stat, names))
    return True
