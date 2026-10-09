# -*- coding: utf-8 -*-
{
    'name': 'Performance Monitor Agent',
    'version': '16.0.1.4.0',
    'category': 'Productivity/Performance',
    'summary': 'Slow calls, DB / host metrics, Top SQL and live queries pushed to a central perf_monitor '
               '(module only, no root collector)',
    'description': """
Performance Monitor Agent
=========================
Companion of perf_monitor, installed in each monitored database (does not depend on perf_monitor).

* PostgreSQL ``application_name`` set per HTTP request and per cron run to
  ``odoo|<db>|<uid>|<login>|<model.method>|<route or cron:name>`` (63 bytes max): visible in
  pg_stat_activity, pg_stat_statements tooling and the perf_monitor Live Queries.
* Slow calls (``perf_monitor_agent.slow_ms``, default 10000) recorded with user, model, method, route,
  duration, SQL count / time, top SQL (literals stripped + fingerprint) and its Python call site, plus
  the UI context: dashboard, menu, action, model, record id (``X-Perf-UI`` header added to every web client
  RPC; parsed only for slow calls).
* Dashboard tiles (odoo_dashboards_saas, optional): a call computing many tiles is timed per tile; the
  slow call names its slowest tile and gets one ``tile`` row per tile (time, SQL, top SQL, call site).
  Names (dashboard, tile, menu path, user) are resolved for slow calls only.
* ``Perf Monitor: agent push`` sends them to the central perf_monitor. Endpoint in Settings > Perf Monitor
  Agent (admins): central URL (default https://central.example.com) / DB (default demo_db) /
  token (write-only, never in code) / enabled / slow_ms;
  ``Perf Monitor: agent cleanup`` keeps 7 days (``perf_monitor_agent.retention_days``).
* In-agent collector (module only: no root script, no ``CHANGE_ME``, no ``perf_monitor_ro`` role):
  ``Perf Monitor: agent collect`` (every 5 minutes) pushes DB latency, Top SQL (pg_stat_statements; when
  missing / unreadable: skipped, logged once per process and change at INFO, reported to the central as
  ``pgss``; never installed automatically) and live / blocked queries of this database, read with the Odoo cursor and
  redacted before they leave, to the central ``/perf_monitor/api/v1/push`` with the same token; plus CPU /
  RAM / disk / load / network of the host (psutil if installed, else /proc) when "Send host metrics" is on
  (ONE database per host). Settings: ``perf_monitor_agent.collect_enabled`` / ``send_host_metrics``.
* Near-zero overhead: one ``SET LOCAL`` per request, no reads on the fast path; kill switch
  ``perf_monitor_agent.enabled = 0``.
""",
    'author': "wags.sa",
    'license': 'LGPL-3',
    'depends': ['base', 'web'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/ir.model.access.csv',
        'data/ir_config_parameter.xml',
        'data/ir_cron.xml',
        'views/perf_agent_slow_call_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'perf_monitor_agent/static/src/js/perf_ui.js',
        ],
    },
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'application': False,
}
