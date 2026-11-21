"""Deterministic fake data so the whole app works with no bank connection.

Mirrors the user's real shape: Chase checking/savings/cards, Fidelity 401k, Vanguard Roth +
backdoor Traditional IRA + taxable brokerage, and Coinbase.
"""
import random
from datetime import date, timedelta

from .db import db, init_db, set_profile

TODAY = date.today


def _acct(c, id, inst, name, type_, subtype, bucket, balance, mask="0000", limit=None):
    c.execute(
        "INSERT OR REPLACE INTO accounts(id,item_id,institution,name,mask,type,subtype,bucket,balance,"
        "credit_limit,is_manual,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,0,datetime('now'))",
        (id, "demo-" + inst.lower(), inst, name, mask, type_, subtype, bucket, balance, limit),
    )


def seed(months: int = 18, seed_value: int = 7):
    rnd = random.Random(seed_value)
    init_db()
    today = TODAY()
    with db() as c:
        for t in ("accounts", "transactions", "holdings", "balance_history", "items"):
            c.execute(f"DELETE FROM {t}")

        for inst in ("Chase", "Fidelity", "Vanguard", "Coinbase"):
            c.execute("INSERT OR REPLACE INTO items(id,institution,last_synced) VALUES(?,?,datetime('now'))",
                      ("demo-" + inst.lower(), inst))

        _acct(c, "chk", "Chase", "Total Checking", "depository", "checking", "cash", 8420.55, "4821")
        _acct(c, "sav", "Chase", "Premier Savings", "depository", "savings", "cash", 31250.00, "9034")
        _acct(c, "csr", "Chase", "Sapphire Reserve", "credit", "credit card", "credit", 2184.37, "1177", 25000)
        _acct(c, "cff", "Chase", "Freedom Unlimited", "credit", "credit card", "credit", 612.9, "6620", 12000)
        _acct(c, "k401", "Fidelity", "Employer 401(k)", "investment", "401k", "pretax", 0, "3301")
        _acct(c, "roth", "Vanguard", "Roth IRA", "investment", "roth", "roth", 0, "7712")
        _acct(c, "tira", "Vanguard", "Traditional IRA (backdoor)", "investment", "ira", "pretax", 0, "7713")
        _acct(c, "brk", "Vanguard", "Brokerage", "investment", "brokerage", "taxable", 0, "5590")
        _acct(c, "cb", "Coinbase", "Coinbase", "crypto", "crypto exchange", "crypto", 0, "")

        holdings = {
            "k401": [("FXAIX", "Fidelity 500 Index", "mutual fund", 410.2, 215.4, 70000),
                     ("FSPSX", "Fidelity Intl Index", "mutual fund", 640.0, 52.3, 30000),
                     ("FXNAX", "Fidelity US Bond Index", "mutual fund", 800.0, 10.6, 8500)],
            "roth": [("VTI", "Vanguard Total Stock Market ETF", "etf", 160.0, 305.0, 38000),
                     ("VXUS", "Vanguard Total Intl Stock ETF", "etf", 180.0, 71.0, 12800)],
            "tira": [("VTSAX", "Vanguard Total Stock Market Admiral", "mutual fund", 12.0, 150.0, 1800),
                     ("VMFXX", "Vanguard Federal Money Market", "cash", 7000, 1.0, 7000)],
            "brk": [("VTI", "Vanguard Total Stock Market ETF", "etf", 95.0, 305.0, 29000),
                    ("VOO", "Vanguard S&P 500 ETF", "etf", 30.0, 560.0, 16800),
                    ("VGT", "Vanguard IT ETF", "etf", 12.0, 640.0, 7700)],
            "cb": [("BTC", "Bitcoin", "crypto", 0.085, 98000.0, 8330),
                   ("ETH", "Ethereum", "crypto", 1.2, 3500.0, 4200)],
        }
        basis_ratio = {"k401": 0.75, "roth": 0.7, "tira": 0.95, "brk": 0.8, "cb": 0.6}
        for acct, items in holdings.items():
            total = 0
            for tk, nm, cls, q, px, val in items:
                c.execute("INSERT INTO holdings(id,account_id,ticker,name,asset_class,quantity,price,value,cost_basis)"
                          " VALUES(?,?,?,?,?,?,?,?,?)",
                          (f"{acct}-{tk}", acct, tk, nm, cls, q, px, val, round(val * basis_ratio[acct], 2)))
                total += val
            c.execute("UPDATE accounts SET balance=? WHERE id=?", (total, acct))

        # ---- transactions ----
        txns = []

        def add(d, acct, name, amount, cat, merchant=None):
            txns.append((f"d{len(txns)}", acct, d.isoformat(), name, merchant or name, round(amount, 2), cat, 0))

        start = today - timedelta(days=int(months * 30.4))
        d = start
        while d <= today:
            # paycheck every other Friday
            if (d - start).days % 14 == 0:
                add(d, "chk", "ACME CORP PAYROLL", -4380.00, "INCOME", "Acme Corp")
            if d.day == 1:
                add(d, "chk", "LUXE APARTMENTS RENT", 2450.00, "RENT_AND_UTILITIES", "Luxe Apartments")
            if d.day == 5:
                add(d, "chk", "CITY POWER & LIGHT", rnd.uniform(70, 130), "RENT_AND_UTILITIES", "City Power & Light")
            if d.day == 9:
                add(d, "chk", "XFINITY INTERNET", 79.99, "RENT_AND_UTILITIES", "Xfinity")
            if d.day == 12:
                add(d, "chk", "VERIZON WIRELESS", 92.40, "RENT_AND_UTILITIES", "Verizon")
            if d.day == 20:  # card payments & savings transfers
                add(d, "chk", "CHASE CARD PAYMENT", 2900 + rnd.uniform(-300, 300), "CREDIT_CARD_PAYMENT")
                add(d, "chk", "TRANSFER TO VANGUARD BROKERAGE", 1000.00, "TRANSFER_OUT")
            # subscriptions on the Sapphire card
            subs = [(3, "NETFLIX.COM", 15.49, "Netflix"), (8, "SPOTIFY", 11.99, "Spotify"),
                    (14, "ICLOUD STORAGE", 2.99, "Apple iCloud"), (17, "PLANET FITNESS", 24.99, "Planet Fitness"),
                    (22, "NYTIMES DIGITAL", 17.00, "New York Times"), (25, "OPENAI CHATGPT", 20.00, "ChatGPT"),
                    (11, "HULU", 17.99, "Hulu")]
            for dom, nm, amt, merch in subs:
                if d.day == dom:
                    add(d, "csr", nm, amt, "ENTERTAINMENT", merch)
            if d.month == 3 and d.day == 15:
                add(d, "csr", "AMAZON PRIME ANNUAL", 139.00, "GENERAL_MERCHANDISE", "Amazon Prime")
            # variable spend
            if rnd.random() < 0.28:
                add(d, "cff", rnd.choice(["WHOLE FOODS", "TRADER JOES", "KROGER"]) + " #" + str(rnd.randint(100, 999)),
                    rnd.uniform(35, 160), "FOOD_AND_DRINK", rnd.choice(["Whole Foods", "Trader Joe's", "Kroger"]))
            if rnd.random() < 0.32:
                m = rnd.choice(["CHIPOTLE", "SWEETGREEN", "LOCAL BISTRO", "BLUE BOTTLE COFFEE", "DOORDASH", "STARBUCKS"])
                add(d, "csr", m, rnd.uniform(8, 78), "FOOD_AND_DRINK", m.title())
            if rnd.random() < 0.10:
                add(d, "cff", "SHELL OIL", rnd.uniform(35, 62), "TRANSPORTATION", "Shell")
            if rnd.random() < 0.13:
                m = rnd.choice(["AMAZON.COM", "TARGET", "BEST BUY", "NIKE", "HOME DEPOT"])
                add(d, "cff", m, rnd.uniform(15, 220), "GENERAL_MERCHANDISE", m.title())
            if rnd.random() < 0.025:
                add(d, "csr", rnd.choice(["DELTA AIR LINES", "MARRIOTT", "AIRBNB"]), rnd.uniform(180, 900),
                    "TRAVEL", None)
            if rnd.random() < 0.04:
                add(d, "chk", "CVS PHARMACY", rnd.uniform(10, 90), "MEDICAL", "CVS")
            if d.day == 28 and d.month in (1, 4, 7, 10):
                add(d, "sav", "INTEREST PAYMENT", -rnd.uniform(110, 130), "INCOME", "Chase")
            d += timedelta(days=1)
        c.executemany("INSERT INTO transactions(id,account_id,date,name,merchant,amount,category,pending)"
                      " VALUES(?,?,?,?,?,?,?,?)", txns)

        # ---- balance history (coarse net-worth trend) ----
        cur = {r["id"]: r["balance"] for r in c.execute("SELECT id,balance FROM accounts")}
        for i in range(months * 30, -1, -15):
            day = (today - timedelta(days=i)).isoformat()
            growth = 1 - 0.0032 * (i / 15) + rnd.uniform(-0.01, 0.01)  # ~ steady compounding + noise
            for aid, bal in cur.items():
                if aid in ("k401", "roth", "tira", "brk", "cb", "sav"):
                    c.execute("INSERT OR REPLACE INTO balance_history VALUES(?,?,?)", (day, aid, round(bal * growth, 2)))
                else:
                    c.execute("INSERT OR REPLACE INTO balance_history VALUES(?,?,?)", (day, aid, bal))

    set_profile({"age": 34, "retirement_age": 58, "gross_annual_income": 142000,
                 "annual_retirement_contribution": 38000, "state": "TX"})
