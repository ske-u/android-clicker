import queue
import sys


_MOD_NAMES = {"ctrl", "shift", "alt", "meta"}


def parse_combo(s):
    """Return (modifier_set: set[str], key_name: str)."""
    parts = s.lower().split("+")
    mods = set()
    key = None
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p in _MOD_NAMES:
            mods.add(p)
            continue
        if key is None:
            key = p
        else:
            raise ValueError(f"multiple non-modifier keys in combo: {s}")
    if key is None:
        raise ValueError(f"no key in combo: {s}")
    return mods, key


class HotkeyListener:
    def __init__(self, config):
        self.queue = queue.Queue(maxsize=32)
        self._impl = None
        if not config:
            return
        plat = sys.platform
        try:
            if plat == "linux":
                from ._linux import LinuxHotkeyImpl
                self._impl = LinuxHotkeyImpl(config, self.queue)
            elif plat == "darwin":
                from ._macos import MacHotkeyImpl
                self._impl = MacHotkeyImpl(config, self.queue)
            elif plat == "win32":
                from ._windows import WindowsHotkeyImpl
                self._impl = WindowsHotkeyImpl(config, self.queue)
        except ImportError:
            pass

    def start(self):
        if self._impl:
            self._impl.start()

    def stop(self):
        if self._impl:
            self._impl.stop()
