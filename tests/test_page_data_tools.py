"""Links, tables, embedded metadata and network response bodies."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from zendriver import cdp

from src.errors import ElementNotFoundError, ZendriverMCPError
from src.tools.content import ContentTools
from src.tools.logging import LoggingTools


def _content(stub_mcp: Any, js_result: Any) -> ContentTools:
    tools = ContentTools(stub_mcp)
    tools.session._page = SimpleNamespace(evaluate=AsyncMock(return_value=js_result))  # type: ignore[attr-defined]
    return tools


def _links(n: int) -> list[dict[str, Any]]:
    return [
        {"text": f"Link {i}", "href": f"https://a.example/{i}", "internal": i % 2 == 0}
        for i in range(n)
    ]


async def test_get_links_formats_scope_text_and_href(stub_mcp: Any) -> None:
    links = [
        {"text": "Home", "href": "https://a.example/", "internal": True},
        {"text": "", "href": "https://b.example/x", "internal": False},
    ]
    out = await _content(stub_mcp, links).get_links()
    assert out.splitlines() == [
        "Found 2 link(s):",
        "  [int] Home -> https://a.example/",
        "  [ext] (no text) -> https://b.example/x",
    ]


async def test_get_links_at_the_limit_shows_everything(stub_mcp: Any) -> None:
    out = await _content(stub_mcp, _links(3)).get_links(limit=3)
    assert len(out.splitlines()) == 4  # header + 3 links, no truncation note
    assert "showing" not in out


async def test_get_links_over_the_limit_says_how_many_are_hidden(stub_mcp: Any) -> None:
    out = await _content(stub_mcp, _links(4)).get_links(limit=3)
    assert out.splitlines()[0] == "Found 4 link(s):"
    assert "Link 2" in out and "Link 3" not in out
    assert out.splitlines()[-1] == "  [showing 3 of 4; raise limit or filter for more]"


async def test_get_links_empty_mentions_the_filter(stub_mcp: Any) -> None:
    assert await _content(stub_mcp, []).get_links() == "No links found"
    assert await _content(stub_mcp, []).get_links(filter_text="pdf") == (
        "No links found matching 'pdf'"
    )


async def test_get_links_filter_reaches_the_page_as_a_safe_literal(stub_mcp: Any) -> None:
    tools = _content(stub_mcp, [])
    await tools.get_links(filter_text='"); alert(1); ("')
    script = tools.session.page.evaluate.call_args[0][0]
    assert json.dumps('"); alert(1); ("') in script


_TABLE = {"headers": ["Name", "Qty"], "rows": [["A", "1"], ["B", "2"], ["C", "3"]]}


async def test_extract_table_json_is_compact_and_counts_rows(stub_mcp: Any) -> None:
    out = await _content(stub_mcp, [dict(_TABLE)]).extract_table()
    assert json.loads(out) == [{**_TABLE, "rowCount": 3}]
    assert "\n" not in out


async def test_extract_table_max_rows_keeps_the_real_row_count(stub_mcp: Any) -> None:
    data = json.loads(await _content(stub_mcp, [dict(_TABLE)]).extract_table(max_rows=2))
    assert data[0]["rows"] == [["A", "1"], ["B", "2"]]
    assert data[0]["rowCount"] == 3


async def test_extract_table_markdown_at_the_row_limit_has_no_truncation_note(
    stub_mcp: Any,
) -> None:
    out = await _content(stub_mcp, [dict(_TABLE)]).extract_table(format="markdown", max_rows=3)
    assert out.splitlines() == [
        "| Name | Qty |",
        "|---|---|",
        "| A | 1 |",
        "| B | 2 |",
        "| C | 3 |",
        "",
        "3 row(s)",
    ]


async def test_extract_table_markdown_over_the_row_limit_notes_it(stub_mcp: Any) -> None:
    out = await _content(stub_mcp, [dict(_TABLE)]).extract_table(format="markdown", max_rows=2)
    assert "| C | 3 |" not in out
    assert out.endswith("3 row(s), showing 2; raise max_rows for more")


async def test_extract_table_markdown_escapes_cells_that_would_break_the_grid(
    stub_mcp: Any,
) -> None:
    table = {"headers": [], "rows": [["a|b", "line1\nline2"]]}
    out = await _content(stub_mcp, [table]).extract_table(format="markdown")
    assert out.splitlines()[0] == "| a\\|b | line1 line2 |"


async def test_extract_table_markdown_numbers_multiple_tables(stub_mcp: Any) -> None:
    out = await _content(stub_mcp, [dict(_TABLE), dict(_TABLE)]).extract_table(format="markdown")
    assert "### Table 1" in out and "### Table 2" in out


async def test_extract_table_without_a_match_raises(stub_mcp: Any) -> None:
    with pytest.raises(ElementNotFoundError):
        await _content(stub_mcp, []).extract_table(selector="#nope")


async def test_extract_table_rejects_unknown_format_before_touching_the_page(
    stub_mcp: Any,
) -> None:
    tools = _content(stub_mcp, [dict(_TABLE)])
    with pytest.raises(ZendriverMCPError, match="csv"):
        await tools.extract_table(format="csv")
    tools.session.page.evaluate.assert_not_called()


async def test_extract_structured_data_paginates_compact_json(stub_mcp: Any) -> None:
    data = {"jsonld": [{"name": "x" * 50}], "opengraph": {}, "twitter": {}, "meta": {}}
    full = json.dumps(data, separators=(",", ":"))
    out = await _content(stub_mcp, data).extract_structured_data(max_chars=20)
    assert out == f"[chars 0-20 of {len(full)}] (next: offset=20)\n{full[:20]}"


def _logging(stub_mcp: Any, logs: list[dict[str, Any]], send: AsyncMock) -> LoggingTools:
    tools = LoggingTools(stub_mcp)
    tools.session.get_network_logs = MagicMock(return_value=logs)  # type: ignore[method-assign]
    tools.session._page = SimpleNamespace(send=send)  # type: ignore[attr-defined]
    return tools


def _entry(url: str, request_id: str | None, method: str = "GET") -> dict[str, Any]:
    entry: dict[str, Any] = {"url": url, "method": method, "status": 200, "mime_type": "x/y"}
    if request_id:
        entry["_request_id"] = cdp.network.RequestId(request_id)
    return entry


def _requested_id(send: AsyncMock) -> str:
    """The requestId of the Network.getResponseBody command handed to ``page.send``."""
    return next(send.call_args[0][0])["params"]["requestId"]


async def test_response_body_returns_the_latest_matching_request(stub_mcp: Any) -> None:
    send = AsyncMock(return_value=('{"ok":true}', False))
    logs = [_entry("https://a.example/API/items", "1"), _entry("https://a.example/api/items", "2")]
    out = await _logging(stub_mcp, logs, send).get_network_response_body("/api/")

    assert _requested_id(send) == "2"
    assert out == 'GET https://a.example/api/items 200\n[chars 0-11 of 11]\n{"ok":true}'


async def test_response_body_method_filter_is_case_insensitive(stub_mcp: Any) -> None:
    send = AsyncMock(return_value=("posted", False))
    logs = [_entry("https://a.example/api", "1", "POST"), _entry("https://a.example/api", "2")]
    await _logging(stub_mcp, logs, send).get_network_response_body("api", method="post")
    assert _requested_id(send) == "1"


async def test_response_body_skips_entries_without_a_request_id(stub_mcp: Any) -> None:
    send = AsyncMock(return_value=("body", False))
    logs = [_entry("https://a.example/api", "1"), _entry("https://a.example/api", None)]
    await _logging(stub_mcp, logs, send).get_network_response_body("api")
    assert _requested_id(send) == "1"


async def test_response_body_decodes_base64_text(stub_mcp: Any) -> None:
    encoded = base64.b64encode("héllo".encode()).decode()
    send = AsyncMock(return_value=(encoded, True))
    out = await _logging(
        stub_mcp, [_entry("https://a.example/t", "1")], send
    ).get_network_response_body("/t")
    assert out.endswith("\nhéllo")


async def test_response_body_reports_binary_instead_of_dumping_it(stub_mcp: Any) -> None:
    raw = bytes([0xFF, 0xD8, 0xFF, 0x00, 0x80])
    send = AsyncMock(return_value=(base64.b64encode(raw).decode(), True))
    out = await _logging(
        stub_mcp, [_entry("https://a.example/i.jpg", "1")], send
    ).get_network_response_body("i.jpg")
    assert "[binary body, 5 bytes, x/y; use download_file to save it]" in out


async def test_response_body_paginates(stub_mcp: Any) -> None:
    send = AsyncMock(return_value=("0123456789", False))
    out = await _logging(
        stub_mcp, [_entry("https://a.example/t", "1")], send
    ).get_network_response_body("/t", max_chars=4, offset=4)
    assert out.endswith("[chars 4-8 of 10] (next: offset=8)\n4567")


async def test_response_body_evicted_by_chrome_is_an_error_line_not_a_crash(
    stub_mcp: Any,
) -> None:
    send = AsyncMock(side_effect=RuntimeError("No resource with given identifier found"))
    out = await _logging(
        stub_mcp, [_entry("https://a.example/t", "1")], send
    ).get_network_response_body("/t")
    assert out == (
        "GET https://a.example/t 200\n"
        "Error: body unavailable (No resource with given identifier found)"
    )


async def test_response_body_without_a_match_never_calls_cdp(stub_mcp: Any) -> None:
    send = AsyncMock()
    out = await _logging(
        stub_mcp, [_entry("https://a.example/t", "1")], send
    ).get_network_response_body("/other")
    assert out == "No captured request matching '/other'"
    send.assert_not_called()
