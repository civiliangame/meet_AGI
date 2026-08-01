#!/usr/bin/env python
"""One command to put Kindred in a meeting.

    python scripts/kindred.py                     # default meeting, start server if needed
    python scripts/kindred.py <meet-url>          # a different meeting
    python scripts/kindred.py --watch             # ...and tail what it hears and says
    python scripts/kindred.py --leave             # pull the bot out
    python scripts/kindred.py --check             # preflight only, dispatch nothing

Does the boring parts in the right order and refuses to dispatch a bot into a setup that
cannot work — a bot that joins and then turns out to be deaf costs a meeting to discover,
and looks identical to a working one until somebody says the wake word.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
PYTHON = BACKEND / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

# The recurring "Voice AI test" call. Overridden by the first positional argument.
DEFAULT_MEETING_URL = "https://meet.google.com/org-rzjs-ici"
DEFAULT_PORT = 5000

OK, WARN, BAD = "  ok  ", " warn ", " FAIL "


def _c(text: str, code: str) -> str:
    return text if os.name == "nt" and not os.environ.get("WT_SESSION") else f"\033[{code}m{text}\033[0m"


def green(t): return _c(t, "32")
def yellow(t): return _c(t, "33")
def red(t): return _c(t, "31")
def dim(t): return _c(t, "2")
def bold(t): return _c(t, "1")


def api(port: int, path: str, method: str = "GET", body: dict | None = None, timeout: float = 90):
    url = f"http://localhost:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def server_up(port: int) -> bool:
    try:
        api(port, "/api/health", timeout=3)
        return True
    except Exception:
        return False


# --- taking over the port ------------------------------------------------------------
# Always restart rather than reusing whatever is already listening. Reusing it means a
# code change silently does not take effect, and the symptom is a feature that "doesn't
# work" while the server quietly serves the previous build. That cost two debugging
# rounds; starting clean every time costs three seconds.


def listeners_on(port: int) -> list[int]:
    """PIDs listening on `port`, using whatever the platform provides."""
    pids: set[int] = set()
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=15
            ).stdout
        except Exception:
            return []
        for line in out.splitlines():
            parts = line.split()
            # Proto  Local            Foreign     State       PID
            if len(parts) >= 5 and parts[3].upper() == "LISTENING":
                local = parts[1]
                if local.rsplit(":", 1)[-1] == str(port) and parts[4].isdigit():
                    pids.add(int(parts[4]))
    else:
        for cmd in (["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], ["fuser", f"{port}/tcp"]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
            except Exception:
                continue  # tool not installed on this box; try the next one
            pids.update(int(t) for t in out.split() if t.isdigit())
            if pids:
                break
    return sorted(pids)


def command_line(pid: int) -> str:
    """Best-effort command line for a pid. Empty string when it cannot be read."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=20,
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=15
            ).stdout
        return out.strip()
    except Exception:
        return ""


def looks_like_kindred(cmdline: str) -> bool:
    lowered = cmdline.lower()
    return "uvicorn" in lowered and "app.main" in lowered


def kill(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=20)
    else:
        subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=15)


def retire_previous(port: int, force: bool) -> bool:
    """Evict any bots the old server owns, then stop it. False if we must not proceed."""
    if server_up(port):
        # Graceful first: ask the running server to pull its own bots out. That closes
        # the Recall session properly instead of orphaning a bot in somebody's meeting.
        try:
            report = api(port, "/api/dev/evict-bots", "POST", {}, timeout=60)
            for bot in report.get("evicted", []):
                where = bot.get("meeting_url") or bot.get("bot_id")[:8]
                mark = green(f"[{OK}]") if bot["left"] else yellow(f"[{WARN}]")
                print(f"{mark} evicted    bot left {where}")
        except Exception as exc:
            print(yellow(f"[{WARN}] could not evict cleanly ({type(exc).__name__}); "
                         f"the sweep after restart will catch strays"))

    pids = listeners_on(port)
    if not pids:
        return True

    for pid in pids:
        cmdline = command_line(pid)
        if not force and cmdline and not looks_like_kindred(cmdline):
            # Port 5000 is popular — Flask, AirPlay Receiver, and others live here.
            # Killing an unrelated process because it happens to hold the port is a
            # much worse outcome than refusing.
            print(red(f"[{BAD}] :{port} is held by pid {pid}, which is not a Kindred server"))
            print(dim(f"           {cmdline[:110]}"))
            print(dim("           free the port, use --port, or pass --force to kill it anyway"))
            return False
        print(dim(f"  stopping previous server (pid {pid}) …"))
        kill(pid)

    for _ in range(20):
        time.sleep(0.25)
        if not listeners_on(port):
            return True
    print(red(f"[{BAD}] :{port} is still held after killing {pids}"))
    return False


def start_server(port: int) -> subprocess.Popen | None:
    print(dim(f"  starting backend on :{port} …"))
    log = open(REPO / "kindred-server.log", "ab")
    process = subprocess.Popen(
        [str(PYTHON), "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)],
        cwd=BACKEND, stdout=log, stderr=subprocess.STDOUT,
    )
    for _ in range(40):
        time.sleep(0.5)
        if server_up(port):
            return process
        if process.poll() is not None:
            print(red(f"[{BAD}] server exited — see kindred-server.log"))
            return None
    print(red(f"[{BAD}] server did not come up in 20s — see kindred-server.log"))
    return None


def preflight(port: int) -> bool:
    """Report every capability. Returns False only for things that block dispatch."""
    health = api(port, "/api/health")
    detail = {p["name"]: p for p in health["providers"]}
    ready = True

    recall = detail.get("recall", {})
    if recall.get("configured"):
        print(green(f"[{OK}] recall     {recall['detail']}"))
    else:
        print(red(f"[{BAD}] recall     {recall.get('detail')}"))
        ready = False

    for name in ("voice", "reasoning", "knowledge"):
        item = detail.get(name, {})
        mark = green(f"[{OK}]") if item.get("configured") else yellow(f"[{WARN}]")
        print(f"{mark} {name:10} {item.get('detail', '')[:88]}")

    # Not in /api/health because it is about reachability, not credentials: the tunnel
    # can be configured and still be down, and that is the failure that looks like
    # "the wake word is broken".
    hears = api(port, "/api/dev/tunnel")
    if hears.get("reachable"):
        print(green(f"[{OK}] hearing    tunnel reachable — \"{hears['wake_word']}\" will work"))
    elif hears.get("configured"):
        print(red(f"[{BAD}] hearing    {hears['detail']}"))
        print(dim(f"           start it with: ngrok http --url=<your-domain> {port}"))
        ready = False
    else:
        print(yellow(f"[{WARN}] hearing    {hears['detail']}"))
        print(dim("           the bot will speak but never hear; /ask still works"))
    return ready


def watch(port: int, meeting_id: str) -> None:
    """Tail the server log, filtered to the things worth seeing on stage."""
    import re

    log_path = REPO / "kindred-server.log"
    if not log_path.exists():
        print(dim("  (no server log to watch — the server was already running elsewhere)"))
        return

    print(bold("\n  watching. ctrl-c to stop.\n"))
    interesting = re.compile(
        r"recall_live: \[|WAKE in|would post to chat|playing \d+ms|filler|"
        r"interjection|Inworld|generativelanguage",
        re.I,
    )
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        try:
            while True:
                line = handle.readline()
                if not line:
                    time.sleep(0.25)
                    continue
                if not interesting.search(line):
                    continue
                if "recall_live: [" in line:
                    print("  " + green("HEARD ") + line.split("] ", 1)[-1].strip()[:110])
                elif "WAKE in" in line:
                    print("  " + bold(yellow("WAKE  ")) + line.split("— ", 1)[-1].strip()[:110])
                elif "would post to chat" in line or "posted" in line:
                    print("  " + _c("CHAT  ", "36") + line.split("): ", 1)[-1].strip()[:110])
                elif "playing" in line:
                    print("  " + _c("SPEAK ", "35") + line.split("playing ", 1)[-1].strip()[:110])
        except KeyboardInterrupt:
            print(dim("\n  stopped watching. the bot is still in the meeting.\n"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Put Kindred in a meeting.")
    parser.add_argument("url", nargs="?", default=DEFAULT_MEETING_URL, help="Google Meet URL.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--title", default="Voice AI test")
    parser.add_argument("--watch", action="store_true", help="Tail what it hears and says.")
    parser.add_argument("--leave", action="store_true", help="Remove Kindred from every meeting.")
    parser.add_argument("--check", action="store_true", help="Preflight against a running server.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Dispatch even if preflight fails, and kill whatever holds the port.",
    )
    args = parser.parse_args()

    print(bold("\n  Kindred\n"))

    if args.leave:
        if not server_up(args.port):
            print(red(f"[{BAD}] no server on :{args.port} to leave through"))
            return 1
        report = api(args.port, "/api/dev/evict-bots", "POST", {}, timeout=60)
        for bot in report.get("evicted", []):
            where = bot.get("meeting_url") or bot["bot_id"][:8]
            mark = green(f"[{OK}]") if bot["left"] else yellow(f"[{WARN}]")
            print(f"{mark} left       {where}")
        if not report.get("evicted"):
            print(dim("  no bots in any meeting"))
        return 0

    if args.check:
        if not server_up(args.port):
            print(red(f"[{BAD}] no server on :{args.port}"))
            return 1
        print(green(f"[{OK}] server     http://localhost:{args.port}"))
        return 0 if preflight(args.port) else 1

    # Always start fresh. Anything already on the port is a previous build.
    if not retire_previous(args.port, args.force):
        return 1
    if start_server(args.port) is None:
        return 1
    print(green(f"[{OK}] server     http://localhost:{args.port}  (docs at /docs)"))

    # Catch bots orphaned by a server that died without evicting — a hard kill, a crash,
    # or a run that predates this script. Recall keeps them in the meeting regardless.
    try:
        report = api(args.port, "/api/dev/evict-bots", "POST", {}, timeout=60)
        for bot in report.get("evicted", []):
            where = bot.get("meeting_url") or bot["bot_id"][:8]
            print(green(f"[{OK}] evicted    stray bot from {where}"))
    except Exception as exc:
        print(yellow(f"[{WARN}] could not sweep for stray bots ({type(exc).__name__})"))

    ready = preflight(args.port)
    if not ready and not args.force:
        print(red("\n  refusing to dispatch. fix the above, or pass --force.\n"))
        return 1

    print(dim(f"\n  joining {args.url} …"))
    try:
        meeting = api(
            args.port, "/api/meetings", "POST", {"meeting_url": args.url, "title": args.title}
        )
    except urllib.error.HTTPError as exc:
        print(red(f"[{BAD}] {exc.read().decode()[:200]}"))
        return 1

    if meeting["state"] == "failed":
        print(red(f"[{BAD}] {meeting.get('error')}"))
        return 1

    meeting_id = meeting["id"]
    print(green(f"[{OK}] dispatched {meeting_id}"))
    print(dim(f"             bot {meeting['bot_id']}"))

    for _ in range(40):
        time.sleep(1.5)
        state = api(args.port, f"/api/meetings/{meeting_id}")["state"]
        if state == "in_call":
            print(green(f"[{OK}] in call    admit it if Meet is asking\n"))
            break
        if state in ("failed", "ended"):
            print(red(f"[{BAD}] {state}\n"))
            return 1
    else:
        print(yellow(f"[{WARN}] still joining — it may be waiting to be admitted\n"))

    print(bold("  try:"))
    print('    say  "Hey AGI, what does the Q3 deck say about the new product line?"')
    print(dim(f"    ask  curl -X POST localhost:{args.port}/api/meetings/{meeting_id}/ask \\"))
    print(dim("           -H 'content-type: application/json' \\"))
    print(dim("           -d '{\"question\":\"what changed on churn?\",\"speak\":true}'"))
    print(dim("    stop python scripts/kindred.py --leave\n"))

    if args.watch:
        watch(args.port, meeting_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
