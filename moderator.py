#!/usr/bin/env python3
"""
moderator.py -- the Hollow Moon game server.  Runs as its own Linux user (hw_mod).

It owns all game state.  Players only ever see what the moderator chooses to
send them over their own socket, so secrets (roles, peek results, wolf chat)
never touch the filesystem.

Message types accepted from players
    chat{text}  whisper{to,text}  ready  vote{target|"none"}
    kill{target}  peek{target}  guard{target}  who
Message types sent to players
    say{channel,from,text}  phase{phase,day,seconds,alive,dead}  role{role,wolves}
    peek{target,is_wolf}    end{winner,roles}
"""
import argparse
import asyncio
import collections
import pwd
import random
import re
import sys
import time

from hollowcomm import Hub

SOCK = "/run/hollow/hollow.sock"


class Player:
    def __init__(self, name, uid):
        self.name, self.uid = name, uid
        self.role = None
        self.alive = True
        self.ready = False
        self.whispers = 0
        self.vote = None
        self.guarded_last = None      # healer only: who was guarded last night
        self.self_guard_used = False  # healer only: self-protection is a one-shot


def clean(text, limit=300):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(text))[:limit].strip()


class Game:
    def __init__(self, a):
        self.a = a
        self.players = {}
        self.phase, self.day = "lobby", 0
        self.done = asyncio.Event()
        self.hub = None
        self.night_votes, self.peeked, self.guard = {}, set(), None
        self.logf = open(a.log, "a", buffering=1) if a.log else None
        self.finished = asyncio.Event()

    # ------------------------------------------------------------ helpers
    def log(self, text):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), text)
        print(line, flush=True)
        if self.logf:
            self.logf.write(line + "\n")

    def say(self, text, channel="mod", to=None, frm="MODERATOR"):
        m = {"type": "say", "channel": channel, "from": frm, "text": text}
        self.hub.send(to, m) if to else self.hub.broadcast(m)

    def alive(self, role=None):
        return [p for p in self.players.values() if p.alive and (role is None or p.role == role)]

    def names(self, ps):
        return [p.name for p in ps]

    def find(self, name):
        name = clean(name or "").lower()
        for p in self.players.values():
            if p.name.lower() == name:
                return p
        return None

    def connected_alive(self):
        return [p for p in self.alive() if self.hub.is_connected(p.name)]

    async def push_phase(self, seconds, to=None):
        m = {"type": "phase", "phase": self.phase, "day": self.day, "seconds": int(seconds),
             "alive": self.names(self.alive()),
             "dead": [p.name for p in self.players.values() if not p.alive]}
        self.hub.send(to, m) if to else self.hub.broadcast(m)

    async def run_phase(self, phase, seconds):
        """Enter a phase and sleep until it ends: either the clock runs out or
        a handler sets self.done (everyone who needs to act has acted)."""
        self.phase = phase
        self.done = asyncio.Event()
        self.log("--- %s %d (%ds) ---" % (phase, self.day, seconds))
        await self.push_phase(seconds)
        warn = seconds - 10
        try:
            if warn > 5:
                await asyncio.wait_for(self.done.wait(), warn)
            else:
                await asyncio.wait_for(self.done.wait(), seconds)
            await asyncio.sleep(1)
            return
        except asyncio.TimeoutError:
            pass
        if warn > 5:
            self.say("10 seconds left.")
            try:
                await asyncio.wait_for(self.done.wait(), 10)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------ hub callbacks
    async def on_connect(self, peer):
        name = peer.name
        if name not in self.players:
            if self.phase != "lobby":
                self.say("The game has already begun; you may only watch.", to=name)
                self.hub.join(name, "dead")
                self.players[name] = Player(name, peer.uid)
                self.players[name].alive = False
                self.players[name].role = "spectator"
                return
            self.players[name] = Player(name, peer.uid)
            self.log("%s connected (uid %d, pid %d)" % (name, peer.uid, peer.pid))
            self.say("%s has entered the Hollow (%d here)." % (name, len(self.hub.connected())))
            self.say("Welcome, %s. Type /help for commands, /ready when you are." % name, to=name)
        else:                                   # reconnect mid-game
            p = self.players[name]
            self.log("%s reconnected" % name)
            if not p.alive:
                self.hub.join(name, "dead")
            if p.role == "wolf":
                self.hub.join(name, "wolves")
            if p.role:
                self.hub.send(name, {"type": "role", "role": p.role,
                                     "wolves": self.names(self.alive("wolf")) if p.role == "wolf" else []})
            await self.push_phase(self.a.talk, to=name)
            self.say("%s is back." % name)

    async def on_disconnect(self, peer):
        self.log("%s disconnected" % peer.name)
        p = self.players.get(peer.name)
        if self.phase == "lobby":
            self.players.pop(peer.name, None)
            self.say("%s left." % peer.name)
        else:
            self.say("%s has gone quiet (disconnected)." % peer.name)
            self.check_done()

    async def on_message(self, peer, msg):
        p = self.players.get(peer.name)
        t = msg.get("type")
        handler = {"chat": self.h_chat, "whisper": self.h_whisper, "ready": self.h_ready,
                   "vote": self.h_vote, "kill": self.h_kill, "peek": self.h_peek,
                   "guard": self.h_guard, "who": self.h_who}.get(t)
        if p and handler:
            await handler(p, msg)
        else:
            self.say("Unknown command.", to=peer.name)

    # ------------------------------------------------------------ handlers
    async def h_who(self, p, m):
        a, d = self.names(self.alive()), [x.name for x in self.players.values() if not x.alive]
        self.say("Alive: %s | Dead: %s" % (", ".join(a) or "-", ", ".join(d) or "-"), to=p.name)

    async def h_chat(self, p, m):
        text = clean(m.get("text"))
        if not text:
            return
        if self.phase in ("lobby", "over"):
            self.say(text, "town", frm=p.name)
        elif not p.alive:
            self.hub.multicast("dead", {"type": "say", "channel": "ghost", "from": p.name, "text": text})
        elif self.phase == "night":
            if p.role == "wolf":                # multicast: only the pack hears this
                self.hub.multicast("wolves", {"type": "say", "channel": "wolves", "from": p.name, "text": text})
                self.log("(wolves) %s: %s" % (p.name, text))
            else:
                self.say("Everyone is asleep. Only the wolves stir at night.", to=p.name)
        else:
            self.say(text, "town", frm=p.name)  # broadcast to the whole village
            self.log("%s: %s" % (p.name, text))

    async def h_whisper(self, p, m):
        t = self.find(m.get("to"))
        text = clean(m.get("text"))
        if self.phase not in ("day_talk", "day_vote") or not p.alive:
            return self.say("You can only whisper by day while alive.", to=p.name)
        if not t or not t.alive or t is p:
            return self.say("Whisper to whom? Pick another living player.", to=p.name)
        if p.whispers <= 0:
            return self.say("You have no whispers left today.", to=p.name)
        if not text:
            return
        p.whispers -= 1
        self.hub.send(t.name, {"type": "say", "channel": "whisper", "from": p.name, "text": text})   # unicast
        self.say("(to %s, %d whisper(s) left)" % (t.name, p.whispers), "whisper", to=p.name, frm="you")
        self.say("%s whispers to someone..." % p.name)       # everyone learns THAT you whispered, not to whom
        self.log("whisper %s -> %s: %s" % (p.name, t.name, text))

    async def h_ready(self, p, m):
        p.ready = not p.ready
        if self.phase == "lobby":
            self.say("%s is %s. (%d/%d ready)" % (p.name, "ready" if p.ready else "not ready",
                     sum(x.ready for x in self.players.values()), len(self.players)))
            self.check_lobby()
        elif self.phase == "day_talk" and p.alive:
            self.say("%s is ready to vote. (%d/%d)" % (p.name, sum(x.ready for x in self.alive()), len(self.connected_alive())))
            self.check_done()

    async def h_kill(self, p, m):
        t = self.find(m.get("target"))
        if self.phase != "night" or not p.alive or p.role != "wolf":
            return self.say("You can't do that now.", to=p.name)
        if not t or not t.alive or t.role == "wolf":
            return self.say("Choose a living non-wolf.", to=p.name)
        self.night_votes[p.name] = t.name
        self.hub.multicast("wolves", {"type": "say", "channel": "wolves", "from": "MODERATOR",
                                      "text": "%s votes to hunt %s." % (p.name, t.name)})
        self.log("(wolves) %s votes %s" % (p.name, t.name))
        self.check_done()

    async def h_peek(self, p, m):
        t = self.find(m.get("target"))
        if self.phase != "night" or not p.alive or p.role != "seer":
            return self.say("You can't do that now.", to=p.name)
        if p.name in self.peeked:
            return self.say("You already looked tonight.", to=p.name)
        if not t or not t.alive or t is p:
            return self.say("Choose another living player.", to=p.name)
        self.peeked.add(p.name)
        self.hub.send(p.name, {"type": "peek", "target": t.name, "is_wolf": t.role == "wolf"})
        self.log("seer %s peeks %s (%s)" % (p.name, t.name, t.role))
        self.check_done()

    async def h_guard(self, p, m):
        t = self.find(m.get("target"))
        if self.phase != "night" or not p.alive or p.role != "healer":
            return self.say("You can't do that now.", to=p.name)
        if not t or not t.alive:
            return self.say("Choose a living player.", to=p.name)
        if t.name == p.guarded_last:
            return self.say("You guarded %s last night; the charm needs a night to recover." % t.name, to=p.name)
        if t is p and p.self_guard_used:
            return self.say("You have already used your self-guard.", to=p.name)
        self.guard = t.name
        self.say("You will guard %s tonight." % t.name, to=p.name)
        self.log("healer %s guards %s" % (p.name, t.name))
        self.check_done()

    async def h_vote(self, p, m):
        raw = clean(m.get("target") or "none")
        t = self.find(raw)
        if self.phase != "day_vote" or not p.alive:
            return self.say("There is no vote right now.", to=p.name)
        if raw.lower() not in ("none", "abstain") and (not t or not t.alive):
            return self.say("Vote for a living player, or 'none'.", to=p.name)
        p.vote = t.name if t else "none"
        self.say("%s votes: %s" % (p.name, p.vote))          # open ballot, so bandwagons are possible
        self.log("vote %s -> %s" % (p.name, p.vote))
        self.check_done()

    # ------------------------------------------------------------ early-finish checks
    def check_lobby(self):
        ps = list(self.players.values())
        if len(ps) >= self.a.min_players and (all(x.ready for x in ps) or len(ps) >= self.a.players):
            self.done.set()

    def check_done(self):
        if self.phase == "night":
            wolves = [p for p in self.connected_alive() if p.role == "wolf"]
            seer = [p for p in self.connected_alive() if p.role == "seer"]
            heal = [p for p in self.connected_alive() if p.role == "healer"]
            if (all(w.name in self.night_votes for w in wolves) and
                    all(s.name in self.peeked for s in seer) and (not heal or self.guard)):
                self.done.set()
        elif self.phase == "day_talk":
            if all(p.ready for p in self.connected_alive()):
                self.done.set()
        elif self.phase == "day_vote":
            if all(p.vote for p in self.connected_alive()):
                self.done.set()

    # ------------------------------------------------------------ game flow
    async def lobby(self):
        self.log("Lobby open. Waiting for %d-%d players." % (self.a.min_players, self.a.players))
        while True:
            self.phase = "lobby"
            self.done = asyncio.Event()
            await self.done.wait()
            await asyncio.sleep(1)
            if len(self.players) >= self.a.min_players:
                return

    def deal(self):
        ps = list(self.players.values())
        random.Random(self.a.seed).shuffle(ps) if self.a.seed is not None else random.shuffle(ps)
        n = len(ps)
        nw = 1 if n < 7 else 2 if n < 10 else 3
        roles = ["wolf"] * nw + ["seer", "healer"] + ["villager"] * (n - nw - 2)
        for p, r in zip(ps, roles):
            p.role = r
        wolves = self.names(self.alive("wolf"))
        for p in ps:
            if p.role == "wolf":
                self.hub.join(p.name, "wolves")
            self.hub.send(p.name, {"type": "role", "role": p.role,
                                   "wolves": wolves if p.role == "wolf" else []})
        self.log("roles: " + ", ".join("%s=%s" % (p.name, p.role) for p in ps))

    def winner(self):
        w, v = len(self.alive("wolf")), len(self.alive()) - len(self.alive("wolf"))
        if w == 0:
            return "village"
        if w >= v:
            return "wolves"
        return None

    def kill(self, p, cause):
        p.alive = False
        self.hub.join(p.name, "dead")
        self.hub.leave(p.name, "wolves")
        self.say("%s %s. They were a %s." % (p.name, cause, p.role.upper()))
        self.log("%s died (%s, %s)" % (p.name, cause, p.role))

    async def night(self):
        self.night_votes, self.peeked, self.guard = {}, set(), None
        for p in self.alive():
            p.vote = None
        self.say("Night %d falls. Wolves: hunt with /kill NAME. Seer: /peek NAME. Healer: /guard NAME." % self.day)
        await self.run_phase("night", self.a.night)
        tally = collections.Counter(self.night_votes.values()).most_common()
        victim = None
        if tally and (len(tally) == 1 or tally[0][1] > tally[1][1]):
            victim = self.players[tally[0][0]]
        for h in self.alive("healer"):
            h.guarded_last = self.guard
            if self.guard == h.name:
                h.self_guard_used = True
        self.phase = "dawn"
        self.say("Dawn breaks over the Hollow.")
        if victim and victim.name != self.guard:
            self.kill(victim, "was torn apart in the night")
        else:
            self.say("Nobody died in the night.")      # was it a tie? a wolf who dithered? a lucky guard? nobody knows
            self.log("night result: no death (victim=%s guard=%s)" % (victim and victim.name, self.guard))

    async def day_phase(self):
        for p in self.alive():
            p.whispers, p.ready, p.vote = self.a.whispers, False, None
        self.say("Day %d. Talk freely. You each have %d whispers. /ready when you want to move to the vote." % (self.day, self.a.whispers))
        await self.run_phase("day_talk", self.a.talk)
        self.say("Time to vote. /vote NAME (or /vote none). Ballots are open and may be changed.")
        await self.run_phase("day_vote", self.a.vote)
        tally = collections.Counter(p.vote for p in self.alive() if p.vote and p.vote != "none").most_common()
        self.say("Votes: " + (", ".join("%s=%d" % kv for kv in tally) or "none cast"))
        if tally and (len(tally) == 1 or tally[0][1] > tally[1][1]):
            self.kill(self.players[tally[0][0]], "was lynched by the village")
        else:
            self.say("The village could not agree. Nobody is lynched.")

    async def run(self):
        await self.lobby()
        self.deal()
        self.say("The game begins with %d players. Check your role card." % len(self.players))
        await asyncio.sleep(2)
        while True:
            self.day += 1
            await self.night()
            if self.winner():
                break
            await self.day_phase()
            if self.winner():
                break
        w = self.winner()
        self.phase = "over"
        roles = {p.name: p.role for p in self.players.values()}
        self.say("GAME OVER -- the %s win!" % w.upper())
        self.hub.broadcast({"type": "end", "winner": w, "roles": roles})
        self.log("winner: %s | %s" % (w, roles))
        await asyncio.sleep(3)
        self.finished.set()


async def amain(a):
    allowed = {}
    for e in pwd.getpwall():
        if e.pw_name.startswith(a.prefix) and e.pw_name != a.prefix + "mod":
            allowed[e.pw_uid] = e.pw_name[len(a.prefix):]
    if not allowed:
        sys.exit("no users named %s* found -- run setup_users.sh first" % a.prefix)
    g = Game(a)
    # the hub is the library; the moderator only supplies callbacks
    g.hub = Hub(a.sock, allowed, g.on_connect, g.on_message, g.on_disconnect)
    await g.hub.start()
    g.log("moderator listening on %s for %d registered players: %s" % (a.sock, len(allowed), ", ".join(sorted(allowed.values()))))
    await g.run()
    await g.finished.wait()
    await g.hub.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Hollow Moon moderator")
    ap.add_argument("--sock", default=SOCK)
    ap.add_argument("--prefix", default="hw_", help="Linux user prefix that identifies players")
    ap.add_argument("--players", type=int, default=7, help="start immediately once this many have joined")
    ap.add_argument("--min-players", type=int, default=5)
    ap.add_argument("--night", type=int, default=45)
    ap.add_argument("--talk", type=int, default=120)
    ap.add_argument("--vote", type=int, default=40)
    ap.add_argument("--whispers", type=int, default=2)
    ap.add_argument("--seed", type=int, default=None, help="fixed role deal (for demos/tests)")
    ap.add_argument("--log", default=None, help="moderator log (contains secrets; keep it 0600)")
    asyncio.run(amain(ap.parse_args()))
