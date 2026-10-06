"""In-memory event bus and `emit()`, the only way events enter the system."""

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Self

from konklawe.events import AgentEvent
from konklawe.store import EventStore

SUBSCRIBER_QUEUE_SIZE = 1000


@dataclass(frozen=True)
class EventsDropped:
    """Delivered in place of events a slow subscriber lost; they remain in the journal."""

    count: int


class Subscription:
    """Async iterator over bus events. Registered as soon as it is created.

    A subscriber that falls behind loses its oldest events; the next item it receives is
    then a single `EventsDropped` covering everything lost since its previous read.
    """

    def __init__(self, bus: "EventBus", session_id: str | None, maxsize: int) -> None:
        self._bus = bus
        self.session_id = session_id
        self._maxsize = maxsize
        self._items: deque[AgentEvent] = deque()
        self._dropped = 0
        self._wakeup = asyncio.Event()
        self._closed = False

    def matches(self, event: AgentEvent) -> bool:
        return self.session_id is None or event.session_id == self.session_id

    def offer(self, event: AgentEvent) -> None:
        """Queue `event` without ever blocking the publisher."""
        if self._closed:
            return
        if len(self._items) >= self._maxsize:
            self._items.popleft()
            self._dropped += 1
        self._items.append(event)
        self._wakeup.set()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus._unsubscribe(self)
            self._wakeup.set()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> AgentEvent | EventsDropped:
        while True:
            if self._dropped:
                count, self._dropped = self._dropped, 0
                return EventsDropped(count)
            if self._items:
                return self._items.popleft()
            if self._closed:
                raise StopAsyncIteration
            self._wakeup.clear()
            await self._wakeup.wait()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class EventBus:
    def __init__(self) -> None:
        self._subscriptions: list[Subscription] = []

    def subscribe(
        self, session_id: str | None = None, *, maxsize: int = SUBSCRIBER_QUEUE_SIZE
    ) -> Subscription:
        """Subscribe to all events, or only to those of `session_id`."""
        subscription = Subscription(self, session_id, maxsize)
        self._subscriptions.append(subscription)
        return subscription

    def publish(self, event: AgentEvent) -> None:
        for subscription in list(self._subscriptions):
            if subscription.matches(event):
                subscription.offer(event)

    def close(self) -> None:
        """End every subscription; iterators finish after draining what they hold."""
        for subscription in list(self._subscriptions):
            subscription.close()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def _unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)


async def emit(store: EventStore, bus: EventBus, event: AgentEvent) -> AgentEvent:
    """Journal `event` (assigning its `seq`), then publish it. Returns the stored event."""
    stored = await store.append_event(event)
    bus.publish(stored)
    return stored
