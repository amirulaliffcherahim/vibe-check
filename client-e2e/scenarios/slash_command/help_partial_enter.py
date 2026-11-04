"""Enter on a partial `/help` prefix completes and runs it, then records the builtin command use."""

from __future__ import annotations

from e2e.app_server.scenario import Timeline

request_methods = {"telemetry/record"}
capture_startup = False
# Two entries that stay inside the viewport however long the command list grows.
screen_contains = ("/help: Show help message", "/debug: Toggle debug console")

timeline: Timeline = ["/hel", "\r"]  # typos:disable-line
