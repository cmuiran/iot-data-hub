"""The hub's event loop survives a socket that a library closed while it was still registered
(Windows: one such socket makes select() fail for all of them, which used to stop every driver)."""

from __future__ import annotations

import asyncio
import logging
import socket
import sys

import pytest

from hub.runtime import new_event_loop

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows select() behaviour")


def _closed_behind_the_loops_back(loop: asyncio.AbstractEventLoop) -> socket.socket:
    a, b = socket.socketpair()
    loop.add_reader(a.fileno(), lambda: None)
    a.close()                      # what paho does after a refused TLS handshake
    return b


async def _tick() -> str:
    await asyncio.sleep(0.05)
    return "alive"


@pytest.mark.filterwarnings("ignore:coroutine '_tick' was never awaited")
def test_plain_selector_loop_dies() -> None:
    loop = asyncio.SelectorEventLoop()
    try:
        other = _closed_behind_the_loops_back(loop)
        with pytest.raises(OSError):
            loop.run_until_complete(_tick())
        other.close()
    finally:
        loop.close()


def test_hub_loop_drops_the_dead_socket_and_keeps_running(
        caplog: pytest.LogCaptureFixture) -> None:
    loop = new_event_loop()
    try:
        live_a, live_b = socket.socketpair()
        got: list[bytes] = []
        loop.add_reader(live_a.fileno(), lambda: got.append(live_a.recv(10)))
        other = _closed_behind_the_loops_back(loop)
        live_b.send(b"ping")
        with caplog.at_level(logging.WARNING, logger="hub.runtime"):
            assert loop.run_until_complete(_tick()) == "alive"
        assert got == [b"ping"]                          # the other sockets still work
        assert "closed while registered" in caplog.text
        loop.remove_reader(live_a.fileno())
        for s in (live_a, live_b, other):
            s.close()
    finally:
        loop.close()
