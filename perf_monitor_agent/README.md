# perf_monitor_agent (Odoo 16)

Companion of `perf_monitor`, installed in each **monitored** database (no dependency on perf_monitor;
destined for the wags-extra repo).

* PostgreSQL `application_name` per HTTP request / cron run:
  `odoo|<db>|<uid>|<login>|<model.method>|<route or cron:name>` (printable ASCII, 63 bytes max).
* Slow calls (`perf_monitor_agent.slow_ms`, default 2000) stored in `perf.agent.slow.call`
  (Settings > Technical > Slow Calls (agent)), 7 days (`Perf Monitor: agent cleanup`), with their UI
  context: dashboard, menu, action, model, record id (`X-Perf-UI` header added by
  `static/src/js/perf_ui.js` to every web client RPC, parsed only for slow calls by `tools/ui_context.py`),
  the dashboard TILE (odoo_dashboards_saas: slow multi-tile calls are timed per tile, one `tile` row per
  tile) and the user name.
* Call site: the first frame outside the ORM / SQL / HTTP layers, this module and `wags_logger/` (its cursor
  patch `_patched_cursor_execute` wraps every query: never the real caller).
* Settings > Perf Monitor Agent (admins): enabled, slow_ms, central URL / DB (defaults
  `https://central.example.com` / `demo_db`), central token (write-only).
* `Perf Monitor: agent push` sends them to the central perf_monitor
  (`perf_monitor_agent.central_url` / `central_token` / optional `central_db`).
* `Perf Monitor: agent collect` (every 5 min, `models/perf_agent_collector.py` + `tools/collect.py`):
  in-agent collector, module only (no root script / `perf_monitor_ro` role). Pushes DB latency, Top SQL
  (pg_stat_statements; when missing / unreadable: skipped, logged ONCE per process and state change at INFO,
  reported as `pgss` = ok / missing / unreadable, see below) and live / blocked queries of this DB, redacted
  (`tools/perf_redact.py`, verbatim copy of `perf_monitor/models/perf_redact.py`), to
  `/perf_monitor/api/v1/push` with the same token; host CPU / RAM / disk / load / net (psutil or /proc)
  only when `perf_monitor_agent.send_host_metrics` is on: enable it in ONE database per host.
  Switch: `perf_monitor_agent.collect_enabled` (default on). Previous counters are kept in
  `perf.agent.collector` (JSON state), saved only after a successful push.

## pg_stat_statements (Top SQL): status and how to enable it

Top SQL needs the PostgreSQL extension `pg_stat_statements` in the **monitored** database. Nothing installs
it automatically: perf_monitor, perf_monitor_agent and the collector never run `CREATE EXTENSION`, never
change a parameter and never restart anything. Without it, metrics, live queries and slow calls still work;
only Top SQL is empty.

**Where to see it.** Every metrics push reports the status (payload key `pgss`): *Performance > Servers*
shows per server card / list row **Top SQL: ON**, **Top SQL OFF - pg_stat_statements missing** or
**Top SQL OFF - pg_stat_statements not readable** (filter "Top SQL OFF"), and the server form shows the fix.
"Unknown" means an older agent (< 16.0.1.4.0) or collector (< 1.0.2) that does not report it. On the
monitored server the agent / collector logs the situation ONCE per worker process and per change (INFO),
not every 5 minutes.

### AWS RDS PostgreSQL

1. RDS console > *Parameter groups* > the **DB parameter group** of the instance (a custom one: the
   `default.postgresNN` group cannot be edited; create a custom group and attach it to the instance if
   needed). Check `shared_preload_libraries`: on recent RDS PostgreSQL versions it already contains
   `pg_stat_statements`. If not, add it (comma-separated, keep the existing values).
2. Only if you changed `shared_preload_libraries` (or attached a new parameter group): **reboot** the
   instance (*Actions > Reboot*) in a maintenance window. The parameter is static: it applies only after a
   restart; the status "pending-reboot" shows it is not applied yet.
3. Connect to the **monitored database** (the Odoo database, not `postgres`) as the master user (member of
   `rds_superuser`) and run:
   ```sql
   CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
   ```
4. Check (as the master user and as the Odoo DB user):
   ```sql
   SELECT count(*) FROM pg_stat_statements;
   ```
   A number = OK. "must be loaded via shared_preload_libraries" = step 1/2 not applied yet (reboot).
   Within 5 minutes the server card shows **Top SQL: ON**.

### Self-managed PostgreSQL (postgresql.conf)

1. In `postgresql.conf` (or `ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';`, keeping
   any existing libraries in the list): `shared_preload_libraries = 'pg_stat_statements'`.
2. Restart PostgreSQL (e.g. `sudo systemctl restart postgresql`): a reload is not enough. All Odoo
   connections drop for the restart: do it in a maintenance window.
3. In the monitored database, as a superuser: `CREATE EXTENSION IF NOT EXISTS pg_stat_statements;`
4. Check: `SELECT count(*) FROM pg_stat_statements;`

### Risks (honestly)

* Small overhead on every statement (hashing + shared-memory counters; usually ~1-2 %, more for
  very short, very frequent queries). Memory: `pg_stat_statements.max` entries (default 5000) in shared
  memory.
* Changing `shared_preload_libraries` needs a **restart** of PostgreSQL (downtime of a few seconds to
  minutes; on RDS a reboot, Multi-AZ fails over).
* It stores the **normalized query text** (literal values replaced by `$1`, `$2` ...) of every statement,
  readable by superusers / `pg_read_all_stats` and by the user that ran the statement. perf_monitor
  redacts the texts again before they leave the server.
* To undo: `DROP EXTENSION pg_stat_statements;` (and remove it from `shared_preload_libraries` + restart).

## Layout and tests

Layout: `runtime.py` (request / cron instrumentation), `tools/probe.py` (pure python: tag, query hook,
call site, rate limiter), `tools/sql_fingerprint.py` (verbatim copy of
`perf_monitor/models/sql_fingerprint.py`, checked by `collector_tests`), `models/` (ir.http, ir.cron
overrides, local table + push / cleanup crons), `bench/bench_overhead.py` (overhead benchmark).

```bash
odoo-bin -d <testdb> -i perf_monitor_agent --test-enable --test-tags /perf_monitor_agent --stop-after-init
python3 bench/bench_overhead.py --micro
python3 bench/bench_overhead.py --url http://127.0.0.1:8069 --target with=<db_agent> --target without=<db_plain> \
    --login admin --password admin --n 500 --rounds 6
```

Deploy, configuration keys, overhead and limitations: `deploy/DEPLOY.md` (perf_monitor_agent section).
