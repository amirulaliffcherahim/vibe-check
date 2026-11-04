"""Custom Mistral-compatible providers, managed from `/provider`.

Vibe ships one primary provider (``mistral``) whose credential is either the
Subscription plan's browser sign-in or a single ``MISTRAL_API_KEY``. This module
adds *additional* providers beside it: any server that speaks Mistral's two
endpoints (``POST {api_base}/chat/completions``, ``GET {api_base}/models``), each
with its own key, its own models, and its own prices.

The primary provider is never touched here. A custom provider may not reuse its
name or its ``MISTRAL_API_KEY`` env var, so signing in to the plan and pointing
Vibe at a proxy or a self-hosted model are not mutually exclusive.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from http import HTTPStatus
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2
from pydantic import ValidationError

from vibe.app_server._dispatch import RequestFailure, method_not_found
from vibe.app_server._model import ProtocolModel, validate_wire
from vibe.app_server._provider_auth import active_credential_source, sanitize_api_base
from vibe.app_server.protocol import (
    ProtocolErrorCode,
    ProvidersAddModelParams,
    ProvidersAddModelResponse,
    ProvidersAddParams,
    ProvidersAddResponse,
    ProvidersFetchModelsParams,
    ProvidersFetchModelsResponse,
    ProvidersListParams,
    ProvidersListResponse,
    ProvidersRemoveParams,
    ProvidersRemoveResponse,
    ProvidersSetKeyParams,
    ProvidersSetKeyResponse,
    ProviderSummaryView,
)
from vibe.core.config import (
    DEFAULT_PROVIDERS,
    PRIMARY_MISTRAL_PROVIDER,
    ModelConfig,
    ProviderConfig,
    VibeConfigSchema,
    resolve_api_key,
)
from vibe.core.config.harness_files import HarnessFilesManager
from vibe.core.config.orchestrator import ConfigOrchestrator
from vibe.core.config.patch import (
    AddOperationPatch,
    PatchOp,
    RemoveOperationPatch,
    escape_json_pointer_token,
)
from vibe.core.types import Backend
from vibe.observability.logging import logger
from vibe.setup.auth.api_key_persistence import (
    apply_provider_to_config,
    persist_api_key,
    remove_api_key,
)
from vibe.utils.http import VibeAsyncHTTPClient, build_ssl_context

__all__ = ["PROVIDER_METHODS", "ProvidersController"]

PROVIDER_METHODS = frozenset({
    "providers/add",
    "providers/addModel",
    "providers/fetchModels",
    "providers/list",
    "providers/remove",
    "providers/setKey",
})

# The primary account's provider. Managed by `vibe --setup`, never here.
_PRIMARY_PROVIDER = PRIMARY_MISTRAL_PROVIDER
_PRIMARY_ENV_VAR = "MISTRAL_API_KEY"

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ENV_VAR_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DISCOVERY_TIMEOUT_SECONDS = 15.0
# What a model costs when the caller states no price: free, as ModelConfig
# defaults. A wrong guess would misreport spend, so the user states it.
_DEFAULT_CONTEXT_WINDOW = 131_072

type SessionFiles = Callable[[Path | None], HarnessFilesManager]
type OrchestratorLoader = Callable[
    [HarnessFilesManager], Awaitable[ConfigOrchestrator[VibeConfigSchema]]
]


def _normalize_name(name: str) -> str:
    cleaned = name.strip().lower()
    if not _NAME_PATTERN.match(cleaned):
        raise RequestFailure(
            ProtocolErrorCode.INVALID_PARAMS,
            f"Invalid provider name '{name}': use letters, digits, '-' or '_'.",
        )
    return cleaned


def _default_env_var(name: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return f"{token}_API_KEY" if token else "CUSTOM_PROVIDER_API_KEY"


def _normalize_api_base(api_base: str) -> str:
    cleaned = api_base.strip()
    if not cleaned:
        raise RequestFailure(
            ProtocolErrorCode.INVALID_PARAMS, "A provider needs an api_base URL."
        )
    try:
        parsed = urlsplit(cleaned)
        _ = parsed.port
    except ValueError as exc:
        raise RequestFailure(
            ProtocolErrorCode.INVALID_PARAMS, f"Invalid api_base '{api_base}': {exc}"
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise RequestFailure(
            ProtocolErrorCode.INVALID_PARAMS,
            f"Invalid api_base '{api_base}': it must be an http:// or https:// URL.",
        )
    return urlunsplit((
        parsed.scheme.lower(),
        parsed.netloc,
        parsed.path.rstrip("/"),
        parsed.query,
        "",
    ))


def _model_alias(provider_name: str, model_id: str) -> str:
    """``<provider>/<model>``, so a custom model never collides with a built-in alias."""
    if model_id.startswith(f"{provider_name}/"):
        return model_id
    return f"{provider_name}/{model_id}"


def _discovered_entries(payload: object) -> list[dict[str, Any]]:
    """The usable models in a ``/models`` reply.

    Reads the OpenAI/Mistral ``{"data": [...]}`` shape, a bare list, and the
    model-catalog ``{"models": [...]}`` shape. An entry with no id is skipped;
    so is one the endpoint says cannot chat.
    """
    if isinstance(payload, dict):
        for key in ("data", "models"):
            if isinstance(payload.get(key), list):
                raw: list[Any] = payload[key]
                break
        else:
            return []
    elif isinstance(payload, list):
        raw = payload
    else:
        return []

    entries: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id") or item.get("name")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        capabilities = item.get("capabilities")
        if isinstance(capabilities, dict) and (
            capabilities.get("completion_chat") is False
            or capabilities.get("CompletionChat") is False
        ):
            continue
        context = (
            item.get("max_context_length")
            or item.get("context_window")
            or item.get("context_length")
        )
        label = item.get("label") or item.get("display_name")
        entries.append({
            "id": model_id.strip(),
            "display_name": label.strip()
            if isinstance(label, str) and label.strip()
            else None,
            "max_context_length": context
            if isinstance(context, int) and context > 0
            else None,
            "supports_images": capabilities.get("vision")
            if isinstance(capabilities, dict)
            and isinstance(capabilities.get("vision"), bool)
            else None,
        })
    return entries


async def _fetch_models(
    provider: ProviderConfig, api_key: str | None
) -> list[dict[str, Any]]:
    """Ask ``provider`` what it serves."""
    url = f"{provider.api_base.rstrip('/')}/models"
    headers: dict[str, str] = dict(provider.extra_headers)
    if api_key:
        if provider.api_style == "anthropic" and provider.backend != Backend.MISTRAL:
            # Anthropic authenticates its model listing with x-api-key.
            headers["x-api-key"] = api_key
            headers.setdefault("anthropic-version", "2023-06-01")
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    async with VibeAsyncHTTPClient(
        timeout=httpx2.Timeout(_DISCOVERY_TIMEOUT_SECONDS), verify=build_ssl_context()
    ) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx2.HTTPError as exc:
            raise RequestFailure(
                ProtocolErrorCode.INTERNAL_ERROR, f"Could not reach {url}: {exc}"
            ) from exc

    if response.status_code >= HTTPStatus.BAD_REQUEST:
        raise RequestFailure(
            ProtocolErrorCode.INTERNAL_ERROR,
            f"{url} returned HTTP {response.status_code}: {response.text[:200].strip()}",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RequestFailure(
            ProtocolErrorCode.INTERNAL_ERROR, f"{url} did not return JSON."
        ) from exc
    return _discovered_entries(payload)


class ProvidersController:
    """``providers/*``: the custom providers beside the primary Mistral account."""

    def __init__(
        self, session_files: SessionFiles, load_orchestrator: OrchestratorLoader
    ) -> None:
        self._session_files = session_files
        self._load_orchestrator = load_orchestrator

    async def dispatch(self, method: str, raw_params: dict[str, Any]) -> ProtocolModel:
        match method:
            case "providers/list":
                validate_wire(ProvidersListParams, raw_params)
                return await self._list()
            case "providers/add":
                return await self._add(validate_wire(ProvidersAddParams, raw_params))
            case "providers/setKey":
                return await self._set_key(
                    validate_wire(ProvidersSetKeyParams, raw_params)
                )
            case "providers/fetchModels":
                return await self._fetch_models(
                    validate_wire(ProvidersFetchModelsParams, raw_params)
                )
            case "providers/addModel":
                return await self._add_model(
                    validate_wire(ProvidersAddModelParams, raw_params)
                )
            case "providers/remove":
                return await self._remove(
                    validate_wire(ProvidersRemoveParams, raw_params)
                )
            case _:
                raise method_not_found(method)

    async def _orchestrator(self) -> ConfigOrchestrator[VibeConfigSchema]:
        # The user's own config, wherever this server runs.
        return await self._load_orchestrator(self._session_files(None))

    async def _list(self) -> ProvidersListResponse:
        config = (await self._orchestrator()).config
        return ProvidersListResponse(
            providers=[
                await self._summarize(config, provider) for provider in config.providers
            ]
        )

    async def _summarize(
        self, config: VibeConfigSchema, provider: ProviderConfig
    ) -> ProviderSummaryView:
        env_var = provider.api_key_env_var
        key = resolve_api_key(env_var) if env_var else None
        # A key pasted into api_base must not reach the client.
        sanitized = sanitize_api_base(provider.api_base, [key] if key else [])
        return ProviderSummaryView(
            name=provider.name,
            api_base=sanitized or provider.api_base,
            backend=str(provider.backend),
            api_style=provider.api_style,
            api_key_env_var=env_var,
            has_api_key=not env_var or key is not None,
            credential_source=(
                await active_credential_source(provider) if env_var else None
            ),
            models=sorted(
                model.alias
                for model in config.models.values()
                if model.provider == provider.name
            ),
            is_default=provider.name == _PRIMARY_PROVIDER,
        )

    async def _add(self, params: ProvidersAddParams) -> ProvidersAddResponse:
        name = _normalize_name(params.name)
        if name == _PRIMARY_PROVIDER:
            raise RequestFailure(
                ProtocolErrorCode.INVALID_PARAMS,
                "The 'mistral' provider holds your Mistral account; use "
                "`vibe --setup` to change it. Pick another name, such as "
                "'mistral-api' or 'local'.",
            )
        api_base = _normalize_api_base(params.api_base)
        env_var = (params.api_key_env_var or "").strip() or _default_env_var(name)
        if not _ENV_VAR_PATTERN.match(env_var):
            raise RequestFailure(
                ProtocolErrorCode.INVALID_PARAMS,
                f"Invalid environment variable name '{env_var}'.",
            )
        if env_var == _PRIMARY_ENV_VAR:
            raise RequestFailure(
                ProtocolErrorCode.INVALID_PARAMS,
                f"'{_PRIMARY_ENV_VAR}' belongs to your Mistral account. A custom "
                f"provider needs its own variable, such as {_default_env_var(name)}.",
            )

        orchestrator = await self._orchestrator()
        if any(provider.name == name for provider in orchestrator.config.providers):
            raise RequestFailure(
                ProtocolErrorCode.INVALID_PARAMS,
                f"Provider '{name}' already exists. Use `/provider set-key` to "
                "change its key, or remove it first.",
            )

        provider = ProviderConfig(
            name=name,
            api_base=api_base,
            api_key_env_var=env_var,
            api_style=params.api_style,
            backend=Backend.MISTRAL if params.backend == "mistral" else Backend.GENERIC,
            # A custom base is reached on purpose: tenant reconciliation must
            # never rewrite it to the Mistral account's own host.
            preserve_api_base=True,
        )
        if not await apply_provider_to_config(
            orchestrator, provider, reason=f"Add provider {name}"
        ):
            raise RequestFailure(
                ProtocolErrorCode.INTERNAL_ERROR,
                f"Could not save provider '{name}' to config.toml.",
            )

        key_outcome: str | None = None
        if params.api_key and params.api_key.strip():
            key_outcome = persist_api_key(
                provider, params.api_key.strip(), orchestrator=orchestrator
            )

        discovered: list[str] = []
        fetch_error: str | None = None
        if params.fetch_models:
            try:
                discovered = await self._discover(orchestrator, provider)
            except RequestFailure as exc:
                # The provider is saved either way; the caller shows why the
                # listing failed and can retry with `/provider fetch-models`.
                fetch_error = str(exc)

        await orchestrator.reload()
        return ProvidersAddResponse(
            provider=await self._summarize(orchestrator.config, provider),
            key_outcome=key_outcome,
            discovered_models=discovered,
            fetch_error=fetch_error,
        )

    async def _set_key(self, params: ProvidersSetKeyParams) -> ProvidersSetKeyResponse:
        name = _normalize_name(params.name)
        if name == _PRIMARY_PROVIDER:
            raise RequestFailure(
                ProtocolErrorCode.INVALID_PARAMS,
                "Use `vibe --setup` to change your Mistral account credential.",
            )
        orchestrator = await self._orchestrator()
        provider = next(
            (p for p in orchestrator.config.providers if p.name == name), None
        )
        if provider is None:
            raise RequestFailure(
                ProtocolErrorCode.NOT_FOUND,
                f"Provider '{name}' is not configured. Add it with "
                f"`/provider add {name} <api_base>`.",
            )
        if not provider.api_key_env_var:
            provider = provider.model_copy(
                update={"api_key_env_var": _default_env_var(name)}
            )
            if not await apply_provider_to_config(
                orchestrator, provider, reason=f"Name the key of provider {name}"
            ):
                raise RequestFailure(
                    ProtocolErrorCode.INTERNAL_ERROR,
                    f"Could not save provider '{name}' to config.toml.",
                )

        api_key = params.api_key.strip()
        if not api_key:
            raise RequestFailure(
                ProtocolErrorCode.INVALID_PARAMS, "The API key must not be empty."
            )
        outcome = persist_api_key(provider, api_key, orchestrator=orchestrator)
        return ProvidersSetKeyResponse(
            provider=await self._summarize(orchestrator.config, provider),
            outcome=outcome,
        )

    async def _fetch_models(
        self, params: ProvidersFetchModelsParams
    ) -> ProvidersFetchModelsResponse:
        name = _normalize_name(params.name)
        orchestrator = await self._orchestrator()
        provider = next(
            (p for p in orchestrator.config.providers if p.name == name), None
        )
        if provider is None:
            raise RequestFailure(
                ProtocolErrorCode.NOT_FOUND, f"Provider '{name}' is not configured."
            )
        discovered = await self._discover(orchestrator, provider)
        await orchestrator.reload()
        return ProvidersFetchModelsResponse(
            provider=await self._summarize(orchestrator.config, provider),
            discovered_models=discovered,
        )

    async def _add_model(
        self, params: ProvidersAddModelParams
    ) -> ProvidersAddModelResponse:
        provider_name = _normalize_name(params.provider)
        orchestrator = await self._orchestrator()
        if not any(p.name == provider_name for p in orchestrator.config.providers):
            raise RequestFailure(
                ProtocolErrorCode.NOT_FOUND,
                f"Provider '{provider_name}' is not configured.",
            )
        model_name = params.model.strip()
        if not model_name:
            raise RequestFailure(
                ProtocolErrorCode.INVALID_PARAMS, "A model name is required."
            )
        alias = (params.alias or "").strip() or _model_alias(provider_name, model_name)
        display_name = (params.display_name or "").strip() or (
            f"{model_name} ({provider_name})"
        )
        try:
            model = ModelConfig(
                name=model_name,
                provider=provider_name,
                alias=alias,
                display_name=display_name,
                input_price=params.input_price or 0.0,
                output_price=params.output_price or 0.0,
                max_context_length=params.max_context_length,
                max_output_tokens=params.max_output_tokens,
                supports_images=params.supports_images,
            )
        except ValidationError as exc:
            raise RequestFailure(
                ProtocolErrorCode.INVALID_PARAMS, f"Invalid model configuration: {exc}"
            ) from exc

        failures = await orchestrator.apply_patch(
            [
                AddOperationPatch(
                    path=f"/models/{escape_json_pointer_token(alias)}",
                    value=_model_payload(model),
                )
            ],
            reason=f"Add model {alias} for provider {provider_name}",
        )
        if failures:
            raise RequestFailure(
                ProtocolErrorCode.INTERNAL_ERROR,
                f"Could not save model '{alias}': {failures[0]}",
            )
        await orchestrator.reload()
        return ProvidersAddModelResponse(
            alias=alias,
            model=model_name,
            provider=provider_name,
            display_name=display_name,
        )

    async def _remove(self, params: ProvidersRemoveParams) -> ProvidersRemoveResponse:
        name = _normalize_name(params.name)
        if name == _PRIMARY_PROVIDER:
            raise RequestFailure(
                ProtocolErrorCode.INVALID_PARAMS,
                "The 'mistral' provider holds your Mistral account and cannot be removed.",
            )
        orchestrator = await self._orchestrator()
        provider = next(
            (p for p in orchestrator.config.providers if p.name == name), None
        )
        if provider is None:
            raise RequestFailure(
                ProtocolErrorCode.NOT_FOUND, f"Provider '{name}' is not configured."
            )

        raw = (await orchestrator.load_persistence_layer()).model_dump()
        providers = raw.get("providers")
        if isinstance(providers, list):
            remaining = [
                entry
                for entry in providers
                if not (isinstance(entry, dict) and entry.get("name") == name)
            ]
            reason = f"Remove provider {name}"
            if remaining:
                await orchestrator.set_field("/providers", remaining, reason=reason)
            else:
                # An empty list would be litter: the built-in providers come
                # from the default layer, so the field is dropped instead.
                await orchestrator.apply_patch(
                    [RemoveOperationPatch(path="/providers")], reason=reason
                )

        removed_models = await self._remove_models(
            orchestrator, name, raw.get("models")
        )

        if params.remove_key and provider.api_key_env_var:
            primary_env_vars = {
                p.api_key_env_var for p in DEFAULT_PROVIDERS if p.api_key_env_var
            }
            if provider.api_key_env_var not in primary_env_vars:
                try:
                    remove_api_key(provider)
                except Exception as exc:
                    # The provider is gone either way; a stale key is reported,
                    # not fatal.
                    logger.warning(
                        "Could not remove the stored key for provider %s: %s", name, exc
                    )

        await orchestrator.reload()
        return ProvidersRemoveResponse(
            name=name, removed=True, removed_models=removed_models
        )

    async def _remove_models(
        self,
        orchestrator: ConfigOrchestrator[VibeConfigSchema],
        provider_name: str,
        persisted: object,
    ) -> list[str]:
        """Drop the provider's models from the writable layer; report their aliases.

        ``models`` must not be empty, so removing the last one removes the field
        rather than writing an empty collection: the lower layers still define
        the built-in models.
        """
        if isinstance(persisted, dict):
            removed = [
                alias
                for alias, entry in persisted.items()
                if isinstance(entry, dict) and entry.get("provider") == provider_name
            ]
            if removed:
                remaining: object = {
                    alias: entry
                    for alias, entry in persisted.items()
                    if alias not in removed
                }
                await self._write_models(orchestrator, remaining, provider_name)
            return removed
        if isinstance(persisted, list):
            removed = [
                str(entry.get("alias") or entry.get("name"))
                for entry in persisted
                if isinstance(entry, dict) and entry.get("provider") == provider_name
            ]
            if removed:
                remaining_list: object = [
                    entry
                    for entry in persisted
                    if not (
                        isinstance(entry, dict)
                        and entry.get("provider") == provider_name
                    )
                ]
                await self._write_models(orchestrator, remaining_list, provider_name)
            return removed
        return []

    async def _write_models(
        self,
        orchestrator: ConfigOrchestrator[VibeConfigSchema],
        remaining: object,
        provider_name: str,
    ) -> None:
        """Write the surviving models, or drop the field when none survive."""
        reason = f"Remove models of provider {provider_name}"
        if remaining:
            await orchestrator.set_field("/models", remaining, reason=reason)
            return
        await orchestrator.apply_patch(
            [RemoveOperationPatch(path="/models")], reason=reason
        )

    async def _discover(
        self,
        orchestrator: ConfigOrchestrator[VibeConfigSchema],
        provider: ProviderConfig,
    ) -> list[str]:
        """Register every model ``provider`` lists; return their aliases."""
        key = (
            resolve_api_key(provider.api_key_env_var)
            if provider.api_key_env_var
            else None
        )
        entries = await _fetch_models(provider, key)
        if not entries:
            return []

        aliases: list[str] = []
        patches: list[PatchOp] = []
        for entry in entries:
            alias = _model_alias(provider.name, entry["id"])
            payload: dict[str, Any] = {
                "name": entry["id"],
                "provider": provider.name,
                "alias": alias,
                "display_name": entry["display_name"]
                or f"{entry['id']} ({provider.name})",
                "max_context_length": entry["max_context_length"]
                or _DEFAULT_CONTEXT_WINDOW,
            }
            if entry["supports_images"] is not None:
                payload["supports_images"] = entry["supports_images"]
            patches.append(
                AddOperationPatch(
                    path=f"/models/{escape_json_pointer_token(alias)}", value=payload
                )
            )
            aliases.append(alias)

        failures = await orchestrator.apply_patch(
            patches, reason=f"Discover models of provider {provider.name}"
        )
        if failures:
            raise RequestFailure(
                ProtocolErrorCode.INTERNAL_ERROR,
                f"Could not save the discovered models: {failures[0]}",
            )
        return aliases


def _model_payload(model: ModelConfig) -> dict[str, Any]:
    """The minimal model entry a durable layer can reconstruct.

    Identity plus what the caller stated; ``exclude_defaults`` keeps today's
    defaults out of the user's config.toml.
    """
    payload = model.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    payload["name"] = model.name
    payload["provider"] = model.provider
    payload["alias"] = model.alias
    payload["display_name"] = model.display_name
    return payload
