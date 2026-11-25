# logging tools - network and console log management
import base64
import time

from zendriver import cdp

from src.tools.base import ToolBase


class LoggingTools(ToolBase):
    """tools for network and console log access"""

    def _register_tools(self) -> None:
        """register logging tools"""
        self._register(self.get_network_logs)
        self._register(self.get_console_logs)
        self._register(self.clear_logs)
        self._register(self.wait_for_network)
        self._register(self.wait_for_request)
        self._register(self.get_network_response_body)

    async def get_network_logs(self, limit: int = 20) -> str:
        """Get recent network request logs captured via CDP."""
        logs = self.session.get_network_logs(limit)
        if not logs:
            return "No network logs captured"

        lines = [f"Network logs ({len(logs)} entries):"]
        for log in logs:
            method = log.get("method", "GET")
            url = log.get("url", "unknown")[:80]
            status = log.get("status", "?")
            rtype = log.get("type", "")
            lines.append(f"  {method} {url} {status} {rtype}".rstrip())
        return "\n".join(lines)

    async def get_console_logs(self, limit: int = 20) -> str:
        """Get recent console logs captured via CDP."""
        logs = self.session.get_console_logs(limit)
        if not logs:
            return "No console logs captured"

        lines = [f"Console logs ({len(logs)} entries):"]
        for log in logs:
            log_type = log.get("type", "log")
            text = log.get("text", "")[:100]
            lines.append(f"  [{log_type}] {text}")
        return "\n".join(lines)

    async def clear_logs(self) -> str:
        """Clear all captured network and console logs."""
        self.session.clear_logs()
        return "Cleared all logs"

    async def wait_for_network(self, timeout: float = 10.0, idle_time: float = 0.5) -> str:
        """Wait for network activity to become idle.

        Useful after triggering actions that cause API calls.

        Args:
            timeout: Maximum time to wait in seconds
            idle_time: How long network must be idle to consider it done
        """
        start = time.time()
        last_count = 0
        idle_start = None

        while time.time() - start < timeout:
            logs = self.session.get_network_logs(100)
            current_count = len(logs)

            if current_count == last_count:
                # no new requests
                if idle_start is None:
                    idle_start = time.time()
                elif time.time() - idle_start >= idle_time:
                    # network has been idle long enough
                    elapsed = time.time() - start
                    return f"Network idle after {elapsed:.1f}s ({current_count} requests captured)"
            else:
                # new request came in, reset idle timer
                idle_start = None
                last_count = current_count

            await self.session.page.wait(0.1)

        return f"Timeout after {timeout}s - network may still be active ({last_count} requests captured)"

    async def wait_for_request(
        self, url_pattern: str, timeout: float = 30.0, method: str | None = None
    ) -> str:
        """Wait for a matching request to **complete** (response received).

        ``url_pattern`` is a case-insensitive URL substring; ``method``
        optionally filters on HTTP verb.
        """
        # Only log entries with a resolved status code are considered, so this
        # returns after the server replied, not the moment Chrome sent the
        # request.
        start = time.time()
        safe_pattern = url_pattern.lower()
        safe_method = method.upper() if method else None
        start_count = len(self.session.get_network_logs(2000))

        while time.time() - start < timeout:
            # Only look at log entries that arrived after we started waiting
            # so stale history doesn't satisfy the wait.
            logs = self.session.get_network_logs(2000)[start_count:]
            for log in logs:
                url = log.get("url", "").lower()
                status = log.get("status", 0)
                # Need a real response (non-zero status or FAILED sentinel)
                # to consider the request "completed".
                if not status or status == "?":
                    continue
                if safe_pattern not in url:
                    continue
                req_method = log.get("method", "GET")
                if safe_method and req_method != safe_method:
                    continue
                elapsed = time.time() - start
                return (
                    f"Found matching request after {elapsed:.1f}s:\n"
                    f"  {req_method} {log.get('url', '')[:100]}\n"
                    f"  Status: {status}\n"
                    f"  Type: {log.get('type', 'unknown')}"
                )
            await self.session.page.wait(0.2)

        return f"Timeout: No completed request matching '{url_pattern}' found after {timeout}s"

    async def get_network_response_body(
        self,
        url_pattern: str,
        method: str | None = None,
        max_chars: int = 10000,
        offset: int = 0,
    ) -> str:
        """Get the response body of the latest captured request matching ``url_pattern``.

        Filters and pagination work as in wait_for_request and get_content.
        """
        safe_pattern = url_pattern.lower()
        safe_method = method.upper() if method else None

        for log in reversed(self.session.get_network_logs(1000)):
            request_id = log.get("_request_id")
            if not request_id or safe_pattern not in log.get("url", "").lower():
                continue
            if safe_method and log.get("method", "GET") != safe_method:
                continue

            header = f"{log.get('method')} {log.get('url', '')[:100]} {log.get('status')}"
            try:
                body, is_base64 = await self.session.page.send(
                    cdp.network.get_response_body(request_id)
                )
            except Exception as exc:  # noqa: BLE001 - CDP raises for evicted bodies
                # Chrome drops bodies after a navigation and only serves
                # them for the tab that made the request.
                return f"{header}\nError: body unavailable ({exc})"
            if is_base64:
                raw = base64.b64decode(body)
                try:
                    body = raw.decode("utf-8")
                except UnicodeDecodeError:
                    return (
                        f"{header}\n[binary body, {len(raw)} bytes, "
                        f"{log.get('mime_type', 'unknown type')}; use download_file to save it]"
                    )
            return f"{header}\n{self._paginate(body, max_chars, offset)}"

        return f"No captured request matching '{url_pattern}'"
