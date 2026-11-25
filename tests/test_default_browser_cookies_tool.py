"""use_default_browser_cookies: nothing is read before the user says yes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import browser_cookies
from src.errors import BrowserNotStartedError, ZendriverMCPError
from src.tools import mcp
from src.tools.cookies import ALLOW_BROWSER_COOKIES_ENV, CookieTools

ZEN = next(b for b in browser_cookies.BROWSERS if b.name == "Zen")
STORE = Path("/profiles/zen/cookies.sqlite")


def _cookie(name: str, domain: str = "github.com", **over: Any) -> browser_cookies.BrowserCookie:
    fields: dict[str, Any] = {
        "name": name,
        "value": f"value-of-{name}",
        "domain": domain,
        "path": "/",
        "secure": True,
        "http_only": True,
        "same_site": "Lax",
        "expires": 2_000_000_000.0,
    }
    fields.update(over)
    return browser_cookies.BrowserCookie(**fields)


class _Harness:
    """A CookieTools wired to a fake browser, a fake profile and a fake MCP client."""

    def __init__(self, stub_mcp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tools = CookieTools(stub_mcp)
        self.set_all = AsyncMock()
        self.tools.session._browser = SimpleNamespace(cookies=SimpleNamespace(set_all=self.set_all))  # type: ignore[assignment]
        self.read = MagicMock(return_value=[_cookie("sid"), _cookie("pref")])
        monkeypatch.setattr(browser_cookies, "default_browser", lambda: ZEN)
        monkeypatch.setattr(browser_cookies, "locate_cookie_store", lambda _b: STORE)
        monkeypatch.setattr(browser_cookies, "read_cookies", self.read)
        monkeypatch.delenv(ALLOW_BROWSER_COOKIES_ENV, raising=False)
        self.can_elicit = True
        self.elicit = AsyncMock(return_value=self.answer("accept", True))
        self.ctx: Any = SimpleNamespace(
            session=SimpleNamespace(check_client_capability=lambda _cap: self.can_elicit),
            elicit=self.elicit,
        )

    @staticmethod
    def answer(action: str, allow: bool | None) -> SimpleNamespace:
        data = SimpleNamespace(allow=allow) if allow is not None else None
        return SimpleNamespace(action=action, data=data)

    async def run(self, domains: list[str] | None = None) -> dict[str, Any]:
        return await self.tools.use_default_browser_cookies(self.ctx, domains)


@pytest.fixture
def h(stub_mcp: Any, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(stub_mcp, monkeypatch)


async def test_accept_copies_the_cookies_into_the_automation_browser(h: _Harness) -> None:
    out = await h.run(["GitHub.com"])

    h.read.assert_called_once_with(ZEN, STORE, ["github.com"])
    (params,) = h.set_all.await_args.args
    assert [p.to_json() for p in params] == [
        {
            "name": name,
            "value": f"value-of-{name}",
            "domain": "github.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
            "expires": 2_000_000_000.0,
        }
        for name in ("sid", "pref")
    ]
    assert out["summary"] == "Copied 2 cookie(s) from Zen for github.com"
    assert out["data"] == {"browser": "Zen", "sites": 1, "top": {"github.com": 2}}
    assert "value-of" not in repr(out)  # the agent gets counts, never values


async def test_the_prompt_names_the_browser_and_the_sites(h: _Harness) -> None:
    await h.run(["github.com", "https://gitlab.com/x"])
    message = h.elicit.await_args.kwargs["message"]
    assert "your Zen cookies for github.com, gitlab.com" in message


async def test_without_domains_the_prompt_says_all_sites(h: _Harness) -> None:
    out = await h.run()
    assert "your Zen cookies for ALL sites" in h.elicit.await_args.kwargs["message"]
    h.read.assert_called_once_with(ZEN, STORE, [])
    assert out["summary"].endswith("for ALL sites")


@pytest.mark.parametrize(
    ("action", "allow"),
    [("decline", None), ("cancel", None), ("accept", False)],
)
async def test_anything_but_an_explicit_yes_reads_nothing(
    h: _Harness, action: str, allow: bool | None
) -> None:
    h.elicit.return_value = h.answer(action, allow)
    out = await h.run(["github.com"])

    h.read.assert_not_called()
    h.set_all.assert_not_called()
    assert out == {"summary": "The user did not allow copying Zen cookies. Do not ask again."}


async def test_client_that_cannot_ask_is_refused(h: _Harness) -> None:
    h.can_elicit = False
    with pytest.raises(ZendriverMCPError, match=ALLOW_BROWSER_COOKIES_ENV):
        await h.run(["github.com"])
    h.elicit.assert_not_called()
    h.read.assert_not_called()
    h.set_all.assert_not_called()


async def test_env_opt_in_stands_in_for_a_client_that_cannot_ask(
    h: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.can_elicit = False
    monkeypatch.setenv(ALLOW_BROWSER_COOKIES_ENV, "1")
    out = await h.run(["github.com"])
    h.elicit.assert_not_called()
    assert out["summary"].startswith("Copied 2 cookie(s)")


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
async def test_env_opt_in_is_exactly_1(
    h: _Harness, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    h.can_elicit = False
    monkeypatch.setenv(ALLOW_BROWSER_COOKIES_ENV, value)
    with pytest.raises(ZendriverMCPError):
        await h.run(["github.com"])
    h.read.assert_not_called()


async def test_env_opt_in_does_not_silence_a_client_that_can_ask(
    h: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_BROWSER_COOKIES_ENV, "1")
    h.elicit.return_value = h.answer("decline", None)
    await h.run(["github.com"])
    h.elicit.assert_awaited_once()
    h.read.assert_not_called()


async def test_browser_not_started_fails_before_the_user_is_asked(h: _Harness) -> None:
    h.tools.session._browser = None
    with pytest.raises(BrowserNotStartedError):
        await h.run(["github.com"])
    h.elicit.assert_not_called()


async def test_unusable_domains_fail_before_the_user_is_asked(h: _Harness) -> None:
    # falling through to "ALL sites" here would widen what the agent asked for
    with pytest.raises(ZendriverMCPError, match="No usable domain"):
        await h.run(["  ", "."])
    h.elicit.assert_not_called()
    h.read.assert_not_called()


async def test_no_matching_cookies_copies_nothing(h: _Harness) -> None:
    h.read.return_value = []
    out = await h.run(["github.com"])
    h.set_all.assert_not_called()
    assert out == {"summary": "Zen holds no valid cookies for github.com; nothing copied."}


async def test_one_rejected_cookie_does_not_lose_the_rest(h: _Harness) -> None:
    h.read.return_value = [_cookie("good"), _cookie("bad"), _cookie("fine")]

    async def set_all(params: list[Any]) -> None:
        if len(params) > 1 or params[0].name == "bad":
            raise RuntimeError("Invalid cookie fields")

    h.set_all.side_effect = set_all
    out = await h.run(["github.com"])

    singles = [call.args[0][0].name for call in h.set_all.await_args_list[1:]]
    assert singles == ["good", "bad", "fine"]
    assert out["summary"] == "Copied 2 cookie(s) from Zen for github.com (1 rejected by Chrome)"


async def test_session_cookie_and_unspecified_same_site_are_left_out_of_the_cdp_call(
    h: _Harness,
) -> None:
    h.read.return_value = [_cookie("s", expires=None, same_site=None)]
    await h.run(["github.com"])
    (params,) = h.set_all.await_args.args
    assert params[0].to_json() == {
        "name": "s",
        "value": "value-of-s",
        "domain": "github.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
    }


async def test_registered_tool_hides_the_context_and_gets_a_long_budget() -> None:
    tool = mcp._tool_manager.get_tool("use_default_browser_cookies")
    assert tool is not None
    assert tool.context_kwarg == "ctx"  # FastMCP injects it
    assert set(tool.parameters["properties"]) == {"domains"}  # and clients never see it
    assert tool.fn.__zendriver_timeout__ == 300  # time for the user to read the prompt
