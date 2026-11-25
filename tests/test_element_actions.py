"""Trusted clicks, hide/remove/highlight, element screenshots, text waits, downloads."""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image as PILImage

from src.errors import ElementNotFoundError, ZendriverMCPError
from src.session import BrowserSession
from src.tools.elements import ElementTools
from src.tools.utils import MAX_SCREENSHOT_WIDTH, UtilityTools


def _element_tools(stub_mcp: Any, **page_attrs: Any) -> ElementTools:
    tools = ElementTools(stub_mcp)
    tools.session._page = SimpleNamespace(**page_attrs)  # type: ignore[attr-defined]
    return tools


def _clickable(position: Any) -> MagicMock:
    elem = MagicMock()
    elem.scroll_into_view = AsyncMock()
    elem.get_position = AsyncMock(return_value=position)
    elem.mouse_click = AsyncMock()
    return elem


async def test_click_by_selector_fires_a_cdp_click_at_the_element_centre(stub_mcp: Any) -> None:
    elem = _clickable(SimpleNamespace(center=(120.5, 48.0)))
    page_click = AsyncMock()
    tools = _element_tools(
        stub_mcp,
        evaluate=AsyncMock(return_value={"found": True, "hidden": False}),
        select=AsyncMock(return_value=elem),
        mouse_click=page_click,
    )
    assert await tools.click("#buy") == "Clicked: #buy"

    elem.scroll_into_view.assert_awaited_once()
    page_click.assert_awaited_once_with(120.5, 48.0)
    # zendriver's Element.mouse_click also flashes a red dot <div> into the page.
    elem.mouse_click.assert_not_called()


async def test_click_without_a_position_reports_an_error_instead_of_fake_success(
    stub_mcp: Any,
) -> None:
    page_click = AsyncMock()
    tools = _element_tools(
        stub_mcp,
        evaluate=AsyncMock(return_value={"found": True, "hidden": False}),
        select=AsyncMock(return_value=_clickable(None)),
        mouse_click=page_click,
    )
    out = await tools.click("#clipped")
    assert out.startswith("Error:")
    page_click.assert_not_called()


async def test_click_by_text_clicks_the_coords_the_page_resolved(stub_mcp: Any) -> None:
    page_click = AsyncMock()
    found = {"ok": True, "x": 10, "y": 20, "tag": "BUTTON", "shadow": True}
    tools = _element_tools(stub_mcp, evaluate=AsyncMock(return_value=found), mouse_click=page_click)
    assert await tools.click(text="Verstuur") == "Clicked: Verstuur (shadow) (<BUTTON>)"
    page_click.assert_awaited_once_with(10, 20)


async def test_click_by_text_without_a_match_raises_and_does_not_click(stub_mcp: Any) -> None:
    page_click = AsyncMock()
    tools = _element_tools(
        stub_mcp,
        evaluate=AsyncMock(return_value={"ok": False, "reason": "no_match"}),
        mouse_click=page_click,
    )
    with pytest.raises(ElementNotFoundError):
        await tools.click(text="Nope")
    page_click.assert_not_called()


@pytest.mark.parametrize(
    ("tool", "verb", "statement"),
    [
        ("hide_element", "Hidden", 'el.style.display = "none"'),
        ("remove_element", "Removed", "el.remove()"),
    ],
)
async def test_hide_and_remove_report_the_match_count(
    stub_mcp: Any, tool: str, verb: str, statement: str
) -> None:
    tools = _element_tools(stub_mcp, evaluate=AsyncMock(return_value=2))
    assert await getattr(tools, tool)(".ad") == f"{verb} 2 element(s): .ad"
    assert statement in tools.session.page.evaluate.call_args[0][0]


@pytest.mark.parametrize("tool", ["hide_element", "remove_element", "highlight_element"])
async def test_element_actions_raise_when_nothing_matches(stub_mcp: Any, tool: str) -> None:
    # 0 matches for hide/remove, null for highlight
    result = None if tool == "highlight_element" else 0
    tools = _element_tools(stub_mcp, evaluate=AsyncMock(return_value=result))
    with pytest.raises(ElementNotFoundError):
        await getattr(tools, tool)("#nope")


async def test_highlight_resolves_numeric_ids_and_reports_the_box(stub_mcp: Any) -> None:
    tools = _element_tools(
        stub_mcp, evaluate=AsyncMock(return_value={"tag": "BUTTON", "box": [8, 20, 86, 21]})
    )
    assert await tools.highlight_element("12") == "Highlighted <BUTTON> at 8,20 (86x21)"
    assert '[data-zendriver-id=\\"12\\"]' in tools.session.page.evaluate.call_args[0][0]


def _png_b64(width: int, height: int) -> str:
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), color=(10, 120, 200)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _utility_tools(stub_mcp: Any, **page_attrs: Any) -> UtilityTools:
    tools = UtilityTools(stub_mcp)
    tools.session._page = SimpleNamespace(**page_attrs)  # type: ignore[attr-defined]
    return tools


async def test_screenshot_element_clips_at_document_coords_and_downscales(stub_mcp: Any) -> None:
    send = AsyncMock(return_value=_png_b64(2048, 100))
    tools = _utility_tools(
        stub_mcp, evaluate=AsyncMock(return_value=[0, 3000.5, 2048, 100]), send=send
    )
    result = await tools.screenshot_element("#wide")

    command = next(send.call_args[0][0])
    assert command["method"] == "Page.captureScreenshot"
    assert command["params"]["clip"] == {
        "x": 0,
        "y": 3000.5,
        "width": 2048,
        "height": 100,
        "scale": 1,
    }
    assert command["params"]["captureBeyondViewport"] is True
    with PILImage.open(io.BytesIO(result.data)) as img:
        assert img.format == "JPEG"
        assert img.size == (MAX_SCREENSHOT_WIDTH, 50)


@pytest.mark.parametrize(("name", "fmt"), [("shot.png", "PNG"), ("shot.jpg", "JPEG")])
async def test_screenshot_element_saves_full_resolution_in_the_requested_format(
    stub_mcp: Any, tmp_path: Any, name: str, fmt: str
) -> None:
    tools = _utility_tools(
        stub_mcp,
        evaluate=AsyncMock(return_value=[0, 0, 2048, 100]),
        send=AsyncMock(return_value=_png_b64(2048, 100)),
    )
    await tools.screenshot_element("#wide", save_path=str(tmp_path / name))
    with PILImage.open(tmp_path / name) as saved:
        assert saved.format == fmt
        assert saved.width == 2048


async def test_screenshot_element_missing_element_raises(stub_mcp: Any) -> None:
    send = AsyncMock()
    tools = _utility_tools(stub_mcp, evaluate=AsyncMock(return_value=None), send=send)
    with pytest.raises(ElementNotFoundError):
        await tools.screenshot_element("#nope")
    send.assert_not_called()


@pytest.mark.parametrize("rect", [[0, 0, 0, 20], [0, 0, 20, 0]])
async def test_screenshot_element_without_size_raises(stub_mcp: Any, rect: list[int]) -> None:
    send = AsyncMock()
    tools = _utility_tools(stub_mcp, evaluate=AsyncMock(return_value=rect), send=send)
    with pytest.raises(ZendriverMCPError, match="no size"):
        await tools.screenshot_element("#collapsed")
    send.assert_not_called()


async def test_screenshot_element_refuses_paths_outside_the_sandbox(stub_mcp: Any) -> None:
    tools = _utility_tools(
        stub_mcp,
        evaluate=AsyncMock(return_value=[0, 0, 10, 10]),
        send=AsyncMock(return_value=_png_b64(10, 10)),
    )
    with pytest.raises(ZendriverMCPError, match="outside the allowed roots"):
        await tools.screenshot_element("#x", save_path="/etc/zendriver-test.png")


async def test_wait_for_text_returns_once_the_text_shows_up(stub_mcp: Any) -> None:
    evaluate = AsyncMock(side_effect=[False, RuntimeError("navigating"), True])
    tools = _utility_tools(stub_mcp, evaluate=evaluate, wait=AsyncMock())
    assert await tools.wait_for_text("Welkom", timeout=5) == "Text found: 'Welkom'"
    assert evaluate.await_count == 3
    # compared lowercased in the page, so it matches like click(text=...)
    assert '"welkom"' in evaluate.call_args[0][0]


async def test_wait_for_text_times_out(stub_mcp: Any) -> None:
    tools = _utility_tools(stub_mcp, evaluate=AsyncMock(return_value=False), wait=AsyncMock())
    out = await tools.wait_for_text("Nope", timeout=0.05)
    assert out == "Timeout: Text 'Nope' not found after 0.05s"


async def test_download_file_writes_the_fetched_bytes(stub_mcp: Any, tmp_path: Any) -> None:
    payload = bytes(range(256))
    evaluate = AsyncMock(
        return_value={"data": base64.b64encode(payload).decode(), "type": "application/pdf"}
    )
    tools = _utility_tools(stub_mcp, evaluate=evaluate)
    target = tmp_path / "nested" / "file.pdf"
    out = await tools.download_file("https://a.example/f.pdf", save_path=str(target))

    assert target.read_bytes() == payload
    assert out == f"Downloaded 256 bytes (application/pdf) to {target.resolve()}"
    # without await_promise the async fetch comes back as an unresolved promise
    assert evaluate.call_args.kwargs == {"await_promise": True}


async def test_download_file_without_save_path_uses_a_temp_file(stub_mcp: Any) -> None:
    evaluate = AsyncMock(return_value={"data": base64.b64encode(b"hi").decode(), "type": "t"})
    out = await _utility_tools(stub_mcp, evaluate=evaluate).download_file("https://a.example/f")
    path = out.rsplit(" to ", 1)[1]
    try:
        with open(path, "rb") as f:
            assert f.read() == b"hi"
    finally:
        import os

        os.remove(path)


@pytest.mark.parametrize(
    ("js_result", "reason"),
    [({"error": "HTTP 404 Not Found"}, "HTTP 404 Not Found"), (None, "no response")],
)
async def test_download_file_failure_raises_and_writes_nothing(
    stub_mcp: Any, tmp_path: Any, js_result: Any, reason: str
) -> None:
    tools = _utility_tools(stub_mcp, evaluate=AsyncMock(return_value=js_result))
    target = tmp_path / "file.bin"
    with pytest.raises(ZendriverMCPError, match=reason):
        await tools.download_file("https://a.example/missing", save_path=str(target))
    assert not target.exists()


async def test_download_file_refuses_paths_outside_the_sandbox(stub_mcp: Any) -> None:
    evaluate = AsyncMock(return_value={"data": base64.b64encode(b"x").decode(), "type": "t"})
    tools = _utility_tools(stub_mcp, evaluate=evaluate)
    with pytest.raises(ZendriverMCPError, match="outside the allowed roots"):
        await tools.download_file("https://a.example/f", save_path="/etc/zendriver-test.bin")


async def test_first_navigation_attaches_listeners_before_loading_the_url(
    fresh_session: BrowserSession,
) -> None:
    """Otherwise the page's own requests never reach the network log."""
    events: list[str] = []
    tab = MagicMock()
    tab.send = AsyncMock(side_effect=lambda _cmd: events.append("cdp enable"))
    tab.get = AsyncMock(side_effect=lambda url: events.append(f"load {url}"))

    async def open_tab(url: str, new_tab: bool = False) -> MagicMock:
        events.append(f"open {url}")
        return tab

    fresh_session._browser = MagicMock(get=open_tab)
    await fresh_session.navigate("https://a.example/")

    assert events == ["open about:blank", "cdp enable", "cdp enable", "load https://a.example/"]


async def test_new_tab_attaches_listeners_before_loading_the_url(
    fresh_session: BrowserSession,
) -> None:
    events: list[str] = []
    tab = MagicMock()
    tab.send = AsyncMock(side_effect=lambda _cmd: events.append("cdp enable"))
    tab.get = AsyncMock(side_effect=lambda url: events.append(f"load {url}"))

    async def open_tab(url: str, new_tab: bool = False) -> MagicMock:
        events.append(f"open {url} new_tab={new_tab}")
        return tab

    fresh_session._browser = MagicMock(get=open_tab)
    tab_id, created = await fresh_session.create_tab("https://a.example/")

    assert created is tab and tab_id == "tab_1"
    assert events == [
        "open about:blank new_tab=True",
        "cdp enable",
        "cdp enable",
        "load https://a.example/",
    ]
