# page content tools - get html, get text, scroll, links, tables, metadata
import json

from src.errors import ElementNotFoundError, ZendriverMCPError
from src.tools.base import ToolBase


class ContentTools(ToolBase):
    """tools for page content and scrolling"""

    def _register_tools(self) -> None:
        """register content tools"""
        self._register(self.get_content)
        self._register(self.get_text_content)
        self._register(self.get_interaction_tree)
        self._register(self.get_links)
        self._register(self.extract_table)
        self._register(self.extract_structured_data)
        self._register(self.scroll)
        self._register(self.scroll_to_element)

    async def get_content(self, max_chars: int = 10000, offset: int = 0) -> str:
        """Get the page HTML, ``max_chars`` chars from ``offset``.

        The first line reports the slice, total size, and the next offset
        when more content is available.
        """
        content = await self.session.page.get_content()
        return self._paginate(content, max_chars, offset)

    async def get_text_content(self, max_chars: int = 10000, offset: int = 0) -> str:
        """Get visible page text; same pagination contract as get_content."""
        text = await self.run_js("document.body.innerText")
        return self._paginate(str(text), max_chars, offset)

    async def get_interaction_tree(self, limit: int = 150) -> str:
        """List interactive elements as compact JSON with numeric ids.

        The ids double as selectors for click/type tools. ``limit`` caps
        the element count.
        """
        import os

        script_path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "dom_walker.js")
        if not os.path.exists(script_path):
            return "Error: dom_walker.js not found in static/js"

        with open(script_path, encoding="utf-8") as f:
            js_code = f.read()

        try:
            tree = await self.run_js(js_code)
        except Exception as e:
            return f"Error analyzing page: {str(e)}"

        tree = tree or []
        limit = max(1, limit)
        total = len(tree)
        payload = json.dumps(tree[:limit], separators=(",", ":"))
        if total > limit:
            return f"[showing {limit} of {total} elements; raise limit for more]\n{payload}"
        return payload

    async def scroll(self, direction: str = "down", pixels: int = 500) -> str:
        """Scroll the page ``up`` or ``down`` by ``pixels`` (instant, not animated)."""
        # ``window.scrollBy`` on purpose: zendriver's ``scroll_down`` reads
        # its argument as a *percentage* of the viewport and animates via
        # ``Input.synthesizeScrollGesture`` - turning e.g. 500 into five
        # viewport-heights of smooth scrolling.
        page = self.session.page
        if direction == "down":
            await page.evaluate(f"window.scrollBy(0, {int(pixels)})")
            return f"Scrolled down {pixels}px"
        elif direction == "up":
            await page.evaluate(f"window.scrollBy(0, -{int(pixels)})")
            return f"Scrolled up {pixels}px"
        return f"Invalid direction: {direction}"

    async def scroll_to_element(self, selector: str) -> str:
        """Scroll an element into view; errors when nothing matches."""
        await self.get_element(selector)
        safe_sel = self.escape_js_string(selector)
        await self.run_js(
            f'document.querySelector("{safe_sel}")'
            f'?.scrollIntoView({{behavior: "smooth", block: "center"}})'
        )
        return f"Scrolled to: {selector}"

    async def get_links(
        self, filter_text: str = "", internal_only: bool = False, limit: int = 50
    ) -> str:
        """List the page's links as ``text -> href``, deduplicated.

        ``filter_text`` keeps links whose URL or text contains it;
        ``internal_only`` keeps same-host links; ``limit`` caps the output.
        """
        links = await self.run_js(f"""
            (function() {{
                const filter = {json.dumps(filter_text.lower())};
                const internalOnly = {json.dumps(internal_only)};
                const seen = new Set();
                const results = [];
                for (const a of document.querySelectorAll("a[href]")) {{
                    const href = a.href;
                    if (!href || href.startsWith("javascript:") || seen.has(href)) continue;
                    seen.add(href);
                    const text = (a.innerText || a.getAttribute("aria-label") || "").trim();
                    if (filter && !href.toLowerCase().includes(filter)
                        && !text.toLowerCase().includes(filter)) continue;
                    let internal = false;
                    try {{ internal = new URL(href).hostname === location.hostname; }} catch (_) {{}}
                    if (internalOnly && !internal) continue;
                    results.push({{ text: text.slice(0, 60), href: href.slice(0, 200), internal }});
                }}
                return results;
            }})()
        """)
        if not links:
            return "No links found" + (f" matching '{filter_text}'" if filter_text else "")

        limit = max(1, limit)
        lines = [f"Found {len(links)} link(s):"]
        for link in links[:limit]:
            scope = "int" if link["internal"] else "ext"
            lines.append(f"  [{scope}] {link['text'] or '(no text)'} -> {link['href']}")
        if len(links) > limit:
            lines.append(f"  [showing {limit} of {len(links)}; raise limit or filter for more]")
        return "\n".join(lines)

    async def extract_table(
        self, selector: str = "table", format: str = "json", max_rows: int = 100
    ) -> str:
        """Extract every matching table's cell text, without the markup.

        ``format`` is ``json`` (headers + rows per table) or ``markdown``;
        ``max_rows`` caps the rows returned per table.
        """
        if format not in ("json", "markdown"):
            raise ZendriverMCPError(f"Unknown format {format!r}; use 'json' or 'markdown'")
        tables = await self.run_js(f"""
            (function() {{
                const cells = (tr) =>
                    Array.from(tr.querySelectorAll("th, td")).map((c) => c.innerText.trim());
                return Array.from(document.querySelectorAll({json.dumps(selector)}))
                    .filter((t) => t.tagName === "TABLE")
                    .map((table) => {{
                        // ``table.rows`` skips rows of nested tables, unlike
                        // querySelectorAll("tr").
                        const trs = Array.from(table.rows);
                        let headers = table.tHead ? trs.filter((tr) => tr.parentNode === table.tHead) : [];
                        let body = trs.filter((tr) => !headers.includes(tr));
                        // No <thead>: only an all-<th> first row counts as a header.
                        if (!headers.length && body.length && body[0].querySelector("th")
                            && !body[0].querySelector("td")) headers = [body.shift()];
                        return {{
                            headers: headers.length ? cells(headers[headers.length - 1]) : [],
                            rows: body.map(cells).filter((r) => r.length),
                        }};
                    }});
            }})()
        """)
        if not tables:
            raise ElementNotFoundError(selector)

        max_rows = max(1, max_rows)
        for table in tables:
            table["rowCount"] = len(table["rows"])
            table["rows"] = table["rows"][:max_rows]

        if format == "json":
            return json.dumps(tables, separators=(",", ":"))

        def row(values: list[str]) -> str:
            # A pipe or newline inside a cell would break the markdown grid.
            return (
                "| " + " | ".join(v.replace("|", "\\|").replace("\n", " ") for v in values) + " |"
            )

        parts: list[str] = []
        for i, table in enumerate(tables, 1):
            if len(tables) > 1:
                parts.append(f"### Table {i}")
            if table["headers"]:
                parts.append(row(table["headers"]))
                parts.append("|" + "---|" * len(table["headers"]))
            parts.extend(row(r) for r in table["rows"])
            shown = len(table["rows"])
            note = f"{table['rowCount']} row(s)"
            if shown < table["rowCount"]:
                note += f", showing {shown}; raise max_rows for more"
            parts.append(f"\n{note}\n")
        return "\n".join(parts).rstrip()

    async def extract_structured_data(self, max_chars: int = 10000, offset: int = 0) -> str:
        """Get the page's embedded metadata as JSON: JSON-LD, OpenGraph, Twitter and meta tags.

        Same pagination contract as get_content.
        """
        data = await self.run_js("""
            (function() {
                const result = { jsonld: [], opengraph: {}, twitter: {}, meta: {} };
                document.querySelectorAll('script[type="application/ld+json"]').forEach((s) => {
                    try { result.jsonld.push(JSON.parse(s.textContent)); } catch (_) {}
                });
                document.querySelectorAll('meta[property^="og:"]').forEach((m) => {
                    result.opengraph[m.getAttribute("property")] = m.content;
                });
                document.querySelectorAll("meta[name]").forEach((m) => {
                    const name = m.name.toLowerCase();
                    if (name.startsWith("twitter:")) result.twitter[name] = m.content;
                    else result.meta[name] = m.content;
                });
                result.meta.title = document.title;
                const canonical = document.querySelector('link[rel="canonical"]');
                if (canonical) result.meta.canonical = canonical.href;
                return result;
            })()
        """)
        return self._paginate(json.dumps(data, separators=(",", ":")), max_chars, offset)
