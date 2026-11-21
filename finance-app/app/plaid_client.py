"""Thin Plaid REST client (httpx) + sync logic that normalizes Plaid data into our schema."""
from datetime import date

import httpx

from .config import settings
from .db import db
from .security import decrypt, encrypt

HOSTS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}


class PlaidError(Exception):
    pass


def _post(path: str, body: dict) -> dict:
    if not settings.plaid_configured:
        raise PlaidError("Plaid is not configured (set PLAID_CLIENT_ID and PLAID_SECRET).")
    payload = {"client_id": settings.plaid_client_id, "secret": settings.plaid_secret, **body}
    r = httpx.post(HOSTS.get(settings.plaid_env, HOSTS["sandbox"]) + path, json=payload, timeout=60)
    if r.status_code >= 400:
        try:
            e = r.json()
            raise PlaidError(f"{e.get('error_code')}: {e.get('error_message')}")
        except ValueError:
            raise PlaidError(f"Plaid HTTP {r.status_code}")
    return r.json()


def create_link_token(kind: str = "bank") -> str:
    """kind='bank'  -> transactions (Chase checking/savings/cards)
       kind='invest'-> investments  (Fidelity, Vanguard, Coinbase)
    Separate flows because Plaid hides institutions that don't support every *required* product."""
    body = {
        "client_name": "My Finance",
        "user": {"client_user_id": "owner"},
        "country_codes": ["US"],
        "language": "en",
    }
    if kind == "invest":
        body["products"] = ["investments"]
    else:
        body["products"] = ["transactions"]
        body["transactions"] = {"days_requested": 730}
    if settings.plaid_redirect_uri:
        body["redirect_uri"] = settings.plaid_redirect_uri
    return _post("/link/token/create", body)["link_token"]


# ---- classification -------------------------------------------------------------------------------
def classify(type_: str, subtype: str | None) -> tuple[str, str]:
    """Returns (our_type, bucket)."""
    st = (subtype or "").lower()
    if type_ == "depository":
        return "depository", "cash"
    if type_ == "credit":
        return "credit", "credit"
    if type_ == "loan":
        return "loan", "debt"
    if type_ == "investment" or type_ == "other":
        if "crypto" in st:
            return "crypto", "crypto"
        if "roth" in st:
            return "investment", "roth"
        if st in ("401k", "403b", "401a", "403(b)", "457b", "ira", "sep ira", "simple ira", "sarsep", "pension",
                  "keogh", "retirement", "profit sharing plan", "thrift savings plan"):
            return "investment", "pretax"
        if type_ == "investment":
            return "investment", "taxable"
    return "other", "other"


def _category(pfc: dict | None, fallback_amount: float) -> str:
    if not pfc:
        return "UNCATEGORIZED"
    if pfc.get("detailed") == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT":
        return "CREDIT_CARD_PAYMENT"
    return pfc.get("primary") or "UNCATEGORIZED"


# ---- exchange + sync ------------------------------------------------------------------------------
def exchange_public_token(public_token: str, institution: str) -> str:
    r = _post("/item/public_token/exchange", {"public_token": public_token})
    with db() as c:
        c.execute("INSERT OR REPLACE INTO items(id,institution,access_token_enc) VALUES(?,?,?)",
                  (r["item_id"], institution or "Unknown", encrypt(r["access_token"])))
    return r["item_id"]


def sync_all() -> dict:
    with db() as c:
        items = [dict(r) for r in c.execute("SELECT * FROM items WHERE access_token_enc IS NOT NULL")]
    out = {"items": 0, "errors": []}
    for it in items:
        try:
            sync_item(it)
            out["items"] += 1
        except Exception as e:  # keep going; report per-institution
            out["errors"].append(f"{it['institution']}: {e}")
    return out


def sync_item(it: dict):
    token = decrypt(it["access_token_enc"])
    today = date.today().isoformat()
    inst = it["institution"]

    accts = _post("/accounts/get", {"access_token": token})["accounts"]
    with db() as c:
        for a in accts:
            typ, bucket = classify(a["type"], a.get("subtype"))
            bal = a["balances"].get("current") or 0
            c.execute(
                "INSERT INTO accounts(id,item_id,institution,name,mask,type,subtype,bucket,balance,credit_limit,is_manual,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,0,datetime('now')) ON CONFLICT(id) DO UPDATE SET "
                "name=excluded.name,balance=excluded.balance,credit_limit=excluded.credit_limit,"
                "bucket=excluded.bucket,type=excluded.type,subtype=excluded.subtype,updated_at=excluded.updated_at",
                (a["account_id"], it["id"], inst, a.get("official_name") or a["name"], a.get("mask"), typ,
                 a.get("subtype"), bucket, bal, a["balances"].get("limit")))
            c.execute("INSERT OR REPLACE INTO balance_history VALUES(?,?,?)", (today, a["account_id"], bal))
    kinds = {a["type"] for a in accts}

    # transactions (depository + credit)
    if kinds & {"depository", "credit"}:
        cursor = it["cursor"]
        while True:
            body = {"access_token": token, "count": 500}
            if cursor:
                body["cursor"] = cursor
            r = _post("/transactions/sync", body)
            with db() as c:
                for t in r["added"] + r["modified"]:
                    c.execute(
                        "INSERT OR REPLACE INTO transactions(id,account_id,date,name,merchant,amount,category,pending)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (t["transaction_id"], t["account_id"], t["date"], t["name"],
                         t.get("merchant_name") or t["name"], t["amount"],
                         _category(t.get("personal_finance_category"), t["amount"]), 1 if t["pending"] else 0))
                for t in r["removed"]:
                    c.execute("DELETE FROM transactions WHERE id=?", (t["transaction_id"],))
                cursor = r["next_cursor"]
                c.execute("UPDATE items SET cursor=? WHERE id=?", (cursor, it["id"]))
            if not r["has_more"]:
                break

    # holdings (investment + crypto)
    if kinds & {"investment", "other"}:
        try:
            r = _post("/investments/holdings/get", {"access_token": token})
        except PlaidError as e:
            if "PRODUCT_NOT_READY" in str(e) or "PRODUCTS_NOT_SUPPORTED" in str(e) or "NO_INVESTMENT" in str(e):
                r = None
            else:
                raise
        if r:
            secs = {s["security_id"]: s for s in r["securities"]}
            with db() as c:
                for aid in {h["account_id"] for h in r["holdings"]}:
                    c.execute("DELETE FROM holdings WHERE account_id=?", (aid,))
                for i, h in enumerate(r["holdings"]):
                    s = secs.get(h["security_id"], {})
                    cls = "crypto" if s.get("type") == "cryptocurrency" else (s.get("type") or "other")
                    c.execute(
                        "INSERT OR REPLACE INTO holdings(id,account_id,ticker,name,asset_class,quantity,price,value,cost_basis)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"{h['account_id']}-{h['security_id']}-{i}", h["account_id"],
                         s.get("ticker_symbol") or s.get("name"), s.get("name"), cls, h["quantity"],
                         h["institution_price"], h["institution_value"], h.get("cost_basis")))
    with db() as c:
        c.execute("UPDATE items SET last_synced=datetime('now') WHERE id=?", (it["id"],))
