# Plan 2: Mistral-compatible and OpenAI-compatible providers from `/provider`

**Branch:** `dev-py`. **Depends on:** `docs/CUSTOM_PROVIDERS_PLAN.md` (already
executed; uncommitted on `dev-py`).

**For the executor:** follow the steps in order. Every edit gives the exact file,
the exact text to find, and the exact replacement. Do not redesign. If a "find"
string is not present, stop and report; do not improvise. Run the gate at the end
of each step before starting the next.

---

## 0. What already works and what does not

### Works today (no change needed)

**OpenAI-compatible** providers (OpenRouter, DeepSeek, Together, vLLM, llama.cpp,
LM Studio, Ollama). `/provider add` always sends `backend = "generic"`,
`api_style = "openai"`, which is exactly OpenAI chat-completions:

```
/provider add openrouter https://openrouter.ai/api/v1 sk-or-...
/provider add local http://127.0.0.1:8080/v1
```

### Does not work today

1. **No way to pick the wire format from the CLI.** `ProvidersAddParams` already
   accepts `backend` and `api_style`
   (`vibe/app_server/protocol.py`, class `ProvidersAddParams`), but
   `vibe/cli-rust/src/commands/provider.rs` `fn add` never sends them. So a
   Mistral-protocol endpoint (a second Mistral workspace key, a Mistral-API
   proxy, a self-hosted Mistral server) cannot be added from `/provider`.

2. **`backend = "mistral"` on a custom provider would hijack the account.**
   `VibeConfigSchema.get_mistral_provider()`
   (`vibe/core/config/vibe_schema.py`) returns the *active* provider when its
   backend is `mistral`, else the first `mistral`-backend provider in the list.
   About 20 callers use it for Mistral-account features: `/whoami`, identity,
   plan badge, telemetry, model catalog, experiments, org config, connectors,
   skills registry, utility completions. If a user selects a model on a custom
   `backend = "mistral"` provider, all of those would send the custom key to
   Mistral's own endpoints, or read the wrong account. Step 1 fixes this.

3. **No validation of `api_style`.** Any string is persisted, then the session
   fails at open with `no completion adapter for backend ... with API style ...`.
   The accepted values are the keys of `_GENERIC_WIRES` in
   `harness/runtimes/python/python/mistralai_vibe_local_harness/vibe/_route_profile.py`:
   `openai`, `reasoning`, `openai-responses`, `anthropic`, `vertex-anthropic`.
   `vertex-anthropic` needs Google ADC and a project id, which `/provider` does
   not collect, so it is refused here.

### Target behaviour after this plan

```
# OpenAI-compatible (default, unchanged)
/provider add openrouter https://openrouter.ai/api/v1 sk-or-...

# Mistral-compatible (new)
/provider add mistral-work https://api.mistral.ai/v1 sk-... --type mistral
/provider add my-mistral-proxy https://proxy.corp/v1 sk-... --type mistral

# Other OpenAI-family wire formats (new)
/provider add oai https://api.openai.com/v1 sk-... --type openai-responses
/provider add claude https://api.anthropic.com/v1 sk-ant-... --type anthropic
```

`--type` maps to `(backend, api_style)`:

| `--type`           | backend   | api_style          |
|--------------------|-----------|--------------------|
| `openai` (default) | `generic` | `openai`           |
| `openai-responses` | `generic` | `openai-responses` |
| `reasoning`        | `generic` | `reasoning`        |
| `anthropic`        | `generic` | `anthropic`        |
| `mistral`          | `mistral` | `openai` (ignored by the Mistral backend) |

---

## Step 1: Keep the account on the primary `mistral` provider

**Why:** problem 2 above. A custom `backend = "mistral"` provider must be usable
for completions without ever becoming "the Mistral account".

**Rule:** the account provider is the one named `mistral`. Only when no provider
is named `mistral` does the old behaviour (active, then first `mistral`-backend)
apply, so existing configs that renamed or replaced it keep working.

### 1a. Edit `vibe/core/config/vibe_schema.py`

Find:

```python
    def get_mistral_provider(self) -> ProviderConfig | None:
        try:
            active_provider = self.get_active_provider()
            if active_provider.backend == Backend.MISTRAL:
                return active_provider
        except ValueError:
            pass
        return next((p for p in self.providers if p.backend == Backend.MISTRAL), None)
```

Replace with:

```python
    def get_mistral_provider(self) -> ProviderConfig | None:
        """The provider that holds the user's Mistral account.

        The provider named ``mistral`` when it uses the Mistral backend: a
        custom provider added with ``/provider add --type mistral`` speaks the
        Mistral API but is not the account, so it never receives account-scoped
        calls (``/whoami``, telemetry, the model catalog, experiments).

        Without such a provider, the active provider when it uses the Mistral
        backend, else the first that does, as before.
        """
        primary = next(
            (
                provider
                for provider in self.providers
                if provider.name == PRIMARY_MISTRAL_PROVIDER
                and provider.backend == Backend.MISTRAL
            ),
            None,
        )
        if primary is not None:
            return primary
        try:
            active_provider = self.get_active_provider()
            if active_provider.backend == Backend.MISTRAL:
                return active_provider
        except ValueError:
            pass
        return next((p for p in self.providers if p.backend == Backend.MISTRAL), None)
```

### 1b. Add the constant in the same file

Find the line:

```python
DEFAULT_PROVIDERS = [
```

Insert **immediately above** it:

```python
# The provider that holds the user's Mistral account. A custom provider may use
# the Mistral backend, but only this one receives account-scoped calls.
PRIMARY_MISTRAL_PROVIDER = "mistral"

```

### 1c. Export it

`vibe/core/config/__init__.py` exports lazily, so the name goes in **three**
places. Edit each:

1. The `TYPE_CHECKING` import block (around line 91). Find:

   ```python
           DEFAULT_TTS_PROVIDERS,
           EnvValueOrigin,
   ```

   Replace with:

   ```python
           DEFAULT_TTS_PROVIDERS,
           PRIMARY_MISTRAL_PROVIDER,
           EnvValueOrigin,
   ```

2. `__all__` (around line 124). Find `    "DEFAULT_PROVIDERS",` inside `__all__`
   and add `    "PRIMARY_MISTRAL_PROVIDER",` on the line after it. (Ruff will
   re-sort `__all__` in the gate if its order differs.)

3. The lazy-import map (around line 318). Find:

   ```python
       "DEFAULT_PROVIDERS": ("vibe.core.config.vibe_schema", "DEFAULT_PROVIDERS"),
   ```

   Replace with:

   ```python
       "DEFAULT_PROVIDERS": ("vibe.core.config.vibe_schema", "DEFAULT_PROVIDERS"),
       "PRIMARY_MISTRAL_PROVIDER": (
           "vibe.core.config.vibe_schema",
           "PRIMARY_MISTRAL_PROVIDER",
       ),
   ```

Verify with:

```powershell
uv run python -c "from vibe.core.config import PRIMARY_MISTRAL_PROVIDER; print(PRIMARY_MISTRAL_PROVIDER)"
```

Expected output: `mistral`.

### 1d. Leave `is_active_model_mistral()` unchanged

It answers "does the active model run on a Mistral backend", which is still true
for a custom Mistral provider and is what its callers (feedback, Vibe Code,
account upsell) mean by it. Do not edit it.

### 1e. Use the constant in `vibe/app_server/_custom_providers.py`

Find:

```python
_PRIMARY_PROVIDER = "mistral"
```

Replace with:

```python
_PRIMARY_PROVIDER = PRIMARY_MISTRAL_PROVIDER
```

In the `from vibe.core.config import (` block of the same file, add
`PRIMARY_MISTRAL_PROVIDER,` (alphabetical: after `ModelConfig`, before
`ProviderConfig`).

### 1f. Tests: add to `tests/core/config/test_config_resolution.py`

The class is `TestGetMistralProvider` (around line 1903). Its tests take the
`make_config` fixture and already import `ProviderConfig`, `ModelConfig`,
`Backend`, `VibeConfigSchema`, and `Callable`. Add these three methods at the
**end of that class** (after `test_returns_none_when_no_mistral_provider` and its
body), indented as class methods:

```python
    def test_primary_named_provider_wins_over_an_active_custom_mistral_provider(
        self, make_config: Callable[..., VibeConfigSchema]
    ) -> None:
        primary = ProviderConfig(
            name="mistral",
            api_base="https://api.mistral.ai/v1",
            api_key_env_var="MISTRAL_API_KEY",
            backend=Backend.MISTRAL,
        )
        work = ProviderConfig(
            name="mistral-work",
            api_base="https://proxy.example.com/v1",
            api_key_env_var="MISTRAL_WORK_API_KEY",
            backend=Backend.MISTRAL,
        )
        cfg = make_config(
            providers=[work, primary],
            models=[
                ModelConfig(
                    name="mistral-large", provider="mistral-work", alias="work-large"
                )
            ],
            active_model="work-large",
        )
        provider = cfg.get_mistral_provider()
        assert provider is not None
        assert provider.name == "mistral"

    def test_primary_named_provider_must_use_the_mistral_backend(
        self, make_config: Callable[..., VibeConfigSchema]
    ) -> None:
        not_mistral = ProviderConfig(
            name="mistral",
            api_base="http://127.0.0.1:8080/v1",
            api_key_env_var="",
            backend=Backend.GENERIC,
        )
        other = ProviderConfig(
            name="other",
            api_base="https://api.mistral.ai/v1",
            api_key_env_var="OTHER_API_KEY",
            backend=Backend.MISTRAL,
        )
        cfg = make_config(
            providers=[not_mistral, other],
            models=[ModelConfig(name="local", provider="mistral", alias="local")],
            active_model="local",
        )
        provider = cfg.get_mistral_provider()
        assert provider is other

    def test_without_a_primary_the_active_mistral_provider_is_used(
        self, make_config: Callable[..., VibeConfigSchema]
    ) -> None:
        work = ProviderConfig(
            name="work",
            api_base="https://api.mistral.ai/v1",
            api_key_env_var="WORK_API_KEY",
            backend=Backend.MISTRAL,
        )
        home = ProviderConfig(
            name="home",
            api_base="https://api.mistral.ai/v1",
            api_key_env_var="HOME_API_KEY",
            backend=Backend.MISTRAL,
        )
        cfg = make_config(
            providers=[work, home],
            models=[ModelConfig(name="m", provider="home", alias="home-m")],
            active_model="home-m",
        )
        provider = cfg.get_mistral_provider()
        assert provider is home
```

If `make_config` merges the given `providers` with the defaults, the default
`mistral` provider may also be present: then test 2 and test 3 will fail because
the default `mistral` (backend mistral) wins. In that case, check how
`test_returns_none_when_no_mistral_provider` gets a config with no Mistral
provider at all (it passes only `llamacpp` and expects `None`), which proves
`make_config` **replaces** the provider list. Proceed only if that test passes
on the unmodified code.

### Gate for step 1

```powershell
uv run pytest tests/core/config/test_config_resolution.py tests/core/experiments tests/app_server/test_connector_catalog.py tests/app_server/test_unified_harness_experiments.py -q
uv run pytest tests/app_server/test_custom_providers.py -q
uv run pyright vibe/core/config/vibe_schema.py vibe/app_server/_custom_providers.py
```

All must pass with 0 pyright errors. If an existing test fails, read it: it is
only allowed to fail if it asserted that an active non-`mistral`-named
`mistral`-backend provider wins over a provider named `mistral`. Report it; do
not change the rule.

---

## Step 2: Validate the wire format on the server

### 2a. Edit `vibe/app_server/protocol.py`, class `ProvidersAddParams`

`api_style: str = "openai"` appears twice in this file (also in
`ProviderSummaryView`, which must stay `str`). Match the **four-line block
below**, which is unique to `ProvidersAddParams`. Find:

```python
    api_style: str = "openai"
    backend: Literal["generic", "mistral"] = "generic"
    # Query GET {api_base}/models and register what it lists.
    fetch_models: bool = True
```

Replace with:

```python
    # The wire format of a generic provider. Ignored by the Mistral backend.
    api_style: Literal["openai", "openai-responses", "reasoning", "anthropic"] = (
        "openai"
    )
    # ``mistral`` speaks the Mistral API (a second workspace key, a proxy, a
    # self-hosted server); it never becomes the Mistral account.
    backend: Literal["generic", "mistral"] = "generic"
    # Query GET {api_base}/models and register what it lists.
    fetch_models: bool = True
```

This makes pydantic reject any other `api_style` as `invalid_params` naming
`apiStyle`. `vertex-anthropic` is deliberately absent: it needs a project id and
Google ADC, which `/provider` does not collect.

### 2b. Edit `vibe/app_server/_custom_providers.py`, method `_add`

Find:

```python
            api_style=params.api_style or "openai",
```

Replace with:

```python
            api_style=params.api_style,
```

### 2c. Discovery must not send a Mistral-style catalogue call to an Anthropic endpoint

Anthropic lists models at `GET {api_base}/models` too, and authenticates with
`x-api-key`, not `Authorization: Bearer`. In `_fetch_models` find:

```python
    headers: dict[str, str] = dict(provider.extra_headers)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
```

Replace with:

```python
    headers: dict[str, str] = dict(provider.extra_headers)
    if api_key:
        if provider.api_style == "anthropic" and provider.backend != Backend.MISTRAL:
            headers["x-api-key"] = api_key
            headers.setdefault("anthropic-version", "2023-06-01")
        else:
            headers["Authorization"] = f"Bearer {api_key}"
```

`Backend` is already imported in this file.

### 2d. Show the type in the list view

In `vibe/app_server/protocol.py`, class `ProviderSummaryView`, the fields
`backend` and `api_style` already exist. No change.

### 2e. Tests: add to `tests/app_server/test_custom_providers.py`

Add these five tests at the end of the file, before
`test_discovered_entries_reads_both_shapes`. Use the existing
`providers_client`, `_stub_models`, `_persisted`, `BASE` helpers.

```python
@pytest.mark.asyncio
async def test_add_a_mistral_compatible_provider(
    providers_client: AppServerClient,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
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
```

### Gate for step 2

```powershell
uv run pytest tests/app_server/test_custom_providers.py -q
uv run ruff check --fix vibe/app_server tests/app_server/test_custom_providers.py
uv run ruff format vibe/app_server tests/app_server/test_custom_providers.py
uv run pyright vibe/app_server/_custom_providers.py vibe/app_server/protocol.py tests/app_server/test_custom_providers.py
```

Expect all tests to pass (14 existing + 5 new functions, parametrised to 11 new
cases = 25) and 0 pyright errors.

---

## Step 3: `--type` on `/provider add` in the Rust TUI

### 3a. Edit `vibe/cli-rust/src/commands/provider.rs`, function `add`

Replace the whole body of `fn add(app: &mut App, client: &Arc<Client>, rest: &str)`
up to and including the `let params = json!({ ... });` statement. Find:

```rust
fn add(app: &mut App, client: &Arc<Client>, rest: &str) {
    let mut parts = rest.split_whitespace();
    let (Some(name), Some(api_base)) = (parts.next(), parts.next()) else {
        reply(
            app,
            CommandEvent::Error(
                "Usage: `/provider add <name> <api_base> [api_key]`".to_owned(),
            ),
        );
        return;
    };
    let api_key = parts.next().map(str::to_owned);
    let name = name.to_owned();
    let params = json!({
        "name": name,
        "apiBase": api_base,
        "apiKey": api_key,
        "fetchModels": true,
    });
```

Replace with:

```rust
fn add(app: &mut App, client: &Arc<Client>, rest: &str) {
    let parsed = match parse_add(rest) {
        Ok(parsed) => parsed,
        Err(message) => {
            reply(app, CommandEvent::Error(message));
            return;
        }
    };
    let name = parsed.name.clone();
    let params = json!({
        "name": parsed.name,
        "apiBase": parsed.api_base,
        "apiKey": parsed.api_key,
        "backend": parsed.kind.backend(),
        "apiStyle": parsed.kind.api_style(),
        "fetchModels": parsed.fetch_models,
    });
```

Leave the `mutate(app, client, method::PROVIDERS_ADD, params, move |value| { ... });`
call that follows untouched.

### 3b. Add the parser and the type enum

Add this code to the same file, directly **above** `fn add`:

```rust
const ADD_USAGE: &str = "Usage: `/provider add <name> <api_base> [api_key] \
[--type openai|openai-responses|reasoning|anthropic|mistral] [--no-fetch]`";

/// The wire format a provider speaks, as `--type` names it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProviderKind {
    OpenAi,
    OpenAiResponses,
    Reasoning,
    Anthropic,
    Mistral,
}

impl ProviderKind {
    pub fn parse(value: &str) -> Option<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "openai" | "openai-compatible" => Some(Self::OpenAi),
            "openai-responses" | "responses" => Some(Self::OpenAiResponses),
            "reasoning" => Some(Self::Reasoning),
            "anthropic" => Some(Self::Anthropic),
            "mistral" | "mistral-compatible" => Some(Self::Mistral),
            _ => None,
        }
    }

    /// The server's `backend`.
    pub fn backend(self) -> &'static str {
        match self {
            Self::Mistral => "mistral",
            _ => "generic",
        }
    }

    /// The server's `apiStyle`; the Mistral backend ignores it.
    pub fn api_style(self) -> &'static str {
        match self {
            Self::OpenAi | Self::Mistral => "openai",
            Self::OpenAiResponses => "openai-responses",
            Self::Reasoning => "reasoning",
            Self::Anthropic => "anthropic",
        }
    }
}

/// A parsed `/provider add`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AddArgs {
    pub name: String,
    pub api_base: String,
    pub api_key: Option<String>,
    pub kind: ProviderKind,
    pub fetch_models: bool,
}

/// Parse `<name> <api_base> [api_key] [--type T] [--no-fetch]`; flags may come
/// anywhere after the command word.
pub fn parse_add(rest: &str) -> Result<AddArgs, String> {
    let mut positionals: Vec<&str> = Vec::new();
    let mut kind = ProviderKind::OpenAi;
    let mut fetch_models = true;
    let mut words = rest.split_whitespace();
    while let Some(word) = words.next() {
        match word {
            "--type" => {
                let Some(value) = words.next() else {
                    return Err(ADD_USAGE.to_owned());
                };
                kind = ProviderKind::parse(value).ok_or_else(|| {
                    format!("Unknown provider type `{value}`.\n\n{ADD_USAGE}")
                })?;
            }
            "--no-fetch" => fetch_models = false,
            flag if flag.starts_with("--") => {
                return Err(format!("Unknown option `{flag}`.\n\n{ADD_USAGE}"));
            }
            other => positionals.push(other),
        }
    }
    let (Some(name), Some(api_base)) = (positionals.first(), positionals.get(1)) else {
        return Err(ADD_USAGE.to_owned());
    };
    if positionals.len() > 3 {
        return Err(ADD_USAGE.to_owned());
    }
    Ok(AddArgs {
        name: (*name).to_owned(),
        api_base: (*api_base).to_owned(),
        api_key: positionals.get(2).map(|key| (*key).to_owned()),
        kind,
        fetch_models,
    })
}
```

### 3c. Show the type in `/provider list`

In `render_providers`, find:

```rust
        lines.push(format!("- **Backend**: `{}`", field("backend")));
```

Replace with:

```rust
        let kind = if field("backend") == "mistral" {
            "Mistral-compatible".to_owned()
        } else {
            match field("apiStyle") {
                "openai" | "" => "OpenAI-compatible".to_owned(),
                other => format!("OpenAI-family (`{other}`)"),
            }
        };
        lines.push(format!("- **Type**: {kind}"));
```

### 3d. Update the `USAGE` constant

Find:

```rust
- `/provider add <name> <api_base> [api_key]`: add a provider, then list its models\n\
```

Replace with:

```rust
- `/provider add <name> <api_base> [api_key] [--type T] [--no-fetch]`: add a provider, then list its models. `T` is `openai` (default), `mistral`, `openai-responses`, `reasoning`, or `anthropic`\n\
```

### 3e. Update the command hint in `vibe/cli-rust/src/commands/commands.rs`

Find:

```rust
.args("[list] | add <name> <api_base> [api_key] | set-key <name> <key> | fetch-models <name> | add-model <provider> <model> | remove <name>"),
```

Replace with:

```rust
.args("[list] | add <name> <api_base> [api_key] [--type openai|mistral|anthropic] | set-key <name> <key> | fetch-models <name> | add-model <provider> <model> | remove <name>"),
```

### 3f. Rust unit tests: create `vibe/cli-rust/tests/units/provider_args.rs`

```rust
//! `/provider add` argument parsing.

use vibe_rs::commands::provider::{parse_add, ProviderKind};

#[test]
fn defaults_to_openai_compatible_and_fetches_models() {
    let args = parse_add("proxy https://h/v1 sk-1").unwrap();
    assert_eq!(args.name, "proxy");
    assert_eq!(args.api_base, "https://h/v1");
    assert_eq!(args.api_key.as_deref(), Some("sk-1"));
    assert_eq!(args.kind, ProviderKind::OpenAi);
    assert!(args.fetch_models);
}

#[test]
fn the_key_is_optional() {
    let args = parse_add("local http://127.0.0.1:8080/v1").unwrap();
    assert_eq!(args.api_key, None);
}

#[test]
fn type_mistral_selects_the_mistral_backend() {
    let args = parse_add("work https://api.mistral.ai/v1 sk --type mistral").unwrap();
    assert_eq!(args.kind, ProviderKind::Mistral);
    assert_eq!(args.kind.backend(), "mistral");
    assert_eq!(args.kind.api_style(), "openai");
}

#[test]
fn flags_may_come_before_the_positionals() {
    let args = parse_add("--type anthropic --no-fetch claude https://h/v1").unwrap();
    assert_eq!(args.kind, ProviderKind::Anthropic);
    assert_eq!(args.kind.backend(), "generic");
    assert_eq!(args.kind.api_style(), "anthropic");
    assert!(!args.fetch_models);
    assert_eq!(args.name, "claude");
}

#[test]
fn every_type_maps_to_a_server_value() {
    for (word, backend, style) in [
        ("openai", "generic", "openai"),
        ("openai-compatible", "generic", "openai"),
        ("openai-responses", "generic", "openai-responses"),
        ("responses", "generic", "openai-responses"),
        ("reasoning", "generic", "reasoning"),
        ("anthropic", "generic", "anthropic"),
        ("mistral", "mistral", "openai"),
        ("Mistral-Compatible", "mistral", "openai"),
    ] {
        let kind = ProviderKind::parse(word).unwrap_or_else(|| panic!("{word}"));
        assert_eq!(kind.backend(), backend, "{word}");
        assert_eq!(kind.api_style(), style, "{word}");
    }
}

#[test]
fn rejects_an_unknown_type_a_missing_value_and_an_unknown_flag() {
    assert!(parse_add("p https://h/v1 --type grpc").unwrap_err().contains("grpc"));
    assert!(parse_add("p https://h/v1 --type").is_err());
    assert!(parse_add("p https://h/v1 --bogus").unwrap_err().contains("--bogus"));
}

#[test]
fn rejects_missing_positionals_and_extra_ones() {
    assert!(parse_add("").is_err());
    assert!(parse_add("only-a-name").is_err());
    assert!(parse_add("p https://h/v1 key extra").is_err());
}
```

### 3g. Register the test module

In `vibe/cli-rust/tests/units.rs` (or `vibe/cli-rust/tests/units/main.rs`,
whichever exists; find the file that has lines like `mod launch;`), add
`mod provider_args;` in alphabetical order among the other `mod` lines.

### 3h. Make `provider` reachable from tests

The test imports `vibe_rs::commands::provider`. `commands/mod.rs` already has
`pub mod provider;`. Confirm `vibe/cli-rust/src/lib.rs` has `pub mod commands;`.
If it does not, stop and report.

### Gate for step 3

```powershell
cargo test --manifest-path vibe/cli-rust/Cargo.toml --test units provider_args
cargo test --manifest-path vibe/cli-rust/Cargo.toml --test units
cargo clippy --manifest-path vibe/cli-rust/Cargo.toml --lib -- -D warnings
```

- `provider_args`: 7 tests pass.
- Full `units`: **1814 + 7 = 1821 passed, 18 failed** (the 18 are pre-existing
  Windows failures; the set must be identical to before). Compare with:

  ```powershell
  cargo test --manifest-path vibe/cli-rust/Cargo.toml --test units 2>&1 | Select-String -- "--- FAILED"
  ```

- `clippy --lib`: exactly the 5 pre-existing errors in `update.rs`,
  `event_handler.rs`, `connect.rs`, `grouping.rs`, `logging.rs`; none in
  `provider.rs` or `commands.rs`.

---

## Step 4: Document it

### 4a. `vibe/plugins/builtins/vibe/skills/vibe/SKILL.md`

Find the bullet that starts with `` - `/provider` (alias `/providers`) ``.
Inside it, find:

```
[api_key]` registers a provider and lists its models;
```

Replace with:

```
[api_key] [--type T]` registers a provider and lists its models, where `T` is
  `openai` (OpenAI-compatible, the default), `mistral` (Mistral-compatible: a
  second Mistral workspace key, a proxy, or a self-hosted server; it never
  becomes your Mistral account), `openai-responses`, `reasoning`, or
  `anthropic`; `--no-fetch` skips listing models;
```

Do not write `/models` or any other `/word` inside backticks in this bullet: a
test reads every backticked `/word` as a slash command
(`builtin_skill_commands::every_documented_command_is_registered`).

### 4b. `docs/CUSTOM_PROVIDERS_PLAN.md`

Append a section:

```markdown
## Update: provider types

See `docs/CUSTOM_PROVIDERS_PLAN_2.md`. `/provider add` takes
`--type openai|mistral|openai-responses|reasoning|anthropic`. The Mistral
account is always the provider named `mistral`
(`VibeConfigSchema.get_mistral_provider`), so a Mistral-compatible custom
provider is used for completions only.
```

### Gate for step 4

```powershell
cargo test --manifest-path vibe/cli-rust/Cargo.toml --test units builtin_skill_commands
```

3 tests pass.

---

## Step 5: Final verification

Run all of these from the repo root. Every line must match.

```powershell
uv run ruff check .                     # All checks passed!
uv run ruff format --check .            # N files already formatted
uv run pyright                          # 31 errors (the pre-existing baseline), not more
uv run pytest tests/app_server/test_custom_providers.py -q        # all pass
uv run pytest tests/core/config/test_config_resolution.py -q      # all pass
cargo test --manifest-path vibe/cli-rust/Cargo.toml --test units  # 1821 passed, 18 failed
cargo build --release --manifest-path vibe/cli-rust/Cargo.toml --bin vibe-rs
```

Then install and try it:

```powershell
Copy-Item .\vibe\cli-rust\target\release\vibe-rs.exe "$env:USERPROFILE\.local\bin\vibe.exe" -Force
vibe
```

In the TUI:

```
/provider add mistral-work https://api.mistral.ai/v1 <key> --type mistral
/provider add openrouter https://openrouter.ai/api/v1 <key>
/provider
/model
/whoami        # must still show your Mistral account, not mistral-work
```

`/whoami` is the check that matters for step 1: it must keep reporting the
account behind the provider named `mistral`.

---

## Things the executor must NOT do

- Do not change `is_active_model_mistral()`.
- Do not add `vertex-anthropic` to the accepted `api_style` values.
- Do not write to `~/.vibe/config.toml` by hand, and never print it: it holds
  MCP bearer tokens.
- Do not commit. Report the gate results; the user decides when to commit.
- If any "find" text is missing, stop and report the file and the step.
