"""A person at the keyboard and mouse of the display the headed browser tests run on.

The tests drive the agent's browser through the browser's own protocol, which reaches a page
whether or not its tab is in front.  A person's keys go to the tab in front, so what a person
typing would reach is found only by sending keys as that person does: as X events (XTest).

Run only inside test/isolated.sh, whose DISPLAY is an Xvfb of the run's own:

    x-user.py focus <window id>     give the window the keyboard (the display has no window manager)
    x-user.py click <x> <y>         a left click at that place on the screen
    x-user.py type <text>           each character's key, pressed and released
    x-user.py press <key>           one key by its X name, as Return or Escape
"""

import ctypes
import os
import sys
import time

REVERT_TO_PARENT = 2
CURRENT_TIME = 0


def main(argv: list[str]) -> None:
    x11 = ctypes.CDLL("libX11.so.6")
    xtst = ctypes.CDLL("libXtst.so.6")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XKeysymToKeycode.restype = ctypes.c_ubyte
    x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XStringToKeysym.restype = ctypes.c_ulong
    x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]

    display = x11.XOpenDisplay(os.environ.get("DISPLAY", "").encode())
    if not display:
        sys.exit("x-user.py: no display to act on")
    what = argv[0]
    if what == "focus":
        x11.XSetInputFocus(display, int(argv[1], 16), REVERT_TO_PARENT, CURRENT_TIME)
    elif what == "click":
        xtst.XTestFakeMotionEvent(display, -1, int(argv[1]), int(argv[2]), CURRENT_TIME)
        x11.XSync(display, 0)
        time.sleep(0.1)
        xtst.XTestFakeButtonEvent(display, 1, 1, CURRENT_TIME)
        xtst.XTestFakeButtonEvent(display, 1, 0, CURRENT_TIME)
        x11.XSync(display, 0)
        # The page takes the click, and its field the focus, before any key follows.
        time.sleep(0.3)
    elif what == "type":
        for character in argv[1]:
            key = x11.XKeysymToKeycode(display, ord(character))
            xtst.XTestFakeKeyEvent(display, key, 1, CURRENT_TIME)
            xtst.XTestFakeKeyEvent(display, key, 0, CURRENT_TIME)
            x11.XSync(display, 0)
            time.sleep(0.06)
    elif what == "press":
        symbol = x11.XStringToKeysym(argv[1].encode())
        if not symbol:
            sys.exit(f"x-user.py: no such key: {argv[1]}")
        key = x11.XKeysymToKeycode(display, symbol)
        xtst.XTestFakeKeyEvent(display, key, 1, CURRENT_TIME)
        xtst.XTestFakeKeyEvent(display, key, 0, CURRENT_TIME)
    else:
        sys.exit(f"x-user.py: no such act: {what}")
    x11.XSync(display, 0)


if __name__ == "__main__":
    main(sys.argv[1:])
