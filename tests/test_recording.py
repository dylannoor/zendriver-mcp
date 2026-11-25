"""Action recording at the tool boundary, and replay through the tool registry."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from src.errors import ZendriverMCPError
from src.tools.base import ToolBase
from src.tools.recording import RecordingTools


class _PageTools(ToolBase):
    """Stand-ins that carry real tool names so the recording filter applies."""

    def _register_tools(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._register(self.click)
        self._register(self.type_text)
        self._register(self.get_page_info)

    async def click(self, selector: str) -> str:
        self.calls.append(("click", selector))
        if selector == "#hidden":
            return f"Error: Element '{selector}' is hidden"
        if selector == "#gone":
            raise ZendriverMCPError(f"Element not found: {selector}")
        return f"Clicked: {selector}"

    async def type_text(self, text: str, selector: str) -> str:
        await self.click(selector)  # same nesting as the real type_text
        self.calls.append(("type_text", text))
        return f"Typed {len(text)} char(s) into {selector}"

    async def get_page_info(self) -> str:
        return "a page"


@pytest.fixture
def tools(stub_mcp: Any) -> Iterator[tuple[_PageTools, RecordingTools]]:
    saved = dict(ToolBase._tools)
    try:
        yield _PageTools(stub_mcp), RecordingTools(stub_mcp)
    finally:
        ToolBase._tools.clear()
        ToolBase._tools.update(saved)


def _recorded(stop_output: str) -> list[dict[str, Any]]:
    return json.loads(stop_output.split("\n", 1)[1])


async def test_records_action_tools_with_their_arguments(tools: Any) -> None:
    _, rec = tools
    await ToolBase._tools["start_recording"]()
    await ToolBase._tools["click"](selector="#buy")
    await ToolBase._tools["get_page_info"]()  # a read, not an action
    out = await ToolBase._tools["stop_recording"]()

    assert out.startswith("Recorded 1 action(s):")
    assert _recorded(out) == [{"action": "click", "params": {"selector": "#buy"}}]


async def test_nested_tool_call_is_recorded_once(tools: Any) -> None:
    await ToolBase._tools["start_recording"]()
    await ToolBase._tools["type_text"](text="hi", selector="#name")
    out = await ToolBase._tools["stop_recording"]()

    # type_text clicks internally; replaying that click as its own step
    # would double-fire handlers on toggles and date pickers.
    assert [a["action"] for a in _recorded(out)] == ["type_text"]


async def test_nothing_is_recorded_outside_a_recording(tools: Any) -> None:
    await ToolBase._tools["click"](selector="#before")
    await ToolBase._tools["start_recording"]()
    await ToolBase._tools["stop_recording"]()
    await ToolBase._tools["click"](selector="#after")

    assert await ToolBase._tools["stop_recording"]() == "No actions were recorded."


async def test_start_recording_drops_the_previous_capture(tools: Any) -> None:
    await ToolBase._tools["start_recording"]()
    await ToolBase._tools["click"](selector="#old")
    await ToolBase._tools["start_recording"]()
    await ToolBase._tools["click"](selector="#new")

    recorded = _recorded(await ToolBase._tools["stop_recording"]())
    assert recorded == [{"action": "click", "params": {"selector": "#new"}}]


async def test_replay_runs_every_step_in_order(tools: Any) -> None:
    page, rec = tools
    sequence = json.dumps(
        [
            {"action": "click", "params": {"selector": "#a"}},
            {"action": "type_text", "params": {"text": "hi", "selector": "#b"}},
        ]
    )
    out = await rec.replay_action_sequence(sequence)

    assert page.calls == [("click", "#a"), ("click", "#b"), ("type_text", "hi")]
    assert out.splitlines() == [
        "Replayed 2 action(s):",
        "  1. click: Clicked: #a",
        "  2. type_text: Typed 2 char(s) into #b",
    ]


async def test_replay_accepts_a_step_without_params(tools: Any) -> None:
    _, rec = tools
    out = await rec.replay_action_sequence('[{"action": "get_page_info"}]')
    assert out.endswith("1. get_page_info: a page")


@pytest.mark.parametrize("failing", ["#hidden", "#gone"])
async def test_replay_stops_at_the_first_failing_step(tools: Any, failing: str) -> None:
    """Both failure shapes end the replay: an ``Error:`` result and a raised error."""
    page, rec = tools
    sequence = json.dumps(
        [
            {"action": "click", "params": {"selector": "#a"}},
            {"action": "click", "params": {"selector": failing}},
            {"action": "click", "params": {"selector": "#never"}},
        ]
    )
    out = await rec.replay_action_sequence(sequence)

    assert out.startswith("Replay stopped at step 2 of 3:")
    assert ("click", "#never") not in page.calls


async def test_replay_rejects_unknown_action_before_running_anything(tools: Any) -> None:
    page, rec = tools
    sequence = json.dumps(
        [
            {"action": "click", "params": {"selector": "#a"}},
            {"action": "format_disk", "params": {}},
        ]
    )
    with pytest.raises(ZendriverMCPError, match="Unknown action.*format_disk"):
        await rec.replay_action_sequence(sequence)
    assert page.calls == []


async def test_replay_cannot_call_itself(tools: Any) -> None:
    _, rec = tools
    nested = json.dumps([{"action": "replay_action_sequence", "params": {"sequence": "[]"}}])
    with pytest.raises(ZendriverMCPError, match="Unknown action"):
        await rec.replay_action_sequence(nested)


@pytest.mark.parametrize(
    "sequence",
    [
        "[{",  # not JSON
        '{"action": "click"}',  # object instead of list
        '["click"]',  # entry is not an object
        '[{"params": {}}]',  # no action
        '[{"action": "click", "params": "#a"}]',  # params is not an object
    ],
)
async def test_replay_rejects_malformed_sequences(tools: Any, sequence: str) -> None:
    page, rec = tools
    with pytest.raises(ZendriverMCPError):
        await rec.replay_action_sequence(sequence)
    assert page.calls == []


async def test_empty_sequence_replays_nothing(tools: Any) -> None:
    page, rec = tools
    assert await rec.replay_action_sequence("[]") == "Replayed 0 action(s):\n"
    assert page.calls == []
