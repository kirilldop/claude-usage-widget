#!/usr/bin/env python3
"""Generate the app icon (icon.svg + PNGs) with pycairo.

Design: a deep charcoal rounded tile with a soft warm glow and corner
vignette, carrying the widget's 5-hour gauge as a sweep-gradient terracotta
arc (dark→peach along its length, like progress heating up) with a glowing
endpoint, and an elegant concave four-point spark in the centre.

The arc's sweep gradient is painted as many short overlapping segments with
linearly interpolated colour — cairo has no conic gradients, but at icon
sizes this is indistinguishable from one.

Run:  python3 tools/make_icon.py   (writes into assets/)
"""
import os
import math
import cairo

SIZE = 256  # design space; PNGs are rendered at higher resolution
ASSETS = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "assets"))

# palette
ARC_START = (0xb3 / 255, 0x4f / 255, 0x2e / 255)   # deep terracotta
ARC_MID = (0xd9 / 255, 0x77 / 255, 0x57 / 255)     # brand terracotta
ARC_END = (0xf6 / 255, 0xae / 255, 0x85 / 255)     # light peach
CREAM = (1.0, 0.97, 0.92)


def rrect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def lerp3(c1, c2, t):
    return tuple(a + (b - a) * t for a, b in zip(c1, c2))


def arc_color(t):
    """Colour along the arc: deep -> brand at 55% -> peach at the tip."""
    if t < 0.55:
        return lerp3(ARC_START, ARC_MID, t / 0.55)
    return lerp3(ARC_MID, ARC_END, (t - 0.55) / 0.45)


def spark_path(cr, x, y, r, q):
    """Concave four-point spark (tips up/right/down/left); q controls the
    waist — smaller q = slimmer, more elegant points."""
    cr.move_to(x, y - r)
    cr.curve_to(x + q, y - q, x + q, y - q, x + r, y)
    cr.curve_to(x + q, y + q, x + q, y + q, x, y + r)
    cr.curve_to(x - q, y + q, x - q, y + q, x - r, y)
    cr.curve_to(x - q, y - q, x - q, y - q, x, y - r)
    cr.close_path()


def draw(cr):
    cx, cy, R, LW = 128, 130, 73, 20

    # ---- background tile ----
    rrect(cr, 8, 8, 240, 240, 58)
    g = cairo.LinearGradient(0, 8, 0, 248)
    g.add_color_stop_rgb(0, 0x2c / 255, 0x28 / 255, 0x23 / 255)
    g.add_color_stop_rgb(0.55, 0x1a / 255, 0x18 / 255, 0x15 / 255)
    g.add_color_stop_rgb(1, 0x0f / 255, 0x0e / 255, 0x0c / 255)
    cr.set_source(g)
    cr.fill_preserve()

    # inner atmosphere, clipped to the tile
    cr.save()
    cr.clip()
    # warm radial glow behind the gauge
    wg = cairo.RadialGradient(cx, cy, 10, cx, cy, 150)
    wg.add_color_stop_rgba(0, *ARC_MID, 0.13)
    wg.add_color_stop_rgba(1, *ARC_MID, 0.0)
    cr.set_source(wg)
    cr.rectangle(8, 8, 240, 240)
    cr.fill()
    # top sheen
    sg = cairo.LinearGradient(0, 8, 0, 140)
    sg.add_color_stop_rgba(0, 1, 1, 1, 0.075)
    sg.add_color_stop_rgba(1, 1, 1, 1, 0.0)
    cr.set_source(sg)
    cr.rectangle(8, 8, 240, 132)
    cr.fill()
    # corner vignette
    vg = cairo.RadialGradient(128, 124, 95, 128, 128, 195)
    vg.add_color_stop_rgba(0, 0, 0, 0, 0.0)
    vg.add_color_stop_rgba(1, 0, 0, 0, 0.32)
    cr.set_source(vg)
    cr.rectangle(8, 8, 240, 240)
    cr.fill()
    cr.restore()

    # hairline border
    rrect(cr, 8.7, 8.7, 238.6, 238.6, 57.3)
    cr.set_source_rgba(1, 1, 1, 0.09)
    cr.set_line_width(1.4)
    cr.stroke()

    # ---- gauge track + subtle quarter ticks ----
    cr.set_line_width(LW)
    cr.set_source_rgba(1, 1, 1, 0.055)
    cr.arc(cx, cy, R, 0, 2 * math.pi)
    cr.stroke()
    for i in range(4):
        a = -math.pi / 2 + i * math.pi / 2
        tx, ty = cx + R * math.cos(a), cy + R * math.sin(a)
        cr.set_source_rgba(1, 1, 1, 0.10)
        cr.arc(tx, ty, 2.3, 0, 2 * math.pi)
        cr.fill()

    start = -math.pi / 2
    sweep = 2 * math.pi * 0.78  # ~78% filled, on-brand progress ring

    # ---- bloom under the arc ----
    for extra, alpha in ((24, 0.05), (14, 0.09), (6, 0.15)):
        cr.set_line_width(LW + extra)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(*ARC_MID, alpha)
        cr.arc(cx, cy, R, start, start + sweep)
        cr.stroke()

    # ---- the arc: sweep gradient via overlapping segments ----
    N = 96
    cr.set_line_cap(cairo.LINE_CAP_BUTT)
    cr.set_line_width(LW)
    for i in range(N):
        t0, t1 = i / N, (i + 1) / N
        cr.set_source_rgb(*arc_color((t0 + t1) / 2))
        cr.arc(cx, cy, R,
               start + sweep * t0,
               start + sweep * t1 + (0.015 if i < N - 1 else 0))
        cr.stroke()
    # round caps, drawn by hand so each keeps its own end colour
    for t, col in ((0.0, arc_color(0.0)), (1.0, arc_color(1.0))):
        a = start + sweep * t
        cr.set_source_rgb(*col)
        cr.arc(cx + R * math.cos(a), cy + R * math.sin(a),
               LW / 2, 0, 2 * math.pi)
        cr.fill()

    # ---- glowing endpoint ----
    ea = start + sweep
    ex, ey = cx + R * math.cos(ea), cy + R * math.sin(ea)
    halo = cairo.RadialGradient(ex, ey, 0, ex, ey, 30)
    halo.add_color_stop_rgba(0, 1.0, 0.84, 0.70, 0.55)
    halo.add_color_stop_rgba(1, *ARC_MID, 0.0)
    cr.set_source(halo)
    cr.arc(ex, ey, 30, 0, 2 * math.pi)
    cr.fill()
    dot = cairo.RadialGradient(ex - 3, ey - 3, 1, ex, ey, 12)
    dot.add_color_stop_rgb(0, *CREAM)
    dot.add_color_stop_rgb(1, 0xe8 / 255, 0x8e / 255, 0x62 / 255)
    cr.set_source(dot)
    cr.arc(ex, ey, 11.5, 0, 2 * math.pi)
    cr.fill()

    # ---- centre spark ----
    glow = cairo.RadialGradient(cx, cy, 2, cx, cy, 58)
    glow.add_color_stop_rgba(0, 1.0, 0.84, 0.72, 0.42)
    glow.add_color_stop_rgba(1, 1.0, 0.84, 0.72, 0.0)
    cr.set_source(glow)
    cr.arc(cx, cy, 58, 0, 2 * math.pi)
    cr.fill()

    spark_path(cr, cx, cy, 37, 10.5)
    sp = cairo.RadialGradient(cx, cy - 6, 2, cx, cy + 3, 38)
    sp.add_color_stop_rgb(0, 1.0, 0.99, 0.96)
    sp.add_color_stop_rgb(0.55, 1.0, 0.93, 0.86)
    sp.add_color_stop_rgb(1, 0xf0 / 255, 0xac / 255, 0x84 / 255)
    cr.set_source(sp)
    cr.fill()

    # accent sparks for life (same shape, smaller)
    for ax, ay, ar, alpha in ((cx + 31, cy - 30, 7.5, 0.9),
                              (cx - 33, cy + 27, 5.0, 0.75)):
        spark_path(cr, ax, ay, ar, ar * 0.30)
        cr.set_source_rgba(1.0, 0.93, 0.86, alpha)
        cr.fill()


def render():
    os.makedirs(ASSETS, exist_ok=True)
    svg = cairo.SVGSurface(os.path.join(ASSETS, "icon.svg"), SIZE, SIZE)
    draw(cairo.Context(svg))
    svg.finish()

    for px in (512, 256, 128):
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, px, px)
        cr = cairo.Context(surf)
        cr.scale(px / SIZE, px / SIZE)
        draw(cr)
        surf.write_to_png(os.path.join(ASSETS, f"icon-{px}.png"))

    print(f"wrote icon.svg + icon-{{512,256,128}}.png to {ASSETS}")


if __name__ == "__main__":
    render()
