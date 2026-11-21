import csv
import io
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, analytics as A, demo, plaid_client
from .config import settings
from .db import db, get_profile, init_db, rows, set_profile
from .security import check_password, make_session, valid_session

STATIC = Path(__file__).resolve().parent.parent / "static"
_fails: list[float] = []


@asynccontextmanager
async def lifespan(_app):
    startup()
    yield


def startup():
    if not settings.demo_mode:
        if not settings.app_password:
            raise RuntimeError("APP_PASSWORD is required when DEMO_MODE is off (this app holds real financial data).")
        if settings.session_secret == "dev-only-insecure-secret":
            raise RuntimeError("Set SESSION_SECRET to a long random string.")
    init_db()
    if settings.demo_mode:
        with db() as c:
            empty = c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
        if empty:
            demo.seed()


app = FastAPI(title="Personal Finance", docs_url=None, redoc_url=None, lifespan=lifespan)


def auth(request: Request):
    if settings.demo_mode and not settings.app_password:
        return
    if not valid_session(request.cookies.get("session")):
        raise HTTPException(401, "Not logged in")


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login, response: Response):
    now = time.time()
    _fails[:] = [t for t in _fails if now - t < 300]
    if len(_fails) >= 8:
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
    if not check_password(body.password):
        _fails.append(now)
        time.sleep(1)
        raise HTTPException(401, "Wrong password")
    response.set_cookie("session", make_session(), httponly=True, samesite="strict",
                        secure=not settings.demo_mode, max_age=60 * 60 * 24 * 14)
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    open_ = settings.demo_mode and not settings.app_password
    return {"authenticated": open_ or valid_session(request.cookies.get("session")),
            "demo": settings.demo_mode, "plaid": settings.plaid_configured, "ai": bool(settings.anthropic_key)}


# ---------------------------------------------------------------- read endpoints
@app.get("/api/overview", dependencies=[Depends(auth)])
def overview():
    with db() as c:
        return A.overview(c)


@app.get("/api/accounts", dependencies=[Depends(auth)])
def accounts():
    with db() as c:
        return {"accounts": A.accounts(c), "items": rows(c, "SELECT id,institution,last_synced FROM items")}


@app.get("/api/spending", dependencies=[Depends(auth)])
def spending(months: int = 3):
    with db() as c:
        return {**A.spending_by_category(c, months), "merchants": A.top_merchants(c, months),
                "cash_flow": A.monthly_cash_flow(c, 12)}


@app.get("/api/transactions", dependencies=[Depends(auth)])
def transactions(q: str = "", category: str = "", start: str = "", end: str = "", limit: int = 100):
    with db() as c:
        return A.search_transactions(c, q, start, end, category, limit=limit)


@app.get("/api/subscriptions", dependencies=[Depends(auth)])
def subscriptions():
    with db() as c:
        return A.subscriptions(c)


@app.get("/api/investments", dependencies=[Depends(auth)])
def investments():
    with db() as c:
        return {**A.allocation(c), "balances": A.investable_balances(c)}


@app.post("/api/retirement", dependencies=[Depends(auth)])
def retirement(overrides: dict | None = None):
    with db() as c:
        return A.project_retirement(c, overrides)


@app.post("/api/afford/car", dependencies=[Depends(auth)])
def afford_car(args: dict | None = None):
    with db() as c:
        return A.afford_car(c, **(args or {}))


@app.post("/api/afford/house", dependencies=[Depends(auth)])
def afford_house(args: dict | None = None):
    with db() as c:
        return A.afford_house(c, **(args or {}))


@app.get("/api/profile", dependencies=[Depends(auth)])
def profile():
    return get_profile()


@app.put("/api/profile", dependencies=[Depends(auth)])
def put_profile(updates: dict):
    return set_profile(updates)


# ---------------------------------------------------------------- chat
class ChatIn(BaseModel):
    message: str


@app.post("/api/chat", dependencies=[Depends(auth)])
def chat(body: ChatIn):
    if not body.message.strip():
        raise HTTPException(400, "Empty message")
    return agent.chat(body.message.strip())


@app.get("/api/chat/history", dependencies=[Depends(auth)])
def chat_history():
    with db() as c:
        return rows(c, "SELECT role, content FROM chat ORDER BY id DESC LIMIT 60")[::-1]


@app.delete("/api/chat", dependencies=[Depends(auth)])
def chat_clear():
    with db() as c:
        c.execute("DELETE FROM chat")
    return {"ok": True}


# ---------------------------------------------------------------- Plaid
class LinkKind(BaseModel):
    kind: str = "bank"


class Exchange(BaseModel):
    public_token: str
    institution: str = ""


def _plaid(fn, *a):
    try:
        return fn(*a)
    except (plaid_client.PlaidError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/plaid/link-token", dependencies=[Depends(auth)])
def link_token(body: LinkKind):
    return {"link_token": _plaid(plaid_client.create_link_token, body.kind)}


@app.post("/api/plaid/exchange", dependencies=[Depends(auth)])
def exchange(body: Exchange):
    _plaid(plaid_client.exchange_public_token, body.public_token, body.institution)
    return _plaid(plaid_client.sync_all)


@app.post("/api/plaid/sync", dependencies=[Depends(auth)])
def sync():
    return _plaid(plaid_client.sync_all)


# ---------------------------------------------------------------- manual fallbacks (e.g. if an institution isn't on Plaid)
class ManualAccount(BaseModel):
    id: str | None = None
    institution: str
    name: str
    bucket: str  # cash | pretax | roth | taxable | crypto | credit | debt
    balance: float


@app.post("/api/accounts/manual", dependencies=[Depends(auth)])
def manual_account(a: ManualAccount):
    if a.bucket not in ("cash", "pretax", "roth", "taxable", "crypto", "credit", "debt"):
        raise HTTPException(400, "Bad bucket")
    aid = a.id or f"manual-{a.institution}-{a.name}".lower().replace(" ", "-")
    typ = {"cash": "depository", "credit": "credit", "debt": "loan", "crypto": "crypto"}.get(a.bucket, "investment")
    with db() as c:
        c.execute("INSERT INTO items(id,institution) VALUES('manual','Manual') ON CONFLICT(id) DO NOTHING")
        c.execute("INSERT INTO accounts(id,item_id,institution,name,type,subtype,bucket,balance,is_manual,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?,1,datetime('now')) ON CONFLICT(id) DO UPDATE SET balance=excluded.balance,"
                  "updated_at=excluded.updated_at,bucket=excluded.bucket,name=excluded.name",
                  (aid, "manual", a.institution, a.name, typ, a.bucket, a.bucket, a.balance))
        c.execute("INSERT OR REPLACE INTO balance_history VALUES(date('now'),?,?)", (aid, a.balance))
    return {"ok": True, "id": aid}


@app.post("/api/accounts/{account_id}/holdings-csv", dependencies=[Depends(auth)])
async def holdings_csv(account_id: str, file: UploadFile):
    """CSV with header: ticker,name,quantity,price[,value,cost_basis,asset_class]. Replaces holdings and balance."""
    text = (await file.read()).decode("utf-8-sig")
    rd = csv.DictReader(io.StringIO(text))
    with db() as c:
        if not c.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
            raise HTTPException(404, "No such account")
        c.execute("DELETE FROM holdings WHERE account_id=?", (account_id,))
        total = 0.0
        for i, r in enumerate(rd):
            try:
                q, px = float(r.get("quantity") or 0), float(r.get("price") or 0)
                val = float(r["value"]) if r.get("value") else q * px
                cb = float(r["cost_basis"]) if r.get("cost_basis") else None
            except ValueError:
                raise HTTPException(400, f"Bad number on row {i + 2}")
            c.execute("INSERT INTO holdings(id,account_id,ticker,name,asset_class,quantity,price,value,cost_basis)"
                      " VALUES(?,?,?,?,?,?,?,?,?)",
                      (f"{account_id}-csv-{i}", account_id, r.get("ticker"), r.get("name"),
                       r.get("asset_class") or "other", q, px, val, cb))
            total += val
        c.execute("UPDATE accounts SET balance=?, updated_at=datetime('now') WHERE id=?", (total, account_id))
        c.execute("INSERT OR REPLACE INTO balance_history VALUES(date('now'),?,?)", (account_id, total))
    return {"ok": True, "total": total}


@app.post("/api/demo/reset", dependencies=[Depends(auth)])
def demo_reset():
    if not settings.demo_mode:
        raise HTTPException(403, "Only available in demo mode")
    demo.seed()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
