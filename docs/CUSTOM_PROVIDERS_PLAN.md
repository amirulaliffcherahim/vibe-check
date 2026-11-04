# Plan: Multi-Provider Mistral-Compatible API Support (Alongside Mistral Subscription Pro)

**Branch:** `dev-py` (derived from `main` / `v3.0.4`)  
**Goal:** Allow Mistral Vibe to keep the primary **Mistral Subscription Pro / Le Chat account** connected while simultaneously adding one or more **custom Mistral/OpenAI-compatible HTTP providers** (with dedicated API keys, auto-discovered `/v1/models`, custom model registration, and per-model token/cost usage tracking — similar to Pi).

---

## 1. Why Stock Vibe Blocks This Today

1. **Single-Provider Onboarding (`vibe --setup`)**:
   - The onboarding wizard only edits the primary `mistral` provider (`MISTRAL_API_KEY` or browser OAuth). Switching from Subscription Pro to an API key overwrites the `mistral` credential.
2. **Model Catalog Hardcoded to `"mistral"` (`vibe/core/config/layers/model_catalog.py`)**:
   - `ModelCatalogLayer` hardcodes `_CATALOG_PROVIDER = "mistral"` and only fetches from the primary Mistral provider.
3. **No Slash Command for Custom Providers (`vibe/cli-rust/src/commands/`)**:
   - While `VibeConfigSchema.providers` supports union-merging `[[providers]]` by `name` and `models` by `alias`, there is no `/provider` command or RPC family to add a provider, store its API key in the OS keyring / `~/.vibe/.env`, auto-discover its `/v1/models`, and reload the live `/model` picker.

---

## 2. Architecture & Design

### 2.1 Zero Interference with Mistral Subscription Pro
- The built-in `name = "mistral"` provider (`Backend.MISTRAL`, `MISTRAL_API_KEY` / OAuth token) is **protected**:
  - `get_mistral_provider()` in `vibe/core/config/vibe_schema.py` continues to resolve the `Backend.MISTRAL` provider for `/whoami`, Subscription Pro plan badge, voice (`voxtral`), and telemetry.
  - Custom providers use `Backend.GENERIC` (or `Backend.MISTRAL` when pointing at a secondary Mistral workspace/proxy) with `preserve_api_base = True` so `/whoami` tenant domain reconciliation (`vibe/app_server/_account.py:98`) never overwrites a custom provider's `api_base`.
  - Each custom provider gets its own isolated environment variable name (default `<NAME>_API_KEY`, e.g., `OPENROUTER_API_KEY`, `MISTRAL_API_API_KEY`, `DEEPSEEK_API_KEY`), stored via `persist_api_key()` in OS keyring (or `~/.vibe/.env` fallback).

### 2.2 Model Discovery & Naming (`<provider>/<model>`)
- **Auto-discovery (`GET {api_base}/models`)**:
  - Queries `{api_base}/models` with `Authorization: Bearer <key>` and optional `extra_headers`.
  - Parses both OpenAI-style `{"data": [{"id": ...}]}` and Mistral-style `{"data": [{"id": ..., "max_context_length": ..., "capabilities": {"vision": ...}}]}` responses.
  - Persists discovered models into `config.toml` with:
    - `alias = "<provider>/<model_id>"` (so `/model` picker clearly groups/labels them and avoids colliding with built-in Mistral aliases)
    - `display_name = "<model_id> (<provider>)"`
    - `provider = "<provider>"`
    - `max_context_length` (from response or `131072` default)
    - `supports_images` (from `capabilities.vision` if present)
- **Manual Model Registration & Pricing (`/provider add-model`)**:
  - Allows registering a specific model ID with custom `input_price` ($/1M tokens), `output_price` ($/1M tokens), `cached_input_price`, and `max_context_length`.
- **Usage & Cost Tracking**:
  - `UnifiedHarnessBackendAdapter` and `RuntimeSnapshot` (`vibe/app_server/_runtime.py:2568`) already read `active_model.input_price`, `active_model.output_price`, and `active_model.cached_input_price` into `AgentStatsSnapshot`, which powers the bottom bar token counter (`13k/800k tokens`) and `/status` (`Session Prompt Tokens`, `Session Completion Tokens`, `Cost: $0.0000`).

---

## 3. Implementation Steps

### Step 1: Protocol Wire Models (`vibe/app_server/protocol.py`)
Add 6 RPC methods to `SERVER_METHODS` and define their Pydantic wire models:
- `providers/list` → `ProvidersListParams` / `ProvidersListResponse(providers: list[ProviderSummaryView])`
  - `ProviderSummaryView`: `name`, `api_base` (redacted via `sanitize_api_base`), `backend`, `api_style`, `api_key_env_var`, `has_api_key`, `credential_source` (`os_keyring` / `vibe_home_env_file` / `process_env`), `models: list[str]`, `is_default: bool`.
- `providers/add` → `ProvidersAddParams(name, api_base, api_key?, api_key_env_var?, api_style="openai", backend="generic", fetch_models=True)` / `ProvidersAddResponse`
- `providers/setKey` → `ProvidersSetKeyParams(name, api_key)` / `ProvidersSetKeyResponse`
- `providers/fetchModels` → `ProvidersFetchModelsParams(name)` / `ProvidersFetchModelsResponse`
- `providers/addModel` → `ProvidersAddModelParams(provider, model, alias?, display_name?, max_context_length?, max_output_tokens?, input_price?, output_price?, supports_images?)` / `ProvidersAddModelResponse`
- `providers/remove` → `ProvidersRemoveParams(name, remove_key=True)` / `ProvidersRemoveResponse`

### Step 2: App Server Controller (`vibe/app_server/_custom_providers.py`)
Create `ProvidersController` and wire it into `HostRequestHandler` (`vibe/app_server/_host.py`) and `AppServer` (`vibe/app_server/server.py`):
- **Validation**:
  - Refuse overwriting or deleting `"mistral"` (`_PROTECTED_PROVIDERS`).
  - Refuse `api_key_env_var == "MISTRAL_API_KEY"` on custom providers so the Subscription Pro / primary Mistral key is never clobbered.
  - Validate `api_base` scheme (`http` / `https`) and hostname.
- **Persistence**:
  - Use `apply_provider_to_config(orchestrator, provider)` (`vibe/setup/auth/api_key_persistence.py:88`) to upsert into `/providers` in `~/.vibe/config.toml`.
  - Use `persist_api_key(provider, api_key, orchestrator=orchestrator)` to store the secret in OS keyring (or `~/.vibe/.env` fallback) and set `os.environ[env_var]` for immediate use in the running server process.
  - Use `AddOperationPatch(path=f"/models/{escape_json_pointer_token(alias)}", value=payload)` to persist discovered or manually added models.
- **Live Session Reload**:
  - When a session is attached in `server.py`, after `providers/add`, `providers/fetchModels`, `providers/addModel`, or `providers/remove` mutates `config.toml`, trigger `root.reload_config(ConfigReloadParams(session_id=root.session_id))` (or return the refreshed `RuntimeSnapshot`) so the TUI's `/model` picker updates immediately without restarting Vibe.

### Step 3: Rust TUI `/provider` Slash Command (`vibe/cli-rust/`)
1. **Register `/provider` in `vibe/cli-rust/src/commands/commands.rs`**:
   ```rust
   cmd(
       &["/provider", "/providers"],
       "Manage custom Mistral/OpenAI-compatible providers, API keys, and models",
       false,
   )
   .args("[list | add <name> <api_base> [api_key] | set-key <name> <key> | fetch-models <name> | add-model <provider> <model> [--input-price X] [--output-price Y] | remove <name>]")
   ```
2. **Add RPC method constants in `vibe/cli-rust/src/server/types.rs`**:
   - `PROVIDERS_LIST = "providers/list"`
   - `PROVIDERS_ADD = "providers/add"`
   - `PROVIDERS_SET_KEY = "providers/setKey"`
   - `PROVIDERS_FETCH_MODELS = "providers/fetchModels"`
   - `PROVIDERS_ADD_MODEL = "providers/addModel"`
   - `PROVIDERS_REMOVE = "providers/remove"`
3. **Implement handler in `vibe/cli-rust/src/commands/provider.rs`** (and wire in `dispatch.rs`):
   - `/provider` or `/provider list`: Calls `providers/list` and renders a Markdown summary table in the transcript showing each provider, its `api_base`, auth status (`✓ OS keyring`, `✓ ~/.vibe/.env`, or `✗ missing`), and model count.
   - `/provider add <name> <api_base> [api_key] [--backend generic|mistral] [--no-fetch]`: Calls `providers/add`, then calls `config/reload` on the active session so `CommandEvent::Runtime` updates `app.model_picker.models` in place.
   - `/provider set-key <name> <api_key>`: Calls `providers/setKey` (and clears input history or masks key if needed).
   - `/provider fetch-models <name>`: Calls `providers/fetchModels` + `config/reload`, reporting how many models were added to `/model`.
   - `/provider add-model <provider> <model> [--alias <alias>] [--context <tokens>] [--input-price <usd_per_m>] [--output-price <usd_per_m>]`: Calls `providers/addModel` + `config/reload`.
   - `/provider remove <name>`: Calls `providers/remove` + `config/reload`.

### Step 4: Verification & Build Gate
1. **Python Unit Tests (`tests/app_server/test_custom_providers.py`)**:
   - Adding a custom provider persists to `config.toml` and stores key in keyring/`.env` without touching `mistral`.
   - Mock `/v1/models` HTTP response auto-registers `<provider>/<model>` entries into `config.models`.
   - Adding a model with `input_price` / `output_price` computes token cost in `AgentStatsSnapshot`.
   - Removing a custom provider removes its `[[providers]]` entry, its models, and its key.
   - Attempting to overwrite or remove `"mistral"` or reuse `"MISTRAL_API_KEY"` is rejected with `INVALID_PARAMS`.
2. **Lint & Typecheck**:
   - `uv run ruff check --fix . && uv run ruff format .`
   - `uv run pyright`
   - `uv run pytest tests/app_server/test_custom_providers.py`
3. **Rust TUI Build & Test**:
   - `cargo test --manifest-path vibe/cli-rust/Cargo.toml --test units`
   - `cargo build --release --manifest-path vibe/cli-rust/Cargo.toml --bin vibe-rs`
   - Copy or point `~/.local/bin/vibe` to the updated build so `/provider` and `/model` work live in your terminal.

## Update: provider types

See `docs/CUSTOM_PROVIDERS_PLAN_2.md`. `/provider add` takes
`--type openai|mistral|openai-responses|reasoning|anthropic`. The Mistral
account is always the provider named `mistral`
(`VibeConfigSchema.get_mistral_provider`), so a Mistral-compatible custom
provider is used for completions only.
