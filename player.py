#!/usr/bin/env python3
"""
player.py -- terminal client for Hollow Moon.  One process per player, run as that player's Linux user.

stdin and the socket are watched at the same time: a reader thread feeds typed lines
into the asyncio loop while the loop also receives moderator messages.  Incoming
messages can therefore arrive while you are typing.
"""
import argparse
import asyncio
import getpass
import os
import pwd
import sys
import threading

from hollowcomm import Client, ProtocolError

try:
    import readline
except ImportError:
    readline = None

C = {"town": "\033[97m", "wolves": "\033[91m", "ghost": "\033[90m", "whisper": "\033[95m",
     "mod": "\033[93m", "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m", "cyan": "\033[96m"}
HELP = """commands:
  <text>            talk (town by day, pack at night if you are a wolf, ghosts if dead)
  /w NAME text      whisper privately to one player (limited per day; others see THAT you whispered)
  /vote NAME|none   vote to lynch (day vote phase; ballots are public)
  /ready            lobby: ready up   |   day: say you are ready to move to the vote
  /kill NAME        wolf, at night: vote for tonight's victim
  /peek NAME        seer, at night: learn whether NAME is a wolf
  /guard NAME       healer, at night: protect NAME (not the same person twice running)
  /who              list living and dead players
  /quit             leave"""

state = {"name": getpass.getuser(), "role": "?", "phase": "lobby"}
isatty = sys.stdin.isatty()


def out(text):
    """Print above the prompt without wrecking whatever the user is half way through typing."""
    buf = readline.get_line_buffer() if (readline and isatty) else ""
    if isatty:
        sys.stdout.write("\r\033[K")
    sys.stdout.write(text + "\n")
    if isatty:
        sys.stdout.write(prompt() + buf)
    sys.stdout.flush()


def prompt():
    return "%s[%s|%s]> " % (state["name"], state["role"], state["phase"])


def render(m):
    t = m.get("type")
    if t == "say":
        ch = m.get("channel", "town")
        col = C.get(ch, C["town"])
        tag = {"town": "", "wolves": "[pack] ", "ghost": "[ghost] ", "whisper": "[whisper] ", "mod": ""}.get(ch, "")
        who = m.get("from", "")
        body = m["text"] if ch == "mod" else "%s: %s" % (who, m["text"])
        out("%s%s%s%s" % (col, tag, body, C["reset"]))
    elif t == "phase":
        state["phase"] = m["phase"]
        out("%s== %s %s (%ds) == alive: %s%s" % (C["cyan"], m["phase"].upper().replace("_", " "), m["day"] or "",
                                                  m["seconds"], ", ".join(m["alive"]), C["reset"]))
    elif t == "role":
        state["role"] = m["role"]
        extra = ("  Your pack: " + ", ".join(m["wolves"])) if m["wolves"] else ""
        out("%s*** You are a %s.%s ***%s" % (C["bold"] + C["mod"], m["role"].upper(), extra, C["reset"]))
    elif t == "peek":
        out("%sYour vision: %s is %s.%s" % (C["bold"] + C["cyan"], m["target"],
                                            "A WOLF" if m["is_wolf"] else "not a wolf", C["reset"]))
    elif t == "end":
        out("%s*** %s WIN ***  roles: %s%s" % (C["bold"] + C["mod"], m["winner"].upper(),
                                              ", ".join("%s=%s" % kv for kv in m["roles"].items()), C["reset"]))
    elif t == "refused":
        out("Refused by moderator: " + m["text"])


def parse(line):
    """Turn a typed line into (type, fields)."""
    line = line.strip()
    if not line.startswith("/"):
        return ("chat", {"text": line}) if line else None
    cmd, _, rest = line[1:].partition(" ")
    cmd = cmd.lower()
    if cmd == "w":
        to, _, text = rest.strip().partition(" ")
        return ("whisper", {"to": to, "text": text})
    if cmd in ("vote", "kill", "peek", "guard"):
        return (cmd, {"target": rest.strip()})
    if cmd in ("ready", "who"):
        return (cmd, {})
    return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sock", default="/run/hollow/hollow.sock")
    ap.add_argument("--mod-user", default="hw_mod", help="Linux user the moderator must be running as")
    ap.add_argument("--stay", action="store_true", help="after stdin closes, keep listening (for scripted input)")
    a = ap.parse_args()
    try:
        expect = pwd.getpwnam(a.mod_user).pw_uid
        cli = Client(a.sock, expect_uid=expect)
        await cli.connect()
    except (OSError, ProtocolError, KeyError) as e:
        sys.exit("cannot join: %s" % e)
    state["name"] = getpass.getuser().split("_", 1)[-1]

    loop = asyncio.get_running_loop()
    lines = asyncio.Queue()

    def reader():                                  # blocking stdin lives in its own thread
        try:
            while True:
                s = input(prompt()) if isatty else sys.stdin.readline()
                if s == "":
                    break
                loop.call_soon_threadsafe(lines.put_nowait, s)
        except EOFError:
            pass
        loop.call_soon_threadsafe(lines.put_nowait, None)
    threading.Thread(target=reader, daemon=True).start()

    async def send_loop():
        while True:
            s = await lines.get()
            if s is None:
                if a.stay:
                    return
                await cli.close()
                return
            if s.strip().lower() in ("/quit", "/exit"):
                await cli.close()
                return
            if s.strip().lower() == "/help":
                out(HELP)
                continue
            cmd = parse(s)
            if cmd:
                await cli.send(cmd[0], **cmd[1])
            elif s.strip():
                out("unknown command; try /help")

    sender = asyncio.ensure_future(send_loop())
    out("%sconnected as %s. /help for commands.%s" % (C["dim"], state["name"], C["reset"]))
    while True:
        m = await cli.recv()
        if m is None:
            out("-- the moderator closed the connection --")
            break
        render(m)
        if m.get("type") == "end":
            await asyncio.sleep(0.5)
            break
    sender.cancel()
    os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())
