"""The chat agent: Claude with tool access to the same analytics the dashboard uses.

Design rule: the model never does financial arithmetic from memory. Every number it states comes
from a tool result, and the system prompt tells it so.
"""
import json
from datetime import date

import anthropic

from . import analytics as A
from .config import settings
from .db import db, get_profile, rows, set_profile

SYSTEM = """You are the user's personal CFO and financial analyst, embedded in their private finance app.
You can see their real (linked) accounts: Chase checking/savings/credit cards, Fidelity 401(k), Vanguard Roth IRA,
backdoor Traditional IRA and taxable brokerage, and Coinbase. You talk like a sharp, candid friend who happens to be
excellent with money: plain language, concrete numbers, a clear recommendation, no lecturing, no boilerplate disclaimers.

Rules:
- NEVER compute or recall financial figures from memory. Call tools for every number you cite. If a tool lacks the
  data, say so rather than guessing.
- Lead with the answer, then the 2-4 numbers that justify it, then (only if useful) one or two next steps.
- Retirement figures are in today's dollars. Say which assumptions drive a result (return, retirement age, spending)
  and what the biggest lever is. Monte Carlo results are probabilities, not promises.
- For big decisions (house, car, quitting a job) show the conservative / standard / stretch range and what it does to
  retirement trajectory when relevant (use project_retirement with overrides to test it).
- Use update_profile ONLY when the user states a fact about themselves (age, income, goals) or asks you to save one.
- You are not a licensed advisor; for tax-law or legal specifics (backdoor Roth pro-rata, wash sales, etc.) give your best
  understanding and flag when they should confirm with a CPA. Don't repeat that on every message.
- Keep replies short unless asked to go deep. Use simple markdown (short lists, bold for key numbers). Format money as $12,345.
"""

TOOLS = [
    {"name": "get_net_worth", "description": "Net worth, assets by bucket (cash, pretax, roth, taxable, crypto), liabilities, and trend history.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_accounts", "description": "List every linked account with institution, type, bucket and current balance.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_cash_flow", "description": "Monthly income, spending, net and savings rate over the last N complete months.",
     "input_schema": {"type": "object", "properties": {"months": {"type": "integer", "default": 6}}}},
    {"name": "get_spending_by_category", "description": "Spending totals by category over the last N complete months, plus top merchants.",
     "input_schema": {"type": "object", "properties": {"months": {"type": "integer", "default": 3}}}},
    {"name": "search_transactions", "description": "Search transactions. Amounts: positive = money out, negative = money in. Dates YYYY-MM-DD.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "substring of merchant/description"},
         "start": {"type": "string"}, "end": {"type": "string"},
         "category": {"type": "string", "description": "e.g. FOOD_AND_DRINK, TRAVEL, INCOME"},
         "min_amount": {"type": "number"}, "max_amount": {"type": "number"},
         "limit": {"type": "integer", "default": 25}}}},
    {"name": "get_subscriptions", "description": "Detected recurring subscriptions and bills with cadence, monthly/annual cost, and possibly-cancelled ones.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_investments", "description": "Asset allocation, top holdings and unrealized gains across all investment accounts, plus balances by tax bucket.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "project_retirement", "description": "Monte Carlo retirement projection in today's dollars. Uses saved profile; pass overrides to test what-ifs "
     "(retirement_age, annual_retirement_contribution, retirement_spending_annual, expected_real_return, return_volatility, social_security_annual, include_crypto, age, life_expectancy).",
     "input_schema": {"type": "object", "properties": {"overrides": {"type": "object"}}}},
    {"name": "afford_car", "description": "How much car the user can afford: 20/4/10 rule, with-own-terms, and cash-flow stretch.",
     "input_schema": {"type": "object", "properties": {
         "down_payment": {"type": "number"}, "apr": {"type": "number", "description": "e.g. 0.065"},
         "term_months": {"type": "integer"}, "insurance_monthly": {"type": "number"},
         "fuel_maint_monthly": {"type": "number"}, "gross_annual_income": {"type": "number"}}}},
    {"name": "afford_house", "description": "How much house the user can afford at conservative/standard/stretch DTI tiers incl. taxes, insurance, PMI.",
     "input_schema": {"type": "object", "properties": {
         "down_payment": {"type": "number"}, "rate": {"type": "number", "description": "e.g. 0.0675"},
         "term_years": {"type": "integer"}, "monthly_debts": {"type": "number"},
         "property_tax_rate": {"type": "number"}, "insurance_rate": {"type": "number"},
         "hoa_monthly": {"type": "number"}, "gross_annual_income": {"type": "number"}}}},
    {"name": "get_profile", "description": "Saved personal assumptions (age, retirement age, income, contributions, return assumptions).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "update_profile", "description": "Persist personal assumptions. Keys: age, retirement_age, life_expectancy, gross_annual_income, "
     "annual_retirement_contribution, retirement_spending_annual, expected_real_return, return_volatility, social_security_annual, "
     "social_security_start_age, state, notes.",
     "input_schema": {"type": "object", "properties": {"updates": {"type": "object"}}, "required": ["updates"]}},
]


def run_tool(name: str, args: dict):
    with db() as c:
        if name == "get_net_worth":
            nw = A.net_worth(c)
            h = nw["history"]
            nw["history"] = h[:: max(len(h) // 12, 1)]  # keep the payload small
            return nw
        if name == "get_accounts":
            return [{k: a[k] for k in ("institution", "name", "subtype", "bucket", "balance", "credit_limit")} for a in A.accounts(c)]
        if name == "get_cash_flow":
            return A.monthly_cash_flow(c, int(args.get("months", 6)))
        if name == "get_spending_by_category":
            m = int(args.get("months", 3))
            return {**A.spending_by_category(c, m), "top_merchants": A.top_merchants(c, m, 10)}
        if name == "search_transactions":
            rs = A.search_transactions(c, **{k: v for k, v in args.items() if k in
                                             ("query", "start", "end", "category", "min_amount", "max_amount", "limit")})
            return {"count": len(rs), "transactions": [{k: r[k] for k in ("date", "name", "merchant", "amount", "category", "account")} for r in rs]}
        if name == "get_subscriptions":
            return A.subscriptions(c)
        if name == "get_investments":
            return {**A.allocation(c), "balances_by_bucket": A.investable_balances(c)}
        if name == "project_retirement":
            r = A.project_retirement(c, args.get("overrides"), sims=1000)
            bands = r.pop("bands")
            r["milestones"] = [b for b in bands if b["age"] % 5 == 0]
            return r
        if name == "afford_car":
            return A.afford_car(c, **args)
        if name == "afford_house":
            return A.afford_house(c, **args)
        if name == "get_profile":
            return get_profile(c)
    if name == "update_profile":
        return set_profile(args.get("updates", {}))
    return {"error": f"unknown tool {name}"}


def chat(user_message: str, max_steps: int = 8) -> dict:
    if not settings.anthropic_key:
        return {"reply": "The chat agent needs an ANTHROPIC_API_KEY. Set it in your environment and restart.", "tools_used": []}
    client = anthropic.Anthropic(api_key=settings.anthropic_key)
    with db() as c:
        hist = rows(c, "SELECT role, content FROM chat ORDER BY id DESC LIMIT 16")[::-1]
        prof = get_profile(c)
        c.execute("INSERT INTO chat(role,content) VALUES('user',?)", (user_message,))
    msgs = [{"role": h["role"], "content": h["content"]} for h in hist]
    msgs.append({"role": "user", "content": user_message})
    system = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}},
              {"type": "text", "text": f"Today is {date.today().isoformat()}. Saved profile: {json.dumps(prof)}"}]

    used, reply = [], ""
    for _ in range(max_steps):
        resp = client.messages.create(model=settings.model, max_tokens=2500, system=system, tools=TOOLS, messages=msgs)
        msgs.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            reply = "".join(b.text for b in resp.content if b.type == "text")
            break
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                used.append(b.name)
                try:
                    out = json.dumps(run_tool(b.name, b.input or {}), default=str)
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
                except Exception as e:
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"Error: {e}", "is_error": True})
        msgs.append({"role": "user", "content": results})
    else:
        reply = "I hit my step limit before finishing. Try asking a narrower question."
    with db() as c:
        c.execute("INSERT INTO chat(role,content) VALUES('assistant',?)", (reply,))
    return {"reply": reply, "tools_used": used}
