"""
FinSight MCP Server.

Exposes financial-data ingestion (CSV, Excel, a read-only SQL warehouse, PDF
statements, a simulated live feed) and deterministic analysis (ratios,
trend/variance, anomaly detection, forecasting, reporting) as MCP tools
over stdio.

Security notes:
  - This server contains NO tool that writes to, or executes a transaction
    against, any external financial account -- every data source is read
    from, never written to. The only writes are to this project's own
    local scratch files (the active in-memory dataset / results cache).
  - query_database enforces read-only access two ways: a syntax check that
    rejects anything but a single SELECT statement, and the underlying
    sqlite3 connection itself is opened in read-only mode (`mode=ro`), so
    even a cleverly-phrased query cannot modify data.
  - Sensitive identifiers are minimized at this tool layer, not left to the
    model to handle carefully: account numbers are masked to their last 4
    digits before ever being stored, and tax-id-shaped fields (vendor EIN)
    are dropped entirely rather than passed through.
  - All ratios/trends/anomaly scores/forecasts are computed here in plain
    Python (pandas/numpy), never left for the LLM to calculate -- numeric
    hallucination is a much bigger credibility risk for a financial analyst
    agent than it was for a prior medication-tracking project, so nothing
    numeric is ever "estimated" by the model.
"""

import json
import math
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pdfplumber
from fastmcp import FastMCP

DATA_DIR = Path(os.environ.get("FINSIGHT_DATA_DIR", Path(__file__).parent.parent / "data"))
ACTIVE_DATASET_PATH = DATA_DIR / "_active_dataset.json"
RESULTS_CACHE_PATH = DATA_DIR / "_last_results.json"
LEDGER_DB_PATH = DATA_DIR / "ledger.db"

REQUIRED_CSV_COLUMNS = {"date", "description", "category", "account_type", "amount"}
_SQL_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|PRAGMA|REPLACE|VACUUM)\b", re.IGNORECASE
)
_PDF_LINE_RE = re.compile(r"^(.+?)\s+\$(-?[\d,]+\.\d{2})$", re.MULTILINE)
_COGS_SYNONYMS = {"cogs", "cost of goods sold", "cost of good sold"}

mcp = FastMCP("finsight-tools")


# ---------------------------------------------------------------- helpers --

def _resolve_path(file_path: str) -> Path | None:
    """Resolves a caller-supplied filename to a path INSIDE the data directory only.

    Returns None if the requested path would escape the data directory (a
    '..' traversal, or an absolute path elsewhere on disk) -- ingestion
    tools only ever need to read demo files under DATA_DIR, and file_path
    is ultimately model-controlled (it can be steered by injected text),
    so this containment check is load-bearing, not a formality.
    """
    data_root = DATA_DIR.resolve()
    candidate = (data_root / file_path).resolve()
    if candidate != data_root and data_root not in candidate.parents:
        return None
    return candidate


def _mask_account_number(raw) -> str | None:
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return None
    raw = str(raw).strip()
    if raw.endswith(".0"):
        # pandas silently upcasts an all-numeric column to float64 when any
        # row in it is blank, turning e.g. "1234567890123456" into
        # 1234567890123456.0 -- strip that artifact before masking.
        raw = raw[:-2]
    if len(raw) < 4:
        return "****"
    return "*" * (len(raw) - 4) + raw[-4:]


def _normalize_row(date_val, description, category, account_type, amount, account_number=None, source="") -> dict:
    amount_f = float(amount)
    if math.isnan(amount_f):
        raise ValueError(f"amount is NaN/blank for row: {description!r}")
    return {
        "date": str(date_val)[:10],
        "description": str(description),
        "category": str(category),
        "type": str(account_type).strip().lower(),
        "amount": round(amount_f, 2),
        "account_number_masked": _mask_account_number(account_number),
        "source": source,
    }


def _dataframe_to_records(df: "pd.DataFrame", source: str) -> tuple[list[dict], int]:
    """Shared row-normalization loop used by both load_csv and load_excel."""
    records, skipped = [], 0
    for _, row in df.iterrows():
        try:
            records.append(
                _normalize_row(
                    row["date"], row["description"], row["category"], row["account_type"],
                    row["amount"], row.get("account_number"), source=source,
                )
            )
        except (ValueError, TypeError):
            skipped += 1
    return records, skipped


def _load_active_dataset() -> list[dict]:
    if not ACTIVE_DATASET_PATH.exists():
        return []
    with open(ACTIVE_DATASET_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _append_active_dataset(records: list[dict]) -> int:
    existing = _load_active_dataset()
    existing.extend(records)
    with open(ACTIVE_DATASET_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    return len(existing)


def _save_result(kind: str, data: dict) -> None:
    cache = {}
    if RESULTS_CACHE_PATH.exists():
        with open(RESULTS_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
    cache[kind] = {"computed_at": datetime.now().isoformat(timespec="seconds"), "result": data}
    with open(RESULTS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def _readonly_ledger_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{LEDGER_DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _balance_snapshot_impl() -> dict:
    if not LEDGER_DB_PATH.exists():
        return {"error": "Ledger database not found."}
    conn = _readonly_ledger_connection()
    rows = conn.execute(
        "SELECT entry_date, account_name, memo FROM gl_entries "
        "WHERE account_type IN ('asset','liability') ORDER BY entry_date DESC"
    ).fetchall()
    conn.close()
    latest: dict[str, float] = {}
    as_of = None
    balance_re = re.compile(r"balance\s+(-?\d+(?:\.\d+)?)", re.IGNORECASE)
    for r in rows:
        name = r["account_name"]
        if name in latest:
            continue
        m = balance_re.search(r["memo"] or "")
        if m:
            latest[name] = float(m.group(1))
            as_of = as_of or r["entry_date"]
    if not latest:
        return {"error": "No balance-sheet accounts found in the ledger."}
    return {
        "as_of": as_of,
        "cash": latest.get("Cash"),
        "accounts_receivable": latest.get("Accounts Receivable"),
        "accounts_payable": latest.get("Accounts Payable"),
        "loan_payable": latest.get("Loan Payable"),
    }


# --------------------------------------------------------------- ingestion --

@mcp.tool()
def load_csv(file_path: str) -> dict:
    """Loads a CSV of financial transactions (e.g. a bank/POS export) into the active dataset.

    Expected columns: date, description, category, account_type ('revenue'
    or 'expense'), amount. An account_number column, if present, is masked
    to its last 4 digits before storage -- the raw number is never
    retained. Any other column (e.g. a vendor tax ID) is dropped entirely.

    Args:
        file_path: Path to the CSV file, relative to the data directory or absolute.

    Returns:
        A dict with 'added', 'skipped_invalid_rows', and 'total_in_dataset',
        or an 'error' key if the file is missing, empty, or malformed.
    """
    path = _resolve_path(file_path)
    if path is None:
        return {"error": "file_path must be inside the data directory."}
    if not path.exists():
        return {"error": f"File not found: {file_path}"}
    try:
        df = pd.read_csv(path)
    except Exception as e:
        return {"error": f"Could not parse CSV: {e}"}
    if df.empty:
        return {"error": "CSV file has no rows."}
    df.columns = [c.lower() for c in df.columns]
    missing = REQUIRED_CSV_COLUMNS - set(df.columns)
    if missing:
        return {"error": f"CSV is missing required columns: {sorted(missing)}"}
    records, skipped = _dataframe_to_records(df, source=f"csv:{path.name}")
    if not records:
        return {"error": "No valid rows could be parsed from this CSV."}
    total = _append_active_dataset(records)
    return {"added": len(records), "skipped_invalid_rows": skipped, "total_in_dataset": total}


@mcp.tool()
def load_excel(file_path: str, sheet_name: str | None = None) -> dict:
    """Loads an Excel workbook of financial transactions into the active dataset.

    Same expected columns and account-number redaction rules as load_csv.

    Args:
        file_path: Path to the .xlsx file, relative to the data directory or absolute.
        sheet_name: Optional sheet name; defaults to the first sheet.

    Returns:
        Same shape as load_csv, plus 'sheet_used'. An 'error' key includes
        'available_sheets' if the requested sheet name doesn't exist.
    """
    path = _resolve_path(file_path)
    if path is None:
        return {"error": "file_path must be inside the data directory."}
    if not path.exists():
        return {"error": f"File not found: {file_path}"}
    try:
        xls = pd.ExcelFile(path)
    except Exception as e:
        return {"error": f"Could not open Excel file: {e}"}
    target_sheet = sheet_name or xls.sheet_names[0]
    if target_sheet not in xls.sheet_names:
        return {"error": f"Sheet '{sheet_name}' not found.", "available_sheets": xls.sheet_names}
    df = xls.parse(target_sheet)
    if df.empty:
        return {"error": f"Sheet '{target_sheet}' has no rows."}
    df.columns = [c.lower() for c in df.columns]
    missing = REQUIRED_CSV_COLUMNS - set(df.columns)
    if missing:
        return {"error": f"Sheet is missing required columns: {sorted(missing)}"}
    records, skipped = _dataframe_to_records(df, source=f"excel:{path.name}:{target_sheet}")
    if not records:
        return {"error": "No valid rows could be parsed from this sheet."}
    total = _append_active_dataset(records)
    return {"added": len(records), "skipped_invalid_rows": skipped, "total_in_dataset": total, "sheet_used": target_sheet}


@mcp.tool()
def load_from_database() -> dict:
    """Pulls revenue/expense entries from the ledger data warehouse into the active dataset.

    This is the turnkey ingestion path for the database source: unlike
    query_database, no SQL is required from the caller -- a fixed,
    known-safe query runs server-side, so financial figures never pass
    through model-generated SQL on the way into the dataset.

    Returns:
        Same shape as load_csv, or an 'error' key.
    """
    if not LEDGER_DB_PATH.exists():
        return {"error": "Ledger database not found."}
    try:
        conn = _readonly_ledger_connection()
        rows = conn.execute(
            "SELECT entry_date, account_name, account_type, debit, credit FROM gl_entries "
            "WHERE account_type IN ('revenue','expense')"
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        return {"error": f"Query failed: {e}"}
    records = [
        _normalize_row(
            r["entry_date"], r["account_name"], r["account_name"], r["account_type"],
            r["credit"] if r["account_type"] == "revenue" else r["debit"],
            None, source="database:ledger.db",
        )
        for r in rows
    ]
    if not records:
        return {"error": "No revenue/expense entries found in the ledger."}
    total = _append_active_dataset(records)
    return {"added": len(records), "total_in_dataset": total}


@mcp.tool()
def query_database(sql: str) -> dict:
    """Runs a read-only SQL query against the financial ledger database for ad-hoc questions.

    Only a single SELECT statement is permitted -- enforced both by a
    keyword check and by the underlying connection being opened in
    read-only mode, so no query can modify data regardless of phrasing.

    Args:
        sql: A single SELECT statement. Table: gl_entries(entry_date,
            account_name, account_type, debit, credit, memo).

    Returns:
        A dict with 'rows' and 'row_count', or an 'error' key if the query
        is rejected or fails.
    """
    stripped = sql.strip().rstrip(";")
    if not stripped.lower().startswith("select"):
        return {"error": "Only a single SELECT statement is permitted (read-only access)."}
    if ";" in stripped:
        return {"error": "Only a single statement is permitted -- remove the embedded ';'."}
    if _SQL_WRITE_KEYWORDS.search(stripped):
        return {"error": "Query contains a disallowed keyword; only simple SELECT statements are permitted."}
    if not LEDGER_DB_PATH.exists():
        return {"error": "Ledger database not found."}
    try:
        conn = _readonly_ledger_connection()
        rows = [dict(r) for r in conn.execute(stripped).fetchall()]
        conn.close()
    except sqlite3.Error as e:
        return {"error": f"Query failed: {e}"}
    return {"rows": rows, "row_count": len(rows)}


@mcp.tool()
def get_balance_snapshot() -> dict:
    """Retrieves the most recent Cash / Accounts Receivable / Accounts Payable / Loan Payable balances.

    Returns:
        A dict with the four balances and the as-of month, or an 'error' key.
    """
    return _balance_snapshot_impl()


@mcp.tool()
def extract_pdf_financials(file_path: str) -> dict:
    """Extracts labeled financial line items (e.g. 'Total Revenue $123,456.78') from a PDF statement.

    Args:
        file_path: Path to the PDF file, relative to the data directory or absolute.

    Returns:
        A dict with 'line_items' and 'added_to_dataset', or an 'error' key
        if the file is missing or has no extractable/recognizable text
        (e.g. a scanned image with no OCR layer).
    """
    path = _resolve_path(file_path)
    if path is None:
        return {"error": "file_path must be inside the data directory."}
    if not path.exists():
        return {"error": f"File not found: {file_path}"}
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        return {"error": f"Could not open PDF: {e}"}
    if not text.strip():
        return {"error": "No extractable text found in this PDF (it may be a scanned image without OCR)."}
    line_items = {label.strip(): float(amount.replace(",", "")) for label, amount in _PDF_LINE_RE.findall(text)}
    if not line_items:
        return {"error": "Text was extracted but no 'Label $amount' line items were recognized.", "raw_text_preview": text[:300]}
    today = datetime.now().date().isoformat()
    records = [
        _normalize_row(today, label, label, "revenue" if "revenue" in label.lower() else "expense", abs(amount), None, source=f"pdf:{path.name}")
        for label, amount in line_items.items()
        if label.lower() not in ("gross profit", "net income")  # derived totals, not raw line items
    ]
    total = _append_active_dataset(records) if records else len(_load_active_dataset())
    return {"line_items": line_items, "added_to_dataset": len(records), "total_in_dataset": total}


@mcp.tool()
def fetch_live_feed() -> dict:
    """Fetches the latest transactions from the (simulated) live accounting feed.

    Stands in for a real accounting-software/banking API call -- see
    data/live_feed_mock.json. No real credentials or endpoints are used
    anywhere in this project. A production version of this tool would add
    HTTP timeout/retry handling around the real API call.

    Returns:
        A dict with 'added', 'total_in_dataset', and 'feed_as_of', or an
        'error' key.
    """
    path = DATA_DIR / "live_feed_mock.json"
    if not path.exists():
        return {"error": "Live feed mock data not found."}
    with open(path, "r", encoding="utf-8") as f:
        feed = json.load(f)
    records = [
        _normalize_row(
            t["date"], t["description"], t["category"],
            "revenue" if t["category"] == "Revenue" else "expense", t["amount"], None, source="live_feed",
        )
        for t in feed.get("transactions", [])
    ]
    if not records:
        return {"error": "No transactions in live feed response."}
    total = _append_active_dataset(records)
    return {"added": len(records), "total_in_dataset": total, "feed_as_of": feed.get("as_of")}


@mcp.tool()
def reset_dataset() -> dict:
    """Clears the active dataset and cached analysis results (start-of-session hygiene).

    Returns:
        A confirmation dict.
    """
    for p in (ACTIVE_DATASET_PATH, RESULTS_CACHE_PATH):
        if p.exists():
            p.unlink()
    return {"reset": True}


# ---------------------------------------------------------------- analysis --

@mcp.tool()
def compute_ratios() -> dict:
    """Computes profitability and liquidity ratios from the active dataset and ledger balances.

    All arithmetic happens here in Python, not in the model. Guards against
    division by zero by returning null (with a reason) instead of crashing.

    Returns:
        A dict with revenue, total_expenses, net_income, gross_margin and
        net_margin (each null if revenue is zero), and current_ratio (null
        with a 'current_ratio_note' if balance data isn't available), or
        an 'error' key if no data has been loaded yet.
    """
    dataset = _load_active_dataset()
    if not dataset:
        return {"error": "No data loaded yet -- use an ingestion tool first."}

    revenue = sum(r["amount"] for r in dataset if r["type"] == "revenue")
    expenses_by_cat: dict[str, float] = {}
    for r in dataset:
        if r["type"] == "expense":
            expenses_by_cat[r["category"]] = expenses_by_cat.get(r["category"], 0) + r["amount"]
    # Match COGS case/spelling-insensitively -- ingestion sources are free to use
    # whatever label they like (e.g. a PDF's "Cost of Goods Sold" vs a CSV's "COGS").
    cogs = sum(v for k, v in expenses_by_cat.items() if k.strip().lower() in _COGS_SYNONYMS)
    total_expenses = sum(expenses_by_cat.values())
    gross_profit = revenue - cogs
    net_income = revenue - total_expenses

    def _safe_div(n, d):
        return round(n / d, 4) if d else None

    result = {
        "revenue": round(revenue, 2),
        "total_expenses": round(total_expenses, 2),
        "net_income": round(net_income, 2),
        "gross_margin": _safe_div(gross_profit, revenue),
        "net_margin": _safe_div(net_income, revenue),
    }
    balance = _balance_snapshot_impl()
    if "error" not in balance:
        current_assets = (balance["cash"] or 0) + (balance["accounts_receivable"] or 0)
        current_liabilities = (balance["accounts_payable"] or 0) + (balance["loan_payable"] or 0)
        result["current_ratio"] = _safe_div(current_assets, current_liabilities)
        result["balance_as_of"] = balance["as_of"]
    else:
        result["current_ratio"] = None
        result["current_ratio_note"] = balance["error"]
    _save_result("ratios", result)
    return result


@mcp.tool()
def compute_trend_variance() -> dict:
    """Computes month-over-month revenue/expense trend and variance from the active dataset.

    Returns:
        A dict with a monthly breakdown (revenue, expenses, net, and
        percent change vs. the prior month), or an 'error' key if there's
        not enough data (fewer than 2 distinct months).
    """
    dataset = _load_active_dataset()
    if not dataset:
        return {"error": "No data loaded yet -- use an ingestion tool first."}
    monthly: dict[str, dict[str, float]] = {}
    for r in dataset:
        month_key = r["date"][:7]
        bucket = monthly.setdefault(month_key, {"revenue": 0.0, "expenses": 0.0})
        bucket["revenue" if r["type"] == "revenue" else "expenses"] += r["amount"]
    months = sorted(monthly.keys())
    if len(months) < 2:
        return {"error": "Need at least 2 distinct months of data to compute trend/variance."}
    breakdown, prev_net = [], None
    for m in months:
        net = round(monthly[m]["revenue"] - monthly[m]["expenses"], 2)
        pct_change = round((net - prev_net) / abs(prev_net) * 100, 1) if prev_net else None
        breakdown.append({
            "month": m, "revenue": round(monthly[m]["revenue"], 2),
            "expenses": round(monthly[m]["expenses"], 2), "net": net,
            "pct_change_vs_prior_month": pct_change,
        })
        prev_net = net
    result = {"monthly_breakdown": breakdown}
    _save_result("trends", result)
    return result


@mcp.tool()
def detect_anomalies(z_threshold: float = 2.5, rare_category_max_count: int = 2) -> dict:
    """Flags unusual expense transactions: statistical outliers within their own
    category, plus transactions in categories that almost never occur.

    Deterministic statistics, not LLM judgment. Z-scores are computed
    WITHIN each expense category rather than across all expenses together
    -- a recurring, naturally large category like COGS shouldn't get
    flagged just for being bigger than Rent or Utilities; a $26k COGS
    charge in a high-revenue month is normal, a single unprecedented
    "Repairs" entry is not.

    Args:
        z_threshold: Standard deviations above the category mean required
            to flag a transaction (default 2.5). Only applied to
            categories with more than rare_category_max_count historical
            transactions.
        rare_category_max_count: Categories with this many or fewer total
            transactions are flagged outright as rare/unprecedented rather
            than statistically scored (default 2).

    Returns:
        A dict with 'flagged' transactions (each with a 'reason' and,
        where applicable, a 'z_score') and 'checked_count'/'categories_checked',
        or an 'error' key if there's not enough data (fewer than 5 expense
        records overall).
    """
    dataset = [r for r in _load_active_dataset() if r["type"] == "expense"]
    if len(dataset) < 5:
        return {"error": "Need at least 5 expense records to run anomaly detection."}

    by_category: dict[str, list[dict]] = {}
    for r in dataset:
        by_category.setdefault(r["category"], []).append(r)

    flagged = []
    for category, records in by_category.items():
        if len(records) <= rare_category_max_count:
            for r in records:
                flagged.append({**r, "reason": f"rare/unprecedented category ({len(records)} occurrence(s) in dataset)"})
            continue
        amounts = np.array([r["amount"] for r in records])
        mean, std = float(np.mean(amounts)), float(np.std(amounts))
        if std == 0:
            continue
        for r, amt in zip(records, amounts):
            z = (amt - mean) / std
            if z > z_threshold:
                flagged.append({**r, "reason": "statistical outlier within its category", "z_score": round(float(z), 2), "category_mean": round(mean, 2)})

    result = {"flagged": flagged, "checked_count": len(dataset), "categories_checked": len(by_category)}
    _save_result("anomalies", result)
    return result


@mcp.tool()
def forecast_series(periods_ahead: int = 3) -> dict:
    """Forecasts future monthly net income using linear regression on historical months.

    Deterministic method (numpy least-squares line fit) -- the method and
    its fit quality (R^2) are always disclosed so projections are never
    presented as more certain than they are.

    Args:
        periods_ahead: How many future months to project (default 3).

    Returns:
        A dict with 'historical_months', 'projected', 'method', and
        'r_squared', or an 'error' key if there's not enough history
        (fewer than 4 distinct months).
    """
    dataset = _load_active_dataset()
    if not dataset:
        return {"error": "No data loaded yet -- use an ingestion tool first."}
    monthly: dict[str, float] = {}
    for r in dataset:
        month_key = r["date"][:7]
        monthly[month_key] = monthly.get(month_key, 0.0) + (r["amount"] if r["type"] == "revenue" else -r["amount"])
    months = sorted(monthly.keys())
    if len(months) < 4:
        return {"error": "Need at least 4 distinct months of history to forecast reliably."}

    y = np.array([monthly[m] for m in months])
    x = np.arange(len(months))
    slope, intercept = np.polyfit(x, y, 1)
    predicted = slope * x + intercept
    ss_res = float(np.sum((y - predicted) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = round(1 - ss_res / ss_tot, 3) if ss_tot else None

    def _next_month_label(label: str, offset: int) -> str:
        year, mon = map(int, label.split("-"))
        total = (year * 12 + (mon - 1)) + offset
        return f"{total // 12}-{total % 12 + 1:02d}"

    projected = [
        {"month": _next_month_label(months[-1], i), "projected_net": round(float(slope * (len(months) - 1 + i) + intercept), 2)}
        for i in range(1, periods_ahead + 1)
    ]
    result = {
        "historical_months": months,
        "projected": projected,
        "method": "linear regression (least-squares) on monthly net income",
        "r_squared": r_squared,
    }
    _save_result("forecast", result)
    return result


@mcp.tool()
def generate_report() -> dict:
    """Assembles the latest computed ratios/trends/anomalies/forecast into one structured report bundle.

    Pulls only from results already computed by the other tools -- never
    re-derives or estimates numbers itself -- so the reporting agent
    narrates from ground truth rather than recalling figures from earlier
    in the conversation.

    Returns:
        A dict keyed by 'ratios', 'trends', 'anomalies', 'forecast', each
        either the last computed result or a note that it hasn't run yet,
        or an 'error' key if nothing has been computed at all.
    """
    if not RESULTS_CACHE_PATH.exists():
        return {"error": "No analysis has been run yet -- compute ratios/trends/anomalies/forecast first."}
    with open(RESULTS_CACHE_PATH, "r", encoding="utf-8") as f:
        cache = json.load(f)
    return {kind: cache.get(kind, {"note": f"{kind} has not been computed yet."}) for kind in ("ratios", "trends", "anomalies", "forecast")}


if __name__ == "__main__":
    mcp.run(transport="stdio")
