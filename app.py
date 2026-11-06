"""
O₂ SENSOR IN ENVIRONMENTAL CHAMBER
Jig 1 – 100% O₂ Sensor Aging and Degradation Monitoring

Live Streamlit dashboard. Data is read directly from the Jig 1 Google Sheet
"O2 Sensor in ENV chamber with 100%Oxygen" (tab "Sheet1") with a Google
service account.

Why not the Apps Script URL?  The Web App
https://script.google.com/macros/s/AKfycbz6mGzgzj4ihcK0QxntqSUelrNOJHMGtgUqWWe7a244han2Uq1vbu880TeNZqJW1pRPyQ/exec
was inspected on 03 Nov 2026: a GET returns only the plain-text line
"ESP8266 Google Sheets Logger is running." It is the logger's *write*
endpoint and serves no readings, so the dashboard reads the sheet the logger
writes to.

Study protocol (fixed – do not edit):
  • Study window : 21 Oct 2026 (Day 0) → 11 Dec 2026 (Day 51), 52 days inclusive
  • Baseline     : fixed common value of 98.2812 mV for S-1 … S-8
  • Valid band   : 50 mV ≤ reading ≤ 110 mV for S-1 … S-8 (100 % O₂)
  • Degradation  : ((Baseline − Daily Average) / Baseline) × 100
  • Dead sensor  : raw readings; <69 mV triggers 1,000-reading recovery + 1,000-reading confirmation
"""

from __future__ import annotations

import io
import math
import re
import threading
import warnings
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# =============================================================================
# 1. FIXED STUDY CONFIGURATION
# =============================================================================
APP_TITLE = "O₂ SENSOR IN ENVIRONMENTAL CHAMBER"
APP_SUBTITLE = "Jig 1 – 100% O₂ Sensor Aging and Degradation Monitoring"

SHEET_ID = "1cvA7pU8m-s6F3ktVxEK2v-Mw8ZpRMAot4uEciEtz9Sk"   # "O2 Sensor in ENV chamber with 100%Oxygen"
WORKSHEET_NAME = "Sheet1"
APPS_SCRIPT_URL = ("https://script.google.com/macros/s/AKfycbz6mGzgzj4ihcK0QxntqSUelrNOJHMGtgU"
                   "qWWe7a244han2Uq1vbu880TeNZqJW1pRPyQ/exec")

STUDY_START = date(2026, 10, 21)          # Day 0
STUDY_DAYS = 52                           # inclusive
STUDY_END = STUDY_START + timedelta(days=STUDY_DAYS - 1)   # 11 Dec 2026 (Day 51)
BASELINE_DATE = STUDY_START               # study start date retained for protocol metadata
BASELINE_MV = 98.2812                    # common fixed baseline for S-1 through S-8

# Dead-sensor verification uses raw readings only.
DEAD_TRIGGER_MV = 69.0                   # trigger only when strictly below 69 mV
RECOVERY_TARGET_MV = 70.0                # recovery threshold (inclusive)
RECOVERY_WINDOW_READINGS = 1000          # readings allowed to reach 70 mV
NORMAL_CONFIRM_READINGS = 1000           # readings that must remain >= 70 mV

MIN_MV = 50.0                             # inclusive
MAX_MV = 110.0                            # inclusive
EXPOSURE = "100% O₂"

REFRESH_SECONDS = 60
SHEET_CACHE_TTL = 50
STALE_AFTER_MINUTES = 10

# Sheet layout: A = Timestamp, B = Temperature (°C), C..J = S-1..S-8 (mV), read by
# position. Temperature is found by a header containing "temp"; if it is ever
# removed, the temperature views hide themselves.
SENSORS = [  # name, sheet column index, physical sensor ID, colour
    ("S-1", 2, "S-20-23%", "#D55E00"), ("S-2", 3, "S-14-23%", "#E69F00"),
    ("S-3", 4, "S-19-23%", "#0072B2"), ("S-4", 5, "S-09-23%", "#009E73"),
    ("S-5", 6, "S-15-23%", "#56B4E9"), ("S-6", 7, "S-05-23%", "#CC79A7"),
    ("S-7", 8, "S-01-23%", "#7B61FF"), ("S-8", 9, "S-11-23%", "#8C8C8C"),
]
SENSOR_NAMES = [s[0] for s in SENSORS]
SENSOR_COL = {s[0]: s[1] for s in SENSORS}
PHYSICAL = {s[0]: s[2] for s in SENSORS}
COLOR = {s[0]: s[3] for s in SENSORS}
COL_LETTER = {s: "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[i] for s, i in SENSOR_COL.items()}

SHEETS_EPOCH = pd.Timestamp("1899-12-30")


# =============================================================================
# 2. OPTIONAL SETTINGS FROM SECRETS
# =============================================================================
def _setting(key: str, default: str) -> str:
    try:
        return str(st.secrets.get("app_settings", {}).get(key, default))
    except Exception:
        return default


DISPLAY_TZ = ZoneInfo(_setting("display_timezone", "Asia/Kolkata"))
# Jig 1 logger writes month/day/year (e.g. 11/3/2026). "auto" detects from data;
# when ambiguous it falls back to month-first for this jig.
TIMESTAMP_ORDER = _setting("timestamp_order", "auto").lower()


# =============================================================================
# 3. PARSING HELPERS
# =============================================================================
_UNIT_RE = re.compile(r"\s*(mv|°c|degc|c)\s*$", re.IGNORECASE)
_MDY_RE = re.compile(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})")


def parse_number(v) -> float:
    if v is None or isinstance(v, bool):
        return math.nan
    if isinstance(v, (int, float, np.integer, np.floating)):
        f = float(v)
        return f if math.isfinite(f) else math.nan
    if isinstance(v, str):
        t = _UNIT_RE.sub("", v.strip())
        if not t:
            return math.nan
        if "," in t and "." not in t:
            t = t.replace(",", ".")
        try:
            f = float(t)
        except ValueError:
            return math.nan
        return f if math.isfinite(f) else math.nan
    return math.nan


def _detect_dayfirst(strings: list[str]) -> bool:
    if TIMESTAMP_ORDER == "dayfirst":
        return True
    if TIMESTAMP_ORDER == "monthfirst":
        return False
    for s in strings:
        m = _MDY_RE.match(s)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12:
                return True
            if b > 12:
                return False
    return False   # ambiguous → month-first (Jig 1 logger format)


def parse_timestamps(raw: pd.Series) -> pd.Series:
    out = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")
    serial_idx, serial_val, str_idx, str_val = [], [], [], []
    for i, v in zip(raw.index, raw.tolist()):
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float, np.integer, np.floating)):
            if 20000 < float(v) < 100000:
                serial_idx.append(i)
                serial_val.append(float(v))
        elif isinstance(v, str) and re.search(r"\d", v):
            str_idx.append(i)
            str_val.append(v.strip())
    if serial_idx:
        ts = SHEETS_EPOCH + pd.to_timedelta(serial_val, unit="D")
        out.loc[serial_idx] = pd.DatetimeIndex(ts).round("s")
    if str_idx:
        dayfirst = _detect_dayfirst(str_val)
        s = pd.Series(str_val, index=str_idx)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            md = "%d/%m/%Y" if dayfirst else "%m/%d/%Y"
            parsed = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
            for fmt in (f"{md} %H:%M:%S", f"{md} %H:%M", md.replace("/", "-") + " %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                todo = parsed.isna()
                if not todo.any():
                    break
                parsed.loc[todo] = pd.to_datetime(s[todo], format=fmt, errors="coerce")
            todo = parsed.isna()
            if todo.any():
                parsed.loc[todo] = pd.to_datetime(s[todo], dayfirst=dayfirst, errors="coerce")
            failed = parsed.isna()
            if failed.any():
                parsed.loc[failed] = pd.to_datetime(s[failed], format="mixed", dayfirst=dayfirst, errors="coerce")
        if getattr(parsed.dt, "tz", None) is not None:
            parsed = parsed.dt.tz_convert(DISPLAY_TZ).dt.tz_localize(None)
        out.loc[str_idx] = parsed.astype("datetime64[ns]")
    return out


def classify(v: float) -> str:
    if v is None or not math.isfinite(v):
        return "No Data"
    if v < MIN_MV:
        return "Below Limit"
    if v > MAX_MV:
        return "Above Limit"
    return "Within Range"


# =============================================================================
# 4. CORE CALCULATIONS (pure functions)
# =============================================================================
def detect_dead_sensors(raw_data: pd.DataFrame) -> dict:
    """Evaluate each sensor from chronological, unfiltered/raw readings.

    A reading <69 mV starts a recovery window. The next 1,000 numeric readings
    must contain a value >=70 mV. Once reached, the following 1,000 numeric
    readings must all remain >=70 mV. Any value <70 mV in that confirmation
    window declares the sensor dead immediately. A dead state is latched.
    """
    results = {}
    for sensor in SENSOR_NAMES:
        state = "NORMAL"
        recovery_count = 0
        confirm_count = 0
        trigger_ts = pd.NaT
        fault_ts = pd.NaT
        last_value = np.nan
        has_reading = False

        for row in raw_data[["Timestamp", sensor]].itertuples(index=False, name=None):
            ts, value = row
            if pd.isna(value):
                continue
            value = float(value)
            last_value = value
            has_reading = True

            if state == "DEAD SENSOR":
                continue

            if state == "NORMAL":
                if value < DEAD_TRIGGER_MV:
                    state = "RECOVERY CHECK"
                    recovery_count = 0
                    confirm_count = 0
                    trigger_ts = ts
                continue

            if state == "RECOVERY CHECK":
                recovery_count += 1
                if value >= RECOVERY_TARGET_MV:
                    state = "NORMAL CONFIRMATION"
                    confirm_count = 0
                elif recovery_count >= RECOVERY_WINDOW_READINGS:
                    state = "DEAD SENSOR"
                    fault_ts = ts
                continue

            if state == "NORMAL CONFIRMATION":
                if value < RECOVERY_TARGET_MV:
                    state = "DEAD SENSOR"
                    fault_ts = ts
                else:
                    confirm_count += 1
                    if confirm_count >= NORMAL_CONFIRM_READINGS:
                        state = "NORMAL"
                        recovery_count = 0
                        confirm_count = 0
                        trigger_ts = pd.NaT
                        fault_ts = pd.NaT

        if not has_reading:
            state = "NO DATA"

        if state == "RECOVERY CHECK":
            display_status = "RECOVERY CHECK"
            progress = f"{recovery_count}/{RECOVERY_WINDOW_READINGS}"
        elif state == "NORMAL CONFIRMATION":
            display_status = "NORMAL CHECK"
            progress = f"{confirm_count}/{NORMAL_CONFIRM_READINGS}"
        else:
            display_status = state
            progress = "Complete" if state == "DEAD SENSOR" else "—"

        results[sensor] = {
            "status": display_status,
            "progress": progress,
            "recovery_count": recovery_count,
            "confirm_count": confirm_count,
            "trigger_ts": trigger_ts,
            "fault_ts": fault_ts,
            "last_raw_value": last_value,
        }
    return results


def process_values(values: list[list]) -> dict:
    rows = [list(r) for r in (values or []) if any(c not in ("", None) for c in r)]
    header, header_skipped = None, False
    if rows and isinstance(rows[0][0], str) and not re.search(r"\d", rows[0][0]):
        header, rows, header_skipped = [str(c) for c in rows[0]], rows[1:], True

    width = max([len(r) for r in rows] + [10])
    rows = [r + [""] * (width - len(r)) for r in rows]
    temp_col = None
    if header:
        for i, h in enumerate(header):
            if i not in SENSOR_COL.values() and i != 0 and "temp" in h.lower():
                temp_col = i
                break
    sensor_headers = {s: (header[c].strip() if header and c < len(header) else f"Column {COL_LETTER[s]}")
                      for s, c in SENSOR_COL.items()}

    col0 = pd.Series([r[0] for r in rows], dtype=object)
    ts = parse_timestamps(col0) if len(rows) else pd.Series(dtype="datetime64[ns]")
    valid_ts = ts.notna()
    start_ts = pd.Timestamp(STUDY_START)
    end_ts = pd.Timestamp(STUDY_END) + pd.Timedelta(days=1)
    in_window = valid_ts & (ts >= start_ts) & (ts < end_ts)

    data = pd.DataFrame({"Timestamp": ts})
    data["Temperature (°C)"] = [parse_number(r[temp_col]) for r in rows] if temp_col is not None else np.nan
    for s, c in SENSOR_COL.items():
        data[s] = [parse_number(r[c]) for r in rows]
    data["Sheet Row"] = np.arange(len(data)) + (2 if header_skipped else 1)
    data = data[in_window].sort_values(["Timestamp", "Sheet Row"], kind="stable").reset_index(drop=True)
    data["Date"] = data["Timestamp"].dt.normalize()
    data["Study Day"] = (data["Date"] - start_ts).dt.days

    raw = data[SENSOR_NAMES]
    missing = raw.isna()
    below = raw.lt(MIN_MV)            # strictly below → 50.0 accepted
    above = raw.gt(MAX_MV)            # strictly above → 110.0 accepted
    excluded = below | above
    valid = ~missing & ~excluded
    filtered = raw.where(valid)

    if len(data):
        last_day = min(data["Date"].max(), pd.Timestamp(STUDY_END))
        all_dates = pd.date_range(start_ts, last_day, freq="D")
    else:
        all_dates = pd.DatetimeIndex([])
    by = data["Date"]

    def agg(frame, how):
        g = getattr(frame.groupby(by), how)()
        return g.reindex(all_dates, fill_value=0 if how == "sum" else np.nan)

    daily_avg = agg(filtered, "mean")
    daily_valid = agg(valid, "sum").astype(int)
    daily_below = agg(below, "sum").astype(int)
    daily_above = agg(above, "sum").astype(int)
    daily_missing = agg(missing, "sum").astype(int)
    daily_records = data.groupby(by).size().reindex(all_dates, fill_value=0).astype(int)
    daily_temp = data.groupby(by)["Temperature (°C)"].mean().reindex(all_dates)
    for f in (daily_avg, daily_valid, daily_below, daily_above, daily_missing, daily_records, daily_temp):
        f.index.name = "Date"

    if start_ts in daily_avg.index:
        baseline_computed = daily_avg.loc[start_ts].astype(float)
        baseline_n = daily_valid.loc[start_ts].astype(int)
        baseline_excl = (daily_below.loc[start_ts] + daily_above.loc[start_ts]).astype(int)
    else:
        baseline_computed = pd.Series(np.nan, index=SENSOR_NAMES, dtype=float)
        baseline_n = pd.Series(0, index=SENSOR_NAMES, dtype=int)
        baseline_excl = pd.Series(0, index=SENSOR_NAMES, dtype=int)

    latest = {}
    for s in SENSOR_NAMES:
        iv, ir = filtered[s].last_valid_index(), raw[s].last_valid_index()
        latest[s] = {
            "valid_value": filtered[s].loc[iv] if iv is not None else np.nan,
            "valid_ts": data["Timestamp"].loc[iv] if iv is not None else pd.NaT,
            "raw_value": raw[s].loc[ir] if ir is not None else np.nan,
            "raw_ts": data["Timestamp"].loc[ir] if ir is not None else pd.NaT,
            "status": classify(raw[s].loc[ir]) if ir is not None else "No Data",
        }
    has_temp = temp_col is not None and data["Temperature (°C)"].notna().any()
    it = data["Temperature (°C)"].last_valid_index() if has_temp else None
    latest_temp = (data["Temperature (°C)"].loc[it], data["Timestamp"].loc[it]) if it is not None else (np.nan, pd.NaT)

    return {
        "data": data, "filtered": filtered, "valid": valid, "below": below, "above": above,
        "excluded": excluded, "missing": missing,
        "daily_avg": daily_avg, "daily_valid": daily_valid, "daily_below": daily_below,
        "daily_above": daily_above, "daily_missing": daily_missing, "daily_records": daily_records,
        "daily_temp": daily_temp, "has_temp": bool(has_temp), "temp_header": header[temp_col] if temp_col is not None else None,
        "sensor_headers": sensor_headers,
        "baseline_computed": baseline_computed, "baseline_n": baseline_n, "baseline_excl": baseline_excl,
        "latest": latest, "latest_temp": latest_temp,
        "dead_sensor": detect_dead_sensors(data),
        "latest_ts": data["Timestamp"].max() if len(data) else pd.NaT,
        "sheet_rows": len(rows), "header_skipped": header_skipped,
        "invalid_ts_rows": int((~valid_ts).sum()), "outside_window_rows": int((valid_ts & ~in_window).sum()),
        "records": len(data),
        "total_readings": int((~missing).to_numpy().sum()),
        "total_valid": int(valid.to_numpy().sum()),
        "total_below": int(below.to_numpy().sum()),
        "total_above": int(above.to_numpy().sum()),
        "total_excluded": int(excluded.to_numpy().sum()),
    }


def baseline_is_valid(b) -> bool:
    return b is not None and isinstance(b, (int, float, np.floating)) and math.isfinite(b) and b > 0


def degradation(avg, baseline):
    base = pd.Series(baseline, dtype=float)
    base = base.where(base.gt(0) & np.isfinite(base))
    return (base - avg) / base * 100.0


def build_daily_wide(res, baseline) -> pd.DataFrame:
    """Date, Study Day, Temp, S-1..S-8 daily averages (blank when no valid readings)."""
    idx = res["daily_avg"].index
    out = pd.DataFrame({"Date": idx, "Study Day": (idx - pd.Timestamp(STUDY_START)).days})
    if res["has_temp"]:
        out["Daily Avg Temp (°C)"] = res["daily_temp"].to_numpy()
    for s in SENSOR_NAMES:
        out[f"{s} Daily Avg (mV)"] = res["daily_avg"][s].to_numpy()
    return out


def build_degradation_wide(res, baseline) -> pd.DataFrame:
    deg = degradation(res["daily_avg"], baseline)
    idx = deg.index
    out = pd.DataFrame({"Date": idx, "Study Day": (idx - pd.Timestamp(STUDY_START)).days})
    for s in SENSOR_NAMES:
        out[f"{s} Degradation (%)"] = deg[s].to_numpy()
    return out


def build_daily_long(res, baseline) -> pd.DataFrame:
    deg = degradation(res["daily_avg"], baseline)
    frames = []
    for s in SENSOR_NAMES:
        frames.append(pd.DataFrame({
            "Date": res["daily_avg"].index,
            "Study Day": (res["daily_avg"].index - pd.Timestamp(STUDY_START)).days,
            "Sensor": s, "Physical ID": PHYSICAL[s],
            "Daily Avg (mV)": res["daily_avg"][s].to_numpy(),
            "Degradation (%)": deg[s].to_numpy(),
            "Valid": res["daily_valid"][s].to_numpy(),
            "Below 50 mV": res["daily_below"][s].to_numpy(),
            "Above 110 mV": res["daily_above"][s].to_numpy(),
            "Missing/Invalid": res["daily_missing"][s].to_numpy(),
        }))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["Date", "Study Day"], kind="stable").reset_index(drop=True)


def build_latest_table(res, baseline, bstatus) -> pd.DataFrame:
    deg = degradation(res["daily_avg"], baseline)
    rows = []
    for s in SENSOR_NAMES:
        col = res["daily_avg"][s].dropna()
        last_day = col.index.max() if len(col) else pd.NaT
        L = res["latest"][s]
        rows.append({
            "Sensor": s, "Physical ID": PHYSICAL[s], "Exposure": EXPOSURE,
            "Baseline Date": "Fixed common baseline",
            "Baseline Output (mV)": baseline[s], "Baseline Status": bstatus[s],
            "Latest Reading (mV)": L["raw_value"], "Latest Reading Time": L["raw_ts"], "Status": L["status"],
            "Latest Avg Date": last_day.strftime("%d %b %Y") if pd.notna(last_day) else "—",
            "Latest Daily Average (mV)": col.iloc[-1] if len(col) else np.nan,
            "Degradation (%)": deg[s].loc[last_day] if pd.notna(last_day) else np.nan,
            "Minimum Limit (mV)": MIN_MV, "Maximum Limit (mV)": MAX_MV,
            "Dead-Sensor Status": res["dead_sensor"][s]["status"],
            "Verification Progress": res["dead_sensor"][s]["progress"],
            "Low-Reading Trigger Time": res["dead_sensor"][s]["trigger_ts"],
            "Dead-Sensor Fault Time": res["dead_sensor"][s]["fault_ts"],
        })
    return pd.DataFrame(rows)


def exclusion_summary(res, mask=None, sensors=None) -> pd.DataFrame:
    keys = ["valid", "below", "above", "missing"]
    fr = {k: res[k] if mask is None else res[k][mask] for k in keys}
    raw = res["data"][SENSOR_NAMES] if mask is None else res["data"].loc[mask, SENSOR_NAMES]
    rows = []
    for s in sensors or SENSOR_NAMES:
        n_num = int((~fr["missing"][s]).sum())
        n_b, n_a = int(fr["below"][s].sum()), int(fr["above"][s].sum())
        rows.append({
            "Sensor": s, "Physical ID": PHYSICAL[s], "Exposure": EXPOSURE, "Min (mV)": MIN_MV, "Max (mV)": MAX_MV,
            "Records": int(len(raw)), "Numeric Readings": n_num, "Valid Readings": int(fr["valid"][s].sum()),
            "Below 50 mV": n_b, "Above 110 mV": n_a, "Total Excluded": n_b + n_a,
            "Missing/Invalid": int(fr["missing"][s].sum()),
            "Excluded (%)": ((n_b + n_a) / n_num * 100) if n_num else np.nan,
            "Min Raw (mV)": raw[s].min() if n_num else np.nan,
            "Max Raw (mV)": raw[s].max() if n_num else np.nan,
        })
    return pd.DataFrame(rows)


def raw_table(res, filtered: bool) -> pd.DataFrame:
    d = res["data"]
    out = pd.DataFrame({"Timestamp": d["Timestamp"], "Study Day": d["Study Day"]})
    if res["has_temp"]:
        out["Temperature (°C)"] = d["Temperature (°C)"]
    src = res["filtered"] if filtered else d[SENSOR_NAMES]
    for s in SENSOR_NAMES:
        out[f"{s} (mV)"] = src[s]
    if filtered:
        names = np.array(SENSOR_NAMES)
        out["Excluded Sensors"] = [", ".join(names[r]) for r in res["excluded"][SENSOR_NAMES].to_numpy()]
    out["Sheet Row"] = d["Sheet Row"]
    return out


# =============================================================================
# 5. GOOGLE SHEETS ACCESS
# =============================================================================
@st.cache_resource(show_spinner=False)
def get_gspread_client():
    import gspread
    if "gcp_service_account" not in st.secrets:
        raise RuntimeError("Streamlit secret [gcp_service_account] is missing. Add the service-account "
                           "credentials to the app secrets (see README).")
    return gspread.service_account_from_dict(dict(st.secrets["gcp_service_account"]))


@st.cache_resource(ttl=SHEET_CACHE_TTL, show_spinner=False, max_entries=1)
def fetch_sheet_values() -> tuple[list[list], str]:
    from gspread.utils import DateTimeOption, ValueRenderOption
    ws = get_gspread_client().open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)
    values = ws.get_all_values(value_render_option=ValueRenderOption.unformatted,
                               date_time_render_option=DateTimeOption.serial_number)
    return values, datetime.now(timezone.utc).isoformat()


@st.cache_resource(show_spinner=False, max_entries=2)
def process_cached(fetch_id: str, _values) -> dict:
    return process_values(_values)


@st.cache_resource(show_spinner=False, max_entries=2)
def raw_tables_cached(fetch_id: str, _res) -> tuple[pd.DataFrame, pd.DataFrame]:
    return raw_table(_res, filtered=False), raw_table(_res, filtered=True)


def friendly_error(exc: Exception) -> str:
    name = type(exc).__name__
    if name == "SpreadsheetNotFound":
        return ("Jig 1 spreadsheet not found or not shared. Share the sheet with the service-account "
                "e-mail (Viewer access is enough).")
    if name == "WorksheetNotFound":
        return f"Worksheet '{WORKSHEET_NAME}' was not found in the Jig 1 spreadsheet."
    if name == "APIError":
        return f"Google Sheets API error: {exc}"
    return f"{name}: {exc}"


# =============================================================================
# 6. BASELINE LOCK + LAST GOOD SNAPSHOT (shared by all viewers)
# =============================================================================
@st.cache_resource(show_spinner=False)
def shared_state() -> dict:
    return {"lock": threading.Lock(), "baseline": {}, "baseline_n": {}, "baseline_locked_at": {}, "last_good": None}


def resolve_baseline(res):
    """Return the agreed common fixed baseline for all eight sensors."""
    baseline = pd.Series({s: BASELINE_MV for s in SENSOR_NAMES}, dtype=float)
    status = {s: "Fixed common baseline" for s in SENSOR_NAMES}
    return baseline, status, []


# =============================================================================
# 7. EXCEL REPORT
# =============================================================================
@st.cache_data(show_spinner=False, max_entries=2)
def build_excel(cache_key: str, _sheets: dict) -> bytes:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, frame in _sheets.items():
            f = frame.copy()
            f.to_excel(xw, sheet_name=name, index=False, startrow=2)
            ws = xw.sheets[name]
            ws["A1"] = f"{APP_TITLE} – {APP_SUBTITLE}"
            ws["A1"].font = Font(bold=True, size=13)
            ws["A2"] = (f"{name} · Fixed common baseline {BASELINE_MV:.4f} mV · Valid band {MIN_MV:.0f}–{MAX_MV:.0f} mV · "
                        f"Generated {datetime.now(DISPLAY_TZ):%d %b %Y %H:%M} IST")
            ws["A2"].font = Font(italic=True, size=9)
            ws.freeze_panes = "A4"
            for cell in ws[3]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1F3A5F")
                cell.alignment = Alignment(wrap_text=True, vertical="center")
            for i, c in enumerate(f.columns, start=1):
                letter = get_column_letter(i)
                width = max([len(str(c))] + [len(str(v)) for v in f[c].head(200).tolist()])
                ws.column_dimensions[letter].width = min(max(width + 2, 10), 40)
                if pd.api.types.is_datetime64_any_dtype(f[c]):
                    fmt = "dd-mmm-yyyy" if c == "Date" else "dd-mmm-yyyy hh:mm:ss"
                    for cell in ws[letter][3:]:
                        cell.number_format = fmt
                elif pd.api.types.is_float_dtype(f[c]):
                    for cell in ws[letter][3:]:
                        cell.number_format = "0.000"
    return buf.getvalue()


# =============================================================================
# 8. CHART HELPERS
# =============================================================================
def _fmt(v, nd=2, unit=""):
    try:
        if v is None or pd.isna(v) or not math.isfinite(float(v)):
            return "—"
    except (TypeError, ValueError):
        return "—"
    return f"{v:,.{nd}f}{unit}"


def _ts(t) -> str:
    return "—" if t is None or pd.isna(t) else pd.Timestamp(t).strftime("%d %b %Y %H:%M:%S")


def line_figure(series_by_sensor, title, ytitle, unit, *, zero_line=False, band=False, markers=True,
                gl=False, height=430, x_title="Date", date_fmt="%d %b %Y"):
    fig = go.Figure()
    Trace = go.Scattergl if gl else go.Scatter
    for s, ser in series_by_sensor.items():
        ser = ser.astype(float)
        if ser.notna().sum() == 0:
            continue
        fig.add_trace(Trace(
            x=ser.index, y=ser.values, name=f"{s} ({PHYSICAL[s]})", mode="lines+markers" if markers else "lines",
            line=dict(color=COLOR[s], width=2), marker=dict(size=5), connectgaps=False,
            hovertemplate=f"<b>{s}</b> · {PHYSICAL[s]} · {EXPOSURE}<br>%{{x|{date_fmt}}}<br>%{{y:.3f}} {unit}<extra></extra>",
        ))
        last = ser.dropna()
        fig.add_trace(go.Scatter(
            x=[last.index[-1]], y=[last.iloc[-1]], mode="markers+text",
            marker=dict(size=12, color=COLOR[s], line=dict(width=2, color="white")),
            text=[f"{last.iloc[-1]:.2f}"], textposition="middle right",
            textfont=dict(color=COLOR[s], size=11), showlegend=False, hoverinfo="skip"))
    if zero_line:
        fig.add_hline(y=0, line_dash="dash", line_color="gray",
                      annotation_text="0 % (baseline)", annotation_position="bottom left")
    if band:
        fig.add_hline(y=MIN_MV, line_dash="dot", line_color="#B42318",
                      annotation_text=f"Min {MIN_MV:.0f} mV", annotation_position="bottom left")
        fig.add_hline(y=MAX_MV, line_dash="dot", line_color="#B42318",
                      annotation_text=f"Max {MAX_MV:.0f} mV", annotation_position="top left")
    fig.update_layout(
        title=dict(text=title, x=0, font=dict(size=16)), height=height,
        margin=dict(l=10, r=40, t=50, b=10), hovermode="closest",
        legend=dict(orientation="h", yanchor="bottom", y=-0.3, x=0),
        xaxis_title=x_title, yaxis_title=ytitle,
    )
    return fig


def plot(fig, key):
    st.plotly_chart(fig, width="stretch", key=key)


def downsample(df: pd.DataFrame, max_points: int = 4000):
    if len(df) <= max_points:
        return df, False
    span = df.index.max() - df.index.min()
    return df.resample(max(pd.Timedelta(seconds=1), span / max_points)).mean(), True


def daily_series(res, key, sensors, baseline=None):
    if key == "avg":
        return {s: res["daily_avg"][s] for s in sensors}
    deg = degradation(res["daily_avg"], baseline)
    return {s: deg[s] for s in sensors}


# =============================================================================
# 9. UI
# =============================================================================
CSS = """
<style>
:root {
  /* Jig 1 uses a distinct forest-teal identity; Jig 2 remains navy/blue. */
  --navy: #174B45;
  --blue: #0F9D8A;
  --ink: #18332F;
  --muted: #607A75;
  --canvas: #F1F8F6;
  --line: #D6E7E2;
}
.stApp { background: var(--canvas); color: var(--ink); }
.block-container { max-width: 1600px; padding-top: 1.35rem; padding-bottom: 2.5rem; }
h1, h2, h3 { color: var(--navy); letter-spacing: -0.02em; }
h1 { font-weight: 750; }
h2 { font-weight: 700; }
[data-testid="stSidebar"] {
  background: linear-gradient(180deg, #174B45 0%, #103A35 100%);
  border-right: 1px solid rgba(255,255,255,.12);
}
[data-testid="stSidebar"] * { color: #F8FAFC; }
[data-testid="stSidebar"] [data-testid="stRadio"] label {
  padding: .35rem .45rem; border-radius: .45rem;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label:hover {
  background: rgba(255,255,255,.09);
}
[data-testid="stSidebar"] hr { border-color: rgba(255,255,255,.18); }
.app-brand { color:#FFFFFF; font-size:1.08rem; font-weight:800;
  letter-spacing:.02em; margin:.15rem 0 .1rem; }
.app-brand-sub { color:#CBD5E1; font-size:.72rem; letter-spacing:.12em;
  text-transform:uppercase; margin:0 0 1.1rem; }
.app-title { font-size:1.75rem; line-height:1.2; font-weight:800;
  letter-spacing:-.035em; color:var(--navy); margin:0; }
.app-sub { color:var(--muted); font-size:.92rem; font-weight:600; margin:.35rem 0 .2rem; }
.app-note { color:var(--muted); font-size:.82rem; margin-bottom:.8rem; }
.status-bar { display:flex; flex-wrap:wrap; gap:.55rem 1.25rem; align-items:center;
  padding:.8rem 1rem; background:#FFFFFF; border:1px solid var(--line);
  border-radius:.75rem; margin-bottom:1rem; box-shadow:0 1px 2px rgba(15,23,42,.035); }
.badge { font-weight:800; padding:.28rem .7rem; border-radius:999px;
  color:#fff; font-size:.72rem; letter-spacing:.07em; }
.badge-live { background:#15803D; } .badge-err { background:#B91C1C; }
.dot { display:inline-block; width:.5rem; height:.5rem; border-radius:50%;
  background:#fff; margin-right:.35rem; animation:pulse 1.6s infinite; }
@keyframes pulse { 0%{opacity:1} 50%{opacity:.25} 100%{opacity:1} }
@media (prefers-reduced-motion: reduce) {.dot{animation:none}}
.si { font-size:.8rem; color:var(--ink); } .si .k { color:var(--muted); margin-right:.3rem; }
.chip { display:inline-block; font-weight:750; font-size:.72rem;
  padding:.18rem .58rem; border-radius:999px; }
.chip-ok { background:#DCFCE7; color:#166534; }
.chip-low { background:#FEF3C7; color:#92400E; }
.chip-high { background:#FEE2E2; color:#991B1B; }
.chip-na { background:#E5E7EB; color:#374151; }
div[data-testid="stMetric"] { background:#FFFFFF; border:1px solid var(--line);
  border-radius:.75rem; padding:.8rem 1rem; box-shadow:0 1px 2px rgba(15,23,42,.035); }
div[data-testid="stMetricLabel"] { color:var(--muted); font-weight:600; }
div[data-testid="stMetricValue"] { font-size:1.55rem; color:var(--navy); }
div[data-testid="stVerticalBlockBorderWrapper"] {
  border-radius:.75rem; border-color:var(--line); background:#FFFFFF;
}
.stButton button, .stDownloadButton button { border-radius:.55rem; font-weight:650; }
.stButton button[kind="primary"], .stDownloadButton button[kind="primary"] {
  background:#0F9D8A; border-color:#0F9D8A; color:#FFFFFF;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) {
  background:rgba(45, 212, 191, .18); border-left:3px solid #5EEAD4;
}
.stTabs [data-baseweb="tab-list"] { gap:.35rem; }
.stTabs [data-baseweb="tab"] { border-radius:.5rem .5rem 0 0; }
[data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:.65rem; overflow:hidden; }
hr { border-color:var(--line); }
</style>
"""
CHIP = {"Within Range": "chip-ok", "Below Limit": "chip-low", "Above Limit": "chip-high", "No Data": "chip-na"}

PAGES = [
    "01  Live Overview",
    "02  Live Sensor Monitoring",
    "03  Daily Sensor Output",
    "04  Degradation Analysis",
    "05  Baseline Summary",
    "06  Temperature Monitoring",
    "07  Data & Validation",
]

NUM3 = st.column_config.NumberColumn(format="%.3f")
PCT = st.column_config.NumberColumn(format="%.2f %%")
DATEC = st.column_config.DateColumn(format="DD MMM YYYY")
DTC = st.column_config.DatetimeColumn(format="DD MMM YYYY HH:mm:ss")


def load_data():
    store = shared_state()
    status = {"ok": False, "error": None, "fetched_at": None}
    try:
        values, fetched_at = fetch_sheet_values()
        res = process_cached(fetched_at, values)
        with store["lock"]:
            store["last_good"] = (res, fetched_at)
        status.update(ok=True, fetched_at=fetched_at)
        return res, status
    except Exception as exc:  # noqa: BLE001
        status["error"] = friendly_error(exc)
        with store["lock"]:
            last = store["last_good"]
        if last is None:
            last = st.session_state.get("last_good")
        if last is not None:
            status["fetched_at"] = last[1]
            return last[0], status
        return None, status


def track_new_readings(res, fetched_at):
    ss = st.session_state
    if ss.get("seen_fetch") != fetched_at:
        prev = ss.get("seen_records")
        ss["new_last"] = max(0, res["records"] - prev) if prev is not None else 0
        ss["new_session"] = ss.get("new_session", 0) + ss["new_last"]
        ss["seen_records"], ss["seen_fetch"] = res["records"], fetched_at
    return ss.get("new_last", 0), ss.get("new_session", 0)


def status_bar(res, status, new_last, new_session):
    fetched = (datetime.fromisoformat(status["fetched_at"]).astimezone(DISPLAY_TZ).strftime("%d %b %Y %H:%M:%S")
               if status["fetched_at"] else "—")
    badge = ('<span class="badge badge-live"><span class="dot"></span>LIVE</span>' if status["ok"]
             else '<span class="badge badge-err">CONNECTION ERROR</span>')
    latest = _ts(res["latest_ts"]) if res else "—"
    day = ""
    if res and pd.notna(res["latest_ts"]):
        d = min((res["latest_ts"].date() - STUDY_START).days, STUDY_DAYS - 1)
        day = f'<span class="si"><span class="k">Study day</span><b>{d} of {STUDY_DAYS - 1}</b></span>'
    excl = f'{res["total_excluded"]:,}' if res else "—"
    st.markdown(
        f'<div class="status-bar">{badge}'
        f'<span class="si"><span class="k">Last successful refresh</span><b>{fetched}</b></span>'
        f'<span class="si"><span class="k">Latest timestamp</span><b>{latest}</b></span>'
        f'<span class="si"><span class="k">New readings (last refresh)</span><b>+{new_last}</b>'
        f'<span class="k" style="margin-left:.3rem">· {new_session} this session</span></span>'
        f'<span class="si"><span class="k">Excluded readings</span><b>{excl}</b></span>{day}</div>',
        unsafe_allow_html=True)


def baseline_messages(res, bstatus, drift):
    st.info(f"All eight sensors use the same fixed baseline: **{BASELINE_MV:.4f} mV**.")


def page_overview(res, baseline, bstatus):
    lt = build_latest_table(res, baseline, bstatus).set_index("Sensor")
    st.caption("Dead-sensor detection uses raw readings only: below 69 mV starts a 1,000-reading recovery window; reaching 70 mV starts a further 1,000-reading confirmation window. Any reading below 70 mV during confirmation declares DEAD SENSOR.")
    now_local = datetime.now(DISPLAY_TZ).replace(tzinfo=None)
    if pd.notna(res["latest_ts"]) and now_local.date() <= STUDY_END:
        age = (now_local - res["latest_ts"]).total_seconds() / 60
        if age > STALE_AFTER_MINUTES:
            st.warning(f"No new readings for **{age:,.0f} min** (latest timestamp {_ts(res['latest_ts'])}). "
                       "Check the data logger.")

    health_states = [res["dead_sensor"][s]["status"] for s in SENSOR_NAMES]
    dead_count = health_states.count("DEAD SENSOR")
    check_count = sum(x in ("RECOVERY CHECK", "NORMAL CHECK") for x in health_states)
    normal_count = health_states.count("NORMAL")
    st.subheader("System health")
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Sensors normal", f"{normal_count}/8")
    h2.metric("Dead sensor alerts", dead_count,
              delta="Requires investigation" if dead_count else "No alerts",
              delta_color="inverse")
    h3.metric("Verification in progress", check_count)
    h4.metric("Latest chamber temperature",
              _fmt(res["latest_temp"][0], 1, " °C") if res["has_temp"] else "Not logged")
    st.divider()

    for chunk in (SENSOR_NAMES[:4], SENSOR_NAMES[4:]):
        cols = st.columns(4)
        for c, s in zip(cols, chunk):
            row, L = lt.loc[s], res["latest"][s]
            with c, st.container(border=True):
                deg = row["Degradation (%)"]
                st.metric(f"{s} · {PHYSICAL[s]}", _fmt(L["raw_value"], 2, " mV"),
                          delta=None if pd.isna(deg) else f"{deg:+.2f} % degradation", delta_color="inverse",
                          help="Latest reading from the sheet. Degradation = latest daily average of in-band "
                               "readings vs the fixed common baseline.")
                health = res["dead_sensor"][s]
                health_label = health["status"]
                health_class = ("chip-high" if health_label == "DEAD SENSOR" else
                                "chip-low" if health_label in ("RECOVERY CHECK", "NORMAL CHECK") else
                                "chip-na" if health_label == "NO DATA" else "chip-ok")
                st.markdown(f'<span class="chip {health_class}">{health_label}</span>', unsafe_allow_html=True)
                if health_label in ("RECOVERY CHECK", "NORMAL CHECK"):
                    st.caption(f"Verification progress: {health['progress']}")
                st.caption(f"Limits {MIN_MV:.0f}–{MAX_MV:.0f} mV · base {_fmt(baseline[s], 2, ' mV')}  \n"
                           f"Day avg {_fmt(row['Latest Daily Average (mV)'], 2, ' mV')} · {_ts(L['raw_ts'])}")

    st.subheader("Study and data summary")
    k = st.columns(4)
    k[0].metric("Study start", STUDY_START.strftime("%d %b %Y"), border=True)
    k[1].metric("Study end", STUDY_END.strftime("%d %b %Y"), border=True)
    k[2].metric("Study duration", f"{STUDY_DAYS} days", border=True)
    k[3].metric("Latest timestamp", _ts(res["latest_ts"]), border=True)
    k = st.columns(4)
    k[0].metric("Latest temperature", _fmt(res["latest_temp"][0], 1, " °C") if res["has_temp"] else "Not logged",
                border=True, help=None if res["has_temp"] else "The Jig 1 sheet has no temperature column.")
    k[1].metric("Total raw readings", f"{res['total_readings']:,}", border=True,
                help=f"Numeric sensor readings in the study window ({res['records']:,} timestamps × 8 sensors).")
    k[2].metric("Total valid readings", f"{res['total_valid']:,}", border=True)
    k[3].metric("Total excluded readings", f"{res['total_excluded']:,}", border=True,
                help=f"{res['total_below']:,} below {MIN_MV:.0f} mV · {res['total_above']:,} above {MAX_MV:.0f} mV")
    d = min(max((res["latest_ts"].date() - STUDY_START).days + 1, 0), STUDY_DAYS) if pd.notna(res["latest_ts"]) else 0
    st.progress(d / STUDY_DAYS, text=f"Study progress: {d} of {STUDY_DAYS} days (Day 0 = 21 Oct, Day 51 = 11 Dec)")


def page_live(res, baseline):
    windows = {"Last 1 hour": 1, "Last 6 hours": 6, "Last 24 hours": 24, "Last 3 days": 72,
               "Last 7 days": 168, "Entire study": None}
    c1, c2 = st.columns([2, 3])
    with c1:
        choice = st.segmented_control("Time window", list(windows), default="Last 24 hours", key="live_window") \
            or "Last 24 hours"
    with c2:
        sel = st.multiselect("Sensors", SENSOR_NAMES, default=SENSOR_NAMES, key="live_sensors") or SENSOR_NAMES
    show_raw = st.toggle("Show raw readings (including out-of-band values)", key="live_raw",
                         help="Off: only readings within 50–110 mV, as used in every calculation.")
    d = res["data"].set_index("Timestamp")
    src = (d[SENSOR_NAMES] if show_raw else res["filtered"].set_index(d.index))
    if len(src) and windows[choice]:
        src = src[src.index >= src.index.max() - pd.Timedelta(hours=windows[choice])]
    ds, binned = downsample(src)
    if binned:
        st.caption("Long window: display is time-binned for speed. Calculations use every reading.")
    fmt = "%d %b %H:%M:%S"
    plot(line_figure({s: ds[s] for s in sel}, "Live sensor output", "Sensor output (mV)", "mV",
                     band=True, markers=False, gl=True, x_title="Timestamp", date_fmt=fmt), "live_all")
    st.subheader("Individual sensors")
    one = st.selectbox("Sensor", SENSOR_NAMES, key="live_one")
    plot(line_figure({one: ds[one]}, f"{one} – live output", "Sensor output (mV)", "mV", band=True,
                     markers=False, gl=True, x_title="Timestamp", date_fmt=fmt, height=360), "live_one_fig")
    live_deg = degradation(ds[SENSOR_NAMES].where(ds[SENSOR_NAMES].ge(MIN_MV) & ds[SENSOR_NAMES].le(MAX_MV)), baseline)
    plot(line_figure({s: live_deg[s] for s in sel}, "Live degradation – in-band readings vs fixed common baseline",
                     "Degradation (%)", "%", zero_line=True, markers=False, gl=True, x_title="Timestamp",
                     date_fmt=fmt), "live_deg")
    st.caption("Reports and cards use daily-average degradation; this chart applies the same formula to individual readings.")


def page_daily(res):
    st.caption(f"Daily average of readings within {MIN_MV:.0f}–{MAX_MV:.0f} mV. Gaps = days without valid readings.")
    plot(line_figure(daily_series(res, "avg", SENSOR_NAMES), "Daily average output – all 8 sensors",
                     "Daily Average Output (mV)", "mV", band=True), "daily_all")
    one = st.selectbox("Individual sensor", SENSOR_NAMES, key="daily_one")
    plot(line_figure(daily_series(res, "avg", [one]), f"{one} – daily average output",
                     "Daily Average Output (mV)", "mV", band=True, height=360), "daily_one_fig")


def page_degradation(res, baseline):
    st.caption("Degradation (%) = ((Baseline − Daily Average) / Baseline) × 100. Positive = output fell; negative = output rose.")
    plot(line_figure(daily_series(res, "deg", SENSOR_NAMES, baseline), "Degradation – all 8 sensors",
                     "Degradation (%)", "%", zero_line=True), "deg_all")
    one = st.selectbox("Individual sensor", SENSOR_NAMES, key="deg_one")
    plot(line_figure(daily_series(res, "deg", [one], baseline), f"{one} – degradation", "Degradation (%)", "%",
                     zero_line=True, height=360), "deg_one_fig")


def baseline_frame(res, baseline, bstatus):
    store = shared_state()
    return pd.DataFrame({
        "Sensor": SENSOR_NAMES, "Physical ID": [PHYSICAL[s] for s in SENSOR_NAMES], "Exposure": EXPOSURE, "Sheet Column": [COL_LETTER[s] for s in SENSOR_NAMES],
        "Sheet Header": [res["sensor_headers"][s] for s in SENSOR_NAMES],
        "Baseline Date": "Fixed common baseline",
        "Baseline Output (mV)": [baseline[s] for s in SENSOR_NAMES],
        "Minimum Limit (mV)": MIN_MV, "Maximum Limit (mV)": MAX_MV,
        "Valid Readings Used": "—",
        "Excluded on baseline date": "—",
        "Status": [bstatus[s] for s in SENSOR_NAMES],
        "Locked At": "—",
    })


def page_baseline(res, baseline, bstatus):
    b = baseline_frame(res, baseline, bstatus)
    st.dataframe(b[["Sensor", "Physical ID", "Exposure", "Baseline Date", "Baseline Output (mV)", "Minimum Limit (mV)",
                    "Maximum Limit (mV)", "Valid Readings Used", "Excluded on baseline date", "Status", "Locked At",
                    "Sheet Column", "Sheet Header"]],
                 hide_index=True, width="stretch", column_config={"Baseline Output (mV)": NUM3})
    st.info(f"**Fixed common baseline:** {BASELINE_MV:.4f} mV is applied equally to S-1 through S-8. It is not recalculated from daily readings.")


def page_temperature(res):
    if not res["has_temp"]:
        st.info("The Jig 1 sheet does not contain a temperature column, so there is nothing to show here. "
                "If a column with “Temp” in its header is added, it will appear automatically.")
        return
    t, td = res["daily_temp"], res["data"]["Temperature (°C)"].dropna()
    c = st.columns(4)
    c[0].metric("Current", _fmt(res["latest_temp"][0], 1, " °C"), border=True)
    c[1].metric("Study mean", _fmt(td.mean(), 2, " °C"), border=True)
    c[2].metric("Study min", _fmt(td.min(), 1, " °C"), border=True)
    c[3].metric("Study max", _fmt(td.max(), 1, " °C"), border=True)
    raw = res["data"].set_index("Timestamp")["Temperature (°C)"]
    rds, _ = downsample(raw.to_frame())
    fig = go.Figure(go.Scattergl(x=rds.index, y=rds.iloc[:, 0], mode="lines", line=dict(color="#C2410C", width=2),
                                 hovertemplate="%{x|%d %b %H:%M:%S}<br>%{y:.2f} °C<extra></extra>"))
    fig.update_layout(title=dict(text="Temperature vs time", x=0), height=360, margin=dict(l=10, r=40, t=50, b=10),
                      xaxis_title="Timestamp", yaxis_title="Temperature (°C)", showlegend=False)
    plot(fig, "temp_live")
    fig = go.Figure(go.Scatter(x=t.index, y=t.values, mode="lines+markers", line=dict(color="#C2410C", width=2),
                               connectgaps=False, hovertemplate="%{x|%d %b %Y}<br>%{y:.2f} °C<extra></extra>"))
    fig.update_layout(title=dict(text="Daily average temperature", x=0), height=360, margin=dict(l=10, r=40, t=50, b=10),
                      xaxis_title="Date", yaxis_title="Temperature (°C)", showlegend=False)
    plot(fig, "temp_daily")


def page_data(res, baseline, raw_all, filt_all):
    last = res["latest_ts"].date() if pd.notna(res["latest_ts"]) else STUDY_START
    c1, c2 = st.columns([1, 2])
    with c1:
        rng = st.date_input("Date range", value=(STUDY_START, min(last, STUDY_END)), min_value=STUDY_START,
                            max_value=STUDY_END, format="DD/MM/YYYY", key="val_range")
    with c2:
        sel = st.multiselect("Sensors", SENSOR_NAMES, default=SENSOR_NAMES, key="val_sensors") or SENSOR_NAMES
    if not isinstance(rng, (tuple, list)) or len(rng) != 2:
        st.info("Select a start and end date.")
        return
    d0, d1 = pd.Timestamp(rng[0]), pd.Timestamp(rng[1])
    mask = (res["data"]["Date"] >= d0) & (res["data"]["Date"] <= d1)
    ex = exclusion_summary(res, mask, sel)
    k = st.columns(4)
    k[0].metric("Valid readings", f"{int(ex['Valid Readings'].sum()):,}", border=True)
    k[1].metric("Below 50 mV", f"{int(ex['Below 50 mV'].sum()):,}", border=True)
    k[2].metric("Above 110 mV", f"{int(ex['Above 110 mV'].sum()):,}", border=True)
    k[3].metric("Missing / invalid", f"{int(ex['Missing/Invalid'].sum()):,}", border=True)

    st.subheader("Dead-sensor verification (raw readings only)")
    dead_rows = pd.DataFrame([{"Sensor": sensor, "Physical ID": PHYSICAL[sensor],
        "Status": res["dead_sensor"][sensor]["status"],
        "Verification Progress": res["dead_sensor"][sensor]["progress"],
        "Low-Reading Trigger Time": res["dead_sensor"][sensor]["trigger_ts"],
        "Fault Time": res["dead_sensor"][sensor]["fault_ts"]} for sensor in sel])
    st.dataframe(dead_rows, hide_index=True, width="stretch",
                 column_config={"Low-Reading Trigger Time": DTC, "Fault Time": DTC})
    st.caption("Detection uses unfiltered sensor values. A value below 69 mV starts the first 1,000-reading recovery window. Reaching 70 mV starts a second 1,000-reading confirmation window; any value below 70 mV in that second window declares DEAD SENSOR.")

    st.subheader("Valid and excluded reading counts")
    st.dataframe(ex, hide_index=True, width="stretch",
                 column_config={"Excluded (%)": PCT, "Min Raw (mV)": NUM3, "Max Raw (mV)": NUM3})

    wide = build_daily_wide(res, baseline)
    dw = build_degradation_wide(res, baseline)
    dmask = (wide["Date"] >= d0) & (wide["Date"] <= d1)
    keep = ["Date", "Study Day"] + (["Daily Avg Temp (°C)"] if res["has_temp"] else [])
    st.subheader("Daily results – date-wise sensor output")
    st.dataframe(wide.loc[dmask, keep + [f"{s} Daily Avg (mV)" for s in sel]], hide_index=True, width="stretch",
                 column_config={"Date": DATEC, **{f"{s} Daily Avg (mV)": NUM3 for s in SENSOR_NAMES}})
    st.subheader("Date-wise degradation")
    st.dataframe(dw.loc[dmask, ["Date", "Study Day"] + [f"{s} Degradation (%)" for s in sel]], hide_index=True,
                 width="stretch", column_config={"Date": DATEC, **{f"{s} Degradation (%)": PCT for s in SENSOR_NAMES}})
    long = build_daily_long(res, baseline)
    long = long[(long["Date"] >= d0) & (long["Date"] <= d1) & long["Sensor"].isin(sel)]
    st.download_button("⬇️ Daily results (CSV)", long.to_csv(index=False).encode(), "jig1_daily_results.csv",
                       "text/csv", key="dl_daily")

    rmask = (raw_all["Timestamp"] >= d0) & (raw_all["Timestamp"] < d1 + pd.Timedelta(days=1))
    base_cols = ["Timestamp", "Study Day"] + (["Temperature (°C)"] if res["has_temp"] else [])
    scol = [f"{s} (mV)" for s in sel]
    st.subheader("Raw sensor readings")
    rv = raw_all.loc[rmask, base_cols + scol + ["Sheet Row"]].iloc[::-1]
    st.dataframe(rv, hide_index=True, width="stretch", height=360, column_config={"Timestamp": DTC})
    st.download_button("⬇️ Raw readings (CSV)", rv.to_csv(index=False).encode(), "jig1_raw_readings.csv",
                       "text/csv", key="dl_raw")
    st.subheader("Filtered sensor readings")
    st.caption(f"Readings outside {MIN_MV:.0f}–{MAX_MV:.0f} mV are blanked for that sensor only; the rest of the row is kept.")
    fv = filt_all.loc[rmask, base_cols + scol + ["Excluded Sensors", "Sheet Row"]].iloc[::-1]
    st.dataframe(fv, hide_index=True, width="stretch", height=360, column_config={"Timestamp": DTC})
    st.download_button("⬇️ Filtered readings (CSV)", fv.to_csv(index=False).encode(), "jig1_filtered_readings.csv",
                       "text/csv", key="dl_filt")


def main():
    st.set_page_config(page_title=f"{APP_TITLE} – {APP_SUBTITLE}", page_icon="🫁", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=REFRESH_SECONDS * 1000, key="live_autorefresh")

    with st.sidebar:
        st.markdown('<div class="app-brand">JIG 1 MONITOR</div>'
                    '<div class="app-brand-sub">100% O₂ Sensor Test System</div>',
                    unsafe_allow_html=True)
        if st.button("↻  Refresh Now", width="stretch", type="primary"):
            fetch_sheet_values.clear()
        st.caption(f"Auto-refresh every {REFRESH_SECONDS} seconds.")
        st.caption("STUDY PROTOCOL")
        st.caption(f"Baseline: {BASELINE_MV:.4f} mV")
        st.caption(f"Valid range: {MIN_MV:.0f}–{MAX_MV:.0f} mV")
        nav_slot = st.container()

    res, status = load_data()
    pages = [p for p in PAGES if p != PAGES[5] or (res is not None and res["has_temp"])]
    with nav_slot:
        st.divider()
        page = st.radio("Navigation", pages, key="jig1_page_professional", label_visibility="collapsed")
    st.markdown(f'<p class="app-title">Jig 1 | Sensor Performance Dashboard</p>'
                f'<p class="app-sub">100% O₂ sensor aging, live output and degradation monitoring</p>'
                f'<p class="app-note">Study: 21 Oct – 11 Dec 2026 · Fixed baseline: {BASELINE_MV:.4f} mV · '
                f'Valid range: {MIN_MV:.0f}–{MAX_MV:.0f} mV · Time zone: {DISPLAY_TZ.key}</p>',
                unsafe_allow_html=True)

    if res is None:
        status_bar(None, status, 0, 0)
        st.error(f"**Could not load Jig 1 data.** {status['error']}")
        st.info("The dashboard retries automatically every minute.")
        return

    st.session_state["last_good"] = (res, status["fetched_at"])
    new_last, new_session = track_new_readings(res, status["fetched_at"])
    status_bar(res, status, new_last, new_session)
    if not status["ok"]:
        st.error(f"**Connection error – showing last successfully loaded data.** {status['error']}")
    if res["records"] == 0:
        st.error(f"No readings with valid timestamps between 21 Oct 2026 and 11 Dec 2026 were found in '{WORKSHEET_NAME}'.")
        return

    baseline, bstatus, drift = resolve_baseline(res)
    baseline_messages(res, bstatus, drift)
    raw_all, filt_all = raw_tables_cached(status["fetched_at"], res)

    bsig = "|".join(f"{baseline[s]:.9g}" for s in SENSOR_NAMES)
    sheets = {
        "Daily Results": build_daily_wide(res, baseline),
        "Baseline Summary": baseline_frame(res, baseline, bstatus),
        "Latest Sensor Comparison": build_latest_table(res, baseline, bstatus),
        "Dead Sensor Status": pd.DataFrame([{"Sensor": sensor, "Physical ID": PHYSICAL[sensor],
            "Status": res["dead_sensor"][sensor]["status"],
            "Verification Progress": res["dead_sensor"][sensor]["progress"],
            "Trigger Time": res["dead_sensor"][sensor]["trigger_ts"],
            "Fault Time": res["dead_sensor"][sensor]["fault_ts"]} for sensor in SENSOR_NAMES]),
        "Threshold Exclusion Summary": exclusion_summary(res),
        "Filtered Raw Data": filt_all,
        "Degradation Results": build_degradation_wide(res, baseline),
    }
    cache_key = f"{status['fetched_at']}|{bsig}"
    with st.sidebar:
        st.divider()
        # Built only when clicked (the full raw-data sheet takes several seconds to write).
        st.download_button("⬇️ Download Excel report", lambda: build_excel(cache_key, sheets),
                           file_name=f"Jig1_O2_Sensor_Environmental_Chamber_{datetime.now(DISPLAY_TZ):%Y%m%d_%H%M}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           width="stretch", key="dl_excel")
        with st.expander("Sensors & limits"):
            st.dataframe(pd.DataFrame({"Sensor": SENSOR_NAMES, "Physical": [PHYSICAL[s] for s in SENSOR_NAMES],
                                       "Col": [COL_LETTER[s] for s in SENSOR_NAMES],
                                       "Sheet header": [res["sensor_headers"][s] for s in SENSOR_NAMES]}),
                         hide_index=True)
            st.caption(f"All 100% O₂ · valid {MIN_MV:.0f}–{MAX_MV:.0f} mV inclusive")

    st.header(page.split("  ", 1)[1])
    if page == PAGES[0]:
        page_overview(res, baseline, bstatus)
    elif page == PAGES[1]:
        page_live(res, baseline)
    elif page == PAGES[2]:
        page_daily(res)
    elif page == PAGES[3]:
        page_degradation(res, baseline)
    elif page == PAGES[4]:
        page_baseline(res, baseline, bstatus)
    elif page == PAGES[5]:
        page_temperature(res)
    elif page == PAGES[6]:
        page_data(res, baseline, raw_all, filt_all)


if __name__ == "__main__":
    main()
