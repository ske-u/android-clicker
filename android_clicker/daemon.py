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
)
from .hotkeys import HotkeyListener
from .modes import create_modes
from .modes.custom import CustomMode
from .platform import PlatformAdapter


SOCKET_PATH = "/tmp/android-clicker.sock"


class ClickDaemon:
    def __init__(self, config):
        self.config = config
        self.running = True
        self.active = config.get("active", False)
        self.mode = config.get("mode", "follow")

        self.platform = PlatformAdapter.detect(config)
        res = self.platform.get_display_resolution()
        if res is None:
            print("error: could not determine display resolution", file=sys.stderr)
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

        ensure_adb(adb_connect, timeout=adb_timeout)

        self.android_w, self.android_h = self._detect_android_resolution()

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

        hk_cfg = config.get("hotkeys", {})
        self._hotkeys = None
        if hk_cfg:
            try:
                self._hotkeys = HotkeyListener(hk_cfg)
            except ImportError:
                print("hotkeys: platform listener not available", file=sys.stderr)

        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
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

        next_click = 0.0

        while self.running:
            now = time.monotonic()

            if self.active:
                if isinstance(self.current_mode, CustomMode):
                    remaining = self.current_mode.next_time - now
                else:
                    remaining = next_click - now
                wait = max(0.001, min(0.05, remaining)) if remaining > 0 else 0.001
            else:
                wait = 0.05

            try:
                r, _, _ = select.select([self.server], [], [], wait)
            except InterruptedError:
                continue

            if r:
                conn, _ = self.server.accept()
                conn.setblocking(True)
                data = conn.recv(65536)
                if data:
                    try:
                        msg = json.loads(data.decode())
                        resp = self.handle_command(msg)
                        try:
                            conn.send(json.dumps(resp).encode())
                        except OSError:
                            pass
                    except Exception as e:
                        try:
                            conn.send(json.dumps({"ok": False, "error": str(e)}).encode())
                        except OSError:
                            pass
                conn.close()

            if self._hotkeys:
                try:
                    while True:
                        action = self._hotkeys.queue.get_nowait()
                        self._handle_hotkey(action)
                except queue.Empty:
                    pass

            now = time.monotonic()
            if self.active and not isinstance(self.current_mode, CustomMode) and now >= next_click:
                try:
                    self.current_mode.tick()
                except Exception:
                    pass

                interval = self.current_mode.interval()
                jit_ms = self.current_mode.jitter_ms()
                jit = (random.random() - 0.5) * 2 * jit_ms / 1000.0
                next_click = now + max(0.001, interval + jit)

            if self.active and isinstance(self.current_mode, CustomMode):
                while now >= self.current_mode.next_time:
                    try:
                        self.current_mode.tick()
                    except Exception:
                        pass
                    now = time.monotonic()

        self.cleanup()

    def handle_command(self, msg):
        cmd = msg.get("cmd")
        args = msg.get("args", {})
        n = self._should_notify

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
            mode = args.get("mode")
            if mode not in self.modes:
                return {"ok": False, "error": "invalid mode"}

            old_method = self.method_name
            self.mode = mode
            self.current_mode = self.modes[mode]
            self.current_mode.reset()

            mode_cfg = load_modeconfig(mode)
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

            if n():
                self.platform.notify(f"mode: {mode}")
            return {"ok": True, "message": mode}

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
        if self._hotkeys:
            self._hotkeys.stop()
        self._kill_proc(self._launcher_proc)
        self._kill_proc(self._overlay_proc)
        self.injector.close()
        if self._shared_uinput:
            ui, _ = self._shared_uinput
            ui.close()
        if self._should_notify():
            self.platform.notify("daemon stopped")
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
