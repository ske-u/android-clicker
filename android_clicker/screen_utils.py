import os
import subprocess

try:
    from PIL import Image
except ImportError:
    Image = None


def get_pixel(img, x, y):
    if img is None:
        return None
    try:
        return img.getpixel((x, y))
    except Exception:
        return None


def rgb_match(pixel, hex_str, tol=15):
    if pixel is None:
        return False
    r = int(hex_str[0:2], 16)
    g = int(hex_str[2:4], 16)
    b = int(hex_str[4:6], 16)
    return abs(pixel[0] - r) <= tol and \
           abs(pixel[1] - g) <= tol and \
           abs(pixel[2] - b) <= tol


def colour_lookup(pixel, table, tol=15):
    if pixel is None:
        return None
    for hex_str, value in table.items():
        if rgb_match(pixel, hex_str, tol):
            return value
    return None


def colour_in_rect(img, x, y, w, h, hex_colour, tol=15):
    if img is None:
        return False
    tr = int(hex_colour[0:2], 16)
    tg = int(hex_colour[2:4], 16)
    tb = int(hex_colour[4:6], 16)
    x_end = min(x + w, img.width)
    y_end = min(y + h, img.height)
    for py in range(y, y_end):
        for px in range(x, x_end):
            p = img.getpixel((px, py))
            if abs(p[0] - tr) <= tol and \
               abs(p[1] - tg) <= tol and \
               abs(p[2] - tb) <= tol:
                return True
    return False


def screencap_adb():
    if Image is None:
        return None
    try:
        tmp_device = "/sdcard/._wdc_screencap.png"
        tmp_local = f"/tmp/._wdc_screencap_{os.getpid()}.png"

        r = subprocess.run(
            ["adb", "shell", f"screencap -p {tmp_device}"],
            capture_output=True, timeout=15,
        )
        if r.returncode != 0:
            return None

        r = subprocess.run(
            ["adb", "pull", tmp_device, tmp_local],
            capture_output=True, timeout=15,
        )
        subprocess.run(
            ["adb", "shell", f"rm {tmp_device}"],
            capture_output=True, timeout=5,
        )

        if r.returncode != 0:
            return None

        img = Image.open(tmp_local)
        os.unlink(tmp_local)
        return img
    except Exception:
        return None
