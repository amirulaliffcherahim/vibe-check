"""`/model` labels rows with their provider, lists scoped models, and Tab shows all."""

from __future__ import annotations

from typing import Any

from e2e.app_server.scenario import Timeline


def _model(
    alias: str, display_name: str, label: str, *, scoped: bool
) -> dict[str, Any]:
    return {
        "name": alias,
        "alias": alias,
        "thinking": "off",
        "supportsImages": False,
        "displayName": display_name,
        "source": "local",
        "providerLabel": label,
        "scoped": scoped,
    }


_DEFAULT = _model("mistral-medium-3.5", "Mistral Medium 3.5", "mistral", scoped=True)
_MODELS = [
    _DEFAULT,
    _model("shy/large", "large", "shy", scoped=True),
    _model("openrouter/glm", "glm", "openrouter", scoped=False),
    _model("claude/sonnet", "sonnet", "claude", scoped=False),
]


def _config() -> dict[str, Any]:
    return {
        "activeModel": _DEFAULT,
        "activeModelPinned": False,
        "defaultModelAlias": _DEFAULT["alias"],
        "modelScopeActive": True,
        "models": _MODELS,
    }


handshake = {
    "config/read": {"config": _config()},
    "runtime/read": {"runtime": {"config": _config()}},
}

# Open /model (scoped), Tab to all, Tab back to scoped, Esc.
timeline: Timeline = ["/model", "\r", "\t", "\t", "\x1b"]
capture_steps = {1, 2, 3}
