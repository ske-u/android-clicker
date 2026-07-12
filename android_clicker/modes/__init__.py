import os

from ._base import BaseMode
from .follow import FollowMode
from .follow_burst import FollowBurstMode
from .fixed import FixedMode
from .custom import CustomMode
from ..config import MODECONFIG_DIR, load_modeconfig

FOLLOW_TEMPLATE = """# follow mode
#
# method         Injection backend: "adb-pipe" or "uinput"
# interval       ms between clicks
# jitter_ms      ms random offset added to interval
# jitter_px      px random offset on x/y per click

method = "adb-pipe"
interval = 13
jitter_ms = 3
jitter_px = 10
"""

BURST_TEMPLATE = """# follow.burst mode
#
# Fires a fixed number of clicks at cursor position, then stops.
# Toggle on to fire another burst.
#
# clicks         Number of clicks per burst
# jitter_clicks  Random offset on click count (\u00b1)
# interval       ms between clicks
# jitter_ms      ms random offset added to interval
# jitter_px      px random offset on x/y per click

method = "adb-pipe"
clicks = 10
jitter_clicks = 0
interval = 13
jitter_ms = 3
jitter_px = 10
"""


def create_modes(injector, daemon):
    os.makedirs(MODECONFIG_DIR, exist_ok=True)
    follow_path = os.path.join(MODECONFIG_DIR, "follow.toml")
    if not os.path.exists(follow_path):
        with open(follow_path, "w") as f:
            f.write(FOLLOW_TEMPLATE)
    burst_path = os.path.join(MODECONFIG_DIR, "follow.burst.toml")
    if not os.path.exists(burst_path):
        with open(burst_path, "w") as f:
            f.write(BURST_TEMPLATE)

    modes = {}

    if os.path.isdir(MODECONFIG_DIR):
        for f in sorted(os.listdir(MODECONFIG_DIR)):
            if f.endswith(".toml"):
                name = f[:-5]
                if name in modes:
                    continue
                cfg = load_modeconfig(name)
                if name == "follow":
                    modes[name] = FollowMode(cfg, injector, daemon, name=name)
                elif name == "follow.burst":
                    modes[name] = FollowBurstMode(cfg, injector, daemon, name=name)
                elif name.startswith("fixed."):
                    modes[name] = FixedMode(cfg, injector, daemon, name=name)
                elif name.startswith("custom."):
                    modes[name] = CustomMode(cfg, injector, daemon, name=name)

    return modes
