"""`/model` provider labels and the scoped model list, as the server projects them."""

from __future__ import annotations

from collections.abc import Callable

from vibe.app_server._projection import project_config_view, provider_label
from vibe.core.config import ModelConfig, ProviderConfig, VibeConfigSchema
from vibe.core.types import Backend

MISTRAL = ProviderConfig(
    name="mistral",
    api_base="https://api.mistral.ai/v1",
    api_key_env_var="MISTRAL_API_KEY",
    backend=Backend.MISTRAL,
)
SHY_ROUTE = ProviderConfig(
    name="shy-route",
    api_base="https://shy-route.example/v1",
    api_key_env_var="SHY_ROUTE_API_KEY",
    backend=Backend.MISTRAL,
)
OPENROUTER = ProviderConfig(
    name="openrouter",
    api_base="https://openrouter.ai/api/v1",
    api_key_env_var="OPENROUTER_API_KEY",
)
CLAUDE = ProviderConfig(
    name="claude",
    api_base="https://api.anthropic.com/v1",
    api_key_env_var="CLAUDE_API_KEY",
    api_style="anthropic",
)


def _models() -> list[ModelConfig]:
    return [
        ModelConfig(name="mistral-medium", provider="mistral", alias="medium"),
        ModelConfig(name="large", provider="shy-route", alias="shy-route/large"),
        ModelConfig(name="glm", provider="openrouter", alias="openrouter/glm"),
        ModelConfig(name="sonnet", provider="claude", alias="claude/sonnet"),
    ]


def _config(
    make_config: Callable[..., VibeConfigSchema], **extra: object
) -> VibeConfigSchema:
    return make_config(
        providers=[MISTRAL, SHY_ROUTE, OPENROUTER, CLAUDE],
        models=_models(),
        active_model="medium",
        **extra,
    )


def test_labels_name_where_each_model_runs(
    make_config: Callable[..., VibeConfigSchema],
) -> None:
    config = _config(make_config)
    labels = {
        alias: provider_label(config, model) for alias, model in config.models.items()
    }
    assert labels["medium"] == "mistral"
    assert labels["shy-route/large"] == "shy-route"
    assert labels["openrouter/glm"] == "openrouter"
    assert labels["claude/sonnet"] == "claude"


def test_the_projection_carries_labels(
    make_config: Callable[..., VibeConfigSchema],
) -> None:
    view = project_config_view(_config(make_config))
    by_alias = {model.alias: model for model in view.models}
    assert by_alias["shy-route/large"].provider_label == "shy-route"
    assert view.active_model.provider_label == "mistral"


def test_no_scope_means_nothing_is_scoped(
    make_config: Callable[..., VibeConfigSchema],
) -> None:
    view = project_config_view(_config(make_config))
    assert view.model_scope_active is False
    assert not any(model.scoped for model in view.models)


def test_scoped_models_are_flagged(
    make_config: Callable[..., VibeConfigSchema],
) -> None:
    view = project_config_view(
        _config(make_config, scoped_models=["shy-route/large", "medium"])
    )
    assert view.model_scope_active is True
    scoped = {model.alias for model in view.models if model.scoped}
    assert scoped == {"shy-route/large", "medium"}
    assert view.active_model.scoped is True


def test_a_scope_naming_only_missing_models_is_inactive(
    make_config: Callable[..., VibeConfigSchema],
) -> None:
    """A removed provider must not leave `/model` empty."""
    view = project_config_view(
        _config(make_config, scoped_models=["gone/model", "also-gone"])
    )
    assert view.model_scope_active is False
    assert not any(model.scoped for model in view.models)
