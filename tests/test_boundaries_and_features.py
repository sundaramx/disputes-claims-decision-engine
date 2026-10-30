"""Anti-leakage, layer-boundary, and API tests."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.decisioning.features.extract import extract_features
from app.domain.enums import ClaimReason, PaymentChannel
from app.domain.models import Account, Claim, Customer, Transaction
from app.store.db import Store

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]


def _dt(y, m, d):
    return datetime(y, m, d, 12, tzinfo=UTC)


def test_features_ignore_post_filing_data():
    """As-of-filed rule: transactions and claims dated after filing must not move
    any feature (critique finding #6)."""
    store = Store(":memory:")
    cust = Customer(id="cu1", name="X", opened_at=_dt(2023, 1, 1), tenure_days=1000)
    acct = Account(id="a1", customer_id="cu1",
                   channel_type=PaymentChannel.CARD_DEBIT, opened_at=_dt(2023, 1, 1))
    store.put_customer(cust)
    store.put_account(acct)
    txn = Transaction(id="t-disputed", account_id="a1", customer_id="cu1",
                      merchant_id="M1", merchant_name="M", mcc="5999", amount=100,
                      channel=PaymentChannel.CARD_DEBIT, device_id="D1",
                      ip_prefix="IP1", occurred_at=_dt(2026, 1, 2), disputed=True)
    store.put_transaction(txn)
    for i in range(3):
        store.put_transaction(Transaction(
            id=f"t-old-{i}", account_id="a1", customer_id="cu1", merchant_id="M1",
            merchant_name="M", mcc="5999", amount=50,
            channel=PaymentChannel.CARD_DEBIT, device_id="D1", ip_prefix="IP1",
            occurred_at=_dt(2026, 1, 2) - timedelta(days=150 + i * 30)))
    claim = Claim(id="cl1", account_id="a1", customer_id="cu1",
                  transaction_id="t-disputed", reason=ClaimReason.FRAUD_UNAUTHORIZED,
                  channel=PaymentChannel.CARD_DEBIT, amount=100,
                  filed_at=_dt(2026, 1, 10))
    store.put_claim(claim)
    before = extract_features(store, claim, cust, acct, txn)

    # inject post-filing history that would look like strong misuse evidence
    for i in range(5):
        store.put_transaction(Transaction(
            id=f"t-post-{i}", account_id="a1", customer_id="cu1", merchant_id="M1",
            merchant_name="M", mcc="5999", amount=50,
            channel=PaymentChannel.CARD_DEBIT, device_id="D1", ip_prefix="IP1",
            occurred_at=_dt(2026, 2, 1) + timedelta(days=i)))
        store.put_claim(Claim(id=f"cl-post-{i}", account_id="a1", customer_id="cu1",
                              transaction_id=f"t-post-{i}",
                              reason=ClaimReason.FRAUD_UNAUTHORIZED,
                              channel=PaymentChannel.CARD_DEBIT, amount=50,
                              filed_at=_dt(2026, 2, 10) + timedelta(days=i)))
    after = extract_features(store, claim, cust, acct, txn)
    assert before == after, "post-filing data leaked into features"


def test_layer_boundary_chassis_never_imports_decisioning_and_vice_versa():
    """The paper's narrow-interface rule, mechanized (critique finding #17)."""
    def imports_of(pkg: str) -> list[tuple[str, str]]:
        hits = []
        for py in (ROOT / "app" / pkg).rglob("*.py"):
            text = py.read_text()
            for m in re.finditer(r"^\s*(?:from|import)\s+(app\.[\w.]+)", text,
                                 re.MULTILINE):
                hits.append((str(py.relative_to(ROOT)), m.group(1)))
        return hits

    for f, mod in imports_of("chassis"):
        assert not mod.startswith("app.decisioning"), f"{f} imports {mod}"
        assert not mod.startswith("app.network"), f"{f} imports {mod}"
    for f, mod in imports_of("decisioning"):
        assert not mod.startswith("app.chassis"), f"{f} imports {mod}"
    for f, mod in imports_of("network"):
        assert not mod.startswith("app.chassis"), f"{f} imports {mod}"
        assert not mod.startswith("app.decisioning"), f"{f} imports {mod}"


def test_api_endpoints(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import server

    monkeypatch.setenv("SIM_SEED", "5")
    monkeypatch.setenv("SIM_CLAIMS", "150")
    monkeypatch.setenv("SIM_CUSTOMERS", "150")
    monkeypatch.setenv("SIM_DAYS", "70")
    monkeypatch.setenv("SIM_LIVE_SECONDS", "0")
    with TestClient(server.app) as client:
        assert client.get("/api/sim").json()["enabled"] is False
        k = client.get("/api/kpis").json()
        assert k["compliance"]["pc_on_time_rate"] == 100.0
        assert k["experiments"]["policy_pack"]["id"] == "adjudication"

        lv = client.get("/api/ledger/verify").json()
        assert lv["valid"]

        cs = client.get("/api/cases?limit=5").json()
        assert cs["count"] > 0
        cid = cs["cases"][0]["id"]
        detail = client.get(f"/api/cases/{cid}").json()
        assert detail["summary"]["id"] == cid
        assert "true_type" not in detail["claim"], "ground truth must never leak"
        assert detail["ledger"], "case must be reconstructable from ledger"

        # review action only valid on PENDING_REVIEW cases
        pending = client.get("/api/cases?queue=HUMAN_REVIEW&open_only=true").json()
        if pending["count"]:
            pid = pending["cases"][0]["id"]
            r = client.post(f"/api/cases/{pid}/review",
                            json={"action": "approve_pay", "rationale": "test",
                                  "reviewer": "t"}).json()
            assert r["ok"] and r["case"]["status"] == "RESOLVED_PAID"
        resolved = [c for c in cs["cases"] if c["resolved_at"]]
        if resolved:
            r = client.post(f"/api/cases/{resolved[0]['id']}/review",
                            json={"action": "approve_pay", "rationale": "x",
                                  "reviewer": "t"})
            assert r.status_code == 409
        assert client.get("/api/ledger/verify").json()["valid"]


def test_live_simulation_advances_and_stops(monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from app.api import server

    monkeypatch.setenv("SIM_SEED", "5")
    monkeypatch.setenv("SIM_CLAIMS", "150")
    monkeypatch.setenv("SIM_CUSTOMERS", "150")
    monkeypatch.setenv("SIM_DAYS", "70")
    monkeypatch.setenv("SIM_TICK_SECONDS", "0.05")
    monkeypatch.setenv("SIM_LIVE_SECONDS", "1")
    with TestClient(server.app) as client:
        start = client.get("/api/kpis").json()
        time.sleep(1.5)                       # live window elapses on its own
        s = client.get("/api/sim").json()
        assert s["enabled"] and not s["running"] and s["days_run"] > 0
        k = client.get("/api/kpis").json()
        assert k["as_of"] > start["as_of"]
        assert k["portfolio"]["total_claims"] > start["portfolio"]["total_claims"]
        assert k["compliance"]["pc_on_time_rate"] == 100.0   # invariants hold live
        assert client.get("/api/ledger/verify").json()["valid"]

        days = s["days_run"]
        assert client.post("/api/sim/resume").json()["running"]
        assert client.post("/api/sim/pause").json()["running"] is False
        time.sleep(0.2)
        assert client.get("/api/sim").json()["days_run"] - days <= 1
        assert client.post("/api/sim/bogus").status_code == 404
