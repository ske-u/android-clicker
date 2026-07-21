import json
import os
import queue
import random
import select
import signal
import socket
import subprocess
import sys
import time

from .config import (
    load_config, load_modeconfig, save_modeconfig,
    MODECONFIG_DIR, FIXED_CREATE_TEMPLATE, CUSTOM_CREATE_TEMPLATE, SCRIPT_DIR,
)
from .injectors import (
    create_injector, create_shared_uinput, available_methods, get_adb_wm_size, ensure_adb,
    set_adb_path, detect_adb_path, set_adb_serial,
)
from .hotkeys import HotkeyListener
from .modes import create_modes
from .modes.custom import CustomMode
from .platform import PlatformAdapter
from . import screen_utils


if sys.platform == "win32":
    SOCKET_FAMILY = socket.AF_INET
    SOCKET_PATH = ("127.0.0.1", 31985)
    LAUNCHER_SOCKET = ("127.0.0.1", 31986)
    OVERLAY_SOCKET = ("127.0.0.1", 31987)
else:
    SOCKET_FAMILY = socket.AF_UNIX
    SOCKET_PATH = "/tmp/android-clicker.sock"
    LAUNCHER_SOCKET = "/tmp/android-clicker-launcher.sock"
    OVERLAY_SOCKET = "/tmp/android-clicker-overlay.sock"


class ClickDaemon:
    def __init__(self, config):
        self.config = config
        self.running = True
        self.active = config.get("active", False)
        self.mode = config.get("mode", "follow")

        self.platform = PlatformAdapter.detect(config)
        disp = self.config.get("display", {})
        if "host_width" in disp and "host_height" in disp:
            self.host_w, self.host_h = disp["host_width"], disp["host_height"]
        else:
            res = self.platform.get_display_resolution()
            if res is None:
                print("error: could not determine display resolution "
                      "(set display.host_width/height in config)", file=sys.stderr)
                sys.exit(1)
            self.host_w, self.host_h = res

        uinput_cfg = config.get("uinput", False)
        self._shared_uinput = None
        if uinput_cfg:
            try:
                self._shared_uinput = create_shared_uinput(self.host_w, self.host_h)
            except Exception as e:
                print(f"warning: uinput init failed ({e}); falling back to adb-pipe", file=sys.stderr)

        adb_cfg = config.get("adb", {})
        adb_connect = adb_cfg.get("connect")
        adb_timeout = adb_cfg.get("connect_timeout")
        if adb_connect is None:
            adb_connect = "192.168.240.112:5555" if sys.platform == "linux" else "127.0.0.1:5555"
        if adb_timeout is None:
            adb_timeout = 5
        self.adb_connect = adb_connect
        self.adb_timeout = adb_timeout
        self.screen_cap_timeout = max(1, min(15, adb_cfg.get("screen_cap_timeout", 15)))

        adb_path = adb_cfg.get("exe_path") or detect_adb_path()
        set_adb_path(adb_path)
        screen_utils.set_adb_path(adb_path)
        set_adb_serial(adb_connect)
        screen_utils.set_adb_serial(adb_connect)

        ensure_adb(adb_connect, timeout=adb_timeout)

        self.android_w, self.android_h = self._detect_android_resolution()
        self._update_window()

        mode_cfg = load_modeconfig(self.mode)
        method = mode_cfg.get("method", "adb-pipe")
        result = create_injector(method, self.host_w, self.host_h,
                                 shared_uinput=self._shared_uinput,
                                 adb_connect=adb_connect,
                                 adb_timeout=adb_timeout)
        if result is None:
            print(f"error: failed to create {method} injector", file=sys.stderr)
            sys.exit(1)
        self.injector, self.method_name = result

        self.modes = create_modes(self.injector, self)
        if self.mode in self.modes:
            self.current_mode = self.modes[self.mode]
        else:
            self.current_mode = next(iter(self.modes.values()))
            self.mode = self.current_mode.name

        self.window_bounds = None
        self.last_window_check = 0.0
        self.overlay_state = {"visible": True, "quit": False}
        self._launcher_proc = None
        self._overlay_proc = None
        self._next_click = 0.0
        self._clients: dict[socket.socket, dict] = {}

        hk_cfg = config.get("hotkeys", {})
        self._hotkey_actions = config.get("hotkey_actions", {})
        self._hk_procs = {}
        self._hotkeys = None
        if hk_cfg:
            try:
                self._hotkeys = HotkeyListener(hk_cfg)
            except ImportError:
                print("hotkeys: platform listener not available", file=sys.stderr)

        if sys.platform != "win32":
            try:
                os.unlink(SOCKET_PATH)
            except FileNotFoundError:
                pass
        self.server = socket.socket(SOCKET_FAMILY, socket.SOCK_STREAM)
        if sys.platform == "win32":
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(SOCKET_PATH)
        self.server.listen(5)
        self.server.setblocking(False)

        signal.signal(signal.SIGINT, self._signal)
        signal.signal(signal.SIGTERM, self._signal)

        if self._hotkeys:
            self._hotkeys.start()

    def _signal(self, sig, frame):
        self.running = False

    def _should_notify(self):
        return self.config.get("notifications", {}).get("enabled", False)

    def _detect_android_resolution(self) -> tuple[int, int]:
        disp = self.config.get("display", {})
        if "android_width" in disp and "android_height" in disp:
            return disp["android_width"], disp["android_height"]
        res = get_adb_wm_size(timeout=self.adb_timeout)
        if res is not None:
            return res
        print("error: could not detect Android resolution "
              "(set display.android_width/height in config)", file=sys.stderr)
        sys.exit(1)

    def _kill_proc(self, proc):
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _handle_hotkey(self, action):
        if action == "toggle_clicking":
            self.active = not self.active
            if self.active:
                self.current_mode.reset()
            if self._should_notify():
                s = "on" if self.active else "off"
                self.platform.notify(f"clicker {s} ({self.mode}, {self.method_name})")

        elif action == "launcher":
            if self._launcher_proc is None or self._launcher_proc.poll() is not None:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "android_clicker.cli", "launcher"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    self._launcher_proc = proc
                else:
                    if self._should_notify():
                        self.platform.notify("launcher failed to start")
            else:
                self._kill_proc(self._launcher_proc)
                self._launcher_proc = None

        elif action == "overlay":
            if self._overlay_proc is None or self._overlay_proc.poll() is not None:
                self._spawn_overlay()
            else:
                self.overlay_state["quit"] = True
                self._kill_proc(self._overlay_proc)
                self._overlay_proc = None

        elif action == "overlay_toggle":
            if self._overlay_proc is not None and self._overlay_proc.poll() is None:
                self.overlay_state["visible"] = not self.overlay_state["visible"]

        elif action == "burst_clicks_plus":
            self._burst_clicks_change(+1)
        elif action == "burst_clicks_minus":
            self._burst_clicks_change(-1)

        elif action in self._hotkey_actions:
            ha = self._hotkey_actions[action]
            t = ha.get("type")
            if t == "run":
                proc = subprocess.Popen(ha["cmd"], shell=True)
                timeout_ms = ha.get("timeout_ms", 0)
                if timeout_ms > 0:
                    self._hk_procs[action] = {
                        "proc": proc,
                        "start": time.monotonic(),
                        "timeout": timeout_ms / 1000.0,
                    }
            elif t == "mode":
                self._switch_mode(ha.get("name"))

    def _switch_mode(self, name: str) -> bool:
        if name not in self.modes:
            return False
        old_method = self.method_name
        self.mode = name
        self.current_mode = self.modes[name]
        self.current_mode.reset()
        self._next_click = 0.0

        mode_cfg = load_modeconfig(name)
        new_method = mode_cfg.get("method", "adb-pipe")

        if new_method != old_method:
            result = create_injector(new_method, self.host_w, self.host_h,
                                     shared_uinput=self._shared_uinput,
                                     adb_connect=self.adb_connect,
                                     adb_timeout=self.adb_timeout)
            if result:
                inj, actual_method = result
                old = self.injector
                self.injector = inj
                self.method_name = actual_method
                old.close()

        if self._should_notify():
            self.platform.notify(f"mode: {name}")
        return True

    def _burst_clicks_change(self, delta):
        if self.mode != "follow.burst":
            return
        data = load_modeconfig(self.mode)
        data["clicks"] = max(1, data.get("clicks", 10) + delta)
        save_modeconfig(self.mode, data)
        self.current_mode.config["clicks"] = data["clicks"]
        if self._should_notify():
            self.platform.notify(f"burst clicks: {data['clicks']}")

    def _spawn_overlay(self):
        self.overlay_state["quit"] = False
        self.overlay_state["visible"] = True
        proc = subprocess.Popen(
            [sys.executable, "-m", "android_clicker.cli", "overlay", "start"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            self._overlay_proc = proc
        else:
            self.overlay_state["quit"] = True
            if self._should_notify():
                self.platform.notify("overlay failed to start (install PyQt6?)")

    @property
    def uinput_available(self):
        return self._shared_uinput is not None

    def get_cursor_pos(self) -> tuple[int, int]:
        return self.platform.get_cursor_position()

    def run(self):
        if self._should_notify():
            self.platform.notify(f"daemon started ({self.method_name}, {self.mode})")
        if self.active and self._should_notify():
            self.platform.notify(f"clicker active ({self.mode}, {self.method_name})")

        while self.running:
            now = time.monotonic()

            if self.active:
                if isinstance(self.current_mode, CustomMode):
                    remaining = self.current_mode.next_time - now
                else:
                    remaining = self._next_click - now
                wait = max(0.001, min(0.05, remaining)) if remaining > 0 else 0.001
            else:
                wait = 0.05

            read_socks = [self.server] + list(self._clients.keys())
            try:
                r, _, _ = select.select(read_socks, [], [], wait)
            except InterruptedError:
                continue

            now = time.monotonic()

            if self.server in r:
                try:
                    conn, _ = self.server.accept()
                    conn.setblocking(False)
                    self._clients[conn] = {"last_activity": now, "buffer": b""}
                except OSError:
                    pass

            dead: list[socket.socket] = []
            for sock in list(self._clients.keys()):
                if sock not in r:
                    continue
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    dead.append(sock)
                    continue
                if not chunk:
                    dead.append(sock)
                    continue

                entry = self._clients[sock]
                entry["buffer"] += chunk
                entry["last_activity"] = now

                while True:
                    try:
                        msg, end = json.JSONDecoder().raw_decode(entry["buffer"].decode())
                    except (json.JSONDecodeError, ValueError):
                        break
                    try:
                        resp = self.handle_command(msg)
                    except Exception as e:
                        resp = {"ok": False, "error": str(e)}
                    try:
                        sock.sendall(json.dumps(resp).encode())
                    except OSError:
                        dead.append(sock)
                        break
                    entry["buffer"] = entry["buffer"][end:]

                if len(entry["buffer"]) > 65536:
                    dead.append(sock)

            idle_cutoff = now - 60.0
            for sock, entry in list(self._clients.items()):
                if entry["last_activity"] < idle_cutoff:
                    dead.append(sock)

            for sock in dead:
                self._clients.pop(sock, None)
                try:
                    sock.close()
                except OSError:
                    pass

            if self._hotkeys:
                try:
                    while True:
                        action = self._hotkeys.queue.get_nowait()
                        self._handle_hotkey(action)
                except queue.Empty:
                    pass

            now = time.monotonic()

            dead = []
            for name, hp in self._hk_procs.items():
                if hp["proc"].poll() is not None:
                    dead.append(name)
                elif hp["timeout"] > 0 and now - hp["start"] >= hp["timeout"]:
                    hp["proc"].kill()
                    hp["proc"].wait()
                    dead.append(name)
            for name in dead:
                del self._hk_procs[name]
            if self.active and not isinstance(self.current_mode, CustomMode) and now >= self._next_click:
                try:
                    self.current_mode.tick()
                except Exception:
                    pass

                interval = self.current_mode.interval()
                jit_ms = self.current_mode.jitter_ms()
                jit = (random.random() - 0.5) * 2 * jit_ms / 1000.0
                self._next_click = now + max(0.001, interval + jit)

            if self.active and isinstance(self.current_mode, CustomMode):
                while self.active and now >= self.current_mode.next_time:
                    try:
                        self.current_mode.tick()
                    except Exception:
                        pass
                    now = time.monotonic()

            if self.active and not self.injector.healthy():
                self.active = False
                if self._should_notify():
                    self.platform.notify("clicker stopped: ADB disconnected")

        self.cleanup()

    def handle_command(self, msg):
        cmd = msg.get("cmd")
        args = msg.get("args", {})
        n = self._should_notify

        if cmd == "ping":
            return {"ok": True}

        if cmd == "toggle":
            self.active = not self.active
            s = "on" if self.active else "off"
            if self.active:
                self.current_mode.reset()
            if n():
                self.platform.notify(f"clicker {s} ({self.mode}, {self.method_name})")
            return {"ok": True, "message": s}

        if cmd == "on":
            self.active = True
            self.current_mode.reset()
            if n():
                self.platform.notify(f"clicker on ({self.mode}, {self.method_name})")
            return {"ok": True, "message": "on"}

        if cmd == "off":
            self.active = False
            if n():
                self.platform.notify("clicker off")
            return {"ok": True, "message": "off"}

        if cmd == "mode":
            name = args.get("mode")
            ok = self._switch_mode(name)
            return {"ok": ok, "message": name} if ok else {"ok": False, "error": "invalid mode"}

        if cmd == "read_mode":
            data = load_modeconfig(args["mode"])
            return {"ok": True, "data": data}

        if cmd == "save_mode":
            mode = args["mode"]
            save_modeconfig(mode, args.get("data", {}))
            self.modes = create_modes(self.injector, self)
            if self.mode in self.modes:
                self.current_mode = self.modes[self.mode]
            return {"ok": True}

        if cmd == "create_mode":
            name = args.get("name")
            type_ = args.get("type")
            full_name = f"{type_}.{name}"
            if full_name in self.modes:
                return {"ok": False, "error": f"mode '{full_name}' already exists"}
            template = FIXED_CREATE_TEMPLATE if type_ == "fixed" else CUSTOM_CREATE_TEMPLATE
            os.makedirs(MODECONFIG_DIR, exist_ok=True)
            path = os.path.join(MODECONFIG_DIR, f"{full_name}.toml")
            with open(path, "w") as f:
                f.write(template)
            self.modes = create_modes(self.injector, self)
            data = load_modeconfig(full_name)
            return {"ok": True, "mode": full_name, "data": data}

        if cmd == "delete_mode":
            mode = args["mode"]
            path = os.path.join(MODECONFIG_DIR, f"{mode}.toml")
            if os.path.exists(path):
                os.remove(path)
            self.modes = create_modes(self.injector, self)
            if self.mode == mode or self.mode not in self.modes:
                self.current_mode = next(iter(self.modes.values()))
                self.mode = self.current_mode.name
                self.current_mode.reset()
            elif self.mode in self.modes:
                self.current_mode = self.modes[self.mode]
            return {"ok": True}

        if cmd == "list_modes":
            return {"ok": True, "modes": sorted(self.modes.keys())}

        if cmd == "method":
            name = args.get("name")
            if name:
                if self.mode != "follow":
                    return {"ok": False, "error": "method switch only allowed in follow mode"}
                if name == "uinput" and self._shared_uinput is None:
                    return {"ok": False, "error": "uinput not available (set uinput=true in config)"}
                result = create_injector(name, self.host_w, self.host_h,
                                         shared_uinput=self._shared_uinput,
                                         adb_connect=self.adb_connect,
                                         adb_timeout=self.adb_timeout)
                if result is None:
                    return {"ok": False, "error": f"failed to create {name} injector"}
                inj, actual_method = result
                old = self.injector
                self.injector = inj
                self.method_name = actual_method
                if old:
                    old.close()
                if n():
                    self.platform.notify(f"method: {actual_method}")
                return {"ok": True, "message": actual_method}
            return {"ok": True, "data": {"method": self.method_name,
                                          "available": available_methods(self._shared_uinput is not None)}}

        if cmd == "stop":
            self.running = False
            return {"ok": True, "message": "stopping"}

        if cmd == "overlay":
            action = args.get("action")
            os_ = self.overlay_state
            if action == "start":
                if self._overlay_proc is not None and self._overlay_proc.poll() is None:
                    return {"ok": True, "state": os_}
                self._spawn_overlay()
                return {"ok": True, "state": os_}
            if action == "stop":
                if self._overlay_proc is not None and self._overlay_proc.poll() is None:
                    os_["quit"] = True
                    self._kill_proc(self._overlay_proc)
                    self._overlay_proc = None
                return {"ok": True, "state": os_}
            if action == "show":
                os_["visible"] = True
            elif action == "hide":
                os_["visible"] = False
            elif action == "toggle":
                os_["visible"] = not os_["visible"]
            elif action == "quit":
                os_["quit"] = True
            elif action == "reset":
                os_["quit"] = False
            else:
                return {"ok": False, "error": f"unknown overlay action: {action}"}
            return {"ok": True, "state": os_}

        if cmd == "cursor_pos":
            host_x, host_y = self.platform.get_cursor_position()
            android = self._translate(host_x, host_y)
            return {
                "ok": True,
                "data": {
                    "active": self.active,
                    "mode": self.mode,
                    "method": self.method_name,
                    "modes": sorted(self.modes.keys()),
                    "host": {"x": host_x, "y": host_y},
                    "android": {"x": android[0], "y": android[1]} if android else None,
                    "window_bounds": self.window_bounds,
                    "android_resolution": {"w": self.android_w, "h": self.android_h},
                    "uinput_available": self.uinput_available,
                    "overlay_state": {
                        **self.overlay_state,
                        "overlay_running": self._overlay_proc is not None
                                            and self._overlay_proc.poll() is None,
                    },
                },
            }

        if cmd == "status":
            return {
                "ok": True,
                "data": {
                    "active": self.active,
                    "mode": self.mode,
                    "method": self.method_name,
                },
            }

        return {"ok": False, "error": f"unknown command: {cmd}"}

    def _translate(self, host_x, host_y):
        now = time.monotonic()
        if now - self.last_window_check > 1.0:
            self._update_window()

        aw = self.android_w
        ah = self.android_h

        if self.window_bounds:
            wx, wy, ww, wh = self.window_bounds
            if ww == 0 or wh == 0:
                return None
            rel_x = (host_x - wx) / ww
            rel_y = (host_y - wy) / wh
            if rel_x < 0 or rel_x > 1 or rel_y < 0 or rel_y > 1:
                return None
            return int(rel_x * aw), int(rel_y * ah)

        return min(host_x, aw - 1), min(host_y, ah - 1)

    def _android_to_host(self, ax, ay):
        self._update_window()

        aw = self.android_w
        ah = self.android_h

        if self.window_bounds and aw > 0 and ah > 0:
            wx, wy, ww, wh = self.window_bounds
            return int(wx + (ax / aw) * ww), int(wy + (ay / ah) * wh)

        return ax, ay

    def _update_window(self):
        self.last_window_check = time.monotonic()
        self.window_bounds = self.platform.get_window_bounds()

    def cleanup(self):
        for sock in list(self._clients.keys()):
            try:
                sock.close()
            except OSError:
                pass
        self._clients.clear()
        if self._hotkeys:
            self._hotkeys.stop()
        for hp in self._hk_procs.values():
            try:
                hp["proc"].kill()
                hp["proc"].wait()
            except Exception:
                pass
        self._hk_procs.clear()
        self._kill_proc(self._launcher_proc)
        self._kill_proc(self._overlay_proc)
        self.injector.close()
        if self._shared_uinput:
            ui, _ = self._shared_uinput
            ui.close()
        if self._should_notify():
            self.platform.notify("daemon stopped")
        if sys.platform != "win32":
            try:
                os.unlink(SOCKET_PATH)
            except FileNotFoundError:
                pass
