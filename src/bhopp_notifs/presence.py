"""Shows the seller as online on BHOPP, as an open bhopp.com tab does.

socket.io on the API host, namespace /user, auth {token}, emitting presence:ping every 60 s.
Connects while the setting is on and the token is valid, reconnects by itself, never raises.
"""
from __future__ import annotations

import asyncio
import logging

from .bhopp import BASE
from .text import redact

log = logging.getLogger(__name__)
NAMESPACE = "/user"
PING_EVERY = 60
RETRY_AFTER = 30


def _socketio_client():
    import socketio

    return socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)


class Presence:
    def __init__(self, token_fn, wanted_fn, client_factory=None, base: str = BASE):
        self.token_fn, self.wanted_fn = token_fn, wanted_fn
        self.client_factory = client_factory or _socketio_client
        self.base = base
        self.sio = None
        self.pings = 0
        self.last_error = ""

    @property
    def connected(self) -> bool:
        return self.sio is not None and bool(self.sio.connected)

    async def _close(self) -> None:
        if self.sio is not None:
            try:
                await self.sio.disconnect()
            except Exception:  # noqa: BLE001 - it is being dropped either way
                pass
        self.sio = None

    async def tick(self) -> None:
        try:
            wanted = bool(self.wanted_fn())
        except Exception:  # noqa: BLE001
            wanted = False
        token = self.token_fn() if wanted else ""
        if not token:
            await self._close()
            return
        if self.sio is not None and not self.sio.connected:
            await self._close()
        if self.sio is None:
            sio = self.client_factory()
            try:
                await sio.connect(self.base, namespaces=[NAMESPACE], auth={"token": token},
                                  transports=["websocket", "polling"], wait_timeout=15)
            except Exception as e:  # noqa: BLE001
                self.last_error = redact(f"{type(e).__name__}: {e}")[:200]
                log.warning("presence: could not connect (%s)", self.last_error)
                return
            self.sio = sio
            log.info("presence: connected, showing online")
        try:
            await self.sio.emit("presence:ping", namespace=NAMESPACE)
            self.pings += 1
            self.last_error = ""
        except Exception as e:  # noqa: BLE001
            self.last_error = redact(f"{type(e).__name__}: {e}")[:200]
            await self._close()

    async def run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(PING_EVERY if self.sio is not None else RETRY_AFTER)
