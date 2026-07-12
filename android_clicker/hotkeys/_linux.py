import os as _os
import select as _select
import sys
import threading

from evdev import InputDevice, list_devices, ecodes
from . import parse_combo

_KEY_CODES = {}
for _code, _name in ecodes.KEY.items():
    if isinstance(_name, str) and _name.startswith("KEY_"):
        _KEY_CODES[_name[4:].lower()] = _code

_MOD_NAMES = {
    "ctrl": [ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL],
    "shift": [ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT],
    "alt": [ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT],
    "meta": [ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA],
}


class LinuxHotkeyImpl:
    def __init__(self, config, queue):
        self.queue = queue
        self._hotkeys = {}
        self._all_mod_codes = {}
        self._all_mod_set = set()
        self._all_key_set = set()
        for action, combo in config.items():
            try:
                mods, key_name = parse_combo(combo)
            except ValueError as e:
                print(f"hotkey: skipping {action}: {e}", file=sys.stderr)
                continue
            keycode = _KEY_CODES.get(key_name)
            if keycode is None:
                print(f"hotkey: unknown key '{key_name}'", file=sys.stderr)
                continue
            self._hotkeys[action] = (mods, keycode)
            self._all_key_set.add(keycode)
            for m in mods:
                codes = set(_MOD_NAMES[m])
                self._all_mod_codes.setdefault(m, set()).update(codes)
                self._all_mod_set.update(codes)

        self._devices = []
        self._running = False
        self._thread = None

    def start(self):
        if not self._hotkeys:
            return
        self._running = True
        for path in list_devices():
            try:
                dev = InputDevice(path)
                if ecodes.EV_KEY in dev.capabilities():
                    _os.set_blocking(dev.fd, False)
                    self._devices.append(dev)
            except (PermissionError, OSError):
                pass
        if not self._devices:
            print("hotkey: no input devices available", file=sys.stderr)
            self._running = False
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        for dev in self._devices:
            try:
                dev.close()
            except Exception:
                pass
        self._devices.clear()

    def _run(self):
        mod_counts = {m: 0 for m in self._all_mod_codes}
        fd_map = {}
        poll = _select.epoll()
        for dev in self._devices:
            fd = dev.fd
            fd_map[fd] = dev
            poll.register(fd, _select.EPOLLIN | _select.EPOLLHUP | _select.EPOLLERR)

        try:
            while self._running and self._devices:
                try:
                    ready = poll.poll(timeout=0.1)
                except InterruptedError:
                    continue
                for fd, event in ready:
                    if event & (_select.EPOLLHUP | _select.EPOLLERR):
                        poll.unregister(fd)
                        dev = fd_map.pop(fd, None)
                        if dev:
                            try:
                                dev.close()
                            except Exception:
                                pass
                        continue
                    dev = fd_map.get(fd)
                    if dev is None:
                        continue
                    try:
                        for ev in dev.read():
                            if ev.type == ecodes.EV_KEY and ev.value != 2:
                                self._on_event(ev, mod_counts)
                    except (BlockingIOError, OSError):
                        pass
        finally:
            poll.close()
            for dev in self._devices:
                try:
                    dev.close()
                except Exception:
                    pass
            self._devices.clear()

    def _on_event(self, ev, mod_counts):
        code = ev.code
        pressed = ev.value == 1

        for m, codes in self._all_mod_codes.items():
            if code in codes:
                if pressed:
                    mod_counts[m] += 1
                else:
                    mod_counts[m] = max(0, mod_counts[m] - 1)
                if code not in self._all_key_set:
                    return
                break

        if not pressed:
            return

        if code not in self._all_key_set:
            return

        for action, (mods, keycode) in self._hotkeys.items():
            if code == keycode and all(mod_counts.get(m, 0) > 0 for m in mods):
                try:
                    self.queue.put_nowait(action)
                except __import__("queue").Full:
                    pass
