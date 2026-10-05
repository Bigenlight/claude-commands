#!/usr/bin/env python3
"""us-stock-advisor v5.1 — the accountability loop. A skill that never grades
itself is how we got here.

Install path: ~/.claude/skills/us-stock-advisor/scripts/score_recs.py
Cron (nightly): installed by scripts/install_cron.sh (dry run by default); the
schedule lives in that script only.

Reads  state/recommendations.jsonl  (one mark per run-date, INCLUDING no-ops)
Writes state/scorecard.csv
Emits  --track-record  -> the <track_record> block injected into Phase 2
       --header        -> the Korean header numbers printed at the TOP of every report
       --shadow        -> the paper satellite track's grade vs QQQ

APPEND-ONLY, including this script: the nightly scorer never rewrites a byte of
the ledger. Matured override spreads are APPENDED as `override_score` lines and
merged on read (merged_overrides). Past accounting errors are corrected by
APPENDING a `cash_flow_adjustment` line (report.py --adjust-flow).

Two cumulative curves, always:
  1. actual vs "100% QQQ from day 0"          -> is the whole skill worth running?
  2. actual vs the un-overridden mechanical baseline -> the LLM layer's ISOLATED P&L.
Curve 2 is what the pre-committed kill trigger reads. "actual" is a time-weighted
return: deposits and withdrawals are external flows, never P&L — but a DECLARED
flow is only trusted once the ledger itself corroborates it (reconcile_flows); the
kill trigger and the board-reconvene trigger always read the conservative figure.

LEDGER INTEGRITY (tamper evidence, fail closed):
  * every line carries prev_hash = sha256(previous line)            (internal chain)
  * last_run.json carries ledger_tip_hash + ledger_len              (external anchor)
  * a non-empty ledger with a missing / null / corrupt anchor is TAMPERED. Only an
    absent-or-empty ledger with no anchor is a genuine first run.
  * detection is STICKY: the first writer that sees a fresh tamper stamps
    ledger_tamper_since / ledger_tamper_last into the anchor AND ledger_tamper=true
    onto the lines it appends; ledger_intact() stays False until
    OVERRIDE_SUSPENSION_DAYS after the latest detection, whichever file survives.
  * every writer holds an exclusive flock on the ledger from the tamper check
    through the (atomic, temp file + os.replace) anchor write.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import fcntl
import json
import math
import os
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

HORIZONS = [1, 5, 20]

# a RUN is a portfolio_mark plus these lines up to the next mark (D6)
RUN_SCOPED_TYPES = {"order", "noop", "override", "override_rejected", "veto", "shadow_pick"}
# bookkeeping lines: never de-duplicated, never scored as decisions
BOOKKEEPING_TYPES = {"cash_flow_adjustment", "override_score", "ledger_repair"}
# lines listed in the header when a same-date re-run superseded them (F8: nothing hidden)
SUPERSEDED_LISTED_TYPES = ("order", "override", "override_rejected")

# sticky tamper fields carried forward by every anchor write. The accepted chain
# breaks are the evidence already recorded by a detection: seeing exactly the same
# breaks again is NOT a new tamper, so the window is not extended (N4d).
STICKY_KEYS = ("ledger_tamper_since", "ledger_tamper_last", "ledger_tamper_events",
               "ledger_tamper_reason", "ledger_tamper_accepted_breaks")

# crash-recovery intent written next to the anchor BEFORE a ledger append (N4a)
PENDING_NAME = "ledger_pending.json"
TORN_COPY_PREFIX = "ledger_torn_tail."


class LedgerTornTail(RuntimeError):
    """The ledger's final line is unparseable / unterminated (a crash mid-append).
    Every writer refuses (appending after it would chain onto garbage); the fix is
    the explicit, logged `report.py --repair-torn-tail`."""


class LedgerUnreadable(RuntimeError):
    """The ledger path cannot be read at all (permissions, a directory, I/O)."""


class RepairRefused(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat(timespec="seconds")


def _read_raw(path):
    if not os.path.exists(path):
        return b""
    with open(path, "rb") as f:
        return f.read()


def _decode_lines(raw):
    """Non-blank lines of raw ledger bytes. Decoding never raises: an invalid UTF-8
    byte becomes U+FFFD, which changes that line's hash (so a corrupted line reads
    as not intact) instead of crashing every reader (N4b)."""
    return [ln for ln in raw.decode("utf-8", errors="replace").splitlines() if ln.strip()]


def load_recs(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    return rows


def _line_hash(line):
    import hashlib
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _read_lines(path):
    return _decode_lines(_read_raw(path))


def torn_tail(raw):
    """None, or {start, removed, terminated, why} when the FINAL line of the raw
    ledger bytes is not a complete JSON object (not UTF-8, not JSON, not an
    object) — the signature of a crash mid-append. `start` is the byte offset of
    that line (just after the previous newline); nothing before it is ever part of
    the verdict. A complete, parseable last line is never "torn", terminated or not
    (whether it belongs is the anchor's and the chain's business)."""
    if not raw:
        return None
    body = raw.rstrip(b"\r\n\t ")
    if not body:
        return None
    start = body.rfind(b"\n") + 1
    seg = body[start:]
    why = None
    try:
        if not isinstance(json.loads(seg.decode("utf-8")), dict):
            why = "final line is not a JSON object"
    except UnicodeDecodeError:
        why = "final line is not valid UTF-8 (torn multi-byte character)"
    except ValueError:
        why = "final line is not valid JSON"
    if why is None:
        return None
    terminated = raw.endswith(b"\n")
    if not terminated:
        why += "; unterminated (no trailing newline)"
    return {"start": start, "removed": raw[start:], "terminated": terminated, "why": why}


# ------------------------------------------------------------ locking + writes
@contextlib.contextmanager
def ledger_lock(path):
    """Exclusive flock on the ledger file, yielding the open a+ handle. Every
    writer (append_run, append_anchored, sync_anchor, repair) runs its tamper
    check, its appends AND its anchor write inside ONE of these, so a concurrent
    cron scorer and an interactive run can neither fork the chain nor leave a stale
    anchor. Never nest: flock is per open file description, a second lock in the
    same process on a new handle would deadlock."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a+", encoding="utf-8", errors="replace") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield f
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _chain_lines(existing_tail, recs):
    """Serialize `recs` as chained lines following `existing_tail` (or None).
    Deterministic for the same recs + tail (logged_utc is set once, setdefault)."""
    prev, out, now = existing_tail, [], _now_iso()
    for rec in recs:
        rec.setdefault("logged_utc", now)
        rec["prev_hash"] = _line_hash(prev) if prev is not None else None
        prev = json.dumps(rec, ensure_ascii=False)
        out.append(prev)
    return out


def _write_lines_locked(f, recs):
    """Append chained lines to an OPEN, LOCKED ledger handle in ONE write+fsync."""
    f.seek(0)
    raw = f.read()
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    out = _chain_lines(lines[-1] if lines else None, recs)
    if not out:
        return
    lead = "\n" if raw and not raw.endswith("\n") else ""
    f.seek(0, os.SEEK_END)
    f.write(lead + "".join(ln + "\n" for ln in out))
    f.flush()
    os.fsync(f.fileno())


def _append_locked(f, rec):
    """Append one chained line to an OPEN, LOCKED ledger handle (a+ mode)."""
    _write_lines_locked(f, [rec])


def append_rec(path, rec):
    """RAW append (chain only, anchor NOT moved). Append-only INTEGRITY CHAIN: each
    record carries prev_hash = sha256 of the prior line, so a silent in-place edit or
    a deleted line breaks the chain. Production writers use append_anchored /
    commit_locked; a raw append leaves the anchor behind and therefore reads as
    tampered — which is exactly what an out-of-band write should look like."""
    with ledger_lock(path) as f:
        _append_locked(f, rec)


def _atomic_dump(path, write, newline=None):
    """temp file + fsync + os.replace. The new file keeps the previous file's
    permission bits (0644 for a new file) — mkstemp's 0600 must not leak (N7)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            mode = 0o644
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _atomic_write_json(path, obj):
    """The ANCHOR writer (last_run.json)."""
    _atomic_dump(path, lambda f: json.dump(obj, f, indent=2, ensure_ascii=False))


def _anchor_path(recs_path):
    """The last_run.json that anchors THIS ledger: the configured one for the
    configured ledger, else the sibling file (a scratch/test ledger must never
    re-point — or be judged against — the live anchor)."""
    if os.path.abspath(recs_path) == os.path.abspath(C.RECS_JSONL):
        return C.LAST_RUN_JSON
    return os.path.join(os.path.dirname(os.path.abspath(recs_path)), "last_run.json")


def _pending_path(last_run_path):
    return os.path.join(os.path.dirname(os.path.abspath(last_run_path)), PENDING_NAME)


def _load_anchor(last_run_path):
    """('absent'|'corrupt'|'ok', dict|None)."""
    if not last_run_path or not os.path.exists(last_run_path):
        return "absent", None
    try:
        with open(last_run_path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return "corrupt", None
    if not isinstance(d, dict):
        return "corrupt", None
    return "ok", d


def _load_pending(last_run_path):
    p = _pending_path(last_run_path)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    return d if isinstance(d, dict) and isinstance(d.get("anchor"), dict) else None


def _unlink_quiet(p):
    with contextlib.suppress(FileNotFoundError):
        os.unlink(p)


def _pending_recoverable(pend, lines, anchor_state, tip, ln):
    """True iff the ledger is EXACTLY an interrupted commit: the anchor still names
    the intent's base (tip + length, or 'no anchor, empty ledger'), and the ledger
    now ends at the intent's recorded new tip + length with the base line intact.
    Anything else is not a crash we wrote and stays a tamper."""
    if not pend:
        return False
    try:
        base_len, new_len = int(pend["base_len"]), int(pend["new_len"])
    except (KeyError, TypeError, ValueError):
        return False
    base_tip, new_tip = pend.get("base_tip"), pend.get("new_tip")
    if anchor_state == "ok":
        if base_len != ln or (base_len and base_tip != tip):
            return False
    elif anchor_state == "absent":
        if base_len != 0 or pend.get("base_anchor") != "absent":
            return False
    else:
        return False
    if new_len <= base_len or len(lines) != new_len or _line_hash(lines[-1]) != new_tip:
        return False
    return base_len == 0 or _line_hash(lines[base_len - 1]) == base_tip


def _chain_breaks(lines):
    """[[i, sha256(lines[i-1]) | None], ...] for every line whose prev_hash is not
    the hash of the line before it (i == 0: a first line with a non-null prev_hash =
    top truncation). The recorded predecessor hash pins the segment before each
    break, so an ACCEPTED set of breaks cannot hide a later edit elsewhere."""
    out = []
    for i, ln in enumerate(lines):
        try:
            r = json.loads(ln)
            ph = r.get("prev_hash") if isinstance(r, dict) else "<not an object>"
        except Exception:
            ph = "<unparseable>"
        want = _line_hash(lines[i - 1]) if i else None
        if ph != want:
            out.append([i, want])
    return out


def _new_tip_len(lines, out):
    allx = len(lines) + len(out)
    last = out[-1] if out else (lines[-1] if lines else None)
    return (_line_hash(last) if last is not None else None), allx


def commit_locked(f, path, last_run_path, recs, anchor_base=None):
    """THE writer. Caller holds ledger_lock(path) (handle f). In order:
      0. a torn final line or an unreadable ledger refuses the write (nothing
         appended) — repair it with report.py --repair-torn-tail;
      1. tamper check (ledger_status) against the current anchor; an interrupted
         earlier commit whose pending intent matches the ledger tip is COMPLETED
         first (its anchor is written), never read as tamper (N4a);
      2. on a FRESH tamper, stamp ledger_tamper=true on every line being appended;
      3. write the pending intent (base tip/len, new tip/len, the anchor to write),
         then append all `recs` in one write + fsync;
      4. write the anchor atomically: anchor_base (or the current anchor's content),
         the carried-forward sticky tamper fields (+ a new stamp and the accepted
         chain breaks on a fresh tamper), and the new tip hash + length; then drop
         the intent.
    The anchor always moves to the new tip — the tamper is not laundered because it
    is recorded sticky in both files. Returns the PRE-append ledger_status dict."""
    try:
        raw = _read_raw(path)
    except OSError as e:
        raise LedgerUnreadable(f"ledger unreadable ({type(e).__name__}: {e})")
    tt = torn_tail(raw)
    if tt is not None:
        raise LedgerTornTail(
            f"TORN_TAIL: {tt['why']} ({len(tt['removed'])} bytes at offset {tt['start']}); "
            "nothing appended. Run `report.py --repair-torn-tail --reason \"...\"`")
    pend_path = _pending_path(last_run_path)
    st = ledger_status(path, last_run_path)
    recovered = False
    if st.get("pending_commit"):
        _atomic_write_json(last_run_path, _load_pending(last_run_path)["anchor"])
        _unlink_quiet(pend_path)
        recovered = True
        st = ledger_status(path, last_run_path)
    else:
        _unlink_quiet(pend_path)                       # stale intent (crash before/after)
    now = _now_iso()
    if st["fresh_tamper"]:
        for r in recs:
            r["ledger_tamper"] = True
            r["ledger_tamper_reason"] = st["reason"]
    lines = _decode_lines(raw)
    out = _chain_lines(lines[-1] if lines else None, recs)
    _, old = _load_anchor(last_run_path)
    d = dict(anchor_base) if anchor_base is not None else dict(old or {})
    for k in STICKY_KEYS:
        d.pop(k, None)
        if old and old.get(k) is not None:
            d[k] = old[k]
    if st["fresh_tamper"]:
        d["ledger_tamper_since"] = d.get("ledger_tamper_since") or now
        d["ledger_tamper_last"] = now
        d["ledger_tamper_events"] = int(d.get("ledger_tamper_events") or 0) + 1
        d["ledger_tamper_reason"] = st["reason"]
        d["ledger_tamper_accepted_breaks"] = st.get("chain_breaks") or []
    d["ledger_tip_hash"], d["ledger_len"] = _new_tip_len(lines, out)
    if out:
        base_tip = _line_hash(lines[-1]) if lines else None
        _atomic_dump(pend_path, lambda fh: json.dump(
            {"base_len": len(lines), "base_tip": base_tip, "base_anchor": st["anchor"],
             "new_len": d["ledger_len"], "new_tip": d["ledger_tip_hash"],
             "written_utc": now, "anchor": d}, fh, indent=2, ensure_ascii=False))
        _write_lines_locked(f, recs)
        tip, ln = tip_and_len(path)
        d["ledger_tip_hash"], d["ledger_len"] = tip, ln
    _atomic_write_json(last_run_path, d)
    _unlink_quiet(pend_path)
    st["recovered_pending_commit"] = recovered
    return st


def append_anchored(path, rec, last_run_path=None):
    """Every legitimate append OUTSIDE report.append_run (halt, cash-flow
    adjustment, override_score, ledger_repair) goes through here: one line, tamper
    check + append + atomic anchor write under one lock. Returns True iff the
    ledger was intact (no fresh tamper and no sticky tamper window) before the
    append."""
    if last_run_path is None:
        last_run_path = _anchor_path(path)
    with ledger_lock(path) as f:
        st = commit_locked(f, path, last_run_path, [rec])
    return st["intact"]


def append_many_anchored(path, recs, last_run_path=None, anchor_base=None):
    """All-or-nothing variant of append_anchored for a list of records."""
    if last_run_path is None:
        last_run_path = _anchor_path(path)
    with ledger_lock(path) as f:
        st = commit_locked(f, path, last_run_path, list(recs), anchor_base)
    return st["intact"]


def repair_torn_tail(path, last_run_path=None, reason=""):
    """THE ONE DOCUMENTED EXCEPTION TO APPEND-ONLY. Drops ONLY an unparseable /
    unterminated FINAL line (torn_tail), after saving its exact bytes to
    <state>/ledger_torn_tail.<utc>.bin, then APPENDS a `ledger_repair` line saying
    what was removed (through commit_locked, so a real tamper underneath — a
    truncation, relink or mid-file edit — is still detected, stamped and sticky).
    Refuses (RepairRefused, nothing touched) when the final line is intact."""
    if last_run_path is None:
        last_run_path = _anchor_path(path)
    if not str(reason or "").strip():
        raise RepairRefused("--reason is required (stored on the ledger_repair line)")
    import hashlib
    with ledger_lock(path) as f:
        raw = _read_raw(path)
        tt = torn_tail(raw)
        if tt is None:
            raise RepairRefused("the final ledger line is a complete JSON object: nothing to "
                                "repair; nothing touched (a tamper is never 'repaired')")
        stamp = _now().strftime("%Y%m%dT%H%M%SZ")
        copy = os.path.join(os.path.dirname(os.path.abspath(last_run_path)),
                            f"{TORN_COPY_PREFIX}{stamp}.bin")
        with open(copy, "wb") as g:
            g.write(tt["removed"])
            g.flush()
            os.fsync(g.fileno())
        os.chmod(copy, 0o644)
        os.truncate(path, tt["start"])
        f.seek(0)
        rec = {"type": "ledger_repair", "date": _now().date().isoformat(),
               "action": "REPAIR_TORN_TAIL", "ticker": None,
               "removed_bytes": len(tt["removed"]),
               "removed_sha256": hashlib.sha256(tt["removed"]).hexdigest(),
               "removed_terminated": tt["terminated"], "removed_copy": os.path.basename(copy),
               "removed_at_offset": tt["start"], "why": tt["why"], "reason": str(reason)}
        st = commit_locked(f, path, last_run_path, [rec])
    return rec, st


def tip_and_len(path):
    """(sha256 of the last line, number of lines). The EXTERNAL anchor persisted in
    last_run.json each run, so a ledger that is deleted, truncated, or re-linked by
    a forger who cannot also rewrite last_run.json is detectable."""
    lines = _read_lines(path)
    return (_line_hash(lines[-1]) if lines else None, len(lines))


def read_anchor(last_run_path):
    """(expected_tip_hash, expected_len) from last_run.json, or (None, None) when
    the file is absent or unreadable. NOTE: (None, None) is NOT "intact" for a
    non-empty ledger — ledger_status() fails closed on it."""
    st, d = _load_anchor(last_run_path)
    if st != "ok":
        return (None, None)
    return d.get("ledger_tip_hash"), d.get("ledger_len")


def _anchor_matches(lines, expected_tip, expected_len):
    if expected_len is None or expected_len <= 0:
        return not (expected_len == 0 and lines)
    return len(lines) == expected_len and _line_hash(lines[-1]) == expected_tip


def _verify_lines(lines, expected_tip=None, expected_len=None):
    anchored = expected_len is not None and expected_len > 0
    if anchored:
        if len(lines) < expected_len:
            return False                      # delete / truncate-empty / bottom-truncate
        if not lines or _line_hash(lines[-1]) != expected_tip:
            return False                      # re-linked forgery / any tip change
    if not lines:
        return not anchored                   # empty ok only with no content anchor
    return not _chain_breaks(lines)


def verify_chain(path, expected_tip=None, expected_len=None):
    """Chain + anchor match only (no sticky / missing-anchor policy; that is
    ledger_status). Two independent guards:

    (1) EXTERNAL ANCHOR: once any run has recorded a length>0, the ledger may never
        be empty, shorter than that length, or carry a different last-line hash.
    (2) INTERNAL prev_hash chain: catches a naive mid-file edit / top truncation.
    A torn final line is never a verified chain.
    """
    raw = _read_raw(path)
    if torn_tail(raw) is not None:
        return False
    return _verify_lines(_decode_lines(raw), expected_tip, expected_len)


def _parse_utc(x):
    if not x:
        return None
    try:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def ledger_status(path, last_run_path=None):
    """Full integrity verdict (never raises):
      intact          False on a fresh tamper, inside a sticky window, on a torn
                      final line, or when the ledger cannot be read
      fresh_tamper    the anchor/chain check fails right now with NEW evidence
                      (the exact chain breaks a previous detection already recorded
                      — ledger_tamper_accepted_breaks — are not new: N4d)
      reason          why (fresh, torn, unreadable) — None when intact/sticky-only
      tamper_since    first recorded detection (anchor, else earliest flagged line)
      sticky_until    latest detection + OVERRIDE_SUSPENSION_DAYS (None if never)
      anchor          'ok' | 'absent' | 'corrupt' | 'unusable'
      torn_tail       None | {why, bytes, terminated} (judged on the lines before it)
      pending_commit  an interrupted commit the next writer completes (not tamper)
      chain_breaks    [[i, predecessor hash], ...]
    Fail closed (F2): a non-empty ledger with an absent/corrupt anchor, or one whose
    ledger_len/ledger_tip_hash is null or malformed, is a fresh tamper. Only an
    absent-or-empty ledger with no anchor is a genuine first run."""
    if last_run_path is None:
        last_run_path = _anchor_path(path)
    base = {"intact": False, "fresh_tamper": False, "tamper_since": None,
            "sticky_until": None, "sticky_active": False, "anchor": None,
            "torn_tail": None, "pending_commit": False, "chain_breaks": [],
            "unreadable": False}
    try:
        raw = _read_raw(path)
    except OSError as e:
        base.update(reason=f"LEDGER_UNREADABLE: {type(e).__name__}: {e}", unreadable=True)
        return base
    tt = torn_tail(raw)
    lines = _decode_lines(raw[:tt["start"]] if tt else raw)
    ast, d = _load_anchor(last_run_path)
    reasons, pending_ok, accepted = [], False, None
    pend = _load_pending(last_run_path)
    tip = ln = None
    if ast == "ok":
        tip, ln = d.get("ledger_tip_hash"), d.get("ledger_len")
        usable = (isinstance(ln, int) and not isinstance(ln, bool) and ln >= 0
                  and (ln == 0 or isinstance(tip, str)))
        if not usable:
            ast = "unusable"
        accepted = d.get("ledger_tamper_accepted_breaks")
    if ast == "ok":
        if not _anchor_matches(lines, tip, ln):
            if _pending_recoverable(pend, lines, "ok", tip, ln):
                pending_ok = True
            elif ln == 0:
                reasons.append("anchor claims an empty ledger but the ledger has content")
            else:
                reasons.append("ledger does not match its anchor (tip hash / length)")
    elif lines:
        if _pending_recoverable(pend, lines, ast, None, 0):
            pending_ok = True
        else:
            reasons.append(f"non-empty ledger with {ast} anchor "
                           f"({os.path.basename(str(last_run_path))})")
    if pending_ok:
        accepted = pend["anchor"].get("ledger_tamper_accepted_breaks", accepted)
    breaks = _chain_breaks(lines)
    if breaks and (ast != "ok" or breaks != accepted):
        reasons.append("prev_hash chain broken at line(s) "
                       + ", ".join(str(b[0] + 1) for b in breaks[:5])
                       + ("..." if len(breaks) > 5 else ""))
    fresh = bool(reasons)
    torn_reason = None
    if tt:
        torn_reason = (f"TORN_TAIL: {tt['why']} ({len(tt['removed'])} bytes); writers refuse "
                       "until `report.py --repair-torn-tail` drops it")

    # sticky: anchor stamps (+ a pending intent's) + flagged lines, whichever survives
    stamps, since = [], []
    for src in (d, (pend or {}).get("anchor") if pending_ok else None):
        if not src:
            continue
        for k in ("ledger_tamper_last", "ledger_tamper_since"):
            t = _parse_utc(src.get(k))
            if t:
                stamps.append(t)
        t = _parse_utc(src.get("ledger_tamper_since"))
        if t:
            since.append(t)
    for ln_ in lines:
        if '"ledger_tamper": true' not in ln_:
            continue
        try:
            r = json.loads(ln_)
        except Exception:
            continue
        if isinstance(r, dict) and r.get("ledger_tamper") is True:
            t = _parse_utc(r.get("logged_utc")) or _parse_utc(r.get("date"))
            if t:
                stamps.append(t)
                since.append(t)
    now = _now()
    if fresh:
        stamps.append(now)
        since.append(now)
    sticky_until = (max(stamps) + timedelta(days=C.OVERRIDE_SUSPENSION_DAYS)) if stamps else None
    in_window = sticky_until is not None and now < sticky_until
    reason = "; ".join(reasons + ([torn_reason] if torn_reason else [])) or None
    base.update({
        "intact": not fresh and not in_window and tt is None,
        "fresh_tamper": fresh,
        "reason": reason,
        "tamper_since": min(since).isoformat(timespec="seconds") if since else None,
        "sticky_until": sticky_until.isoformat(timespec="seconds") if sticky_until else None,
        "sticky_active": in_window,
        "anchor": ast,
        "torn_tail": ({"why": tt["why"], "bytes": len(tt["removed"]),
                       "terminated": tt["terminated"]} if tt else None),
        "pending_commit": pending_ok,
        "chain_breaks": breaks,
    })
    return base


def ledger_intact(path, last_run_path=None):
    """The oracle validate.py calls. False on any fresh tamper AND for
    OVERRIDE_SUSPENSION_DAYS after the latest recorded detection (sticky), so a
    normal --log after a tamper cannot restore override privileges."""
    return ledger_status(path, last_run_path)["intact"]


def sync_anchor(recs_path, last_run_path=None):
    """Re-point last_run.json at the current tip+len, atomically, under the ledger
    lock, WITHOUT laundering: a fresh tamper is stamped sticky first (commit of zero
    lines). Do not call while holding ledger_lock."""
    if last_run_path is None:
        last_run_path = _anchor_path(recs_path)
    with ledger_lock(recs_path) as f:
        commit_locked(f, recs_path, last_run_path, [])


def repairs(recs):
    """Every ledger_repair line (header: 원장_복구_이력)."""
    return [{k: r.get(k) for k in ("date", "removed_bytes", "removed_sha256", "removed_copy",
                                    "why", "reason", "logged_utc")}
            for r in recs if r.get("type") == "ledger_repair"]


# ------------------------------------------------------- de-dup + cash flows
def _run_ids(recs):
    """Per rec: the index of the portfolio_mark that opens its run, or None for a
    line that belongs to no run (bookkeeping, halts, lines before the first mark)."""
    ids, cur = [], None
    for i, r in enumerate(recs):
        t = r.get("type")
        if t == "portfolio_mark":
            cur = i
            ids.append(i)
        elif t in RUN_SCOPED_TYPES:
            ids.append(cur)
        else:
            ids.append(None)
    return ids


def _effective_mark_ids(recs):
    last_for_date = {}
    for i, r in enumerate(recs):
        if r.get("type") == "portfolio_mark":
            last_for_date[r.get("date")] = i
    return set(last_for_date.values())


def effective_recs(recs):
    """D6: for each mark date only the LAST run survives (a same-date re-run
    supersedes the earlier one instead of double counting it). pipeline_halt,
    cash_flow_adjustment and override_score lines are never dropped. Input order
    is preserved. Nothing is ever deleted from the ledger — this is a read view.
    Use satellite_history_recs / merged_overrides / superseded_lines where a
    superseded run may still have been EXECUTED."""
    ids = _run_ids(recs)
    keep_marks = _effective_mark_ids(recs)
    return [r for r, rid in zip(recs, ids) if rid is None or rid in keep_marks]


def superseded_run_count(recs):
    marks = [r for r in recs if r.get("type") == "portfolio_mark"]
    return len(marks) - len({r.get("date") for r in marks})


def superseded_lines(recs):
    """Order / override / override_rejected lines of superseded same-date runs,
    listed in the header so a de-dup never hides something that may have been
    executed."""
    ids = _run_ids(recs)
    keep = _effective_mark_ids(recs)
    out = []
    for r, rid in zip(recs, ids):
        if rid is None or rid in keep or r.get("type") not in SUPERSEDED_LISTED_TYPES:
            continue
        out.append({k: r.get(k) for k in ("date", "type", "ticker", "action", "shares",
                                           "usd", "direction", "logged_utc")
                    if r.get(k) is not None})
    return out


def satellite_history_recs(recs):
    """Order records that count toward satellite min-hold / round-trip history,
    INCLUDING orders of superseded same-date runs (they may have been executed
    before the re-run). An order repeated verbatim by the superseding run of the
    same date (same date, ticker, action) is one decision, counted once (the
    effective copy is preferred). Returns copies, ledger order, each with a
    `superseded` flag. Core lines are included; callers filter by sleeve."""
    ids = _run_ids(recs)
    keep = _effective_mark_ids(recs)
    chosen = {}
    for i, (r, rid) in enumerate(zip(recs, ids)):
        if r.get("type") != "order":
            continue
        sup = rid is not None and rid not in keep
        key = (r.get("date"), str(r.get("ticker") or "").upper(), r.get("action"))
        cur = chosen.get(key)
        if cur is None or not sup or cur[1]["superseded"]:
            o = dict(r)
            o["superseded"] = sup
            chosen[key] = (i, o)
    return [o for _, o in sorted(chosen.values(), key=lambda x: x[0])]


def _mark_flow(m):
    """Signed external flow declared on one mark: v5.1 external_flow_usd, else
    the legacy deposit_usd."""
    v = m.get("external_flow_usd")
    if v is None:
        v = m.get("deposit_usd")
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def day_flow_total(marks):
    """Net declared external flow for ONE date across its marks (ledger order,
    superseded included). A mark carrying day_flow_total_usd (report.py v5.1-fix:
    prior same-date total + this run's --deposit/--withdraw) states the day total
    outright, so two genuine same-date deposits are summed and a re-log that
    declares nothing keeps the earlier one. Legacy marks without it: the LAST
    NON-ZERO declared flow (a forced re-log that repeated --deposit is not double
    counted)."""
    total = 0.0
    for m in marks:
        if m.get("day_flow_total_usd") is not None:
            try:
                total = float(m["day_flow_total_usd"])
            except (TypeError, ValueError):
                pass
            continue
        f = _mark_flow(m)
        if f != 0.0:
            total = f
    return total


def _pos_shares(d):
    try:
        return float((d or {}).get("shares") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _pos_px(d):
    try:
        sh, usd = float(d.get("shares") or 0.0), float(d.get("usd"))
        return usd / sh if sh else None
    except (TypeError, ValueError, AttributeError):
        return None


def implied_flow(a, b):
    """External flow the LEDGER implies between two marks (a before b):

        F = cash_b − cash_a + Σ_t (shares_b − shares_a) × p̂_t

    i.e. the change in cash NOT explained by position changes, each share change
    valued at p̂_t = the mean of the two marks' per-share prices (whichever exist).
    Trades net to ~0 (cash <-> shares); what remains is money that entered or left.
    It is independent of price moves on unchanged holdings. LIMITS: (1) the true
    execution price is unknown — error ≤ |Δshares| × |px_b − px_a| / 2 per ticker;
    (2) dividends, fees, FX conversions and interest inside the account show up as
    "flow"; (3) orders logged but never executed are irrelevant (observed position
    deltas are used, not logged orders). None when cash or a changed position's
    price is unavailable (unverifiable)."""
    try:
        ca, cb = float(a["cash_usd"]), float(b["cash_usd"])
    except (KeyError, TypeError, ValueError):
        return None
    pa, pb = a.get("positions") or {}, b.get("positions") or {}
    if not isinstance(pa, dict) or not isinstance(pb, dict):
        return None
    f = cb - ca
    for t in set(pa) | set(pb):
        dsh = _pos_shares(pb.get(t)) - _pos_shares(pa.get(t))
        if abs(dsh) < 1e-9:
            continue
        pxs = [p for p in (_pos_px(pa.get(t)), _pos_px(pb.get(t))) if p]
        if not pxs:
            return None
        f += dsh * sum(pxs) / len(pxs)
    return round(f, 2)


def reconcile_flows(recs):
    """Every declared external flow (mark --deposit/--withdraw day totals and every
    cash_flow_adjustment), each with its receiving effective mark and a verdict.
    All declared flows landing on the same receiving mark b are reconciled JOINTLY
    against I = implied_flow(previous effective mark, b). With D = Σ declared and
    dev = D − I, the group is VERIFIED only when the ledger CORROBORATES it:
        |dev| <= UNEXPLAINED_FLOW_WARN_PCT × total_b          (absolute, book-relative)
        |dev| <= FLOW_VERIFY_MAX_REL_DEV × |D|                  (relative to the claim:
                                                                 a near-zero I cannot
                                                                 verify a non-trivial D)
        Σ|dev| over every verified group so far + |dev|
             <= FLOW_VERIFY_CUM_DEV_MAX_PCT × total_b           (cumulative, ledger order)
    verified None  = no effect on TWR (received by the first mark, or no receiving
                     mark at all); verified False = contradicted or unverifiable.
    Returns (entries, undeclared) where `undeclared` lists effective mark dates
    whose implied flow exceeds the tolerance with NOTHING declared."""
    eff_marks = [r for r in effective_recs(recs) if r.get("type") == "portfolio_mark"]
    eff_dates = [m.get("date") for m in eff_marks]
    pos = {d: i for i, d in enumerate(eff_dates)}
    by_date = {}
    for m in recs:
        if m.get("type") == "portfolio_mark":
            by_date.setdefault(m.get("date"), []).append(m)
    entries = []
    for d, ms in by_date.items():
        tot = day_flow_total(ms)
        if abs(tot) > 1e-9:
            why = [m.get("relog_reason") for m in ms if m.get("relog_reason")]
            entries.append({"kind": "declared", "date": d, "effective_date": d,
                            "receiving_mark": d, "amount_usd": round(tot, 2),
                            "reason": "; ".join(why) if why else "--deposit/--withdraw",
                            "n_marks": len(ms)})
    for a in recs:
        if a.get("type") != "cash_flow_adjustment":
            continue
        try:
            amt = float(a.get("flow_usd") or 0.0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(amt):
            continue
        eff = str(a.get("effective_date") or "")
        recv = next((d for d in eff_dates if str(d) >= eff), None)
        entries.append({"kind": "adjustment", "date": a.get("date"), "effective_date": eff,
                        "receiving_mark": recv, "amount_usd": round(amt, 2),
                        "reason": a.get("reason"), "basis": a.get("basis"),
                        "flow_krw": a.get("flow_krw"), "logged_utc": a.get("logged_utc")})
    groups = {}
    for e in entries:
        groups.setdefault(e["receiving_mark"], []).append(e)
    cum_dev = 0.0
    # ledger (chronological) order, so an earlier verdict never depends on a later line
    for recv in sorted(groups, key=lambda r: (pos.get(r) is None, pos.get(r, 0))):
        es = groups[recv]
        i = pos.get(recv)
        verdict = {"verified": None, "implied_usd": None, "declared_total_usd":
                   round(sum(e["amount_usd"] for e in es), 2), "deviation_usd": None,
                   "tolerance_usd": None, "note": None}
        if i is None:
            verdict["note"] = "no portfolio_mark on/after the effective date: no effect"
        elif i == 0:
            verdict["note"] = "received by the first mark: no effect on TWR"
        else:
            b = eff_marks[i]
            imp = implied_flow(eff_marks[i - 1], b)
            total_b = float(b.get("total_usd") or 0.0)
            tol = C.UNEXPLAINED_FLOW_WARN_PCT * total_b
            verdict["tolerance_usd"] = round(tol, 2)
            if imp is None:
                verdict["verified"] = False
                verdict["note"] = "unverifiable: cash/positions missing on an adjacent mark"
            else:
                D = verdict["declared_total_usd"]
                dev = D - imp
                ad = abs(dev)
                verdict.update(implied_usd=imp, deviation_usd=round(dev, 2))
                span = f"between {eff_marks[i - 1].get('date')} and {recv}"
                if ad > tol + 1e-9:
                    verdict["verified"] = False
                    verdict["note"] = f"ledger implies {imp:+.2f} {span}"
                elif ad > C.FLOW_VERIFY_MAX_REL_DEV * abs(D) + 1e-9:
                    verdict["verified"] = False
                    verdict["note"] = (f"not corroborated: ledger implies {imp:+.2f} {span}; "
                                       f"deviation {dev:+.2f} > FLOW_VERIFY_MAX_REL_DEV of the "
                                       f"declared {D:+.2f}")
                elif cum_dev + ad > C.FLOW_VERIFY_CUM_DEV_MAX_PCT * total_b + 1e-9:
                    verdict["verified"] = False
                    verdict["note"] = (f"cumulative deviation of verified flows would reach "
                                       f"{cum_dev + ad:.2f} > FLOW_VERIFY_CUM_DEV_MAX_PCT of the "
                                       f"book ({C.FLOW_VERIFY_CUM_DEV_MAX_PCT * total_b:.2f})")
                else:
                    verdict["verified"] = True
                    cum_dev += ad
        verdict["cumulative_deviation_usd"] = round(cum_dev, 2)
        for e in es:
            e.update(verdict)
    undeclared = []
    for i in range(1, len(eff_marks)):
        d = eff_dates[i]
        if d in groups:
            continue
        imp = implied_flow(eff_marks[i - 1], eff_marks[i])
        tol = C.UNEXPLAINED_FLOW_WARN_PCT * float(eff_marks[i].get("total_usd") or 0.0)
        if imp is not None and abs(imp) > tol:
            undeclared.append(d)
    return entries, undeclared


def external_flows(recs, verified_only=False, _entries=None, conservative=False, _undeclared=None):
    """{receiving effective mark date: net external flow USD} (D5).
    1. marks: per date, day_flow_total() across ALL same-date marks.
    2. cash_flow_adjustment lines: flow_usd summed onto the first effective mark
       with date >= effective_date (flows arrive just before the receiving mark).
    verified_only=True drops every flow reconcile_flows() marks verified=False.
    conservative=True (the curve the kill trigger reads) never lets an unverified
    or undeclared flow HELP: an unverified group counts as max(0, declared,
    implied) — the largest inflow any source claims, so a contradicted withdrawal
    is P&L and a poisoned genuine deposit is still removed — and an UNDECLARED
    implied inflow above the tolerance (미확인_입출금_의심) counts as a deposit."""
    if _entries is None:
        _entries, _undeclared = reconcile_flows(recs)
    groups = {}
    for e in _entries:
        if e["receiving_mark"] is None:
            continue
        groups.setdefault(e["receiving_mark"], []).append(e)
    flows = {}
    for recv, es in groups.items():
        dsum = sum(e["amount_usd"] for e in es)
        if es[0].get("verified") is False:
            if conservative:
                imp = es[0].get("implied_usd")
                dsum = max(0.0, dsum, float(imp) if imp is not None else 0.0)
            elif verified_only:
                continue
        flows[recv] = dsum
    if conservative and _undeclared:
        marks = [r for r in effective_recs(recs) if r.get("type") == "portfolio_mark"]
        for a, b in zip(marks, marks[1:]):
            if b.get("date") in _undeclared and b.get("date") not in flows:
                imp = implied_flow(a, b)
                if imp is not None and imp > 0:
                    flows[b.get("date")] = imp
    return {d: round(v, 2) for d, v in flows.items() if abs(v) > 1e-9}


def _num_or_none(x):
    try:
        f = float(x)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def merged_overrides(recs):
    """Override recs — effective AND superseded (F8: a superseded run's override may
    have been executed) — with spread_vs_baseline_pp and `graded` filled from the
    append-only override_score lines (match: ref_logged_utc + ticker). A verbatim
    re-log of the same override on the same date (date, ticker, direction, weights)
    is one decision, counted once (the effective copy preferred). Score match key:
    ref_prev_hash (v5.1.2 score lines), else legacy ref_logged_utc + ticker.
      graded True   spread is a real number (may be 0.0 = no effect)
      graded False  matured but UNGRADEABLE (e.g. a legacy core override without the
                    persisted equity weights): spread None -> neither good nor bad
      graded None   not matured yet
    A legacy override whose spread was written in place keeps it. Returns copies."""
    scores, legacy = {}, {}
    for r in recs:
        if r.get("type") != "override_score":
            continue
        if r.get("ref_prev_hash") is not None:
            # v5.1.2: the override line's own prev_hash — unique per ledger line, so
            # two overrides logged in the same second can never share a grade (N7)
            scores.setdefault(r["ref_prev_hash"], r)
        else:
            legacy.setdefault((r.get("ref_logged_utc"), r.get("ticker")), r)
    ids = _run_ids(recs)
    keep = _effective_mark_ids(recs)
    chosen = {}
    for i, (r, rid) in enumerate(zip(recs, ids)):
        if r.get("type") != "override":
            continue
        sup = rid is not None and rid not in keep
        o = dict(r)
        o["superseded"] = sup
        sc = scores.get(r.get("prev_hash")) if r.get("prev_hash") is not None else None
        if sc is None:
            sc = legacy.get((r.get("logged_utc"), r.get("ticker")))
        if o.get("spread_vs_baseline_pp") is not None:
            o["graded"] = True
        elif sc is not None:
            o["spread_vs_baseline_pp"] = sc.get("spread_vs_baseline_pp")
            g = sc.get("graded")
            o["graded"] = (o["spread_vs_baseline_pp"] is not None) if g is None else bool(g)
            if not o["graded"]:
                o["spread_vs_baseline_pp"] = None
            o["score_method"] = sc.get("method")
        else:
            o["graded"] = None
        key = (r.get("date"), r.get("ticker"), r.get("direction"),
               r.get("baseline_equity_pct"), r.get("realised_equity_pct"))
        cur = chosen.get(key)
        if cur is None or not sup or cur[1]["superseded"]:
            chosen[key] = (i, o)
    return [o for _, o in sorted(chosen.values(), key=lambda x: x[0])]


# ------------------------------------------------------------------ pricing
def price_frame(tickers, csv_path=None):
    if csv_path:
        import pandas as pd
        return pd.read_csv(csv_path, parse_dates=["Date"]).set_index("Date").sort_index()
    import yfinance as yf
    d = yf.download(list(dict.fromkeys(tickers)), period="2y", interval="1d",
                    auto_adjust=True, progress=False)
    return d["Close"] if "Close" in d else d


def _safe_price_frame(tickers, csv_path=None):
    """price_frame, or None on any failure (network, proxy, parse)."""
    try:
        return price_frame(tickers, csv_path)
    except Exception as e:
        sys.stderr.write(f"WARNING: price data unavailable ({type(e).__name__}: {e})\n")
        return None


def _bench_series(df):
    """The benchmark close series, or None when missing/empty (F9)."""
    if df is None:
        return None
    try:
        s = df[C.BENCHMARK].dropna() if hasattr(df, "columns") else df.dropna()
    except Exception:
        return None
    return s if len(s) else None


def fwd(df, ticker, date, n):
    if ticker not in df.columns:
        return None
    s = df[ticker].dropna()
    idx = s.index[s.index >= date]
    if len(idx) == 0:
        return None
    i0 = s.index.get_loc(idx[0])
    if i0 + n >= len(s):
        return None
    return float(s.iloc[i0 + n] / s.iloc[i0] - 1.0) * 100.0


def _bracket_outcome(df, r, horizon):
    """Close-based bracket for a shadow pick: the first close (entry session
    onward, at most `horizon` sessions after entry) at/above target -> TARGET,
    at/below stop -> STOP; otherwise OPEN once the horizon has fully elapsed, and
    PENDING while it has not (unmatured is never tallied as OPEN). Entry = first
    close on/after the pick date, so no look-ahead."""
    t, stop, target = r.get("ticker"), r.get("stop"), r.get("target")
    if t not in getattr(df, "columns", []) or stop is None or target is None:
        return None
    import pandas as pd
    s = df[t].dropna()
    idx = s.index[s.index >= pd.Timestamp(r["date"])]
    if len(idx) == 0:
        return "PENDING"
    i0 = s.index.get_loc(idx[0])
    for px in s.iloc[i0:i0 + horizon + 1]:
        if float(px) >= float(target):
            return "TARGET"
        if float(px) <= float(stop):
            return "STOP"
    return "OPEN" if i0 + horizon < len(s) else "PENDING"


# ------------------------------------------------------------------ scoring
def _grade_override(df, o):
    """(spread_pp | None, graded, method, extra) for ONE matured override, or None
    when it has not matured yet.

    Core/equity-weight override (v5.1-fix lines carry both weights):
        spread_pp = (realised_equity_pct − baseline_equity_pct)/100 × QQQ fwd return
                    over the override horizon (expires_after_trading_days), in pp.
        A de-risk (realised < baseline) before a fall scores > 0, before a rally < 0.
    Legacy satellite-ticker override without weights: ticker − QQQ over 20d (the
    pre-fix formula; still meaningful for a non-core ticker).
    Legacy CORE override without weights: UNGRADED (graded False, spread None) —
    the old ticker-vs-itself formula scored 0.0 by construction and read as a miss."""
    import pandas as pd
    d = pd.Timestamp(o["date"])
    h = int(o.get("expires_after_trading_days") or C.OVERRIDE_EXPIRY_TRADING_DAYS)
    rw, bw = _num_or_none(o.get("realised_equity_pct")), _num_or_none(o.get("baseline_equity_pct"))
    tkr = str(o.get("ticker") or C.BENCHMARK).upper()
    if rw is not None and bw is not None:
        q = fwd(df, C.BENCHMARK, d, h)
        if q is None:
            return None
        return (round((rw - bw) / 100.0 * q, 2), True, "equity_weight_x_qqq_fwd",
                {"horizon_trading_days": h, "qqq_fwd_pct": round(q, 2),
                 "equity_delta_pp": round(rw - bw, 2)})
    if tkr not in C.CORE_TICKERS:
        a, b = fwd(df, tkr, d, 20), fwd(df, C.BENCHMARK, d, 20)
        if a is None or b is None:
            return None
        return (round(a - b, 2), True, "legacy_ticker_excess_20d", {"horizon_trading_days": 20})
    q = fwd(df, C.BENCHMARK, d, h)
    if q is None:
        return None
    return (None, False, "ungraded_no_equity_weights", {"horizon_trading_days": h})


def score(recs_path, out_csv, csv_path=None):
    """Grade every EFFECTIVE decision at +1/+5/+20d vs matched-window QQQ. Never
    rewrites the ledger: a matured override spread (effective or superseded) is
    APPENDED as an override_score line (append_anchored) exactly once. When the
    benchmark price data is unavailable NOTHING is written (no all-empty cache)
    and None is returned."""
    recs = load_recs(recs_path)
    if not recs:
        sys.stderr.write("no recs to score\n")
        return []
    import pandas as pd
    eff = [r for r in effective_recs(recs) if r.get("type") not in BOOKKEEPING_TYPES]
    ovrs = merged_overrides(recs)
    tickers = sorted({r["ticker"] for r in eff if r.get("ticker")}
                     | {o["ticker"] for o in ovrs if o.get("ticker")} | {C.BENCHMARK})
    df = _safe_price_frame(tickers, csv_path)
    if _bench_series(df) is None:
        sys.stderr.write("ERROR: benchmark price data unavailable; scorecard NOT written, "
                         "nothing graded\n")
        return None
    today = _now().date().isoformat()
    appended = 0
    for o in ovrs:
        if o.get("graded") is not None or not o.get("date"):
            continue
        g = _grade_override(df, o)
        if g is None:
            continue
        spread, graded, method, extra = g
        line = {"type": "override_score", "date": today,
                "ref_logged_utc": o.get("logged_utc"), "ref_date": o.get("date"),
                "ref_prev_hash": o.get("prev_hash"),
                "ticker": o.get("ticker"), "spread_vs_baseline_pp": spread,
                "graded": graded, "method": method, "action": "SCORE"}
        line.update(extra)
        try:
            append_anchored(recs_path, line)
        except (LedgerTornTail, LedgerUnreadable) as e:
            sys.stderr.write(f"WARNING: override_score NOT appended ({e})\n")
            break
        appended += 1
    if appended:
        ovrs = merged_overrides(load_recs(recs_path))

    def _okey(o):
        return o.get("prev_hash") if o.get("prev_hash") is not None else (o.get("logged_utc"), o.get("ticker"))
    merged = {_okey(o): o.get("spread_vs_baseline_pp") for o in ovrs}
    out = []
    for r in eff:
        d = pd.Timestamp(r["date"])
        row = {"date": r["date"], "type": r.get("type"), "ticker": r.get("ticker"),
               "action": r.get("action"), "regime": r.get("regime")}
        for h in HORIZONS:
            rr = fwd(df, r.get("ticker"), d, h) if r.get("ticker") else None
            bb = fwd(df, C.BENCHMARK, d, h)
            row[f"fwd_{h}d"] = None if rr is None else round(rr, 2)
            row[f"qqq_{h}d"] = None if bb is None else round(bb, 2)
            row[f"excess_{h}d"] = (None if rr is None or bb is None else round(rr - bb, 2))
        row["spread_vs_baseline_pp"] = (merged.get(_okey(r))
                                        if r.get("type") == "override" else None)
        if r.get("type") == "shadow_pick":
            row["bracket_outcome"] = _bracket_outcome(df, r, C.SHADOW_HORIZON_DAYS)
        out.append(row)
    fields = []
    for row in out:
        for k in row:
            if k not in fields:
                fields.append(k)
    def _w(f):
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)
    dest = out_csv
    try:
        _atomic_dump(out_csv, _w, newline="")      # keeps the file's permission bits (N7)
    except OSError as e:
        # a read-only state dir must not crash --header / --track-record
        sys.stderr.write(f"WARNING: scorecard NOT written ({type(e).__name__}: {e})\n")
        dest = "(not written)"
    sys.stderr.write(f"scored {len(out)} effective recs -> {dest}"
                     f" ({superseded_run_count(recs)} superseded runs excluded,"
                     f" {appended} override_score appended)\n")
    return out


# ------------------------------------------------------------ shadow track
def binom_sf(k, n, p):
    """P(X >= k) for X ~ Binomial(n, p). Pure python, exact."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return min(1.0, sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def shadow_stats(recs_path, csv_path=None):
    """The paper satellite track, graded at SHADOW_HORIZON_DAYS vs matched-window
    QQQ. Only MATURED picks (horizon fully elapsed) enter the hit rate and the
    TARGET/STOP/OPEN bracket tally; unmatured picks are counted in n_unmatured.
    Graduation is REPORTED, never applied: flipping SATELLITE_REAL_MONEY_ENABLED is
    a human edit to config.py."""
    picks = [r for r in effective_recs(load_recs(recs_path)) if r.get("type") == "shadow_pick"]
    h = C.SHADOW_HORIZON_DAYS
    out = {"n_total": len(picks), "n_graded": 0, "n_unmatured": 0, "hits": 0, "hit_rate": None,
           "mean_excess_20d_pp": None, "dartboard_base_rate": C.DARTBOARD_BASE_RATE,
           "p_value": None, "bracket": {"TARGET": 0, "STOP": 0, "OPEN": 0},
           "price_data_ok": None, "graduation_ok": False,
           "graduation_need": {"min_picks": C.SHADOW_GRADUATION_MIN_PICKS,
                               "max_pvalue": C.SHADOW_GRADUATION_MAX_PVALUE,
                               "min_mean_excess_pp": C.SHADOW_GRADUATION_MIN_MEAN_EXCESS_PP}}
    if not picks:
        return out
    import pandas as pd
    df = _safe_price_frame(sorted({p["ticker"] for p in picks} | {C.BENCHMARK}), csv_path)
    if _bench_series(df) is None:
        out["price_data_ok"] = False
        out["n_unmatured"] = None
        return out
    out["price_data_ok"] = True
    excess = []
    for p in picks:
        d = pd.Timestamp(p["date"])
        a, b = fwd(df, p["ticker"], d, h), fwd(df, C.BENCHMARK, d, h)
        if a is None or b is None:
            out["n_unmatured"] += 1
            continue
        excess.append(a - b)
        bo = _bracket_outcome(df, p, h)
        if bo in out["bracket"]:
            out["bracket"][bo] += 1
    n = len(excess)
    hits = sum(1 for x in excess if x > 0)
    out["n_graded"], out["hits"] = n, hits
    if n:
        mean = sum(excess) / n
        pv = binom_sf(hits, n, C.DARTBOARD_BASE_RATE)
        out["hit_rate"] = round(hits / n, 3)
        out["mean_excess_20d_pp"] = round(mean, 2)
        out["p_value"] = round(pv, 4)
        out["graduation_ok"] = bool(n >= C.SHADOW_GRADUATION_MIN_PICKS
                                    and pv <= C.SHADOW_GRADUATION_MAX_PVALUE
                                    and mean > C.SHADOW_GRADUATION_MIN_MEAN_EXCESS_PP)
    return out


# ------------------------------------------------- cumulative + track record
def _trading_days_between(s, a, b):
    """Sessions in [a, b] on the benchmark's own index. The kill trigger counts
    forward TRADING days, not calendar days and not runs."""
    import pandas as pd
    return int(((s.index >= pd.Timestamp(a)) & (s.index <= pd.Timestamp(b))).sum())


def _valuation_px(s, mark):
    """The benchmark close the mark was valued at: the mark's own last_bar (v5.1),
    else the last bar strictly BEFORE the mark's UTC date (a run never sees its own
    day's close)."""
    import pandas as pd
    lb = mark.get("last_bar")
    sub = s[s.index <= pd.Timestamp(lb)] if lb else s[s.index < pd.Timestamp(mark["date"])]
    return float(sub.iloc[-1])


def _run_px(mark):
    """The CORE_TICKER price the run itself saw (the basis the mechanical baseline
    curve compounds on): v5.1 benchmark_px, else the held core position's usd/shares."""
    px = _num_or_none(mark.get("benchmark_px"))
    if px:
        return px
    return _pos_px((mark.get("positions") or {}).get(C.CORE_TICKER) or {})


def _twr(equity, flows):
    """Unit-value chain: growth *= (V_b − F_b) / V_a. Returns (pct, pnl, net_flow)."""
    growth, pnl, net_flow = 1.0, 0.0, 0.0
    for a, b in zip(equity, equity[1:]):
        va = float(a["total_usd"])
        fb = flows.get(b.get("date"), 0.0)
        vb = float(b["total_usd"]) - fb
        net_flow += fb
        pnl += vb - va
        if va > 0:
            growth *= vb / va
    return ((growth - 1) * 100 if len(equity) > 1 else 0.0), pnl, net_flow


def cumulative(recs_path, csv_path=None):
    """actual (TWR, external flows removed) vs 100%-QQQ-from-day-0, and actual vs
    the mechanical baseline — all over the same effective marks.

    Declared flows are reconciled (reconcile_flows). Two headline figures:
      actual_cum_pct                 every declared flow applied
      actual_cum_pct_verified_flows  unverified flows excluded
    The kill trigger and board_reconvene read actual_cum_pct_conservative = the
    LOWEST of those two and the external_flows(conservative=True) curve (an
    unverified group counts as max(0, declared, implied), an undeclared implied
    inflow above tolerance as a deposit), so an unverified flow can never disarm
    them — not even by poisoning a genuine deposit's group.

    Price basis (F10): QQQ_cum is dividend-adjusted (total return), while the
    mechanical baseline curve compounds on the run-time close (price return). The
    gap is reported (dividend_basis_gap_pp) and the kill trigger compares against
    mechanical_baseline_cum_pct_tr_bound = baseline × (QQQ TR / QQQ PR over the
    window), which is exact for a 100%-equity baseline and an upper bound on the
    baseline's dividends otherwise (conservative: it can only arm the trigger
    earlier). mechanical_baseline_cum_pct / llm_layer_spread_pp stay price-basis
    for continuity.

    When benchmark prices are unavailable (F9) price_data_ok is False and the
    maturity-gated triggers read "unknown", never False."""
    recs = load_recs(recs_path)
    eff = effective_recs(recs)
    equity = [r for r in eff if r.get("type") == "portfolio_mark"]
    lst = ledger_status(recs_path, _anchor_path(recs_path))
    if not equity:
        return None
    s = _bench_series(_safe_price_frame([C.BENCHMARK], csv_path))
    try:
        qqq_ret = (_valuation_px(s, equity[-1]) / _valuation_px(s, equity[0]) - 1) * 100
    except Exception:
        qqq_ret = None
    price_ok = s is not None and qqq_ret is not None

    entries, undeclared = reconcile_flows(recs)
    actual, pnl, net_flow = _twr(equity, external_flows(recs, _entries=entries))
    actual_v, _, _ = _twr(equity, external_flows(recs, verified_only=True, _entries=entries))
    actual_w, _, _ = _twr(equity, external_flows(recs, _entries=entries, conservative=True,
                                                 _undeclared=undeclared))
    actual_cons = min(actual, actual_v, actual_w)

    base = [r.get("baseline_cum_return_pct") for r in equity if r.get("baseline_cum_return_pct") is not None]
    baseline_ret = base[-1] if base else None
    spread = (None if (actual is None or baseline_ret is None) else round(actual - baseline_ret, 2))
    p0, p1 = _run_px(equity[0]), _run_px(equity[-1])
    qqq_pr = (p1 / p0 - 1) * 100 if (p0 and p1) else None
    gap = (None if (qqq_pr is None or qqq_ret is None) else round(qqq_ret - qqq_pr, 2))
    base_tr = baseline_ret
    if baseline_ret is not None and qqq_pr is not None and qqq_ret is not None:
        ratio = (1 + qqq_ret / 100) / (1 + qqq_pr / 100)
        base_tr = max(baseline_ret, ((1 + baseline_ret / 100) * ratio - 1) * 100)
    spread_cons = (None if base_tr is None else actual_cons - base_tr)

    # the kill trigger may only fire on MATURE evidence: KILL_EVAL_TRADING_DAYS of
    # forward, post-cutoff data. Without this it would arm on the second run.
    if s is None:
        tdays, mature = None, None
    else:
        tdays = None if len(equity) < 2 else _trading_days_between(s, equity[0]["date"], equity[-1]["date"])
        mature = bool(tdays is not None and tdays >= C.KILL_EVAL_TRADING_DAYS)

    ovr_all = merged_overrides(recs)
    graded = [r for r in ovr_all if r.get("spread_vs_baseline_pp") is not None]
    ovr_hits = sum(1 for r in graded if float(r["spread_vs_baseline_pp"]) > 0)
    ovr_miss = sum(1 for r in graded if float(r["spread_vs_baseline_pp"]) < 0)
    decided = ovr_hits + ovr_miss            # a 0.0 (no effect) is neither hit nor miss
    ovr_hitrate = round(ovr_hits / decided, 3) if decided else None

    if mature is None or not price_ok:
        # D2: ANY price-data failure (series missing OR a valuation lookup failing)
        # makes both triggers "unknown", never a boolean
        kill, board = "unknown", "unknown"
    else:
        kill = bool(spread_cons is not None and mature
                    and spread_cons < C.KILL_SATELLITE_IF_SPREAD_BELOW_PP)
        board = bool(qqq_ret is not None and mature
                     and (actual_cons - qqq_ret) < -C.BOARD_RECONVENE_IF_CORE_TRAILS_QQQ_PP)
    log_time_unexplained = [m["date"] for m in equity if m.get("unexplained_flow_usd") is not None]
    return {
        "since": equity[0]["date"],
        "ledger_tamper_detected": not lst["intact"],
        "ledger_tamper_since": lst["tamper_since"],
        "ledger_tamper_sticky_until": lst["sticky_until"],
        "ledger_tamper_reason": lst["reason"],
        "ledger_torn_tail": lst.get("torn_tail"),
        "ledger_pending_commit": lst.get("pending_commit"),
        "ledger_repairs": repairs(recs),
        "price_data_ok": price_ok,
        "method": "TWR",
        "actual_cum_pct": None if actual is None else round(actual, 2),
        "actual_cum_pct_verified_flows": round(actual_v, 2),
        "actual_cum_pct_conservative": round(actual_cons, 2),
        "qqq_cum_pct": None if qqq_ret is None else round(qqq_ret, 2),
        "qqq_cum_pct_price_basis": None if qqq_pr is None else round(qqq_pr, 2),
        "dividend_basis_gap_pp": gap,
        "vs_qqq_pp": None if (actual is None or qqq_ret is None) else round(actual - qqq_ret, 2),
        "mechanical_baseline_cum_pct": baseline_ret,
        "mechanical_baseline_cum_pct_tr_bound": None if base_tr is None else round(base_tr, 2),
        "llm_layer_spread_pp": spread,
        "llm_layer_spread_pp_conservative": None if spread_cons is None else round(spread_cons, 2),
        "net_external_flow_usd": round(net_flow, 2),
        "flow_adjustments": sum(1 for r in recs if r.get("type") == "cash_flow_adjustment"),
        "flows": entries,
        "unverified_flows": sum(1 for e in entries if e.get("verified") is False),
        "pnl_usd": round(pnl, 2),
        "superseded_runs": superseded_run_count(recs),
        "superseded_lines": superseded_lines(recs),
        "unexplained_flow_marks": sorted(set(log_time_unexplained) | set(undeclared)),
        "last_residual": equity[-1].get("execution_residual"),
        "override_hit_rate": ovr_hitrate,
        "overrides_graded": decided,
        "overrides_ungraded": sum(1 for r in ovr_all if r.get("graded") is False),
        "overrides_pending": sum(1 for r in ovr_all if r.get("graded") is None),
        "overrides_rejected": sum(1 for r in recs if r.get("type") == "override_rejected"),
        "override_privileges_at_risk": (ovr_hitrate is not None
                                        and decided >= C.OVERRIDE_HITRATE_MIN_DECIDED
                                        and ovr_hitrate < C.KILL_MIN_OVERRIDE_HITRATE),
        "eval_trading_days": tdays,
        "eval_mature": mature,
        "kill_trigger_armed": kill,
        "board_reconvene_armed": board,
    }


def _csv_stale(out_csv, recs_path):
    if not os.path.exists(out_csv):
        return True
    return os.path.exists(recs_path) and os.path.getmtime(out_csv) < os.path.getmtime(recs_path)


def _csv_rows_trusted(out_csv):
    """Rows of a cached scorecard, or None when it must not be trusted: unreadable,
    or every row lacks a benchmark return (the signature of a price outage)."""
    try:
        with open(out_csv) as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return None
    if rows and all(r.get("qqq_1d") in (None, "", "None") for r in rows):
        return None
    return rows


def track_record(recs_path, out_csv, csv_path=None):
    rows = None if _csv_stale(out_csv, recs_path) else _csv_rows_trusted(out_csv)
    if rows is None:
        rows = score(recs_path, out_csv, csv_path)
    scored_ok = rows is not None
    rows = rows or []
    # the hit rate grades SATELLITE decisions only. A core BUY graded against the
    # core itself has excess 0 by construction and would read as a miss forever.
    sat = [r for r in rows if r.get("type") == "order"
           and str(r.get("ticker") or "").upper() not in C.CORE_TICKERS]
    sat = sat[-C.TRACK_RECORD_WINDOW:]
    rows = rows[-C.TRACK_RECORD_WINDOW:]
    excess = []
    for r in sat:
        if r.get("excess_20d") in (None, "", "None"):
            continue
        x = float(r["excess_20d"])
        excess.append(-x if r.get("action") == "SELL" else x)   # a SELL is right if it lags
    hits = sum(1 for x in excess if x > 0)
    cum = cumulative(recs_path, csv_path) or {}
    mean_excess = (sum(excess) / len(excess)) if excess else None
    hr = (hits / len(excess)) if excess else None
    return {
        "last_n": len(rows),
        "graded": len(excess),
        "price_data_ok": bool(scored_ok and cum.get("price_data_ok", True)),
        "hit_rate_vs_qqq": round(hr, 3) if hr is not None else None,
        "mean_excess_20d_pp": None if mean_excess is None else round(mean_excess, 2),
        "dartboard_base_rate": C.DARTBOARD_BASE_RATE,   # 39.8%, NOT 50%
        "beats_dartboard": (hr > C.DARTBOARD_BASE_RATE) if excess else None,
        # expansion is evidence-gated in exactly one place, here:
        "expand_satellite_ok": (cum.get("eval_mature") is True and hr is not None
                                and hr > C.EXPAND_SATELLITE_IF_HITRATE_ABOVE
                                and mean_excess is not None and mean_excess > 0),
        "cumulative": cum,
        "shadow": shadow_stats(recs_path, csv_path),
        "recent": rows[-10:],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recs", default=C.RECS_JSONL)
    ap.add_argument("--out", default=C.SCORECARD_CSV)
    ap.add_argument("--prices-csv")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--track-record", action="store_true")
    ap.add_argument("--header", action="store_true")
    ap.add_argument("--shadow", action="store_true")
    ap.add_argument("--cron-log-max-kib", action="store_true",
                    help="print config.CRON_LOG_MAX_KIB (install_cron.sh reads the log cap here)")
    a = ap.parse_args()
    if a.cron_log_max_kib:
        print(int(C.CRON_LOG_MAX_KIB))
        return
    rc = 0
    if a.score and score(a.recs, a.out, a.prices_csv) is None:
        rc = 3                                   # price outage: visible to cron's log
    if a.score:
        lst = ledger_status(a.recs, _anchor_path(a.recs))
        if lst.get("torn_tail") or lst.get("unreadable"):
            sys.stderr.write(f"ERROR: {lst['reason']}\n")
            rc = 5                               # torn tail / unreadable: visible to cron
    if a.track_record:
        print(json.dumps(track_record(a.recs, a.out, a.prices_csv), indent=2, ensure_ascii=False))
    if a.header:
        print(json.dumps(cumulative(a.recs, a.prices_csv), indent=2, ensure_ascii=False))
    if a.shadow:
        print(json.dumps(shadow_stats(a.recs, a.prices_csv), indent=2, ensure_ascii=False))
    sys.exit(rc)


if __name__ == "__main__":
    main()
