"""TurnEngine unit tests: every turn-taking rule as a named, sockets-free
test, plus the replay harness asserting full state traces (redesign §5).

D1/D3/D6 regression coverage lives here at the rule level; D4 (history
truncation) is session wiring and is covered in test_session_scripted.py.
"""
from runtime.endpointing import FixedSilenceEndpointer, ProviderEndpointer
from runtime.turn_engine import (
    ArmEndpointTimer,
    CancelOutput,
    CommitUserTurn,
    PlayGreeting,
    TurnEngine,
    TurnPolicy,
    TurnState,
)

POLICY = TurnPolicy(bargein_min_frames=3, partial_interrupt_after_s=0.5, filler="")


def make_engine(policy=POLICY, endpointer=None):
    return TurnEngine(policy, endpointer or FixedSilenceEndpointer(delay_s=0.55))


def finish_greeting(e):
    """Drive the greeting to completion: sending done, then playback done."""
    e.speaking_finished(0, any_audio=True)
    return e.playback_finished()


# ------------------------------------------------------------------ greeting
def test_call_started_plays_uninterruptible_greeting():
    e = make_engine()
    assert e.call_started() == [PlayGreeting()]
    assert e.state is TurnState.AGENT_SPEAKING
    assert e.greeting_active


def test_greeting_ends_only_after_playback_finished():
    e = make_engine()
    e.call_started()
    e.speaking_finished(0, any_audio=True)
    assert e.state is TurnState.AGENT_SPEAKING  # draining
    e.playback_finished()
    assert e.state is TurnState.LISTENING
    assert not e.greeting_active


def test_d3_greeting_immune_to_bargein_frames():
    e = make_engine()
    e.call_started()
    for _ in range(20):  # far past bargein_min_frames
        assert e.media_frame(is_speech=True) == []
    assert e.state is TurnState.AGENT_SPEAKING


def test_d3_greeting_immune_to_partial_interrupt():
    e = make_engine()
    e.call_started()
    # Well past the 0.5 s window — still no interrupt during the greeting.
    assert e.stt_partial(now=99.0) == []
    assert e.state is TurnState.AGENT_SPEAKING


def test_d3_commit_due_during_greeting_is_deferred_then_flushes():
    e = make_engine()
    e.call_started()
    [arm] = e.stt_final("hello")
    assert e.endpoint_fired(arm.generation) == []  # held: greeting still playing
    assert e.state is TurnState.AGENT_SPEAKING

    e.speaking_finished(0, any_audio=True)
    intents = e.playback_finished()
    assert intents == [CommitUserTurn(text="hello", turn_seq=1, play_filler=False)]
    assert e.state is TurnState.THINKING


# ------------------------------------------------------------- normal turns
def test_final_arms_endpoint_timer_with_endpointer_delay():
    e = make_engine(endpointer=FixedSilenceEndpointer(delay_s=0.55))
    e.call_started()
    finish_greeting(e)
    [arm] = e.stt_final("namaste")
    assert isinstance(arm, ArmEndpointTimer)
    assert arm.delay_s == 0.55
    assert e.state is TurnState.USER_SPEAKING


def test_endpoint_commits_accumulated_finals():
    e = make_engine()
    e.call_started()
    finish_greeting(e)
    e.stt_final("mujhe 2BHK")
    [arm] = e.stt_final("chahiye")
    intents = e.endpoint_fired(arm.generation)
    assert intents == [
        CommitUserTurn(text="mujhe 2BHK chahiye", turn_seq=1, play_filler=False)]
    assert e.state is TurnState.THINKING


def test_stale_timer_generation_is_a_noop():
    e = make_engine()
    e.call_started()
    finish_greeting(e)
    [arm1] = e.stt_final("first")
    [arm2] = e.stt_final("second")
    assert e.endpoint_fired(arm1.generation) == []  # superseded
    assert e.state is TurnState.USER_SPEAKING
    assert e.endpoint_fired(arm2.generation) != []


def test_empty_pending_never_commits():
    e = make_engine()
    e.call_started()
    finish_greeting(e)
    assert e.endpoint_fired(e._endpoint_generation) == []
    assert e.state is TurnState.LISTENING


def test_partial_moves_listening_to_user_speaking():
    e = make_engine()
    e.call_started()
    finish_greeting(e)
    e.stt_partial(now=1.0)
    assert e.state is TurnState.USER_SPEAKING


def test_filler_flag_follows_policy():
    e = make_engine(policy=TurnPolicy(filler="हम्म"))
    e.call_started()
    finish_greeting(e)
    [arm] = e.stt_final("hello")
    [commit] = e.endpoint_fired(arm.generation)
    assert commit.play_filler is True


# --------------------------------------------------------- agent speaking
def start_reply_turn(e, now=10.0):
    """Commit turn 1 and start speaking it."""
    [arm] = e.stt_final("bolo")
    e.endpoint_fired(arm.generation)
    e.speaking_started(1, now=now)
    return e


def test_d6_playback_finished_mid_sending_is_ignored():
    e = make_engine()
    e.call_started()
    finish_greeting(e)
    start_reply_turn(e)
    # Buffer drained between clauses while the pipeline is still sending.
    assert e.playback_finished() == []
    assert e.state is TurnState.AGENT_SPEAKING
    # Once sending completes, the next playback-finished ends the turn.
    e.speaking_finished(1)
    e.playback_finished()
    assert e.state is TurnState.LISTENING


def test_d6_bargein_stays_armed_in_draining_tail():
    e = make_engine()
    e.call_started()
    finish_greeting(e)
    start_reply_turn(e)
    e.speaking_finished(1)  # sent everything; carrier still playing tail
    intents = []
    for _ in range(3):
        intents += e.media_frame(is_speech=True)
    assert intents == [CancelOutput(turn_seq=1)]
    assert e.state is TurnState.LISTENING


def test_bargein_needs_sustained_speech():
    e = make_engine()
    e.call_started()
    finish_greeting(e)
    start_reply_turn(e)
    assert e.media_frame(is_speech=True) == []
    assert e.media_frame(is_speech=True) == []
    assert e.media_frame(is_speech=False) == []  # cough gap resets the run
    assert e.media_frame(is_speech=True) == []
    assert e.media_frame(is_speech=True) == []
    assert e.state is TurnState.AGENT_SPEAKING
    [cancel] = e.media_frame(is_speech=True)
    assert cancel == CancelOutput(turn_seq=1)


def test_partial_interrupts_only_after_grace_window():
    e = make_engine()
    e.call_started()
    finish_greeting(e)
    start_reply_turn(e, now=10.0)
    assert e.stt_partial(now=10.3) == []  # inside 0.5 s grace: likely echo
    [cancel] = e.stt_partial(now=10.6)
    assert cancel == CancelOutput(turn_seq=1)
    assert e.state is TurnState.LISTENING


def test_d1_new_commit_cancels_inflight_output_first():
    e = make_engine()
    e.call_started()
    finish_greeting(e)
    start_reply_turn(e)
    # Caller spoke quietly through the reply; endpoint fires mid-speech.
    [arm] = e.stt_final("ek minute")
    intents = e.endpoint_fired(arm.generation)
    assert intents == [
        CancelOutput(turn_seq=1),
        CommitUserTurn(text="ek minute", turn_seq=2, play_filler=False),
    ]
    assert e.is_stale(1)
    assert e.state is TurnState.THINKING


def test_speaking_finished_without_audio_closes_turn_immediately():
    # TTS produced nothing: no playedStream will ever arrive, so the engine
    # must not wait for one.
    e = make_engine()
    e.call_started()
    e.speaking_finished(0, any_audio=False)
    assert e.state is TurnState.LISTENING
    assert not e.greeting_active


def test_stale_speaking_notifications_are_ignored():
    e = make_engine()
    e.call_started()
    finish_greeting(e)
    start_reply_turn(e)
    [arm] = e.stt_final("naya sawaal")
    e.endpoint_fired(arm.generation)  # turn 2 committed, turn 1 stale
    assert e.speaking_started(1, now=99.0) == []
    assert e.speaking_finished(1) == []
    assert e.state is TurnState.THINKING


# ----------------------------------------------------------- endpointers
def test_provider_endpoint_commits_immediately():
    e = make_engine(endpointer=ProviderEndpointer(fallback_delay_s=0.55))
    e.call_started()
    finish_greeting(e)
    [arm] = e.stt_final("hello")
    intents = e.stt_endpoint()
    assert intents == [CommitUserTurn(text="hello", turn_seq=1, play_filler=False)]
    # The pending silence timer was invalidated by the provider signal.
    assert e.endpoint_fired(arm.generation) == []


def test_fixed_endpointer_ignores_provider_signal():
    e = make_engine(endpointer=FixedSilenceEndpointer(delay_s=0.55))
    e.call_started()
    finish_greeting(e)
    [arm] = e.stt_final("hello")
    assert e.stt_endpoint() == []
    assert e.state is TurnState.USER_SPEAKING
    # The silence timer still commits as usual.
    assert e.endpoint_fired(arm.generation) != []


# ---------------------------------------------------------------- replay
def test_replay_full_call_state_trace():
    """The replay harness: drive a whole scripted call through the engine
    and assert the exact state trace — greeting, one turn, a barge-in."""
    e = make_engine()

    e.call_started()                          # greeting
    e.speaking_finished(0)
    e.playback_finished()

    [arm] = e.stt_final("2BHK ka price?")     # user turn 1
    e.endpoint_fired(arm.generation)
    e.speaking_started(1, now=5.0)            # reply plays
    e.speaking_finished(1)
    e.playback_finished()

    e.stt_partial(now=8.0)                    # user starts turn 2
    [arm] = e.stt_final("aur amenities?")
    e.endpoint_fired(arm.generation)
    e.speaking_started(2, now=9.0)
    for _ in range(3):                        # sustained speech: barge-in
        e.media_frame(is_speech=True)

    assert e.trace == [
        TurnState.AGENT_SPEAKING,   # greeting
        TurnState.LISTENING,
        TurnState.USER_SPEAKING,    # turn 1
        TurnState.THINKING,
        TurnState.AGENT_SPEAKING,
        TurnState.LISTENING,
        TurnState.USER_SPEAKING,    # turn 2
        TurnState.THINKING,
        TurnState.AGENT_SPEAKING,
        TurnState.INTERRUPTED,      # barge-in
        TurnState.LISTENING,
    ]
    assert e.turn_seq == 2
