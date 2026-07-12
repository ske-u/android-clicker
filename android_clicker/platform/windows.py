from android_clicker.platform import PlatformAdapter


class WindowsAdapter(PlatformAdapter):
    def notify(self, message: str) -> None:
        print(f"[notification] {message}")

    def get_window_bounds(self) -> None:
        return None

    def get_cursor_position(self) -> tuple[int, int]:
        raise NotImplementedError(
            "TODO: use ctypes.windll.user32.GetCursorPos "
            "or win32api to get mouse position on Windows"
        )

    def get_display_resolution(self) -> tuple[int, int]:
        raise NotImplementedError(
            "TODO: use ctypes.windll.user32.GetSystemMetrics "
            "or tkinter to get display size on Windows"
        )
