"""AgentConfig + registry tests: resolution, default fallback, and the
inherit-from-engine-defaults merge that lets an agent file stay sparse."""
import config
from runtime import agent_registry
from runtime.agent import AgentConfig
from runtime.turn_engine import TurnPolicy


def test_resolve_default_returns_priya():
    agent = agent_registry.resolve()
    assert agent.agent_id == "priya"
    assert agent.tenant_id == "n-rose-developers"
    assert "Priya" in agent.system_prompt
    assert agent.greeting  # non-empty
    # Markers are still the booking mechanism until M7.
    assert "[[BOOK" in agent.system_prompt


def test_unknown_agent_falls_back_to_default(caplog):
    agent = agent_registry.resolve(agent_id="does-not-exist")
    assert agent.agent_id == "priya"


def test_explicit_agent_id_resolves():
    assert agent_registry.resolve(agent_id="priya").agent_id == "priya"


def test_pinned_voice_wins_over_config_default(monkeypatch):
    # Priya pins her voice in agents/priya.json; a deployment default must
    # not override it.
    monkeypatch.setattr(config, "TTS_SPEAKER", "some-other-voice")
    assert agent_registry.resolve().voice.speaker != "some-other-voice"


def test_omitted_policy_inherits_live_config_default(monkeypatch):
    # priya.json omits the llm section, so it inherits config — and because
    # defaults are read live, an env/monkeypatch override flows through.
    monkeypatch.setattr(config, "LLM_MODEL", "patched-model-xyz")
    assert agent_registry.resolve().llm.model == "patched-model-xyz"


def test_turn_settings_inherit_config(monkeypatch):
    monkeypatch.setattr(config, "ENDPOINT_SILENCE_MS", 999)
    monkeypatch.setattr(config, "BARGEIN_MIN_FRAMES", 7)
    turn = agent_registry.resolve().turn
    assert turn.endpoint_silence_ms == 999
    assert turn.bargein_min_frames == 7


def test_from_dict_fills_every_section_from_defaults():
    defaults = {
        "voice": {"model": "m", "speaker": "s", "language": "l", "pace": 1.0},
        "stt": {"model": "nova-2", "language": "hi", "endpointer": "fixed"},
        "llm": {"base_url": "u", "model": "gpt", "temperature": 0.5,
                "max_tokens": 128, "tool_dispatch": "marker"},
        "turn": {
            "endpoint_silence_ms": 550, "bargein_rms_threshold": 650.0,
            "bargein_min_frames": 25, "vad_aggressiveness": 2,
            "partial_interrupt_after_s": 0.5, "filler": "हम्म",
        },
    }
    # A spec that pins nothing but the required persona fields.
    spec = {"agent_id": "sparse", "system_prompt": "p", "greeting": "g"}
    agent = AgentConfig.from_dict(spec, defaults)

    assert agent.tenant_id == "default"          # defaulted
    assert agent.knowledge == ""                 # defaulted
    assert agent.voice.speaker == "s"            # inherited from defaults
    assert agent.llm.max_tokens == 128
    assert agent.llm.tool_dispatch == "marker"
    assert agent.turn.filler == "हम्म"
    assert agent.tools == ()                     # toolless by default
    assert agent.tool_config == {}


def test_from_dict_spec_overrides_defaults():
    defaults = {
        "voice": {"model": "m", "speaker": "default-voice", "language": "l", "pace": 1.0},
        "stt": {"model": "nova-2", "language": "hi", "endpointer": "fixed"},
        "llm": {"base_url": "u", "model": "gpt", "temperature": 0.5,
                "max_tokens": 128, "tool_dispatch": "marker"},
        "turn": {
            "endpoint_silence_ms": 550, "bargein_rms_threshold": 650.0,
            "bargein_min_frames": 25, "vad_aggressiveness": 2,
            "partial_interrupt_after_s": 0.5, "filler": "",
        },
    }
    spec = {
        "agent_id": "custom", "system_prompt": "p", "greeting": "g",
        "voice": {"speaker": "pinned-voice"},  # partial section: merges over defaults
        "llm": {"tool_dispatch": "native"},    # per-agent strategy choice
        "tools": ["book_site_visit"],
    }
    agent = AgentConfig.from_dict(spec, defaults)
    assert agent.voice.speaker == "pinned-voice"
    assert agent.voice.model == "m"  # the unpinned field still inherits
    assert agent.llm.tool_dispatch == "native"
    assert agent.tools == ("book_site_visit",)


def test_engine_policy_projection():
    turn = agent_registry.resolve().turn
    policy = turn.engine_policy()
    assert isinstance(policy, TurnPolicy)
    assert policy.bargein_min_frames == turn.bargein_min_frames
    assert policy.partial_interrupt_after_s == turn.partial_interrupt_after_s
    assert policy.filler == turn.filler
