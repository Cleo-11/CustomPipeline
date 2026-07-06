"""CircuitBreaker + ResilientTTS/ResilientLLM failure-injection tests."""
import asyncio

import pytest

from runtime.resilience import CircuitBreaker, ResilientLLM, ResilientTTS
from runtime.types import MULAW_8K, AudioFrame, LLMDelta


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# ------------------------------------------------------------------ breaker
def test_breaker_opens_after_threshold_and_cools_down():
    clock = Clock()
    b = CircuitBreaker(failure_threshold=3, cooldown_s=30, clock=clock)
    assert b.allow()

    b.record_failure()
    b.record_failure()
    assert b.allow()          # below threshold
    b.record_failure()
    assert not b.allow()      # open
    assert b.is_open

    clock.now = 29.9
    assert not b.allow()
    clock.now = 30.0
    assert b.allow()          # half-open: one probe allowed
    b.record_failure()        # probe failed → re-open
    assert not b.allow()

    clock.now = 61.0
    assert b.allow()
    b.record_success()        # probe succeeded → closed, counters reset
    assert b.allow() and not b.is_open
    b.record_failure()
    b.record_failure()
    assert b.allow()          # count restarted from zero


# ---------------------------------------------------------------------- TTS
FRAME = AudioFrame(payload=b"\xff" * 160, format=MULAW_8K)


class FlakyTTS:
    supports_streaming_input = False

    def __init__(self, failures, *, frames=3, fail_after=0):
        self.failures = failures        # attempts that raise
        self.fail_after = fail_after    # frames yielded before raising
        self.frames = frames
        self.calls = 0

    async def synthesize(self, text, fmt):
        call = self.calls
        self.calls += 1
        if call in self.failures:
            for _ in range(self.fail_after):
                yield FRAME
            raise RuntimeError("tts boom")
        for _ in range(self.frames):
            yield FRAME


async def test_tts_retries_once_when_nothing_was_yielded():
    inner = FlakyTTS(failures={0})
    tts = ResilientTTS(inner)
    out = [f async for f in tts.synthesize("नमस्ते", MULAW_8K)]
    assert len(out) == 3
    assert inner.calls == 2


async def test_tts_two_failures_yield_nothing_and_count_on_breaker():
    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=30)
    inner = FlakyTTS(failures={0, 1})
    tts = ResilientTTS(inner, breaker=breaker)
    out = [f async for f in tts.synthesize("x", MULAW_8K)]
    assert out == []
    assert inner.calls == 2
    assert breaker.is_open  # both attempts recorded


async def test_tts_midstream_failure_never_retries():
    # 2 frames went out, then the stream died: a retry would replay audio.
    inner = FlakyTTS(failures={0}, fail_after=2)
    out = [f async for f in ResilientTTS(inner).synthesize("x", MULAW_8K)]
    assert len(out) == 2
    assert inner.calls == 1


async def test_tts_open_breaker_skips_provider_entirely():
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=1000)
    breaker.record_failure()
    inner = FlakyTTS(failures=set())
    out = [f async for f in ResilientTTS(inner, breaker=breaker)
           .synthesize("x", MULAW_8K)]
    assert out == []
    assert inner.calls == 0


async def test_tts_attempt_timeout_is_a_failure():
    class HangingTTS:
        supports_streaming_input = False

        async def synthesize(self, text, fmt):
            await asyncio.sleep(60)
            yield FRAME

    tts = ResilientTTS(HangingTTS(), attempt_timeout_s=0.01)
    out = [f async for f in tts.synthesize("x", MULAW_8K)]
    assert out == []


async def test_tts_success_passes_through_and_closes_breaker():
    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=30)
    breaker.record_failure()
    out = [f async for f in ResilientTTS(FlakyTTS(failures=set()),
                                         breaker=breaker)
           .synthesize("x", MULAW_8K)]
    assert len(out) == 3
    breaker.record_failure()
    assert not breaker.is_open  # success had reset the count


# ---------------------------------------------------------------------- LLM
class ScriptedLLM:
    def __init__(self, deltas, *, explode_at=None, hang_first=False):
        self._deltas = deltas
        self._explode_at = explode_at
        self._hang_first = hang_first

    async def stream(self, messages, tools=None):
        if self._hang_first:
            await asyncio.sleep(60)
        for i, d in enumerate(self._deltas):
            if i == self._explode_at:
                raise RuntimeError("llm boom")
            yield d


async def test_llm_passes_deltas_through():
    llm = ResilientLLM(ScriptedLLM([LLMDelta(text="a"), LLMDelta(text="b")]))
    out = [d.text async for d in llm.stream([])]
    assert out == ["a", "b"]


async def test_llm_first_token_timeout_raises_and_trips_breaker():
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=1000)
    llm = ResilientLLM(ScriptedLLM([LLMDelta(text="a")], hang_first=True),
                       breaker=breaker, first_token_timeout_s=0.01)
    with pytest.raises(TimeoutError):
        [d async for d in llm.stream([])]
    assert breaker.is_open


async def test_llm_midstream_error_propagates_after_partial_yield():
    # clauses.py downstream speaks what it has; the wrapper must not eat
    # the text that already arrived, nor the exception.
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=1000)
    llm = ResilientLLM(
        ScriptedLLM([LLMDelta(text="कुछ"), LLMDelta(text="और")], explode_at=1),
        breaker=breaker)
    got = []
    with pytest.raises(RuntimeError):
        async for d in llm.stream([]):
            got.append(d.text)
    assert got == ["कुछ"]
    assert breaker.is_open


async def test_llm_open_breaker_yields_nothing():
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=1000)
    breaker.record_failure()
    llm = ResilientLLM(ScriptedLLM([LLMDelta(text="a")]), breaker=breaker)
    assert [d async for d in llm.stream([])] == []
