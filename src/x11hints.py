"""
x11hints.py — X11/XWayland window helpers for desktop-widget behavior.

GTK4 removed the window-level stick/keep-below/move APIs, and Wayland offers
no client-side equivalents at all. When the widget runs on the X11 backend
(GDK_BACKEND=x11 → XWayland under a GNOME Wayland session), this module talks
to the X server / window manager directly via ctypes + libX11:

  apply_widget_state(xid)  EWMH STICKY + SKIP_TASKBAR + SKIP_PAGER
                           (all workspaces, no dock/taskbar/Alt-Tab entry)
  raise_window(xid)        top of the *below* layer — critical on Ubuntu:
                           the DING desktop-icons extension owns a desktop
                           window in the same layer, and a freshly-lowered
                           window lands UNDER it, where DING swallows every
                           click (the widget looks like a wallpaper picture)
  get_position / move_window / pointer_position
                           manual dragging + remembered position

skip-taskbar / skip-pager go through here too: GTK4's GdkX11 setters for
them are deprecated, and the EWMH client message is the same mechanism.
Everything is best-effort: on failure callers get None/False and the window
just behaves like a normal window. All calls must come from the main thread
(one cached Display connection, no locking).
"""

import ctypes
import ctypes.util

_CLIENT_MESSAGE = 33
_NET_WM_STATE_ADD = 1
_SUBSTRUCTURE_REDIRECT = 1 << 20
_SUBSTRUCTURE_NOTIFY = 1 << 19

_X = None      # cached libX11 handle
_DPY = None    # cached Display*

# Without an error handler one stray BadWindow (e.g. a request against an xid
# the WM just destroyed) is fatal: Xlib error handlers are process-global, so
# errors from OUR display connection land in GDK's handler, which aborts on
# anything outside its own error traps. Swallow errors for our connection
# only; everything else is chained to the previous (GDK's) handler. The
# CFUNCTYPE object must stay referenced or ctypes frees the trampoline.
_ERROR_HANDLER_TYPE = ctypes.CFUNCTYPE(ctypes.c_int,
                                       ctypes.c_void_p, ctypes.c_void_p)
_PREV_HANDLER = None


def _x_error(dpy, ev):
    if _DPY is not None and dpy == _DPY:
        return 0  # our own best-effort connection — ignore
    if _PREV_HANDLER:
        return _PREV_HANDLER(dpy, ev)
    return 0


_error_handler = _ERROR_HANDLER_TYPE(_x_error)


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
        # pad to XEvent union size (192 bytes on LP64) so XSendEvent, which
        # takes an XEvent*, can never read past our allocation
        ("_pad", ctypes.c_char * 96),
    ]


def _lib():
    global _X
    if _X is not None:
        return _X
    path = ctypes.util.find_library("X11")
    if not path:
        return None
    try:
        x = ctypes.CDLL(path)
    except OSError:
        return None
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XInternAtom.restype = ctypes.c_ulong
    x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x.XDefaultRootWindow.restype = ctypes.c_ulong
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XSendEvent.restype = ctypes.c_int
    x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                             ctypes.c_long, ctypes.c_void_p]
    x.XMoveWindow.restype = ctypes.c_int
    x.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                              ctypes.c_int, ctypes.c_int]
    x.XRaiseWindow.restype = ctypes.c_int
    x.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x.XTranslateCoordinates.restype = ctypes.c_int
    x.XTranslateCoordinates.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong)]
    x.XQueryPointer.restype = ctypes.c_int
    x.XQueryPointer.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint)]
    x.XFlush.argtypes = [ctypes.c_void_p]
    x.XSetErrorHandler.restype = _ERROR_HANDLER_TYPE
    x.XSetErrorHandler.argtypes = [_ERROR_HANDLER_TYPE]
    global _PREV_HANDLER
    _PREV_HANDLER = x.XSetErrorHandler(_error_handler)
    _X = x
    return x


def _display():
    """(lib, display) — both cached for the process lifetime, or (None, None)."""
    global _DPY
    x = _lib()
    if x is None:
        return None, None
    if _DPY is None:
        _DPY = x.XOpenDisplay(None)
    return (x, _DPY) if _DPY else (None, None)


def apply_widget_state(xid: int) -> bool:
    """Ask the WM to add STICKY + SKIP_TASKBAR + SKIP_PAGER to window `xid`
    (visible on all workspaces, no dock/taskbar/Alt-Tab entry). One EWMH
    client message carries at most two state atoms, so this sends two.

    Deliberately NOT _NET_WM_STATE_BELOW: Ubuntu's DING desktop-icons window
    is a normal-layer window kept at the bottom of the normal layer, and the
    EWMH below layer sits UNDER the entire normal layer — a below'd widget
    lands beneath DING's full-screen input-catching window and never receives
    another click (confirmed empirically: input dies the moment BELOW is
    applied, regardless of raising within the below layer).
    """
    x, dpy = _display()
    if x is None:
        return False
    state = x.XInternAtom(dpy, b"_NET_WM_STATE", 0)
    atoms = [x.XInternAtom(dpy, name, 0) for name in
             (b"_NET_WM_STATE_STICKY",
              b"_NET_WM_STATE_SKIP_TASKBAR",
              b"_NET_WM_STATE_SKIP_PAGER")]
    root = x.XDefaultRootWindow(dpy)

    ok = True
    for i in range(0, len(atoms), 2):
        ev = _XClientMessageEvent()
        ev.type = _CLIENT_MESSAGE
        ev.window = xid
        ev.message_type = state
        ev.format = 32
        ev.data[0] = _NET_WM_STATE_ADD
        ev.data[1] = atoms[i]
        ev.data[2] = atoms[i + 1] if i + 1 < len(atoms) else 0
        ev.data[3] = 1              # source indication: normal application
        ok = bool(x.XSendEvent(dpy, root, 0,
                               _SUBSTRUCTURE_REDIRECT | _SUBSTRUCTURE_NOTIFY,
                               ctypes.byref(ev))) and ok
    x.XFlush(dpy)
    return ok


def raise_window(xid: int) -> bool:
    """Raise `xid` within its stacking layer (for BELOW windows: to the top
    of the below layer — above the desktop-icons window, under app windows)."""
    x, dpy = _display()
    if x is None:
        return False
    x.XRaiseWindow(dpy, xid)
    x.XFlush(dpy)
    return True


def get_position(xid: int):
    """Absolute root-coordinates of window `xid`'s top-left, or None."""
    x, dpy = _display()
    if x is None:
        return None
    root = x.XDefaultRootWindow(dpy)
    rx, ry = ctypes.c_int(), ctypes.c_int()
    child = ctypes.c_ulong()
    ok = x.XTranslateCoordinates(dpy, xid, root, 0, 0,
                                 ctypes.byref(rx), ctypes.byref(ry),
                                 ctypes.byref(child))
    return (rx.value, ry.value) if ok else None


def move_window(xid: int, px: int, py: int) -> bool:
    """Move window `xid` so its top-left lands at root coords (px, py)."""
    x, dpy = _display()
    if x is None:
        return False
    x.XMoveWindow(dpy, xid, int(px), int(py))
    x.XFlush(dpy)
    return True


def pointer_position():
    """Pointer position in root coordinates, or None."""
    x, dpy = _display()
    if x is None:
        return None
    root = x.XDefaultRootWindow(dpy)
    root_ret, child = ctypes.c_ulong(), ctypes.c_ulong()
    rx, ry = ctypes.c_int(), ctypes.c_int()
    wx, wy = ctypes.c_int(), ctypes.c_int()
    mask = ctypes.c_uint()
    ok = x.XQueryPointer(dpy, root, ctypes.byref(root_ret),
                         ctypes.byref(child), ctypes.byref(rx),
                         ctypes.byref(ry), ctypes.byref(wx),
                         ctypes.byref(wy), ctypes.byref(mask))
    return (rx.value, ry.value) if ok else None
