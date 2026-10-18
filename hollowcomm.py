#!/usr/bin/env python3
"""
hollowcomm.py -- the ONLY communications library used by Hollow Moon.

Primitive : AF_UNIX / SOCK_STREAM sockets + asyncio (non-blocking, event-loop I/O)
Framing   : 4-byte big-endian length prefix, followed by a UTF-8 JSON object
Identity  : the *kernel* tells the hub which Linux user is on the other end of
            every connection (SO_PEERCRED).  The "from" field of a message is
            stamped by the hub, never trusted from the sender, so one player
            cannot impersonate another.  The client does the same check in the
            other direction (it verifies the hub runs as the moderator's uid).

Public API
----------
Server side (used by moderator.py):
    hub = Hub(path, allowed={uid: name}, on_connect=, on_message=, on_disconnect=)
    await hub.start() / await hub.stop()
    hub.send(name, msg)                  unicast        (1 -> 1)
    hub.multicast(group, msg)            multicast      (1 -> named subset)
    hub.broadcast(msg, exclude=())       broadcast      (1 -> everyone)
    hub.join(name, group) / hub.leave(name, group) / hub.members(group)
    hub.is_connected(name) / hub.connected()

Client side (used by player.py and bot.py):
    cli = Client(path, expect_uid=None)
    await cli.connect()
    await cli.send(type, **fields)       send one message to the hub
    msg = await cli.recv()               next message, or None when hub closed
    await cli.close()

A message is a plain dict with a "type" key.  The hub adds "from" (the sender's
name) and "seq" (a hub-wide increasing number, so everyone agrees on ordering).
"""
import asyncio
import json
import os
import socket
import struct

MAX_FRAME = 64 * 1024            # refuse absurdly large frames (DoS protection)
MAX_QUEUE = 500                  # per-peer outbound backlog before we cut them off
_HDR = struct.Struct("!I")
_CRED = struct.Struct("3i")      # struct ucred { pid_t pid; uid_t uid; gid_t gid; }


class ProtocolError(Exception):
    pass


# --------------------------------------------------------------------------
# Framing helpers (shared by both sides)
# --------------------------------------------------------------------------
def encode(msg):
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ProtocolError("frame too large")
    return _HDR.pack(len(body)) + body


async def read_frame(reader):
    """Read exactly one framed message.  Raises IncompleteReadError at EOF."""
    (n,) = _HDR.unpack(await reader.readexactly(_HDR.size))
    if n > MAX_FRAME:
        raise ProtocolError("frame too large")
    obj = json.loads((await reader.readexactly(n)).decode("utf-8"))
    if not isinstance(obj, dict):
        raise ProtocolError("message must be an object")
    return obj


def peer_credentials(writer):
    """(pid, uid, gid) of the process at the other end, as reported by the kernel."""
    sock = writer.get_extra_info("socket")
    return _CRED.unpack(sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _CRED.size))


# --------------------------------------------------------------------------
# Server side
# --------------------------------------------------------------------------
class Peer:
    """One connected player, as seen by the hub."""

    def __init__(self, name, uid, pid, writer):
        self.name, self.uid, self.pid, self.writer = name, uid, pid, writer
        self.groups = set()
        self.outq = asyncio.Queue()
        self.alive = True


class Hub:
    def __init__(self, path, allowed, on_connect=None, on_message=None, on_disconnect=None):
        self.path = path
        self.allowed = dict(allowed)             # uid -> player name
        self.on_connect, self.on_message, self.on_disconnect = on_connect, on_message, on_disconnect
        self.peers = {}                          # name -> Peer
        self.seq = 0
        self.server = None

    # ---- lifecycle ----
    async def start(self):
        try:
            os.unlink(self.path)                 # stale socket from a previous run
        except FileNotFoundError:
            pass
        self.server = await asyncio.start_unix_server(self._serve, path=self.path)
        os.chmod(self.path, 0o666)               # anyone may *connect*; SO_PEERCRED decides who may *play*

    async def stop(self):
        for p in list(self.peers.values()):
            self._drop(p)
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    # ---- addressing ----
    def join(self, name, group):
        if name in self.peers:
            self.peers[name].groups.add(group)

    def leave(self, name, group):
        if name in self.peers:
            self.peers[name].groups.discard(group)

    def members(self, group):
        return [p.name for p in self.peers.values() if group in p.groups]

    def is_connected(self, name):
        return name in self.peers

    def connected(self):
        return list(self.peers)

    # ---- delivery ----
    def _stamp(self, msg):
        self.seq += 1
        out = dict(msg)
        out["seq"] = self.seq
        return out

    def _enqueue(self, peer, frame):
        if peer.outq.qsize() >= MAX_QUEUE:       # a stuck reader must not stall the game
            self._drop(peer)
        else:
            peer.outq.put_nowait(frame)

    def send(self, name, msg):                   # unicast
        p = self.peers.get(name)
        if p:
            self._enqueue(p, encode(self._stamp(msg)))

    def multicast(self, group, msg, exclude=()):  # multicast
        frame = encode(self._stamp(msg))         # encoded ONCE, same seq for every receiver
        for p in list(self.peers.values()):
            if group in p.groups and p.name not in exclude:
                self._enqueue(p, frame)

    def broadcast(self, msg, exclude=()):        # broadcast
        frame = encode(self._stamp(msg))
        for p in list(self.peers.values()):
            if p.name not in exclude:
                self._enqueue(p, frame)

    # ---- connection handling (one coroutine pair per client, all concurrent) ----
    async def _serve(self, reader, writer):
        try:
            pid, uid, _gid = peer_credentials(writer)
        except OSError:
            writer.close()
            return
        name = self.allowed.get(uid)
        if name is None or name in self.peers:
            why = "not a registered player" if name is None else "already connected"
            try:
                writer.write(encode({"type": "refused", "text": why}))
                await writer.drain()
            finally:
                writer.close()
            return
        peer = Peer(name, uid, pid, writer)
        self.peers[name] = peer
        sender = asyncio.ensure_future(self._writer_loop(peer))
        try:
            if self.on_connect:
                await self.on_connect(peer)
            while True:
                msg = await read_frame(reader)
                msg["from"] = peer.name          # stamped by the hub: cannot be forged
                msg.pop("seq", None)
                if self.on_message:
                    await self.on_message(peer, msg)
        except (asyncio.IncompleteReadError, ConnectionError, ProtocolError, ValueError):
            pass                                 # client left, or spoke garbage: drop them
        finally:
            sender.cancel()
            self._drop(peer)
            if self.on_disconnect:
                await self.on_disconnect(peer)

    async def _writer_loop(self, peer):
        try:
            while True:
                frame = await peer.outq.get()
                peer.writer.write(frame)
                await peer.writer.drain()        # back-pressure is per player
        except (ConnectionError, asyncio.CancelledError):
            pass

    def _drop(self, peer):
        if self.peers.get(peer.name) is peer:
            del self.peers[peer.name]
        peer.alive = False
        try:
            peer.writer.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Client side
# --------------------------------------------------------------------------
class Client:
    def __init__(self, path, expect_uid=None):
        self.path, self.expect_uid = path, expect_uid
        self.reader = self.writer = None

    async def connect(self):
        self.reader, self.writer = await asyncio.open_unix_connection(self.path)
        if self.expect_uid is not None:          # mutual authentication
            _pid, uid, _gid = peer_credentials(self.writer)
            if uid != self.expect_uid:
                self.writer.close()
                raise ProtocolError("socket is not owned by the moderator (uid %d) -- refusing" % uid)

    async def send(self, type, **fields):
        fields["type"] = type
        self.writer.write(encode(fields))
        await self.writer.drain()

    async def recv(self):
        try:
            return await read_frame(self.reader)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None

    async def close(self):
        if self.writer:
            self.writer.close()
