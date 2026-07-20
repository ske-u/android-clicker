import ctypes
import ctypes.util
import os as _os
import select as _select
import struct
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

_INOTIFY_MASK = 0x100
_IN_NONBLOCK = 0o4000
_IN_CLOEXEC = 0o2000000
_INOTIFY_EVENT_SIZE = 16


def _inotify_init():
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        fd = libc.inotify_init1(_IN_NONBLOCK | _IN_CLOEXEC)
        if fd < 0:
            return None
        wd = libc.inotify_add_watch(fd, b"/dev/input", _INOTIFY_MASK)
        if wd < 0:
            _os.close(fd)
            return None
        return fd
    except Exception:
        return None


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
        self._device_paths = set()
        self._inotify_fd = None
        self._running = False
        self._thread = None
        self._stop_r, self._stop_w = _os.pipe()
        _os.set_blocking(self._stop_r, False)
        _os.set_blocking(self._stop_w, False)

    def start(self):
        if not self._hotkeys:
            return
        self._running = True
        for path in list_devices():
            if path in self._device_paths:
                continue
            try:
                dev = InputDevice(path)
                if ecodes.EV_KEY in dev.capabilities():
                    _os.set_blocking(dev.fd, False)
                    self._devices.append(dev)
                    self._device_paths.add(path)
            except (PermissionError, OSError):
                pass
        self._inotify_fd = _inotify_init()
        if not self._devices and self._inotify_fd is None:
            print("hotkey: no input devices available", file=sys.stderr)
            self._running = False
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        try:
            _os.write(self._stop_w, b"\x00")
        except OSError:
            pass

    def _run(self):
        mod_counts = {m: 0 for m in self._all_mod_codes}
        fd_map = {}
        poll = _select.epoll()

        poll.register(self._stop_r, _select.EPOLLIN)
        fd_map[self._stop_r] = None

        for dev in self._devices:
            fd = dev.fd
            fd_map[fd] = dev
            poll.register(fd, _select.EPOLLIN | _select.EPOLLHUP | _select.EPOLLERR)

        if self._inotify_fd is not None:
            fd_map[self._inotify_fd] = None
            poll.register(self._inotify_fd, _select.EPOLLIN)

        try:
            while self._running:
                try:
                    ready = poll.poll(timeout=0.1)
                except InterruptedError:
                    continue

                for fd, event in ready:
                    if fd == self._stop_r:
                        try:
                            _os.read(self._stop_r, 4096)
                        except OSError:
                            pass
                        return

                    if self._inotify_fd is not None and fd == self._inotify_fd:
                        self._handle_inotify(poll, fd_map)
                        continue

                    if event & (_select.EPOLLHUP | _select.EPOLLERR):
                        poll.unregister(fd)
                        dev = fd_map.pop(fd, None)
                        if dev:
                            try:
                                dev.close()
                            except Exception:
                                pass
                            if dev in self._devices:
                                self._devices.remove(dev)
                                try:
                                    self._device_paths.discard(dev.path)
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
            self._device_paths.clear()
            if self._inotify_fd is not None:
                try:
                    _os.close(self._inotify_fd)
                except Exception:
                    pass
                self._inotify_fd = None
            try:
                _os.close(self._stop_r)
            except Exception:
                pass
            try:
                _os.close(self._stop_w)
            except Exception:
                pass

    def _handle_inotify(self, poll, fd_map):
        try:
            data = _os.read(self._inotify_fd, 4096)
        except (BlockingIOError, OSError):
            return

        offset = 0
        while offset < len(data):
            wd, mask, cookie, name_len = struct.unpack_from("iIII", data, offset)
            offset += _INOTIFY_EVENT_SIZE
            name = data[offset : offset + name_len].rstrip(b"\x00").decode("utf-8", errors="replace")
            offset += name_len
            if not name:
                continue

            path = f"/dev/input/{name}"
            if path in self._device_paths:
                continue

            try:
                dev = InputDevice(path)
                if ecodes.EV_KEY not in dev.capabilities():
                    dev.close()
                    return
                _os.set_blocking(dev.fd, False)
                self._devices.append(dev)
                self._device_paths.add(path)
                fd_map[dev.fd] = dev
                poll.register(dev.fd, _select.EPOLLIN | _select.EPOLLHUP | _select.EPOLLERR)
            except (PermissionError, OSError):
                pass

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
