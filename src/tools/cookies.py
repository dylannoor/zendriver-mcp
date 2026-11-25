"""Cookie import/export via CDP (works on HTTP-only cookies, all domains).

Upstream ``StorageTools.get_cookies`` reads ``document.cookie`` - that misses
HTTP-only cookies and only sees the current origin. These tools use
``Network.getAllCookies`` / ``Network.setCookies`` which see everything the
browser stores, so you can round-trip a full authenticated session.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field
from zendriver import cdp

from src import browser_cookies
from src.artifacts import resolve_artifact_path
from src.errors import ZendriverMCPError
from src.response import ToolResponse
from src.tools.base import ToolBase

# Standing consent for MCP clients that cannot show the user a prompt.
ALLOW_BROWSER_COOKIES_ENV = "ZENDRIVER_MCP_ALLOW_BROWSER_COOKIES"


# The form the MCP client shows the user before any cookie is read. No
# docstring: pydantic would put it in the schema the user gets to see.
class _CookieConsent(BaseModel):
    model_config = ConfigDict(title="Use browser cookies")

    allow: bool = Field(description="Copy these cookies into the automation browser")


class CookieTools(ToolBase):
    """Full-fidelity cookie I/O, including HTTP-only cookies across all origins."""

    def _register_tools(self) -> None:
        self._register(self.export_cookies)
        self._register(self.import_cookies)
        self._register(self.clear_all_cookies)
        self._register(self.list_all_cookies)
        # The budget covers the user reading the consent prompt and, for
        # Chromium browsers on macOS, the Keychain prompt after it.
        self._register(self.use_default_browser_cookies, timeout=300)

    async def list_all_cookies(self) -> list[dict[str, Any]]:
        """Return every cookie the browser is holding, across all origins."""
        tab = self.session.page
        cookies = await tab.send(cdp.network.get_all_cookies())
        return [c.to_json() for c in cookies or []]

    async def export_cookies(self, file_path: str) -> dict[str, Any]:
        """Dump all cookies to a JSON file.

        Compatible with the "Edit This Cookie" Chrome extension export format
        and Playwright's storage state shape (just the cookies slice).
        Use ``import_cookies`` on another session to restore.
        """
        if not file_path:
            raise ZendriverMCPError("file_path is required")

        tab = self.session.page
        cookies = await tab.send(cdp.network.get_all_cookies()) or []
        serialised = [c.to_json() for c in cookies]

        target = resolve_artifact_path(
            file_path, default_prefix="zendriver-cookies", default_ext="json"
        )
        target.write_text(json.dumps(serialised, indent=2, sort_keys=True))
        # Cookies contain session secrets; restrict to owner-read/write.
        try:
            target.chmod(0o600)
        except OSError:
            pass  # best-effort on platforms without POSIX perms
        return ToolResponse(
            summary=f"Exported {len(serialised)} cookies -> {target}",
            data={"count": len(serialised)},
            files=[str(target)],
        ).to_dict()

    async def import_cookies(self, file_path: str) -> str:
        """Load cookies from a JSON file produced by ``export_cookies``.

        Silently skips entries that fail the ``CookieParam`` schema so a
        partial restore still works. Existing cookies with the same
        (name, domain, path) triple are overwritten by Chrome.
        """
        source = Path(file_path).expanduser().resolve()
        if not source.exists():
            raise ZendriverMCPError(f"Cookie file not found: {source}")

        raw = json.loads(source.read_text())
        if not isinstance(raw, list):
            raise ZendriverMCPError("Cookie file must contain a JSON array")

        params: list[cdp.network.CookieParam] = []
        skipped = 0
        for entry in raw:
            try:
                params.append(cdp.network.CookieParam.from_json(entry))
            except (KeyError, ValueError, TypeError):
                skipped += 1

        if not params:
            raise ZendriverMCPError("No valid cookies found in file")

        await self.session.page.send(cdp.network.set_cookies(cookies=params))
        suffix = f" ({skipped} skipped)" if skipped else ""
        return f"Imported {len(params)} cookies{suffix}"

    async def clear_all_cookies(self) -> str:
        """Delete every cookie in the current browser context."""
        await self.session.page.send(cdp.network.clear_browser_cookies())
        return "All cookies cleared"

    async def use_default_browser_cookies(
        self, ctx: Context, domains: list[str] | None = None
    ) -> dict[str, Any]:
        """Copy cookies from the user's default browser into this one, after asking the user.

        ``domains`` limits the copy to those sites and their subdomains;
        omit it only when every site is needed. Call it after
        ``start_browser`` and before navigating to a site that needs a
        login. If the user declines, do not ask again.
        """
        jar = self.session.browser.cookies  # raises before we bother the user
        browser = browser_cookies.default_browser()
        store = browser_cookies.locate_cookie_store(browser)
        wanted = browser_cookies.normalize_domains(domains)
        if domains and not wanted:
            raise ZendriverMCPError(f"No usable domain in {domains!r}")
        scope = ", ".join(wanted) if wanted else "ALL sites"

        if not await self._user_allows(ctx, browser.name, scope):
            return ToolResponse(
                summary=f"The user did not allow copying {browser.name} cookies. Do not ask again."
            ).to_dict()

        # Off the event loop: the Keychain prompt can block for a while and
        # the CDP websocket has to stay serviced meanwhile.
        cookies = await asyncio.to_thread(browser_cookies.read_cookies, browser, store, wanted)
        if not cookies:
            return ToolResponse(
                summary=f"{browser.name} holds no valid cookies for {scope}; nothing copied."
            ).to_dict()

        params = [
            cdp.network.CookieParam(
                name=c.name,
                value=c.value,
                domain=c.domain,
                path=c.path,
                secure=c.secure,
                http_only=c.http_only,
                same_site=cdp.network.CookieSameSite(c.same_site) if c.same_site else None,
                expires=cdp.network.TimeSinceEpoch(c.expires) if c.expires else None,
            )
            for c in cookies
        ]
        skipped = 0
        try:
            await jar.set_all(params)
        except Exception:  # noqa: BLE001 - one cookie Chrome rejects fails the whole batch
            for param in params:
                try:
                    await jar.set_all([param])
                except Exception:  # noqa: BLE001
                    skipped += 1

        copied = len(params) - skipped
        suffix = f" ({skipped} rejected by Chrome)" if skipped else ""
        return ToolResponse(
            summary=f"Copied {copied} cookie(s) from {browser.name} for {scope}{suffix}",
            data={"browser": browser.name, **browser_cookies.summarize_domains(cookies)},
        ).to_dict()

    async def _user_allows(self, ctx: Context, browser_name: str, scope: str) -> bool:
        """Ask the user through the MCP client; no answer means no."""
        can_ask = ctx.session.check_client_capability(
            types.ClientCapabilities(elicitation=types.ElicitationCapability())
        )
        if not can_ask:
            if os.environ.get(ALLOW_BROWSER_COOKIES_ENV) == "1":
                return True
            raise ZendriverMCPError(
                "This MCP client cannot ask the user for consent (no elicitation support), "
                "so browser cookies stay untouched. The user can allow it without a prompt "
                f"by setting {ALLOW_BROWSER_COOKIES_ENV}=1 in this server's environment."
            )
        result = await ctx.elicit(
            message=(
                f"Zendriver wants to copy your {browser_name} cookies for {scope} into the "
                "automation browser. The agent driving that browser can then act as you, "
                "logged in, on those sites."
            ),
            schema=_CookieConsent,
        )
        return result.action == "accept" and result.data.allow is True
