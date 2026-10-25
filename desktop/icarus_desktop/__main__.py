"""
icarus-desktop — the Icarus desktop daemon and its control client.

  icarus-desktop run                 start the daemon (spawns the avatar overlay)
  icarus-desktop ctl listen-start    hotkey pressed: open the mic
  icarus-desktop ctl listen-stop     hotkey released: transcribe and send
  icarus-desktop ctl toggle          tap-to-talk alternative to start/stop
  icarus-desktop ctl cancel          stop talking / drop the current turn
  icarus-desktop ctl status          JSON status
  icarus-desktop say "open firefox"  send typed text as if spoken (testing)

The ctl path imports nothing heavy so hotkeys stay fast.
"""

import socket
import sys


def ctl(line: str) -> int:
    from .config import runtime_dir
    path = runtime_dir() / "ctl.sock"
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(str(path))
        sock.sendall((line + "\n").encode())
        reply = sock.recv(65536).decode().strip()
    except OSError as e:
        print(f"icarus-desktop daemon not reachable at {path}: {e}", file=sys.stderr)
        return 1
    if reply != "ok":
        print(reply)
    return 0 if not reply.startswith(("error", "unknown")) else 1


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if args[0] == "ctl" and len(args) >= 2:
        return ctl(" ".join(args[1:]))
    if args[0] == "say" and len(args) >= 2:
        return ctl("say " + " ".join(args[1:]))
    if args[0] == "run":
        import asyncio
        import logging
        import os
        from .config import load_config
        from .daemon import Daemon
        logging.basicConfig(
            level=logging.DEBUG if os.getenv("DESKTOP_DEBUG") else logging.INFO,
            format="%(asctime)s - ICARUS DESKTOP - %(levelname)s - %(message)s",
        )
        logging.getLogger("faster_whisper").setLevel(logging.WARNING)
        try:
            asyncio.run(Daemon(load_config()).run())
        except KeyboardInterrupt:
            pass
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
