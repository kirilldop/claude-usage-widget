"""
usage_core.py — parse local Claude Code usage logs and compute metrics.

Data source: ~/.claude/projects/**/*.jsonl
Each assistant message line carries message.usage (input/output/cache tokens)
and message.model + a top-level ISO timestamp. We dedupe by message id +
requestId (same approach as ccusage) so retried/streamed duplicates don't
double-count, then aggregate into: today / last 7d / last 30d / all-time,
the current rolling 5-hour "block" (matching Claude's rate-limit windows),
and a per-model-family breakdown.

Token counts ("tok") are "real" tokens = input + output only; cache read/
creation tokens are EXCLUDED (they're cheap re-reads that inflate totals into
the billions and dominate the count). Cost, however, is still computed from the
full usage incl. cache pricing.

Cost is an ESTIMATE using public per-MTok pricing (see PRICING). For a
Pro/Max subscription it has no billing meaning — it's a "tokens worth ~$X"
indicator only.
"""

import os
import glob
import json
import time
import fcntl
import urllib.request
import urllib.error
from collections import defaultdict
from statistics import quantiles, StatisticsError
from datetime import datetime

import auth  # noqa: E402  (sibling module)

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
CONFIG_PATH = os.path.expanduser("~/.config/claude-usage-widget.json")
STATUS_PATH = os.path.expanduser("~/.cache/claude-usage-widget/status.json")
# last successful /api/oauth/usage payload, so a fresh process (or one that's
# temporarily rate-limited/offline) can show the real numbers instead of the
# rough local estimate.
LIMITS_CACHE_PATH = os.path.expanduser(
    "~/.cache/claude-usage-widget/limits.json")
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"


def fetch_limits(timeout=8) -> dict:
    """Fetch the REAL account limits from Anthropic's /api/oauth/usage — the
    same source Claude Code's /usage panel uses.

    Returns {"status": ..., "data": ..., "source": ...} where status is
    "ok" / "expired" / "offline" / "no_auth" (see auth.get_token).
    """
    token, status = auth.get_token()
    src = auth.source()
    if token is None:
        return {"status": status, "data": None, "source": src}
    req = urllib.request.Request(USAGE_ENDPOINT, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "User-Agent": "claude-usage-widget",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        write_limits_cache(data, src)
        return {"status": "ok", "data": data, "source": src}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):       # token dead -> prompt re-auth
            # it may be revoked while locally unexpired — make the next
            # get_token() try refresh / the CC fallback instead of serving
            # the same dead token until its local expiry
            auth.invalidate(token)
            return {"status": "expired", "data": None, "source": src}
        if e.code == 429:              # rate limited -> back off, keep cache
            ra = e.headers.get("Retry-After")
            try:
                ra = int(ra)
            except (TypeError, ValueError):
                ra = None
            return {"status": "rate_limited", "data": None,
                    "source": src, "retry_after": ra}
        return {"status": "offline", "data": None, "source": src}
    except (urllib.error.URLError, OSError, ValueError):
        return {"status": "offline", "data": None, "source": src}


def _atomic_write_json(path: str, obj, indent=None) -> None:
    """Atomic JSON write. The tmp name is per-process: the GUI and statusd
    write the same files, and a shared fixed tmp name would let one process
    truncate the other's half-written file before its os.replace."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=indent)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_limits_cache(data: dict, source: str) -> None:
    """Persist the last good /api/oauth/usage payload (with a timestamp)."""
    try:
        _atomic_write_json(LIMITS_CACHE_PATH,
                           {"ts": time.time(), "source": source, "data": data})
    except OSError:
        pass


def read_limits_cache() -> dict | None:
    """Return {"ts","source","data"} from the last good fetch, or None."""
    try:
        with open(LIMITS_CACHE_PATH, "r", encoding="utf-8") as fh:
            c = json.load(fh)
        return c if c.get("data") else None
    except (OSError, json.JSONDecodeError):
        return None


def _merge_status(new_fields: dict) -> None:
    """Update status.json, preserving keys other writers maintain (e.g. the
    GUI's widget_visible flag survives statusd's periodic rewrite). The GUI
    and statusd both call this, so the read-modify-write is serialized with
    an flock — otherwise one writer could resurrect fields the other just
    changed (e.g. widget_visible flipping back after a hide)."""
    try:
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        with open(STATUS_PATH + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            existing = {}
            try:
                with open(STATUS_PATH, "r", encoding="utf-8") as fh:
                    existing = json.load(fh) or {}
            except (OSError, json.JSONDecodeError):
                pass
            existing.update(new_fields)
            _atomic_write_json(STATUS_PATH, existing)
    except OSError:
        pass


def update_status_fields(**fields) -> None:
    """Set individual status.json fields (e.g. widget_visible) immediately."""
    _merge_status(fields)


def write_status(comp: dict, limres: dict) -> None:
    """Write a small status file (~/.cache/...) for the GNOME panel extension.
    comp is a compute() result; limres is a fetch_limits() result."""
    status = (limres or {}).get("status", "no_auth")
    source = (limres or {}).get("source", "none")
    data = (limres or {}).get("data")
    if data is None and status in ("offline", "rate_limited"):
        # temporarily unreachable: fall back to the last good payload so
        # the panel keeps showing real numbers (same as the GUI does)
        cached = read_limits_cache()
        if cached and time.time() - cached.get("ts", 0) < 1800:
            data = cached.get("data")
            source = cached.get("source", source)
    data = data or {}
    fh = data.get("five_hour") or {}
    sd = data.get("seven_day") or {}
    # prefer the account-aligned session counter over the heuristic block
    blk = comp.get("session") or comp.get("block", {})
    today = ((comp.get("windows") or {}).get("today") or {}).get("tok", 0)
    # the panel renders rate_limited and offline identically (amber);
    # write "offline" so even an older loaded copy of the extension
    # (Wayland reloads extensions only on re-login) shows amber + %,
    # not the red "!"
    _merge_status({
        "ts": time.time(),
        "status": "offline" if status == "rate_limited" else status,
        "source": source,
        "five_hour_pct": fh.get("utilization"),
        "seven_day_pct": sd.get("utilization"),
        "five_hour_reset": fh.get("resets_at"),
        "seven_day_reset": sd.get("resets_at"),
        "block_tokens": fmt_tokens(blk.get("tok", 0)),
        "today_tokens": fmt_tokens(today),
        "burn": fmt_tokens(blk.get("burn", 0)) + "/min",
    })

# Public list prices per 1M tokens (input, output). Cache is derived from input:
#   write 5m = 1.25x input, write 1h = 2.0x input, read = 0.1x input.
PRICING = {
    "fable":   {"in": 10.0, "out": 50.0},
    "opus":    {"in": 5.0,  "out": 25.0},
    "sonnet":  {"in": 3.0,  "out": 15.0},
    "haiku":   {"in": 1.0,  "out": 5.0},
    "default": {"in": 5.0,  "out": 25.0},
}

FIVE_HOURS = 5 * 3600


def model_family(model: str) -> str:
    if not model:
        return "default"
    m = model.lower()
    if "fable" in m or "mythos" in m:
        return "fable"
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "default"


def _record_cost(family, inp, out, cw5, cw1, cr) -> float:
    p = PRICING.get(family, PRICING["default"])
    rin, rout = p["in"], p["out"]
    return (
        inp * rin
        + out * rout
        + cw5 * rin * 1.25
        + cw1 * rin * 2.0
        + cr * rin * 0.1
    ) / 1_000_000.0


def _read_config() -> dict:
    """Optional settings: {"block_limit", "weekly_limit", "scale", "pos"}."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def update_config(key: str, value) -> None:
    """Merge one key into the config file (atomic write, best-effort)."""
    cfg = _read_config()
    cfg[key] = value
    try:
        _atomic_write_json(CONFIG_PATH, cfg, indent=2)
    except OSError:
        pass


def _p90(values, floor: int) -> int:
    """90th percentile (matches claude-monitor's custom-plan auto-detect),
    with a floor so a brand-new/light history doesn't yield a tiny limit."""
    vals = sorted(v for v in values if v > 0)
    if not vals:
        return floor
    if len(vals) == 1:
        return max(int(vals[0]), floor)
    try:
        q = quantiles(vals, n=10)[8]  # P90
    except StatisticsError:
        q = vals[-1]
    return max(int(q), floor)


def parse_iso(s: str) -> datetime | None:
    """ISO-8601 string -> aware datetime, or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _parse_ts(ts: str) -> float | None:
    dt = parse_iso(ts)
    return dt.timestamp() if dt else None


class UsageData:
    """Incrementally parses the JSONL logs, caching per-file results."""

    def __init__(self):
        # path -> (size, mtime, end_offset, [records]); end_offset is the
        # byte position right after the last fully-parsed line, so a grown
        # file only has its appended tail parsed (the active session log
        # changes on every tick and can be tens of MB)
        self._cache: dict[str, tuple[int, float, int, list]] = {}
        # deduped+sorted records, rebuilt only when some file changed —
        # compute() runs every few seconds and the full dedup/sort over the
        # whole history is the expensive part
        self._deduped: list | None = None

    def _parse_file(self, path: str, offset: int = 0) -> tuple[list, int]:
        """Parse JSONL records starting at byte `offset`. Returns
        (records, end_offset) where end_offset stops right after the last
        complete line: a live log may end mid-write, so the partial tail is
        re-read on the next pass. A tail that already parses as full JSON
        (file legitimately ends without a newline) is counted too, but the
        offset still doesn't advance past it — if more bytes arrive it gets
        parsed again, and compute()'s key dedup drops the extra copy."""
        try:
            with open(path, "rb") as fh:
                if offset:
                    fh.seek(offset)
                data = fh.read()
        except OSError:
            return [], offset
        nl = data.rfind(b"\n")
        body, tail = (data[:nl + 1], data[nl + 1:]) if nl >= 0 else (b"", data)
        end = offset + len(body)
        lines = body.split(b"\n") if body else []
        if tail:
            lines.append(tail)
        records = []
        for line in lines:
            if b'"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:  # bad JSON or undecodable bytes
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            usage = msg.get("usage")
            if not usage:
                continue
            model = msg.get("model") or ""
            if model.startswith("<"):  # <synthetic> etc.
                continue
            ts = _parse_ts(d.get("timestamp"))
            if ts is None:
                continue

            inp = usage.get("input_tokens", 0) or 0
            out = usage.get("output_tokens", 0) or 0
            cw = usage.get("cache_creation_input_tokens", 0) or 0
            cr = usage.get("cache_read_input_tokens", 0) or 0
            cc = usage.get("cache_creation") or {}
            cw5 = cc.get("ephemeral_5m_input_tokens")
            cw1 = cc.get("ephemeral_1h_input_tokens")
            if cw5 is None and cw1 is None:
                cw5, cw1 = cw, 0
            else:
                cw5 = cw5 or 0
                cw1 = cw1 or 0

            key = f"{msg.get('id', '')}|{d.get('requestId', '')}"
            if key == "|":
                key = d.get("uuid", "") or f"{path}:{ts}:{out}"

            fam = model_family(model)
            records.append({
                "key": key,
                "ts": ts,
                "family": fam,
                "in": inp, "out": out,
                "cw5": cw5, "cw1": cw1, "cr": cr,
                # derived fields are fixed per record — computing them
                # here (once per file change) keeps compute(), which
                # runs every few seconds, to plain arithmetic
                "tok": inp + out,  # "real" tokens (no cache rd/wr)
                "cost": _record_cost(fam, inp, out, cw5, cw1, cr),
                "week": tuple(
                    datetime.fromtimestamp(ts).isocalendar()[:2]),
            })
        return records, end

    def _all_records(self) -> tuple[list, bool]:
        """Returns (records, changed) — changed is True when any log file was
        (re)parsed or removed since the previous call."""
        files = glob.glob(os.path.join(CLAUDE_PROJECTS, "**", "*.jsonl"),
                          recursive=True)
        live = set(files)
        changed = False
        # drop deleted files from cache
        for gone in [p for p in self._cache if p not in live]:
            del self._cache[gone]
            changed = True

        records = []
        for path in files:
            try:
                st = os.stat(path)
            except OSError:
                continue
            cached = self._cache.get(path)
            if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
                records.extend(cached[3])
                continue
            if cached and st.st_size > cached[0]:
                # grew — these logs are append-only, parse just the new tail
                new, end = self._parse_file(path, offset=cached[2])
                parsed = cached[3] + new
                changed = changed or bool(new)
            else:
                # new, shrunk, or rewritten in place — full parse
                parsed, end = self._parse_file(path)
                changed = True
            self._cache[path] = (st.st_size, st.st_mtime, end, parsed)
            records.extend(parsed)
        return records, changed

    def compute(self, session_start: float | None = None) -> dict:
        """session_start — UTC timestamp of the REAL account 5-hour window
        start (resets_at − 5h from the usage API). When given, the result
        carries a "session" dict with token/cost/burn for exactly that window,
        so the displayed counter matches the official ring %. The heuristic
        local "block" is still computed as the offline fallback."""
        records, changed = self._all_records()

        # global dedup, then sort by time (cached until a log file changes)
        if changed or self._deduped is None:
            seen = set()
            deduped = []
            for r in records:
                if r["key"] in seen:
                    continue
                seen.add(r["key"])
                deduped.append(r)
            deduped.sort(key=lambda r: r["ts"])
            self._deduped = deduped
        deduped = self._deduped

        now = time.time()
        midnight = datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        d7 = now - 7 * 86400
        d30 = now - 30 * 86400

        def blank():
            return {"tok": 0, "in": 0, "out": 0, "cw": 0, "cr": 0, "cost": 0.0}

        windows = {"today": blank(), "d7": blank(), "d30": blank(),
                   "all": blank()}
        per_model = {}
        week_buckets = defaultdict(float)  # (iso_year, iso_week) -> tokens

        for r in deduped:
            tok = r["tok"]
            cost = r["cost"]
            week_buckets[r["week"]] += tok

            def add(w):
                w["tok"] += tok
                w["in"] += r["in"]
                w["out"] += r["out"]
                w["cw"] += r["cw5"] + r["cw1"]
                w["cr"] += r["cr"]
                w["cost"] += cost

            add(windows["all"])
            if r["ts"] >= d30:
                add(windows["d30"])
                pm = per_model.setdefault(
                    r["family"], {"tok": 0, "cost": 0.0})
                pm["tok"] += tok
                pm["cost"] += cost
            if r["ts"] >= d7:
                add(windows["d7"])
            if r["ts"] >= midnight:
                add(windows["today"])

        # ---- 5-hour blocks (rate-limit-style windows) ----
        blocks = []
        cur = None
        for r in deduped:
            tok = r["tok"]
            cost = r["cost"]
            new_block = (
                cur is None
                or r["ts"] >= cur["start"] + FIVE_HOURS
                or r["ts"] - cur["last"] >= FIVE_HOURS
            )
            if new_block:
                start = datetime.fromtimestamp(r["ts"]).replace(
                    minute=0, second=0, microsecond=0).timestamp()
                cur = {"start": start, "last": r["ts"], "tok": 0, "cost": 0.0}
                blocks.append(cur)
            cur["last"] = r["ts"]
            cur["tok"] += tok
            cur["cost"] += cost

        active = None
        if blocks:
            last = blocks[-1]
            if now < last["start"] + FIVE_HOURS:
                active = last

        if active:
            end = active["start"] + FIVE_HOURS
            elapsed = max(1.0, now - active["start"])
            burn = active["tok"] / (elapsed / 60.0)  # tokens / min
            block = {
                "active": True,
                "tok": active["tok"],
                "cost": active["cost"],
                "start": active["start"],
                "end": end,
                "remaining": max(0, end - now),
                "elapsed_frac": min(1.0, elapsed / FIVE_HOURS),
                "burn": burn,
                "projected": burn * (FIVE_HOURS / 60.0),
            }
        else:
            block = {"active": False, "tok": 0, "cost": 0.0,
                     "remaining": 0, "elapsed_frac": 0.0, "burn": 0.0,
                     "projected": 0.0}

        # ---- limits (auto-calibrated to history, P90; config-overridable) ----
        cfg = _read_config()

        completed = [b["tok"] for b in blocks
                     if b is not active and b["tok"] > 0]
        block_limit = int(cfg.get("block_limit") or _p90(completed, 80_000))
        block["limit"] = block_limit
        block["limit_pct"] = (block["tok"] / block_limit) if block_limit else 0.0
        block["limit_auto"] = "block_limit" not in cfg

        # NOTE: this local fallback counts ISO calendar weeks (Mon-based);
        # the real account window is a rolling 7 days from the API. Close
        # enough for an offline estimate, not meant to match it exactly.
        cur_iso = datetime.now().astimezone().isocalendar()
        cur_key = (cur_iso[0], cur_iso[1])
        hist_weeks = [v for k, v in week_buckets.items()
                      if k != cur_key and v > 0]
        week_used = week_buckets.get(cur_key, 0.0)
        week_limit = int(cfg.get("weekly_limit")
                         or _p90(hist_weeks, 2_000_000))
        week = {
            "used": week_used,
            "limit": week_limit,
            "pct": (week_used / week_limit) if week_limit else 0.0,
            "auto": "weekly_limit" not in cfg,
        }

        # ---- account-aligned session window (real resets_at − 5h) ----
        session = None
        if session_start is not None and session_start < now:
            stok = scost = 0.0
            for r in deduped:
                if r["ts"] >= session_start:
                    stok += r["tok"]
                    scost += r["cost"]
            elapsed_min = max(1.0, (now - session_start) / 60.0)
            session = {"tok": stok, "cost": scost,
                       "burn": stok / elapsed_min, "start": session_start}

        return {
            "now": now,
            "windows": windows,
            "per_model": per_model,
            "block": block,
            "session": session,
            "week": week,
            "n_records": len(deduped),
        }


def fmt_tokens(n: float) -> str:
    n = float(n)
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}K"
    return f"{int(n)}"


def fmt_cost(c: float) -> str:
    if c >= 1000:
        return f"${c / 1000:.1f}k"
    if c >= 100:
        return f"${c:.0f}"
    return f"${c:.2f}"


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, m = sec // 3600, (sec % 3600) // 60
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


if __name__ == "__main__":
    import sys
    data = UsageData().compute()
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2, default=str))
    else:
        w = data["windows"]
        b = data["block"]
        print(f"records: {data['n_records']}")
        for name in ("today", "d7", "d30", "all"):
            x = w[name]
            print(f"  {name:6} {fmt_tokens(x['tok']):>9} tok   "
                  f"{fmt_cost(x['cost']):>8}  "
                  f"(in {fmt_tokens(x['in'])} out {fmt_tokens(x['out'])} "
                  f"cache {fmt_tokens(x['cw'] + x['cr'])})")
        if b["active"]:
            print(f"  block: {fmt_tokens(b['tok'])} tok, {fmt_cost(b['cost'])}, "
                  f"resets in {fmt_duration(b['remaining'])}, "
                  f"{fmt_tokens(b['burn'])}/min, "
                  f"proj {fmt_tokens(b['projected'])}")
        else:
            print("  block: (inactive)")
        print(f"  5h limit:  {fmt_tokens(b['tok'])} / {fmt_tokens(b['limit'])}"
              f"  ({b['limit_pct'] * 100:.0f}%)")
        wk = data["week"]
        print(f"  weekly:    {fmt_tokens(wk['used'])} / {fmt_tokens(wk['limit'])}"
              f"  ({wk['pct'] * 100:.0f}%)")
        for fam, pm in sorted(data["per_model"].items(),
                              key=lambda kv: -kv[1]["tok"]):
            print(f"  {fam:8} {fmt_tokens(pm['tok']):>9} tok  "
                  f"{fmt_cost(pm['cost'])}")
