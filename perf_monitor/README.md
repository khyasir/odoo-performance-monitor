# Performance Monitor (perf_monitor)

Odoo 16 module (author wags.sa): server metrics, top SQL, live queries, slow calls pushed by
`perf_monitor_agent`, performance issues built by rules, and an optional AI layer (Amazon Bedrock).
The full feature list is in `__manifest__.py`; the changes per version are in `CHANGELOG.md`.

## Deploy notes

### Optional Python packages

Neither package is listed in `external_dependencies`: the module installs and runs without them.

| Package    | Used for | Without it |
|------------|----------|------------|
| `boto3`    | AI analysis / chat (Bedrock Converse, IAM role of the server) | AI calls fail with an error, logged on the issue |
| `tiktoken` | token **estimate** of each AI prompt before it is sent, only when the system parameter `perf_monitor.ai_tiktoken` is `1` (default off) | estimate = characters / 4 |

```
pip install tiktoken   # optional, in the Odoo virtualenv (Python 3.8 compatible versions)
# then Settings > Technical > System Parameters: perf_monitor.ai_tiktoken = 1
```

tiktoken downloads its encoding file (`cl100k_base`) on first use and caches it (`TIKTOKEN_CACHE_DIR`).
That download has no timeout, so it is only ever attempted from an AI run (cron / button), never when a
form opens (the Claude Code prompt size and the "Input sent to AI" sizes always use characters / 4), and
only when `perf_monitor.ai_tiktoken` is on. On a server without internet access, pre-fill that cache or
leave the parameter off: when the encoding cannot be loaded the module falls back to characters / 4
(tried once per worker process).
The estimate is display-only: costs and limits use the token counts returned by Bedrock
(`usage.inputTokens` / `usage.outputTokens`) and the existing upper estimate (characters / 3).

### AI models table (16.0.1.12.0)

*Performance > Configuration > AI models* (Perf Monitor Managers): one row per Bedrock model or inference
profile with its region, USD prices per million input / output tokens (cost log) and **Send temperature**.

* Seeded rows: **Haiku 4.5** (`eu.anthropic.claude-haiku-4-5-20251001-v1:0`, 1 / 5 USD, temperature sent) and
  **Opus 5.5** (`eu.anthropic.claude-opus-5-5`, 4 / 20 USD, temperature NOT sent: Bedrock answers
  `ValidationException: temperature is deprecated for this model`). Check the prices of your region.
* *Settings > Performance > Default AI model* is used by every issue whose **AI model** is empty: new issues
  are created with it empty (the form shows the placeholder *Settings default*) and the default is read at
  run time, so changing it also applies to issues created earlier and not analysed yet. Every issue has an
  **AI model** field in its *AI advice* tab (used by *Re-analyse*); the chat has its own selector (empty =
  the issue's model).
* A call refused because of the temperature is retried once without it (warning in the log): untick
  *Send temperature* on that row.
* Upgrade: the migration links the default to the old `perf_monitor.ai_model_id` (+ region and price
  parameters, a row is created when the id is not in the table) and turns an old "deep analyse" model id into
  a row; existing issues keep an empty AI model (= the default). The old parameters stay as a fallback when
  the table has no active row. The *Deep analyse* button is gone: choose the stronger model in the issue's AI
  model field.
* `perf_monitor.ai_max_input_tokens` default is now 150000 per run (an install still on the old default
  60000 is raised to 150000 by the migration; any other saved value is kept).

### Upgrade

```
odoo-bin -d <db> -u perf_monitor --stop-after-init
```

The 16.0.1.12.0 migration is idempotent (AI models / default, chat sessions for the existing chat messages,
call sites recomputed for issues that pointed at `wags_logger/models/wags_logger.py`).

Note: the call site itself is computed by `perf_monitor_agent` on the monitored databases (16.0.1.4.0 skips
`wags_logger/` frames itself). perf_monitor now
ignores frames of `wags_logger/` (its cursor patch), `odoo/sql_db.py`, `odoo/models.py` and the monitors, and
takes the first application frame among the frames / calls it receives; when only the cursor patch is known
the issue has no call site rather than a wrong one.
(The first application frame is picked before the stored call site is clipped to 256 characters.)

### "Input sent to AI": who can read it, retention

* The tabs *Input sent to AI* and *AI calls* of an issue (and the AI call log) are readable by **every
  Performance user (viewer)**, not only managers: this is a deliberate owner decision. The stored input is the
  full system prompt, the prompt (SQL without literal values, plan, table sizes, the code around the call
  site), every tool call and tool result of the run (source files of this server's addons, up to 20,000
  characters each, index definitions, module links) and the lessons. So viewers can read addon source code
  through it, while the *Code* and *Claude Code Prompt* tabs stay manager-only. Restrict it with field
  `groups` if that ever changes.
* **In short: viewers can read the AI input, including the tool results with this server's addon source code,
  and the AI error text** (the *error* column of *AI calls* and the issue's AI error: raw Bedrock / boto
  exception text, which can contain the IAM role ARN and AWS account id).
* Retention: the daily cron *Perf Monitor: Purge old agent slow calls* also blanks `input_system`,
  `input_prompt` and `rounds_log` of the AI calls older than `perf_monitor.ai_input_retention_days`
  (default 30; 0 = keep forever). The call rows (tokens, cost, status, model, prices) are kept: they feed the
  daily / monthly AI limits and the issue totals.

### Chat guard

A message that is only a greeting / thanks ("hi", "salam", "shukriya", "ok") gets a canned answer without a model
call. The chat refuses, without a model call, only narrow phrasings: a secret noun (API key, password, token,
credentials ...) as the object of a request ("show me the api key", "what is the password?", "api key
batao"), prompt extraction / injection ("ignore previous instructions", "you are now a ...", "enter developer
mode"), asking for config files ("cat odoo.conf") and clear off-topic requests without context. Normal Odoo
vocabulary passes ("does this only happen in developer mode?", "workers in odoo.conf?", "show me the access
token validation code"). It is a cost filter, not a security boundary.

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
