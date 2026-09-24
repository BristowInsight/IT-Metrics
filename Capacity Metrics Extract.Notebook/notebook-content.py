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
# This is a real Fabric parameter cell. The workspace toggled it on 2026-09-21 and
# Fabric wrote the "# PARAMETERS CELL" marker above, so a pipeline or a schedule can
# override any value below with base parameters. Leave that marker alone: Fabric owns
# it, and editing it by hand turns this back into an ordinary code cell.
# The Capacity Metrics Smoke and Capacity Metrics Daily pipelines pass days_in_scope,
# write_mode and table_prefix. See Capacity-Metrics-Extract.md at the repository root.

# How many whole days to load, counting back from yesterday in model time. Twelve
# stays inside the source model's retention of about 13 days, so the oldest retained
# day, which the source trims continuously, is normally left out of the window.
days_in_scope = 12

# Bristow Insight capacity. Uppercase, as the model stores it.
capacity_id = "6ABDFB99-6499-4226-93E5-C4C3B5D0E924"

# Source: the Microsoft Fabric Capacity Metrics workspace and semantic model.
metric_workspace = "75af6cf1-9c91-4220-b258-ab1d1dedc0d4"
metric_dataset = "e510b503-48b3-4414-ad4c-1e40f2be1d28"

# "replace_days" loads each in-scope date for this capacity, guarded against the
# source's trimming (see load_day). "dry_run" runs every query and writes only the
# evidence file.
write_mode = "replace_days"

# Prefix of the five Delta tables and of the evidence folder
# Files/<table_prefix without its trailing underscore>/runs/. "capmetrics_" is the
# real history. The Smoke pipeline passes "capmetrics_smoke_", so a check run never
# touches it.
table_prefix = "capmetrics_"

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

import json
import re
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone, date

import pandas as pd
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

# Written into every evidence file, so a reader knows which notebook produced it.
NOTEBOOK_VERSION = "3.1.0"

# The Capacity Metrics model stores its timepoints at a fixed offset from UTC.
# Measured 2026-09-18: UTC 19:50:43 against a latest window start of 13:46:30,
# a gap of 6 hours plus the app's own few minutes of refresh lag. This is a fixed
# offset, not US Central, so it does not follow daylight saving. See
# Capacity-Metrics-Extract.md.
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

# Transient source failures. The source model's two DirectQuery fact tables answer
# "Internal Error: Error obtaining data location" for minutes at a time and then
# recover with no change to the query, while its Import tables answer normally
# throughout: about fifteen minutes on 2026-09-21, seen through both Semantic Link
# and the Power BI REST endpoint, and 14:34 to 14:41 UTC on 2026-09-23 on every
# query through Semantic Link. The source is what fails, not the transport.
#
# Waiting inside one query cannot fix that: short waits lose dates and long waits
# make a run grind for most of an hour. So the run probes first. The probe is the
# first query of the run, three attempts 10 and 20 seconds apart; if the source is
# still not answering, the run records source_unavailable and fails at once, and
# the pipeline that started it tries again later. Every later query gets three
# attempts 20 and 60 seconds apart.
PROBE_ATTEMPTS = 3
PROBE_BACKOFF_S = (10, 20)
QUERY_ATTEMPTS = 3
QUERY_BACKOFF_S = (20, 60)

# Phrases only, never a bare status code and never the bare word "connection". The
# source dataset id e510b503-48b3-4414-ad4c-1e40f2be1d28 contains "503", so a bare
# "503" marker retried "Dataset ... not found" for this very dataset, and a bare
# "connection" retried permission errors that mention a connection. Semantic Link
# words an HTTP failure "<status> <reason> for url: ...", so the reason phrases
# below are what match it.
RETRY_ON_TEXT = (
    "error obtaining data location",
    "timed out",
    "timeout",
    "too many requests",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "connection reset",
    "connection refused",
    "connection timed out",
    "connection aborted",
    "failed to establish a new connection",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "status code 429",
    "status code 500",
    "status code 502",
    "status code 503",
    "status code 504",
)

# A loaded date counts as verified when the table's CU seconds total for it matches
# what was extracted within this relative difference.
VERIFY_REL_TOL = 1e-9

# The table names are table_prefix plus these, set once the parameters are checked.
TABLE_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]*_$")
SUFFIX_OPS = "item_operation_day"
SUFFIX_CUD = "cu_window_30s"
SUFFIX_ITEMS = "items"
SUFFIX_CAPS = "capacities"
SUFFIX_LOG = "run_log"

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Value converters. Every DataFrame is built from explicit Python tuples rather
# than handed straight from pandas, so a day that comes back empty or with a null
# column cannot change a Delta column's type between runs.
#
# None of these may be named for an IPython output variable. The notebook kernel is
# IPython, and after every cell it rebinds `_i`, `_ii` and `_iii` to the source text
# of the last three cells, and `_`, `__` and `___` to their results. A helper defined
# here under one of those names is a string by the time a later cell calls it. This
# cost a run: on 2026-09-21 the integer converter was called `_i`, and every one of
# the 13 daily dates failed with "TypeError: 'str' object is not callable" while the
# snapshot path, which never calls it, succeeded (Fabric job 34b674cb). It is `_int`
# now. The reserved names to avoid are `_`, `__`, `___`, `_i`, `_ii`, `_iii`, `_ih`,
# `_oh`, `_dh`, `In`, `Out`, `exit`, `quit` and `get_ipython`.

def _isnull(v):
    """True for None and for every missing value pandas uses.

    Semantic Link types its result columns with pandas nullable dtypes: 'string'
    for text, 'Int64' for whole numbers, 'Float64' for decimals. Their missing
    value is pd.NA, and any comparison against pd.NA returns pd.NA rather than a
    boolean, so a plain `if v == ""` on a missing measure raises
    "TypeError: boolean value of NA is ambiguous". Measured 2026-09-21 against the
    real column types, which is also why str(pd.NA) must never reach a Delta
    column: it would land the literal text "<NA>".
    """
    if v is None:
        return True
    try:
        missing = pd.isna(v)
    except (TypeError, ValueError):
        return False
    if isinstance(missing, bool):
        return missing
    try:
        return bool(missing)
    except (TypeError, ValueError):
        return False


def _s(v):
    if _isnull(v):
        return None
    t = str(v).strip()
    return t if t else None


def _f(v):
    if _isnull(v):
        return None
    if isinstance(v, str) and not v.strip():
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _int(v):
    f = _f(v)
    return None if f is None else int(f)


# _d and _ts return exactly datetime.date and datetime.datetime, never a subclass.
# spark.createDataFrame checks each value's exact type against its field, not
# isinstance, so a pandas Timestamp, which is a datetime subclass and is what
# Semantic Link returns for a DAX datetime, is refused for a TimestampType field:
# on 2026-09-23 every window write failed with "TimestampType() can not accept
# object Timestamp('2026-09-15 00:00:00')". A pandas Timestamp goes through
# to_pydatetime(), and the result, like any other datetime, is rebuilt from its
# parts, which drops any subclass and any time zone. The model's times are naive,
# so a time zone is dropped rather than converted. Every other date or timestamp
# the notebook writes (loaded_at_utc, started_at_utc, finished_at_utc,
# snapshot_date, window_date, window_start_utc, hour_start) is derived from a
# plain value.

def _d(v):
    if _isnull(v):
        return None
    if isinstance(v, pd.Timestamp):
        v = v.to_pydatetime()
    if isinstance(v, date):
        return date(v.year, v.month, v.day)
    text = str(v).strip()
    return datetime.fromisoformat(text[:19]).date() if text else None


def _ts(v):
    if _isnull(v):
        return None
    if isinstance(v, pd.Timestamp):
        v = v.to_pydatetime()
    if isinstance(v, datetime):
        return datetime(v.year, v.month, v.day,
                        v.hour, v.minute, v.second, v.microsecond)
    text = str(v).strip()
    return datetime.fromisoformat(text[:19]) if text else None


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


def _is_retryable(ex):
    """Whether this failure is worth another attempt rather than a recorded error."""
    text = f"{type(ex).__name__}: {ex}".lower()
    return any(marker in text for marker in RETRY_ON_TEXT)


def run_dax(dax_string, attempts=None, backoff=None, trace=None):
    """Run one DAX query against the Capacity Metrics model.

    Retries a transient source failure (see RETRY_ON_TEXT) up to `attempts` times,
    QUERY_ATTEMPTS unless the caller says otherwise, waiting backoff[n - 1] seconds
    after failed attempt n. Anything not on that list is raised at once, since a
    wrong query or a missing permission does not improve with waiting. When `trace`
    is a dict, trace["attempts"] holds the number of attempts made.
    """
    attempts = QUERY_ATTEMPTS if attempts is None else attempts
    backoff = QUERY_BACKOFF_S if backoff is None else backoff
    for attempt in range(1, attempts + 1):
        if trace is not None:
            trace["attempts"] = attempt
        try:
            return fabric.evaluate_dax(
                workspace=metric_workspace, dataset=metric_dataset, dax_string=dax_string
            )
        except Exception as ex:
            if attempt == attempts or not _is_retryable(ex):
                raise
            wait = backoff[attempt - 1]
            print(f"query attempt {attempt} of {attempts} failed "
                  f"({type(ex).__name__}: {ex}); retrying in {wait} s")
            time.sleep(wait)

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
# drift between runs. Adding a column later means adding it here, in the matching
# row builder and in Capacity-Metrics-Extract.md.

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

# window_hour and hour_start exist for hour-of-day analysis in the semantic model:
# Direct Lake has no calculated columns, so anything a report groups by has to be
# a real column here.
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
    StructField("window_hour", LongType(), True),
    StructField("hour_start", TimestampType(), True),
    StructField("loaded_at_utc", TimestampType(), True),
    StructField("run_id", StringType(), True),
])

# item_label is what the semantic model and the report show for an item. Item
# names are not unique across workspaces, so the label adds the kind and the
# workspace, and is unique within each snapshot (see rows_items).
SCHEMA_ITEMS = StructType([
    StructField("item_id", StringType(), True),
    StructField("workspace_id", StringType(), True),
    StructField("workspace_name", StringType(), True),
    StructField("item_name", StringType(), True),
    StructField("item_kind", StringType(), True),
    StructField("billable_type", StringType(), True),
    StructField("capacity_id", StringType(), True),
    StructField("item_label", StringType(), True),
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

# The grain of each fact table: one row per distinct combination of these columns.
GRAIN_OPS = ("capacity_id", "workspace_id", "item_id", "item_kind", "operation_name", "date")
GRAIN_CUD = ("capacity_id", "window_start")

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
            _int(r[9]), _int(r[10]), _int(r[11]),  # users, operations, successful
            _int(r[12]), _int(r[13]), _int(r[14]), _int(r[15]),  # rejected, invalid, failed, cancelled
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
        sku_cu = _int(r[11])
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
            window_start.hour,                                   # window_hour, model local
            window_start.replace(minute=0, second=0, microsecond=0),  # hour_start
            loaded_at,
            run_id,
        ))
    return out


def item_label(item_id, item_name, item_kind, workspace_name):
    """An item's readable label, before any collision suffix.

    The item name, or the item id when the name is missing, then the item kind and
    the workspace name in parentheses, a missing part left out and no parentheses
    when both are missing: "RAMCO Ingest CICD (DataflowFabric, DF RAMCO)". The kind
    is the model's own value. Names alone are not unique: on the 522 items captured
    2026-09-21, 66 names were shared by 209 items, while name, kind and workspace
    together were unique.
    """
    head = item_name or item_id
    tail = ", ".join(part for part in (item_kind, workspace_name) if part)
    if head and tail:
        return f"{head} ({tail})"
    if head:
        return head
    return f"({tail})" if tail else None


def rows_items(df, snapshot_day, loaded_at, run_id):
    """Items snapshot rows, with item_label unique within the snapshot.

    When two or more rows share a label, compared without regard to case because
    the semantic model compares text that way, each of those rows gets a space and
    the first 8 characters of its item_id in square brackets. A row whose label is
    already unique is left as it is, so the rule changes nothing until a collision
    appears, and it gives the same labels for the same snapshot every time.
    """
    labelled = []
    for r in df.itertuples(index=False, name=None):
        # item_id, workspace_id, workspace_name, item_name, item_kind,
        # billable_type, capacity_id
        values = tuple(_s(v) for v in r[:7])
        labelled.append((values, item_label(values[0], values[3], values[4], values[2])))
    uses = {}
    for values, label in labelled:
        if label is not None:
            uses[label.casefold()] = uses.get(label.casefold(), 0) + 1
    out = []
    for values, label in labelled:
        if label is not None and values[0] and uses[label.casefold()] > 1:
            label = f"{label} [{values[0][:8]}]"
        out.append(values + (label, snapshot_day, loaded_at, run_id))
    return out


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


def build_frame(rows, schema):
    """A Spark DataFrame from the rows. PySpark checks every value's type here."""
    return spark.createDataFrame(rows, schema=schema)


def save_frame(name, frame, mode="append", overwrite_schema=False):
    writer = frame.write.format("delta").mode(mode)
    if overwrite_schema:
        writer = writer.option("overwriteSchema", "true")
    writer.saveAsTable(name)


def write_rows(name, rows, schema, mode="append", overwrite_schema=False):
    save_frame(name, build_frame(rows, schema), mode, overwrite_schema)
    return len(rows)


def replace_all(name, rows, schema):
    """Replace the whole table. Used for the two snapshot tables."""
    return write_rows(name, rows, schema, mode="overwrite", overwrite_schema=True)


def _day_filter(date_col, day, cap_id):
    return f"capacity_id = '{cap_id}' AND {date_col} = DATE '{day.isoformat()}'"


def count_day(name, date_col, day, cap_id):
    """(rows, CU seconds total) already in the table for one date and capacity."""
    found = spark.sql(
        f"SELECT COUNT(*) AS n_rows, SUM(cu_s) AS cu_s FROM {name} "
        f"WHERE {_day_filter(date_col, day, cap_id)}"
    ).collect()[0]
    return int(found[0]), (None if found[1] is None else float(found[1]))


def count_keys(name, grain, date_col, day, cap_id):
    """Distinct grain keys in the table for one date and capacity.

    SELECT DISTINCT rather than COUNT(DISTINCT a, b, ...), because the second skips
    every row that has a null in any of the columns, and workspace_id can be null.
    """
    found = spark.sql(
        f"SELECT COUNT(*) AS n_keys FROM (SELECT DISTINCT {', '.join(grain)} "
        f"FROM {name} WHERE {_day_filter(date_col, day, cap_id)}) AS k"
    ).collect()[0]
    return int(found[0])


def _rel_diff(a, b):
    a = 0.0 if a is None else float(a)
    b = 0.0 if b is None else float(b)
    if a == b:
        return 0.0
    return abs(a - b) / max(abs(a), abs(b))


def new_unit(name):
    """The evidence record for one fact table on one date."""
    return {
        "table": name,
        "extracted_rows": None,
        "extracted_cu_s": None,
        "existing_rows": None,
        "action": None,
        "verification": None,
        "error": None,
    }


def note_extracted(unit, rows, schema):
    cu_at = schema.fieldNames().index("cu_s")
    unit["extracted_rows"] = len(rows)
    unit["extracted_cu_s"] = sum(r[cu_at] or 0.0 for r in rows)


def load_day(unit, name, rows, schema, date_col, grain, day, cap_id):
    """Load one date of one fact table for one capacity, then read it back.

    Trim guard: the source trims its oldest day continuously, so a later run can
    extract fewer rows for a date than an earlier run already stored. When the
    table holds more rows for the date than this extraction has, the stored rows
    are kept and nothing is deleted (action kept_existing). Otherwise the date is
    replaced: the DataFrame is built first, so a value Spark refuses fails before
    anything is deleted, then the date is deleted and the rows appended.

    Verification reads the date back. For a written date it passes when the row
    count equals the rows written, every row has its own grain key, and the CU
    seconds total matches the extraction within VERIFY_REL_TOL. For a kept date it
    passes when every stored row has its own grain key.
    """
    exists = table_exists(name)
    if exists:
        existing_rows, existing_cu = count_day(name, date_col, day, cap_id)
        unit["existing_rows"] = existing_rows
        if len(rows) < existing_rows:
            unit["action"] = "kept_existing"
            keys = count_keys(name, grain, date_col, day, cap_id)
            unit["verification"] = {
                "expected_rows": existing_rows,
                "rows": existing_rows,
                "distinct_keys": keys,
                "expected_cu_s": existing_cu,
                "cu_s": existing_cu,
                "rel_diff_cu_s": 0.0,
                "passed": keys == existing_rows,
            }
            return unit

    frame = build_frame(rows, schema)
    if exists:
        spark.sql(f"DELETE FROM {name} WHERE {_day_filter(date_col, day, cap_id)}")
    save_frame(name, frame)
    unit["action"] = "written"

    n_rows, cu = count_day(name, date_col, day, cap_id)
    keys = count_keys(name, grain, date_col, day, cap_id)
    rel = _rel_diff(cu, unit["extracted_cu_s"])
    unit["verification"] = {
        "expected_rows": len(rows),
        "rows": n_rows,
        "distinct_keys": keys,
        "expected_cu_s": unit["extracted_cu_s"],
        "cu_s": cu,
        "rel_diff_cu_s": rel,
        "passed": n_rows == len(rows) and keys == n_rows and rel <= VERIFY_REL_TOL,
    }
    return unit

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Closing a run: the run log row, the evidence file and the job status.
#
# The evidence file is one JSON document per run, at
# Files/<prefix>/runs/<run_id>.json and again at Files/<prefix>/runs/latest.json,
# in the default Lakehouse. It is what the coordinator reads after triggering a
# pipeline, since job history cannot say what a run loaded and the run log holds
# counts only. In a Spark notebook a relative path resolves to the default
# Lakehouse, and notebookutils.fs.mkdirs creates any missing parent folders.


def _jsonable(value):
    """The value with dates as ISO text and no NaN or infinity, which JSON lacks."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def write_evidence(record):
    text = json.dumps(_jsonable(record), indent=2, allow_nan=False)
    notebookutils.fs.mkdirs(EVIDENCE_DIR)
    for path in (f"{EVIDENCE_DIR}/{record['run_id']}.json", f"{EVIDENCE_DIR}/latest.json"):
        if notebookutils.fs.put(path, text, True) is False:
            raise OSError(f"notebookutils.fs.put returned False for {path}")
        print(f"evidence written: {path}")


def finish_run(status):
    """Write the run log row, then the evidence file, then fail the job unless clean.

    notebookutils.notebook.exit() leaves the job status Completed whatever value it
    is handed, so a partial or a failed run used to look like a success to the
    scheduler and in job history. Anything but a clean run raises RuntimeError
    after both records are written, so the job, and the pipeline activity that ran
    it, end Failed. A failure to write either record fails the job too.
    """
    finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
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
    for name in (TBL_OPS, TBL_CUD, TBL_ITEMS, TBL_CAPS):
        print(f"{name:<40}: {counts[name]} rows written")
    for message in errors:
        print(f"error             : {message}")

    problems = []
    if write_mode == "dry_run":
        print("dry_run: run log not written")
    else:
        try:
            write_rows(TBL_LOG, [log_row], SCHEMA_LOG, mode="append")
            evidence["run_log_row_written"] = True
            print(f"{TBL_LOG:<40}: 1 row appended")
        except Exception:
            message = f"run log: {traceback.format_exc()}"
            errors.append(message)
            problems.append("the run log row was not written")
            print(f"ERROR {message}")

    evidence["status"] = status
    evidence["finished_at_utc"] = finished_at
    evidence["elapsed_s"] = round((finished_at - started_at).total_seconds(), 3)
    try:
        write_evidence(evidence)
    except Exception:
        problems.append("the evidence file was not written")
        print(f"ERROR evidence: {traceback.format_exc()}")

    if status != "success" or problems:
        raise RuntimeError(
            f"Capacity Metrics Extract finished with status {status!r}"
            + (f" ({'; '.join(problems)})" if problems else "")
            + f". {error_text}"
        )

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

table_prefix = str(table_prefix).strip()
if not TABLE_PREFIX_RE.match(table_prefix):
    raise ValueError(
        f"table_prefix must match {TABLE_PREFIX_RE.pattern}, got {table_prefix!r}")

capacity_id = str(capacity_id).strip().upper()
for name, value in (("capacity_id", capacity_id),
                    ("metric_workspace", metric_workspace),
                    ("metric_dataset", metric_dataset)):
    if not GUID_RE.match(str(value).strip()):
        raise ValueError(f"{name} must be a GUID, got {value!r}")

TBL_OPS = f"{table_prefix}{SUFFIX_OPS}"
TBL_CUD = f"{table_prefix}{SUFFIX_CUD}"
TBL_ITEMS = f"{table_prefix}{SUFFIX_ITEMS}"
TBL_CAPS = f"{table_prefix}{SUFFIX_CAPS}"
TBL_LOG = f"{table_prefix}{SUFFIX_LOG}"
EVIDENCE_DIR = f"Files/{table_prefix.rstrip('_')}/runs"

run_id = str(uuid.uuid4())
started_at = datetime.now(timezone.utc).replace(tzinfo=None)
target_dates = dates_in_scope(days_in_scope)
snapshot_day = model_today()
probe_day = target_dates[-1]

print(f"notebook_version  : {NOTEBOOK_VERSION}")
print(f"run_id            : {run_id}")
print(f"started_at_utc    : {started_at.isoformat()}")
print(f"write_mode        : {write_mode}")
print(f"table_prefix      : {table_prefix}")
print(f"capacity_id       : {capacity_id}")
print(f"model today       : {snapshot_day.isoformat()} (UTC {MODEL_UTC_OFFSET_HOURS:+d} hours)")
print(f"days_in_scope     : {days_in_scope}")
print(f"dates to process  : {', '.join(d.isoformat() for d in target_dates)}")
print(f"evidence folder   : {EVIDENCE_DIR}")

counts = {TBL_OPS: 0, TBL_CUD: 0, TBL_ITEMS: 0, TBL_CAPS: 0}
errors = []
evidence = {
    "notebook_version": NOTEBOOK_VERSION,
    "run_id": run_id,
    "started_at_utc": started_at,
    "finished_at_utc": None,
    "parameters": {
        "days_in_scope": days_in_scope,
        "capacity_id": capacity_id,
        "metric_workspace": metric_workspace,
        "metric_dataset": metric_dataset,
        "write_mode": write_mode,
        "table_prefix": table_prefix,
    },
    "probe": {"date": probe_day, "attempts": 0, "seconds": None,
              "outcome": None, "error": None},
    "dates": {},
    "snapshots": {},
    "run_log_row_written": False,
    "status": None,
    "errors": errors,
    "elapsed_s": None,
}

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Probe. The first query of the run is the 30 second window query for yesterday,
# the same query the daily loop would send for that date. If the source answers,
# the result is kept and used for yesterday's windows, so that date is not queried
# twice. If it keeps answering with a transient failure, the run records
# source_unavailable, writes the run log row and the evidence file, and fails
# within about two minutes, leaving the retry to the pipeline. Any other failure
# (a permission, a wrong id) ends the run the same way with status failed.

probe_frame = None
probe_clock = time.monotonic()
try:
    probe_frame = run_dax(dax_cu_window_30s(capacity_id, probe_day),
                          attempts=PROBE_ATTEMPTS, backoff=PROBE_BACKOFF_S,
                          trace=evidence["probe"])
    evidence["probe"]["outcome"] = "ok"
except Exception as ex:
    evidence["probe"]["outcome"] = "source_unavailable" if _is_retryable(ex) else "failed"
    evidence["probe"]["error"] = traceback.format_exc()
    errors.append(f"probe {probe_day.isoformat()}: {traceback.format_exc()}")
finally:
    evidence["probe"]["seconds"] = round(time.monotonic() - probe_clock, 3)

print(f"probe {probe_day.isoformat()}: {evidence['probe']['outcome']} after "
      f"{evidence['probe']['attempts']} attempt(s), {evidence['probe']['seconds']} s")

if probe_frame is None:
    finish_run(evidence["probe"]["outcome"])

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# One day at a time, and within a day one table at a time. A failure on one table
# for one date is recorded and the loop carries on, so a single bad query or write
# cannot cost the rest of the run.


def record_failure(unit, label):
    # The whole traceback, not just the type and the message. A run that fails
    # inside a library is unreadable without it, and the run log is the only
    # record left once the Spark session is gone.
    message = f"{label}: {traceback.format_exc()}"
    errors.append(message)
    unit["error"] = message
    if unit["action"] is None:
        unit["action"] = "failed"
    print(f"ERROR {message}")


def report_unit(unit):
    v = unit["verification"]
    if v is None:
        return
    print(f"{unit['table']}: {unit['action']}, {v['rows']} rows (expected "
          f"{v['expected_rows']}), {v['distinct_keys']} distinct keys, cu_s relative "
          f"difference {v['rel_diff_cu_s']:.1e}: "
          f"{'verified' if v['passed'] else 'VERIFICATION FAILED'}")
    if not v["passed"]:
        message = f"{unit['table']}: verification failed: {v}"
        errors.append(message)
        unit["error"] = message


for day in target_dates:
    loaded_at = datetime.now(timezone.utc).replace(tzinfo=None)
    label = day.isoformat()
    print(f"\n--- {label} ---")
    day_record = {}
    evidence["dates"][label] = day_record

    ops_unit = new_unit(TBL_OPS)
    day_record[SUFFIX_OPS] = ops_unit
    try:
        ops_rows = rows_item_operation_day(
            run_dax(dax_item_operation_day(capacity_id, day)), loaded_at, run_id)
        note_extracted(ops_unit, ops_rows, SCHEMA_OPS)
        print(f"item operation rows : {len(ops_rows)}   cu_s {ops_unit['extracted_cu_s']:,.4f}")
        if write_mode == "dry_run":
            ops_unit["action"] = "dry_run"
            top = sorted(ops_rows, key=lambda r: r[7] or 0.0, reverse=True)[:5]
            print("top 5 item operations by cu_s (item_id, operation, kind, cu_s):")
            for r in top:
                print(f"  {r[2]}  {r[4]:<48.48}  {r[3]:<16.16}  {r[7]:,.4f}")
        else:
            load_day(ops_unit, TBL_OPS, ops_rows, SCHEMA_OPS, "date", GRAIN_OPS,
                     day, capacity_id)
            if ops_unit["action"] == "written":
                counts[TBL_OPS] += len(ops_rows)
            report_unit(ops_unit)
    except Exception:
        record_failure(ops_unit, f"{label} {TBL_OPS}")

    cud_unit = new_unit(TBL_CUD)
    cud_unit["window_count"] = None
    cud_unit["peak_utilization_pct"] = None
    cud_unit["window_hours"] = None
    day_record[SUFFIX_CUD] = cud_unit
    try:
        if day == probe_day and probe_frame is not None:
            cud_df = probe_frame
            print("30 second windows from the probe, not queried again")
        else:
            cud_df = run_dax(dax_cu_window_30s(capacity_id, day))
        cud_rows = rows_cu_window_30s(cud_df, capacity_id, loaded_at, run_id)
        note_extracted(cud_unit, cud_rows, SCHEMA_CUD)
        cud_unit["window_count"] = len(cud_rows)
        cud_unit["peak_utilization_pct"] = max(
            (r[16] for r in cud_rows if r[16] is not None), default=None)
        # Distinct window_hour values, 24 on a complete day.
        cud_unit["window_hours"] = len({r[17] for r in cud_rows})
        print(f"30 second windows   : {len(cud_rows)}   cu_s {cud_unit['extracted_cu_s']:,.4f}")
        if len(cud_rows) not in (0, 2880):
            print(f"NOTE: {len(cud_rows)} windows, a complete day is 2880. "
                  f"Expected for the oldest retained day, or after a gap in the source.")
        if cud_unit["peak_utilization_pct"] is not None:
            print(f"peak window utilization: {cud_unit['peak_utilization_pct']:.4%}")
        if write_mode == "dry_run":
            cud_unit["action"] = "dry_run"
        else:
            load_day(cud_unit, TBL_CUD, cud_rows, SCHEMA_CUD, "window_date", GRAIN_CUD,
                     day, capacity_id)
            if cud_unit["action"] == "written":
                counts[TBL_CUD] += len(cud_rows)
            report_unit(cud_unit)
    except Exception:
        record_failure(cud_unit, f"{label} {TBL_CUD}")

    if write_mode == "dry_run":
        print("dry_run: nothing written")

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
snap_items = {"table": TBL_ITEMS, "rows": None, "action": None, "error": None}
snap_caps = {"table": TBL_CAPS, "rows": None, "action": None, "error": None}
evidence["snapshots"] = {SUFFIX_ITEMS: snap_items, SUFFIX_CAPS: snap_caps}
try:
    items_rows = rows_items(run_dax(dax_items(capacity_id)), snapshot_day, loaded_at, run_id)
    snap_items["rows"] = len(items_rows)
    caps_rows = rows_capacities(run_dax(dax_capacities(capacity_id)), snapshot_day, loaded_at, run_id)
    snap_caps["rows"] = len(caps_rows)

    distinct_items = len({r[0] for r in items_rows})
    distinct_labels = len({r[7].casefold() for r in items_rows if r[7] is not None})
    print(f"items      : {len(items_rows)} rows, {distinct_items} distinct item_id, "
          f"{distinct_labels} distinct item_label")
    if distinct_items != len(items_rows):
        print("NOTE: item_id is not unique in this snapshot, which was not true on 2026-09-18.")
    print(f"capacities : {len(caps_rows)} rows")

    if write_mode == "dry_run":
        snap_items["action"] = snap_caps["action"] = "dry_run"
        print("dry_run: nothing written")
    else:
        counts[TBL_ITEMS] = replace_all(TBL_ITEMS, items_rows, SCHEMA_ITEMS)
        snap_items["action"] = "written"
        counts[TBL_CAPS] = replace_all(TBL_CAPS, caps_rows, SCHEMA_CAPS)
        snap_caps["action"] = "written"
        print(f"written to {TBL_ITEMS} and {TBL_CAPS}")

except Exception:
    message = f"snapshots: {traceback.format_exc()}"
    errors.append(message)
    for entry in (snap_items, snap_caps):
        if entry["action"] is None:
            entry["action"] = "failed"
            entry["error"] = message
    print(f"ERROR {message}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Close the run. success when nothing went wrong; failed when every table on every
# date and the snapshots all failed; partial otherwise, including a date whose
# read-back did not verify. A kept_existing date is not a failure.

fact_units = [u for record in evidence["dates"].values() for u in record.values()]
snapshots_failed = any(e["error"] for e in evidence["snapshots"].values())
if not errors:
    status = "success"
elif fact_units and all(u["error"] for u in fact_units) and snapshots_failed:
    status = "failed"
else:
    status = "partial"

finish_run(status)

notebookutils.notebook.exit(status)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
