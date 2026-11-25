# recording tools - record and replay action sequences
import json

from src.errors import ZendriverMCPError
from src.tools.base import ToolBase


class RecordingTools(ToolBase):
    """tools for recording and replaying action sequences"""

    def _register_tools(self) -> None:
        """register recording tools"""
        self._register(self.start_recording)
        self._register(self.stop_recording)
        # A replay runs many steps, each under its own per-tool budget.
        self._register(self.replay_action_sequence, timeout=600)

    async def start_recording(self) -> str:
        """Start recording page actions (navigate, click, type, scroll, waits, ...) for replay."""
        self.session.start_recording()
        return "Recording started"

    async def stop_recording(self) -> str:
        """Stop recording and return the actions as JSON for ``replay_action_sequence``."""
        actions = self.session.stop_recording()
        if not actions:
            return "No actions were recorded."
        return f"Recorded {len(actions)} action(s):\n{json.dumps(actions)}"

    async def replay_action_sequence(self, sequence: str) -> str:
        """Replay a JSON action list from ``stop_recording``; stops at the first failing step.

        Each entry is ``{"action": <tool name>, "params": {...}}``.
        """
        try:
            actions = json.loads(sequence)
        except json.JSONDecodeError as exc:
            raise ZendriverMCPError(f"Invalid JSON sequence: {exc.msg} at char {exc.pos}") from exc
        if not isinstance(actions, list) or not all(
            isinstance(a, dict)
            and isinstance(a.get("action"), str)
            and isinstance(a.get("params", {}), dict)
            for a in actions
        ):
            raise ZendriverMCPError(
                'sequence must be a JSON list of {"action": <tool name>, "params": {...}} objects'
            )
        # Validate every step before running any, so a typo in step 5
        # doesn't leave the page half-driven.
        unknown = sorted(
            {a["action"] for a in actions}
            - (self._tools.keys() - {self.replay_action_sequence.__name__})
        )
        if unknown:
            raise ZendriverMCPError(f"Unknown action(s) in sequence: {', '.join(unknown)}")

        lines: list[str] = []
        for i, entry in enumerate(actions, 1):
            action = entry["action"]
            try:
                result = str(await self._tools[action](**entry.get("params", {})))
            except Exception as exc:  # noqa: BLE001 - any tool failure ends the replay
                result = f"Error: {exc}"
            lines.append(f"  {i}. {action}: {result[:100]}")
            if result.startswith("Error:"):
                # Carrying on after a failed step would click and type on a
                # page that is no longer in the recorded state.
                return f"Replay stopped at step {i} of {len(actions)}:\n" + "\n".join(lines)
        return f"Replayed {len(actions)} action(s):\n" + "\n".join(lines)
