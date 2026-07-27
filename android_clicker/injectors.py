import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time

ADB_PATH = "adb"
ADB_SERIAL = ""

def set_adb_path(v):
    global ADB_PATH
    ADB_PATH = v

def set_adb_serial(v):
    global ADB_SERIAL
    ADB_SERIAL = v

APP_PROCESS_ENABLED = False

def set_app_process_enabled(v):
    global APP_PROCESS_ENABLED
    APP_PROCESS_ENABLED = v

def get_app_process_enabled():
    return APP_PROCESS_ENABLED

JAR_NAME = "injector.jar"
JAR_REMOTE = "/data/local/tmp/injector.jar"
LOCAL_PORT = 17000
REMOTE_PORT = 17000

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

METHODS = ["adb-socket", "uinput", "app-process"]


def available_methods(uinput_enabled=False, app_process_enabled=False):
    m = ["adb-socket"]
    if app_process_enabled:
        m.append("app-process")
    if sys.platform == "linux" and uinput_enabled:
        m.append("uinput")
    return m


def push_jar(adb_connect, timeout=5):
    set_adb_serial(adb_connect)
    jar_local = os.path.join(os.path.dirname(__file__), JAR_NAME)
    try:
        subprocess.run(_adb_cmd("push", jar_local, JAR_REMOTE),
                       capture_output=True, timeout=timeout)
    except Exception:
        pass


def create_uinput_device(host_w, host_h):
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
    supports_zoom = False

    def tap(self, x, y):
        raise NotImplementedError
    def zoom_start(self, lx, rx, center_y):
        pass
    def zoom_tick(self, sx, dx, center_y):
        pass
    def zoom_end(self):
        pass
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
        self._queue = queue.Queue(maxsize=250)
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
    supports_zoom = True

    def __init__(self, host_w, host_h, uinput_device):
        self.ui, self.e = uinput_device

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

    def zoom_start(self, lx, rx, center_y):
        e = self.e
        self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, 0)
        self.ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, 1)
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, lx)
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, center_y)
        self.ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 100)
        self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, 1)
        self.ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, 2)
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, rx)
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, center_y)
        self.ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 100)
        self.ui.write(e.EV_KEY, e.BTN_TOUCH, 1)
        self.ui.syn()

    def zoom_tick(self, sx, dx, center_y):
        e = self.e
        self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, 0)
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, sx)
        self.ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 100)
        self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, 1)
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, dx)
        self.ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 100)
        self.ui.syn()

    def zoom_end(self):
        e = self.e
        self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, 0)
        self.ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
        self.ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 0)
        self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, 1)
        self.ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
        self.ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 0)
        self.ui.write(e.EV_KEY, e.BTN_TOUCH, 0)
        self.ui.syn()

    def close(self):
        pass


class AppProcessInjector(BaseInjector):
    coord_space = "android"
    supports_zoom = True

    def __init__(self, adb_connect, timeout=5):
        ensure_adb(adb_connect, timeout=timeout)
        self._dead = False
        self._closed = False
        self._proc = None
        self._sock = None

        subprocess.run(
            _adb_cmd("forward", f"tcp:{LOCAL_PORT}", f"tcp:{REMOTE_PORT}"),
            capture_output=True, timeout=timeout,
        )

        self._proc = subprocess.Popen(
            _adb_cmd("shell",
                     f"CLASSPATH={JAR_REMOTE}",
                     "app_process", "/", "com.clicker.Injector",
                     str(REMOTE_PORT)),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        self._sock = self._connect_retry(timeout)

        self._queue = queue.Queue(maxsize=250)
        self._worker = threading.Thread(target=self._writer, daemon=True)
        self._worker.start()

    def _connect_retry(self, timeout):
        deadline = time.monotonic() + timeout
        last_err = None
        while time.monotonic() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=2)
                return s
            except ConnectionRefusedError as e:
                last_err = e
                time.sleep(0.3)
        raise ConnectionError(f"app_process not ready in {timeout}s")

    def _reconnect(self, timeout=5):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        subprocess.run(_adb_cmd("forward", "--remove", f"tcp:{LOCAL_PORT}"),
                       capture_output=True, timeout=timeout)
        subprocess.run(_adb_cmd("forward", f"tcp:{LOCAL_PORT}", f"tcp:{REMOTE_PORT}"),
                       capture_output=True, timeout=timeout)
        self._proc = subprocess.Popen(
            _adb_cmd("shell", f"CLASSPATH={JAR_REMOTE}",
                     "app_process", "/", "com.clicker.Injector", str(REMOTE_PORT)),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._sock = self._connect_retry(timeout)
        self._dead = False

    def _writer(self):
        while not self._dead:
            try:
                cmd = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._sock.sendall(cmd.encode() + b"\n")
                self._sock.recv(1024)
            except OSError:
                if self._closed:
                    self._dead = True
                    break
                try:
                    self._reconnect(5)
                except (ConnectionError, OSError, subprocess.TimeoutExpired):
                    self._dead = True
                    break

    def tap(self, x, y):
        if self._dead:
            return
        try:
            self._queue.put_nowait(f"T {x} {y}")
        except queue.Full:
            pass

    def zoom_start(self, lx, rx, center_y):
        if self._dead:
            return
        try:
            self._queue.put_nowait(f"D {lx} {center_y} {rx} {center_y}")
        except queue.Full:
            pass

    def zoom_tick(self, sx, dx, center_y):
        if self._dead:
            return
        try:
            self._queue.put_nowait(f"M {sx} {center_y} {dx} {center_y}")
        except queue.Full:
            pass

    def zoom_end(self):
        if self._dead:
            return
        try:
            self._queue.put_nowait("U")
        except queue.Full:
            pass

    def healthy(self) -> bool:
        return not self._dead

    def drain(self):
        self._dead = True
        if self._sock:
            self._sock.setblocking(False)
            try:
                while self._queue.get_nowait():
                    pass
            except queue.Empty:
                pass
            try:
                while self._sock.recv(4096):
                    pass
            except (BlockingIOError, OSError):
                pass
            self._sock.setblocking(True)
        self._dead = False
        self._worker = threading.Thread(target=self._writer, daemon=True)
        self._worker.start()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._dead = True
        if self._sock:
            try:
                self._sock.sendall(b"Q\n")
                self._sock.recv(1024)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        subprocess.run(_adb_cmd("forward", "--remove", f"tcp:{LOCAL_PORT}"),
                       capture_output=True, timeout=3)
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()


INJECTOR_CLASSES = {
    "adb-socket": AdbSocketInjector,
    "adb-pipe": AdbSocketInjector,
    "uinput": UinputInjector,
    "app-process": AppProcessInjector,
}


def create_injector(method, host_w, host_h, uinput_device=None, adb_connect=None, adb_timeout=5):
    if method == "uinput":
        if sys.platform != "linux":
            print("warning: uinput requires Linux, falling back to adb-socket", file=sys.stderr)
            method = "adb-socket"
        elif uinput_device is None:
            print("warning: uinput disabled globally, falling back to adb-socket", file=sys.stderr)
            method = "adb-socket"
    if method == "app-process" and not APP_PROCESS_ENABLED:
        print("warning: app_process disabled globally, falling back to adb-socket", file=sys.stderr)
        method = "adb-socket"
    cls = INJECTOR_CLASSES.get(method)
    if cls is None:
        return None
    try:
        if cls is UinputInjector:
            return (cls(host_w=host_w, host_h=host_h, uinput_device=uinput_device), method)
        if cls is AppProcessInjector:
            return (cls(adb_connect=adb_connect, timeout=adb_timeout), method)
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
