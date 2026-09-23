# Capacity Metrics Extract

A Fabric notebook that copies Microsoft Fabric Capacity Metrics into the IT_Metrics
Lakehouse one day at a time, so capacity history survives the short retention window
of the source model.

## Why this exists

The Fabric Capacity Metrics semantic model keeps only about 13 to 14 days of
item level and 30 second detail. Utilization, top consumers and headroom cannot be
trended month over month from the source alone. This notebook lands the same numbers
in Delta tables that keep growing.

## What it writes

Five Delta tables in the IT_Metrics Lakehouse. The Lakehouse has no schemas enabled,
so the names carry a `capmetrics_` prefix to stay clear of the existing tables.

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
| loaded_at_utc | timestamp | |
| run_id | string | |

### capmetrics_items and capmetrics_capacities

Snapshots of the two Import tables in the source model, replaced whole on every run
and stamped with `snapshot_date`. Items carries item_id, workspace_id, workspace_name,
item_name, item_kind, billable_type and capacity_id. Capacities carries capacity_id,
capacity_name, sku, state, region and owners. Item_id was unique across all 520 rows
when this was built, so it is safe as a key.

### capmetrics_run_log

One appended row per run: run_id, started_at_utc, finished_at_utc, days_in_scope,
write_mode, dates_processed, a row count per table, status and error_text. Status is
success, partial or failed. `error_text` carries the full Python traceback of every
failure, oldest first, truncated at 4000 characters, because a failure inside a
library cannot be diagnosed from an exception type and message alone and the Spark
session that held the log output is gone by the time anyone looks.

A run whose status is not success writes its run log row and then raises, so the
Fabric job ends Failed. This matters: `notebookutils.notebook.exit()` leaves the job
status Completed whatever value it is handed, so before this the scheduler and the
job history showed a green run for an extraction that had written nothing. Read the
outcome from `capmetrics_run_log`, not from job history.

## Parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| days_in_scope | 3 | How many whole days to load, counting back from yesterday |
| capacity_id | 6ABDFB99-6499-4226-93E5-C4C3B5D0E924 | Bristow Insight, P1 |
| metric_workspace | 75af6cf1-9c91-4220-b258-ab1d1dedc0d4 | Fabric Capacity Metrics workspace |
| metric_dataset | e510b503-48b3-4414-ad4c-1e40f2be1d28 | Fabric Capacity Metrics model |
| write_mode | replace_days | Or dry_run, which queries and prints but writes nothing |

The parameters live in the first cell, which is a real Fabric parameter cell. The
workspace toggled it on 2026-09-21 and Fabric wrote the marker
`# PARAMETERS CELL ********************` into `notebook-content.py` itself, so a
pipeline or a schedule can override any of the five values with base parameters.
Fabric owns that marker line. Editing it by hand turns the cell back into an
ordinary code cell and the overrides stop being applied.

## How to run it

Normal daily run: leave the defaults and run. It loads the last three whole days,
replacing each one, and refreshes both snapshots.

Backfill: raise `days_in_scope`. Each day costs about 16 seconds of query time, so a
full backfill to the edge of retention takes a few minutes. Reruns are safe. Each date
is deleted for this capacity and appended again, so running the same day twice leaves
the same rows.

Check before writing: set `write_mode` to `dry_run`. Every query runs and the row
counts, the top five item operations and the peak window utilization are printed, and
nothing is written.

## How to schedule it

Not scheduled by this notebook. Set a schedule on the notebook item in the workspace.
Daily at 06:00 US Central is the proposal, which is comfortably after the source model
has the previous day complete.

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
nor permissions explain it. The notebook now retries a failing query three times, 20
seconds then 60 seconds apart, and prints each retry. An outage longer than that
still fails the run, which is the honest outcome. The retry was lengthened on
2026-09-23, see below.

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

**The outage comes back, lasted about seven minutes this time, and the retry now
outlasts it.** In an interactive run from 14:34 to 14:41 UTC every query the
notebook sent through Semantic Link answered `Error obtaining data location`, and
then the same queries, unchanged, through the same Semantic Link path, returned rows
that matched the reference figures exactly. So the transport is sound and the outage
is on the source side. The earlier retry waited 80 seconds in all, far short of
seven minutes. It now makes five attempts with 30, 60, 120 and 240 seconds between
them, up to 450 seconds (7.5 minutes) of waiting per query, and prints each retry. A
query that is still failing after the fifth attempt is recorded as an error for its
date and fails the run. The waiting is per query, and a query that gives up ends
its date, so with the default three days an outage that never recovers costs at
most four such waits (one per date and one for the snapshots), 30 minutes, before
the run ends Failed.

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
`window_start_utc`) is derived from a plain value. The rule to keep: a value bound
for a Spark field must be exactly `str`, `float`, `int`, `datetime.date` or
`datetime.datetime`, or None, never a pandas or numpy type that merely behaves like
one.

## Known limits

Zero CU rows are dropped from the item operation table. This follows the pattern in
Microsoft's own FUAM notebook. The cost is small but real: on the day measured, 4 rows
per day carried operation counts with no CU consumption, mostly Eventstream uptime
rows. If those matter later, widen the filter from `cu_s > 0` to also keep rows where
`operations > 0`.

Only one capacity is loaded per run. Storage metrics, autoscale, System Events and
Item History are not extracted.

The notebook reads the source model through Semantic Link
(`sempy.fabric.evaluate_dax`), which reaches the model over XMLA under the identity
that runs the notebook. That identity needs Build permission on the Fabric Capacity
Metrics semantic model. A scheduled run uses the identity of whoever owns the
schedule, so handing the notebook to someone else means checking that permission
again.
