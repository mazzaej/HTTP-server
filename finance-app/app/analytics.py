"""All the number-crunching. Pure functions over a sqlite connection / plain dicts so they are easy
to test and so the chat agent and the dashboard share exactly the same math.

Sign convention (Plaid): transaction amount > 0 is money OUT, < 0 is money IN.
"""
import math
import random
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

from .db import get_profile, rows

# Not "spending" and not "income": money just moving between the user's own accounts.
NON_SPEND = {"TRANSFER_IN", "TRANSFER_OUT", "CREDIT_CARD_PAYMENT"}
INVEST_BUCKETS = ("pretax", "roth", "taxable")


def _month(d: str) -> str:
    return d[:7]


def _full_month_cutoff(months: int) -> tuple[str, str]:
    """(start, end) covering the last `months` COMPLETE calendar months."""
    first_this = date.today().replace(day=1)
    end = first_this - timedelta(days=1)
    y, m = first_this.year, first_this.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1).isoformat(), end.isoformat()


# ----------------------------------------------------------------------------- accounts / net worth
def accounts(conn) -> list[dict]:
    return rows(conn, "SELECT * FROM accounts ORDER BY institution, name")


def net_worth(conn) -> dict:
    accts = accounts(conn)
    by_bucket = defaultdict(float)
    assets = liabilities = 0.0
    for a in accts:
        if a["bucket"] in ("credit", "debt"):
            liabilities += a["balance"]
        else:
            by_bucket[a["bucket"]] += a["balance"]
            assets += a["balance"]
    hist = defaultdict(float)
    for r in conn.execute("SELECT h.date, h.balance, a.bucket FROM balance_history h JOIN accounts a ON a.id=h.account_id"):
        hist[r["date"]] += -r["balance"] if r["bucket"] in ("credit", "debt") else r["balance"]
    return {
        "net_worth": round(assets - liabilities, 2),
        "assets": round(assets, 2),
        "liabilities": round(liabilities, 2),
        "by_bucket": {k: round(v, 2) for k, v in by_bucket.items()},
        "history": [{"date": d, "net_worth": round(v, 2)} for d, v in sorted(hist.items())],
    }


def allocation(conn) -> dict:
    hs = rows(conn, "SELECT h.*, a.bucket, a.name AS account FROM holdings h JOIN accounts a ON a.id=h.account_id")
    total = sum(h["value"] for h in hs) or 1
    by_class, by_ticker = defaultdict(float), defaultdict(float)
    for h in hs:
        by_class[h["asset_class"] or "other"] += h["value"]
        by_ticker[h["ticker"] or h["name"]] += h["value"]
    gain = sum(h["value"] - h["cost_basis"] for h in hs if h["cost_basis"])
    return {
        "total_invested": round(total if hs else 0, 2),
        "unrealized_gain": round(gain, 2),
        "by_asset_class": [{"class": k, "value": round(v, 2), "pct": round(v / total * 100, 1)}
                           for k, v in sorted(by_class.items(), key=lambda x: -x[1])],
        "top_holdings": [{"ticker": k, "value": round(v, 2), "pct": round(v / total * 100, 1)}
                         for k, v in sorted(by_ticker.items(), key=lambda x: -x[1])[:12]],
    }


# ----------------------------------------------------------------------------- cash flow / spending
def _txns(conn, start: str, end: str) -> list[dict]:
    return rows(conn, "SELECT * FROM transactions WHERE date>=? AND date<=? AND pending=0", (start, end))


def monthly_cash_flow(conn, months: int = 12) -> dict:
    start, end = _full_month_cutoff(months)
    inc, spend = defaultdict(float), defaultdict(float)
    for t in _txns(conn, start, end):
        m = _month(t["date"])
        if t["category"] == "INCOME" and t["amount"] < 0:
            inc[m] += -t["amount"]
        elif t["category"] not in NON_SPEND and t["category"] != "INCOME":
            spend[m] += t["amount"]  # refunds (negative) net out
    ms = sorted(set(inc) | set(spend))
    series = [{"month": m, "income": round(inc[m], 2), "spending": round(spend[m], 2),
               "net": round(inc[m] - spend[m], 2),
               "savings_rate": round((inc[m] - spend[m]) / inc[m] * 100, 1) if inc[m] else None} for m in ms]
    n = len(ms) or 1
    ai, asp = sum(inc.values()) / n, sum(spend.values()) / n
    return {
        "window": {"start": start, "end": end, "months_with_data": len(ms)},
        "avg_monthly_income": round(ai, 2),
        "avg_monthly_spending": round(asp, 2),
        "avg_monthly_net": round(ai - asp, 2),
        "avg_savings_rate_pct": round((ai - asp) / ai * 100, 1) if ai else None,
        "months": series,
        "note": "Income = deposits categorized INCOME. Spending excludes transfers and credit-card payments "
                "(to avoid double counting). 'Net' is before any money you moved into investments.",
    }


def spending_by_category(conn, months: int = 3) -> dict:
    start, end = _full_month_cutoff(months)
    tot, cnt = defaultdict(float), defaultdict(int)
    for t in _txns(conn, start, end):
        if t["category"] in NON_SPEND or t["category"] == "INCOME":
            continue
        tot[t["category"] or "UNCATEGORIZED"] += t["amount"]
        cnt[t["category"] or "UNCATEGORIZED"] += 1
    total = sum(tot.values()) or 1
    return {
        "window": {"start": start, "end": end, "months": months},
        "total": round(sum(tot.values()), 2),
        "avg_per_month": round(sum(tot.values()) / months, 2),
        "categories": [{"category": k, "total": round(v, 2), "per_month": round(v / months, 2),
                        "pct": round(v / total * 100, 1), "transactions": cnt[k]}
                       for k, v in sorted(tot.items(), key=lambda x: -x[1])],
    }


def top_merchants(conn, months: int = 3, limit: int = 15) -> list[dict]:
    start, end = _full_month_cutoff(months)
    tot, cnt = defaultdict(float), defaultdict(int)
    for t in _txns(conn, start, end):
        if t["category"] in NON_SPEND or t["category"] == "INCOME" or t["amount"] <= 0:
            continue
        k = t["merchant"] or t["name"]
        tot[k] += t["amount"]
        cnt[k] += 1
    return [{"merchant": k, "total": round(v, 2), "transactions": cnt[k]}
            for k, v in sorted(tot.items(), key=lambda x: -x[1])[:limit]]


def search_transactions(conn, query: str = "", start: str = "", end: str = "", category: str = "",
                        min_amount: float | None = None, max_amount: float | None = None, limit: int = 50) -> list[dict]:
    sql, p = "SELECT t.*, a.name AS account FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE 1=1", []
    if query:
        sql += " AND (LOWER(t.name) LIKE ? OR LOWER(IFNULL(t.merchant,'')) LIKE ?)"
        p += [f"%{query.lower()}%"] * 2
    if start:
        sql += " AND t.date>=?"; p.append(start)
    if end:
        sql += " AND t.date<=?"; p.append(end)
    if category:
        sql += " AND t.category=?"; p.append(category.upper())
    if min_amount is not None:
        sql += " AND t.amount>=?"; p.append(min_amount)
    if max_amount is not None:
        sql += " AND t.amount<=?"; p.append(max_amount)
    sql += " ORDER BY t.date DESC LIMIT ?"
    p.append(max(1, min(limit, 200)))
    return rows(conn, sql, p)


# ----------------------------------------------------------------------------- subscriptions
_CADENCES = [("weekly", 7, 2), ("biweekly", 14, 3), ("monthly", 30.4, 5), ("quarterly", 91, 10), ("annual", 365, 20)]
_PER_MONTH = {"weekly": 52 / 12, "biweekly": 26 / 12, "monthly": 1, "quarterly": 1 / 3, "annual": 1 / 12}
_SUB_CATS = {"ENTERTAINMENT", "GENERAL_SERVICES", "PERSONAL_CARE", "GENERAL_MERCHANDISE"}


def subscriptions(conn) -> dict:
    """Detect recurring charges: same merchant, steady amount, regular cadence."""
    groups = defaultdict(list)
    for t in rows(conn, "SELECT * FROM transactions WHERE amount>0 AND pending=0 ORDER BY date"):
        if t["category"] in NON_SPEND or t["category"] == "INCOME":
            continue
        groups[(t["merchant"] or t["name"]).strip().lower()].append(t)

    found = []
    today = date.today()
    for key, ts in groups.items():
        dates = [datetime.strptime(t["date"], "%Y-%m-%d").date() for t in ts]
        amounts = [t["amount"] for t in ts]
        if len(ts) < 2:
            continue
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        med = statistics.median(gaps)
        cadence = next((n for n, c, tol in _CADENCES if abs(med - c) <= tol), None)
        if not cadence:
            continue
        if len(ts) < (2 if cadence == "annual" else 3):
            continue
        mean = statistics.mean(amounts)
        cv = statistics.pstdev(amounts) / mean if mean else 1
        if cv > 0.15:
            continue
        cad_days = dict((n, c) for n, c, _ in _CADENCES)[cadence]
        last = dates[-1]
        active = (today - last).days <= cad_days * 1.6
        cat = ts[-1]["category"]
        found.append({
            "merchant": ts[-1]["merchant"] or ts[-1]["name"],
            "category": cat,
            "kind": "subscription" if cat in _SUB_CATS else "bill",
            "cadence": cadence,
            "amount": round(statistics.mean(amounts[-3:]), 2),
            "monthly_cost": round(statistics.mean(amounts[-3:]) * _PER_MONTH[cadence], 2),
            "annual_cost": round(statistics.mean(amounts[-3:]) * _PER_MONTH[cadence] * 12, 2),
            "last_charge": last.isoformat(),
            "next_expected": (last + timedelta(days=round(cad_days))).isoformat(),
            "occurrences": len(ts),
            "active": active,
        })
    found.sort(key=lambda x: -x["monthly_cost"])
    act = [f for f in found if f["active"]]
    subs = [f for f in act if f["kind"] == "subscription"]
    return {
        "items": found,
        "active_subscription_monthly": round(sum(f["monthly_cost"] for f in subs), 2),
        "active_subscription_annual": round(sum(f["annual_cost"] for f in subs), 2),
        "active_bills_monthly": round(sum(f["monthly_cost"] for f in act if f["kind"] == "bill"), 2),
        "possibly_cancelled": [f["merchant"] for f in found if not f["active"]],
    }


# ----------------------------------------------------------------------------- loan / mortgage math
def payment(principal: float, annual_rate: float, months: int) -> float:
    if principal <= 0:
        return 0.0
    r = annual_rate / 12
    if r == 0:
        return principal / months
    return principal * r / (1 - (1 + r) ** -months)


def principal_for_payment(pmt: float, annual_rate: float, months: int) -> float:
    r = annual_rate / 12
    if r == 0:
        return pmt * months
    return pmt * (1 - (1 + r) ** -months) / r


# ----------------------------------------------------------------------------- retirement
def investable_balances(conn) -> dict:
    nw = net_worth(conn)["by_bucket"]
    return {k: nw.get(k, 0.0) for k in ("pretax", "roth", "taxable", "crypto", "cash")}


def _annual_spending(conn) -> float:
    return monthly_cash_flow(conn, 12)["avg_monthly_spending"] * 12


def _simulate(start_bal, age, retire_age, end_age, contrib, spend_need, ss, ss_age, mean, vol, tax, sims, seed):
    rnd = random.Random(seed)
    s2 = math.log(1 + (vol / (1 + mean)) ** 2)
    mu, sigma = math.log(1 + mean) - s2 / 2, math.sqrt(s2)
    years = max(end_age - age, 1)
    paths, ok, retire_bals = [], 0, []
    for _ in range(sims):
        bal, path, failed = start_bal, [], False
        for y in range(years):
            a = age + y
            path.append(bal)
            ret = math.exp(rnd.gauss(mu, sigma)) - 1
            if a < retire_age:
                bal = bal * (1 + ret) + contrib
            else:
                need = max(spend_need - (ss if a >= ss_age else 0), 0) / (1 - tax)
                bal = (bal - need) * (1 + ret) if bal > 0 else 0
                if bal <= 0:
                    bal, failed = 0, True
            if a + 1 == retire_age:
                retire_bals.append(bal)
        if not failed:
            ok += 1
        paths.append(path)
    return ok / sims, paths, retire_bals


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(int(p * len(xs)), len(xs) - 1)]


def project_retirement(conn, overrides: dict | None = None, sims: int = 1500, seed: int = 42) -> dict:
    """Monte Carlo in TODAY'S dollars (real returns). Inputs come from the profile; `overrides` lets
    the user/agent ask "what if I retire at 55?" without changing the saved profile."""
    p = {**get_profile(conn), **(overrides or {})}
    bal = investable_balances(conn)
    include_crypto = bool(p.get("include_crypto", False))
    start = bal["pretax"] + bal["roth"] + bal["taxable"] + (bal["crypto"] if include_crypto else 0)
    if "starting_balance" in p:
        start = p["starting_balance"]
    spend = p["retirement_spending_annual"] or _annual_spending(conn)
    tax = p.get("retirement_tax_rate", 0.10)
    age, ret_age, end_age = int(p["age"]), int(p["retirement_age"]), int(p["life_expectancy"])
    args = (age, ret_age, end_age)

    prob, paths, rbals = _simulate(start, *args, p["annual_retirement_contribution"], spend,
                                   p["social_security_annual"], p["social_security_start_age"],
                                   p["expected_real_return"], p["return_volatility"], tax, sims, seed)
    bands = []
    for i in range(end_age - age):
        col = [pa[i] for pa in paths]
        bands.append({"age": age + i, "p10": round(_pct(col, .1)), "p50": round(_pct(col, .5)), "p90": round(_pct(col, .9))})

    # contribution needed for an 85% success rate (bisection on a lighter simulation)
    def prob_for(c):
        return _simulate(start, *args, c, spend, p["social_security_annual"], p["social_security_start_age"],
                         p["expected_real_return"], p["return_volatility"], tax, 400, seed)[0]

    need_contrib = None
    if prob < 0.85:
        lo, hi = p["annual_retirement_contribution"], max(p["annual_retirement_contribution"] * 4, 100000)
        if prob_for(hi) >= 0.85:
            for _ in range(12):
                mid = (lo + hi) / 2
                lo, hi = (lo, mid) if prob_for(mid) >= 0.85 else (mid, hi)
            need_contrib = round(hi, -2)
    ss_net = p["social_security_annual"]
    fire = max(spend - ss_net, 0) / (1 - tax) / 0.04
    return {
        "assumptions": {
            "current_age": age, "retirement_age": ret_age, "plan_through_age": end_age,
            "starting_investable_balance": round(start, 2),
            "crypto_included": include_crypto, "crypto_balance_excluded": 0 if include_crypto else round(bal["crypto"], 2),
            "annual_contribution": p["annual_retirement_contribution"],
            "retirement_spending_annual_todays_dollars": round(spend, 2),
            "spending_source": "profile" if p["retirement_spending_annual"] else "trailing 12-mo actual spending",
            "social_security_annual": ss_net, "social_security_start_age": p["social_security_start_age"],
            "expected_real_return": p["expected_real_return"], "volatility": p["return_volatility"],
            "retirement_tax_rate": tax, "simulations": sims,
            "units": "all dollars are inflation-adjusted (today's purchasing power)",
        },
        "success_probability": round(prob, 3),
        "balance_at_retirement": {k: round(_pct(rbals, q)) for k, q in (("p10", .1), ("median", .5), ("p90", .9))} if rbals else {},
        "four_percent_target_nest_egg": round(fire),
        "median_progress_to_target_pct": round(_pct(rbals, .5) / fire * 100, 1) if rbals and fire else None,
        "contribution_needed_for_85pct_success": need_contrib,
        "bands": bands,
    }


# ----------------------------------------------------------------------------- affordability
def afford_car(conn, down_payment: float | None = None, apr: float = 0.065, term_months: int = 60,
               insurance_monthly: float = 180, fuel_maint_monthly: float = 150,
               gross_annual_income: float | None = None) -> dict:
    p = get_profile(conn)
    gross = gross_annual_income or p["gross_annual_income"]
    gm = gross / 12
    cf = monthly_cash_flow(conn, 6)
    bal = investable_balances(conn)
    emergency = cf["avg_monthly_spending"] * 6
    cash_free = max(bal["cash"] - emergency, 0)
    down = cash_free * 0.5 if down_payment is None else down_payment

    # (1) 20/4/10 rule: >=20% down, <=4yr loan, total car costs <=10% of gross monthly income
    pay_cap = max(gm * 0.10 - insurance_monthly - fuel_maint_monthly, 0)
    loan_4 = principal_for_payment(pay_cap, apr, 48)
    price_rule = loan_4 + loan_4 / 0.8 * 0.2  # loan is 80% of price
    # (2) With the user's own down payment & term, budget = 10% of gross all-in
    loan = principal_for_payment(pay_cap, apr, term_months)
    price_custom = loan + down
    # (3) Cash-flow stretch: spend up to 40% of avg monthly net cash flow on the payment
    free = max(cf["avg_monthly_net"], 0)
    stretch_pay = free * 0.4
    price_stretch = principal_for_payment(stretch_pay, apr, term_months) + down
    return {
        "inputs": {"gross_annual_income": gross, "apr": apr, "term_months": term_months, "down_payment": round(down),
                   "insurance_monthly": insurance_monthly, "fuel_maintenance_monthly": fuel_maint_monthly},
        "rule_20_4_10": {
            "max_price": round(price_rule, -2), "down_payment_20pct": round(price_rule * .2, -2),
            "max_monthly_payment": round(pay_cap), "term_months": 48,
            "explanation": "20% down, 4-year loan, all car costs (payment+insurance+fuel/maintenance) <= 10% of gross income."},
        "with_your_down_payment_and_term": {"max_price": round(price_custom, -2), "monthly_payment": round(pay_cap)},
        "cash_flow_stretch": {"max_price": round(price_stretch, -2), "monthly_payment": round(stretch_pay),
                              "explanation": "Payment capped at 40% of your average monthly free cash flow."},
        "cash_on_hand_above_6mo_emergency_fund": round(cash_free),
        "buy_with_cash_ceiling": round(cash_free),
        "avg_monthly_free_cash_flow": cf["avg_monthly_net"],
    }


def afford_house(conn, down_payment: float | None = None, rate: float = 0.0675, term_years: int = 30,
                 monthly_debts: float = 0, property_tax_rate: float = 0.015, insurance_rate: float = 0.005,
                 hoa_monthly: float = 0, gross_annual_income: float | None = None) -> dict:
    p = get_profile(conn)
    gross = gross_annual_income or p["gross_annual_income"]
    gm = gross / 12
    bal = investable_balances(conn)
    cf = monthly_cash_flow(conn, 6)
    emergency = cf["avg_monthly_spending"] * 6
    if down_payment is None:
        down_payment = max(bal["cash"] + bal["taxable"] - emergency, 0)
    n = term_years * 12

    def piti(price):
        loan = max(price - down_payment, 0)
        pmi = loan * 0.006 / 12 if loan > 0.8 * price else 0
        pi = payment(loan, rate, n)
        tax_ins = price * (property_tax_rate + insurance_rate) / 12
        return pi + tax_ins + pmi + hoa_monthly, dict(principal_interest=pi, tax_insurance=tax_ins, pmi=pmi, hoa=hoa_monthly)

    def max_price(budget):
        lo, hi = 0.0, 5_000_000.0
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if piti(mid)[0] <= budget else (lo, mid)
        return lo

    tiers = {}
    for name, front, back in (("conservative", .25, .33), ("standard", .28, .36), ("stretch", .31, .43)):
        budget = min(gm * front, gm * back - monthly_debts)
        price = max_price(max(budget, 0))
        total, parts = piti(price)
        tiers[name] = {
            "max_price": round(price, -3), "monthly_housing_payment": round(total),
            "breakdown": {k: round(v) for k, v in parts.items()},
            "down_payment_pct": round(down_payment / price * 100, 1) if price else None,
            "rule": f"housing <= {int(front*100)}% and total debt <= {int(back*100)}% of gross income",
            "cash_needed_at_close_incl_3pct_closing": round(min(down_payment, price) + price * 0.03),
        }
    rent = None
    for r in rows(conn, "SELECT amount FROM transactions WHERE category='RENT_AND_UTILITIES' AND LOWER(name) LIKE '%rent%' ORDER BY date DESC LIMIT 1"):
        rent = r["amount"]
    return {
        "inputs": {"gross_annual_income": gross, "rate": rate, "term_years": term_years, "down_payment": round(down_payment),
                   "monthly_other_debts": monthly_debts, "property_tax_rate": property_tax_rate,
                   "insurance_rate": insurance_rate, "hoa_monthly": hoa_monthly},
        "tiers": tiers,
        "current_rent_monthly": rent,
        "liquid_cash_and_taxable": round(bal["cash"] + bal["taxable"]),
        "emergency_fund_reserved": round(emergency),
        "note": "Property tax/insurance vary a lot by location; tax rate defaults to 1.5% of price. "
                "Using retirement accounts for a down payment has tax/penalty implications (Roth contributions aside).",
    }


# ----------------------------------------------------------------------------- dashboard bundle
def overview(conn) -> dict:
    nw = net_worth(conn)
    cf = monthly_cash_flow(conn, 6)
    subs = subscriptions(conn)
    cats = spending_by_category(conn, 3)
    credit = [a for a in accounts(conn) if a["bucket"] == "credit"]
    util = None
    lim = sum(a["credit_limit"] or 0 for a in credit)
    if lim:
        util = round(sum(a["balance"] for a in credit) / lim * 100, 1)
    liquid = nw["by_bucket"].get("cash", 0)
    return {
        "net_worth": nw, "cash_flow": cf, "top_categories": cats["categories"][:6],
        "subscription_monthly": subs["active_subscription_monthly"],
        "credit_utilization_pct": util,
        "emergency_fund_months": round(liquid / cf["avg_monthly_spending"], 1) if cf["avg_monthly_spending"] else None,
    }
