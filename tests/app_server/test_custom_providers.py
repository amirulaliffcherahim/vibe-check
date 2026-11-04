"""`providers/*`: custom Mistral-compatible providers beside the Mistral account.

The HTTP `/models` call is replaced, so no test reaches the network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
import tomllib
from typing import Any, Never

import pytest
import pytest_asyncio

from vibe.app_server._custom_providers import _discovered_entries
from vibe.app_server._process_services import ProcessServices
from vibe.app_server._runtime import HarnessProcess, create_harness_server
from vibe.app_server.client import AppServerClient
from vibe.app_server.protocol import (
    AppServerResponseError,
    ClientInfo,
    ProtocolErrorCode,
)
from vibe.app_server.transport import memory_transport_pair

BASE = "https://proxy.example.com/v1"


@pytest_asyncio.fixture
async def providers_client(
    monkeypatch: pytest.MonkeyPatch, process_services: ProcessServices
) -> AsyncIterator[AppServerClient]:
    """A connection with no session: provider management never needs one."""

    async def build_session_context(*args: object, **kwargs: object) -> Never:
        raise AssertionError("provider management must not create a session")

    client_transport, server_transport = memory_transport_pair()
    process = HarnessProcess(services=process_services)
    monkeypatch.setattr(process, "build_unified_session_context", build_session_context)
    server = await create_harness_server(
        server_transport, transport_kind="in_process", process=process
    )
    client = AppServerClient(client_transport, run_peer=server.serve)
    try:
        await client.initialize(ClientInfo(name="test", version="1"))
        await client.notify("initialized")
        yield client
    finally:
        await client.close()
        await process.close()


def _stub_models(monkeypatch: pytest.MonkeyPatch, payload: Any) -> list[str]:
    """Replace the `/models` fetch; return the URLs it was asked for."""
    asked: list[str] = []

    async def fetch(provider: Any, api_key: str | None) -> list[dict[str, Any]]:
        asked.append(provider.api_base)
        return _discovered_entries(payload)

    monkeypatch.setattr("vibe.app_server._custom_providers._fetch_models", fetch)
    return asked


def _persisted(config_dir: Path) -> dict[str, Any]:
    return tomllib.loads((config_dir / "config.toml").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_list_shows_the_primary_provider_and_no_key(
    providers_client: AppServerClient,
) -> None:
    response = await providers_client.request("providers/list", {})

    providers = response["providers"]
    assert [provider["name"] for provider in providers] == ["mistral", "llamacpp"]
    mistral = providers[0]
    assert mistral["isDefault"] is True
    assert mistral["backend"] == "mistral"
    assert mistral["apiKeyEnvVar"] == "MISTRAL_API_KEY"
    # The suite's autouse fixture exports a mock key, so the primary account
    # reports a credential; a custom provider is what this feature adds.
    assert mistral["hasApiKey"] is True
    assert providers[1]["isDefault"] is False


@pytest.mark.asyncio
async def test_add_registers_provider_and_its_models(
    providers_client: AppServerClient, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _stub_models(
        monkeypatch,
        {
            "data": [
                {"id": "llama-3.1-8b", "max_context_length": 131_072},
                {"id": "qwen-2.5-72b", "capabilities": {"vision": True}},
                {"object": "model"},
            ]
        },
    )

    response = await providers_client.request(
        "providers/add",
        {"name": "proxy", "apiBase": BASE, "apiKey": "sk-proxy", "fetchModels": True},
    )

    assert asked == [BASE], "discovery must ask the provider's own base"
    assert response["provider"]["name"] == "proxy"
    assert response["provider"]["apiKeyEnvVar"] == "PROXY_API_KEY"
    assert response["provider"]["hasApiKey"] is True
    assert response["fetchError"] is None
    assert response["discoveredModels"] == ["proxy/llama-3.1-8b", "proxy/qwen-2.5-72b"]

    persisted = _persisted(config_dir)
    assert any(entry["name"] == "proxy" for entry in persisted["providers"])
    proxy = next(entry for entry in persisted["providers"] if entry["name"] == "proxy")
    assert proxy["api_base"] == BASE
    assert proxy["api_key_env_var"] == "PROXY_API_KEY"
    # The custom base is reached on purpose; tenant reconciliation must not
    # rewrite it.
    assert proxy["preserve_api_base"] is True

    models = persisted["models"]
    assert isinstance(models, list)
    aliases = {entry["alias"] for entry in models}
    assert {"proxy/llama-3.1-8b", "proxy/qwen-2.5-72b"} <= aliases
    qwen = next(entry for entry in models if entry["alias"] == "proxy/qwen-2.5-72b")
    assert qwen["provider"] == "proxy"
    assert qwen["supports_images"] is True


@pytest.mark.asyncio
async def test_add_keeps_the_provider_when_discovery_fails(
    providers_client: AppServerClient, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(provider: Any, api_key: str | None) -> list[dict[str, Any]]:
        raise AssertionError

    async def unreachable(provider: Any, api_key: str | None) -> list[dict[str, Any]]:
        from vibe.app_server._dispatch import RequestFailure

        raise RequestFailure(
            ProtocolErrorCode.INTERNAL_ERROR, "Could not reach the endpoint"
        )

    monkeypatch.setattr("vibe.app_server._custom_providers._fetch_models", unreachable)

    response = await providers_client.request(
        "providers/add", {"name": "offline", "apiBase": BASE, "fetchModels": True}
    )

    assert response["discoveredModels"] == []
    assert response["fetchError"] == "Could not reach the endpoint"
    assert any(
        entry["name"] == "offline" for entry in _persisted(config_dir)["providers"]
    )


@pytest.mark.asyncio
async def test_add_refuses_the_primary_name_and_its_env_var(
    providers_client: AppServerClient,
) -> None:
    with pytest.raises(AppServerResponseError) as exc_info:
        await providers_client.request(
            "providers/add", {"name": "mistral", "apiBase": BASE}
        )
    assert exc_info.value.error.code is ProtocolErrorCode.INVALID_PARAMS
    assert "vibe --setup" in exc_info.value.error.message

    with pytest.raises(AppServerResponseError) as exc_info:
        await providers_client.request(
            "providers/add",
            {"name": "proxy", "apiBase": BASE, "apiKeyEnvVar": "MISTRAL_API_KEY"},
        )
    assert exc_info.value.error.code is ProtocolErrorCode.INVALID_PARAMS
    assert "MISTRAL_API_KEY" in exc_info.value.error.message


@pytest.mark.asyncio
async def test_add_rejects_a_bad_name_or_url(providers_client: AppServerClient) -> None:
    for params in (
        {"name": "!!!", "apiBase": BASE},
        {"name": "proxy", "apiBase": "ftp://example.com"},
        {"name": "proxy", "apiBase": "   "},
    ):
        with pytest.raises(AppServerResponseError) as exc_info:
            await providers_client.request("providers/add", params)
        assert exc_info.value.error.code is ProtocolErrorCode.INVALID_PARAMS


@pytest.mark.asyncio
async def test_add_twice_is_refused(
    providers_client: AppServerClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch, {"data": []})
    await providers_client.request(
        "providers/add", {"name": "proxy", "apiBase": BASE, "fetchModels": False}
    )

    with pytest.raises(AppServerResponseError) as exc_info:
        await providers_client.request(
            "providers/add", {"name": "proxy", "apiBase": BASE, "fetchModels": False}
        )
    assert exc_info.value.error.code is ProtocolErrorCode.INVALID_PARAMS
    assert "already exists" in exc_info.value.error.message


@pytest.mark.asyncio
async def test_fetch_models_registers_what_the_endpoint_lists(
    providers_client: AppServerClient, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch, {"data": []})
    await providers_client.request(
        "providers/add", {"name": "proxy", "apiBase": BASE, "fetchModels": False}
    )

    _stub_models(monkeypatch, {"models": [{"id": "mistral-nemo"}]})
    response = await providers_client.request(
        "providers/fetchModels", {"name": "proxy"}
    )

    assert response["discoveredModels"] == ["proxy/mistral-nemo"]
    aliases = {entry["alias"] for entry in _persisted(config_dir)["models"]}
    assert "proxy/mistral-nemo" in aliases


@pytest.mark.asyncio
async def test_add_model_records_prices_for_cost_reporting(
    providers_client: AppServerClient, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch, {"data": []})
    await providers_client.request(
        "providers/add", {"name": "proxy", "apiBase": BASE, "fetchModels": False}
    )

    response = await providers_client.request(
        "providers/addModel",
        {
            "provider": "proxy",
            "model": "custom-large",
            "maxContextLength": 32_768,
            "inputPrice": 0.5,
            "outputPrice": 1.5,
        },
    )

    assert response == {
        "alias": "proxy/custom-large",
        "model": "custom-large",
        "provider": "proxy",
        "displayName": "custom-large (proxy)",
    }
    entry = next(
        model
        for model in _persisted(config_dir)["models"]
        if model["alias"] == "proxy/custom-large"
    )
    assert entry["input_price"] == 0.5
    assert entry["output_price"] == 1.5
    assert entry["max_context_length"] == 32_768


@pytest.mark.asyncio
async def test_set_key_reports_a_stored_key(
    providers_client: AppServerClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch, {"data": []})
    await providers_client.request(
        "providers/add", {"name": "proxy", "apiBase": BASE, "fetchModels": False}
    )

    response = await providers_client.request(
        "providers/setKey", {"name": "proxy", "apiKey": "sk-new"}
    )

    # The suite's keyring backend stores nothing, so the key falls back to the
    # 0600 .env file and the outcome says so rather than claiming the keyring.
    assert response["outcome"] in {"completed", "save_error"}
    assert response["provider"]["name"] == "proxy"


@pytest.mark.asyncio
async def test_remove_drops_the_provider_its_models_and_key(
    providers_client: AppServerClient, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch, {"data": [{"id": "llama-3.1-8b"}]})
    await providers_client.request(
        "providers/add", {"name": "proxy", "apiBase": BASE, "fetchModels": True}
    )
    assert any(
        entry["alias"] == "proxy/llama-3.1-8b"
        for entry in _persisted(config_dir)["models"]
    )

    response = await providers_client.request("providers/remove", {"name": "proxy"})

    assert response["removed"] is True
    assert response["removedModels"] == ["proxy/llama-3.1-8b"]
    persisted = _persisted(config_dir)
    assert all(entry["name"] != "proxy" for entry in persisted.get("providers", []))
    assert all(
        entry.get("provider") != "proxy" for entry in persisted.get("models", [])
    )


@pytest.mark.asyncio
async def test_remove_works_when_it_empties_the_user_models(
    providers_client: AppServerClient, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`models` may not be empty, so the last removal must drop the field."""
    # A user config with no models of its own: the built-ins come from the
    # default layer, so this provider's model is the only one persisted.
    (config_dir / "config.toml").write_text(
        'active_model = "devstral-latest"\n', encoding="utf-8"
    )
    _stub_models(monkeypatch, {"data": [{"id": "only-model"}]})
    await providers_client.request(
        "providers/add", {"name": "solo", "apiBase": BASE, "fetchModels": True}
    )
    assert "models" in _persisted(config_dir)

    response = await providers_client.request("providers/remove", {"name": "solo"})

    assert response["removedModels"] == ["solo/only-model"]
    # Both fields are gone rather than left as empty lists: `models` would be
    # rejected by the schema, and `providers` would be litter.
    persisted = _persisted(config_dir)
    assert "models" not in persisted
    assert "providers" not in persisted
    # And the config still resolves: the built-in models are untouched.
    providers = (await providers_client.request("providers/list", {}))["providers"]
    assert any(provider["name"] == "mistral" for provider in providers)


@pytest.mark.asyncio
async def test_remove_refuses_the_primary_provider(
    providers_client: AppServerClient,
) -> None:
    with pytest.raises(AppServerResponseError) as exc_info:
        await providers_client.request("providers/remove", {"name": "mistral"})
    assert exc_info.value.error.code is ProtocolErrorCode.INVALID_PARAMS
    assert "cannot be removed" in exc_info.value.error.message


@pytest.mark.asyncio
async def test_unknown_provider_is_not_found(providers_client: AppServerClient) -> None:
    for method, params in (
        ("providers/setKey", {"name": "ghost", "apiKey": "k"}),
        ("providers/fetchModels", {"name": "ghost"}),
        ("providers/remove", {"name": "ghost"}),
        ("providers/addModel", {"provider": "ghost", "model": "m"}),
    ):
        with pytest.raises(AppServerResponseError) as exc_info:
            await providers_client.request(method, params)
        assert exc_info.value.error.code is ProtocolErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_add_a_mistral_compatible_provider(
    providers_client: AppServerClient, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch, {"data": [{"id": "mistral-large-latest"}]})

    response = await providers_client.request(
        "providers/add",
        {
            "name": "mistral-work",
            "apiBase": "https://api.mistral.ai/v1",
            "backend": "mistral",
            "fetchModels": True,
        },
    )

    assert response["provider"]["backend"] == "mistral"
    assert response["provider"]["apiKeyEnvVar"] == "MISTRAL_WORK_API_KEY"
    assert response["provider"]["isDefault"] is False
    assert response["discoveredModels"] == ["mistral-work/mistral-large-latest"]
    entry = next(
        p for p in _persisted(config_dir)["providers"] if p["name"] == "mistral-work"
    )
    assert entry["backend"] == "mistral"


@pytest.mark.asyncio
async def test_a_mistral_compatible_provider_never_becomes_the_account(
    providers_client: AppServerClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch, {"data": []})
    await providers_client.request(
        "providers/add",
        {
            "name": "mistral-work",
            "apiBase": "https://proxy.example.com/v1",
            "backend": "mistral",
            "fetchModels": False,
        },
    )

    providers = (await providers_client.request("providers/list", {}))["providers"]
    defaults = [p["name"] for p in providers if p["isDefault"]]
    assert defaults == ["mistral"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api_style", ["openai", "openai-responses", "reasoning", "anthropic"]
)
async def test_add_accepts_each_supported_wire_format(
    providers_client: AppServerClient,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    api_style: str,
) -> None:
    _stub_models(monkeypatch, {"data": []})

    response = await providers_client.request(
        "providers/add",
        {"name": "p", "apiBase": BASE, "apiStyle": api_style, "fetchModels": False},
    )

    assert response["provider"]["apiStyle"] == api_style
    entry = next(p for p in _persisted(config_dir)["providers"] if p["name"] == "p")
    # ``openai`` is the field default, which the writer omits.
    assert entry.get("api_style", "openai") == api_style


@pytest.mark.asyncio
@pytest.mark.parametrize("api_style", ["vertex-anthropic", "grpc", ""])
async def test_add_rejects_an_unsupported_wire_format(
    providers_client: AppServerClient, api_style: str
) -> None:
    with pytest.raises(AppServerResponseError) as exc_info:
        await providers_client.request(
            "providers/add", {"name": "p", "apiBase": BASE, "apiStyle": api_style}
        )
    assert exc_info.value.error.code is ProtocolErrorCode.INVALID_PARAMS


@pytest.mark.asyncio
async def test_anthropic_discovery_uses_its_own_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibe.app_server import _custom_providers
    from vibe.core.config import ProviderConfig
    from vibe.core.types import Backend

    sent: dict[str, str] = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self) -> dict[str, list[object]]:
            return {"data": []}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, headers: dict[str, str]) -> FakeResponse:
            sent.update(headers)
            return FakeResponse()

    monkeypatch.setattr(_custom_providers, "VibeAsyncHTTPClient", FakeClient)
    provider = ProviderConfig(
        name="claude",
        api_base="https://api.anthropic.com/v1",
        api_key_env_var="CLAUDE_API_KEY",
        api_style="anthropic",
        backend=Backend.GENERIC,
    )

    await _custom_providers._fetch_models(provider, "sk-ant-test")

    assert sent["x-api-key"] == "sk-ant-test"
    assert "Authorization" not in sent
    assert sent["anthropic-version"] == "2023-06-01"


def test_discovered_entries_reads_both_shapes() -> None:
    assert _discovered_entries({"data": [{"id": "a"}]})[0]["id"] == "a"
    assert _discovered_entries({"models": [{"id": "b"}]})[0]["id"] == "b"
    assert _discovered_entries([{"id": "c"}])[0]["id"] == "c"
    # An endpoint that says a model cannot chat is skipped.
    assert (
        _discovered_entries({
            "data": [{"id": "d", "capabilities": {"completion_chat": False}}]
        })
        == []
    )
    assert _discovered_entries({"unexpected": []}) == []
