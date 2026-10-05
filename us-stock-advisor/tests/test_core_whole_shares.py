"""Phase 0: whole shares, combined QQQ+QQQM core sleeve, residual + cash triggers."""
from __future__ import annotations

import json
import random

import pytest

import config as C
import core as CORE
from helpers import (HISTORY_CSV, LIVE_PX, make_baseline, make_prices_csv, portfolio, run,
                     write_json)

EPS = 1e-6


def core_orders(plan):
    return [o for o in plan["orders"] if o["ticker"] in C.CORE_TICKERS]


def assert_whole(o):
    assert float(o["shares"]).is_integer(), o
    assert o["usd"] == pytest.approx(round(o["shares"] * o["limit_ref_price"], 2), abs=0.005)


# ------------------------------------------------------------------ live book
def test_live_book_buys_exactly_one_qqqm():
    """QQQ 2 sh @737.93 + cash 392.25, QQQM @303.83 -> exactly BUY QQQM 1."""
    b = make_baseline()
    assert [(o["ticker"], o["action"], o["shares"]) for o in b["orders"]] == [("QQQM", "BUY", 1.0)]
    o = b["orders"][0]
    assert_whole(o)
    assert o["usd"] == pytest.approx(303.83)
    assert o["sleeve"] == "core" and o["whole_shares"] is True and o["stop"] is None
    assert o["usd"] * (1 + C.WHOLE_SHARE_PRICE_BUFFER_PCT) <= b["portfolio"]["cash_usd"] + EPS
    ex = b["execution"]
    assert ex["whole_shares"] is True
    r = ex["residual"]
    assert r["unavoidable"] is True and r["deployable"] is False
    assert r["cash_usd"] == pytest.approx(392.25 - 303.83, abs=0.01)
    assert ex["post_trade"]["positions"]["QQQM"]["shares"] == 1.0
    assert ex["post_trade"]["positions"]["QQQ"]["shares"] == 2.0
    # combined core: target allocation is ONE core line
    assert b["targets"]["target_allocation"] == {"CORE": 100.0}
    assert b["targets"]["core_tickers"] == list(C.CORE_TICKERS)
    assert b["prices"]["QQQM"] == pytest.approx(303.83)
    # pre-trade: the excess IS deployable (a whole share fits) -> a real trigger
    assert b["events"]["cash_over_max"] is True
    assert b["events"]["cash_over_max_unavoidable"] is False


def test_post_trade_book_is_quiet():
    """Second pass on the book the order produces: no order, no trigger."""
    b = make_baseline(pf=portfolio(88.42, QQQ=2, QQQM=1))
    assert b["orders"] == []
    assert b["events"]["cash_over_max"] is False
    assert b["events"]["cash_over_max_unavoidable"] is False       # 4.7% < CASH_MAX_PCT
    assert b["execution"]["residual"]["unavoidable"] is True


def test_live_book_via_cli(state, tmp_path):
    csv = make_prices_csv(tmp_path / "px.csv")
    pf = write_json(tmp_path / "pf.json", portfolio(392.25, QQQ=2))
    out = tmp_path / "baseline.json"
    r = run("core.py", "--prices-csv", csv, "--portfolio", pf, "--out", out, state=state)
    assert r.returncode == 0, r.stderr
    b = json.load(open(out))
    assert b["schema"] == "baseline_plan/v5.1"
    assert b["regime"]["regime"] == "TREND"
    assert [(o["ticker"], o["action"], o["shares"]) for o in b["orders"]] == [("QQQM", "BUY", 1.0)]
    assert isinstance(b["orders"][0]["shares"], float) and b["orders"][0]["shares"].is_integer()
    assert b["execution"]["residual"]["unavoidable"] is True
    assert b["satellite"]["real_money_enabled"] is False


# ------------------------------------------------------- residual semantics
def test_cash_below_cheapest_lot_is_unavoidable_not_a_trigger():
    b = make_baseline(pf=portfolio(250.0, QQQ=1))       # drift ~25% > band, 250 < lot
    assert b["orders"] == []
    r = b["execution"]["residual"]
    assert r["unavoidable"] is True and r["deployable"] is False
    assert r["reason"] and "no whole core share fits" in r["reason"]
    assert b["events"]["cash_over_max"] is False
    assert b["events"]["cash_over_max_unavoidable"] is True      # informational only


def test_within_band_but_deployable_excess():
    """Drift inside the band: no order, yet the excess could buy a share -> deployable,
    not unavoidable; below CASH_MAX_PCT -> no trigger."""
    b = make_baseline(pf=portfolio(700.0, QQQ=20))
    assert b["orders"] == []
    r = b["execution"]["residual"]
    assert r["deployable"] is True and r["unavoidable"] is False
    assert b["events"]["cash_over_max"] is False


def test_defensive_target_cash_is_not_over_max():
    """DEFENSIVE's 50% cash sleeve is the target, not an excess (latent 'fires
    forever' bug)."""
    b = make_baseline(regime="DEFENSIVE", pf=portfolio(737.93, QQQ=1))
    assert b["events"]["cash_over_max"] is False
    assert b["events"]["cash_over_max_unavoidable"] is False
    assert b["orders"] == []


# ------------------------------------------------------------- max-deploy
def test_max_deploy_prefers_three_qqqm_over_one_qqq():
    b = make_baseline(pf=portfolio(1000.0))
    assert [(o["ticker"], o["shares"]) for o in b["orders"]] == [("QQQM", 3.0)]


def test_max_deploy_mixes_when_it_deploys_more():
    b = make_baseline(pf=portfolio(1100.0))
    assert sorted((o["ticker"], o["shares"]) for o in b["orders"]) == [("QQQ", 1.0), ("QQQM", 1.0)]
    assert all(o["action"] == "BUY" for o in b["orders"])


def test_price_buffer_edge():
    px = dict(LIVE_PX)
    none = CORE.plan_core_orders(1000.0, 2000.0, {}, px, 303.83)
    assert none == []                                            # exactly the price: no fit
    fits = CORE.plan_core_orders(1000.0, 2000.0, {}, px,
                                 303.83 * (1 + C.WHOLE_SHARE_PRICE_BUFFER_PCT) + 0.01)
    assert [(o["ticker"], o["shares"]) for o in fits] == [("QQQM", 1.0)]


def test_drift_band_suppresses_orders():
    total = 10000.0
    d = C.REBALANCE_DRIFT_BAND_PCT * total * 0.99
    assert CORE.plan_core_orders(d, total, {}, dict(LIVE_PX), 10000.0) == []
    assert CORE.plan_core_orders(-d, total, {"QQQ": 10}, dict(LIVE_PX), 0.0) == []


# ------------------------------------------------------------- DEFENSIVE
def test_defensive_sells_whole_core_shares_and_force_closes_satellite():
    px = dict(LIVE_PX, NVDA=180.0)
    b = make_baseline(regime="DEFENSIVE", prices=px, pf=portfolio(0.0, QQQ=2, QQQM=1, NVDA=1.5))
    assert not [o for o in b["orders"] if o["action"] == "BUY"], b["orders"]
    nv = [o for o in b["orders"] if o["ticker"] == "NVDA"]
    assert len(nv) == 1 and nv[0]["action"] == "SELL" and nv[0]["shares"] == 1.5   # entire lot
    core = core_orders(b)
    assert core and all(o["action"] == "SELL" for o in core)
    for o in core:
        assert_whole(o)
    total = b["portfolio"]["total_usd"]
    need = b["portfolio"]["core_value_usd"] - C.TARGETS["DEFENSIVE"][0] * total
    sold = sum(o["usd"] for o in core)
    # nearest whole-share combination to the needed de-risk
    for q in range(0, 3):
        for m in range(0, 2):
            assert abs(sold - need) <= abs(q * 737.93 + m * 303.83 - need) + 0.01


# ---------------------------------------------------------- property test
@pytest.mark.parametrize("seed", range(6))
def test_property_random_books(seed):
    rng = random.Random(seed)
    for _ in range(80):
        qqq = round(rng.uniform(300, 900), 2)
        px = {"QQQ": qqq, "QQQM": round(qqq * 0.4117, 2)}
        sat = rng.random() < 0.4
        if sat:
            px["NVDA"] = round(rng.uniform(50, 300), 2)
        shares = {"QQQ": rng.choice([0, 0, 1, 2, 3, 0.5316]), "QQQM": rng.choice([0, 0, 1, 2, 5])}
        if sat:
            shares["NVDA"] = rng.choice([0.2348, 1, 2])
        shares = {t: s for t, s in shares.items() if s}
        cash = round(rng.uniform(0, 3000), 2)
        if not shares and cash < 1:
            cash = 100.0
        regime = rng.choice(["TREND", "TREND", "DEFENSIVE"])
        b = make_baseline(regime=regime, prices=px, pf=portfolio(cash, **shares))
        core = core_orders(b)
        acts = {o["action"] for o in core}
        assert len(acts) <= 1, f"mixed core BUY/SELL (a conversion): {core}"
        buys = [o for o in b["orders"] if o["action"] == "BUY"]
        sells = [o for o in b["orders"] if o["action"] == "SELL"]
        spendable = cash + sum(o["usd"] for o in sells)
        cost = sum(o["usd"] * (1 + C.WHOLE_SHARE_PRICE_BUFFER_PCT) for o in buys)
        assert cost <= spendable + 0.01, (cost, spendable, b["orders"])
        for o in core:
            assert_whole(o)
            if o["action"] == "SELL":
                assert o["shares"] <= int(shares.get(o["ticker"], 0) + 1e-9)
        assert all(o["ticker"] in C.CORE_TICKERS for o in buys), "only the core may be bought"
        assert b["execution"]["post_trade"]["cash_usd"] >= -0.01
        if regime == "DEFENSIVE" and "NVDA" in shares:
            nv = [o for o in b["orders"] if o["ticker"] == "NVDA"]
            assert nv and nv[0]["action"] == "SELL" and nv[0]["shares"] == pytest.approx(shares["NVDA"])
        # SELLs are listed before BUYs
        kinds = [o["action"] for o in b["orders"]]
        assert kinds == sorted(kinds, key=lambda a: a != "SELL")


def test_plan_core_orders_bounds():
    """BUY: cost incl. buffer <= spendable, overshoot of the target <= the allocation
    tolerance of the book (documented deviation from 'never overshoots'), and the plan
    is max-deploy (no single extra share of either ticker would still fit).
    SELL: never more than floor(held); never an oversell the next run would buy back
    (>= one deployable lot, or beyond the drift band with a share that fits); proceeds
    nearest |delta| among the candidates that pass that rule."""
    rng = random.Random(42)
    buf = C.WHOLE_SHARE_PRICE_BUFFER_PCT
    for _ in range(600):
        qqq = round(rng.uniform(300, 900), 2)
        px = {"QQQ": qqq, "QQQM": round(qqq * 0.4117, 2)}
        total = rng.uniform(1000, 20000)
        d = rng.uniform(-0.6, 0.6) * total
        spend = rng.uniform(0, abs(d) * 1.5)
        held = {"QQQ": rng.choice([0, 1, 2, 5, 2.7]), "QQQM": rng.choice([0, 1, 3])}
        out = CORE.plan_core_orders(d, total, held, px, spend)
        if abs(d) / total <= C.REBALANCE_DRIFT_BAND_PCT:
            assert out == []
            continue
        assert len({o["action"] for o in out}) <= 1
        if d > 0:
            cost = sum(o["usd"] * (1 + buf) for o in out)
            dep = sum(o["usd"] for o in out)
            tgt_cap = d + C.CONFIRM_ALLOCATION_TOLERANCE_PP / 100 * total
            assert cost <= spend + 0.01 and dep <= tgt_cap + 0.01
            for t in px:
                extra = px[t]
                if cost + extra * (1 + buf) <= spend - 0.01 and dep + extra <= tgt_cap - 0.01:
                    # a whole extra share still fits: only allowed via the tie window
                    assert extra <= C.MIN_ORDER_USD, (d, spend, out)
        else:
            for o in out:
                assert o["shares"] <= int(held[o["ticker"]] + 1e-9)
            sold = sum(o["usd"] for o in out)
            thr = max(min(px.values()) * (1 + buf), C.MIN_ORDER_USD)
            tol = C.CONFIRM_ALLOCATION_TOLERANCE_PP / 100 * total

            def rebuys(usd):
                over = usd - abs(d)
                return over >= thr - 1e-9 or (over / total > C.REBALANCE_DRIFT_BAND_PCT
                                              and min(px.values()) <= over + tol)
            assert not rebuys(sold), (d, held, out)
            for q in range(int(held["QQQ"]) + 1):
                for m in range(int(held["QQQM"]) + 1):
                    alt = q * px["QQQ"] + m * px["QQQM"]
                    if rebuys(alt):
                        continue
                    assert abs(sold - abs(d)) <= abs(alt - abs(d)) + 0.01


def test_never_converts_qqq_into_qqqm():
    """Holding only QQQ with no cash and overweight in nothing: no plan ever sells
    QQQ to buy QQQM, whatever the prices."""
    for qqq in (400.0, 737.93, 900.0):
        px = {"QQQ": qqq, "QQQM": round(qqq * 0.4117, 2)}
        b = make_baseline(prices=px, pf=portfolio(5.0, QQQ=3))
        assert b["orders"] == []
        b = make_baseline(prices=px, pf=portfolio(qqq * 0.9, QQQ=3))
        assert not [o for o in b["orders"] if o["action"] == "SELL"]


# ------------------------------------------------------------- regime
def test_regime_ignores_qqqm_column(state, tmp_path):
    with_m = make_prices_csv(tmp_path / "a.csv")
    only_q = make_prices_csv(tmp_path / "b.csv", last={"QQQ": 737.93})
    pf = write_json(tmp_path / "pf.json", portfolio(392.25, QQQ=2))
    ra = run("core.py", "--prices-csv", with_m, "--portfolio", pf, "--out", "/dev/stdout", state=state)
    rb = run("core.py", "--prices-csv", only_q, "--portfolio", pf, "--out", "/dev/stdout", state=state)
    assert ra.returncode == 0 and rb.returncode == 0, ra.stderr + rb.stderr
    a, b = json.loads(ra.stdout), json.loads(rb.stdout)
    assert a["regime"] == b["regime"]
    # without a QQQM price the plan runs QQQ-only with a warning
    assert any("QQQM" in w for w in b["execution"]["warnings"])
    assert all(o["ticker"] == "QQQ" for o in b["orders"])


def test_defensive_fixture_regime(state, tmp_path):
    csv = make_prices_csv(tmp_path / "down.csv", trend=-1.0)
    pf = write_json(tmp_path / "pf.json", portfolio(0.0, QQQ=2, QQQM=1))
    r = run("core.py", "--prices-csv", csv, "--portfolio", pf, "--out", "/dev/stdout", state=state)
    assert r.returncode == 0, r.stderr
    b = json.loads(r.stdout)
    assert b["regime"]["regime"] == "DEFENSIVE"
    assert b["orders"] and all(o["action"] == "SELL" for o in b["orders"])


# ------------------------------------------------------- fractional switch
def test_fractional_switch_restores_v50(monkeypatch):
    monkeypatch.setattr(C, "FRACTIONAL_SHARES", True)
    b = make_baseline()
    assert len(b["orders"]) == 1
    o = b["orders"][0]
    assert (o["ticker"], o["action"], o["whole_shares"]) == ("QQQ", "BUY", False)
    assert o["usd"] == pytest.approx(392.25)
    assert o["shares"] == pytest.approx(round(392.25 / 737.93, 4))


# ------------------------------------------------------------------ weekly
def test_weekly_only_stop_breach_and_core_buy_of_proceeds():
    px = dict(LIVE_PX, NVDA=100.0)
    st = {"stops_atr": {"NVDA": 110.0}}
    b = make_baseline(prices=px, pf=portfolio(50.0, QQQ=2, NVDA=4), satellite_state=st, weekly=True)
    acts = [(o["ticker"], o["action"], o["shares"]) for o in b["orders"]]
    assert ("NVDA", "SELL", 4.0) in acts
    assert b["orders"][0].get("stop_breach") is True
    buys = [o for o in b["orders"] if o["action"] == "BUY"]
    assert buys and all(o["ticker"] in C.CORE_TICKERS for o in buys)
    for o in buys:
        assert_whole(o)
    # no breach: nothing at all, even with large drift
    b2 = make_baseline(prices=px, pf=portfolio(900.0, QQQ=2, NVDA=4),
                       satellite_state={"stops_atr": {"NVDA": 50.0}}, weekly=True)
    assert b2["orders"] == []


def test_weekly_cli_emits_no_levels(state, tmp_path):
    csv = make_prices_csv(tmp_path / "px.csv", extra={"NVDA": 100.0, "AMD": 150.0})
    write_json(state / "last_run.json", {"regime": "TREND", "satellite_stops_atr": {"NVDA": 110.0}})
    pf = write_json(tmp_path / "pf.json", portfolio(50.0, QQQ=2, NVDA=4))
    r = run("core.py", "--prices-csv", csv, "--portfolio", pf, "--weekly", "--out", "/dev/stdout",
            state=state)
    assert r.returncode == 0, r.stderr
    b = json.loads(r.stdout)
    assert b["mode"] == "weekly"
    assert b["satellite"]["levels"] == {}
    assert all(o.get("stop_breach") or (o["ticker"] in C.CORE_TICKERS and o["action"] == "BUY")
               for o in b["orders"])


# ------------------------------------------------------- satellite levels
def test_levels_are_script_computed_brackets(state, tmp_path):
    csv = make_prices_csv(tmp_path / "px.csv", extra={"AMD": 150.0, "NVDA": 180.0})
    pf = write_json(tmp_path / "pf.json", portfolio(392.25, QQQ=2))
    r = run("core.py", "--prices-csv", csv, "--portfolio", pf, "--out", "/dev/stdout", state=state)
    b = json.loads(r.stdout)
    lv = b["satellite"]["levels"]
    assert set(lv) == {"AMD", "NVDA"}                       # only universe names present
    for t, x in lv.items():
        assert x["atr_source"] == "close_proxy"
        assert x["stop"] < x["price"] < x["target"]
        assert x["rr"] == pytest.approx(C.SHADOW_TARGET_ATR_MULT / C.SATELLITE_STOP_ATR_MULT)
        assert x["rr"] >= C.SATELLITE_MIN_RR
    assert b["prices"]["AMD"] == pytest.approx(150.0)


def test_atr_levels_contract():
    assert CORE.atr_levels(100.0, None) is None
    assert CORE.atr_levels(100.0, 0) is None
    lv = CORE.atr_levels(100.0, 2.0)
    assert lv["stop"] == pytest.approx(100 - C.SATELLITE_STOP_ATR_MULT * 2)
    assert lv["target"] == pytest.approx(100 + C.SHADOW_TARGET_ATR_MULT * 2)


# ---------------------------------------------------------------- backtests
def test_backtest_acceptance_offline(state):
    r = run("core.py", "--backtest", "--history-csv", HISTORY_CSV, state=state)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["acceptance_regime"] is True and d["acceptance_return"] is True, d
    for t, s in d["final_shares"].items():
        assert float(s).is_integer(), d["final_shares"]


def test_backtest_bear_fixture_offline(state):
    r = run("core.py", "--backtest-fixture", "bear", "--history-csv", HISTORY_CSV, state=state)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["acceptance_bear"] is True, {k: v for k, v in d.items() if k.startswith("assert")}


@pytest.mark.network
def test_backtest_acceptance_network(state):
    r = run("core.py", "--backtest", state=state)
    d = json.loads(r.stdout)
    assert d["acceptance_regime"] and d["acceptance_return"]


@pytest.mark.network
def test_backtest_bear_network(state):
    r = run("core.py", "--backtest-fixture", "bear", state=state)
    assert json.loads(r.stdout)["acceptance_bear"]
