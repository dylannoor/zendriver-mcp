# form and input tools - fill form, submit, keyboard, mouse
import json

from zendriver import cdp

from src.errors import ZendriverMCPError
from src.tools.base import ToolBase

# Metadata for named keys: (windowsVirtualKeyCode, key/code name, text to
# actually insert or None for non-text keys). Values sourced from Chromium's
# ui/events/keycodes/dom/keycode_converter_data.inc.
_NON_ALPHA_CODE: dict[str, str] = {
    "-": "Minus",
    "=": "Equal",
    "[": "BracketLeft",
    "]": "BracketRight",
    "\\": "Backslash",
    ";": "Semicolon",
    "'": "Quote",
    ",": "Comma",
    ".": "Period",
    "/": "Slash",
    "`": "Backquote",
}


def _char_to_code(ch: str) -> str:
    """Return the CDP ``code`` (physical key) for a single-character input."""
    if ch.isalpha():
        return f"Key{ch.upper()}"
    if ch.isdigit():
        return f"Digit{ch}"
    return _NON_ALPHA_CODE.get(ch, ch)


_KEY_METADATA: dict[str, tuple[int, str, str | None]] = {
    "Enter": (13, "Enter", "\r"),
    "Tab": (9, "Tab", "\t"),
    "Escape": (27, "Escape", None),
    "Backspace": (8, "Backspace", None),
    "Delete": (46, "Delete", None),
    "ArrowUp": (38, "ArrowUp", None),
    "ArrowDown": (40, "ArrowDown", None),
    "ArrowLeft": (37, "ArrowLeft", None),
    "ArrowRight": (39, "ArrowRight", None),
    "Space": (32, "Space", " "),
    " ": (32, "Space", " "),
    "Home": (36, "Home", None),
    "End": (35, "End", None),
    "PageUp": (33, "PageUp", None),
    "PageDown": (34, "PageDown", None),
}


class FormTools(ToolBase):
    """tools for forms and input handling"""

    def _register_tools(self) -> None:
        """register form and input tools"""
        self._register(self.fill_form)
        self._register(self.submit_form)
        self._register(self.press_key)
        self._register(self.press_enter)
        self._register(self.mouse_click)

    async def fill_form(self, form_data: str) -> str:
        """Fill multiple form fields. Pass JSON like ``{"#email": "x@y.com"}``.

        Fields are cleared first; the result lists which selectors were
        filled and which were missing.
        """
        try:
            data = json.loads(form_data)
        except json.JSONDecodeError as exc:
            raise ZendriverMCPError(f"Invalid JSON form data: {exc.msg} at char {exc.pos}") from exc
        if not isinstance(data, dict):
            raise ZendriverMCPError("form_data JSON must decode to an object of selector -> value")
        filled: list[str] = []
        missing: list[str] = []

        for selector, value in data.items():
            elem = await self.try_select(selector)
            if elem is None:
                missing.append(selector)
                continue
            await elem.clear_input()
            await elem.send_keys(str(value))
            filled.append(selector)

        if missing:
            return (
                f"Filled {len(filled)} field(s): {', '.join(filled) or '(none)'}; "
                f"missing {len(missing)}: {', '.join(missing)}"
            )
        return f"Filled {len(filled)} field(s): {', '.join(filled)}"

    async def submit_form(self, selector: str = "form") -> str:
        """Submit a form so framework submit handlers and HTML validation run.

        Errors when the selector matches no form.
        """
        # ``form.requestSubmit()`` makes React/Vue/Formik ``onSubmit`` handlers
        # fire, falling back to a bubbling ``submit`` event and finally
        # ``form.submit()`` for ancient pages.
        import json as _json

        sel_json = _json.dumps(selector)
        result = await self.run_js(
            f"""
            (function() {{
                const form = document.querySelector({sel_json});
                if (!form) return 'missing';
                if (typeof form.requestSubmit === 'function') {{
                    form.requestSubmit();
                    return 'requestSubmit';
                }}
                const ev = new Event('submit', {{ bubbles: true, cancelable: true }});
                const proceed = form.dispatchEvent(ev);
                if (proceed) form.submit();
                return proceed ? 'dispatched' : 'prevented';
            }})()
            """
        )
        if result == "missing":
            return f"Error: no form matched selector {selector!r}"
        return f"Form submitted: {selector} ({result})"

    async def press_key(self, key: str, selector: str | None = None) -> str:
        """Press a key with a real browser key event; ``selector`` is focused first.

        Names: ``Enter``, ``Tab``, ``Escape``, ``Backspace``, ``Delete``,
        ``ArrowUp/Down/Left/Right``, ``Space``, ``Home``, ``End``,
        ``PageUp``, ``PageDown``, or any single character.
        """
        # CDP ``Input.dispatchKeyEvent`` rather than synthetic JS dispatch, so
        # native form-submit semantics, browser shortcuts, and IME all behave
        # as they would with a human keyboard.
        if selector:
            await self.focus_selector(selector)

        meta = _KEY_METADATA.get(key)
        tab = self.session.page
        if meta is not None:
            vk, key_name, text = meta
            # Use "keyDown" when there's text to insert (Space, Enter, Tab);
            # ``rawKeyDown`` skips text-insertion events. For pure modifier
            # keys (arrows, Escape, etc.) ``rawKeyDown`` is correct.
            if text is not None:
                await tab.send(
                    cdp.input_.dispatch_key_event(
                        type_="keyDown",
                        key=key_name,
                        code=key_name,
                        windows_virtual_key_code=vk,
                        native_virtual_key_code=vk,
                        text=text,
                        unmodified_text=text,
                    )
                )
            else:
                await tab.send(
                    cdp.input_.dispatch_key_event(
                        type_="rawKeyDown",
                        key=key_name,
                        code=key_name,
                        windows_virtual_key_code=vk,
                        native_virtual_key_code=vk,
                    )
                )
            await tab.send(
                cdp.input_.dispatch_key_event(
                    type_="keyUp",
                    key=key_name,
                    code=key_name,
                    windows_virtual_key_code=vk,
                    native_virtual_key_code=vk,
                )
            )
        elif len(key) == 1:
            code = _char_to_code(key)
            await tab.send(
                cdp.input_.dispatch_key_event(
                    type_="keyDown",
                    text=key,
                    unmodified_text=key,
                    key=key,
                    code=code,
                )
            )
            await tab.send(
                cdp.input_.dispatch_key_event(
                    type_="keyUp",
                    key=key,
                    code=code,
                )
            )
        else:
            raise ZendriverMCPError(f"Unknown key: {key!r}")

        return f"Pressed key: {key}" + (f" on {selector}" if selector else "")

    async def focus_selector(self, selector: str) -> None:
        """Focus an element via JS (helper, no separate MCP registration)."""
        elem = await self.get_element(selector)
        await elem.focus()

    async def press_enter(self, selector: str | None = None) -> str:
        """Press Enter key - convenience wrapper for press_key('Enter')."""
        return await self.press_key("Enter", selector)

    async def mouse_click(self, x: int, y: int) -> str:
        """Click at specific coordinates."""
        await self.session.page.mouse_click(x, y)
        return f"Clicked at ({x}, {y})"
