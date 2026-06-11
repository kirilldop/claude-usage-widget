#!/usr/bin/env python3
"""Render the README screenshot from simulated demo data.

Builds a temporary set of fake Claude Code logs (all model families, used to
different degrees) and a fake account-limits payload, patches usage_core to
read those — and to never write the real status/limits caches — then renders
the widget once through its normal --shot path. No network calls.

Run:  python3 tools/demo_shot.py [out.png]     (default: assets/screenshot.png)
"""
import json
import os
import random
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
# must be set before GTK initializes (imported via widget below)
os.environ.setdefault("GDK_BACKEND", "x11,wayland")

import usage_core as core  # noqa: E402

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                      else os.path.join(ROOT, "assets", "screenshot.png"))

NOW = time.time()
RESET_IN = 1 * 3600 + 47 * 60          # 5h window resets in 1h 47m
SESSION_START = NOW - (5 * 3600 - RESET_IN)
SESSION_TOK = 412_000                  # tokens inside that window
rng = random.Random(7)

# family -> (model id, ~30-day total tokens): every family used, all unequal
MODELS = {
    "opus":   ("claude-opus-4-8",            9_600_000),
    "sonnet": ("claude-sonnet-4-6",          6_800_000),
    "fable":  ("claude-fable-5",             3_100_000),
    "haiku":  ("claude-haiku-4-5-20251001",  1_700_000),
}

LIMITS = None  # filled below (needs iso())


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def record(i, model, ts, tok):
    inp = int(tok * 0.18)
    return json.dumps({
        "type": "assistant",
        "timestamp": iso(ts),
        "requestId": f"demo-req-{i}",
        "message": {
            "id": f"demo-msg-{i}",
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": tok - inp,
                "cache_creation_input_tokens": int(tok * 1.4),
                "cache_read_input_tokens": int(tok * 24),
            },
        },
    })


def make_logs(root):
    lines, n = [], 0
    for fam, (model, total) in MODELS.items():
        # spread over 30 days, weighted toward recent days (~1/3 in last 7)
        weights = [1.0 + 2.2 * d / 29 for d in range(30)]  # d=29 newest
        wsum = sum(weights)
        for d in range(30):
            day_tok = total * weights[d] / wsum
            for _ in range(2):
                n += 1
                # 4-12h behind the day anchor keeps the newest day's records
                # out of the current 5h session window (counted separately)
                ts = (NOW - (29 - d) * 86400
                      - rng.uniform(4 * 3600, 12 * 3600))
                lines.append(record(n, model, ts,
                                    max(1000, int(day_tok / 2
                                                  * rng.uniform(0.6, 1.4)))))
    # records inside the current 5h window: gauge counter + burn rate
    for fam, share in (("opus", 0.50), ("sonnet", 0.22),
                       ("fable", 0.18), ("haiku", 0.10)):
        for k in range(3):
            n += 1
            ts = SESSION_START + (k + 0.6) * ((NOW - SESSION_START) / 3.6)
            lines.append(record(n, MODELS[fam][0], ts,
                                int(SESSION_TOK * share / 3)))
    proj = os.path.join(root, "demo-project")
    os.makedirs(proj)
    with open(os.path.join(proj, "session.jsonl"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


LIMITS = {
    "five_hour":        {"utilization": 76.0, "resets_at": iso(NOW + RESET_IN)},
    "seven_day":        {"utilization": 41.0,
                         "resets_at": iso(NOW + 4.6 * 86400)},
    "seven_day_opus":   {"utilization": 57.0,
                         "resets_at": iso(NOW + 4.6 * 86400)},
    "seven_day_sonnet": {"utilization": 23.0,
                         "resets_at": iso(NOW + 4.6 * 86400)},
}

tmp = tempfile.mkdtemp(prefix="cuw-demo-")
make_logs(tmp)

# point the parser at the fake logs; serve the fake limits; and make every
# writer a no-op so the demo never touches the real caches / panel status
core.CLAUDE_PROJECTS = tmp
core.fetch_limits = lambda timeout=8: {"status": "ok", "data": LIMITS,
                                       "source": "widget"}
core.read_limits_cache = lambda: {"ts": NOW, "source": "widget",
                                  "data": LIMITS}
core.write_limits_cache = lambda *a, **k: None
core.write_status = lambda *a, **k: None
core.update_status_fields = lambda **k: None
core._read_config = lambda: {}            # ignore the user's scale/pos
core.update_config = lambda *a, **k: None

import widget  # noqa: E402  (same usage_core module object — patches apply)

try:
    sys.argv = ["widget.py", "--shot", OUT]
    widget.main()
finally:
    shutil.rmtree(tmp, ignore_errors=True)
