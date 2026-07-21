import io
import subprocess

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


def screencap_adb(timeout=15):
    if Image is None:
        return None
    try:
        r = subprocess.run(
            _adb_cmd("shell", "screencap", "-p"),
            capture_output=True, timeout=timeout,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        return Image.open(io.BytesIO(r.stdout))
    except Exception:
        return None
