"""Internal Windows launcher: never start the command before Job attachment."""
import subprocess
import sys


def main():
    import win32event
    # The event name is unique and the parent owns it before Popen. Opening it
    # fails closed if the parent is already gone; no fallback executes commands.
    event = win32event.OpenEvent(0x00100000, False, sys.argv[1])  # SYNCHRONIZE
    try:
        if win32event.WaitForSingleObject(event, win32event.INFINITE) != win32event.WAIT_OBJECT_0:
            raise OSError("Windows launch gate wait failed")
    finally:
        event.Close()
    return subprocess.call(sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
