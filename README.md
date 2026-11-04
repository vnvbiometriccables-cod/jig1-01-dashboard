# O₂ SENSOR IN ENVIRONMENTAL CHAMBER
### Jig 1 – 100% O₂ Sensor Aging and Degradation Monitoring

A live Streamlit dashboard for the eight 100% O₂ sensors in Jig 1. It reads the
Jig 1 Google Sheet every 60 seconds and filters readings to the 50–110 mV band.
It holds the 21 Oct 2026 baseline fixed and reports daily output and degradation.
It is a separate app from the Jig 2 dashboard, and the two never share data.

| Item | Value |
|---|---|
| Data source | Google Sheet `1cvA7pU8m-s6F3ktVxEK2v-Mw8ZpRMAot4uEciEtz9Sk`, tab **Sheet1** |
| Sheet title | O2 Sensor in ENV chamber with 100%Oxygen |
| Study window | 21 Oct 2026 (Day 0) → 11 Dec 2026 (Day 51), 52 days inclusive |
| Valid band | 50 mV ≤ reading ≤ 110 mV for every sensor (both limits accepted) |
| Baseline | Mean of in-band readings dated 21 Oct 2026 only |
| Refresh | Every 60 s while the tab is open, plus **Refresh Now** |

## Why the dashboard reads the sheet, not the Apps Script URL

The Apps Script Web App
`https://script.google.com/macros/s/AKfycbz6mGzgzj4ihcK0QxntqSUelrNOJHMGtgUqWWe7a244han2Uq1vbu880TeNZqJW1pRPyQ/exec`
was inspected on 3 Nov 2026. It follows Google's redirect and returns only the
plain text *"ESP8266 Google Sheets Logger is running."* This is the endpoint the
ESP8266 logger uses to **write** readings into the sheet. It serves no readings
back. So the dashboard reads the sheet the logger writes to, with the same
Google service account as the Jig 2 dashboard.

If you later change the Apps Script so that `doGet` returns the readings as JSON
or CSV, the app can be switched to it. Ask for that change; the URL is kept in
`app.py` as `APPS_SCRIPT_URL` for reference.

## Sheet layout and sensor mapping

| Sheet column | Header in the sheet | Dashboard sensor | Physical ID |
|---|---|---|---|
| A | Timestamp (month/day/year) | – | – |
| B | Temperature (°C) | Temperature | – |
| C | With o2 S1 (mV) | S-1 | S-20-23% |
| D | With o2 S2 (mV) | S-2 | S-14-23% |
| E | With o2 S3 (mV) | S-3 | S-19-23% |
| F | With o2 S4 (mV) | S-4 | S-09-23% |
| G | With o2 S5 (mV) | S-5 | S-15-23% |
| H | With o2 S6 (mV) | S-6 | S-05-23% |
| I | With o2 S7 (mV) | S-7 | S-01-23% |
| J | With o2 S8 (mV) | S-8 | S-11-23% |

All eight sensors are exposed to 100% O₂.

* Sensors are read **by column position**, so renaming a header doesn't break anything.
  Moving or inserting columns would.
* Temperature is found from the header containing "Temp". If that column is ever
  removed, the temperature views hide themselves without errors.

## Calculations

1. **Study window.** Only rows timestamped 21 Oct 2026 00:00 to 11 Dec 2026 23:59:59
   are used. Rows with unreadable timestamps are counted and skipped.
2. **Band filter (per reading).** A reading below 50 or above 110 is excluded for
   that sensor only, and the rest of the row is kept. Exactly 50 and exactly 110 are
   accepted. Blank or non-numeric cells count as missing and never enter an average.
   Below-limit and above-limit exclusions are counted separately.
3. **Daily average.** The mean of valid readings per sensor per day. It is left blank
   when a sensor has no valid reading that day.
4. **Baseline.** The 21 Oct 2026 daily average. No other date is used. A sensor with
   no in-band readings on 21 Oct shows **Unavailable**, and degradation is not calculated for it.
5. **Degradation (%).** ((Baseline − Daily average) / Baseline) × 100. Positive means
   the output fell. Day 0 is 0 % by construction.
6. **Sensor status** uses the latest reading:
   * **Within Range:** 50–110 mV.
   * **Below Limit:** under 50 mV.
   * **Above Limit:** over 110 mV.
   * **No Data:** no current reading.

### How the baseline stays fixed

* Once readings after 21 Oct exist, each sensor's baseline is **locked**. It's held
  in a server-wide store shared by all viewers, and refreshes never recompute it.
* Every refresh re-checks the 21 Oct rows. If they've been edited, the locked value
  is kept and a **Baseline protection** warning names the sensors.
* If the app restarts (a redeploy, or Streamlit Cloud sleeping), the baseline is
  rebuilt from the same 21 Oct rows. It is identical unless those rows were changed.
  The Baseline Summary page shows each value, the readings used, and when it was locked.

## Pages

| Page | Contents |
|---|---|
| 🟢 Live Overview | One card per sensor with the latest reading, degradation, limits and status. Study dates, latest timestamp, and raw/valid/excluded totals. |
| 📈 Live Sensor Monitoring | Live output for selected sensors with 50 and 110 mV reference lines and an option to show raw out-of-band values. Also an individual sensor chart and live degradation. |
| 📊 Daily Sensor Output | Daily averages for all sensors plus one selected sensor, with reference lines. |
| 📉 Degradation Analysis | Degradation for all sensors and one selected sensor, with a 0 % line. |
| 🧮 Baseline Summary | Sensor, exposure, baseline date and value, limits, readings used, status and lock time. |
| 🌡️ Temperature Monitoring | Live temperature, temperature vs time, daily average temperature and study min/mean/max (from column B). |
| 🗂️ Data & Validation | Date-range and sensor filters. Valid and excluded counts, daily output and degradation by date, raw and filtered readings, and CSV downloads. |

The status bar shows several things:
* **LIVE** or **CONNECTION ERROR**
* the last successful refresh and the latest timestamp
* new readings since the last refresh, and excluded readings

If the sheet can't be reached, the last good data stays on screen with an error,
and the baseline is never reset.

**Excel report** (sidebar). It is built when you click, which takes a few seconds,
and contains six sheets: Daily Results, Baseline Summary, Latest Sensor Comparison,
Threshold Exclusion Summary, Filtered Raw Data and Degradation Results.

---

## Setup

You already have a working Google service account from the Jig 2 dashboard.
Jig 1 reuses it.

### 1. Share the Jig 1 sheet

Open **O2 Sensor in ENV chamber with 100%Oxygen** → **Share** →
add `jig2-dashboard@jig2-dashboard.iam.gserviceaccount.com` as **Viewer** → untick
"Notify people" → **Share**.

### 2. Put the project on GitHub

Create a **new private repository**, for example `jig1-o2-dashboard`. Keep it
separate from the Jig 2 repo. Upload:

* `app.py`, `requirements.txt`, `README.md`, `.gitignore`
* `.streamlit/config.toml`. In GitHub's web upload, drag the whole `.streamlit`
  folder, or use **Add file → Create new file** and type `.streamlit/config.toml`
  as the name.

Never upload `secrets.toml`.

### 3. Deploy on Streamlit Community Cloud (free)

1. Go to <https://share.streamlit.io> → **Create app** → **Deploy a public app from GitHub**.
2. Repository: paste the repo URL from your browser's address bar. Branch: `main`.
   Main file: `app.py`.
3. **Advanced settings:**
   * Python **3.11**.
   * In **Secrets**, paste the same contents as the Jig 2 app's secrets.
   * Change the last line to `timestamp_order = "monthfirst"`.
4. **Deploy.**

### Run locally (optional)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # fill in real values
streamlit run app.py
```

## Troubleshooting

| Message | Fix |
|---|---|
| `Jig 1 spreadsheet not found or not shared` | Do step 1. |
| `Worksheet 'Sheet1' was not found` | The data tab must be named exactly `Sheet1`. |
| `No secrets found` / `[gcp_service_account] is missing` | Paste the secrets (step 3.3). |
| `Baseline unavailable for S-x` | That sensor had no readings between 50 and 110 mV on 21 Oct 2026. |
| Occasional "Above Limit" readings | Spikes over 110 mV are excluded by design. Data & Validation shows the counts per sensor. |
| Dates a month off | Set `timestamp_order = "monthfirst"` (or `"dayfirst"`) in `[app_settings]`. |
| Apps Script URL shows only "Logger is running" | Expected: it's the logger's write endpoint and has no data to read. |
