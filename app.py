import streamlit as st
import pandas as pd
import re
import io
import traceback

# --- Page config ---
st.set_page_config(page_title="Mirror Trade App", layout="wide")

# --- Helper functions ---
def clean_name(s):
    return re.sub(r'[^0-9a-zA-Z]+', '_', str(s)).strip('_').lower()

def find_col(orig_cols, patterns):
    if isinstance(patterns, str):
        patterns = [patterns]
    for pat in patterns:
        pat_clean = clean_name(pat)
        for c in orig_cols:
            if clean_name(c) == pat_clean:
                return c
    return None

def extract_quote(market_str):
    s = str(market_str).upper().strip()
    if "-" in s:
        # common format: ASSET-QUOTE
        return s.split("-")[-1]
    # fallback tries
    common_quotes = ["USDT", "BUSD", "USD", "ETH", "BTC", "INR"]
    for q in common_quotes:
        if s.endswith(q):
            return q
    return s[-3:]

# --- UI: instructions shown before upload ---
st.title("📊 Mirror Trade Processor")
st.markdown(
    "**Important columns required BEFORE upload** — make sure your file includes these headers (names can vary slightly):"
)
st.info(
    """
- **Market**  — e.g. `BTC-USDT` or `USD-INR`  
- **Date**    — trade date  
- **Trade Type** — `BUY` or `SELL`  
- **Quantity** — numeric (only quantity)  
- **Price** — final per-unit price  
- **Total** — Quantity × Price
"""
)

uploaded_file = st.file_uploader("📂 Upload your Excel file (.xlsx)", type=["xlsx"])

if uploaded_file is None:
    st.write("Upload a file to begin. The app will validate required columns and show warnings/errors.")
    st.stop()

# --- Try reading the file ---
try:
    df_raw = pd.read_excel(uploaded_file, engine="openpyxl")
except Exception as e:
    st.error("Failed to read the uploaded Excel file. Make sure it's a valid .xlsx file.")
    st.exception(e)
    st.stop()

orig_cols = df_raw.columns.tolist()
st.subheader("Preview (first 5 rows)")
st.dataframe(df_raw.head(), height=220)

# --- Detect columns using flexible matching ---
col = {
    "market": find_col(orig_cols, ["market", "pair", "market_2"]),
    "date": find_col(orig_cols, ["date", "trade date", "trade_date"]),
    "trade_type": find_col(orig_cols, ["trade type", "trade_type", "side", "action"]),
    "quantity": find_col(orig_cols, ["quantity", "qty", "totalvolume", "total_volume"]),
    "price": find_col(orig_cols, ["price", "final price", "rate", "actualrate"]),
    "total": find_col(orig_cols, ["total", "amount", "total_inr", "gross_amount"]),
    "usd_inr": find_col(orig_cols, ["usd_inr_rate", "usd_inr", "usd-inr", "usd inr"])
}

# Show the detected mapping
st.write("Detected columns (automatically matched):")
for k, v in col.items():
    st.write(f"- **{k}** -> `{v}`")

# --- Validate required columns ---
required = ["market", "date", "trade_type", "quantity", "price", "total"]
missing = [r for r in required if col.get(r) is None]
if missing:
    st.error("❌ Required column(s) missing: " + ", ".join(missing))
    st.warning("Please rename your Excel headers or add these columns and re-upload.")
    st.stop()

# --- Process file with error handling ---
try:
    # Copy originals
    df = df_raw.copy()

    mkt_col = col["market"]
    tt_col = col["trade_type"]
    qty_col = col["quantity"]
    price_col = col["price"]
    total_col = col["total"]
    date_col = col["date"]
    usd_col = col["usd_inr"]

    # Separate INR pairs and non-INR pairs
    is_inr = df[mkt_col].astype(str).str.upper().str.endswith("INR")
    df_inr = df[is_inr].copy()
    df_non_inr = df[~is_inr].copy()

    if df_non_inr.empty:
        st.info("No non-INR rows found — nothing to mirror. You can download the original file.")
        # prepare original download
        towrite = io.BytesIO()
        df.to_excel(towrite, index=False, engine="openpyxl")
        towrite.seek(0)
        st.download_button("⬇️ Download original file", data=towrite, file_name="original_no_nonINR.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.stop()

    # Prepare mirrored rows
    mirrored = df_non_inr.copy()
    mirrored["__quote__"] = mirrored[mkt_col].apply(extract_quote)
    mirrored[mkt_col] = mirrored["__quote__"] + "INR"

    # Flip buy/sell
    mirrored[tt_col] = mirrored[tt_col].astype(str).str.upper().apply(
        lambda x: "BUY" if x == "SELL" else ("SELL" if x == "BUY" else x)
    )

    # Keep quantity same (best-effort)
    mirrored[qty_col] = mirrored[qty_col]

    # If USD-INR (or similar) column exists, try to convert Price and Total to INR
    if usd_col:
        # best-effort numeric conversion
        try:
            mirrored[price_col] = pd.to_numeric(mirrored[price_col], errors="coerce") * pd.to_numeric(mirrored[usd_col], errors="coerce")
            mirrored[total_col] = pd.to_numeric(mirrored[total_col], errors="coerce") * pd.to_numeric(mirrored[usd_col], errors="coerce")
            mirrored[usd_col] = 1  # now quoted in INR
        except Exception:
            st.warning("Could not compute exact INR Price/Total because of non-numeric values. Mirrored Price/Total may contain NaNs.")
    else:
        st.warning("No USD-INR rate column found. Mirrored INR Price/Total cannot be computed precisely and will be left as original values or NaN.")

    mirrored.drop(columns=["__quote__"], inplace=True, errors="ignore")

    # Combine
    final_df = pd.concat([df, mirrored], ignore_index=True, sort=False)

    # Try sorting by date if possible
    try:
        final_df[date_col] = pd.to_datetime(final_df[date_col], errors="coerce")
        final_df = final_df.sort_values(by=date_col).reset_index(drop=True)
    except Exception:
        pass

    # Provide final downloadable file (in-memory)
    out_buffer = io.BytesIO()
    final_df.to_excel(out_buffer, index=False, engine="openpyxl")
    out_buffer.seek(0)

    st.success("✅ Processing complete. You can download the processed file below.")
    st.download_button(
        label="⬇️ Download Mirrored Output",
        data=out_buffer,
        file_name="output_trades_with_mirrors.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    st.balloons()

except Exception as exc:
    st.error("⚠️ An error occurred while processing the file.")
    st.exception(traceback.format_exc())
