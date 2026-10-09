# -*- coding: utf-8 -*-
"""Pure-python core of the agent (no Odoo import, unit-testable without a database).

* ``build_app_name``: the PostgreSQL application_name tag
  ``odoo|<db>|<uid>|<login>|<model.method>|<route or cron:name>`` (printable ASCII, max 63 bytes).
* ``CallProbe``: an ``odoo.sql_db`` query hook (``threading.current_thread().query_hooks``, called by
  ``Cursor.execute`` after every statement) counting statements / SQL time and aggregating time per
  query template to find the top SQL of a call and its Python call site.
* ``RateLimiter``: per-process cap of slow-call rows written per minute (a DB-wide slowdown must not
  turn into one extra INSERT per request).
* ``time_tile``: per-tile timing inside one call (odoo_dashboards_saas computes every tile of a dashboard in
  one RPC): wall time plus a nested ``CallProbe`` per tile (its own SQL count / time / top SQL / call site).

Everything here runs on the request path: keep it allocation-light and never raise.
"""
import os
import sys
import threading
import time

NAMEDATALEN_MAX = 63  # PostgreSQL truncates application_name to NAMEDATALEN - 1 bytes
MAX_TEMPLATES = 2000  # distinct query templates tracked per call (bounded memory)
MAX_CALLSITE_WALKS = 8  # stack walks per call (only when the top template changes)
MAX_FRAMES = 80
_FIELD_MAX = 64

# printable ASCII only (PostgreSQL escapes anything else), '|' is our separator
_TRANS = {i: '?' for i in range(0, 256) if i < 32 or i > 126}
_TRANS[ord('|')] = '/'


def _clean(value, size=_FIELD_MAX):
    if value is None or value is False:
        return ''
    if not isinstance(value, str):
        value = str(value)
    value = value[:size].translate(_TRANS)
    # non-latin-1 code points are not in the table: map anything non-ASCII left to '?'
    return value if value.isascii() else ''.join(c if c.isascii() else '?' for c in value)


def build_app_name(db, uid, login, model_method, where):
    """``odoo|<db>|<uid>|<login>|<model.method>|<where>`` sanitized to printable ASCII and cut to 63
    bytes (so the value stored by PostgreSQL is exactly what we built)."""
    name = 'odoo|%s|%s|%s|%s|%s' % (_clean(db), _clean(uid or 0), _clean(login) or '-',
                                    _clean(model_method, 128) or '-', _clean(where, 256) or '-')
    return name[:NAMEDATALEN_MAX]


def parse_app_name(name):
    """Inverse of build_app_name for untruncated tags: dict or None (used by tests / tooling)."""
    parts = (name or '').split('|')
    if len(parts) != 6 or parts[0] != 'odoo':
        return None
    return dict(zip(('db', 'uid', 'login', 'model_method', 'where'), parts[1:]))


def short_route(path):
    """Route part of the tag: call_kw / call_button paths drop their redundant /<model>/<method>."""
    if not path:
        return '-'
    for prefix in ('/web/dataset/call_kw', '/web/dataset/call_button'):
        if path.startswith(prefix):
            return prefix
    return path


# ------------------------------------------------------------------------------------ call site
_ODOO_DIR = None
_OWN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep
_STDLIB_DIR = os.path.dirname(os.path.abspath(os.__file__)) + os.sep
# generic RPC / cron plumbing living in addons: never the interesting caller
_PLUMBING = tuple(os.sep + p.replace('/', os.sep) for p in (
    'web/controllers/dataset.py', 'web/models/models.py', 'base/models/ir_http.py',
    'base/models/ir_cron.py', 'base/models/ir_actions.py', 'http_routing/models/ir_http.py', 'odoo-bin'))
# monitoring addons that wrap the cursor: wags_logger patches cursor.execute (every query passes through
# wags_logger/models/wags_logger.py _patched_cursor_execute), so its frame is never the real call site
_SKIPPED_ADDON_DIRS = tuple(os.sep + d + os.sep for d in ('wags_logger',))


def _odoo_dir():
    global _ODOO_DIR
    if _ODOO_DIR is None:
        mod = sys.modules.get('odoo')
        path = getattr(mod, '__file__', None)
        _ODOO_DIR = (os.path.dirname(os.path.abspath(path)) + os.sep) if path else ''
    return _ODOO_DIR


def _is_framework(filename, odoo_dir):
    """ORM / SQL / HTTP plumbing, this module and wags_logger: not an interesting call site. Addons shipped
    inside the odoo package (odoo/addons/base ...) count as business code."""
    if filename.startswith(_OWN_DIR) or filename.endswith(_PLUMBING) or any(
            part in filename for part in _SKIPPED_ADDON_DIRS):
        return True
    if odoo_dir and filename.startswith(odoo_dir):
        return not filename.startswith(odoo_dir + 'addons' + os.sep)
    return filename.startswith(('<', _STDLIB_DIR)) or os.sep + 'site-packages' + os.sep in filename


def _short_path(filename):
    parts = filename.replace('\\', '/').split('/')
    return '/'.join(parts[-3:])


def find_callsite(frame=None):
    """``path/tail.py:line (function)`` of the innermost frame outside the ORM / SQL / HTTP layers
    (falls back to the innermost frame outside this module)."""
    try:
        frame = frame or sys._getframe(1)
        odoo_dir = _odoo_dir()
        fallback = None
        depth = 0
        while frame is not None and depth < MAX_FRAMES:
            code = frame.f_code
            filename = code.co_filename
            if not _is_framework(filename, odoo_dir):
                return '%s:%d (%s)' % (_short_path(filename), frame.f_lineno, code.co_name)
            if fallback is None and not filename.startswith(_OWN_DIR) and not filename.endswith('sql_db.py') \
                    and not any(part in filename for part in _SKIPPED_ADDON_DIRS):
                fallback = '%s:%d (%s)' % (_short_path(filename), frame.f_lineno, code.co_name)
            frame = frame.f_back
            depth += 1
        return fallback or ''
    except Exception:  # pragma: no cover - never break the caller
        return ''


# ------------------------------------------------------------------------------------ probe
class CallProbe(object):
    """One HTTP request / cron run. Installed in ``thread.query_hooks`` by ``install()`` and
    removed by ``uninstall()``; ``__call__`` is the per-statement hook (the only per-query cost)."""
    __slots__ = ('kind', 'start', 'start_wall', 'sql_count', 'sql_s', 'stats', 'top_key', 'top_s',
                 'walks', 'thread', 'info', 'app_name', 'slow_ms', 'tiles', 'in_tile')

    def __init__(self, kind, slow_ms, info, app_name):
        self.kind = kind
        self.slow_ms = slow_ms
        self.info = info  # dict: uid, login, model_name, method, route, cron_name
        self.app_name = app_name
        self.sql_count = 0
        self.sql_s = 0.0
        self.stats = {}  # query template -> [calls, seconds, callsite]
        self.top_key = None
        self.top_s = 0.0
        self.walks = 0
        self.thread = None
        self.tiles = None  # tile id -> TileStat (time_tile), None when the call computed no tile
        self.in_tile = False
        self.start_wall = time.time()
        self.start = time.perf_counter()

    def __call__(self, cr, query, params, start, delay):
        try:
            self.sql_count += 1
            self.sql_s += delay
            key = query if type(query) is str else repr(query)  # psycopg2.sql.Composed is unhashable
            st = self.stats.get(key)
            if st is None:
                if len(self.stats) >= MAX_TEMPLATES:
                    return
                st = self.stats[key] = [0, 0.0, None]
            st[0] += 1
            st[1] += delay
            if st[1] > self.top_s:
                self.top_s = st[1]
                if key is not self.top_key:
                    self.top_key = key
                    if st[2] is None and self.walks < MAX_CALLSITE_WALKS:
                        self.walks += 1
                        st[2] = find_callsite(sys._getframe(1))
        except Exception:  # pragma: no cover - a hook must never break a query
            pass

    def install(self):
        thread = threading.current_thread()
        hooks = getattr(thread, 'query_hooks', None)
        if hooks is None:
            thread.query_hooks = hooks = []
        hooks.append(self)
        self.thread = thread

    def uninstall(self):
        thread, self.thread = self.thread, None
        if thread is None:
            return
        hooks = getattr(thread, 'query_hooks', None)
        if hooks:
            for i in range(len(hooks) - 1, -1, -1):
                if hooks[i] is self:
                    del hooks[i]
                    break

    def elapsed_ms(self):
        return (time.perf_counter() - self.start) * 1000.0

    def top(self):
        """(template, calls, total_ms, callsite) of the query template with the most total time."""
        if self.top_key is None:
            return None
        calls, seconds, callsite = self.stats[self.top_key]
        return self.top_key, calls, seconds * 1000.0, callsite or ''


# ------------------------------------------------------------------------------------ tiles
MAX_TILES = 100  # tiles timed per call (bounded memory); later tiles run untimed


class TileStat(object):
    """Time spent computing one tile / widget during a call (all its timed sections summed)."""
    __slots__ = ('tile_id', 'ms', 'sections', 'probe')

    def __init__(self, tile_id):
        self.tile_id = tile_id
        self.ms = 0.0
        self.sections = 0
        self.probe = CallProbe('tile', 0, None, None)  # SQL of this tile only

    @property
    def sql_count(self):
        return self.probe.sql_count

    @property
    def sql_ms(self):
        return self.probe.sql_s * 1000.0


def time_tile(probe, tile_id, func, args, kwargs):
    """``func(*args, **kwargs)``, its wall time and SQL added to ``probe.tiles[tile_id]``. Nested timed
    sections (a timed method calling another one) are counted once, by the outermost section."""
    if probe is None or probe.in_tile or not isinstance(tile_id, int) or isinstance(tile_id, bool):
        return func(*args, **kwargs)
    stat = None
    try:
        tiles = probe.tiles
        if tiles is None:
            tiles = probe.tiles = {}
        stat = tiles.get(tile_id)
        if stat is None and len(tiles) < MAX_TILES:
            stat = tiles[tile_id] = TileStat(tile_id)
        if stat is not None:
            probe.in_tile = True
            stat.probe.install()
    except Exception:  # pragma: no cover - timing must never break the tile
        stat = None
    if stat is None:
        return func(*args, **kwargs)
    start = time.perf_counter()
    try:
        return func(*args, **kwargs)
    finally:
        try:
            stat.ms += (time.perf_counter() - start) * 1000.0
            stat.sections += 1
            stat.probe.uninstall()
        finally:
            probe.in_tile = False


def slowest_tiles(probe, limit=MAX_TILES):
    """TileStats of a call, slowest first ([] when it computed no tile)."""
    tiles = getattr(probe, 'tiles', None)
    if not tiles:
        return []
    return sorted(tiles.values(), key=lambda t: t.ms, reverse=True)[:limit]


class RateLimiter(object):
    """At most ``limit`` events per rolling-minute window per process (thread-safe)."""

    def __init__(self, limit):
        self.limit = limit
        self.lock = threading.Lock()
        self.minute = None
        self.count = 0

    def allow(self, now=None):
        minute = int((now if now is not None else time.time()) // 60)
        with self.lock:
            if minute != self.minute:
                self.minute, self.count = minute, 0
            self.count += 1
            return self.count <= self.limit
