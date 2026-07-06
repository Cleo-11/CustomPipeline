"""Priya's business tools (agents/priya_tools.py) — successor to the
deleted test_booking.py. Bookings write JSONL off the loop; brochure
payload comes from the agent's tool_config."""
import dataclasses
import json

from agents import priya_tools
from runtime import agent_registry
from runtime.tools import ToolContext, ToolRegistry


def make_ctx(tool_config=None):
    agent = agent_registry.resolve()
    if tool_config is not None:
        agent = dataclasses.replace(agent, tool_config=tool_config)
    return ToolContext(call_id="c1", caller_number="+911234567890",
                       caller_name="Rahul", agent=agent)


def test_register_installs_both_specs_with_markers():
    reg = ToolRegistry()
    priya_tools.register(reg)

    book = reg.get("book_site_visit")
    brochure = reg.get("send_brochure")
    assert book is not None and brochure is not None
    assert book.marker == "BOOK"
    assert brochure.marker == "BROCHURE"
    assert book.owner == "n-rose-developers"
    assert set(book.parameters["required"]) == {"day", "time", "name"}


async def test_book_site_visit_appends_jsonl(tmp_path):
    store = tmp_path / "bookings.jsonl"
    ctx = make_ctx(tool_config={"bookings_path": str(store)})

    await priya_tools.book_site_visit(ctx, {"day": "Sunday", "time": "4pm"})
    await priya_tools.book_site_visit(ctx, {"day": "Monday", "time": "11am"})

    records = [json.loads(line) for line in
               store.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert records[0]["call_id"] == "c1"
    assert records[0]["caller"] == "Rahul"
    assert records[0]["day"] == "Sunday"
    assert "ts" in records[0]


async def test_book_site_visit_raises_on_write_failure(tmp_path):
    # A directory in place of the file: the error must propagate — the
    # ToolExecutor owns the catch and the ToolFailed event.
    ctx = make_ctx(tool_config={"bookings_path": str(tmp_path)})
    try:
        await priya_tools.book_site_visit(ctx, {"day": "Sunday"})
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError")


def test_brochure_payload_comes_from_tool_config():
    payload = priya_tools._brochure_payload("+919999999999", {
        "brochure_url": "https://cdn.example.com/b.pdf",
        "brochure_filename": "NH.pdf",
        "brochure_caption": "Northern Heights",
    })
    assert payload == {
        "to": "+919999999999",
        "type": "document",
        "document": {"link": "https://cdn.example.com/b.pdf",
                     "filename": "NH.pdf", "caption": "Northern Heights"},
    }


def test_priya_record_lists_her_tools():
    agent = agent_registry.resolve()
    assert agent.tools == ("book_site_visit", "send_brochure")
    assert "brochure_url" in agent.tool_config
    assert agent.llm.tool_dispatch == "marker"  # inherited engine default
