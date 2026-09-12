"""Open/focus or gracefully close this project's X11 application only."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def is_our_process(pid):
    try:
        args = Path(f"/proc/{int(pid)}/cmdline").read_bytes().split(b"\0")
        return str(ROOT / "auto_fishing.py").encode() in args
    except (OSError, ValueError):
        return False


def main(action):
    if action not in ("open", "close"):
        raise SystemExit("Usage: desktop_control.py open|close")
    from Xlib import X, display, protocol
    connection = display.Display()
    try:
        root = connection.screen().root
        clients = root.get_full_property(connection.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
        windows = []
        for wid in clients.value if clients is not None else []:
            window = connection.create_resource_object("window", int(wid))
            try:
                pid = window.get_full_property(connection.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
                if pid is not None and len(pid.value) and is_our_process(pid.value[0]):
                    windows.append(window)
            except Exception:
                continue  # A window may close while the desktop list is read.
        if action == "close":
            for window in windows:
                event = protocol.event.ClientMessage(window=window.id,
                    client_type=connection.intern_atom("WM_PROTOCOLS"),
                    data=(32, [connection.intern_atom("WM_DELETE_WINDOW"), X.CurrentTime, 0, 0, 0]))
                window.send_event(event, event_mask=X.NoEventMask)
        elif windows:
            event = protocol.event.ClientMessage(window=windows[0].id,
                client_type=connection.intern_atom("_NET_ACTIVE_WINDOW"),
                data=(32, [2, X.CurrentTime, 0, 0, 0]))
            root.send_event(event, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        else:
            with (ROOT / "launcher.log").open("ab") as log:
                subprocess.Popen([str(ROOT / "run.sh")], cwd=ROOT,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        connection.sync()
    finally:
        connection.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) == 2 else "")
