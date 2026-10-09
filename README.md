# Odoo Performance Monitor

Find the exact function, user, query and line of code behind Odoo slowness, always on, in production.

Built by **Yasir Rauf**, ERP Administrator, as the final capstone for **100x Engineers Cohort 7**.

## The problem

In Odoo, one slow function (an ORM call, compute field, cron or API call) holds a worker, and every other request waits behind it. The whole system feels slow for everyone, but the admin cannot see which function, user, module or line caused it. The usual fixes are restarts and bigger servers, which cost more and do not fix the code.

Existing tools each show one layer only:

| Tool | Gap |
|------|-----|
| Odoo built-in profiler | Manual, per request, not always on |
| pg_stat_statements | SQL only, no link to Odoo function or user |
| Sentry / generic APM | Sampling, not Odoo-aware, paid per server |

## Journey

1. **4 Jun 2026:** Discovery to MVP (100x Assignment 1).
2. **MVP:** about 60-70% technical, the rest manual. Used it to optimize functions, add indexes and right-size servers, saving around $1,000 in server cost.
3. **Final version (this repo):** fully automatic detection, grouping into issues, AI explanations and right-size advice.

## What is inside

Two Odoo 16 modules:

### `perf_monitor_agent` (install in every monitored database)
- Tags each DB connection with `application_name = odoo|<db>|<uid>|<login>|<model.method>|<route or cron>`, so every running query shows who and what caused it
- Records slow calls (default over 10 s): user, model, method, route, duration, SQL count and time, top SQL (literals removed), Python call site, UI context
- Times dashboard tiles one by one
- Built-in collector cron every 5 min: DB latency, `pg_stat_activity`, `pg_stat_statements` Top SQL, optional host CPU/RAM/disk
- Pushes to the central server over HTTPS with a per-server token
- Near-zero overhead on fast requests, kill switch included

### `perf_monitor` (central dashboard)
- Server cards with green / yellow / red status
- Metrics history, Top SQL, Live Queries, Slow Calls
- **Performance Issues:** auto-built from slow calls (per SQL fingerprint, `model.method` or tile), severity rules for duration, frequency, N+1 and sequential scans, plain explanations in English and Roman Urdu
- **AI analysis (optional):** Amazon Bedrock explains new issues and suggests fixes (advice only)
- **Right-size card:** 14-day p95 from CloudWatch (EC2 + RDS) with a recommended size
- **AI chat per issue:** ask follow-up questions, with per-answer model, tokens and cost
- **Add Server wizard:** one-time token and settings to paste

## Architecture

```
[Odoo DB A + perf_monitor_agent] --HTTPS + token--> [Central Odoo + perf_monitor] --> Dashboards, Issues, Right-size
[Odoo DB B + perf_monitor_agent] --HTTPS + token-->            |
                                                   AWS CloudWatch (read-only) --> Right-size card
                                                   Amazon Bedrock (optional)  --> AI advice
```

## Install

1. Back up the database.
2. Central Odoo: install `perf_monitor`. Go to Performance Monitor > **Add Server**, enter a unique name, copy the token.
3. Target database: install `perf_monitor_agent`.
4. Settings > Perf Monitor Agent: set Central URL, Central DB and token. Keep `slow_ms` at 10000.
5. Turn on **Send host metrics** in only one database per host.
6. For Top SQL: add `pg_stat_statements` to `shared_preload_libraries` and run `CREATE EXTENSION IF NOT EXISTS pg_stat_statements;`
7. In 5-10 minutes the server card turns green.

## Safety rules

- Measures and recommends only. No automatic index creation, config changes or resizing.
- SQL literals are removed before data leaves the database.
- Tokens are per server, never stored in code, and can be regenerated.
- Issues are never auto-closed. No emails, only Odoo To-Do activities.

## Tech

Modules: `perf_monitor` 16.0.1.12.0, `perf_monitor_agent` 16.0.1.4.0. Odoo 16 Community, Python 3.8+, PostgreSQL (AWS RDS), AWS EC2, CloudWatch, Amazon Bedrock. 256 automated tests (Python compile and XML checks pass).

## Author

**Yasir Rauf**, ERP Administrator, WAGS, Riyadh | 100x Engineers Cohort 7
