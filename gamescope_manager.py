"""Gamescope management, dynamic display discovery, and target window inspection.

This module detects nested X11 displays spawned by Gamescope without hardcoding
display numbers, locates the target Sober/Roblox window, and manages Gamescope
session lifecycles safely.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from Xlib import X, display
    from Xlib.error import XError
except (ImportError, Exception):  # pragma: no cover
    X = None
    display = None
    XError = Exception


class GamescopeLaunchError(RuntimeError):
    """A launch failed before a usable Gamescope display appeared."""


def is_sober_running() -> bool:
    """Check if a Flatpak Sober or Roblox window/process is already active."""
    try:
        session = find_active_gamescope_session()
        if session and session.get("target_window_id"):
            return True
    except Exception:
        pass

    try:
        proc_dir = Path("/proc")
        if proc_dir.is_dir():
            for p in proc_dir.iterdir():
                if p.name.isdigit():
                    try:
                        cmdline = (p / "cmdline").read_bytes().lower()
                        if b"org.vinegarhq.sober" in cmdline or b"/app/bin/sober" in cmdline:
                            return True
                    except (OSError, PermissionError):
                        continue
    except Exception:
        pass
    return False


def get_available_x_displays() -> list[str]:
    """List all available X11 displays from /tmp/.X11-unix."""
    socket_dir = Path("/tmp/.X11-unix")
    if not socket_dir.is_dir():
        return []
    displays = []
    for p in socket_dir.glob("X*"):
        name = p.name
        if name.startswith("X") and name[1:].isdigit():
            displays.append(":" + name[1:])
    return sorted(displays, key=lambda d: int(d[1:]))


def get_nested_displays(host_display: str | None = None) -> list[str]:
    """Return all X11 displays excluding the host desktop display."""
    if host_display is None:
        host_display = os.environ.get("DISPLAY", ":0")
    host_num = host_display.split(".")[0]
    return [d for d in get_available_x_displays() if d.split(".")[0] != host_num]


def inspect_display(disp_name: str) -> dict[str, Any] | None:
    """Connect to a display and inspect root window and top-level clients."""
    try:
        d = display.Display(disp_name)
    except Exception:
        return None

    try:
        root = d.screen().root
        windows = []
        sober_win = None
        main_app_win = None

        for child in root.query_tree().children:
            try:
                attrs = child.get_attributes()
                if attrs.map_state != X.IsViewable:
                    continue

                wm_name = child.get_wm_name() or ""
                wm_class = child.get_wm_class() or ("", "")
                class_str = " ".join(wm_class).lower()
                name_str = wm_name.lower()

                geom = child.get_geometry()
                info = {
                    "id": child.id,
                    "name": wm_name,
                    "class": wm_class,
                    "x": geom.x,
                    "y": geom.y,
                    "width": geom.width,
                    "height": geom.height,
                }
                windows.append(info)

                # Identify Sober or Roblox window
                if "sober" in class_str or "roblox" in class_str or "sober" in name_str or "roblox" in name_str:
                    sober_win = child
                elif geom.width > 200 and geom.height > 200 and "steamcompmgr" not in name_str:
                    main_app_win = child

            except (XError, Exception):
                continue

        chosen_win = sober_win or main_app_win
        result = {
            "display": disp_name,
            "windows": windows,
            "target_window_id": chosen_win.id if chosen_win else None,
            "target_window_name": chosen_win.get_wm_name() if chosen_win else None,
        }
        d.close()
        return result
    except Exception:
        try:
            d.close()
        except Exception:
            pass
        return None


def find_active_gamescope_session(host_display: str | None = None) -> dict[str, Any] | None:
    """Detect the active Gamescope display with a running client window."""
    nested = get_nested_displays(host_display)
    for disp in nested:
        info = inspect_display(disp)
        if info and info.get("target_window_id"):
            return info
    for disp in nested:
        info = inspect_display(disp)
        if info is not None:
            return info
    return None


def get_window_geometry(disp_name: str, window_id: int) -> tuple[int, int, int, int] | None:
    """Return (x, y, width, height) of a window on a specific display."""
    try:
        d = display.Display(disp_name)
        win = d.create_resource_object("window", window_id)
        geom = win.get_geometry()
        res = (geom.x, geom.y, geom.width, geom.height)
        d.close()
        return res
    except Exception:
        return None


def get_default_refresh_rate() -> int:
    """Return configured or detected default refresh rate."""
    env_val = os.environ.get("GAMESCOPE_REFRESH")
    if env_val:
        try:
            val = int(env_val)
            if val > 0:
                return val
        except ValueError:
            pass
    return 60


def launch_sober_in_gamescope(
    display_index: int = 0,
    width: int = 1280,
    height: int = 720,
    fullscreen: bool = False,
    refresh_rate: int | None = None,
    timeout: float = 10.0,
    poll_interval: float = 0.25,
) -> tuple[subprocess.Popen[Any], str | None]:
    """Launch Flatpak Sober wrapped inside Gamescope.

    Returns (process, discovered_display).
    """
    if refresh_rate is None:
        refresh_rate = get_default_refresh_rate()
    existing = set(get_available_x_displays())
    cmd = build_gamescope_command(
        display_index=display_index,
        width=width,
        height=height,
        fullscreen=fullscreen,
        refresh_rate=refresh_rate,
    )

    try:
        # A file keeps the long-lived child from blocking on an unread pipe.
        try:
            output_file = tempfile.TemporaryFile(mode="w+b", dir=Path(__file__).parent)
        except OSError:
            # Read-only packaged installs still need a safe transient sink.
            output_file = tempfile.TemporaryFile(mode="w+b")
        proc = subprocess.Popen(cmd, stdout=output_file, stderr=subprocess.STDOUT)
        proc._gamescope_output_file = output_file
    except FileNotFoundError as exc:
        if "output_file" in locals():
            output_file.close()
        raise GamescopeLaunchError("ไม่พบคำสั่ง gamescope — ติดตั้ง Gamescope ก่อนแล้วลองใหม่") from exc
    except OSError as exc:
        if "output_file" in locals():
            output_file.close()
        raise GamescopeLaunchError(f"เปิด Gamescope ไม่ได้: {exc}") from exc

    discovered = None
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        return_code = proc.poll()
        if return_code is not None:
            detail = " ".join(read_process_output(proc).split())
            suffix = f": {detail[-4000:]}" if detail else ""
            _close_process_output(proc)
            raise GamescopeLaunchError(f"Gamescope หยุดทำงานก่อนเริ่มเกม (รหัส {return_code}){suffix}")
        current = set(get_available_x_displays())
        diff = current - existing
        if diff:
            discovered = sorted(diff)[0]
            break
        time.sleep(poll_interval)

    return proc, discovered


def build_gamescope_command(
    display_index: int = 0,
    width: int = 1280,
    height: int = 720,
    fullscreen: bool = False,
    refresh_rate: int | None = None,
) -> list[str]:
    """Build the Gamescope command with matching nested output timing."""
    if refresh_rate is None:
        refresh_rate = get_default_refresh_rate()
    cmd = [
        "gamescope",
        "-W", str(width),
        "-H", str(height),
        "-r", str(refresh_rate),
        "-o", str(refresh_rate),
        "--framerate-limit", str(refresh_rate),
        "--display-index", str(display_index),
        "--force-grab-cursor",
        "--immediate-flips",
    ]
    if fullscreen:
        cmd.append("-f")

    cmd.extend(["--", "flatpak", "run", "org.vinegarhq.Sober"])
    return cmd


def read_process_output(proc: subprocess.Popen[Any]) -> str:
    """Return captured Gamescope output for an exited or stopped child."""
    output_file = vars(proc).get("_gamescope_output_file")
    if output_file is not None:
        try:
            output_file.flush()
            fd = output_file.fileno()
            size = os.fstat(fd).st_size
            if hasattr(os, "pread"):
                captured = os.pread(fd, 4000, max(0, size - 4000))
            else:
                output_file.seek(max(0, size - 4000))
                captured = output_file.read(4000)
            if captured:
                return captured.decode(errors="replace")
        except (AttributeError, OSError):
            pass
    try:
        captured = proc.communicate(timeout=0.1)[0]
    except Exception:
        return ""
    return captured.decode(errors="replace") if isinstance(captured, bytes) else str(captured or "")


def _close_process_output(proc: subprocess.Popen[Any]) -> None:
    output_file = vars(proc).get("_gamescope_output_file")
    if output_file is not None:
        try:
            output_file.close()
        except OSError:
            pass
        try:
            del proc._gamescope_output_file
        except AttributeError:
            pass


def stop_process_safely(proc: subprocess.Popen[Any] | None, timeout: float = 3.0) -> None:
    """Safely terminate a spawned process without affecting other applications."""
    if proc is None or proc.poll() is not None:
        if proc is not None:
            _close_process_output(proc)
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception:
            pass
    except Exception:
        pass
    finally:
        _close_process_output(proc)
