#!/usr/bin/env python3
"""
statusd.py — headless usage updater for the GNOME panel extension.

No GUI. Periodically computes usage + fetches account limits and writes
~/.cache/claude-usage-widget/status.json, which the GNOME Shell extension
reads. Runs independently of the GUI widget, so the panel indicator works
even when the full widget window is closed.

Plays nice with the GUI (which polls the same account API):
  • single instance — an flock on ~/.cache/.../statusd.lock; a second copy
    (e.g. the extension's enable() firing again after unlock) exits quietly;
  • no duplicate API traffic — if limits.json was refreshed recently by the
    GUI (or by us), reuse it instead of hitting the network;
  • 429 backoff — when rate-limited, wait it out instead of retrying hot.
"""

import os
import sys
import time
import fcntl

import usage_core as core

POLL_SECONDS = 150
FRESH_ENOUGH = 145          # reuse limits.json younger than this (no network)
RATELIMIT_BACKOFF = 600     # minimum sit-out after an HTTP 429

LOCK_PATH = os.path.expanduser("~/.cache/claude-usage-widget/statusd.lock")


def acquire_single_instance():
    """Take the statusd lock. Returns the open lock file (keep it referenced
    for the process lifetime) or None if another statusd already holds it."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lk = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lk.close()
        return None
    lk.write(str(os.getpid()))
    lk.flush()
    return lk


def main():
    lk = acquire_single_instance()
    if lk is None:
        print("statusd: another instance is already running", file=sys.stderr)
        return
    data = core.UsageData()
    backoff_until = 0.0
    while True:
        try:
            now = time.time()
            cached = core.read_limits_cache()
            age = (now - cached.get("ts", 0)) if cached else None
            # align the token counter to the REAL account 5h window when known
            hint = None
            if cached and cached.get("data"):
                fh_seg = cached["data"].get("five_hour") or {}
                ts = core._parse_ts(fh_seg.get("resets_at") or "")
                if ts and ts > now:
                    hint = ts - 5 * 3600
            comp = data.compute(session_start=hint)
            if now < backoff_until or (age is not None and age < FRESH_ENOUGH):
                # recent payload exists (ours or the GUI's) or we're backing
                # off a 429 — don't touch the network this round
                limres = {
                    "status": ("ok" if age is not None and age < FRESH_ENOUGH
                               else "rate_limited"),
                    "data": cached.get("data") if cached else None,
                    "source": (cached or {}).get("source", "none"),
                }
            else:
                limres = core.fetch_limits()
                if limres.get("status") == "rate_limited":
                    ra = limres.get("retry_after") or 0
                    backoff_until = now + max(ra, RATELIMIT_BACKOFF)
            core.write_status(comp, limres)
        except Exception as e:  # keep the daemon alive, but say what broke
            print(f"statusd: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
