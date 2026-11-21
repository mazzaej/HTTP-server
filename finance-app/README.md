# My Finance: personal finance dashboard + AI agent

A private, single-user app that pulls in your accounts (Chase, Fidelity 401k, Vanguard Roth / backdoor
Traditional / brokerage, Chase cards, Coinbase) and gives you:

- **Dashboard**: net worth over time, cash flow, savings rate, spending by category/merchant, credit utilization, emergency-fund runway
- **Subscriptions**: auto-detected recurring charges and bills, annual cost, "possibly cancelled" flags
- **Investments**: allocation, top holdings, unrealized gains, balances by tax bucket (pre-tax / Roth / taxable / crypto)
- **Retirement**: Monte Carlo projection in today's dollars, success probability, "what contribution gets me to 85%"
- **Affordability**: car (20/4/10 rule + cash-flow stretch) and house (conservative / standard / stretch DTI tiers with tax, insurance, PMI)
- **Chat agent** (Claude): ask anything in plain English. It calls the same analytics as the dashboard via tools, so every number it
  quotes is computed, never recalled or guessed. It can run what-ifs ("what if I retire at 55?") and remember facts you tell it.

## Try it now (no accounts needed)

```bash
cd finance-app
pip install -r requirements.txt
DEMO_MODE=true uvicorn app.main:app --reload      # http://localhost:8000
```
Demo mode seeds ~18 months of fake data shaped like your real accounts. Add `ANTHROPIC_API_KEY` to enable chat.

Tests: `python -m pytest tests`

## Going live with real data

1. **Copy `.env.example` → `.env`** and fill it in (`DEMO_MODE=false`). The app refuses to start without
   `APP_PASSWORD` and `SESSION_SECRET`, and refuses to store Plaid tokens without `FERNET_KEY`. Generation commands are in the file.
2. **Plaid**: create an account at plaid.com, get `PLAID_CLIENT_ID` / `PLAID_SECRET`. Start in `sandbox`, then request
   **Production** access to link real institutions (check Plaid's current pricing and limits for personal/small use).
3. **OAuth banks**: Chase, Fidelity and Vanguard connect via OAuth, which requires an **https** URL registered in the Plaid
   dashboard (Allowed redirect URIs). Set it as `PLAID_REDIRECT_URI`, so deploy first, then connect.
4. **Connect** from the Accounts tab. There are two buttons because Plaid hides institutions that don't support every product
   you ask for at once: *bank/cards* (transactions) and *investments* (holdings).
5. **Anything Plaid can't reach** (a brokerage it doesn't support, or Coinbase if it isn't available to you): add it
   manually on the Accounts tab, and upload a holdings CSV (`ticker,name,quantity,price[,value,cost_basis,asset_class]`) to
   `POST /api/accounts/{id}/holdings-csv`. Coinbase also has its own read-only API if you want a direct integration later.

## Deploying to the cloud

The `Dockerfile` works on Fly.io, Render, Railway, etc. Requirements: **https**, a **persistent volume mounted at `/data`**
(the SQLite file lives there), and the env vars above as secrets. Example for Fly:

```bash
# one-time: install flyctl and `fly auth login`, then edit `app` in fly.toml
fly apps create <your-app-name>
fly volumes create data --size 1 --region iad
fly secrets set \
  APP_PASSWORD="$(python -c 'import secrets;print(secrets.token_urlsafe(16))')" \
  SESSION_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  FERNET_KEY="$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')" \
  ANTHROPIC_API_KEY=... PLAID_CLIENT_ID=... PLAID_SECRET=... \
  PLAID_REDIRECT_URI=https://<your-app-name>.fly.dev/
fly deploy
```
`fly.toml` pins a single machine, which is what you want with SQLite (don't scale it to multiple).

## Security notes

- This holds your real financial data. Use a long, unique `APP_PASSWORD`; login is rate-limited and the session cookie is
  `HttpOnly`, `SameSite=Strict`, `Secure`.
- Plaid access tokens are Fernet-encrypted at rest. Back up `FERNET_KEY`: losing it means re-linking accounts.
- The app is read-only toward your accounts: Plaid is only asked for transactions and investments, so it can't move money.
- When you chat, summaries of your financial data are sent to Anthropic's API as tool results. Don't use chat if that's not acceptable to you.
- Charts are served from this repo (no third-party CDN). Only Plaid Link loads an external script, and only on the Accounts tab.

## How it's laid out

```
app/analytics.py   all the math (net worth, cash flow, subscriptions, Monte Carlo, car/house)
app/agent.py       Claude tool-use loop + tool definitions + system prompt
app/plaid_client.py  Plaid REST client + sync into the local schema
app/demo.py        fake data generator
app/main.py        FastAPI routes + auth
static/index.html  dashboard + chat UI (no build step)
```

## Known limits / ideas for next

- Retirement model uses constant real returns, a flat effective tax rate on withdrawals, and no Roth-conversion or RMD modeling.
- Backdoor Roth pro-rata: the Traditional IRA balance is treated as pre-tax. A tracker for basis (Form 8606) would be valuable.
- Chat replies aren't streamed yet; budgets/goals, alerts (e.g. unusual charge, big balance change), scheduled daily sync, and tax-loss-harvesting hints are natural next steps.
