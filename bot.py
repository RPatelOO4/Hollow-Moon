#!/usr/bin/env python3
"""
bot.py -- an automatic Hollow Moon player.  It is just another client built on hollowcomm.Client,
run as its own Linux user, so a lone developer (or a reviewer) can fill a table with opponents.
"""
import argparse
import asyncio
import collections
import getpass
import pwd
import random
import re

from hollowcomm import Client

LINES = ["I'm just a plain villager, honest.", "Something feels off about the quiet ones.",
         "Who has been whispering the most?", "Let's not lynch at random this time.",
         "I'll go with whatever the seer says.", "Keep an eye on who changes their vote."]


class Bot:
    def __init__(self, cli, name, speed):
        self.cli, self.name, self.speed = cli, name, speed
        self.role, self.wolves = None, []
        self.alive, self.phase, self.tasks = [], "lobby", []
        self.known_wolf, self.known_clean = set(), set()
        self.accused = collections.Counter()
        self.last_guard, self.self_guarded = None, False

    async def delay(self, lo, hi):
        await asyncio.sleep(random.uniform(lo, hi) * self.speed)

    def others(self):
        return [n for n in self.alive if n != self.name]

    async def act(self, coro):
        self.tasks = [t for t in self.tasks if not t.done()]
        self.tasks.append(asyncio.ensure_future(coro))

    async def on_phase(self, m):
        self.phase, self.alive = m["phase"], m["alive"]
        for t in self.tasks:
            t.cancel()
        self.tasks = []
        if self.name not in self.alive or not self.role:
            return
        await self.act({"night": self.night, "day_talk": self.talk, "day_vote": self.vote}.get(self.phase, self.noop)())

    async def noop(self):
        pass

    async def night(self):
        if self.role == "wolf":
            await self.delay(1, 3)
            prey = [n for n in self.others() if n not in self.wolves]
            tgt = random.choice(prey)
            await self.cli.send("chat", text="Let's take %s tonight." % tgt)
            await self.delay(1, 2)
            await self.cli.send("kill", target=tgt)
        elif self.role == "seer":
            await self.delay(1, 4)
            pool = [n for n in self.others() if n not in self.known_wolf | self.known_clean]
            if pool:
                await self.cli.send("peek", target=random.choice(pool))
        elif self.role == "healer":
            await self.delay(1, 4)
            pool = [n for n in self.alive if n != self.last_guard and (n != self.name or not self.self_guarded)]
            tgt = random.choice(pool)
            self.last_guard = tgt
            self.self_guarded |= tgt == self.name
            await self.cli.send("guard", target=tgt)

    async def talk(self):
        await self.delay(2, 8)
        if self.role == "seer" and self.known_wolf:
            w = random.choice(sorted(self.known_wolf & set(self.alive)) or [None])
            if w:
                await self.cli.send("chat", text="I am the seer. I peeked %s: wolf! Vote them." % w)
        elif self.role == "wolf":
            tgt = random.choice([n for n in self.others() if n not in self.wolves])
            await self.cli.send("chat", text="I suspect %s, they are too quiet." % tgt)
        else:
            sus = random.choice(self.others())
            await self.cli.send("chat", text=random.choice(LINES) if random.random() < .5 else "I suspect %s." % sus)
        if random.random() < .5 and self.others():
            await self.delay(1, 4)
            await self.cli.send("whisper", to=random.choice(self.others()), text="Between us: who do you trust?")
        await self.delay(2, 6)
        await self.cli.send("ready")

    async def vote(self):
        await self.delay(2, 7)
        live_wolves = [n for n in self.known_wolf if n in self.alive]
        if live_wolves:
            tgt = live_wolves[0]
        else:
            pool = [n for n in self.others() if n not in self.wolves and n not in self.known_clean]
            cand = [(self.accused[n], random.random(), n) for n in pool] or [(0, 0, random.choice(self.others()))]
            tgt = max(cand)[2]
        await self.cli.send("vote", target=tgt)

    def on_say(self, m):
        if m["channel"] != "town":
            return
        for w in re.findall(r"I suspect (\w+)", m["text"]):
            self.accused[w] += 1
        for w in re.findall(r"I peeked (\w+): wolf", m["text"]):   # a seer claim: believe it half the time
            if "I am the seer" in m["text"] and random.random() < .6 and w != self.name:
                self.known_wolf.add(w)

    async def run(self):
        while True:
            m = await self.cli.recv()
            if m is None:
                return
            t = m["type"]
            if t == "role":
                self.role, self.wolves = m["role"], m["wolves"]
            elif t == "phase":
                await self.on_phase(m)
            elif t == "say":
                self.on_say(m)
            elif t == "peek":
                (self.known_wolf if m["is_wolf"] else self.known_clean).add(m["target"])
            elif t == "end":
                return


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sock", default="/run/hollow/hollow.sock")
    ap.add_argument("--mod-user", default="hw_mod")
    ap.add_argument("--speed", type=float, default=1.0, help="scale thinking delays (0.2 = fast)")
    a = ap.parse_args()
    cli = Client(a.sock, expect_uid=pwd.getpwnam(a.mod_user).pw_uid)
    await cli.connect()
    await cli.send("ready")
    await Bot(cli, getpass.getuser().split("_", 1)[-1], a.speed).run()


asyncio.run(main())
