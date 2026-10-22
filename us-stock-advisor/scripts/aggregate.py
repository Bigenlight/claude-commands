#!/usr/bin/env python3
"""us-stock-advisor v6.0 — P4 step 1: validate the three decider files and aggregate their votes.

Install path: ~/.claude/skills/us-stock-advisor/scripts/aggregate.py

CLI:
    aggregate.py --run-id R [--event-scope T,T]

Reads  state/run/R/{indicators.json, book.json, digest.json (optional), evidence.json,
                    deciders/D1_TREND.json, deciders/D2_FUNDAMENTAL.json, deciders/D3_MACRO.json}
Writes state/run/R/aggregate_out.json   (schema "aggregate/v6")

Exit codes: 0 ok; 2 refused (missing evidence.json / any decider file / indicators / book, or an
unparseable evidence.json) -- NOTHING is written. There is deliberately no flag that disables the
missing-file refusal: it is the anti-phase-skipping guard. 3: aggregate_out.json written with
valid=false (>= 2 of 3 decider files invalid, or evidence.json without an 'items' list), so
book.py --solve and ledger.py --log refuse it.
Event runs (--event-scope): names/SGOV outside the scope are frozen at their current holding (no
vote, exit, kill, trim, renewal or expiry) and the weekly no-op counter does not advance.

A decider file that is not parseable JSON (after stripping ``` fences) becomes a core-only vote
with drop INVALID_FILE (held sleeve names stay at their current weight, i.e. "no opinion").

Importable:
    validate_decider(doc, ctx, expected_id=None) -> (clean_doc, drops, clamps)
    aggregate(deciders: dict[str, dict|None], ctx) -> dict
    build_ctx(run_dir_path, event_scope=None) -> ctx
    parse_json_text(text) -> obj   (fence-stripping JSON reader)

ctx keys (pinned): regime, total_usd, lot_table, candidate_menu, held_sleeve {T: weight}, caps,
evidence_ids {id: item}, indicators, digest. Optional: held_shares {T: int} (sleeve + SGOV),
held_sgov (weight), prices {T: px}, as_of_close_date, event_scope [T].

Design notes (deviations / pinned interpretations, all deterministic):
  * A "dropped" line reverts to the CURRENT holding: 0 for a name not held, the current share
    count for a held name. Moving toward core never needs a code; keeping an incumbent never
    needs a code; only a deviation does.
  * A held name's "current weight" is expressed as the buffered weight that maps back to exactly
    the held share count under the solver's floor(w*total/(px*(1+PRICE_BUFFER_PCT))) rule, so the
    incumbent rule never sells a share by rounding.
  * lot_ok for a HELD name uses px*(1-PRICE_BUFFER_PCT) instead of the buffered lot, otherwise a
    single held share (weight ~= px/total) could never collect a vote.
  * HOLD on a name that is not held is validated as ENTER (no evidence loophole).
  * Held sleeve names come from book.held_sleeve; a FILLER-tagged position (SPYM without a thesis)
    is not a sleeve name (the solver's free variable). SGOV is read from positions.
  * A kill fires on: thesis status FIRED / kill_fired, a watchdog KILL_* event, or a clerk tag
    evidence.items[].thesis_tags {ticker, kill_type EVENT, fires true}. A kill override needs 3/3
    HOLD each carrying a kill that differs from EVERY current thesis kill.
  * EXIT THESIS_BROKEN on a held name whose kill fired needs no E-ID.
  * Sleeve total is a hard cap: if the held sleeve exceeds caps.sleeve_max_pct and the solver's
    held-name rounding would keep it over, forced_sells trim the most overweight held name one
    share at a time (never below its buffered-floor target): action TRIM / why CAP_DRIFT.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

SCHEMA = "aggregate/v6"
EPS = 1e-9
AGG = "AGGREGATE"          # 'decider' field of aggregate-level clamps
SGOV = C.DERISK_TICKER
KILL_KIND_TO_TYPE = {"KILL_PRICE": "PRICE_BELOW", "KILL_DATE": "DATE", "KILL_EVENT": "EVENT",
                     "KILL_RS": "RS_RANK_BELOW"}
RS_RANK_KILL_RANGE = (1, 20)
EVENT_KILL_MAX_CHARS = 200


# ---------------------------------------------------------------- JSON reading
def strip_fences(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        nl = s.find("\n")
        s = s[nl + 1:] if nl != -1 else ""
    s = s.rstrip()
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


def parse_json_text(text: str):
    """Parse agent JSON: strip ``` fences; fall back to the outermost {...} span."""
    s = strip_fences(text)
    try:
        return json.loads(s)
    except ValueError:
        pass
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:
        return json.loads(s[i:j + 1])   # raises ValueError if still bad
    raise ValueError("not parseable JSON")


def load_json_file(path: str):
    with open(path, encoding="utf-8") as f:
        return parse_json_text(f.read())


def atomic_write_json(path: str, obj) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------- small helpers
def _num(x):
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
    elif isinstance(x, str):
        try:
            v = float(x.strip())
        except ValueError:
            return None
    else:
        return None
    return v if math.isfinite(v) else None


def _r6(x: float) -> float:
    return round(float(x) + 0.0, 6)


def _ceil6(x: float) -> float:
    return math.ceil(x * 1e6) / 1e6


def _words(text, n: int):
    if not isinstance(text, str):
        return None
    w = text.split()
    return " ".join(w[:n]) if len(w) > n else text.strip()


def _parse_date(s):
    if not isinstance(s, str) or len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _median(xs):
    return float(statistics.median(xs)) if xs else 0.0


def _caps(ctx) -> dict:
    caps = dict(ctx.get("caps") or {})
    regime = ctx.get("regime") or C.REGIME_UP
    caps.setdefault("authority", "FULL")
    caps.setdefault("sleeve_max_pct", C.SLEEVE_MAX_PCT)
    caps.setdefault("sleeve_position_max_pct", C.SLEEVE_POSITION_MAX_PCT)
    caps.setdefault("sleeve_max_positions", C.SLEEVE_MAX_POSITIONS)
    caps.setdefault("sgov_max_pct", C.SGOV_MAX_DOWN if regime == C.REGIME_DOWN else C.SGOV_MAX_UP)
    caps.setdefault("sgov_authority", True)
    return caps


def _total(ctx) -> float:
    t = _num(ctx.get("total_usd"))
    if not t or t <= 0:
        raise ValueError("ctx.total_usd must be > 0")
    return t


def _as_of(ctx):
    a = ctx.get("as_of_close_date") or (ctx.get("indicators") or {}).get("as_of_close_date")
    return _parse_date(a)


def _price(ctx, t):
    lt = (ctx.get("lot_table") or {}).get(t) or {}
    p = _num(lt.get("price"))
    if p:
        return p
    p = _num((ctx.get("prices") or {}).get(t))
    if p:
        return p
    ind = ((ctx.get("indicators") or {}).get("tickers") or {}).get(t) or {}
    return _num(ind.get("price"))


def _held_shares(ctx) -> dict:
    """Non-core held shares (sleeve names + SGOV)."""
    hs = ctx.get("held_shares")
    if hs is not None:
        return {t: int(n) for t, n in hs.items() if t not in C.CORE_TICKERS and int(n) > 0}
    total = _total(ctx)
    out = {}
    items = list((ctx.get("held_sleeve") or {}).items())
    if _num(ctx.get("held_sgov")):
        items.append((SGOV, ctx["held_sgov"]))
    for t, w in items:
        px = _price(ctx, t)
        w = _num(w) or 0.0
        if px and w > 0:
            n = int(round(w * total / px))
            if n > 0:
                out[t] = n
    return out


def _held_sleeve_set(ctx) -> list:
    out = [t for t in (ctx.get("held_sleeve") or {}) if t not in C.CORE_TICKERS and t != SGOV]
    for t in _held_shares(ctx):
        if t != SGOV and t not in out:
            out.append(t)
    return out


def hold_weight(ctx, t) -> float:
    """Weight that the solver maps back to exactly the held share count (0 if not held)."""
    n = _held_shares(ctx).get(t, 0)
    if n <= 0:
        return 0.0
    px = _price(ctx, t)
    total = _total(ctx)
    if not px:
        w = (ctx.get("held_sleeve") or {}).get(t) if t != SGOV else ctx.get("held_sgov")
        return _r6(_num(w) or 0.0)
    exact = n * px * (1 + C.PRICE_BUFFER_PCT) / total
    return _ceil6(exact + 1e-7)


def current_weight(ctx, t) -> float:
    """Actual (unbuffered) current weight of a held name."""
    n = _held_shares(ctx).get(t, 0)
    px = _price(ctx, t)
    if n and px:
        return n * px / _total(ctx)
    w = (ctx.get("held_sleeve") or {}).get(t) if t != SGOV else ctx.get("held_sgov")
    return _num(w) or 0.0


def target_shares(ctx, t, w) -> int:
    """The solver's share rule: floor(w*total / (px*(1+buffer)))."""
    px = _price(ctx, t)
    if not px or w <= EPS:
        return 0
    return int(math.floor(w * _total(ctx) / (px * (1 + C.PRICE_BUFFER_PCT)) + 1e-9))


def solver_shares(ctx, t, w) -> int:
    """Shares book.solve keeps: buffered floor; a held name keeps up to its held count at nearest rounding."""
    n = target_shares(ctx, t, w)
    held = _held_shares(ctx).get(t, 0)
    px = _price(ctx, t)
    if held and px and w > EPS:
        n = max(n, min(held, int(round(w * _total(ctx) / px))))
    return n


def _lot_threshold(ctx, t) -> float:
    px = _price(ctx, t)
    if px and _held_shares(ctx).get(t, 0) > 0:
        return px * (1 - C.PRICE_BUFFER_PCT)
    lt = (ctx.get("lot_table") or {}).get(t) or {}
    v = _num(lt.get("lot_usd_buffered"))
    if v:
        return v
    if px:
        return px * (1 + C.PRICE_BUFFER_PCT)
    return math.inf


def lot_ok(ctx, t, w) -> bool:
    return w > EPS and w * _total(ctx) + 1e-6 >= _lot_threshold(ctx, t)


def _same_shares_as_held(ctx, t, w) -> bool:
    """True if weight w is the held share count, rounding to nearest at the unbuffered price."""
    n = _held_shares(ctx).get(t, 0)
    px = _price(ctx, t)
    if n <= 0 or not px:
        return False
    return int(round(w * _total(ctx) / px)) == n


# ---------------------------------------------------------------- evidence predicates
def _evidence_supports(item: dict, t: str) -> bool:
    tks = [str(x).upper() for x in (item.get("tickers") or [])]
    if t in tks:
        return True
    if C.group_of(t) in ("sector", "broad"):
        ent = str(item.get("entity") or "").upper()
        return ent in ("NDX", "MACRO") or "NDX" in tks or "MACRO" in tks
    return False


def _pred(item: dict, key: str, val) -> bool:
    if key == "entity_in":
        universe = getattr(C, val, ()) if isinstance(val, str) else tuple(val)
        tks = item.get("tickers") or []
        head = str(tks[0]).upper() if tks else str(item.get("entity") or "").upper()
        return head in universe
    return item.get(key) == val


def derisk_justified(code, evids, ctx):
    if code not in C.DERISK_CODES:
        return False, f"reason_code {code!r} not in DERISK enum"
    rule = C.DERISK_EVIDENCE_RULES.get(code, {})
    if "regime" in rule and ctx.get("regime") != rule["regime"]:
        return False, f"{code} but regime {ctx.get('regime')}"
    item_rule = {k: v for k, v in rule.items() if k != "regime"}
    if not item_rule:
        return True, ""
    ev = ctx.get("evidence_ids") or {}
    cited = [ev[e] for e in evids if e in ev]
    if not cited:
        return False, f"{code} without a cited E-ID present in evidence.json"
    for it in cited:
        if all(_pred(it, k, v) for k, v in item_rule.items()):
            return True, ""
    return False, f"{code}: no cited E-ID satisfies {sorted(item_rule)}"


def _validate_kills(raw, ctx, t):
    good, bad = [], []
    if raw is None:
        return good, bad
    if not isinstance(raw, list):
        return good, ["kill_conditions is not a list"]
    px = _price(ctx, t)
    as_of = _as_of(ctx)
    for kc in raw:
        if not isinstance(kc, dict):
            bad.append(f"not an object: {kc!r}"[:120])
            continue
        typ = str(kc.get("type") or "").upper()
        val = kc.get("value")
        if typ not in C.KILL_TYPES:
            bad.append(f"type {typ!r} not in KILL_TYPES")
        elif typ == "PRICE_BELOW":
            v = _num(val)
            if v is None or v <= 0 or (px is not None and v >= px):
                bad.append(f"PRICE_BELOW {val!r} must be numeric and < price {px}")
            else:
                good.append({"type": typ, "value": v})
        elif typ == "DATE":
            d = _parse_date(val)
            if d is None or (as_of is not None and d <= as_of):
                bad.append(f"DATE {val!r} must be ISO and > {as_of}")
            else:
                good.append({"type": typ, "value": d.isoformat()})
        elif typ == "RS_RANK_BELOW":
            v = _num(val)
            if v is None or v != int(v) or not RS_RANK_KILL_RANGE[0] <= v <= RS_RANK_KILL_RANGE[1]:
                bad.append(f"RS_RANK_BELOW {val!r} must be an int in {RS_RANK_KILL_RANGE}")
            else:
                good.append({"type": typ, "value": int(v)})
        else:  # EVENT
            if not isinstance(val, str) or not val.strip() or len(val) > EVENT_KILL_MAX_CHARS:
                bad.append(f"EVENT must be a non-empty string <= {EVENT_KILL_MAX_CHARS} chars")
            else:
                good.append({"type": typ, "value": val.strip()})
    return good, bad


def _kill_key(kc):
    v = kc.get("value")
    n = _num(v)
    return (str(kc.get("type") or "").upper(), round(n, 4) if n is not None else str(v).strip())


# ---------------------------------------------------------------- step 1: validation
def validate_decider(doc, ctx, expected_id=None):
    """Validate/clamp one decider document. Returns (clean_doc, drops, clamps)."""
    did = expected_id or (doc.get("decider_id") if isinstance(doc, dict) else None) or "UNKNOWN"
    drops, clamps = [], []
    caps = _caps(ctx)
    held_set = _held_sleeve_set(ctx)
    held_n = _held_shares(ctx)
    menu = set(ctx.get("candidate_menu") or []) | set(held_set)
    menu -= set(C.CORE_TICKERS) | {SGOV}
    scope = ctx.get("event_scope")

    def drop(t, rule, detail):
        drops.append({"decider": did, "ticker": t, "rule": rule, "detail": detail})

    def clamp(t, rule, frm, to, detail=""):
        clamps.append({"decider": did, "ticker": t, "rule": rule,
                       "from": None if frm is None else _r6(frm), "to": None if to is None else _r6(to),
                       "detail": detail})

    invalid = not isinstance(doc, dict)
    if invalid:
        drop("FILE", "INVALID_FILE", "decider file missing, unparseable or not a JSON object; core-only vote")
        doc = {}
    elif expected_id and doc.get("decider_id") != expected_id:
        drop("FILE", "ID_MISMATCH", f"decider_id={doc.get('decider_id')!r} in file {expected_id}")

    # -- target_weights
    tw_raw = doc.get("target_weights")
    if not isinstance(tw_raw, dict):
        if not invalid:
            drop("FILE", "BAD_SCHEMA", "target_weights missing or not an object; core-only")
        tw_raw = {"CORE": 1.0}
    weights, sgov_tw, sgov_tw_given, vals = {}, 0.0, False, []
    for k, v in tw_raw.items():
        t = str(k).strip().upper()
        x = _num(v)
        if x is None:
            drop(t, "BAD_WEIGHT", f"weight {v!r} not numeric; to CORE")
            continue
        if x < 0 or x > 1:
            y = min(max(x, 0.0), 1.0)
            clamp(t, "RANGE", x, y, "weight clamped to [0,1]")
            x = y
        vals.append(x)
        if t == "CORE":
            continue
        if t == SGOV:
            sgov_tw, sgov_tw_given = sgov_tw + x, True
        elif t in menu:
            weights[t] = weights.get(t, 0.0) + x
        else:
            drop(t, "NON_MENU_TICKER", "not in candidate_menu or holdings; weight to CORE")
    s = sum(vals)
    if s > EPS and abs(s - 1.0) > C.WEIGHT_SUM_TOLERANCE:
        weights = {t: w / s for t, w in weights.items()}
        sgov_tw /= s
        clamp("FILE", "SUM_RENORM", s, 1.0, "target_weights renormalised")

    # -- positions
    pos_map = {}
    raw_pos = doc.get("positions")
    if raw_pos is not None and not isinstance(raw_pos, list):
        drop("FILE", "BAD_SCHEMA", "positions is not a list")
        raw_pos = []
    for p in raw_pos or []:
        if not isinstance(p, dict) or not str(p.get("ticker") or "").strip():
            drop("FILE", "BAD_SCHEMA", "position entry without ticker")
            continue
        t = str(p["ticker"]).strip().upper()
        if t in pos_map:
            drop(t, "DUP_POSITION", "second position entry ignored")
            continue
        pos_map[t] = p
        if t not in menu and t not in weights and t not in ("CORE", SGOV) and \
                not any(d["ticker"] == t for d in drops):
            drop(t, "NON_MENU_TICKER", "position for a ticker not in candidate_menu or holdings")

    ev = ctx.get("evidence_ids") or {}
    indicators = (ctx.get("indicators") or {}).get("tickers") or {}
    final, clean_pos = {}, {}
    order = list(weights) + [t for t in held_set if t not in weights]

    def synth_hold(t, p):
        """Position record for a held name kept at its current weight."""
        kills, _ = _validate_kills((p or {}).get("kill_conditions"), ctx, t)
        code = (p or {}).get("reason_code")
        return {"ticker": t, "action": "HOLD", "reason_code": code if code in C.ENTRY_CODES else None,
                "evidence_ids": [], "horizon_weeks": None, "expected_excess_vs_qqq_pp": None,
                "kill_conditions": kills, "thesis_30w": _words((p or {}).get("thesis_30w"), C.THESIS_MAX_WORDS),
                "reverted": True}

    for t in order:
        w = weights.get(t, 0.0)
        is_held = t in held_set
        revert_w = hold_weight(ctx, t) if is_held else 0.0
        p = pos_map.get(t)
        action = str(p.get("action") or "").strip().upper() if p else None
        if action == "HOLD" and not is_held:
            action = "ENTER"      # a new name is an entry whatever it is called
        elif action == "HOLD" and w > EPS and _price(ctx, t) and \
                int(round(w * _total(ctx) / _price(ctx, t))) > held_n.get(t, 0):
            action = "ENTER"      # adding shares to a held name is an entry (evidence/flag/kill checks)

        def revert(rule, detail, _t=t, _p=p, _held=is_held, _rw=revert_w):
            if not invalid:
                drop(_t, rule, detail)
            final[_t] = _rw
            if _held:
                clean_pos[_t] = synth_hold(_t, _p)

        if invalid:
            revert("INVALID_FILE", "")
            continue
        if w > EPS:
            if action not in ("ENTER", "HOLD"):
                revert("ACTION_WEIGHT_MISMATCH", f"weight {_r6(w)} needs ENTER/HOLD, got {action}")
                continue
        else:
            if not is_held:
                if action == "ENTER":
                    drop(t, "ACTION_WEIGHT_MISMATCH", "ENTER with weight 0")
                continue
            if action != "EXIT":
                revert("ACTION_WEIGHT_MISMATCH", f"held name at weight 0 needs EXIT, got {action}")
                continue
        code = p.get("reason_code")
        if code not in C.REASON_CODES[action]:
            revert("BAD_CODE", f"reason_code {code!r} not in the {action} enum")
            continue
        evids_raw = p.get("evidence_ids") if isinstance(p.get("evidence_ids"), list) else []
        evids = [str(e).strip() for e in evids_raw if isinstance(e, (str, int))]
        valid_e = [e for e in evids if e in ev and _evidence_supports(ev[e], t)]
        if action in ("ENTER", "EXIT") and code in C.EVIDENCE_REQUIRED_CODES and not valid_e:
            # EXIT THESIS_BROKEN on a held name whose kill has already fired needs no E-ID (the fired
            # kill is the evidence; prompt STEP 2a tells deciders to vote exactly this)
            kill_exit = (action == "EXIT" and code == "THESIS_BROKEN" and is_held and
                         _kill_state(t, _thesis_map(ctx.get("digest")).get(t), ctx.get("digest"), ev)[0])
            if not kill_exit:
                revert("NO_EVIDENCE", f"{code} needs an E-ID in evidence.json for {t}; cited {evids}")
                continue
        if action == "ENTER" and code in C.RS_FLAG_CODES:
            rank = (indicators.get(t) or {}).get("rs_rank")
            if not isinstance(rank, int) or isinstance(rank, bool) or rank > C.RS_RANK_FLAG_MAX:
                revert("NO_RS_FLAG", f"{code} needs rs_rank <= {C.RS_RANK_FLAG_MAX}, got {rank}")
                continue
        kills, bad = [], []
        if action != "EXIT":
            kills, bad = _validate_kills(p.get("kill_conditions"), ctx, t)
            for b in bad:
                drop(t, "BAD_KILL", b)
            if action == "ENTER" and not kills:
                revert("BAD_KILL", "ENTER needs >= 1 valid kill condition")
                continue
        hw = None
        if action != "EXIT":
            hraw = _num(p.get("horizon_weeks"))
            if hraw is None:
                hw = C.HORIZON_WEEKS_MIN
                clamp(t, "HORIZON", None, hw, "horizon_weeks missing")
            else:
                hw = int(round(min(max(hraw, C.HORIZON_WEEKS_MIN), C.HORIZON_WEEKS_MAX)))
                if abs(hw - hraw) > EPS:
                    clamp(t, "HORIZON", hraw, hw, "horizon_weeks clamped")
            cap = caps["sleeve_position_max_pct"]
            if is_held:   # a held name may sit between POSITION_MAX and TRIM_ABOVE (trim only above)
                cap = max(cap, min(revert_w, C.SLEEVE_TRIM_ABOVE_PCT))
            if w > cap + EPS:
                clamp(t, "CAP_POSITION", w, cap, "per-name cap")
                w = cap
            if is_held and _same_shares_as_held(ctx, t, w):
                w = revert_w
        final[t] = w if action != "EXIT" else 0.0
        clean_pos[t] = {"ticker": t, "action": action, "reason_code": code, "evidence_ids": valid_e,
                        "horizon_weeks": hw,
                        "expected_excess_vs_qqq_pp": _num(p.get("expected_excess_vs_qqq_pp")),
                        "kill_conditions": kills,
                        "thesis_30w": _words(p.get("thesis_30w"), C.THESIS_MAX_WORDS)}

    # -- SGOV / derisk
    dr = doc.get("derisk") if isinstance(doc.get("derisk"), dict) else {}
    sg = _num(dr.get("sgov_weight"))
    if sg is None:
        sg = sgov_tw
    elif sgov_tw_given and abs(sgov_tw - sg) > 1e-6:
        clamp(SGOV, "SGOV_MISMATCH", sgov_tw, sg, "target_weights.SGOV != derisk.sgov_weight; derisk wins")
    if sg < 0 or sg > 1:
        y = min(max(sg, 0.0), 1.0)
        clamp(SGOV, "RANGE", sg, y, "sgov_weight clamped to [0,1]")
        sg = y
    dcode = dr.get("reason_code")
    devids = [str(e).strip() for e in (dr.get("evidence_ids") or []) if isinstance(e, (str, int))] \
        if isinstance(dr.get("evidence_ids"), list) else []
    if sg > EPS:
        ok, why = derisk_justified(dcode, devids, ctx)
        if not ok:
            drop(SGOV, "DERISK_UNJUSTIFIED", why)
            sg = 0.0
    sg_cap = caps["sgov_max_pct"] if caps.get("sgov_authority", True) else 0.0
    if sg > sg_cap + EPS:
        clamp(SGOV, "CAP_SGOV", sg, sg_cap, "sgov cap in effect")
        sg = sg_cap

    # -- event scope: only the listed tickers may change
    sgov_reverted = False
    if scope is not None:
        for t in list(final):
            if t in scope:
                continue
            rw = hold_weight(ctx, t)
            if target_shares(ctx, t, final[t]) != held_n.get(t, 0):
                drop(t, "OUT_OF_SCOPE", f"event run scope {sorted(scope)}; reverted to current")
            final[t] = rw
            if rw > 0:
                if t not in clean_pos or clean_pos[t]["action"] != "HOLD":
                    clean_pos[t] = synth_hold(t, pos_map.get(t))
            else:
                clean_pos.pop(t, None)
        if SGOV not in scope:
            sgov_reverted = True
            rw = hold_weight(ctx, SGOV)
            if target_shares(ctx, SGOV, sg) != held_n.get(SGOV, 0):
                drop(SGOV, "OUT_OF_SCOPE", f"event run scope {sorted(scope)}; reverted to current")
            sg = rw

    # -- sleeve total cap (per decider, so its paper book respects the caps too)
    ssum = sum(final.values())
    smax = min(caps["sleeve_max_pct"], max(0.0, 1.0 - sg))
    if ssum > smax + EPS:
        f = smax / ssum if ssum > 0 else 0.0
        final = {t: w * f for t, w in final.items()}
        clamp("FILE", "CAP_SLEEVE_TOTAL", ssum, smax, "sleeve total scaled down")

    # -- scalars
    p_up = _num(doc.get("p_qqq_up_20d"))
    if p_up is not None and not 0.0 <= p_up <= 1.0:
        y = min(max(p_up, 0.0), 1.0)
        clamp("FILE", "P_RANGE", p_up, y, "p_qqq_up_20d clamped to [0,1]")
        p_up = y
    dissent = _words(doc.get("dissent_note"), C.DISSENT_MAX_WORDS)

    sleeve = {t: _r6(w) for t, w in final.items() if w > EPS}
    sg = _r6(sg)
    core = _r6(max(0.0, 1.0 - sg - sum(sleeve.values())))
    paper = {"CORE": core}
    if sg > EPS:
        paper["SGOV"] = sg
    paper.update(sleeve)
    clean = {
        "decider_id": did,
        "regime_seen": doc.get("regime_seen"),
        "invalid": invalid,
        "target_weights": {"CORE": core, "SGOV": sg, **sleeve},
        "weights_all": {t: _r6(w) for t, w in final.items()},
        "sleeve": sleeve,
        "sgov_weight": sg,
        "sgov_reverted": sgov_reverted,
        "core": core,
        "positions": clean_pos,
        "derisk": {"sgov_weight": sg, "reason_code": dcode if sg > EPS else None,
                   "evidence_ids": devids if sg > EPS else []},
        "p_qqq_up_20d": p_up,
        "dissent_note": dissent,
        "paper_targets": paper,
    }
    return clean, drops, clamps


# ---------------------------------------------------------------- steps 2-5: aggregation
def _thesis_map(digest) -> dict:
    out = {}
    for th in (digest or {}).get("open_theses") or []:
        if isinstance(th, dict) and th.get("ticker"):
            out[str(th["ticker"]).upper()] = th
    return out


def _tag_fires(tag) -> bool:
    f = tag.get("fires")
    return f is True or (isinstance(f, str) and f.strip().lower() == "true")


def _kill_state(t, th, digest, evidence_ids=None):
    """(fired, fired_keys) for held name t: thesis kill status FIRED / digest kill_fired, watchdog
    KILL_* events, and clerk-tagged EVENT kills (evidence item thesis_tags {ticker==t,
    kill_type EVENT, fires true}; the tag carries no kill text, so its key is the thesis' EVENT
    kill text(s), else the tag's kill_text/value/text/note)."""
    th = th or {}
    fired, keys = bool(th.get("kill_fired")), set()
    for kc in th.get("kill_conditions") or []:
        if isinstance(kc, dict) and kc.get("status") == "FIRED":
            fired = True
            keys.add(_kill_key(kc))
    th_event_kills = [kc for kc in th.get("kill_conditions") or []
                      if isinstance(kc, dict) and str(kc.get("type") or "").upper() == "EVENT"]
    for item in (evidence_ids or {}).values():
        if not isinstance(item, dict):
            continue
        for tag in item.get("thesis_tags") or []:
            if not isinstance(tag, dict) or str(tag.get("ticker") or "").strip().upper() != t:
                continue
            if str(tag.get("kill_type") or "").strip().upper() != "EVENT" or not _tag_fires(tag):
                continue
            fired = True
            text = next((tag[k] for k in ("kill_text", "value", "text")
                         if isinstance(tag.get(k), str) and tag[k].strip()), None)
            if text is not None:
                keys.add(_kill_key({"type": "EVENT", "value": text}))
            elif th_event_kills:
                keys.update(_kill_key(kc) for kc in th_event_kills)
            else:
                keys.add(_kill_key({"type": "EVENT", "value": str(tag.get("note") or "")}))
    wt = (digest or {}).get("watchdog_triggers") or {}
    for e in wt.get("events") or [] if isinstance(wt, dict) else []:
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind") or "")
        if kind.startswith("KILL_") and str(e.get("ticker") or "").upper() == t:
            fired = True
            det = e.get("detail") or {}
            if "value" in det and kind in KILL_KIND_TO_TYPE:
                keys.add(_kill_key({"type": KILL_KIND_TO_TYPE[kind], "value": det["value"]}))
    return fired, keys


def _min_hold_met(th) -> bool:
    if not th:
        return True            # held without a v6 thesis (pre-epoch / manual): no hold clock
    if "min_hold_met" in th:
        return bool(th["min_hold_met"])
    n = _num(th.get("sessions_held"))
    return n is None or n >= C.MIN_HOLD_SESSIONS


def _horizon_reached(th, as_of) -> bool:
    if not th:
        return False
    if th.get("horizon_reached"):
        return True
    d = _parse_date(th.get("horizon_end_date"))
    return bool(d and as_of and d <= as_of)


def aggregate(deciders: dict, ctx: dict) -> dict:
    """Validate three decider docs and aggregate per spec §5 (user-overridden caps via ctx.caps)."""
    caps = _caps(ctx)
    ids = list(C.DECIDER_IDS)
    as_of = _as_of(ctx)
    digest = ctx.get("digest") or {}
    theses = _thesis_map(digest)
    held_n = _held_shares(ctx)
    held_set = _held_sleeve_set(ctx)

    val, drops, clamps = {}, [], []
    for did in ids:
        clean, d, c = validate_decider((deciders or {}).get(did), ctx, expected_id=did)
        val[did] = clean
        drops += d
        clamps += c

    def agg_clamp(t, rule, frm, to, detail=""):
        clamps.append({"decider": AGG, "ticker": t, "rule": rule, "from": _r6(frm), "to": _r6(to),
                       "detail": detail})

    exits, no_majority, forced = [], [], {}

    # -- SGOV (step 2)
    sws = [val[d]["sgov_weight"] for d in ids]
    s_ok = [lot_ok(ctx, SGOV, w) and not val[d].get("sgov_reverted") for d, w in zip(ids, sws)]
    s_votes = sum(s_ok)
    regime = ctx.get("regime") or C.REGIME_UP
    sgov_held = held_n.get(SGOV, 0) > 0
    sg_cap = caps["sgov_max_pct"] if caps.get("sgov_authority", True) else 0.0
    sg_w, sg_rule, renewed = 0.0, "NONE", False
    scope = ctx.get("event_scope")
    if scope is not None and SGOV not in scope:
        sg_w, sg_rule = hold_weight(ctx, SGOV), "OUT_OF_SCOPE"     # event run: SGOV frozen, no renewal/expiry
    elif regime == C.REGIME_UP and s_votes >= C.VOTES_REQUIRED_SGOV_UP:
        sg_w, sg_rule, renewed = min(sws), "MIN_UP", True
    elif regime == C.REGIME_DOWN and s_votes >= C.VOTES_REQUIRED_SGOV_DOWN:
        sg_w, sg_rule, renewed = _median(sws), "MEDIAN_DOWN", True
    elif sgov_held:
        runs_held = int(_num(((digest.get("sgov") or {}).get("runs_held"))) or 0)
        if runs_held < C.SGOV_EXPIRY_RUNS:
            sg_w, sg_rule = hold_weight(ctx, SGOV), "INCUMBENT"
        else:
            exits.append({"ticker": SGOV, "why": "SGOV_EXPIRED"})
    if sg_w > sg_cap + EPS:
        agg_clamp(SGOV, "CAP_SGOV", sg_w, sg_cap, "sgov cap in effect")
        sg_w = sg_cap
        if sg_w <= EPS and sgov_held and not any(e["ticker"] == SGOV for e in exits):
            exits.append({"ticker": SGOV, "why": "CAP_DRIFT"})
    s_voters = [d for d, ok in zip(ids, s_ok) if ok]
    sgov_out = {"weight": _r6(sg_w), "votes": s_votes, "weights": [_r6(w) for w in sws], "rule": sg_rule,
                "renewed": bool(renewed and sg_w > EPS),
                "reason_codes": [val[d]["derisk"]["reason_code"] for d in s_voters],
                "evidence_ids": list(dict.fromkeys(e for d in s_voters for e in val[d]["derisk"]["evidence_ids"])),
                "held_shares": held_n.get(SGOV, 0)}

    # -- sleeve (steps 3-4)
    tickers = []
    for d in ids:
        for t in val[d]["weights_all"]:
            if t not in tickers:
                tickers.append(t)
    for t in held_set:
        if t not in tickers:
            tickers.append(t)

    sleeve = {}
    for t in tickers:
        ws = [val[d]["weights_all"].get(t, 0.0) for d in ids]
        # a reverted (dropped/omitted/out-of-scope) line on a held name abstains: it sits at the current
        # weight for the median but is neither a keep vote nor an exit vote
        abst = [bool((val[d]["positions"].get(t) or {}).get("reverted")) for d in ids]
        oks = [lot_ok(ctx, t, w) and not ab for w, ab in zip(ws, abst)]
        votes = sum(oks)
        is_held = t in held_set
        th = theses.get(t, {})
        zeros = sum(1 for w, ab in zip(ws, abst) if w <= EPS and not ab)
        e = {"weight": 0.0, "votes": votes, "exit_votes": zeros if is_held else 0,
             "weights": [_r6(w) for w in ws], "action": "NONE", "rule": "NONE",
             "held": is_held, "held_shares": held_n.get(t, 0),
             "current_weight": _r6(current_weight(ctx, t)) if is_held else 0.0,
             "reason_codes": [], "evidence_ids": [], "kill_conditions": [], "horizon_weeks": None,
             "thesis_30w": None, "kill_override": False, "why": None}
        w = 0.0
        frozen = is_held and scope is not None and t not in scope
        if frozen:
            w = hold_weight(ctx, t)
            e["rule"], e["action"] = "OUT_OF_SCOPE", "HOLD"      # event run: untouched (no exit/kill/trim)
        elif votes >= C.VOTES_REQUIRED_SLEEVE:
            w = _median(ws)
            e["rule"], e["action"] = "MEDIAN", "HOLD" if is_held else "ENTER"
        elif is_held:
            if zeros >= C.VOTES_REQUIRED_EXIT_WITHIN_HOLD:
                e["action"], e["why"] = "EXIT", "VOTES_3_3"
            elif zeros >= C.VOTES_REQUIRED_EXIT_AFTER_HOLD and _min_hold_met(th):
                e["action"], e["why"] = "EXIT", "VOTES_2_3"
            elif _horizon_reached(th, as_of):
                e["action"], e["why"] = "EXIT", "HORIZON"
            else:
                w = hold_weight(ctx, t)
                e["rule"], e["action"] = "INCUMBENT", "HOLD"
        elif votes == 1:
            no_majority.append({"ticker": t, "votes": 1,
                                "proposers": [d for d, ok in zip(ids, oks) if ok]})

        if is_held and not frozen:
            fired, fired_keys = _kill_state(t, th, digest, ctx.get("evidence_ids"))
            if fired and e["action"] != "EXIT":
                # a "new" kill differs from EVERY current thesis kill (fired or not), not only the fired one
                known = set(fired_keys) | {_kill_key(kc) for kc in th.get("kill_conditions") or []
                                           if isinstance(kc, dict)}

                def reaffirms(d):
                    pos = val[d]["positions"].get(t) or {}
                    if pos.get("action") not in ("HOLD", "ENTER") or pos.get("reverted"):
                        return False
                    return any(_kill_key(kc) not in known for kc in pos.get("kill_conditions") or [])
                n_re = sum(1 for d, ok in zip(ids, oks) if ok and reaffirms(d))
                if n_re >= C.VOTES_REQUIRED_KILL_OVERRIDE and e["rule"] == "MEDIAN":
                    e["kill_override"] = True
                else:
                    e["action"], e["why"], w = "EXIT", "KILL_FIRED", 0.0
                    forced[t] = held_n.get(t, 0)
            if e["action"] == "HOLD":
                if _same_shares_as_held(ctx, t, w):
                    w = hold_weight(ctx, t)
                if current_weight(ctx, t) > C.SLEEVE_TRIM_ABOVE_PCT and w > caps["sleeve_position_max_pct"] + EPS:
                    w = caps["sleeve_position_max_pct"]
                    e["action"], e["why"] = "TRIM", "CAP_DRIFT"
            if e["action"] == "EXIT":
                w = 0.0
                exits.append({"ticker": t, "why": e["why"]})

        # metadata
        if e["action"] == "EXIT":
            src_ids = [d for d in ids if (val[d]["positions"].get(t) or {}).get("action") == "EXIT"]
        else:
            src_ids = [d for d, ok in zip(ids, oks) if ok]
        e["reason_codes"] = [val[d]["positions"][t]["reason_code"] for d in src_ids
                             if t in val[d]["positions"] and val[d]["positions"][t].get("reason_code")]
        e["evidence_ids"] = list(dict.fromkeys(ev for d in src_ids if t in val[d]["positions"]
                                               for ev in val[d]["positions"][t]["evidence_ids"]))
        if e["action"] in ("ENTER", "HOLD", "TRIM"):
            voters = [d for d, ok in zip(ids, oks) if ok and t in val[d]["positions"]
                      and not val[d]["positions"][t].get("reverted")]
            med = _median(ws) if e["rule"] == "MEDIAN" else None
            src = next((d for d in voters if med is not None and abs(val[d]["weights_all"].get(t, 0) - med) < 1e-9),
                       voters[0] if voters else None)
            if e["rule"] == "INCUMBENT" and th:
                src = None       # incumbent keeps the open thesis' kills/horizon
            if src:
                sp = val[src]["positions"][t]
                e["kill_conditions"], e["thesis_30w"] = sp["kill_conditions"], sp["thesis_30w"]
                hws = [val[d]["positions"][t]["horizon_weeks"] for d in voters
                       if val[d]["positions"][t].get("horizon_weeks") is not None]
                e["horizon_weeks"] = int(round(_median(hws))) if hws else None
            elif th:
                e["kill_conditions"] = [{"type": k.get("type"), "value": k.get("value")}
                                        for k in th.get("kill_conditions") or [] if isinstance(k, dict)]
                e["thesis_30w"] = th.get("thesis_30w")
                e["horizon_weeks"] = th.get("horizon_weeks")
        e["weight"] = w
        e["frozen"] = frozen
        sleeve[t] = e

    # -- position count cap: held first, then by weight
    kept = [t for t, e in sleeve.items() if e["weight"] > EPS]
    maxn = int(caps["sleeve_max_positions"])
    if len(kept) > maxn:
        ranked = sorted(kept, key=lambda t: (0 if sleeve[t]["held"] else 1, -sleeve[t]["weight"], t))
        for t in ranked[maxn:]:
            agg_clamp(t, "CAP_POSITIONS", sleeve[t]["weight"], 0.0, f"more than {maxn} sleeve positions")
            sleeve[t]["weight"] = 0.0
            sleeve[t]["action"] = "NONE"
            sleeve[t]["rule"] = "CAP_POSITIONS"

    # -- sleeve total cap (also keeps CORE >= 0)
    fixed = sum(e["weight"] for e in sleeve.values() if e.get("frozen"))
    ssum = sum(e["weight"] for e in sleeve.values() if not e.get("frozen"))
    smax = max(0.0, min(caps["sleeve_max_pct"], 1.0 - sg_w) - fixed)
    if ssum > smax + EPS:
        f = smax / ssum if ssum > 0 else 0.0
        for t, e in sleeve.items():
            if e["weight"] > EPS and not e.get("frozen"):
                agg_clamp(t, "CAP_SLEEVE_TOTAL", e["weight"], e["weight"] * f, f"sleeve total {ssum:.4f} > {smax:.4f}")
                e["weight"] *= f

    # held names that ended at 0 for a cap reason are exits too
    for t, e in sleeve.items():
        if e["held"] and e["weight"] <= EPS and e["action"] != "EXIT":
            e["action"], e["why"] = "EXIT", "CAP_DRIFT"
            exits.append({"ticker": t, "why": "CAP_DRIFT"})
        e["weight"] = _r6(e["weight"])

    # -- sleeve total is a HARD cap: the solver's held-name rounding (round(), not the buffered floor)
    # can keep every held share after the total-cap scaling, leaving e.g. a 63% sleeve "unchanged".
    # When the held sleeve is over the cap and the solver's projected post-trade sleeve still is,
    # force-sell one share at a time from the most overweight non-frozen held name (never below its
    # buffered-floor target) until the projected sleeve <= cap. book.py's rounding is not touched.
    total = _total(ctx)
    held_sleeve_w = sum(current_weight(ctx, t) for t in held_set)
    cap_total = min(caps["sleeve_max_pct"], 1.0 - sg_w)

    def proj_n(t):
        n = solver_shares(ctx, t, sleeve[t]["weight"])
        if t in forced:
            n = min(n, held_n.get(t, 0) - forced[t])
        return max(n, 0)

    def proj_sleeve():
        return sum(proj_n(t) * (_price(ctx, t) or 0.0) for t in sleeve) / total if total else 0.0

    # gate on the PROJECTION (held-name round-up + a new entry can overshoot even when the
    # current held sleeve is under the cap); only held, non-frozen names are ever force-sold.
    if held_set and proj_sleeve() > cap_total + EPS:
        while proj_sleeve() > cap_total + EPS:
            cands = [t for t in held_set if t in sleeve and not sleeve[t].get("frozen") and _price(ctx, t)
                     and proj_n(t) > target_shares(ctx, t, sleeve[t]["weight"])]
            if not cands:
                break
            t = max(cands, key=lambda x: (proj_n(x) * _price(ctx, x) / total - sleeve[x]["weight"], x))
            forced[t] = held_n.get(t, 0) - (proj_n(t) - 1)
            e = sleeve[t]
            if proj_n(t) == 0 and e["action"] != "EXIT":
                e["action"], e["why"], e["weight"] = "EXIT", "CAP_DRIFT", 0.0
                exits.append({"ticker": t, "why": "CAP_DRIFT"})
            elif e["action"] != "EXIT":
                e["action"], e["why"] = "TRIM", "CAP_DRIFT"
        for t, k in list(forced.items()):
            if sleeve.get(t, {}).get("why") == "CAP_DRIFT" and t in held_set:
                agg_clamp(t, "CAP_SLEEVE_DRIFT", current_weight(ctx, t),
                          proj_n(t) * _price(ctx, t) / total,
                          f"held sleeve {held_sleeve_w:.4f} > cap {cap_total:.4f}: forced sell {k}")

    sg_w = _r6(sg_w)
    sleeve_t = {t: e["weight"] for t, e in sleeve.items() if e["weight"] > EPS}
    core = _r6(max(0.0, 1.0 - sg_w - sum(sleeve_t.values())))
    targets = {"CORE": core, "SGOV": sg_w, **sleeve_t}

    # -- unchanged / no-op counter (LLM layer: sleeve + SGOV share counts)
    def unchanged_for(tg: dict, fs=None) -> bool:
        fs = fs or {}
        names = set(t for t in tg if t != "CORE") | set(held_n)
        if any(k > 0 for k in fs.values()):
            return False
        return all(solver_shares(ctx, t, tg.get(t, 0.0)) == held_n.get(t, 0) for t in names)

    unchanged = unchanged_for(targets, forced)
    prev = digest.get("unchanged_weeks")
    if prev is None:
        prev = (((digest.get("triggers") or {}).get("NOOP") or {}).get("unchanged_weeks"))
    prev = int(_num(prev) or 0)
    if scope is not None:
        unchanged_weeks = prev                    # an event run is not a week
    else:
        unchanged_weeks = prev + 1 if unchanged else 0

    drops_by = {d: sum(1 for x in drops if x["decider"] == d) for d in ids}
    clamps_by = {d: sum(1 for x in clamps if x["decider"] == d) for d in ids}
    vote_table = []
    for d in ids:
        v = val[d]
        vote_table.append({
            "decider": d, "core": v["core"], "sgov": v["sgov_weight"], "sleeve": v["sleeve"],
            "p_qqq_up_20d": v["p_qqq_up_20d"], "dissent_note": v["dissent_note"],
            "unchanged": unchanged_for(v["paper_targets"]), "drops": drops_by[d], "clamps": clamps_by[d],
            "invalid": v["invalid"], "regime_seen": v["regime_seen"],
            "target_weights": v["target_weights"],
            "positions": [{k: p.get(k) for k in ("ticker", "action", "reason_code", "evidence_ids",
                                                    "horizon_weeks", "kill_conditions", "thesis_30w",
                                                    "expected_excess_vs_qqq_pp")}
                          for p in v["positions"].values()],
            "derisk_reason_code": v["derisk"]["reason_code"],
            "derisk_evidence_ids": v["derisk"]["evidence_ids"],
        })
    ps = [val[d]["p_qqq_up_20d"] for d in ids if val[d]["p_qqq_up_20d"] is not None]
    invalid_reasons = []
    n_bad = sum(1 for d in ids if val[d]["invalid"])
    if n_bad >= C.VOTES_REQUIRED_SLEEVE:
        invalid_reasons.append(f"{n_bad} of {len(ids)} decider files invalid; no 2/3 vote possible")
    if ctx.get("evidence_ok") is False:
        invalid_reasons.append("evidence.json has no 'items' list (clerk output missing)")
    return {
        "schema": SCHEMA,
        "run_id": ctx.get("run_id"),
        "valid": not invalid_reasons,
        "invalid_reasons": invalid_reasons,
        "version": C.VERSION,
        "regime": regime,
        "as_of_close_date": as_of.isoformat() if as_of else None,
        "total_usd": _total(ctx),
        "caps": caps,
        "targets": targets,
        "sleeve": sleeve,
        "sgov": sgov_out,
        "exits": exits,
        "no_majority": no_majority,
        "drops": drops,
        "clamps": clamps,
        "vote_table": vote_table,
        "paper_targets": {d: val[d]["paper_targets"] for d in ids},
        "forced_sells": forced,
        "unchanged": unchanged,
        "unchanged_weeks": unchanged_weeks,
        "noop_flag": unchanged_weeks >= C.NOOP_FLAG_WEEKS,
        "p_qqq_up_20d_median": _r6(_median(ps)) if ps else None,
        "event_scope": sorted(scope) if scope is not None else None,
    }


# ---------------------------------------------------------------- context from a run dir
def build_ctx(run_dir_path: str, event_scope=None) -> dict:
    ind = load_json_file(os.path.join(run_dir_path, "indicators.json"))
    book = load_json_file(os.path.join(run_dir_path, "book.json"))
    dpath = os.path.join(run_dir_path, "digest.json")
    digest = load_json_file(dpath) if os.path.exists(dpath) else {}
    evidence = load_json_file(os.path.join(run_dir_path, "evidence.json"))
    total = _num(book.get("total_usd"))
    positions = book.get("positions") or {}
    # sleeve names come from book.held_sleeve (book.py excludes a RESIDUAL_FILLER without a thesis
    # and tags it positions[T].sleeve == "FILLER"): a filler is the solver's free variable, never a
    # voted sleeve position. Older books without held_sleeve: every non-core, non-SGOV position.
    hs_list = book.get("held_sleeve")
    hs_names = {str(x).upper() for x in hs_list} if isinstance(hs_list, list) else None
    held_shares, held_sleeve, prices = {}, {}, {}
    held_sgov = 0.0
    for t, p in positions.items():
        t = str(t).upper()
        if not isinstance(p, dict):
            continue
        px = _num(p.get("price"))
        if px:
            prices[t] = px
        n = int(_num(p.get("shares")) or 0)
        if n <= 0 or t in C.CORE_TICKERS:
            continue
        if t != SGOV and (str(p.get("sleeve") or "").upper() == "FILLER" or
                          (hs_names is not None and t not in hs_names)):
            continue
        held_shares[t] = n
        usd = _num(p.get("usd")) or (n * px if px else 0.0)
        if t == SGOV:
            held_sgov = usd / total
        else:
            held_sleeve[t] = usd / total
    items = evidence.get("items") if isinstance(evidence, dict) else None
    ev = {str(it["id"]): it for it in items or [] if isinstance(it, dict) and it.get("id")}
    regime = ((ind.get("regime") or {}).get("regime")) or C.REGIME_UP
    return {
        "run_id": book.get("run_id") or os.path.basename(os.path.normpath(run_dir_path)),
        "regime": regime,
        "total_usd": total,
        "lot_table": book.get("lot_table") or {},
        "candidate_menu": [str(t).upper() for t in book.get("candidate_menu") or []],
        "held_sleeve": held_sleeve,
        "held_sgov": held_sgov,
        "held_shares": held_shares,
        "prices": prices,
        "caps": book.get("caps_in_effect") or {},
        "evidence_ids": ev,
        "evidence_ok": isinstance(items, list),
        "indicators": ind,
        "digest": digest,
        "as_of_close_date": ind.get("as_of_close_date") or book.get("as_of_close_date"),
        "event_scope": event_scope,
    }


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="v6 decider validation + vote aggregation")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--event-scope", default=None, help="comma-separated tickers an event run may change")
    a = ap.parse_args(argv)
    rd = C.run_dir(a.run_id)
    required = [os.path.join(rd, "evidence.json")] + \
               [os.path.join(rd, "deciders", f"{d}.json") for d in C.DECIDER_IDS] + \
               [os.path.join(rd, "indicators.json"), os.path.join(rd, "book.json")]
    missing = [p for p in required if not os.path.isfile(p)]
    if missing:
        print("aggregate: REFUSED (exit 2, nothing written) - missing required input(s): "
              + ", ".join(os.path.relpath(p, rd) for p in missing)
              + ". Run P1b (evidence clerk) and all three P2 deciders first.", file=sys.stderr)
        return 2
    try:
        load_json_file(os.path.join(rd, "evidence.json"))
    except (ValueError, OSError) as exc:
        print(f"aggregate: REFUSED (exit 2) - evidence.json unparseable: {exc}", file=sys.stderr)
        return 2
    deciders = {}
    for d in C.DECIDER_IDS:
        try:
            deciders[d] = load_json_file(os.path.join(rd, "deciders", f"{d}.json"))
        except (ValueError, OSError):
            deciders[d] = None
    scope = None
    if a.event_scope is not None:
        scope = [t.strip().upper() for t in a.event_scope.split(",") if t.strip()]
    try:
        ctx = build_ctx(rd, scope)
        ctx["run_id"] = a.run_id
        out = aggregate(deciders, ctx)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"aggregate: failed (exit 2, nothing written): {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    out["run_id"] = a.run_id
    path = os.path.join(rd, "aggregate_out.json")
    atomic_write_json(path, out)
    print(json.dumps({"aggregate_out": path, "targets": out["targets"],
                      "sgov_votes": out["sgov"]["votes"],
                      "votes": {t: e["votes"] for t, e in out["sleeve"].items()},
                      "exits": out["exits"], "no_majority": len(out["no_majority"]),
                      "drops": len(out["drops"]), "clamps": len(out["clamps"]),
                      "unchanged_weeks": out["unchanged_weeks"], "noop_flag": out["noop_flag"]},
                     ensure_ascii=False))
    if not out["valid"]:
        print("aggregate: valid=false (exit 3) - " + "; ".join(out["invalid_reasons"]), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
