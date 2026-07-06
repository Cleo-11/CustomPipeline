"""EventBus contract tests: ordered delivery, non-blocking emit, subscriber
crash isolation, loop re-binding, NullBus."""
import asyncio

from runtime import events
from runtime.events import NULL_BUS, EventBus


class Recorder:
    def __init__(self):
        self.seen = []

    async def __call__(self, event):
        self.seen.append(event)


def _ev(n: int) -> events.ThinkingStarted:
    return events.ThinkingStarted(call_id="c1", turn_seq=n)


async def test_delivers_in_order_to_all_subscribers():
    bus = EventBus()
    a, b = Recorder(), Recorder()
    bus.subscribe(a)
    bus.subscribe(b)

    for n in range(3):
        bus.emit(_ev(n))
    await bus.flush()

    assert [e.turn_seq for e in a.seen] == [0, 1, 2]
    assert a.seen == b.seen
    bus.close()


async def test_emit_is_non_blocking():
    """emit() must return before any subscriber runs — the hot-path rule."""
    bus = EventBus()
    gate = asyncio.Event()
    rec = Recorder()

    async def slow(event):
        await gate.wait()
        rec.seen.append(event)

    bus.subscribe(slow)
    bus.emit(_ev(1))  # returns immediately even though `slow` is blocked
    assert rec.seen == []
    gate.set()
    await bus.flush()
    assert len(rec.seen) == 1
    bus.close()


async def test_subscriber_exception_is_isolated():
    bus = EventBus()
    rec = Recorder()

    async def crashing(event):
        raise RuntimeError("boom")

    bus.subscribe(crashing)  # registered first: crashes before rec runs
    bus.subscribe(rec)

    bus.emit(_ev(1))
    bus.emit(_ev(2))
    await bus.flush()

    assert [e.turn_seq for e in rec.seen] == [1, 2]
    bus.close()


def test_bus_rebinds_across_event_loops():
    """A process-wide bus must survive each test/process creating its own
    loop: the drain task re-binds on first emit in a new loop."""
    bus = EventBus()
    rec = Recorder()
    bus.subscribe(rec)

    async def use():
        bus.emit(_ev(0))
        await bus.flush()

    asyncio.run(use())
    asyncio.run(use())
    assert len(rec.seen) == 2
    bus.close()


def test_emit_without_running_loop_drops_silently():
    EventBus().emit(_ev(0))  # must not raise


async def test_null_bus_drops():
    NULL_BUS.emit(_ev(0))  # no queue, no task, no error
