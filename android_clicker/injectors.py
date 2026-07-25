import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading

ADB_PATH = "adb"
ADB_SERIAL = ""

def set_adb_path(v):
    global ADB_PATH
    ADB_PATH = v

def set_adb_serial(v):
    global ADB_SERIAL
    ADB_SERIAL = v

def _adb_cmd(*args):
    cmd = [ADB_PATH]
    if ADB_SERIAL:
        cmd += ["-s", ADB_SERIAL]
    cmd += list(args)
    return cmd

def detect_adb_path():
    if sys.platform != "win32":
        return "adb"
    candidates = [
        os.environ.get("PROGRAMFILES", "C:\\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"),
    ]
    for base in candidates:
        for variant in ("BlueStacks_nxt", "BlueStacks"):
            path = os.path.join(base, variant, "HD-Adb.exe")
            if os.path.exists(path):
                return path
    return "adb"

METHODS = ["adb-socket", "uinput"]


def available_methods(uinput_enabled=False):
    if sys.platform == "linux" and uinput_enabled:
        return list(METHODS)
    return ["adb-socket"]


def create_shared_uinput(host_w, host_h):
    """Create the daemon-level persistent uinput device. Returns (ui, e) or raises."""
    from evdev import UInput, ecodes as e, AbsInfo
    ui = UInput(
        {e.EV_KEY: [e.BTN_TOUCH],
         e.EV_ABS: [
             (e.ABS_MT_SLOT, AbsInfo(0, 0, 9, 0, 0, 0)),
             (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
             (e.ABS_MT_POSITION_X, AbsInfo(0, 0, host_w - 1, 0, 0, 0)),
             (e.ABS_MT_POSITION_Y, AbsInfo(0, 0, host_h - 1, 0, 0, 0)),
             (e.ABS_MT_PRESSURE, AbsInfo(0, 0, 255, 0, 0, 0)),
         ]},
        name="android-clicker-touch",
        phys="android-clicker/input0",
        input_props=(e.INPUT_PROP_DIRECT,),
    )
    return ui, e


class BaseInjector:
    coord_space = "android"

    def tap(self, x, y):
        raise NotImplementedError
    def zoom(self, x, y, amount, duration=200, spread=20, steps=10):
        raise NotImplementedError
    def close(self):
        pass
    def drain(self):
        pass
    def healthy(self) -> bool:
        return True


def ensure_adb(adb_connect, timeout=5):
    try:
        r = subprocess.run([ADB_PATH, "devices"], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        print("error: adb not found (install android-tools-adb)", file=sys.stderr)
        return False

    lines = [l.strip() for l in r.stdout.strip().split("\n")
             if l.strip() and not l.startswith("*") and "List" not in l]
    if any("device" in l and "offline" not in l for l in lines):
        return True

    subprocess.run([ADB_PATH, "connect", adb_connect], capture_output=True, timeout=timeout)
    r = subprocess.run([ADB_PATH, "devices"], capture_output=True, text=True, timeout=timeout)
    lines = [l.strip() for l in r.stdout.strip().split("\n")
             if l.strip() and not l.startswith("*") and "List" not in l]
    ok = any("device" in l and "offline" not in l for l in lines)
    if not ok:
        print(f"warning: no ADB device after connect ({adb_connect})", file=sys.stderr)
    return ok


def get_adb_wm_size(timeout=5) -> tuple[int, int] | None:
    """Run `adb shell wm size` and return (width, height) or None."""
    try:
        r = subprocess.run(
            _adb_cmd("shell", "wm size"),
            capture_output=True, text=True, timeout=timeout,
        )
        m = re.search(r"Physical size:\s*(\d+)x(\d+)", r.stdout)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


class AdbSocketInjector(BaseInjector):
    _ADB_PORT = 5037
    _ADDR = ("127.0.0.1", _ADB_PORT)

    def __init__(self, adb_connect, timeout=5):
        ensure_adb(adb_connect, timeout=timeout)
        self._dead = False
        self._closed = False
        self._shell_pid = None
        self.sock = None
        self._queue = queue.Queue(maxsize=10000)
        self._connect()
        self._worker = threading.Thread(target=self._writer, daemon=True)
        self._worker.start()

    def _connect(self):
        try:
            self.sock = socket.create_connection(self._ADDR, timeout=5)
        except OSError:
            self._dead = True
            return
        try:
            cmd = f"host:transport:{ADB_SERIAL}" if ADB_SERIAL else "host:transport-any"
            self._send_adb(cmd)
            resp = self.sock.recv(4)
            if resp != b"OKAY":
                raise ConnectionError(f"ADB transport failed: {resp!r}")
            self._send_adb("shell,raw:echo PID$$; while read line; do eval $line; done")
            resp = self.sock.recv(4)
            if resp != b"OKAY":
                raise ConnectionError(f"ADB shell failed: {resp!r}")
            self.sock.settimeout(2)
            pid_buf = b""
            try:
                while True:
                    c = self.sock.recv(1)
                    if c == b"\n":
                        break
                    pid_buf += c
                pid_line = pid_buf.decode().strip()
                self._shell_pid = int(pid_line[3:]) if pid_line.startswith("PID") else None
            except (socket.timeout, ValueError):
                self._shell_pid = None
            finally:
                self.sock.settimeout(None)
            self._dead = False
        except OSError:
            self._dead = True
        except ConnectionError:
            self._dead = True

    def _send_adb(self, cmd):
        payload = cmd.encode() + b"\n"
        self.sock.sendall(f"{len(payload):04x}".encode() + payload)

    def _writer(self):
        while not self._dead:
            try:
                cmd = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.sock.sendall(cmd)
            except OSError:
                break

    def healthy(self) -> bool:
        return not self._dead

    def tap(self, x, y):
        if self._dead:
            return
        try:
            self._queue.put_nowait(f"input tap {x} {y}\n".encode())
        except queue.Full:
            pass

    def _reconnect(self):
        self.close()
        self._closed = False
        self._connect()

    def drain(self):
        old_pid = self._shell_pid
        self._shell_pid = None
        self._dead = True
        if self.sock is not None:
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        if old_pid is not None:
            threading.Thread(target=self._kill_pid, args=(old_pid,), daemon=True).start()
        self._dead = False
        self._connect()
        self._worker = threading.Thread(target=self._writer, daemon=True)
        self._worker.start()

    def _kill_pid(self, pid):
        try:
            subprocess.run(_adb_cmd("shell", "kill", "-9", str(pid)),
                           capture_output=True, timeout=2)
        except Exception:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass


class UinputInjector(BaseInjector):
    coord_space = "host"

    def __init__(self, host_w, host_h, shared=None):
        if shared:
            self.ui, self.e = shared
            self._shared = True
        else:
            from evdev import UInput, ecodes as e, AbsInfo
            self.e = e
            self.ui = UInput(
                {
                    e.EV_KEY: [e.BTN_TOUCH],
                    e.EV_ABS: [
                        (e.ABS_MT_SLOT, AbsInfo(0, 0, 9, 0, 0, 0)),
                        (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
                        (e.ABS_MT_POSITION_X, AbsInfo(0, 0, host_w - 1, 0, 0, 0)),
                        (e.ABS_MT_POSITION_Y, AbsInfo(0, 0, host_h - 1, 0, 0, 0)),
                        (e.ABS_MT_PRESSURE, AbsInfo(0, 0, 255, 0, 0, 0)),
                    ],
                },
                name="android-clicker-touch",
                phys="android-clicker/input0",
                input_props=(e.INPUT_PROP_DIRECT,),
            )
            self._shared = False

    def tap(self, x, y):
        e = self.e
        self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, 0)
        self.ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, 1)
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, x)
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, y)
        self.ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 1)
        self.ui.write(e.EV_KEY, e.BTN_TOUCH, 1)
        self.ui.syn()
        self.ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 0)
        self.ui.write(e.EV_KEY, e.BTN_TOUCH, 0)
        self.ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
        self.ui.syn()

    def close(self):
        if not self._shared:
            self.ui.close()


INJECTOR_CLASSES = {
    "adb-socket": AdbSocketInjector,
    "adb-pipe": AdbSocketInjector,
    "uinput": UinputInjector,
}


def create_injector(method, host_w, host_h, shared_uinput=None, adb_connect=None, adb_timeout=5):
    if method == "uinput":
        if sys.platform != "linux":
            print("warning: uinput requires Linux, falling back to adb-socket", file=sys.stderr)
            method = "adb-socket"
        elif shared_uinput is None:
            print("warning: uinput disabled globally, falling back to adb-socket", file=sys.stderr)
            method = "adb-socket"
    cls = INJECTOR_CLASSES.get(method)
    if cls is None:
        return None
    try:
        if cls is UinputInjector:
            return (cls(host_w=host_w, host_h=host_h, shared=shared_uinput), method)
        return (cls(adb_connect=adb_connect, timeout=adb_timeout), method)
    except ImportError:
        print(f"error: {method} requires python-evdev (pip install python-evdev)", file=sys.stderr)
    except PermissionError:
        print(f"error: {method} needs 'input' group (usermod -aG input $USER)", file=sys.stderr)
    except OSError as e:
        print(f"error: {method} init failed: {e}", file=sys.stderr)
    except Exception as e:
        print(f"error: {method} init failed: {e}", file=sys.stderr)
    return None
