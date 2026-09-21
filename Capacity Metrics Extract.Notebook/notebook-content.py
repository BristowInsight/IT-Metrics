# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "861c9438-1e8d-4123-8287-35e2d4692cff",
# META       "default_lakehouse_name": "IT_Metrics",
# META       "default_lakehouse_workspace_id": "a3f3d6a5-db42-471c-9455-e7fb68ef38b2",
# META       "known_lakehouses": [
# META         {
# META           "id": "861c9438-1e8d-4123-8287-35e2d4692cff"
# META         }
# META       ]
# META     }
# META   }
# META }

# PARAMETERS CELL ********************

# Parameters for the Capacity Metrics Extract notebook.
#
# This is the first code cell so a pipeline or a person can override these values.
# It is a plain code cell, not a toggled Fabric parameter cell, because the exact
# marker for a parameter cell in the Git .py source format is not documented by
# Microsoft and this lane does not guess file formats. To turn it into a real
# parameter cell, open the notebook in the workspace after the first Git sync,
# open the cell menu, and choose "Toggle parameter cell". See README.md.

# How many whole days to load, counting back from yesterday in model time.
days_in_scope = 3

# Bristow Insight capacity. Uppercase, as the model stores it.
capacity_id = "6ABDFB99-6499-4226-93E5-C4C3B5D0E924"

# Source: the Microsoft Fabric Capacity Metrics workspace and semantic model.
metric_workspace = "75af6cf1-9c91-4220-b258-ab1d1dedc0d4"
metric_dataset = "e510b503-48b3-4414-ad4c-1e40f2be1d28"

# "replace_days" deletes each in-scope date for this capacity then appends.
# "dry_run" runs every query and prints counts without writing anything.
write_mode = "replace_days"

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

import re
import uuid
from datetime import datetime, timedelta, timezone, date

import sempy.fabric as fabric
from pyspark.sql.types import (
    StructType,
    StructField,
    StringType,
    DoubleType,
    LongType,
    DateType,
    TimestampType,
)

# The Capacity Metrics model stores its timepoints at a fixed offset from UTC.
# Measured 2026-09-18: UTC 19:50:43 against a latest window start of 13:46:30,
# a gap of 6 hours plus the app's own few minutes of refresh lag. This is a fixed
# offset, not US Central, so it does not follow daylight saving. See README.md.
MODEL_UTC_OFFSET_HOURS = -6

# Seconds in one capacity metrics window.
WINDOW_SECONDS = 30

# Fallback only. The real number is read from CU Detail[Base capacity units].
SKU_CU = {
    "P1": 64, "P2": 128, "P3": 256, "P4": 512, "P5": 1024,
    "A1": 1, "A2": 2, "A3": 4, "A4": 8, "A5": 16, "A6": 32, "A7": 64, "A8": 128,
    "F2": 2, "F4": 4, "F8": 8, "F16": 16, "F32": 32, "F64": 64,
    "F128": 128, "F256": 256, "F512": 512, "F1024": 1024, "F2048": 2048,
}

TBL_OPS = "capmetrics_item_operation_day"
TBL_CUD = "capmetrics_cu_window_30s"
TBL_ITEMS = "capmetrics_items"
TBL_CAPS = "capmetrics_capacities"
TBL_LOG = "capmetrics_run_log"

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Value converters. Every DataFrame is built from explicit Python tuples rather
# than handed straight from pandas, so a day that comes back empty or with a null
# column cannot change a Delta column's type between runs.

def _s(v):
    if v is None:
        return None
    t = str(v).strip()
    return t if t else None


def _f(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _i(v):
    f = _f(v)
    return None if f is None else int(f)


def _d(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.fromisoformat(str(v)[:19]).date()


def _ts(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v)[:19])


def model_today():
    """Today's date as the Capacity Metrics model sees it.

    Fabric Spark clocks run in UTC. Without this shift a 06:00 US Central run
    would ask for the wrong day for part of the year.
    """
    return (datetime.now(timezone.utc) + timedelta(hours=MODEL_UTC_OFFSET_HOURS)).date()


def dates_in_scope(n_days):
    """The last n_days whole days, oldest first, ending yesterday in model time."""
    end = model_today() - timedelta(days=1)
    return [end - timedelta(days=i) for i in range(n_days - 1, -1, -1)]


def run_dax(dax_string):
    """Run one DAX query against the Capacity Metrics model."""
    return fabric.evaluate_dax(
        workspace=metric_workspace, dataset=metric_dataset, dax_string=dax_string
    )

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# DAX builders. Every query starts with the MPARAMETER clause. Without it the
# model answers "Error obtaining data location", because filter context alone
# does not tell the Capacity Metrics model which capacity's data to read.
#
# One day per query. A 30 day span over these tables times out at 225 seconds.
# Measured 2026-09-18: one day of item operations took 8 seconds for 1633 rows,
# one day of CU detail took 8 seconds for 2880 rows.


def dax_item_operation_day(cap_id, day):
    return f"""
    DEFINE
        MPARAMETER 'CapacitiesList' = {{ "{cap_id}" }}
        VAR __Day =
            FILTER(
                KEEPFILTERS(VALUES('Metrics By Item Operation And Day'[Date])),
                'Metrics By Item Operation And Day'[Date] = DATE({day.year}, {day.month}, {day.day})
            )
        VAR __Core =
            SUMMARIZECOLUMNS(
                Capacities[Capacity Id],
                Items[Workspace Id],
                'Metrics By Item Operation And Day'[Date],
                'Metrics By Item Operation And Day'[Item Id],
                Items[Item kind],
                'Metrics By Item Operation And Day'[Operation name],
                FILTER(Capacities, Capacities[Capacity Id] = "{cap_id}"),
                __Day,
                "duration_s", SUM('Metrics By Item Operation And Day'[Duration (s)]),
                "cu_s", SUM('Metrics By Item Operation And Day'[CU (s)]),
                "throttling_min", SUM('Metrics By Item Operation And Day'[Throttling (min)]),
                "users", SUM('Metrics By Item Operation And Day'[Users]),
                "operations", SUM('Metrics By Item Operation And Day'[Operations]),
                "successful", SUM('Metrics By Item Operation And Day'[Successful operations]),
                "rejected", SUM('Metrics By Item Operation And Day'[Rejected operations]),
                "invalid", SUM('Metrics By Item Operation And Day'[Invalid operations]),
                "failed", SUM('Metrics By Item Operation And Day'[Failed operations]),
                "cancelled", SUM('Metrics By Item Operation And Day'[Cancelled operations])
            )
    EVALUATE FILTER(__Core, [cu_s] > 0) ORDER BY [cu_s] DESC
    """


def dax_cu_window_30s(cap_id, day):
    return f"""
    DEFINE
        MPARAMETER 'CapacitiesList' = {{ "{cap_id}" }}
        VAR __Cap = TREATAS({{"{cap_id}"}}, 'Capacities'[Capacity Id])
        VAR __Day = TREATAS({{DATE({day.year}, {day.month}, {day.day})}}, 'Timepoints'[Date])
    EVALUATE
    SUMMARIZECOLUMNS(
        'Timepoints'[Timepoint], __Cap, __Day,
        "cu_s", SUM('CU Detail'[CU (s)]),
        "background_cu", SUM('CU Detail'[Background]),
        "interactive_cu", SUM('CU Detail'[Interactive]),
        "background_nonbillable_cu", SUM('CU Detail'[Background non billable]),
        "interactive_nonbillable_cu", SUM('CU Detail'[Interactive non billable]),
        "cu_limit_norm", MAX('CU Detail'[CU limit]),
        "interactive_delay_pct", MAX('CU Detail'[Interactive delay %]),
        "interactive_rejection_pct", MAX('CU Detail'[Interactive rejection %]),
        "background_rejection_pct", MAX('CU Detail'[Background rejection %]),
        "sku", MAX('CU Detail'[SKU]),
        "base_capacity_units", MAX('CU Detail'[Base capacity units])
    )
    ORDER BY 'Timepoints'[Timepoint]
    """


def dax_items(cap_id):
    return f"""
    DEFINE
        MPARAMETER 'CapacitiesList' = {{ "{cap_id}" }}
    EVALUATE
    SELECTCOLUMNS(
        FILTER(Items, Items[Capacity Id] = "{cap_id}"),
        "item_id", Items[Item Id],
        "workspace_id", Items[Workspace Id],
        "workspace_name", Items[Workspace name],
        "item_name", Items[Item name],
        "item_kind", Items[Item kind],
        "billable_type", Items[Billable type],
        "capacity_id", Items[Capacity Id]
    )
    """


def dax_capacities(cap_id):
    return f"""
    DEFINE
        MPARAMETER 'CapacitiesList' = {{ "{cap_id}" }}
    EVALUATE
    SELECTCOLUMNS(
        FILTER(Capacities, Capacities[Capacity Id] = "{cap_id}"),
        "capacity_id", Capacities[Capacity Id],
        "capacity_name", Capacities[Capacity name],
        "sku", Capacities[SKU],
        "state", Capacities[State],
        "region", Capacities[Region],
        "owners", Capacities[Owners]
    )
    """

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Explicit Delta schemas. Column names and types are fixed here so they cannot
# drift between runs. Adding a column later means adding it here and in README.md.

SCHEMA_OPS = StructType([
    StructField("capacity_id", StringType(), True),
    StructField("workspace_id", StringType(), True),
    StructField("item_id", StringType(), True),
    StructField("item_kind", StringType(), True),
    StructField("operation_name", StringType(), True),
    StructField("date", DateType(), True),
    StructField("duration_s", DoubleType(), True),
    StructField("cu_s", DoubleType(), True),
    StructField("throttling_min", DoubleType(), True),
    StructField("users", LongType(), True),
    StructField("operations", LongType(), True),
    StructField("successful", LongType(), True),
    StructField("rejected", LongType(), True),
    StructField("invalid", LongType(), True),
    StructField("failed", LongType(), True),
    StructField("cancelled", LongType(), True),
    StructField("loaded_at_utc", TimestampType(), True),
    StructField("run_id", StringType(), True),
])

SCHEMA_CUD = StructType([
    StructField("capacity_id", StringType(), True),
    StructField("window_date", DateType(), True),
    StructField("window_start", TimestampType(), True),
    StructField("window_start_utc", TimestampType(), True),
    StructField("cu_s", DoubleType(), True),
    StructField("background_cu", DoubleType(), True),
    StructField("interactive_cu", DoubleType(), True),
    StructField("background_nonbillable_cu", DoubleType(), True),
    StructField("interactive_nonbillable_cu", DoubleType(), True),
    StructField("cu_limit_norm", DoubleType(), True),
    StructField("interactive_delay_pct", DoubleType(), True),
    StructField("interactive_rejection_pct", DoubleType(), True),
    StructField("background_rejection_pct", DoubleType(), True),
    StructField("sku", StringType(), True),
    StructField("sku_cu", LongType(), True),
    StructField("budget_cu_s", DoubleType(), True),
    StructField("utilization_pct", DoubleType(), True),
    StructField("loaded_at_utc", TimestampType(), True),
    StructField("run_id", StringType(), True),
])

SCHEMA_ITEMS = StructType([
    StructField("item_id", StringType(), True),
    StructField("workspace_id", StringType(), True),
    StructField("workspace_name", StringType(), True),
    StructField("item_name", StringType(), True),
    StructField("item_kind", StringType(), True),
    StructField("billable_type", StringType(), True),
    StructField("capacity_id", StringType(), True),
    StructField("snapshot_date", DateType(), True),
    StructField("loaded_at_utc", TimestampType(), True),
    StructField("run_id", StringType(), True),
])

SCHEMA_CAPS = StructType([
    StructField("capacity_id", StringType(), True),
    StructField("capacity_name", StringType(), True),
    StructField("sku", StringType(), True),
    StructField("state", StringType(), True),
    StructField("region", StringType(), True),
    StructField("owners", StringType(), True),
    StructField("snapshot_date", DateType(), True),
    StructField("loaded_at_utc", TimestampType(), True),
    StructField("run_id", StringType(), True),
])

SCHEMA_LOG = StructType([
    StructField("run_id", StringType(), True),
    StructField("started_at_utc", TimestampType(), True),
    StructField("finished_at_utc", TimestampType(), True),
    StructField("days_in_scope", LongType(), True),
    StructField("write_mode", StringType(), True),
    StructField("dates_processed", StringType(), True),
    StructField("rows_item_operation_day", LongType(), True),
    StructField("rows_cu_window_30s", LongType(), True),
    StructField("rows_items", LongType(), True),
    StructField("rows_capacities", LongType(), True),
    StructField("status", StringType(), True),
    StructField("error_text", StringType(), True),
])

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Row builders. Each takes the pandas frame returned by Semantic Link and emits
# tuples in the exact order of the matching schema. Columns are read by position,
# not by name, because Semantic Link returns bracketed names such as
# "Capacities[Capacity Id]" that vary with the model version.


def rows_item_operation_day(df, loaded_at, run_id):
    out = []
    for r in df.itertuples(index=False, name=None):
        out.append((
            _s(r[0]),                       # capacity_id
            _s(r[1]),                       # workspace_id
            _s(r[3]),                       # item_id
            _s(r[4]),                       # item_kind
            _s(r[5]),                       # operation_name
            _d(r[2]),                       # date
            _f(r[6]), _f(r[7]), _f(r[8]),   # duration_s, cu_s, throttling_min
            _i(r[9]), _i(r[10]), _i(r[11]),  # users, operations, successful
            _i(r[12]), _i(r[13]), _i(r[14]), _i(r[15]),  # rejected, invalid, failed, cancelled
            loaded_at,
            run_id,
        ))
    return out


def rows_cu_window_30s(df, cap_id, loaded_at, run_id):
    out = []
    for r in df.itertuples(index=False, name=None):
        window_start = _ts(r[0])
        if window_start is None:
            continue
        cu_s = _f(r[1])
        sku = _s(r[10])
        sku_cu = _i(r[11])
        if sku_cu is None and sku is not None:
            sku_cu = SKU_CU.get(sku)
        budget = float(sku_cu * WINDOW_SECONDS) if sku_cu else None
        utilization = (cu_s / budget) if (budget and cu_s is not None) else None
        out.append((
            cap_id,
            window_start.date(),
            window_start,
            window_start - timedelta(hours=MODEL_UTC_OFFSET_HOURS),
            cu_s,
            _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5]),
            _f(r[6]), _f(r[7]), _f(r[8]), _f(r[9]),
            sku,
            sku_cu,
            budget,
            utilization,
            loaded_at,
            run_id,
        ))
    return out


def rows_items(df, snapshot_day, loaded_at, run_id):
    return [(
        _s(r[0]), _s(r[1]), _s(r[2]), _s(r[3]), _s(r[4]), _s(r[5]), _s(r[6]),
        snapshot_day, loaded_at, run_id,
    ) for r in df.itertuples(index=False, name=None)]


def rows_capacities(df, snapshot_day, loaded_at, run_id):
    return [(
        _s(r[0]), _s(r[1]), _s(r[2]), _s(r[3]), _s(r[4]), _s(r[5]),
        snapshot_day, loaded_at, run_id,
    ) for r in df.itertuples(index=False, name=None)]


# Write helpers.

def table_exists(name):
    try:
        return spark.catalog.tableExists(name)
    except Exception:
        return False


def write_rows(name, rows, schema, mode="append", overwrite_schema=False):
    writer = spark.createDataFrame(rows, schema=schema).write.format("delta").mode(mode)
    if overwrite_schema:
        writer = writer.option("overwriteSchema", "true")
    writer.saveAsTable(name)
    return len(rows)


def replace_day(name, rows, schema, date_col, day, cap_id):
    """Delete one date for one capacity, then append. Safe to rerun."""
    if table_exists(name):
        spark.sql(
            f"DELETE FROM {name} "
            f"WHERE capacity_id = '{cap_id}' AND {date_col} = DATE '{day.isoformat()}'"
        )
    return write_rows(name, rows, schema, mode="append")


def replace_all(name, rows, schema):
    """Replace the whole table. Used for the two snapshot tables."""
    return write_rows(name, rows, schema, mode="overwrite", overwrite_schema=True)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Validate the parameters and open the run.

GUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")

if write_mode not in ("replace_days", "dry_run"):
    raise ValueError(f"write_mode must be 'replace_days' or 'dry_run', got {write_mode!r}")

days_in_scope = int(days_in_scope)
if days_in_scope < 1:
    raise ValueError(f"days_in_scope must be 1 or more, got {days_in_scope}")

capacity_id = str(capacity_id).strip().upper()
for name, value in (("capacity_id", capacity_id),
                    ("metric_workspace", metric_workspace),
                    ("metric_dataset", metric_dataset)):
    if not GUID_RE.match(str(value).strip()):
        raise ValueError(f"{name} must be a GUID, got {value!r}")

run_id = str(uuid.uuid4())
started_at = datetime.now(timezone.utc).replace(tzinfo=None)
target_dates = dates_in_scope(days_in_scope)
snapshot_day = model_today()

print(f"run_id            : {run_id}")
print(f"started_at_utc    : {started_at.isoformat()}")
print(f"write_mode        : {write_mode}")
print(f"capacity_id       : {capacity_id}")
print(f"model today       : {snapshot_day.isoformat()} (UTC {MODEL_UTC_OFFSET_HOURS:+d} hours)")
print(f"days_in_scope     : {days_in_scope}")
print(f"dates to process  : {', '.join(d.isoformat() for d in target_dates)}")

counts = {TBL_OPS: 0, TBL_CUD: 0, TBL_ITEMS: 0, TBL_CAPS: 0}
errors = []

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# One day at a time. A failure on one date is recorded and the loop carries on,
# so a single bad day cannot cost the whole run.

for day in target_dates:
    loaded_at = datetime.now(timezone.utc).replace(tzinfo=None)
    print(f"\n--- {day.isoformat()} ---")
    try:
        ops_df = run_dax(dax_item_operation_day(capacity_id, day))
        ops_rows = rows_item_operation_day(ops_df, loaded_at, run_id)

        cud_df = run_dax(dax_cu_window_30s(capacity_id, day))
        cud_rows = rows_cu_window_30s(cud_df, capacity_id, loaded_at, run_id)

        day_cu = sum(r[7] or 0.0 for r in ops_rows)
        window_cu = sum(r[4] or 0.0 for r in cud_rows)
        print(f"item operation rows : {len(ops_rows)}   cu_s {day_cu:,.4f}")
        print(f"30 second windows   : {len(cud_rows)}   cu_s {window_cu:,.4f}")
        if len(cud_rows) not in (0, 2880):
            print(f"NOTE: {len(cud_rows)} windows, a complete day is 2880. "
                  f"Expected for today, for the oldest retained day, or after an outage.")

        if write_mode == "dry_run":
            top = sorted(ops_rows, key=lambda r: r[7] or 0.0, reverse=True)[:5]
            print("top 5 item operations by cu_s (item_id, operation, kind, cu_s):")
            for r in top:
                print(f"  {r[2]}  {r[4]:<48.48}  {r[3]:<16.16}  {r[7]:,.4f}")
            peak = max((r[16] for r in cud_rows if r[16] is not None), default=None)
            if peak is not None:
                print(f"peak window utilization: {peak:.4%}")
            print("dry_run: nothing written")
        else:
            counts[TBL_OPS] += replace_day(
                TBL_OPS, ops_rows, SCHEMA_OPS, "date", day, capacity_id)
            counts[TBL_CUD] += replace_day(
                TBL_CUD, cud_rows, SCHEMA_CUD, "window_date", day, capacity_id)
            print(f"written to {TBL_OPS} and {TBL_CUD}")

    except Exception as ex:
        message = f"{day.isoformat()}: {type(ex).__name__}: {ex}"
        errors.append(message)
        print(f"ERROR {message}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Snapshots. Items and Capacities are Import tables in the source model and only
# move when that model refreshes, so the whole table is replaced each run and
# stamped with the date it was read.

loaded_at = datetime.now(timezone.utc).replace(tzinfo=None)
print(f"\n--- snapshots {snapshot_day.isoformat()} ---")
try:
    items_rows = rows_items(run_dax(dax_items(capacity_id)), snapshot_day, loaded_at, run_id)
    caps_rows = rows_capacities(run_dax(dax_capacities(capacity_id)), snapshot_day, loaded_at, run_id)

    distinct_items = len({r[0] for r in items_rows})
    print(f"items      : {len(items_rows)} rows, {distinct_items} distinct item_id")
    if distinct_items != len(items_rows):
        print("NOTE: item_id is not unique in this snapshot, which was not true on 2026-09-18.")
    print(f"capacities : {len(caps_rows)} rows")

    if write_mode == "dry_run":
        print("dry_run: nothing written")
    else:
        counts[TBL_ITEMS] = replace_all(TBL_ITEMS, items_rows, SCHEMA_ITEMS)
        counts[TBL_CAPS] = replace_all(TBL_CAPS, caps_rows, SCHEMA_CAPS)
        print(f"written to {TBL_ITEMS} and {TBL_CAPS}")

except Exception as ex:
    message = f"snapshots: {type(ex).__name__}: {ex}"
    errors.append(message)
    print(f"ERROR {message}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Close the run: write one row to the run log and print a summary.

finished_at = datetime.now(timezone.utc).replace(tzinfo=None)

if not errors:
    status = "success"
elif len(errors) >= len(target_dates) + 1:
    status = "failed"
else:
    status = "partial"

error_text = None if not errors else " | ".join(errors)[:4000]

log_row = (
    run_id,
    started_at,
    finished_at,
    days_in_scope,
    write_mode,
    ",".join(d.isoformat() for d in target_dates),
    counts[TBL_OPS],
    counts[TBL_CUD],
    counts[TBL_ITEMS],
    counts[TBL_CAPS],
    status,
    error_text,
)

print("\n--- run summary ---")
print(f"status            : {status}")
print(f"elapsed           : {(finished_at - started_at).total_seconds():.1f} s")
print(f"{TBL_OPS:<32}: {counts[TBL_OPS]} rows")
print(f"{TBL_CUD:<32}: {counts[TBL_CUD]} rows")
print(f"{TBL_ITEMS:<32}: {counts[TBL_ITEMS]} rows")
print(f"{TBL_CAPS:<32}: {counts[TBL_CAPS]} rows")
if errors:
    for message in errors:
        print(f"error             : {message}")

if write_mode == "dry_run":
    print("dry_run: run log not written")
else:
    write_rows(TBL_LOG, [log_row], SCHEMA_LOG, mode="append")
    print(f"{TBL_LOG:<32}: 1 row appended")

notebookutils.notebook.exit(status)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
