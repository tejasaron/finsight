"""One-time generator for FinSight's synthetic demo dataset.

Fictional company: Aperture Retail Co., a 12-person retail business used
throughout the demo. All data below is synthetic and generated with a fixed
random seed for reproducibility -- no real financial data of any kind.

Not a runtime dependency of the agent itself -- this just produces the files
committed under data/. Run manually:

    pip install fpdf2   # only needed to (re)generate the PDF statement
    python scripts/generate_demo_data.py
"""

import csv
import json
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

random.seed(42)

DATA_DIR = Path(__file__).parent.parent / "data"
COMPANY = "Aperture Retail Co."

MONTHS = [date(2025, m, 1) for m in range(1, 13)]

# (base_amount, monthly_growth_rate) -- revenue grows steadily with a holiday bump.
BASE_REVENUE = 45000.0
REVENUE_GROWTH = 0.012
HOLIDAY_MONTHS = {11, 12}  # Nov, Dec seasonal bump

VENDORS = {
    "COGS": ("Northwind Wholesale Supply", "88-1234567"),
    "Payroll": ("Aperture Payroll Processing", "88-2345678"),
    "Rent": ("Riverbend Properties LLC", "88-3456789"),
    "Marketing": ("BrightPath Media Co", "88-4567890"),
    "Utilities": ("Metro Power & Water", "88-5678901"),
}


def _fake_account_number(seed_key: str) -> str:
    rnd = random.Random(seed_key)
    return "".join(str(rnd.randint(0, 9)) for _ in range(16))


def monthly_figures():
    rows = []
    revenue = BASE_REVENUE
    for i, m in enumerate(MONTHS):
        rev = revenue * (1.35 if m.month in HOLIDAY_MONTHS else 1.0)
        cogs = rev * 0.38
        payroll = 11500 + (500 if m.month >= 7 else 0)
        rent = 4200.0
        marketing = 4500.0 if m.month in HOLIDAY_MONTHS else 2000.0
        utilities = 1000 + 300 * abs(6 - m.month) / 6  # cheaper in spring/fall
        rows.append(
            {
                "month": m,
                "Revenue": round(rev, 2),
                "COGS": round(cogs, 2),
                "Payroll": round(payroll, 2),
                "Rent": round(rent, 2),
                "Marketing": round(marketing, 2),
                "Utilities": round(utilities, 2),
            }
        )
        revenue *= 1 + REVENUE_GROWTH
    # Inject one deliberate anomaly: an unusual one-off equipment repair spike in August.
    for r in rows:
        r["OneOff"] = 0.0
    rows[7]["OneOff"] = 9800.0  # August (index 7) -- equipment repair spike
    return rows


def write_transactions_csv(rows):
    path = DATA_DIR / "transactions.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["transaction_id", "date", "description", "category", "account_type", "amount", "account_number", "vendor_ein"]
        )
        tx_id = 1000
        for r in rows:
            m = r["month"]
            # Revenue: spread across ~10 daily deposits summing to the month total.
            n_deposits = 10
            per = round(r["Revenue"] / n_deposits, 2)
            for d in range(n_deposits):
                tx_id += 1
                tx_date = m + timedelta(days=int(d * 3) % 27)
                writer.writerow(
                    [
                        tx_id,
                        tx_date.isoformat(),
                        "Daily sales deposit",
                        "Revenue",
                        "revenue",
                        per,
                        _fake_account_number("aperture-operating"),
                        "",
                    ]
                )
            # Expense line items, one per category per month.
            for cat in ("COGS", "Payroll", "Rent", "Marketing", "Utilities"):
                tx_id += 1
                vendor, ein = VENDORS[cat]
                writer.writerow(
                    [
                        tx_id,
                        (m + timedelta(days=4)).isoformat(),
                        f"{vendor} - {cat}",
                        cat,
                        "expense",
                        r[cat],
                        _fake_account_number(vendor),
                        ein,
                    ]
                )
            if r["OneOff"]:
                tx_id += 1
                writer.writerow(
                    [
                        tx_id,
                        (m + timedelta(days=18)).isoformat(),
                        "EMERGENCY: Freezer unit compressor replacement",
                        "Repairs",
                        "expense",
                        r["OneOff"],
                        _fake_account_number("facilities-emergency"),
                        "88-9999999",
                    ]
                )
    print(f"wrote {path}")


def write_ledger_db(rows):
    path = DATA_DIR / "ledger.db"
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE gl_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            account_name TEXT NOT NULL,
            account_type TEXT NOT NULL,
            debit REAL NOT NULL DEFAULT 0,
            credit REAL NOT NULL DEFAULT 0,
            memo TEXT
        )"""
    )
    cash_balance = 22000.0
    loan_balance = 30000.0
    for r in rows:
        m = r["month"].isoformat()
        net = r["Revenue"] - r["COGS"] - r["Payroll"] - r["Rent"] - r["Marketing"] - r["Utilities"] - r["OneOff"]
        cash_balance += net * 0.6  # remainder assumed tied up in receivables/inventory
        loan_payment = 500.0
        loan_balance = max(0.0, loan_balance - loan_payment)
        ar_balance = round(r["Revenue"] * 0.15, 2)
        ap_balance = round((r["COGS"] + r["Rent"]) * 0.35, 2)
        entries = [
            (m, "Revenue", "revenue", 0, r["Revenue"], "Monthly sales"),
            (m, "COGS", "expense", r["COGS"], 0, "Cost of goods sold"),
            (m, "Payroll Expense", "expense", r["Payroll"], 0, "Staff wages"),
            (m, "Rent Expense", "expense", r["Rent"], 0, "Storefront lease"),
            (m, "Marketing Expense", "expense", r["Marketing"], 0, "Advertising"),
            (m, "Utilities Expense", "expense", r["Utilities"], 0, "Power/water/internet"),
            (m, "Cash", "asset", 0, 0, f"Month-end balance {round(cash_balance, 2)}"),
            (m, "Accounts Receivable", "asset", 0, 0, f"Month-end balance {ar_balance}"),
            (m, "Accounts Payable", "liability", 0, 0, f"Month-end balance {ap_balance}"),
            (m, "Loan Payable", "liability", 0, 0, f"Month-end balance {round(loan_balance, 2)}"),
        ]
        if r["OneOff"]:
            entries.append((m, "Repairs Expense", "expense", r["OneOff"], 0, "Emergency equipment repair"))
        conn.executemany(
            "INSERT INTO gl_entries (entry_date, account_name, account_type, debit, credit, memo) VALUES (?,?,?,?,?,?)",
            entries,
        )
    conn.commit()
    conn.close()
    print(f"wrote {path}")


def write_live_feed_mock():
    path = DATA_DIR / "live_feed_mock.json"
    today = date(2026, 1, 5)
    feed = {
        "source": "simulated-accounting-api",
        "as_of": today.isoformat(),
        "note": "SIMULATED response -- stands in for a real accounting/banking API feed. No live credentials are used anywhere in this project.",
        "transactions": [
            {"date": (today - timedelta(days=3)).isoformat(), "description": "Daily sales deposit", "category": "Revenue", "amount": 2140.55},
            {"date": (today - timedelta(days=2)).isoformat(), "description": "Daily sales deposit", "category": "Revenue", "amount": 1980.10},
            {"date": (today - timedelta(days=2)).isoformat(), "description": "Northwind Wholesale Supply - COGS", "category": "COGS", "amount": 6100.00},
            {"date": (today - timedelta(days=1)).isoformat(), "description": "Daily sales deposit", "category": "Revenue", "amount": 2350.75},
            {"date": today.isoformat(), "description": "Metro Power & Water - Utilities", "category": "Utilities", "amount": 410.20},
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(feed, f, indent=2)
    print(f"wrote {path}")


def write_pdf_statement(rows):
    try:
        from fpdf import FPDF
    except ImportError:
        print("fpdf2 not installed -- skipping PDF generation (pip install fpdf2 to enable).")
        return

    q4 = rows[9:12]  # Oct, Nov, Dec
    revenue = sum(r["Revenue"] for r in q4)
    cogs = sum(r["COGS"] for r in q4)
    opex = sum(r["Payroll"] + r["Rent"] + r["Marketing"] + r["Utilities"] + r["OneOff"] for r in q4)
    gross_profit = revenue - cogs
    net_income = gross_profit - opex

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, COMPANY, ln=True)
    pdf.set_font("Helvetica", "", 12)
    pdf.cell(0, 8, "Q4 2025 Income Statement (Oct-Dec)", ln=True)
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 11)
    line_items = [
        ("Total Revenue", revenue),
        ("Cost of Goods Sold", -cogs),
        ("Gross Profit", gross_profit),
        ("Operating Expenses", -opex),
        ("Net Income", net_income),
    ]
    for label, value in line_items:
        pdf.cell(120, 8, label)
        pdf.cell(0, 8, f"${value:,.2f}", ln=True)
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 6, "Synthetic demo data generated for the FinSight Kaggle capstone submission. Not a real company.")

    out_path = DATA_DIR / "statements" / "aperture_q4_2025_income_statement.pdf"
    pdf.output(str(out_path))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    rows = monthly_figures()
    write_transactions_csv(rows)
    write_ledger_db(rows)
    write_live_feed_mock()
    write_pdf_statement(rows)
