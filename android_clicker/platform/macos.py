import subprocess

from android_clicker.platform import PlatformAdapter

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False


class MacAdapter(PlatformAdapter):
    def __init__(self, config=None):
        self._target = "BlueStacks"
        if config:
            self._target = config.get("display", {}).get("target_app", "BlueStacks")

    def _find_process(self):
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of every process'],
            capture_output=True, text=True, timeout=2
        )
        if r.returncode != 0:
            return None
        for name in r.stdout.strip().split(", "):
            if self._target.lower() in name.lower():
                return name
        return None

    def notify(self, message):
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "android-clicker"'],
            capture_output=True,
        )

    def get_window_bounds(self):
        proc_name = self._find_process()
        if not proc_name:
            return None
        try:
            pos = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to get position '
                 f'of window 1 of process "{proc_name}"'],
                capture_output=True, text=True, timeout=2
            )
            size = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to get size '
                 f'of window 1 of process "{proc_name}"'],
                capture_output=True, text=True, timeout=2
            )
            if pos.returncode == 0 and size.returncode == 0:
                px, py = pos.stdout.strip().split(", ")
                sw, sh = size.stdout.strip().split(", ")
                return int(px), int(py), int(sw), int(sh)
        except Exception:
            pass
        return None

    def get_cursor_position(self):
        if not HAS_PYAUTOGUI:
            return 0, 0
        try:
            x, y = pyautogui.position()
            return int(x), int(y)
        except Exception:
            return 0, 0

    def get_display_resolution(self):
        if not HAS_PYAUTOGUI:
            return None
        try:
            w, h = pyautogui.size()
            return int(w), int(h)
        except Exception:
            return None
