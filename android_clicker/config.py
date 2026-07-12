import os
import sys
import tomllib


SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "android-clicker.toml")
MODECONFIG_DIR = os.path.join(SCRIPT_DIR, "modeconfigs")


def load_config():
    cfg = {"active": False, "mode": "follow"}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "rb") as f:
            cfg.update(tomllib.load(f))
    return cfg


def parse_value(s):
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        return int(s)
    except ValueError:
        pass
    return s


def list_modeconfigs():
    names = []
    if os.path.isdir(MODECONFIG_DIR):
        for f in sorted(os.listdir(MODECONFIG_DIR)):
            if f.endswith(".toml"):
                names.append(f[:-5])
    return names


def load_modeconfig(name):
    path = os.path.join(MODECONFIG_DIR, f"{name}.toml")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return tomllib.load(f)
    return {}


def _toml_dumps(data):
    lines = []
    for k, v in data.items():
        if isinstance(v, list) and k in ("sequence", "points"):
            continue
        lines.append(f"{k} = {_toml_val(v)}")
    for array_key in ("points", "sequence"):
        arr = data.get(array_key, [])
        if arr:
            lines.append(f"{array_key} = [")
            for item in arr:
                items = ", ".join(f"{sk} = {_toml_val(sv)}" for sk, sv in item.items())
                lines.append(f"  {{ {items} }},")
            lines.append("]")
        elif array_key in data:
            lines.append(f"{array_key} = []")
    return "\n".join(lines) + "\n"


def save_modeconfig(name, data):
    os.makedirs(MODECONFIG_DIR, exist_ok=True)
    path = os.path.join(MODECONFIG_DIR, f"{name}.toml")
    with open(path, "w") as f:
        f.write(_toml_dumps(data))


def save_config(data):
    import tomlkit

    if not os.path.exists(CONFIG_FILE):
        cfg = tomlkit.document()
        cfg["active"] = False
        cfg["mode"] = "follow"
    else:
        with open(CONFIG_FILE) as f:
            cfg = tomlkit.parse(f.read())

    for k in ("active", "mode", "uinput"):
        if k in data:
            cfg[k] = data[k]

    for section, sec_data in data.items():
        if section in ("active", "mode") or not isinstance(sec_data, dict):
            continue
        if section in cfg:
            for key, value in sec_data.items():
                if value is None:
                    cfg[section].pop(key, None)
                else:
                    cfg[section][key] = value
        else:
            non_none = {k: v for k, v in sec_data.items() if v is not None}
            if non_none:
                cfg[section] = tomlkit.table()
                for key, value in non_none.items():
                    cfg[section][key] = value

    with open(CONFIG_FILE, "w") as f:
        f.write(tomlkit.dumps(cfg))


def _toml_val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, dict):
        items = ", ".join(f"{k} = {_toml_val(val)}" for k, val in v.items())
        return "{" + items + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    return str(v)


FIXED_CREATE_TEMPLATE = """\
method = "adb-pipe"                                                                                          # Injection backend: adb-pipe or uinput
interval = 15                                                                                                # ms between clicks
jitter_px = 5                                                                                                # random offset on click x/y
jitter_ms = 5                                                                                                # random offset on interval
reset_timer = 10000                                                                                          # ms before cycling back to first point
jitter_timer = 250                                                                                           # random offset on reset_timer
cycle = true                                                                                                 # cycle through points
cycle_mode = "clicks"                                                                                        # clicks or time
cycle_clicks = 4                                                                                             # clicks per point before cycling
jitter_clicks = 1                                                                                            # random offset on cycle_clicks
cycle_delay = 15                                                                                             # ms before advancing to next point (when cycle_mode = "time")
points = [
  { x = 400, y = 400, clicks = 3 },                                                                          # 3 taps before cycling
  { x = 800, y = 800 },                                                                                      # 1 tap per cycle (default)
]

"""

CUSTOM_CREATE_TEMPLATE = """\
method = "adb-pipe"       # Injection backend: adb-pipe or uinput
screen_cap = false        # Enable screencap_check action (uses ADB regardless of method)
interval = 15             # ms between click repeats
jitter_px = 5             # random offset on click x/y
jitter_ms = 5             # random offset on interval
default_wait_ms = 50      # delay before each sequence step
wait_jitter = 10          # random offset on wait steps
repeat = true             # restart sequence when finished
sequence = [
  { action = "click", x = 400, y = 400, clicks = 3, interval = 100, jitter_px = 3, jitter_ms = 2 },          # tap burst + jitter
  { action = "wait", ms = 500, wait_jitter = 50 },                                                           # pause with jitter
  { action = "screencap_check", x = 300, y = 400, w = 20, h = 20, colour = "32343B", tol = 1, then = 0 },    # jump to step 0 if colour matches
  { action = "screencap_check", x = 770, y = 515, w = 20, h = 20, checks = [                                 # multi-check: first match wins
    { colour = "32343B", tol = 1, then = 3 },                                                                # match → jump to step 3
    { colour = "FF0000", tol = 5, then = 0 },                                                                # match → jump to step 0
  ], else = -1 },                                                                                            # -1 = advance to next step if no check matches
  { action = "zoom", x = 500, y = 500, start = 10, end = 90, duration = 300 },                               # pinch-to-zoom (start<end=zoom in, start>end=zoom out)
  { action = "notify", message = "done" },                                                                   # desktop notification
  { action = "log", message = "step completed" },                                                            # print to daemon stdout
  { action = "run_mode", mode = "", duration_ms = 30000, interval = 20, jitter_px = 3 },                     # run another mode for 30s, overriding interval/jitter
  { action = "run", cmd = "echo hello", timeout_ms = 5000 },                                                 # shell command with 5s timeout
]

"""
