#!/usr/bin/env python3
"""
Claude Usage — a dark desktop widget for Ubuntu/GNOME.

Shows live Claude Code usage parsed from ~/.claude/projects:
  • a circular gauge for the current 5-hour rate-limit window
  • today / last 7d / last 30d token totals + estimated cost
  • a per-model-family breakdown

Refreshes every few seconds (incremental, cheap after first load).

Run:        python3 widget.py
Snapshot:   python3 widget.py --shot /tmp/shot.png   (render once to PNG, quit)
"""

import os
import re
import sys
import math
import time
import warnings
import threading
from datetime import datetime, timezone

import cairo
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GLib, Gio, Graphene  # noqa: E402

import usage_core as core  # noqa: E402
import auth  # noqa: E402
import x11hints  # noqa: E402  (EWMH hints + position; no-op off X11)

REFRESH_SECONDS = 8           # local log re-scan (cheap, no network)
LIMITS_POLL_SECONDS = 150     # account-usage API poll when healthy (gentle)
LIMITS_RETRY_SECONDS = 30     # retry cadence when expired/offline
LIMITS_RATELIMIT_SECONDS = 300  # back off hard on HTTP 429 (don't hammer)
STALE_CACHE_SECONDS = 1800    # keep showing the last real data this long when
                              # we can't refresh (better than the local guess)
SCALE_MIN, SCALE_MAX, SCALE_STEP = 0.7, 1.6, 0.05
BASE_WIDTH = 340              # window width at scale 1.0

# palette
ACCENT = (0xD9 / 255, 0x77 / 255, 0x57 / 255)  # Claude terracotta
SEV_OK = (0x5F / 255, 0xB9 / 255, 0x8E / 255)   # green
SEV_CRIT = (0xE5 / 255, 0x53 / 255, 0x4B / 255)  # red


def severity(pct):
    """Return (rgb, css_class) for a 0..1+ limit fraction.
    Thresholds are duplicated in the panel extension (extension.js _update) —
    keep the two in sync."""
    if pct < 0.70:
        return SEV_OK, "lim-ok"
    if pct < 0.92:
        return ACCENT, "lim-warn"
    return SEV_CRIT, "lim-crit"


FAMILY_COLORS = {
    "fable":   "#c9a227",
    "opus":    "#d97757",
    "sonnet":  "#6ea8fe",
    "haiku":   "#5fb98e",
    "default": "#9a9a97",
}
FAMILY_LABELS = {
    "fable": "Fable", "opus": "Opus", "sonnet": "Sonnet",
    "haiku": "Haiku", "default": "Other",
}

CSS = """
window.usage-window {
    background-color: transparent;
    background-image: none;
}
window.usage-window > box {
    background-color: transparent;
    background-image: none;
}
.card {
    background: linear-gradient(160deg, #201f1d, #171614);
    border-radius: 20px;
    border: 1px solid rgba(255,255,255,0.07);
    padding: 18px 18px 14px 18px;
    /* margin must exceed the shadow's reach (offset+blur) or it gets clipped
       by the window edge into a hard diagonal */
    margin: 24px;
    box-shadow: 0 5px 18px rgba(0,0,0,0.5);
}
.title {
    font-size: 13px;
    font-weight: 700;
    color: #efeae3;
    letter-spacing: 0.3px;
}
.dot {
    background: #d97757;
    border-radius: 50%;
    min-width: 9px;
    min-height: 9px;
    box-shadow: 0 0 8px rgba(217,119,87,0.8);
}
.menubtn {
    background: transparent;
    border: none;
    box-shadow: none;
    color: #8a8781;
    padding: 0 4px;
    min-height: 20px;
}
.menubtn:hover { color: #efeae3; }
.gauge-big {
    font-size: 30px;
    font-weight: 800;
    color: #f3efe9;
}
.gauge-unit {
    font-size: 10px;
    font-weight: 600;
    color: #8a8781;
    letter-spacing: 1.5px;
}
.reset {
    font-size: 12px;
    color: #c9c4bc;
}
.reset-accent { color: #d97757; font-weight: 700; }
.sep {
    background: rgba(255,255,255,0.07);
    min-height: 1px;
}
.stat-label {
    font-size: 9px;
    font-weight: 700;
    color: #807d77;
    letter-spacing: 1.2px;
}
.stat-value {
    font-size: 16px;
    font-weight: 800;
    color: #f0ebe4;
}
.stat-cost {
    font-size: 10px;
    color: #837f78;
}
.fam-name { font-size: 11px; color: #cbc6bd; font-weight: 600; }
.fam-tok  { font-size: 11px; color: #918d85; }
.footer   { font-size: 9px; color: #6d6a64; letter-spacing: 0.5px; }
levelbar.fam trough {
    min-height: 5px; border-radius: 3px;
    background: rgba(255,255,255,0.06); border: none;
}
levelbar.fam block.filled { border-radius: 3px; border: none; }
levelbar.fam-fable  block.filled { background: #c9a227; }
levelbar.fam-opus   block.filled { background: #d97757; }
levelbar.fam-sonnet block.filled { background: #6ea8fe; }
levelbar.fam-haiku  block.filled { background: #5fb98e; }
levelbar.fam-default block.filled { background: #9a9a97; }
.limit-label { font-size: 9px; font-weight: 700; color: #807d77;
               letter-spacing: 1.2px; }
.limit-val { font-size: 11px; font-weight: 700; color: #d2cdc4; }
.limit-pct { font-size: 11px; font-weight: 800; }
levelbar.lim trough {
    min-height: 7px; border-radius: 4px;
    background: rgba(255,255,255,0.06); border: none;
}
levelbar.lim block.filled { border-radius: 4px; border: none; }
levelbar.lim-ok   block.filled { background: #5fb98e; }
levelbar.lim-warn block.filled { background: #d97757; }
levelbar.lim-crit block.filled { background: #e5534b; }
.authbtn {
    background: rgba(217,119,87,0.16);
    color: #f0c0aa;
    border: 1px solid rgba(217,119,87,0.55);
    border-radius: 10px;
    padding: 7px 10px;
    font-size: 11px;
    font-weight: 700;
}
.authbtn:hover { background: rgba(217,119,87,0.28); color: #ffe4d6; }
.dialog-bg { background: #181715; }
.dialog-title { font-size: 15px; font-weight: 800; color: #f0ebe4; }
.dialog-text { font-size: 12px; color: #b8b3aa; }
.dialog-err { font-size: 11px; color: #e5534b; }
.dialog-ok { font-size: 11px; color: #5fb98e; }
.pill {
    background: #d97757; color: #1a1614; border: none; border-radius: 9px;
    padding: 8px 12px; font-weight: 700;
}
.pill:hover { background: #e3865f; }
.swatch { min-width: 9px; min-height: 9px; border-radius: 50%; }
.swatch-fable   { background: #c9a227; }
.swatch-opus    { background: #d97757; }
.swatch-sonnet  { background: #6ea8fe; }
.swatch-haiku   { background: #5fb98e; }
.swatch-default { background: #9a9a97; }
"""

GAUGE_SIZE = 168


def scaled_css(scale: float) -> str:
    """Scale every px value in the stylesheet by `scale` (sizes, paddings,
    radii, fonts, shadows all scale together)."""
    if abs(scale - 1.0) < 0.001:
        return CSS
    return re.sub(r"(\d+(?:\.\d+)?)px",
                  lambda m: f"{float(m.group(1)) * scale:.4g}px", CSS)


def make_gauge_texture(frac, rgb=ACCENT, size=GAUGE_SIZE, scale=2):
    """Draw the 5-hour limit ring with standalone pycairo -> Gdk.Texture.

    Avoids the gi<->cairo foreign bridge (not installed) by rendering to our
    own ImageSurface instead of a GTK-supplied context. Proportions are kept
    relative to `size`, so the ring stays balanced at any widget scale.
    """
    frac = max(0.0, min(1.0, frac))
    w = size * scale
    k = size / GAUGE_SIZE  # keep ring thickness proportional
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, w)
    cr = cairo.Context(surface)
    cx = cy = w / 2
    lw = 13 * scale * k
    r = w / 2 - lw
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_width(lw)
    cr.set_source_rgba(1, 1, 1, 0.07)
    cr.arc(cx, cy, r, 0, 2 * math.pi)
    cr.stroke()
    if frac > 0:
        start = -math.pi / 2
        end = start + 2 * math.pi * frac
        cr.set_source_rgba(*rgb, 1.0)
        cr.arc(cx, cy, r, start, end)
        cr.stroke()
        cr.arc(cx + r * math.cos(end), cy + r * math.sin(end),
               lw / 2 + 1.5 * scale * k, 0, 2 * math.pi)
        cr.fill()
    surface.flush()
    data = bytes(surface.get_data())
    return Gdk.MemoryTexture.new(
        w, w, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
        GLib.Bytes.new(data), surface.get_stride())


class ClaudeUsageWindow(Gtk.ApplicationWindow):
    def __init__(self, app, shot_path=None):
        super().__init__(application=app)
        self._shot_path = shot_path
        self.set_title("Claude Usage")
        self.set_decorated(False)
        self.set_resizable(False)
        self.add_css_class("usage-window")

        # user settings: scale (0.7–1.6) and remembered position
        cfg = core._read_config()
        try:
            self._scale = min(SCALE_MAX, max(SCALE_MIN,
                                             float(cfg.get("scale") or 1.0)))
        except (TypeError, ValueError):
            self._scale = 1.0
        pos = cfg.get("pos")
        self._saved_pos = (list(pos) if isinstance(pos, list)
                           and len(pos) == 2 else None)
        self._css_provider = None
        self._apply_css()
        self._gauge_size = int(GAUGE_SIZE * self._scale)
        self._gauge_state = (0.0, ACCENT)   # last drawn (frac, rgb)
        self.set_default_size(int(BASE_WIDTH * self._scale), 0)

        self._data = core.UsageData()
        self._lim_data = None      # last successful /api/oauth/usage payload
        self._lim_data_ts = 0.0    # when that payload was fetched
        self._lim_status = "no_auth"
        self._lim_source = "none"
        self._lim_retry_after = None  # Retry-After (s) from a 429, if any
        self._limits_ts = 0.0      # last network attempt
        self._refreshing = False
        # seed from the on-disk cache so a fresh process shows the real numbers
        # (incl. Sonnet) right away instead of the local estimate.
        cached = core.read_limits_cache()
        if cached:
            self._lim_data = cached.get("data")
            self._lim_data_ts = cached.get("ts", 0.0)
            self._lim_source = cached.get("source", "none")
            # cache fresh enough? behave as if we just polled — restarts then
            # cost zero API calls (rapid dev restarts used to trip HTTP 429)
            if time.time() - self._lim_data_ts < LIMITS_POLL_SECONDS:
                self._lim_status = "ok"
                self._limits_ts = self._lim_data_ts
        self._login_ctx = None     # in-flight PKCE login {url,verifier,state}
        self._login_win = None     # open login dialog, if any (single window)
        self._build_ui()
        self._install_actions(app)

        # keyboard: Esc hides the widget (process keeps running)
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

        # Ctrl+scroll anywhere on the card resizes the widget
        self._scroll_acc = 0.0  # accumulates fractional touchpad deltas
        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        # drag anywhere on the card to move the widget (manual on X11 so it
        # works even in the below layer; compositor-driven on Wayland)
        self._drag_ctx = None
        drag = Gtk.GestureDrag.new()
        drag.set_button(1)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

        # desktop-widget mode on X11/XWayland (no-op on pure Wayland):
        # re-applied on every map since the WM may drop states on unmap
        self.connect("map", self._on_map)

        if shot_path:
            # synchronous load so the snapshot has real numbers
            d = self._data.compute(
                session_start=self._session_window_start())
            res = core.fetch_limits()
            self._lim_status = res["status"]
            self._lim_source = res.get("source", self._lim_source)
            if res["data"] is not None:
                self._lim_data = res["data"]
                self._lim_data_ts = time.time()
            d["limits"] = {"status": self._lim_status, "data": self._lim_data,
                           "data_ts": self._lim_data_ts,
                           "source": self._lim_source}
            fake = os.environ.get("CLAUDE_WIDGET_FAKE_STATUS")
            if fake:  # debug: force a status to preview the indicator/button
                d["limits"] = {"status": fake, "data": None, "data_ts": 0}
            self._apply(d)
            GLib.timeout_add(700, self._do_shot)
        else:
            self._refresh_async()
            GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)

    # ---------- UI ----------
    def _build_ui(self):
        # plain container — dragging is implemented manually (see _drag_*):
        # Gtk.WindowHandle delegates the move to the WM, which is unreliable
        # for a below-layer window; manual XMoveWindow always works
        root = Gtk.Box()
        self.set_child(root)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("card")
        root.append(card)

        # header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dot = Gtk.Box()
        dot.add_css_class("dot")
        dot.set_valign(Gtk.Align.CENTER)
        header.append(dot)
        title = Gtk.Label(label="Claude Usage")
        title.add_css_class("title")
        header.append(title)
        spacer = Gtk.Box(hexpand=True)
        header.append(spacer)
        # hide the widget completely (keeps running; reopen from the top bar)
        self.hide_btn = Gtk.Button()
        self.hide_btn.set_icon_name("window-minimize-symbolic")
        self.hide_btn.set_tooltip_text("Hide — reopen from the top-bar menu")
        self.hide_btn.add_css_class("menubtn")
        self.hide_btn.set_valign(Gtk.Align.CENTER)
        self.hide_btn.connect("clicked", lambda *_: self._hide_widget())
        header.append(self.hide_btn)
        menu = Gio.Menu()
        menu.append("Refresh now", "app.refresh")
        menu.append("Bigger  (Ctrl+scroll)", "app.zoom-in")
        menu.append("Smaller", "app.zoom-out")
        menu.append("Reset size", "app.zoom-reset")
        menu.append("Authorize / Log in…", "app.login")
        menu.append("Log out", "app.logout")
        menu.append("Hide widget", "app.hide")
        menu.append("Quit", "app.quit")
        mbtn = Gtk.MenuButton()
        mbtn.set_icon_name("view-more-symbolic")
        mbtn.set_menu_model(menu)
        mbtn.add_css_class("menubtn")
        mbtn.set_valign(Gtk.Align.CENTER)
        header.append(mbtn)
        card.append(header)

        # auth banner (shown only when the token is expired / not connected)
        self.auth_btn = Gtk.Button(label="🔐  Connect your Claude account")
        self.auth_btn.add_css_class("authbtn")
        self.auth_btn.connect("clicked", lambda *_: self._open_login())
        self.auth_btn.set_visible(False)
        card.append(self.auth_btn)

        # gauge + centered text
        overlay = Gtk.Overlay()
        self.gauge = Gtk.Picture()
        self.gauge.set_size_request(self._gauge_size, self._gauge_size)
        self.gauge.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.gauge.set_can_shrink(True)
        self._set_gauge(0.0)
        overlay.set_child(self.gauge)
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        center.set_halign(Gtk.Align.CENTER)
        center.set_valign(Gtk.Align.CENTER)
        self.big = Gtk.Label(label="—")
        self.big.add_css_class("gauge-big")
        center.append(self.big)
        self.unit = Gtk.Label(label="5H LIMIT")
        self.unit.add_css_class("gauge-unit")
        center.append(self.unit)
        overlay.add_overlay(center)
        overlay.set_tooltip_text(
            "Ring + % — official 5-hour session usage from your account "
            "(same as Claude Code's /usage).\n"
            "Big number — tokens (input + output, cache excluded) used in "
            "that same 5-hour window, from local logs.")
        card.append(overlay)

        # reset / burn line
        self.reset = Gtk.Label(label="")
        self.reset.add_css_class("reset")
        self.reset.set_use_markup(True)
        self.reset.set_tooltip_text(
            "resets in …  — time left until the current 5-hour limit window "
            "rolls over and your session usage goes back to 0.\n"
            "…K/min  — current burn rate: tokens consumed per minute in this "
            "5-hour session.")
        card.append(self.reset)

        # weekly limit bar
        week = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        wtop = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        wlbl = Gtk.Label(label="WEEKLY")
        wlbl.add_css_class("limit-label")
        wlbl.set_valign(Gtk.Align.CENTER)
        wtop.append(wlbl)
        wtop.append(Gtk.Box(hexpand=True))
        self.week_val = Gtk.Label(label="")
        self.week_val.add_css_class("limit-val")
        wtop.append(self.week_val)
        self.week_pct = Gtk.Label(label="")
        self.week_pct.add_css_class("limit-pct")
        self.week_pct.set_use_markup(True)
        wtop.append(self.week_pct)
        week.append(wtop)
        self.week_bar = Gtk.LevelBar()
        self.week_bar.add_css_class("lim")
        self.week_bar.set_min_value(0)
        self.week_bar.set_max_value(1)
        week.append(self.week_bar)
        card.append(week)

        sep1 = Gtk.Box()
        sep1.add_css_class("sep")
        card.append(sep1)

        # stat columns
        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                        homogeneous=True)
        self.stat_widgets = {}
        for keyname, lbl in (("today", "TODAY"), ("d7", "7 DAYS"),
                             ("d30", "30 DAYS")):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            col.set_halign(Gtk.Align.CENTER)
            l = Gtk.Label(label=lbl)
            l.add_css_class("stat-label")
            v = Gtk.Label(label="—")
            v.add_css_class("stat-value")
            c = Gtk.Label(label="")
            c.add_css_class("stat-cost")
            col.append(l)
            col.append(v)
            col.append(c)
            stats.append(col)
            self.stat_widgets[keyname] = (v, c)
        card.append(stats)

        sep2 = Gtk.Box()
        sep2.add_css_class("sep")
        card.append(sep2)

        # per-model breakdown (rebuilt each refresh)
        self.models_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                  spacing=7)
        self.models_box.set_tooltip_text(
            "Per-model tokens over the last 30 days; each bar is that "
            "family's share of the total.\n"
            "\"wk N%\" — that family's official weekly-limit usage from "
            "your account.")
        card.append(self.models_box)

        self.footer = Gtk.Label(label="")
        self.footer.add_css_class("footer")
        self.footer.set_halign(Gtk.Align.END)
        card.append(self.footer)

    def _install_actions(self, app):
        a = Gio.SimpleAction.new("refresh", None)
        a.connect("activate", lambda *_: self._refresh_async())
        app.add_action(a)
        q = Gio.SimpleAction.new("quit", None)
        q.connect("activate", lambda *_: app.quit())
        app.add_action(q)
        lg = Gio.SimpleAction.new("login", None)
        lg.connect("activate", lambda *_: self._open_login())
        app.add_action(lg)
        lo = Gio.SimpleAction.new("logout", None)
        lo.connect("activate", lambda *_: self._do_logout())
        app.add_action(lo)
        hd = Gio.SimpleAction.new("hide", None)
        hd.connect("activate", lambda *_: self._hide_widget())
        app.add_action(hd)
        for name, kwargs, accels in (
                ("zoom-in", {"delta": SCALE_STEP}, ["<Control>plus",
                                                    "<Control>equal"]),
                ("zoom-out", {"delta": -SCALE_STEP}, ["<Control>minus"]),
                ("zoom-reset", {"reset": True}, ["<Control>0"])):
            za = Gio.SimpleAction.new(name, None)
            za.connect("activate",
                       lambda *_a, kw=kwargs: self._change_scale(**kw))
            app.add_action(za)
            app.set_accels_for_action(f"app.{name}", accels)

    def _on_key(self, _c, keyval, _kc, _state):
        if keyval == Gdk.KEY_Escape:
            self._hide_widget()  # Esc hides, doesn't quit
            return True
        return False

    def _set_gauge(self, frac, rgb=ACCENT):
        self._gauge_state = (frac, rgb)
        self.gauge.set_paintable(
            make_gauge_texture(frac, rgb, size=self._gauge_size))

    # ---------- scale (size) ----------
    def _apply_css(self):
        display = Gdk.Display.get_default()
        if self._css_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(
                display, self._css_provider)
        prov = Gtk.CssProvider()
        prov.load_from_data(scaled_css(self._scale).encode())
        Gtk.StyleContext.add_provider_for_display(
            display, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._css_provider = prov

    def _change_scale(self, delta=None, reset=False):
        new = 1.0 if reset else self._scale + delta
        new = round(min(SCALE_MAX, max(SCALE_MIN, new)), 2)
        if abs(new - self._scale) < 0.001:
            return
        self._scale = new
        self._apply_css()
        self._gauge_size = int(GAUGE_SIZE * new)
        self.gauge.set_size_request(self._gauge_size, self._gauge_size)
        self._set_gauge(*self._gauge_state)   # re-render ring at new size
        self.set_default_size(int(BASE_WIDTH * new), -1)
        core.update_config("scale", new)

    def _on_scroll(self, ctl, _dx, dy):
        state = ctl.get_current_event_state()
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return False
        # a wheel notch is dy=±1, but a touchpad streams dozens of fractional
        # deltas per swipe — accumulate to one scale step per notch-worth so
        # both input kinds zoom at the same pace
        self._scroll_acc += dy
        steps = int(self._scroll_acc)
        if steps == 0:
            return True
        self._scroll_acc -= steps
        self._change_scale(-SCALE_STEP * steps)
        return True

    def _week_sub_row(self, name, frac, fam):
        """A labeled weekly sub-limit bar (Opus / Sonnet), styled like a model
        row so it sits with the per-model breakdown. 0% → empty bar."""
        frac = max(0.0, min(1.0, frac))
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        swatch = Gtk.Box()
        swatch.add_css_class("swatch")
        swatch.add_css_class(f"swatch-{fam}" if fam in FAMILY_COLORS
                             else "swatch-default")
        swatch.set_valign(Gtk.Align.CENTER)
        top.append(swatch)
        nm = Gtk.Label(label=name)
        nm.add_css_class("fam-name")
        top.append(nm)
        top.append(Gtk.Box(hexpand=True))
        pc = Gtk.Label(label=f"{frac * 100:.0f}%")
        pc.add_css_class("fam-tok")
        top.append(pc)
        row.append(top)
        bar = Gtk.LevelBar()
        bar.add_css_class("fam")
        bar.add_css_class(f"fam-{fam}")
        bar.set_min_value(0)
        bar.set_max_value(1)
        bar.set_value(frac)
        row.append(bar)
        return row

    def _hide_widget(self):
        # Disappear completely but keep the process alive (so it keeps updating
        # the top-bar indicator and reopens instantly). Hiding — not closing —
        # keeps the window registered with the app, so GTK doesn't quit. Reopen
        # via the panel's "Open full widget" or by relaunching (single instance
        # → App.do_activate raises this same window).
        self._save_pos()  # so it reappears exactly where it was
        self.set_visible(False)
        core.update_status_fields(widget_visible=False)  # panel menu label

    def _xid(self):
        """The X11 window id, or None when not on the X11 backend."""
        try:
            gi.require_version("GdkX11", "4.0")
            from gi.repository import GdkX11
        except (ValueError, ImportError):
            return None
        surface = self.get_surface()
        if not isinstance(surface, GdkX11.X11Surface):
            return None
        with warnings.catch_warnings():
            # GTK 4.18 deprecated the whole X11 backend wholesale; running
            # on X11/XWayland is this app's deliberate desktop-widget mode
            # (see the launcher), so the blanket deprecation is just noise
            warnings.simplefilter("ignore", DeprecationWarning)
            return surface.get_xid()

    def _on_map(self, *_):
        """Make the window behave like a desktop widget when on X11/XWayland:
        no dock/taskbar/Alt-Tab entry, visible on all workspaces, restored to
        its remembered position. All hints go through x11hints' EWMH client
        messages (GTK's GdkX11 skip-taskbar/pager setters are deprecated).
        The launcher prefers the X11 backend for exactly this; on pure
        Wayland none of these hints exist, so it's a no-op."""
        core.update_status_fields(widget_visible=True)  # panel menu label
        xid = self._xid()
        if xid is None:
            return
        try:
            # sticky + skip-taskbar/pager (NOT below — see x11hints)
            x11hints.apply_widget_state(xid)
            if self._saved_pos and not self._shot_path:
                x11hints.move_window(xid, *self._saved_pos)
        except Exception:
            pass  # best-effort: without it the window is just normal

    def _save_pos(self):
        """Remember the current position if it moved (the user drags the card
        by its body; there's no move-end signal, so we poll on the tick)."""
        xid = self._xid()
        if xid is None or not self.get_visible():
            return
        try:
            pos = x11hints.get_position(xid)
        except Exception:
            return
        if pos and list(pos) != self._saved_pos:
            self._saved_pos = list(pos)
            core.update_config("pos", self._saved_pos)

    # ---------- dragging ----------
    def _drag_begin(self, gesture, _x, _y):
        self._drag_ctx = None
        xid = self._xid()
        if xid is not None:
            # manual move: window start + global pointer start; deltas are
            # computed from the ROOT pointer, immune to the window moving
            # under the pointer mid-drag
            wp = x11hints.get_position(xid)
            pp = x11hints.pointer_position()
            if wp and pp:
                self._drag_ctx = (xid, wp, pp)
                return
        # Wayland fallback: hand the whole drag to the compositor
        surface = self.get_surface()
        if surface is None:
            return
        ok, sx, sy = gesture.get_start_point()
        ev = gesture.get_last_event(None)
        surface.begin_move(gesture.get_device(), 1,
                           sx if ok else 0, sy if ok else 0,
                           ev.get_time() if ev else 0)

    def _drag_update(self, *_):
        if not self._drag_ctx:
            return
        xid, (wx, wy), (px, py) = self._drag_ctx
        cur = x11hints.pointer_position()
        if cur:
            x11hints.move_window(xid, wx + cur[0] - px, wy + cur[1] - py)

    def _drag_end(self, *_):
        if self._drag_ctx:
            self._drag_ctx = None
            self._save_pos()

    # ---------- auth ----------
    def _open_uri(self, url):
        try:
            Gtk.UriLauncher.new(url).launch(self, None, None)
        except Exception:
            import webbrowser
            webbrowser.open(url)

    def _do_logout(self):
        auth.logout()
        self._lim_status = "no_auth"
        self._lim_data = None
        self._limits_ts = 0.0  # re-fetch right away (CC fallback may apply)
        self._refresh_async()

    def _open_login(self):
        if self._login_win is not None:
            # one dialog at a time — a second one would invalidate the first's
            # PKCE state and leave a zombie window behind
            self._login_win.present()
            return
        self._login_ctx = auth.begin_login()
        win = Gtk.Window(title="Connect Claude account")
        win.set_transient_for(self)
        win.set_modal(True)
        win.set_resizable(False)
        win.set_default_size(370, -1)
        self._login_win = win
        win.connect("close-request", self._login_closed)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.add_css_class("dialog-bg")
        for m in ("set_margin_top", "set_margin_bottom",
                  "set_margin_start", "set_margin_end"):
            getattr(box, m)(20)
        win.set_child(box)

        title = Gtk.Label(label="Connect your Claude account", xalign=0)
        title.add_css_class("dialog-title")
        box.append(title)
        steps = Gtk.Label(xalign=0, wrap=True)
        steps.add_css_class("dialog-text")
        steps.set_markup(
            "1.  Open the Claude login page.\n"
            "2.  Approve access — Claude shows you a code.\n"
            "3.  Paste that code below and press Connect.")
        box.append(steps)

        open_btn = Gtk.Button(label="Open Claude login  ↗")
        open_btn.add_css_class("pill")
        open_btn.connect("clicked",
                         lambda *_: self._open_uri(self._login_ctx["url"]))
        box.append(open_btn)

        entry = Gtk.Entry()
        entry.set_placeholder_text("Paste the code here")
        box.append(entry)

        msg = Gtk.Label(xalign=0)
        box.append(msg)

        connect = Gtk.Button(label="Connect")
        connect.add_css_class("pill")
        box.append(connect)

        busy = {"v": False}  # guards against double-submit (Enter + click)

        def do_exchange(*_):
            if busy["v"]:
                return
            busy["v"] = True
            code = entry.get_text()
            ctx = self._login_ctx or {}
            msg.remove_css_class("dialog-err")
            msg.add_css_class("dialog-ok")
            msg.set_text("Connecting…")
            connect.set_sensitive(False)
            entry.set_sensitive(False)

            def work():
                ok, m = auth.finish_login(
                    code, ctx.get("verifier", ""), ctx.get("state", ""))
                GLib.idle_add(finish, ok, m)

            def finish(ok, m):
                busy["v"] = False
                connect.set_sensitive(True)
                entry.set_sensitive(True)
                if ok:
                    self._limits_ts = 0.0       # fetch immediately next tick
                    self._refresh_async()
                    win.close()
                else:
                    msg.remove_css_class("dialog-ok")
                    msg.add_css_class("dialog-err")
                    msg.set_text(m)
                return False

            threading.Thread(target=work, daemon=True).start()

        connect.connect("clicked", do_exchange)
        entry.connect("activate", do_exchange)
        win.present()

    def _login_closed(self, *_):
        self._login_win = None
        return False  # let the window actually close

    # ---------- data ----------
    def _tick(self):
        self._save_pos()  # cheap; persists the position after drags
        self._refresh_async()
        return True

    def _refresh_async(self):
        if self._refreshing:
            return
        self._refreshing = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _session_window_start(self):
        """Start of the REAL account 5-hour window (resets_at − 5h), so the
        token counter covers the same window as the official ring %."""
        fh = (self._lim_data or {}).get("five_hour") or {}
        dt = core.parse_iso(fh.get("resets_at"))
        if dt is None:
            return None
        ts = dt.timestamp()
        return ts - 5 * 3600 if ts > time.time() else None

    def _worker(self):
        try:
            d = self._data.compute(session_start=self._session_window_start())
            now = time.time()
            # Poll the account API gently when healthy; retry a bit faster (but
            # not every tick) when expired/offline. On a 429 back off hard so we
            # stop hammering and let the rate limit clear.
            if self._lim_status == "ok":
                interval = LIMITS_POLL_SECONDS
            elif self._lim_status == "rate_limited":
                interval = max(self._lim_retry_after or 0,
                               LIMITS_RATELIMIT_SECONDS)
            else:
                interval = LIMITS_RETRY_SECONDS
            if (now - self._limits_ts) > interval:
                res = core.fetch_limits()
                self._limits_ts = now
                self._lim_status = res["status"]
                self._lim_source = res.get("source", "none")
                self._lim_retry_after = res.get("retry_after")
                if res["data"] is not None:
                    self._lim_data = res["data"]
                    self._lim_data_ts = now
            d["limits"] = {
                "status": self._lim_status,
                "data": self._lim_data,
                "data_ts": self._lim_data_ts,
                "source": self._lim_source,
            }
            core.write_status(d, d["limits"])  # for the GNOME panel extension
        except Exception as e:  # never let the timer die
            d = {"error": str(e)}
        GLib.idle_add(self._apply, d)

    def _apply(self, d):
        self._refreshing = False
        if "error" in d:
            self.footer.set_text(f"error: {d['error'][:40]}")
            return False

        b = d["block"]
        w = d["windows"]
        wk = d["week"]
        res = d.get("limits") or {}
        status = res.get("status", "no_auth")
        lim = res.get("data")
        data_ts = res.get("data_ts", 0)
        fresh = (lim is not None and status == "ok")
        # keep showing the last real data (incl. Sonnet) while we're temporarily
        # offline or rate-limited, rather than dropping to the local estimate
        stale = (lim is not None and status in ("offline", "rate_limited")
                 and (time.time() - data_ts) < STALE_CACHE_SECONDS)
        live = fresh or stale
        if not live:
            lim = None  # fall back to local estimate in the sections below

        need_auth = status in ("expired", "no_auth")
        self.auth_btn.set_visible(need_auth)
        if need_auth:
            self.auth_btn.set_label(
                "🔐  Re-authorize — session expired" if status == "expired"
                else "🔐  Connect your Claude account")

        def remaining(dt):
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())

        # ----- 5-hour session limit (real if available, else local estimate) -----
        fh = (lim or {}).get("five_hour") or {}
        if live and fh:
            spct = (fh.get("utilization") or 0.0) / 100.0
            reset_dt = core.parse_iso(fh.get("resets_at"))
            reset_dur = (core.fmt_duration(remaining(reset_dt))
                         if reset_dt else None)
        else:
            spct = b["limit_pct"]
            reset_dur = core.fmt_duration(b["remaining"]) if b["active"] else None

        srgb, _ = severity(spct)
        self._set_gauge(min(1.0, spct), srgb)
        # token counter aligned to the REAL account window when live (so the
        # number and the ring describe the same 5 hours); local heuristic
        # block only as the offline fallback
        sess = d.get("session")
        if live and sess:
            self.big.set_text(core.fmt_tokens(sess["tok"]))
            burn = sess["burn"]
        else:
            self.big.set_text(core.fmt_tokens(b["tok"]) if b["active"] else "0")
            burn = b["burn"]
        self.unit.set_text(f"{spct * 100:.0f}% · 5H SESSION")

        if reset_dur is not None:
            self.reset.set_markup(
                f"resets in <span foreground='#d97757' weight='bold'>"
                f"{reset_dur}</span>"
                f"   ·   {core.fmt_tokens(burn)}/min")
        else:
            self.reset.set_markup("no active session")

        # ----- weekly limit (real if available, else local estimate) -----
        sd = (lim or {}).get("seven_day") or {}
        if live and sd:
            wpct = (sd.get("utilization") or 0.0) / 100.0
            wreset = core.parse_iso(sd.get("resets_at"))
            wright = ("resets " + wreset.astimezone().strftime("%a %H:%M")
                      if wreset else "all models")
        else:
            wpct = wk["pct"]
            wright = (f"{core.fmt_tokens(wk['used'])} / "
                      f"{core.fmt_tokens(wk['limit'])}")

        wrgb, wcls = severity(wpct)
        whex = "#%02x%02x%02x" % tuple(int(c * 255) for c in wrgb)
        for c in ("lim-ok", "lim-warn", "lim-crit"):
            self.week_bar.remove_css_class(c)
        self.week_bar.add_css_class(wcls)
        self.week_bar.set_value(min(1.0, wpct))
        self.week_val.set_text(wright)
        self.week_pct.set_markup(
            f"<span foreground='{whex}'>{wpct * 100:.0f}%</span>")

        for keyname, (vw, cw) in self.stat_widgets.items():
            x = w[keyname]
            vw.set_text(core.fmt_tokens(x["tok"]))
            cw.set_text(core.fmt_cost(x["cost"]))

        # rebuild model rows
        child = self.models_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.models_box.remove(child)
            child = nxt

        # weekly per-model sub-limits from the API (Opus / Sonnet)
        wk_sub = {}
        if live:
            for key, fam in (("seven_day_opus", "opus"),
                             ("seven_day_sonnet", "sonnet")):
                seg = lim.get(key)
                if seg and seg.get("utilization") is not None:
                    wk_sub[fam] = seg["utilization"]

        per = d["per_model"]
        total = sum(p["tok"] for p in per.values()) or 1
        for fam, pm in sorted(per.items(), key=lambda kv: -kv[1]["tok"]):
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            swatch = Gtk.Box()
            swatch.add_css_class("swatch")
            cls = f"swatch-{fam}" if fam in FAMILY_COLORS else "swatch-default"
            swatch.add_css_class(cls)
            swatch.set_valign(Gtk.Align.CENTER)
            top.append(swatch)
            name = Gtk.Label(label=FAMILY_LABELS.get(fam, fam.title()))
            name.add_css_class("fam-name")
            top.append(name)
            top.append(Gtk.Box(hexpand=True))
            # token total + this family's weekly-limit % when the API has one
            # (the token bar shows share-of-usage; "wk N%" is the real limit)
            txt = core.fmt_tokens(pm["tok"])
            if fam in wk_sub:
                txt += f"   ·   wk {wk_sub[fam]:.0f}%"
            tok = Gtk.Label(label=txt)
            tok.add_css_class("fam-tok")
            top.append(tok)
            row.append(top)

            bar = Gtk.LevelBar()
            bar.add_css_class("fam")
            bar.add_css_class(f"fam-{fam}")
            bar.set_min_value(0)
            bar.set_max_value(1)
            bar.set_value(pm["tok"] / total)
            row.append(bar)
            self.models_box.append(row)

        # families that have a weekly sub-limit but no usage yet get their own
        # bar (0% → empty bar) so the limit is still visible
        for fam, util in wk_sub.items():
            if fam not in per:
                self.models_box.append(
                    self._week_sub_row(FAMILY_LABELS.get(fam, fam.title()),
                                       util / 100.0, fam))

        # footer: clear status indicator (incl. which credential source)
        src = {"widget": "your login",
               "claude-code": "Claude Code"}.get(res.get("source"), "account")
        if fresh:
            foot = f"<span foreground='#5fb98e'>●</span> live · {src}"
        elif stale:
            label = "rate-limited" if status == "rate_limited" else "offline"
            foot = f"<span foreground='#d9a23f'>◑</span> {label} · cached"
        elif status == "rate_limited":
            foot = "<span foreground='#d9a23f'>○</span> rate-limited · estimate"
        elif status == "offline":
            foot = "<span foreground='#d9a23f'>○</span> offline · estimate"
        elif status == "expired":
            foot = "<span foreground='#e5534b'>⚠</span> session expired"
        else:
            foot = "<span foreground='#e5534b'>⚠</span> not connected"
        self.footer.set_markup(f"{foot} · {time.strftime('%H:%M:%S')}")
        return False

    # ---------- snapshot ----------
    def _do_shot(self):
        try:
            widget = self.get_child()
            w = widget.get_width()
            h = widget.get_height()
            if w < 2 or h < 2:
                GLib.timeout_add(300, self._do_shot)
                return False
            paintable = Gtk.WidgetPaintable.new(widget)
            snapshot = Gtk.Snapshot.new()
            paintable.snapshot(snapshot, w, h)
            node = snapshot.to_node()
            renderer = self.get_native().get_renderer()
            texture = renderer.render_texture(
                node, Graphene.Rect().init(0, 0, w, h))
            texture.save_to_png(self._shot_path)
            print(f"saved {self._shot_path} ({w}x{h})")
        except Exception as e:
            print(f"shot failed: {e}")
        self.get_application().quit()
        return False


class App(Gtk.Application):
    def __init__(self, shot_path=None):
        # Unique by default: a second launch (e.g. the panel's "Open full
        # widget", or relaunching from the dock) is routed to this running
        # instance and just raises the existing window instead of spawning
        # another one. --shot is a throwaway render, so it stays non-unique and
        # always draws its own frame.
        default_flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS",
                                Gio.ApplicationFlags.FLAGS_NONE)
        flags = Gio.ApplicationFlags.NON_UNIQUE if shot_path else default_flags
        super().__init__(application_id="io.github.kirilldop.ClaudeUsage",
                         flags=flags)
        self._shot_path = shot_path
        self._win = None

    def do_activate(self):
        # Re-activation (a second launch) lands here on the primary instance.
        # Raise the window we already have rather than creating a new one.
        if self._win is not None:
            self._win.set_visible(True)
            self._win.present()
            return
        s = Gtk.Settings.get_default()
        s.set_property("gtk-application-prefer-dark-theme", True)
        # CSS is applied by the window itself (it owns the scale setting)
        self._win = ClaudeUsageWindow(self, shot_path=self._shot_path)
        self._win.present()


def main():
    shot = None
    if "--shot" in sys.argv:
        i = sys.argv.index("--shot")
        shot = sys.argv[i + 1]
    # Pin the Wayland app_id so GNOME maps our window to the installed
    # .desktop file (StartupWMClass matches this) and shows our icon.
    GLib.set_prgname("io.github.kirilldop.ClaudeUsage")
    App(shot_path=shot).run(None)


if __name__ == "__main__":
    main()
