import ctypes
from ctypes import wintypes

from android_clicker.platform import PlatformAdapter

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False

try:
    from win10toast import ToastNotifier
    HAS_WIN10TOAST = True
except ImportError:
    HAS_WIN10TOAST = False


class WindowsAdapter(PlatformAdapter):
    def __init__(self, config=None):
        self._target = "BlueStacks"
        if config:
            self._target = config.get("display", {}).get("target_app", "BlueStacks")
        self._user32 = ctypes.windll.user32

    def notify(self, message):
        if HAS_WIN10TOAST:
            try:
                ToastNotifier().show_toast("android-clicker", message, duration=3)
            except Exception:
                pass

    def get_window_bounds(self):
        try:
            target = self._target.lower()
            found = [0, 0, 0, 0]

            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def callback(hwnd, _):
                length = self._user32.GetWindowTextLengthW(hwnd) + 1
                buf = ctypes.create_unicode_buffer(length)
                self._user32.GetWindowTextW(hwnd, buf, length)
                if target in buf.value.lower():
                    rect = wintypes.RECT()
                    self._user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    found[0] = rect.left
                    found[1] = rect.top
                    found[2] = rect.right - rect.left
                    found[3] = rect.bottom - rect.top
                    return False
                return True

            self._user32.EnumWindows(WNDENUMPROC(callback), 0)
            if found[2] > 0 and found[3] > 0:
                return tuple(found)
        except Exception:
            pass
        return None

    def get_cursor_position(self):
        if HAS_PYAUTOGUI:
            try:
                x, y = pyautogui.position()
                return int(x), int(y)
            except Exception:
                pass
        try:
            pt = wintypes.POINT()
            self._user32.GetCursorPos(ctypes.byref(pt))
            return pt.x, pt.y
        except Exception:
            return 0, 0

    def get_display_resolution(self):
        if HAS_PYAUTOGUI:
            try:
                w, h = pyautogui.size()
                return int(w), int(h)
            except Exception:
                pass
        try:
            w = self._user32.GetSystemMetrics(0)
            h = self._user32.GetSystemMetrics(1)
            return w, h
        except Exception:
            return None
