import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from konklawe.bus import EventBus, EventsDropped, Subscription, emit
from konklawe.events import AgentEvent, ParsedEvent
from konklawe.store import EventStore


def event(text: str, session_id: str = "s1") -> AgentEvent:
    return AgentEvent.from_parsed(
        ParsedEvent.text_delta(text), session_id=session_id, turn_id=None, agent="a", provider="p"
    )


async def drain(subscription: Subscription) -> list[AgentEvent | EventsDropped]:
    subscription.close()
    return [item async for item in subscription]


def texts(items: list[AgentEvent | EventsDropped]) -> list[str]:
    return [i.data["text"] if isinstance(i, AgentEvent) else f"dropped:{i.count}" for i in items]


async def test_every_subscriber_gets_every_event() -> None:
    bus = EventBus()
    first, second = bus.subscribe(), bus.subscribe()

    for text in ("a", "b", "c"):
        bus.publish(event(text))

    assert texts(await drain(first)) == ["a", "b", "c"]
    assert texts(await drain(second)) == ["a", "b", "c"]


async def test_session_filter() -> None:
    bus = EventBus()
    only_s2 = bus.subscribe("s2")
    everything = bus.subscribe()

    bus.publish(event("one", "s1"))
    bus.publish(event("two", "s2"))

    assert texts(await drain(only_s2)) == ["two"]
    assert texts(await drain(everything)) == ["one", "two"]


async def test_subscriber_waits_for_events() -> None:
    bus = EventBus()
    subscription = bus.subscribe()

    async def publish_later() -> None:
        await asyncio.sleep(0.01)
        bus.publish(event("late"))

    task = asyncio.create_task(publish_later())
    received = await asyncio.wait_for(anext(subscription), timeout=1)
    await task

    assert isinstance(received, AgentEvent) and received.data["text"] == "late"


async def test_overflow_drops_oldest_and_never_blocks_publisher() -> None:
    bus = EventBus()
    slow = bus.subscribe(maxsize=10)

    # Synchronous loop: publish() returning at all proves it never waits for the subscriber.
    for i in range(25):
        bus.publish(event(str(i)))

    assert texts(await drain(slow)) == ["dropped:15"] + [str(i) for i in range(15, 25)]


async def test_one_warning_per_overflow_episode() -> None:
    bus = EventBus()
    slow = bus.subscribe(maxsize=2)

    for i in range(4):
        bus.publish(event(str(i)))
    assert texts([await anext(slow), await anext(slow)]) == ["dropped:2", "2"]

    for i in range(4, 7):
        bus.publish(event(str(i)))

    assert texts(await drain(slow)) == ["dropped:2", "5", "6"]


async def test_close_ends_iteration_and_unsubscribes() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    assert bus.subscriber_count == 1

    waiter = asyncio.create_task(anext(subscription, None))
    await asyncio.sleep(0)
    bus.close()

    assert await asyncio.wait_for(waiter, timeout=1) is None
    assert bus.subscriber_count == 0
    bus.publish(event("ignored"))


async def test_context_manager_unsubscribes() -> None:
    bus = EventBus()
    with bus.subscribe() as subscription:
        bus.publish(event("x"))
        assert texts([await anext(subscription)]) == ["x"]

    assert bus.subscriber_count == 0


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[EventStore]:
    async with await EventStore.open(tmp_path / "k.db") as opened:
        yield opened


async def test_emit_journals_before_publishing(store: EventStore) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    session = await store.create_session("a", "p", Path("/w"))

    returned = await emit(store, bus, event("hello", session.id))

    [published] = await drain(subscription)
    stored = [e async for e in store.iter_events()]
    assert isinstance(published, AgentEvent)
    assert published.seq is not None
    assert published == returned == stored[0]
