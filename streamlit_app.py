# streamlit_app.py — Complete Daily Spend Tracker (Google Sheets + Charts + Add Row fix)
import streamlit as st
import pandas as pd
import importlib
from datetime import datetime, timedelta, date, time as dt_time
import altair as alt

st.set_page_config(page_title="💳 Daily Spend Tracker", layout="wide")
st.title("💳 Daily Spending")

# ------------------ Module Imports ------------------
try:
    transform = importlib.import_module("transform")
except Exception as e:
    st.error("❌ Missing or broken transform.py.")
    st.exception(e)
    st.stop()

try:
    import io_helpers as io_mod
except Exception:
    io_mod = None

try:
    charts_mod = importlib.import_module("charts")
except Exception:
    charts_mod = None

# ------------------ Load from Streamlit Secrets ------------------
_secrets = getattr(st, "secrets", {}) or {}
SHEET_ID = _secrets.get("SHEET_ID")
RANGE = _secrets.get("RANGE")
APPEND_RANGE = _secrets.get("APPEND_RANGE")

if not SHEET_ID or not RANGE or not APPEND_RANGE:
    st.error("Missing Google Sheet secrets: SHEET_ID, RANGE, APPEND_RANGE.")
    st.stop()

# ------------------ Session State ------------------
if "reload_key" not in st.session_state:
    st.session_state.reload_key = 0

if "last_refreshed" not in st.session_state:
    st.session_state.last_refreshed = None

# ------------------ Helpers ------------------
def _get_creds_info():
    if io_mod is None:
        return None
    try:
        if "gcp_service_account" in st.secrets:
            return io_mod.parse_service_account_secret(st.secrets["gcp_service_account"])
    except Exception:
        return None
    return None


def _read_sheet_with_index(spreadsheet_id, range_name, source_name, creds_info):
    try:
        df = io_mod.read_google_sheet(spreadsheet_id, range_name, creds_info=creds_info)
    except Exception as e:
        st.error(f"Failed to read sheet '{range_name}': {e}")
        return pd.DataFrame()
    df = df.reset_index(drop=True)
    if not df.empty:
        df["_sheet_row_idx"] = df.index.astype(int)
    df["_source_sheet"] = source_name
    return df


@st.cache_data(ttl=300, show_spinner=False)
def fetch_sheets(spreadsheet_id, range_hist, range_append, creds_info, reload_key):
    hist_df = _read_sheet_with_index(spreadsheet_id, range_hist, "history", creds_info)
    app_df = _read_sheet_with_index(spreadsheet_id, range_append, "append", creds_info)
    return hist_df, app_df

# ------------------ Refresh UI ------------------
col_refresh, col_time = st.columns([1, 3])
if col_refresh.button("🔁 Refresh Data", use_container_width=True):
    st.session_state.reload_key += 1
    st.experimental_rerun()

if st.session_state.last_refreshed:
    col_time.caption(f"Last refreshed: {st.session_state.last_refreshed.strftime('%Y-%m-%d %H:%M:%S UTC')}")

# ------------------ Fetch Data ------------------
creds_info = _get_creds_info()
if creds_info is None:
    st.error("Missing Google credentials (gcp_service_account).")
    st.stop()

with st.spinner("Fetching Google Sheets..."):
    history_df, append_df = fetch_sheets(SHEET_ID, RANGE, APPEND_RANGE, creds_info, st.session_state.reload_key)
    st.session_state.last_refreshed = datetime.utcnow()

if history_df.empty and append_df.empty:
    st.error("No data found in the Google Sheet.")
    st.stop()

df_raw = pd.concat([history_df, append_df], ignore_index=True, sort=False)

# ------------------ Filter Deleted ------------------
if "is_deleted" in df_raw.columns:
    mask = df_raw["is_deleted"].astype(str).str.lower().isin(["true", "t", "1", "yes"])
    df_raw = df_raw.loc[~mask].copy()

if df_raw.empty:
    st.warning("No visible rows (after filtering deleted entries).")
    st.stop()

# ------------------ Transform ------------------
converted_df = transform.convert_columns_and_derives(df_raw)

if "Bank" not in converted_df.columns:
    converted_df["Bank"] = "Unknown"

# ------------------ Sidebar Filters ------------------
st.sidebar.header("Filters")
banks = sorted(converted_df["Bank"].dropna().unique().tolist())
sel_banks = st.sidebar.multiselect("Banks", options=banks, default=banks)
filtered_df = converted_df[converted_df["Bank"].isin(sel_banks)]

with st.spinner("Computing daily totals..."):
    merged = transform.compute_daily_totals(filtered_df)

if not merged.empty:
    merged["Date"] = pd.to_datetime(merged["Date"]).dt.normalize()

# ------------------ Date Range ------------------
if "timestamp" in filtered_df.columns:
    filtered_df["timestamp"] = pd.to_datetime(filtered_df["timestamp"], errors="coerce")
else:
    filtered_df["timestamp"] = pd.to_datetime(filtered_df.get("date"), errors="coerce")

valid_dates = filtered_df["timestamp"].dropna()
if valid_dates.empty:
    st.warning("No valid dates found.")
    st.stop()

min_date, max_date = valid_dates.min().date(), valid_dates.max().date()
today = datetime.utcnow().date()

totals_mode = st.sidebar.radio("Totals mode", ["Single date", "Date range"], index=0)

if totals_mode == "Single date":
    sel_date = st.sidebar.date_input("Pick date", value=today, min_value=min_date, max_value=max_date)
    start_sel, end_sel = sel_date, sel_date
else:
    dr = st.sidebar.date_input("Pick date range", value=(min_date, max_date), min_value=min_date, max_value=max_date)
    if isinstance(dr, (tuple, list)) and len(dr) == 2:
        start_sel, end_sel = dr
    else:
        start_sel, end_sel = dr, dr

# ------------------ Totals ------------------
tmp = filtered_df.copy()
mask = tmp["timestamp"].dt.date.between(start_sel, end_sel)
sel_df = tmp.loc[mask]

amt_col = next((c for c in sel_df.columns if c.lower() == "amount"), None)
type_col = next((c for c in sel_df.columns if c.lower() == "type"), None)
credit_sum = debit_sum = credit_count = debit_count = 0

if amt_col:
    sel_df["amt"] = pd.to_numeric(sel_df[amt_col], errors="coerce").fillna(0.0)
    if type_col:
        sel_df["type_norm"] = sel_df[type_col].astype(str).str.lower()
        credit_mask = sel_df["type_norm"] == "credit"
        debit_mask = sel_df["type_norm"] == "debit"
        credit_sum = sel_df.loc[credit_mask, "amt"].sum()
        debit_sum = sel_df.loc[debit_mask, "amt"].sum()
        credit_count = int(credit_mask.sum())
        debit_count = int(debit_mask.sum())

c1, c2, c3 = st.columns(3)
c1.metric("Credits", f"₹{credit_sum:,.0f}", f"{credit_count} txns")
c2.metric("Debits", f"₹{debit_sum:,.0f}", f"{debit_count} txns")
c3.metric("Net", f"₹{(credit_sum - debit_sum):,.0f}")

# ------------------ Charts ------------------
st.subheader("📊 Charts")
if not merged.empty and charts_mod is not None:
    chart_type = st.selectbox("Chart type", ["Daily line", "Monthly bars", "Top categories (Top-N)"], index=0)
    series_selected = []
    if st.sidebar.checkbox("Show Debit (Total_Spent)", True):
        series_selected.append("Total_Spent")
    if st.sidebar.checkbox("Show Credit (Total_Credit)", True):
        series_selected.append("Total_Credit")
    top_n = st.sidebar.slider("Top-N categories", 3, 20, 5)
    charts_mod.render_chart(merged, filtered_df, chart_type, series_selected, top_n=top_n, height=420)
else:
    st.info("No data for charts.")

# ------------------ Rows Table ------------------
st.subheader("Rows (matching selection)")
rows_df = filtered_df.copy()
mask = rows_df["timestamp"].dt.date.between(start_sel, end_sel)
rows_df = rows_df.loc[mask]

display_cols = [c for c in ["timestamp", "Bank", "Type", "Amount", "Message"] if c in rows_df.columns]
st.dataframe(rows_df[display_cols], use_container_width=True, height=420)

csv_data = rows_df.to_csv(index=False).encode("utf-8")
st.download_button("📥 Download CSV", csv_data, "transactions.csv", "text/csv")

# ------------------ Add New Row (✅ FIXED DATE FORMAT) ------------------
st.markdown("---")
st.write("➕ Add a new transaction")

if io_mod is not None:
    with st.expander("Add new row"):
        with st.form("add_row_form", clear_on_submit=True):
            new_date = st.date_input("Date", value=datetime.utcnow().date())
            bank = st.text_input("Bank", "HDFC Bank")
            txn_type = st.selectbox("Type", ["debit", "credit"])
            amount = st.number_input("Amount (₹)", min_value=0.0, step=1.0, format="%.2f")
            msg = st.text_input("Message / Description", "")
            submit_add = st.form_submit_button("Save new row")

            if submit_add:
                # ✅ FIX: Use human-readable date/time (ISO 8601)
                dt_combined = datetime.combine(new_date, datetime.utcnow().time())
                timestamp_str = dt_combined.strftime("%Y-%m-%d %H:%M:%S")

                new_row = {
                    "DateTime": timestamp_str,  # Sheets will recognize this as a date
                    "timestamp": timestamp_str,
                    "date": new_date.isoformat(),
                    "Bank": bank,
                    "Type": txn_type,
                    "Amount": amount,
                    "Message": msg,
                    "is_deleted": "false",
                }

                try:
                    res = io_mod.append_new_row(
                        spreadsheet_id=SHEET_ID,
                        range_name=APPEND_RANGE,
                        new_row_dict=new_row,
                        creds_info=creds_info,
                        history_range=RANGE,
                    )
                    if res.get("status") == "ok":
                        st.success("✅ Row added successfully with correct date format!")
                        st.session_state.reload_key += 1
                        st.experimental_rerun()
                    else:
                        st.error(f"Failed to add row: {res}")
                except Exception as e:
                    st.error(f"Error adding row: {e}")
