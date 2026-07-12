import argparse
import json
import re
import socket
import subprocess
import sys

from .config import (
    load_config, list_modeconfigs, load_modeconfig, parse_value, SCRIPT_DIR,
)
from .daemon import ClickDaemon, SOCKET_PATH
from .platform import PlatformAdapter


def send_cmd(command, **kwargs):
    payload = {"cmd": command}
    if kwargs:
        payload["args"] = kwargs
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(SOCKET_PATH)
    sock.send(json.dumps(payload).encode())
    data = sock.recv(65536)
    sock.close()
    return json.loads(data.decode())


class _Formatter(argparse.HelpFormatter):
    def _format_usage(self, usage, actions, groups, prefix):
        result = super()._format_usage(usage, actions, groups, prefix)
        return re.sub(r' \.\.\.', '', result)

    def start_section(self, heading):
        heading = None
        super().start_section(heading)

    def _format_args(self, action, default_metavar):
        if action.nargs == argparse.ZERO_OR_MORE and action.metavar:
            return '[%s]' % action.metavar
        return super()._format_args(action, default_metavar)

    def _format_action(self, action):
        if isinstance(action, argparse._SubParsersAction):
            parts = []
            for subaction in action._get_subactions():
                parts.append(self._format_action(subaction))
            return '\n'.join(parts)
        result = super()._format_action(action)
        return result.rstrip('\n')


def print_status(data):
    s = data.get("data", {})
    a = "ACTIVE" if s.get("active") else "INACTIVE"
    print(f"state:   {a}")
    print(f"mode:    {s.get('mode', '?')}")
    print(f"method:  {s.get('method', '?')}")


def _p(parent, name, **kw):
    kw.setdefault('add_help', False)
    kw.setdefault('formatter_class', _Formatter)
    return parent.add_parser(name, **kw)


def _build_step_from_list(action, args):
    step = {"action": "screencap_check" if action == "screencap" else action}
    if action == "click":
        step["x"] = int(args[0])
        step["y"] = int(args[1])
        if len(args) > 2:
            step["clicks"] = int(args[2])
    elif action == "click_cursor":
        if len(args) > 0:
            step["clicks"] = int(args[0])
    elif action == "wait":
        step["ms"] = int(args[0])
    elif action in ("notify", "log"):
        step["message"] = " ".join(args)
    elif action == "run":
        step["cmd"] = " ".join(args)
    elif action == "run_mode":
        step["mode"] = args[0]
        step["duration_ms"] = int(args[1])
    elif action == "zoom":
        step["x"] = int(args[0])
        step["y"] = int(args[1])
        step["start"] = max(5, min(95, int(args[2])))
        if len(args) > 3:
            step["end"] = max(5, min(95, int(args[3])))
        if len(args) > 4:
            step["duration"] = int(args[4])
    elif action in ("screencap", "screencap_check"):
        step["x"] = int(args[0])
        step["y"] = int(args[1])
        step["w"] = int(args[2])
        step["h"] = int(args[3])
        if len(args) > 4:
            step["colour"] = args[4]
            step["tol"] = int(args[5]) if len(args) > 5 else 0
            step["then"] = int(args[6]) if len(args) > 6 else 0
    return step


def _handle_scalar_edit(name, data, key, val_args):
    if not val_args:
        v = data.get(key, "<not set>")
        print(f"  {key} = {v}")
    else:
        val = parse_value(val_args[0])
        data[key] = val
        send_cmd("save_mode", mode=name, data=data)
        print(f"  set {key} = {val}")


def _handle_custom_array(name, data, seq, action, action_args):
    if action is None:
        for i, step in enumerate(seq):
            print(f"  {i}: {step}")
        return

    if action == "set":
        idx, k = int(action_args[0]), action_args[1]
        val = parse_value(" ".join(action_args[2:]))
        if 0 <= idx < len(seq):
            seq[idx][k] = val
            send_cmd("save_mode", mode=name, data=data)
            print(f"  set step {idx} {k} = {val}")
        else:
            print(f"  error: index {idx} out of range", file=sys.stderr)
        return

    if action == "rm":
        idx = int(action_args[0])
        if 0 <= idx < len(seq):
            removed = seq.pop(idx)
            send_cmd("save_mode", mode=name, data=data)
            print(f"  removed step {idx}: {removed}")
        else:
            print(f"  error: index {idx} out of range", file=sys.stderr)
        return

    if action == "clear":
        seq.clear()
        send_cmd("save_mode", mode=name, data=data)
        print("  sequence cleared")
        return

    if action in ("append", "prepend"):
        idx = int(action_args[0])
        step_action = action_args[1]
        step = _build_step_from_list(step_action, action_args[2:])
        pos = idx + 1 if action == "append" else idx
        pos = max(0, min(pos, len(seq)))
        seq.insert(pos, step)
        send_cmd("save_mode", mode=name, data=data)
        print(f"  inserted step at {pos}: {step}")
        return

    step = _build_step_from_list(action, action_args)
    seq.append(step)
    send_cmd("save_mode", mode=name, data=data)
    print(f"  added step {len(seq)-1}: {step}")


def _handle_fixed_array(name, data, points, action, action_args):
    if action is None:
        for i, p in enumerate(points):
            print(f"  {i}: {p}")
        return

    if action == "set":
        idx = int(action_args[0])
        x, y = int(action_args[1]), int(action_args[2])
        if 0 <= idx < len(points):
            points[idx]["x"] = x
            points[idx]["y"] = y
            if len(action_args) > 3:
                points[idx]["clicks"] = int(action_args[3])
            send_cmd("save_mode", mode=name, data=data)
            print(f"  set point {idx} to ({x}, {y})")
        else:
            print(f"  error: index {idx} out of range", file=sys.stderr)
        return

    if action == "rm":
        idx = int(action_args[0])
        if 0 <= idx < len(points):
            removed = points.pop(idx)
            send_cmd("save_mode", mode=name, data=data)
            print(f"  removed point {idx}: {removed}")
        else:
            print(f"  error: index {idx} out of range", file=sys.stderr)
        return

    if action == "clear":
        points.clear()
        send_cmd("save_mode", mode=name, data=data)
        print("  points cleared")
        return

    if action in ("append", "prepend"):
        idx = int(action_args[0])
        x, y = int(action_args[1]), int(action_args[2])
        point = {"x": x, "y": y}
        if len(action_args) > 3:
            point["clicks"] = int(action_args[3])
        pos = idx + 1 if action == "append" else idx
        pos = max(0, min(pos, len(points)))
        points.insert(pos, point)
        send_cmd("save_mode", mode=name, data=data)
        print(f"  inserted point at {pos}: ({x}, {y})")
        return

    x, y = int(action_args[0]), int(action_args[1])
    point = {"x": x, "y": y}
    if len(action_args) > 2:
        point["clicks"] = int(action_args[2])
    points.append(point)
    send_cmd("save_mode", mode=name, data=data)
    print(f"  added point {len(points)-1}: ({x}, {y})")


def _handle_mode_edit(args):
    if args.name is None:
        try:
            resp = send_cmd("list_modes")
            names = resp.get("modes", [])
        except Exception:
            names = list_modeconfigs()
        if names:
            for n in names:
                print(f"  {n}")
        else:
            print("  (no mode configs)")
        return

    try:
        resp = send_cmd("read_mode", mode=args.name)
        data = resp.get("data", {})
    except Exception:
        print("error: daemon not running (mode edit requires daemon)", file=sys.stderr)
        return
    if args.key is None:
        for k, v in data.items():
            if isinstance(v, list):
                print(f"  {k}  [{len(v)} items]")
            else:
                print(f"  {k} = {v}")
        return

    v = data.get(args.key)
    if v is None:
        print(f"  key '{args.key}' not found", file=sys.stderr)
        return

    if isinstance(v, list):
        action = args.args[0] if args.args else None
        action_args = args.args[1:] if args.args else []
        if args.key == "sequence":
            _handle_custom_array(args.name, data, v, action, action_args)
        elif args.key == "points":
            _handle_fixed_array(args.name, data, v, action, action_args)
        else:
            print(f"  array '{args.key}' — edit manually in the TOML file")
    else:
        _handle_scalar_edit(args.name, data, args.key, args.args)


def main():
    parser = argparse.ArgumentParser(formatter_class=_Formatter, add_help=False)
    sub = parser.add_subparsers(dest="command", metavar="[command]")

    for name, h in [
        ("start", "Start the daemon"),
        ("launcher", "Open the desktop launcher GUI"),
        ("toggle", "Toggle clicking on/off"),
        ("on", "Enable clicking"),
        ("off", "Disable clicking"),
        ("stop", "Stop the daemon"),
        ("status", "Show daemon status"),
    ]:
        _p(sub, name, help=h)

    op = _p(sub, "overlay", help="Overlay window control")
    ops = op.add_subparsers(dest="overlay_action", metavar="[action]")
    _p(ops, "start", help="Start overlay")
    _p(ops, "toggle", help="Toggle overlay visibility")
    _p(ops, "show", help="Show overlay")
    _p(ops, "hide", help="Hide overlay")
    _p(ops, "quit", help="Quit overlay process")

    mp = _p(sub, "mode", help="Show/switch click mode")
    mps = mp.add_subparsers(dest="mode_action", metavar="[action]")
    ms = _p(mps, "select", help="Switch to a mode")
    ms.add_argument("name", help="Mode name (follow, fixed.<name>, or <custom>)")
    mc = _p(mps, "create", help="Create a new mode")
    mcs = mc.add_subparsers(dest="create_action", metavar="[type]")
    _p(mcs, "fixed", help="Create a fixed variant mode").add_argument("name")
    _p(mcs, "custom", help="Create a custom mode").add_argument("name")

    mep = _p(mps, "edit", help="Edit mode config or array")
    mep.add_argument("name", nargs="?", help="Mode name (omit to list)")
    mep.add_argument("key", nargs="?", help="Config key or array name")
    mep.add_argument("args", nargs="*", help="Value or array operation + args")

    mt = _p(sub, "method", help="Show/switch injection backend")
    mts = mt.add_subparsers(dest="method_action", metavar="[method]")
    for name, h in [
        ("adb-pipe", "Switch to ADB pipe injector"),
        ("uinput", "Switch to uinput injector"),
    ]:
        _p(mts, name, help=h)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "start":
        config = load_config()
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect(SOCKET_PATH)
            s.close()
            print("error: daemon already running", file=sys.stderr)
            if config.get("notifications", {}).get("enabled", False):
                PlatformAdapter.detect().notify("daemon already running")
            sys.exit(1)
        except (ConnectionRefusedError, FileNotFoundError):
            pass
        mode = config.get("mode", "follow")
        mode_cfg = load_modeconfig(mode)
        print(f"daemon running (method: {mode_cfg.get('method', '?')}, mode: {mode})",
              file=sys.stderr)
        daemon = ClickDaemon(config)
        daemon.run()
        return

    if args.command == "launcher":
        subprocess.Popen(
            [sys.executable, "-m", "android_clicker.launcher"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=SCRIPT_DIR,
        )
        return

    if args.command == "overlay":
        if args.overlay_action is None:
            op.print_help()
            return
        if args.overlay_action == "start":
            from .overlay import main as overlay_main
            overlay_main()
            return
        resp = send_cmd("overlay", action=args.overlay_action)
        if resp.get("ok"):
            if args.overlay_action == "toggle":
                s = "visible" if resp["state"]["visible"] else "hidden"
                print(f"overlay {s}")
            else:
                print("ok")
        else:
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(1)
        return

    try:
        if args.command == "toggle":
            resp = send_cmd("toggle")
        elif args.command == "on":
            resp = send_cmd("on")
        elif args.command == "off":
            resp = send_cmd("off")
        elif args.command == "stop":
            resp = send_cmd("stop")
        elif args.command == "mode":
            if args.mode_action is None:
                mp.print_help()
                return
            if args.mode_action == "select":
                resp = send_cmd("mode", mode=args.name)
            elif args.mode_action == "create":
                resp = send_cmd("create_mode", name=args.name, type=args.create_action)
                if not resp.get("ok"):
                    print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
                    sys.exit(1)
                print(f"created mode '{resp['mode']}'")
                return
            elif args.mode_action == "edit":
                _handle_mode_edit(args)
                return
            else:
                mp.print_help()
                return
        elif args.command == "method":
            if args.method_action is None:
                mt.print_help()
                return
            resp = send_cmd("method", name=args.method_action)
            if resp.get("ok") and "data" in resp:
                d = resp["data"]
                print(f"method: {d['method']}  (available: {', '.join(d['available'])})")
                return

        elif args.command == "status":
            resp = send_cmd("status")
            if resp.get("ok"):
                print_status(resp)
            return
        else:
            print(f"unknown command: {args.command}")
            return

        if resp.get("ok"):
            msg = resp.get("message")
            if msg:
                print(msg)
        else:
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(1)

    except ConnectionRefusedError:
        print("error: daemon not running (start with 'android-clicker start')", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"error: socket not found ({SOCKET_PATH})", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
