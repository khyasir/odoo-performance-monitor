# Changelog (perf_monitor_agent)

## 16.0.1.4.0

- pg_stat_statements status: the collect payload carries `pgss` = `ok` / `missing` / `unreadable` (shown by
  perf_monitor >= 16.0.1.12.0 on the server card as "Top SQL: ON" / "Top SQL OFF - pg_stat_statements
  missing"; older perf_monitor versions ignore the key).
- No repeated warning in the Odoo log: a missing / unreadable pg_stat_statements is logged ONCE per worker
  process and per state change at INFO (it was a WARNING every 5 minutes). Nothing is ever installed: see the
  README for the manual steps (AWS RDS parameter group / postgresql.conf, `CREATE EXTENSION`) and the risks.
- Call site: frames of `wags_logger/` (its cursor patch wraps every query) are skipped like the ORM / SQL layer,
  also for the fallback frame.
- No schema change, no migration.

## 16.0.1.3.0

- In-agent collector (`Perf Monitor: agent collect`): DB latency, Top SQL, live queries and optional host metrics
  pushed to the central `/perf_monitor/api/v1/push`.
