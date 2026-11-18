"""In-process event bus for the memory-runtime scheduler.

The bus is a WAKE SIGNAL, not a data channel. Subscribers must never rely
on receiving every event, nor on payload content for decisions: the
authoritative state is always re-derived by the consumer's own scan
(DB seq cursors, file glob, recall ledger). A lost event makes a wake
LATE (bounded by the consumer's backstop timeout), never missed.

Leaf module: stdlib imports only, so vault file_ops, proposals, db recall
and bgagent scheduler can all import it without cycles.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable

logger = logging.getLogger("md-vault.events")

__all__ = ["Bus", "set_bus", "get_bus", "publish", "set_filter", "wake_wait"]

_bus: "Bus | None" = None


class Bus:
    """In-process event bus. Construct INSIDE the event loop it will serve."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._t = threading.current_thread()  # publish thread check: is self._t
        self._subs: dict[str, list[asyncio.Queue]] = {}
        self._filters: dict[str, Callable[[dict], bool]] = {}

    def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(topic, []).append(q)
        return q

    def set_filter(self, topic: str, predicate: Callable[[dict], bool] | None) -> None:
        """Publisher-side early drop: predicate(payload) is False → the event
        is dropped before fan-out. None clears. Pure function; evaluated at
        publish time (loop thread)."""
        if predicate is None:
            self._filters.pop(topic, None)
        else:
            self._filters[topic] = predicate

    def publish(self, topic: str, payload: dict) -> None:
        """Fan out to subscribers. Contract: NEVER RAISES.

        Same thread (== the loop thread) → deliver directly; cross-thread →
        call_soon_threadsafe. A closed loop raises RuntimeError inside
        call_soon_threadsafe — wide catch: the event is simply dropped
        (wake hints are loss-tolerant; the consumer's backstop covers).
        """
        try:
            if threading.current_thread() is self._t:
                self._deliver(topic, payload)
            else:
                self._loop.call_soon_threadsafe(self._deliver, topic, payload)
        except Exception:
            logger.debug("event dropped: %s", topic, exc_info=True)

    def _deliver(self, topic: str, payload: dict) -> None:
        # Runs on the loop thread (direct same-thread call, or via
        # call_soon_threadsafe).
        f = self._filters.get(topic)
        if f is not None and not f(payload):
            logger.debug("early drop: %s %r", topic, payload)
            return
        for q in self._subs.get(topic, ()):
            q.put_nowait(payload)


def set_bus(bus: "Bus | None") -> None:
    global _bus
    _bus = bus


def get_bus() -> "Bus | None":
    return _bus


def publish(topic: str, payload: dict) -> None:
    """Module-level publish. No-op when no bus is installed (before
    startup, after shutdown, or in tests that don't want one)."""
    b = _bus
    if b is not None:
        b.publish(topic, payload)


def set_filter(topic: str, predicate: Callable[[dict], bool] | None) -> None:
    """Register a publisher-side filter on the installed bus (no-op if none)."""
    b = _bus
    if b is not None:
        b.set_filter(topic, predicate)


async def wake_wait(queue: asyncio.Queue, timeout: float) -> None:
    """Wait for a queue item OR the backstop timeout. Either way RETURNS —
    the consumer MUST run its authoritative scan on both paths (lost-wake
    keystone: an item consumed in the timeout race is a wake hint only;
    the payload is never needed). CancelledError propagates (never
    swallowed) so outer cancellation exits promptly.
    """
    try:
        await asyncio.wait_for(queue.get(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
