# element interaction tools - click, type, clear, focus, select, upload,
# highlight, hide, remove
import json

from zendriver import cdp

from src.errors import ElementNotFoundError
from src.tools._shadow_js import (
    DESCRIBE_SHADOW_JS,
    FIND_CLICK_COORDS_BY_TEXT_JS,
    FIND_SHADOW_HOST_COORDS_JS,
)
from src.tools.base import ZENDRIVER_ID_ATTR, ToolBase


class ElementTools(ToolBase):
    """tools for interacting with page elements"""

    def _register_tools(self) -> None:
        """register element interaction tools"""
        self._register(self.click)
        self._register(self.click_shadow)
        self._register(self.describe_shadow)
        self._register(self.type_text)
        self._register(self.clear_input)
        self._register(self.focus_element)
        self._register(self.select_option)
        self._register(self.upload_file)
        self._register(self.highlight_element)
        self._register(self.hide_element)
        self._register(self.remove_element)

    async def click(self, selector: str | None = None, text: str | None = None) -> str:
        """Click a visible element with a real (trusted) mouse event.

        Pass a CSS selector, a numeric id from ``get_interaction_tree``,
        or ``text`` to match visible text (shadow-DOM aware).
        """
        # ``Input.dispatchMouseEvent`` at the element's rect centre gives
        # ``isTrusted = true``. Synthetic ``element.click()`` /
        # ``dispatchEvent(new MouseEvent(...))`` calls fire with
        # ``isTrusted = false`` and are ignored by handlers that gate on
        # it - Fluent UI v8 combobox (Azure Portal "Supported account
        # types" dropdown is the canonical case), Office Fabric callouts,
        # Google's consent dialogs and a few shadow-DOM design systems.
        if selector:
            selector = self.resolve_selector(selector)

            check = await self.check_visibility(selector)
            if not check["found"]:
                if f"[{ZENDRIVER_ID_ATTR}=" in selector:
                    return "Error: ID not found. The page may have changed. Please run get_interaction_tree() again."
                raise ElementNotFoundError(selector)
            if check.get("hidden"):
                reason = "0x0 rect" if check.get("zero_size") else "display/visibility hidden"
                return f"Error: Element '{selector}' is hidden ({reason}). Cannot click."
            elem = await self.try_select(selector)
            if elem:
                # scroll_into_view first so get_position() returns a rect
                # that's actually inside the viewport - CDP mouse events
                # fire at viewport coords, clicking outside the viewport
                # is a no-op.
                await elem.scroll_into_view()
                position = await elem.get_position()
                if position is None:
                    return f"Error: Element '{selector}' has no visible position. Cannot click."
                # Not ``elem.mouse_click()``: that silently no-ops without a
                # position and then calls ``elem.flash()``, which injects a
                # red dot <div> plus a keyframes rule into the page.
                await self.session.page.mouse_click(*position.center)
                return f"Clicked: {selector}"
            raise ElementNotFoundError(selector)
        elif text:
            # Shadow-DOM-aware click: walks light + every open shadow root,
            # picks the tightest text match, climbs/dives to the deepest
            # interactive element (button, role=radio, custom-element
            # inner control) and returns its viewport-centre coords. We
            # then fire a real CDP mouse click at those coords.
            needle = json.dumps(text)
            result = await self.run_js(
                f"(() => {{\n{FIND_CLICK_COORDS_BY_TEXT_JS}\nreturn findClickCoordsByText({needle});\n}})()"
            )
            if not isinstance(result, dict) or not result.get("ok"):
                raise ElementNotFoundError(f"text={text!r}")
            await self.session.page.mouse_click(result["x"], result["y"])
            suffix = " (shadow)" if result.get("shadow") else ""
            return f"Clicked: {text}{suffix} (<{result['tag']}>)"
        return "Error: Provide selector or text"

    async def click_shadow(self, selector: str, max_depth: int = 6) -> str:
        """Click the real control inside a custom element's shadow DOM.

        ``selector`` matches the outer custom element (``<nes-button>``,
        ``<sds-radio>``, ...); nested open shadow roots are searched up
        to ``max_depth`` levels for the first interactive element, which
        gets a trusted mouse click.
        """
        selector = self.resolve_selector(selector)
        safe_sel = json.dumps(selector)
        result = await self.run_js(
            f"(() => {{\n{FIND_SHADOW_HOST_COORDS_JS}\nreturn findShadowHostCoords({safe_sel}, {int(max_depth)});\n}})()"
        )
        if not isinstance(result, dict) or not result.get("ok"):
            reason = result.get("reason") if isinstance(result, dict) else "unknown"
            if reason == "host_not_found":
                raise ElementNotFoundError(selector)
            return f"Error: no interactive element inside {selector} ({reason})"
        await self.session.page.mouse_click(result["x"], result["y"])
        tag = result["tag"]
        role = result.get("role")
        role_info = f" role={role}" if role else ""
        return f"Shadow-clicked: <{tag}{role_info}> inside {selector}"

    async def describe_shadow(self, selector: str, max_depth: int = 6) -> dict:
        """Dump a custom element's nested shadow-DOM tree as JSON.

        Each node has ``tag``, ``id``, ``role``, ``type``, ``text`` plus
        ``light`` and ``shadow`` child arrays. Use it when a visible
        control doesn't show up in ``find_buttons`` / ``find_inputs``,
        to find the host path for ``click_shadow``.
        """
        selector = self.resolve_selector(selector)
        safe_sel = json.dumps(selector)
        result = await self.run_js(
            f"(() => {{\n{DESCRIBE_SHADOW_JS}\nreturn describeShadow({safe_sel}, {int(max_depth)});\n}})()"
        )
        if not isinstance(result, dict) or not result.get("ok"):
            raise ElementNotFoundError(selector)
        return {"selector": selector, "tree": result["tree"]}

    async def type_text(self, text: str, selector: str, replace: bool = True) -> str:
        """Focus an element and type ``text`` via CDP Input.insertText.

        By default the field is cleared first (``replace=True``). Set
        ``replace=False`` to append to the existing value.
        """
        selector = self.resolve_selector(selector)

        # Propagate click errors (hidden element, 0x0 rect, not found) so
        # we don't blast insert_text into whatever else holds focus.
        click_result = await self.click(selector)
        if click_result.startswith("Error:"):
            return click_result
        if replace:
            # clear_input uses elem.apply(...) which doesn't move focus, so
            # no need to re-click here; doing so would double-fire click
            # handlers on date-pickers, toggles, etc.
            await self.clear_input(selector)
        await self.session.page.send(cdp.input_.insert_text(text=text))
        return f"Typed {len(text)} char(s) into {selector}"

    async def clear_input(self, selector: str) -> str:
        """Clear an input, textarea or contenteditable element."""
        # Native value setter + input/change events: the technique React's
        # synthetic-event system expects, so controlled components see the
        # change. ``document.execCommand`` is deprecated and invisible to
        # React/Vue/Angular state.
        selector = self.resolve_selector(selector)

        elem = await self.get_element(selector)
        await elem.apply(
            """
            (el) => {
                const tag = el.tagName;
                if (tag === 'INPUT' || tag === 'TEXTAREA') {
                    const proto = tag === 'INPUT'
                        ? window.HTMLInputElement.prototype
                        : window.HTMLTextAreaElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    setter.call(el, '');
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                } else if (el.isContentEditable) {
                    el.textContent = '';
                    el.dispatchEvent(new InputEvent('input', { bubbles: true }));
                }
            }
            """
        )
        return f"Cleared: {selector}"

    async def focus_element(self, selector: str) -> str:
        """Focus on an element."""
        selector = self.resolve_selector(selector)
        elem = await self.get_element(selector)
        await elem.focus()
        return f"Focused on: {selector}"

    async def select_option(self, selector: str, value: str) -> str:
        """Select an option from a dropdown."""
        selector = self.resolve_selector(selector)
        await self.get_element(selector)
        safe_sel = self.escape_js_string(selector)
        safe_val = self.escape_js_string(value)
        await self.run_js(f'''
            const select = document.querySelector("{safe_sel}");
            select.value = "{safe_val}";
            select.dispatchEvent(new Event("change", {{ bubbles: true }}));
        ''')
        return f"Selected '{value}' in: {selector}"

    async def upload_file(self, selector: str, file_path: str) -> str:
        """Upload a file to a file input."""
        selector = self.resolve_selector(selector)
        elem = await self.get_element(selector)
        await elem.send_file(file_path)
        return f"Uploaded file '{file_path}' to: {selector}"

    async def highlight_element(
        self, selector: str, color: str = "red", duration: float = 3.0
    ) -> str:
        """Outline an element in ``color`` for ``duration`` seconds (visual debugging)."""
        selector = self.resolve_selector(selector)
        result = await self.run_js(f"""
            (function() {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return null;
                const color = {json.dumps(color)};
                const prev = el.style.cssText;
                el.style.outline = "3px solid " + color;
                el.style.outlineOffset = "2px";
                el.style.boxShadow = "0 0 10px " + color;
                setTimeout(() => {{ el.style.cssText = prev; }}, {int(duration * 1000)});
                const r = el.getBoundingClientRect();
                return {{
                    tag: el.tagName,
                    box: [r.x, r.y, r.width, r.height].map(Math.round),
                }};
            }})()
        """)
        if not result:
            raise ElementNotFoundError(selector)
        x, y, w, h = result["box"]
        return f"Highlighted <{result['tag']}> at {x},{y} ({w}x{h})"

    async def hide_element(self, selector: str) -> str:
        """Hide every matching element (display:none), e.g. popups and overlays."""
        return await self._apply_to_all(selector, 'el.style.display = "none"', "Hidden")

    async def remove_element(self, selector: str) -> str:
        """Remove every matching element from the DOM, e.g. cookie banners and ads."""
        return await self._apply_to_all(selector, "el.remove()", "Removed")

    async def _apply_to_all(self, selector: str, statement: str, verb: str) -> str:
        """Run a JS ``statement`` on every match (``el``), report the count."""
        selector = self.resolve_selector(selector)
        count = await self.run_js(f"""
            (function() {{
                const els = document.querySelectorAll({json.dumps(selector)});
                els.forEach((el) => {{ {statement}; }});
                return els.length;
            }})()
        """)
        if not count:
            raise ElementNotFoundError(selector)
        return f"{verb} {count} element(s): {selector}"
