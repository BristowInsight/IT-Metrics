# Capacity Metrics Extract

A Fabric notebook that copies Microsoft Fabric Capacity Metrics into the IT_Metrics
Lakehouse one day at a time, so capacity history survives the short retention window
of the source model. Two Data Pipelines run it: Capacity Metrics Daily on a schedule,
and Capacity Metrics Smoke to check a change without touching the real history. This
page describes notebook version 3.1.0.

## Why this exists

The Fabric Capacity Metrics semantic model keeps only about 13 to 14 days of
item level and 30 second detail. Utilization, top consumers and headroom cannot be
trended month over month from the source alone. This notebook lands the same numbers
in Delta tables that keep growing.

## What it writes

Five Delta tables in the IT_Metrics Lakehouse. The Lakehouse has no schemas enabled,
so the names carry a prefix to stay clear of the existing tables. The prefix is the
`table_prefix` parameter: `capmetrics_` for the real history, which is what the
Daily pipeline uses, and `capmetrics_smoke_` for the Smoke pipeline, which writes the
same five tables under that prefix. The table descriptions below use `capmetrics_`.
Each run also writes an evidence file, described under "The evidence file".

### capmetrics_item_operation_day

One row per capacity, workspace, item, item kind, operation and date. About 1600 rows
per day for this capacity.

| Column | Type | Notes |
| --- | --- | --- |
| capacity_id | string | Uppercase, as the source model stores it |
| workspace_id | string | From the Items table |
| item_id | string | |
| item_kind | string | Dataset, Pipeline, Warehouse, Lakehouse and so on |
| operation_name | string | |
| date | date | Model local date, see the time zone note below |
| duration_s | double | |
| cu_s | double | Capacity units consumed, in CU seconds |
| throttling_min | double | |
| users | long | |
| operations | long | |
| successful | long | |
| rejected | long | |
| invalid | long | |
| failed | long | |
| cancelled | long | |
| loaded_at_utc | timestamp | When this notebook read the row |
| run_id | string | Joins to capmetrics_run_log |

Rows with zero CU seconds are not kept. On 2026-09-10 that excluded 252 of 1885 rows.
Of those 252, 248 were empty in every measure and 4 carried operation counts with no
CU consumption and no failures. See "Known limits" below.

### capmetrics_cu_window_30s

One row per 30 second window. A complete day is 2880 rows.

| Column | Type | Notes |
| --- | --- | --- |
| capacity_id | string | |
| window_date | date | The date used to delete and reload a day |
| window_start | timestamp | Model local, the timepoint itself |
| window_start_utc | timestamp | window_start plus 6 hours |
| cu_s | double | Absolute CU seconds in the window |
| background_cu | double | |
| interactive_cu | double | |
| background_nonbillable_cu | double | |
| interactive_nonbillable_cu | double | |
| cu_limit_norm | double | Normalized limit from the source, always 1.0 |
| interactive_delay_pct | double | |
| interactive_rejection_pct | double | |
| background_rejection_pct | double | |
| sku | string | P1 for this capacity |
| sku_cu | long | Capacity units, read from the source, 64 for P1 |
| budget_cu_s | double | sku_cu times 30, so 1920 for P1 |
| utilization_pct | double | cu_s divided by budget_cu_s, 1.0 means at the limit |
| window_hour | long | Hour of window_start in model local time, 0 to 23 |
| hour_start | timestamp | window_start floored to the hour, model local |
| loaded_at_utc | timestamp | |
| run_id | string | |

`window_hour` and `hour_start` exist for hour-of-day analysis in the semantic model.
Direct Lake has no calculated columns, so anything a report groups or filters by has
to be a real column in the table. Both are model local, like `window_start`: they
follow the model's fixed UTC minus 6 hours, not US Central wall clock time.

### capmetrics_items and capmetrics_capacities

Snapshots of the two Import tables in the source model, replaced whole on every run
and stamped with `snapshot_date`. Items carries item_id, workspace_id, workspace_name,
item_name, item_kind, billable_type, capacity_id and item_label. Capacities carries
capacity_id, capacity_name, sku, state, region and owners. Item_id was unique across
all 520 rows when this was built, so it is safe as a key.

| Column | Type | Notes |
| --- | --- | --- |
| item_label | string | Built by the notebook, after capacity_id and before snapshot_date. The item name, or the item_id when the name is null or blank, then a space and, in parentheses, item_kind and workspace_name separated by a comma and a space, a missing part left out and no parentheses when both are missing. For example `RAMCO Ingest CICD (DataflowFabric, DF RAMCO)`. item_kind is the model's own value |

`item_label` is the column the semantic model and the report show for an item,
because item names are not unique: in the 522 items captured on 2026-09-21, 66
names were shared by 209 items, and DataflowsStagingLakehouse alone was 42 items in
29 workspaces, while the label was unique for all 522. The label is kept unique
within each snapshot: when two or more rows would share a label, compared without
regard to case as the semantic model compares text, each of those rows gets a space
and the first 8 characters of its item_id in square brackets, so two items that
would both be `Sales (Dataset, My workspace)` become, for example,
`Sales (Dataset, My workspace) [1A2B3C4D]` and `Sales (Dataset, My workspace) [5E6F7A8B]`.
Rows whose label is already unique are left as they are, so on the 2026-09-21 data
no label carries a suffix. The rule can trigger where two workspaces share a name,
as personal workspaces all called "My workspace" do.

### capmetrics_run_log

One appended row per run: run_id, started_at_utc, finished_at_utc, days_in_scope,
write_mode, dates_processed, a count of rows written per table, status and
error_text. Status is one of:

| Status | Meaning |
| --- | --- |
| success | Every date and table loaded or kept, every read-back verified, snapshots written |
| partial | Something failed or did not verify, and something else succeeded |
| failed | Every date and table failed and so did the snapshots, or the probe failed on an error that waiting does not fix (a permission, a wrong id) |
| source_unavailable | The probe found the source not answering; nothing was loaded |

`dates_processed` lists every date in the window, whatever happened to it. The rows
counts are rows written by this run, so a date kept by the trim guard adds nothing.
`error_text` carries the full Python traceback of every failure, oldest first,
truncated at 4000 characters, because a failure inside a library cannot be diagnosed
from an exception type and message alone and the Spark session that held the log
output is gone by the time anyone looks. The evidence file carries the same
tracebacks without the truncation.

A dry run writes no run log row.

## The evidence file

Every run, a dry run included, writes one JSON document to the default Lakehouse at
`Files/<prefix>/runs/<run_id>.json`, and the same content to
`Files/<prefix>/runs/latest.json`, which each run overwrites. `<prefix>` is
`table_prefix` without its trailing underscore: `Files/capmetrics/runs/` for Daily,
`Files/capmetrics_smoke/runs/` for Smoke. It is written after the run log row and
before the job is failed, so a failed run leaves one too. The exceptions are a run
whose parameters are invalid, which stops before it opens, and a run whose evidence
write itself fails, which fails the job.

| Key | Holds |
| --- | --- |
| notebook_version | "3.1.0" |
| run_id | Also the file name, and the run_id on every row the run wrote |
| started_at_utc, finished_at_utc | ISO text, UTC |
| parameters | days_in_scope, capacity_id, metric_workspace, metric_dataset, write_mode, table_prefix, as the run used them |
| probe | date, attempts, seconds, outcome (ok, source_unavailable or failed), error |
| dates | One entry per date in the window, holding `item_operation_day` and `cu_window_30s` |
| snapshots | `items` and `capacities`: table, rows, action, error |
| run_log_row_written | true once the run log row is in |
| status | As in the run log |
| errors | Every failure as a full traceback |
| elapsed_s | Seconds from start to finish |

Each date and table entry holds `table`, `extracted_rows`, `extracted_cu_s`,
`existing_rows` (rows already stored for that date, null when the table did not
exist), `action`, `verification` and `error`. `action` is `written`, `kept_existing`,
`failed` or `dry_run`. `verification` holds six numbers and a verdict:
`expected_rows`, `rows`, `distinct_keys`, `expected_cu_s`, `cu_s`, `rel_diff_cu_s` and
`passed`. The window table entry also holds `window_count`, `peak_utilization_pct`
and `window_hours`, the number of distinct `window_hour` values extracted, which is
24 on a complete day.

## Parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| days_in_scope | 12 | How many whole days to load, counting back from yesterday in model time |
| capacity_id | 6ABDFB99-6499-4226-93E5-C4C3B5D0E924 | Bristow Insight, P1 |
| metric_workspace | 75af6cf1-9c91-4220-b258-ab1d1dedc0d4 | Fabric Capacity Metrics workspace |
| metric_dataset | e510b503-48b3-4414-ad4c-1e40f2be1d28 | Fabric Capacity Metrics model |
| write_mode | replace_days | Or dry_run, which runs every query and writes only the evidence file |
| table_prefix | capmetrics_ | Prefix of the five tables and of the evidence folder. Must match `^[a-z][a-z0-9_]*_$` |

The parameters live in the first cell, which is a real Fabric parameter cell. The
workspace toggled it on 2026-09-21 and Fabric wrote the marker
`# PARAMETERS CELL ********************` into `notebook-content.py` itself, so a
pipeline or a schedule can override any of the six values with base parameters.
Fabric owns that marker line. Editing it by hand turns the cell back into an
ordinary code cell and the overrides stop being applied.

Twelve days stays inside the source's retention of about 13 days, so the oldest
retained day, which the source trims continuously, is normally outside the window.

## How a run works

1. **Window.** The dates are yesterday in model time and the `days_in_scope - 1`
   days before it, oldest first. Model time is UTC minus 6 hours, see below.
2. **Probe.** The first query of the run is the 30 second window query for
   yesterday, the same query the loop would send for that date. It gets three
   attempts, 10 and then 20 seconds apart. If the source keeps answering with a
   transient failure, the run writes its run log row with status
   `source_unavailable`, writes the evidence file and fails, about a minute after it
   started plus Spark start-up; nothing else is queried. If the probe fails on
   anything else, the run ends the same way with status `failed`. If it succeeds,
   its result is used for yesterday's windows, so that date is not queried twice.
3. **Loop.** For each date, the item operation table and then the window table,
   each on its own: a failure on one is recorded and the run carries on. Every query
   after the probe gets three attempts, 20 and then 60 seconds apart, on the
   transient failures listed in the notebook's `RETRY_ON_TEXT`; anything else fails
   that table for that date at once.
4. **Trim guard.** Before writing a date, the run counts the rows the table already
   holds for that capacity and date. If the table holds more than this run
   extracted, the stored rows are kept, nothing is deleted, and the entry records
   `kept_existing` with both counts. This is what protects the oldest days: the
   source trims its oldest day continuously, and a later run would otherwise replace
   a complete day with a trimmed one. Measured on 2026-09-18, the oldest retained day
   had 1245 of its 2880 windows left. Otherwise the date is replaced: the Spark
   DataFrame is built first, so a value Spark refuses fails before anything is
   deleted, then the date is deleted and the new rows appended. Reruns are safe and
   leave no duplicates.
5. **Verification.** After each date's write the run reads the date back:
   `COUNT(*)`, the count of distinct grain keys (through a `SELECT DISTINCT`
   subquery, so a null in a key column still counts), and `SUM(cu_s)`. A written
   date is verified when the row count equals the rows written, every row has its
   own key, and the CU seconds total matches the extraction within a relative
   difference of 1e-9. A kept date is verified when every stored row has its own
   key. A date that does not verify is recorded as an error.
6. **Snapshots.** Items and Capacities are replaced whole.
7. **Close.** The run log row (not in a dry run), then the evidence file, then,
   unless the status is `success` and the records were written, the notebook raises
   RuntimeError so the
   job and the pipeline activity end Failed. `notebookutils.notebook.exit()` alone
   leaves the job Completed whatever value it is handed, which is why the notebook
   raises instead.

**How long a bad run takes.** Worst cases, counting only the waits between
attempts: a source that is down when the run starts costs 30 seconds (the probe's
10 and 20). A source that goes down after a good probe and never comes back costs 80
seconds for every remaining query: about 32 minutes for Daily (24 queries) and just
under 3 minutes for Smoke (2 queries). Both end `partial` and fail the job.

**Dry run.** Set `write_mode` to `dry_run`: every query runs, the counts, the top
five item operations and the peak window utilization are printed, and the only
thing written is the evidence file. No table and no run log row is touched.

**Backfill.** The window cannot usefully reach past the source's retention. Running
Daily by hand with `days_in_scope` 13 reaches the oldest retained day, which is
partial; the trim guard keeps any fuller copy already stored. Dates older than that
come back empty and change nothing that is stored.

## The pipelines

| Pipeline | days_in_scope | write_mode | table_prefix | Retry | Timeout per attempt |
| --- | --- | --- | --- | --- | --- |
| Capacity Metrics Smoke | 1 | replace_days | capmetrics_smoke_ | none | 30 minutes |
| Capacity Metrics Daily | 12 | replace_days | capmetrics_ | 3, 30 minutes apart | 2 hours |

Each is one Fabric notebook activity running Capacity Metrics Extract. The values in
the table are pipeline parameter defaults, passed to the notebook's parameters of the
same names as expressions (`@pipeline().parameters.days_in_scope` and so on), so a
run started with no parameters uses them and a run started by hand from the pipeline
can override them. In the Git definition the activity names the notebook by its
logical id (the `logicalId` in `Capacity Metrics Extract.Notebook/.platform`) with
the empty workspace id, which is how Fabric records a reference to an item in the
same workspace.

The Daily retry answers the source's outages: an attempt that finds the source down
fails within minutes, and the next attempt starts 30 minutes later, so three retries
cover about an hour and a half of outage.

**Schedule.** Daily carries its schedule in the Git definition
(`Capacity Metrics Daily.DataPipeline/.schedules`): enabled, every day at 06:00 and
18:00 in the `Central Standard Time` zone, which is US Central wall clock time and
follows daylight saving, from 2026-09-24 06:00 to 2099-01-01. Microsoft documents
that a schedule whose start time is already in the past triggers a job at once. So
an Update from git that brings this schedule into the workspace after 2026-09-24
06:00 Central fires a Daily run immediately, and the coordinator sequences the
Update from git before that time. Microsoft's REST reference describes a schedule's
start time as UTC, while its own examples and its Git sample give a time with no
zone beside `localTimeZoneId`, as this file does; read the conservative way, the
start is 2026-09-24 06:00 UTC, which is 01:00 Central. Smoke has no schedule.

**Who the notebook runs as.** Microsoft documents that a notebook run as a pipeline
activity runs under the identity of the user who last modified the pipeline, and
that a schedule's owner is the user who created or last modified it. After an
Update from git both are whoever ran it. That identity needs Build permission on the
Fabric Capacity Metrics semantic model. Microsoft also documents that a schedule expires if its owner does not sign in
to Fabric for 90 consecutive days, and that a scheduler is disabled after repeated
consecutive failures (typically 10).

## How the coordinator verifies a run

1. Start the pipeline (Smoke or Daily) and wait for it to finish. A Failed
   pipeline means the status was not `success`, or the run log row or the evidence
   file could not be written; the evidence file, when there is one, says which.
2. Download `Files/capmetrics_smoke/runs/latest.json` for Smoke, or
   `Files/capmetrics/runs/latest.json` for Daily, from the IT_Metrics Lakehouse.
3. Check that it belongs to this run: `started_at_utc` is after the pipeline was
   started. A stale file means the run failed before it could write one; then
   `Files/<prefix>/runs/` and the run log are the next places to look.
4. Check `notebook_version` is `3.1.0`, `parameters.write_mode` is
   `replace_days` (a dry run writes to the same `latest.json`), `status` is
   `success`, `run_log_row_written` is true, the probe `outcome` is `ok`, and every
   date and table entry is `written` or `kept_existing` with `verification.passed`
   true. For a complete day expect 2880 windows and 24 window hours.
5. List the Lakehouse tables to confirm the five tables for the prefix exist.

## What has been run and what has not

Version 3.1.0 has been run only locally, offline, by the lane that built it: every
cell executed in order in a harness that replays query results captured live from
the source model on 2026-09-21, with Spark and notebookutils replaced by in-memory
stand-ins and PySpark's own row type verifier applied to every row. That covered the
probe, the trim guard, the read-back verification, the evidence file, dry runs, the
retry timings, both table prefixes and the item label. The lane made no Fabric
run of any version 3 notebook and ran neither pipeline. The first Fabric run
proves what the harness cannot: the Delta DELETE and read-back SQL, the OneLake
evidence write, and that Fabric accepts the two pipeline definitions and the
schedule.

## Measured facts

All measured on 2026-09-18 against the live model. Worth rechecking if behavior looks
wrong later.

**Retention is about 13 days, not 14, and the oldest day is always partial.** On
2026-09-18 the item operation fact table started at 2026-09-05 and the 30 second
detail started at 2026-09-05 13:50. The 2026-09-05 total had already fallen from
788,739.0594 to 774,903.7906 CU seconds between two reads a few hours apart, because
the oldest day is being trimmed continuously. Do not expect to backfill further than
about 13 days, and expect the oldest day in any backfill to be incomplete.

**The model's timepoints are at a fixed offset of UTC minus 6 hours.** They are not
US Central. Central is UTC minus 5 for most of the year under daylight saving, so for
that part of the year these timestamps sit one hour behind Central wall clock time.
Measured: at 19:50:43 UTC the latest window start was 13:46:30, a gap of 6 hours and
4 minutes, of which about 4 minutes is the source app's own refresh lag. This is why
`window_start_utc` exists and why the notebook computes "yesterday" from UTC shifted
by 6 hours rather than from the Spark clock, which runs in UTC.

**Query one day at a time.** A 30 day span over these tables times out at 225 seconds.
One day returns in about 8 seconds per query.

**Every DAX query must start with the MPARAMETER clause.** Without
`DEFINE MPARAMETER 'CapacitiesList' = { "<capacity id>" }` the model answers
"Error obtaining data location". Filter context alone is not enough.

**Days can be short.** A complete day is 2880 windows. Two of the ten days checked
came back with 2878 and 2879, and the same counts appear in data captured earlier, so
the gaps are in the source rather than in this extraction. The notebook prints a note
and carries on.

**The two fact tables will not tie to each other.** Item attributed CU seconds and
capacity CU seconds measure different things and the daily totals differ in both
directions. On 2026-09-10 the item operation total was 4,378,598 against 4,508,652 in
the 30 second windows. On 2026-09-15 it was 3,857,739 against 2,951,293. Use the 30
second windows for capacity utilization and the item operation table for attribution.

## Measured 2026-09-21

**The source model's fact tables go away for minutes at a time.** Between about
16:45 and 17:00 UTC every query against `CU Detail` and
`Metrics By Item Operation And Day` answered `Internal Error: Error obtaining data
location. . The exception was raised by the IDbCommand interface.`
(AnalysisServicesErrorCode 3239182364), and then the identical queries succeeded
again with nothing changed. It was reproduced in that window through Semantic Link
inside Fabric and through the Power BI REST executeQueries endpoint from outside, so
the source is what fails rather than the way it is queried. Throughout the outage the
model's Import tables (Items, Capacities, Timepoints, Dates) answered normally, and
its scheduled refresh had completed that morning at 05:05 UTC, so neither staleness
nor permissions explain it. The notebook's answer to this has changed twice: a short
per-query retry after this date, a longer one on 2026-09-23, and since version 3 a
probe at the start of the run with the retry left to the pipeline (see "How a run
works").

**Semantic Link returns pandas nullable columns, and a blank measure is `pd.NA`.**
Its result columns are typed `string`, `Int64` and `Float64`, whose missing value is
`pd.NA`. Comparing `pd.NA` with anything returns `pd.NA` rather than a boolean, so a
test like `if v == ""` on a blank measure raises
`TypeError: boolean value of NA is ambiguous`, and `str(pd.NA)` produces the literal
text `<NA>`. The value converters check for missing values with `pandas.isna` before
anything else, so a blank measure lands as a real null instead of raising or landing
as text.

**A helper named for an IPython output variable is a string by the time a later
cell calls it.** The notebook kernel is IPython, which rebinds `_i`, `_ii` and
`_iii` to the source text of the last three cells after every cell, and `_`, `__`
and `___` to their results. The integer converter was called `_i`, so by the time
the daily loop ran it was the text of a previous cell: every one of the 13 daily
dates failed with `TypeError: 'str' object is not callable` while the snapshot
path, which never calls that converter, succeeded in the same run (Fabric job
34b674cb). It is `_int` now. Nothing defined in this notebook may take one of the
names `_`, `__`, `___`, `_i`, `_ii`, `_iii`, `_ih`, `_oh`, `_dh`, `In`, `Out`,
`exit`, `quit` or `get_ipython`, and the lane harness fails the build if one does.

## Measured 2026-09-23

**The outage comes back, and lasted about seven minutes this time.** In an
interactive run from 14:34 to 14:41 UTC every query the notebook sent through
Semantic Link answered `Error obtaining data location`, and then the same queries,
unchanged, through the same Semantic Link path, returned rows that matched the
reference figures exactly. So the transport is sound and the outage is on the source
side. The retry of the time waited 80 seconds in all, far short of seven minutes, and
was lengthened that day to five attempts over 7.5 minutes per query. Two Fabric runs
of the notebook still ended without the window table and with a partial item
operation table, which is why version 3 stops waiting inside a query: it probes
first, fails fast when the source is down, and lets the Daily pipeline try again 30
minutes later.

**Bare status codes in the retry list matched this project's own ids.** The source
dataset id `e510b503-48b3-4414-ad4c-1e40f2be1d28` contains `503`, so a retry list
holding a bare `"503"` retried `Dataset ... not found` for this very dataset, and a
bare `"connection"` retried a permission error that mentions a connection. The list
since version 3 holds phrases only (`service unavailable`, `status code 503`,
`connection reset` and so on); Semantic Link words an HTTP failure
`<status> <reason> for url: ...`, so the reason phrases are what match it.

**Spark takes only the exact Python types for dates and timestamps.**
`spark.createDataFrame` checks each value's exact type against its field rather
than asking whether it is an instance, so a pandas `Timestamp`, a subclass of
`datetime` and what Semantic Link returns for a DAX datetime, is refused for a
`TimestampType` field. Once the queries recovered, the item operation write
succeeded and the 30 second window write failed on every date with
`TypeError: field window_start: TimestampType() can not accept object
Timestamp('2026-09-15 00:00:00') in type <class
'pandas._libs.tslibs.timestamps.Timestamp'>`. The item operation date had
survived only because its converter happened to call `.date()`. Both converters
now return exactly `datetime.date` and `datetime.datetime`: a pandas `Timestamp`
goes through `to_pydatetime()`, and any datetime is rebuilt from its parts, which
drops any subclass and any time zone (the model's times are naive, so nothing is
converted). Every other date or timestamp column (`loaded_at_utc`,
`started_at_utc`, `finished_at_utc`, `snapshot_date`, `window_date`,
`window_start_utc`, `hour_start`) is derived from a plain value. The rule to keep:
a value bound for a Spark field must be exactly `str`, `float`, `int`,
`datetime.date` or `datetime.datetime`, or None, never a pandas or numpy type that
merely behaves like one. The notebook of the time deleted a date before it built
the DataFrame, so the same failure against a table that already held the date would
have deleted the stored rows and written nothing. It did not happen that day only
because the window table did not exist yet. Since version 3 the DataFrame is built
before anything is deleted.

## Known limits

Zero CU rows are dropped from the item operation table. This follows the pattern in
Microsoft's own FUAM notebook. The cost is small but real: on the day measured, 4 rows
per day carried operation counts with no CU consumption, mostly Eventstream uptime
rows. If those matter later, widen the filter from `cu_s > 0` to also keep rows where
`operations > 0`.

The trim guard compares row counts only. A date that the source legitimately
restates with fewer rows keeps the larger stored version, and a date restated with
the same number of rows but different values is replaced. Neither has been seen.

The delete and the append for a date are two Delta commits, not one. The DataFrame
is built and type checked before the delete, which closes the type failure seen on
2026-09-23, but a failure of the append itself after the delete would leave that
date empty until the next run reloads it, or lose it if the source has trimmed it
by then.

Only one capacity is loaded per run. Storage metrics, autoscale, System Events and
Item History are not extracted.

The notebook reads the source model through Semantic Link
(`sempy.fabric.evaluate_dax`), which reaches the model over XMLA under the identity
that runs the notebook. That identity needs Build permission on the Fabric Capacity
Metrics semantic model. See "Who the notebook runs as" for which identity that is
under each pipeline.

The Smoke tables and `Files/capmetrics_smoke/` are not cleaned up by anything.
