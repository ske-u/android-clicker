# android-clicker

Auto-clicker daemon for Android emulators — **Waydroid on Linux (Hyprland)** and **BlueStacks on macOS and Windows**.

Single-process socket server with a CLI frontend. Optional PyQt6 overlay and launcher GUIs.

## Platform support

| Platform | Emulator | Status |
|----------|----------|--------|
| Linux (Hyprland) | Waydroid | **Stable** |
| macOS | BlueStacks | **Experimental** |
| Windows | BlueStacks | **Experimental** |

## Quick start

```sh
git clone https://github.com/ske-u/android-clicker
cd android-clicker
python -m android_clicker start      # start the daemon

# Or open the launcher GUI (requires PyQt6)
python -m android_clicker launcher
```

**Linux / macOS** — wrapper script (requires `chmod +x android-clicker` once):
```sh
./android-clicker start
./android-clicker launcher
```

**Windows** — invoke via Python directly (no shebang support):
```sh
python android-clicker start
python android-clicker launcher
```

### macOS requirements

```sh
pip install pyautogui            # required: cursor tracking + display resolution
```

### Optional dependencies

```sh
pip install PyQt6                # overlay + launcher GUI (all platforms)
pip install tomlkit              # launcher config editor (all platforms)
pip install Pillow               # screencap_check action (all platforms)
pip install python-evdev         # uinput injector + global hotkeys (Linux)
pip install pynput               # global hotkeys (macOS, Windows)
pip install win10toast           # desktop notifications (Windows)
```

Uinput on Linux requires `input` group membership:

```sh
sudo usermod -aG input $USER && logout
```

## Usage

Commands below use the `./android-clicker` wrapper script (repo root).
Equivalent: `python -m android_clicker <command>`.

<details>
<summary>Daemon control</summary>

- `./android-clicker start` — start the daemon
- `./android-clicker stop` — shut the daemon down
- `./android-clicker toggle` — enable/disable clicking
- `./android-clicker on` — enable clicking
- `./android-clicker off` — disable clicking
- `./android-clicker status` — show daemon state

</details>

<details>
<summary>Mode management</summary>

- `./android-clicker mode select <name>` — switch mode (follow, follow.burst, fixed.\*, custom.\*)
- `./android-clicker mode create fixed <name>` — create fixed mode variant
- `./android-clicker mode create custom <name>` — create custom mode
- `./android-clicker mode edit <name>` — list all mode configs (or show keys for a mode)
- `./android-clicker mode edit <name> sequence` — list/manage sequence steps (click, wait, screencap, notify, log, run_mode, run, zoom, set, rm, clear, append, prepend)
- `./android-clicker mode edit <name> points` — list/manage points (add, set, rm, clear, append, prepend)

</details>

<details>
<summary>Backend & overlay</summary>

- `./android-clicker method adb-pipe` — switch to ADB pipe injector
- `./android-clicker method uinput` — switch to uinput injector (Linux only)
- `./android-clicker overlay start` — open overlay GUI (blocks terminal)
- `./android-clicker overlay toggle` — show/hide overlay
- `./android-clicker overlay show` — show overlay
- `./android-clicker overlay hide` — hide overlay
- `./android-clicker overlay quit` — quit overlay process
- `./android-clicker launcher` — open launcher GUI

</details>

## Modes

| Mode | Description |
|------|-------------|
| `follow` | Clicks at host cursor position mapped into Android coordinates |
| `follow.burst` | Fires a fixed number of clicks at cursor position, then deactivates |
| `fixed.*` | Cycles through predefined points with jitter, reset timer, cycle config |
| `custom.*` | Configurable sequence of actions: click, wait, screencap check, zoom, notify, log, run shell, run sub-mode |

Custom modes are defined as `modeconfigs/<name>.toml` files (created with `mode create`).

## License

GPL v3
