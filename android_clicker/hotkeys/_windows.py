import sys

_KEY_TO_PYNPUT = {
    "pgup": "page_up",
    "pgdown": "page_down",
    "escape": "esc",
    "grave": None,
    "help": None,
}


def _to_pynput(combo_str):
    parts = combo_str.lower().split("+")
    result = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p == "meta":
            result.append("<cmd>")
        elif p in ("ctrl", "shift", "alt"):
            result.append(f"<{p}>")
        elif len(p) == 1:
            result.append(p)
        else:
            mapped = _KEY_TO_PYNPUT.get(p, p)
            if mapped is None:
                return None
            result.append(f"<{mapped}>")
    return "+".join(result)


class WindowsHotkeyImpl:
    def __init__(self, config, queue):
        self.queue = queue
        self._hotkeys = None
        self._bindings = []

        for action, combo in config.items():
            pc = _to_pynput(combo)
            if pc is None:
                print(f"windows hotkey: skipping {action}: unsupported key in '{combo}'",
                      file=sys.stderr)
            else:
                self._bindings.append((pc, action))

    def start(self):
        if not self._bindings:
            return
        try:
            from pynput.keyboard import GlobalHotKeys
        except ImportError:
            print("windows hotkey: pynput not available (pip install pynput)",
                  file=sys.stderr)
            return

        hk_map = {}
        for pc, action in self._bindings:
            hk_map[pc] = lambda a=action: self._queue_push(a)

        try:
            self._hotkeys = GlobalHotKeys(hk_map)
            self._hotkeys.start()
        except Exception as e:
            print(f"windows hotkey: failed to start: {e}", file=sys.stderr)
            self._hotkeys = None

    def _queue_push(self, action):
        try:
            self.queue.put_nowait(action)
        except __import__("queue").Full:
            pass

    def stop(self):
        if self._hotkeys:
            try:
                self._hotkeys.stop()
            except Exception:
                pass
            self._hotkeys = None
