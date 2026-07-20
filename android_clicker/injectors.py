import atexit
import queue
import re
import sys
import subprocess
import threading

METHODS = ["adb-pipe", "uinput"]


def available_methods(uinput_enabled=False):
    if sys.platform == "linux" and uinput_enabled:
        return list(METHODS)
    return ["adb-pipe"]


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
    def healthy(self) -> bool:
        return True


def ensure_adb(adb_connect, timeout=5):
    try:
        r = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        print("error: adb not found (install android-tools-adb)", file=sys.stderr)
        return False

    lines = [l.strip() for l in r.stdout.strip().split("\n")
             if l.strip() and not l.startswith("*") and "List" not in l]
    if any("device" in l and "offline" not in l for l in lines):
        return True

    subprocess.run(["adb", "connect", adb_connect], capture_output=True, timeout=timeout)
    r = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=timeout)
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
            ["adb", "shell", "wm size"],
            capture_output=True, text=True, timeout=timeout,
        )
        m = re.search(r"Physical size:\s*(\d+)x(\d+)", r.stdout)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


class AdbPipeInjector(BaseInjector):
    _SENTINEL = b""

    def __init__(self, adb_connect, timeout=5):
        ensure_adb(adb_connect, timeout=timeout)
        self._dead = False
        self._closed = False
        self.proc = subprocess.Popen(
            ["adb", "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        atexit.register(self.close)
        self._queue = queue.Queue(maxsize=1000)
        self._worker = threading.Thread(target=self._writer, daemon=True)
        self._worker.start()

    def healthy(self) -> bool:
        return not self._dead

    def _writer(self):
        while not self._closed:
            cmd = self._queue.get()
            if cmd is self._SENTINEL:
                break
            try:
                self.proc.stdin.write(cmd)
            except (BrokenPipeError, OSError):
                if self._reconnect_worker():
                    try:
                        self.proc.stdin.write(cmd)
                        continue
                    except (BrokenPipeError, OSError):
                        pass
                self._dead = True
                break

    def _reconnect_worker(self) -> bool:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                self.proc.wait(timeout=3)
        except Exception:
            pass
        try:
            self.proc = subprocess.Popen(
                ["adb", "shell"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            return True
        except Exception:
            return False

    def tap(self, x, y):
        if self._dead:
            return
        try:
            self._queue.put_nowait(f"input tap {x} {y}\n".encode())
        except queue.Full:
            pass

    def _reconnect(self):
        self.close()
        try:
            self.proc = subprocess.Popen(
                ["adb", "shell"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            self._dead = False
            self._closed = False
            atexit.register(self.close)
            self._queue = queue.Queue(maxsize=1000)
            self._worker = threading.Thread(target=self._writer, daemon=True)
            self._worker.start()
        except Exception:
            self._dead = True

    def close(self):
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self.close)
        try:
            self._queue.put_nowait(self._SENTINEL)
        except queue.Full:
            pass
        self._worker.join(timeout=3)
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


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
    "adb-pipe": AdbPipeInjector,
    "uinput": UinputInjector,
}


def create_injector(method, host_w, host_h, shared_uinput=None, adb_connect=None, adb_timeout=5):
    if method == "uinput":
        if sys.platform != "linux":
            print("warning: uinput requires Linux, falling back to adb-pipe", file=sys.stderr)
            method = "adb-pipe"
        elif shared_uinput is None:
            print("warning: uinput disabled globally, falling back to adb-pipe", file=sys.stderr)
            method = "adb-pipe"
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
