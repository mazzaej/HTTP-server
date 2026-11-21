import os
import tempfile

import pytest
from fastapi.testclient import TestClient

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DEMO_MODE"] = "true"
os.environ.pop("APP_PASSWORD", None)

from app import analytics as A  # noqa: E402
from app.db import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_overview_and_networth(client):
    o = client.get("/api/overview").json()
    nw = o["net_worth"]
    assert nw["net_worth"] == pytest.approx(nw["assets"] - nw["liabilities"], abs=0.01)
    assert nw["by_bucket"]["pretax"] > 0 and nw["by_bucket"]["roth"] > 0
    assert 0 < o["cash_flow"]["avg_savings_rate_pct"] < 100


def test_transfers_and_card_payments_not_counted_as_spending(client):
    with db() as c:
        cats = {x["category"] for x in A.spending_by_category(c, 6)["categories"]}
    assert not cats & {"TRANSFER_OUT", "TRANSFER_IN", "CREDIT_CARD_PAYMENT", "INCOME"}


def test_subscriptions_found(client):
    s = client.get("/api/subscriptions").json()
    names = {i["merchant"] for i in s["items"] if i["kind"] == "subscription"}
    assert {"Netflix", "Spotify", "Hulu"} <= names
    assert 100 < s["active_subscription_monthly"] < 200


def test_loan_math_roundtrip():
    p = A.payment(300000, 0.06, 360)
    assert p == pytest.approx(1798.65, abs=0.01)
    assert A.principal_for_payment(p, 0.06, 360) == pytest.approx(300000, abs=0.5)


def test_retirement_monotonic(client):
    base = client.post("/api/retirement", json={}).json()
    later = client.post("/api/retirement", json={"retirement_age": 65}).json()
    early = client.post("/api/retirement", json={"retirement_age": 45}).json()
    assert later["success_probability"] >= base["success_probability"] >= early["success_probability"]
    assert base["bands"][0]["p50"] > 0


def test_house_tiers_ordered(client):
    t = client.post("/api/afford/house", json={}).json()["tiers"]
    assert t["conservative"]["max_price"] < t["standard"]["max_price"] < t["stretch"]["max_price"]


def test_car_rule(client):
    r = client.post("/api/afford/car", json={}).json()
    assert r["rule_20_4_10"]["max_price"] > 0


def test_profile_roundtrip(client):
    assert client.put("/api/profile", json={"age": 41, "bogus": 1}).json()["age"] == 41
    assert "bogus" not in client.get("/api/profile").json()


def test_agent_tools_run():
    from app import agent
    for name in ("get_net_worth", "get_accounts", "get_cash_flow", "get_spending_by_category", "get_subscriptions",
                 "get_investments", "project_retirement", "afford_car", "afford_house", "get_profile"):
        agent.run_tool(name, {})
    r = agent.run_tool("search_transactions", {"query": "netflix", "limit": 3})
    assert r["count"] == 3


def test_chat_without_key_is_graceful(client):
    os.environ.pop("ANTHROPIC_API_KEY", None)
    assert "ANTHROPIC_API_KEY" in client.post("/api/chat", json={"message": "hi"}).json()["reply"]


def test_auth_enforced_when_password_set(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    with TestClient(app, base_url="https://testserver") as client:  # Secure cookie needs https
        assert client.get("/api/overview").status_code == 401
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
        assert client.post("/api/login", json={"password": "hunter2"}).status_code == 200
        assert client.get("/api/overview").status_code == 200


def test_agent_tool_loop_with_fake_model(client, monkeypatch):
    """Model asks for a tool, gets real analytics output back, then answers. Verifies the loop plumbing."""
    from types import SimpleNamespace as NS

    from app import agent

    seen = {}

    class FakeMessages:
        def __init__(self):
            self.n = 0

        def create(self, **kw):
            self.n += 1
            if self.n == 1:
                return NS(stop_reason="tool_use", content=[
                    NS(type="tool_use", id="t1", name="get_subscriptions", input={})])
            last = kw["messages"][-1]["content"][0]
            seen["tool_result"] = last["content"]
            return NS(stop_reason="end_turn", content=[NS(type="text", text="You pay $110/mo in subscriptions.")])

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.setattr(agent.anthropic, "Anthropic", lambda api_key: NS(messages=FakeMessages()))
    r = client.post("/api/chat", json={"message": "what do I pay in subscriptions?"}).json()
    assert r["tools_used"] == ["get_subscriptions"]
    assert "Netflix" in seen["tool_result"]
    assert "$110" in r["reply"]
    hist = client.get("/api/chat/history").json()
    assert hist[-1]["role"] == "assistant"
