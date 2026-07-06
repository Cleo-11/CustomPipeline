"""trim_history — the proto Context Compiler's eviction rules."""
from runtime.context import trim_history


def _msgs(n_pairs):
    out = [{"role": "system", "content": "SYS"}]
    for i in range(n_pairs):
        out.append({"role": "user", "content": f"u{i}"})
        out.append({"role": "assistant", "content": f"a{i}"})
    return out


def test_within_budget_is_untouched():
    msgs = _msgs(3)
    assert trim_history(msgs, max_messages=24, max_chars=6000) == msgs


def test_message_budget_evicts_oldest_keeps_system():
    msgs = _msgs(10)  # system + 20
    out = trim_history(msgs, max_messages=4, max_chars=6000)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert [m["content"] for m in out[1:]] == ["u8", "a8", "u9", "a9"]


def test_char_budget_evicts_oldest():
    msgs = [{"role": "system", "content": "SYS"},
            {"role": "user", "content": "x" * 100},
            {"role": "assistant", "content": "y" * 100},
            {"role": "user", "content": "z" * 100}]
    out = trim_history(msgs, max_messages=24, max_chars=250)
    # 300 chars of tail > 250: the oldest goes; 200 fits.
    assert [m["content"][0] for m in out] == ["S", "y", "z"]


def test_system_prompt_never_counts_and_never_evicts():
    msgs = [{"role": "system", "content": "S" * 10_000},
            {"role": "user", "content": "hi"}]
    out = trim_history(msgs, max_messages=24, max_chars=100)
    assert out == msgs


def test_empty_and_system_only():
    assert trim_history([], max_messages=4, max_chars=100) == []
    sys_only = [{"role": "system", "content": "SYS"}]
    assert trim_history(sys_only, max_messages=4, max_chars=1) == sys_only
