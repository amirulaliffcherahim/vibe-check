# Plan 3: Provider labels in `/model`, and a scoped model list

**Branch:** `dev-py`. **Depends on:** `docs/CUSTOM_PROVIDERS_PLAN.md` (executed,
uncommitted) and `docs/CUSTOM_PROVIDERS_PLAN_2.md` (**must be executed first**:
this plan reads the provider `backend` and `api_style` that plan 2 makes
settable, and uses `PRIMARY_MISTRAL_PROVIDER`).

**For the executor:** follow the steps in order. Every edit gives the file, the
exact text to find, and the exact replacement. Do not redesign. If a "find"
string is missing, stop and report the step and file; do not improvise. Run each
step's gate before the next step. Do not commit.

---

## 0. What the user asked for

1. **Every row in `/model` shows where it runs**, in parentheses, as the
   provider's **name** and nothing else: `(mistral)`, `(shy)`, `(openrouter)`,
   `(claude)`. No type suffix, no decoration. The name is whatever the provider
   was added as, so a Mistral-compatible provider shows its own name like any
   other.
2. **A scoped model list**, so `/model` stays short: the user stars the models
   they use, and `/model` shows only those by default. One key switches to
   **all** models and back.

### Definitions used below

- **Scoped list**: the aliases in a new config field `scoped_models`. Empty
  means "no scope": `/model` shows all models, exactly as today.
- **Provider label**: the name of the provider a model runs on, which the
  server sends per model as `ModelConfigView.provider_label`. The client only
  renders it (ADR 0002: the server owns config; the TUI renders).

### What is out of scope

- No change to which models a session may *run*. Scoping is display-only.
  `allowed_models` (admin policy) is the enforcement mechanism and stays as is.
- No change to the `/config` model picker's contents (it lists every model).
- No new slash command. Scope is edited inside `/model`.

---

## 1. Design (read once; no decisions left for the executor)

### 1.1 Labels (server side)

`provider_label` for a model is the **name** of its provider
(`config.get_provider_for_model(model).name`):

| Provider | Label |
|---|---|
| `mistral` (the account) | `mistral` |
| `shy` (Mistral-compatible) | `shy` |
| `openrouter` (OpenAI-compatible) | `openrouter` |
| `claude` (Anthropic wire) | `claude` |
| provider not found (bad config) | `None` (no label) |

Nothing else is appended: the backend and `api_style` do not appear in the
label. The provider's type stays visible in `/provider list`, which is where a
user checks it.

### 1.2 Display name (client side)

The row text is `"{display_name} ({provider_label})"`, or just `display_name`
when there is no label. **Strip a trailing `(… )` the display name already
carries** when it equals the label, so plan-1 models named
`"llama-3.1-8b (proxy)"` do not render as `llama-3.1-8b (proxy) (proxy)`.

### 1.3 Scoped list (config + server)

- New field on `VibeConfigSchema`: `scoped_models: list[str]`, replace-merge,
  default empty. Each entry is a model **alias**.
- New field on `ModelConfigView`: `scoped: bool` — true when the alias is in
  `scoped_models`.
- New field on `ConfigView`: `model_scope_active: bool` — true when
  `scoped_models` names at least one alias that exists in the available models.
  If every listed alias is gone (provider removed), the scope is inactive and
  `/model` shows all models: the picker is never empty.
- Writes go through the **existing** `config/write` RPC (`/scoped_models`,
  `op: "set"`), saved to the user config like every other `/model` pick. No new
  RPC.

### 1.4 Picker behaviour (client side)

- `ModelPicker` gets `show_all: bool`, reset to `false` each time `/model`
  opens.
- Visible rows = Default row + (all models if `show_all` or the scope is
  inactive; else only `scoped` models).
- **The active model is always visible**, even when not scoped, so the `›`
  marker and Enter never point at a hidden row.
- Keys inside `/model`:
  - `Tab` toggles `show_all`.
  - `*` stars/unstars the highlighted model (adds/removes its alias in
    `scoped_models` via `config/write`). Not on the Default row.
- The title becomes `Select Model (scoped · Tab for all)` when filtered,
  `Select Model (all · Tab for scoped)` when showing all with a scope active,
  and stays `Select Model` when there is no scope.
- A scoped row shows a `★` after its label when `show_all` is on, so the user
  sees what is scoped while browsing all.
- Hint line adds `(Tab, "all/scoped")` and `("*", "scope")`.

### 1.5 Index safety

Today `model_picker.rs` indexes `app.model_picker.models` directly by
`selected - 1`. After filtering, the visible list and `models` differ. **All
row ↔ model mapping goes through one function** `visible_models(app)` returning
`Vec<&ModelOption>`. Every place that today does `models[i - 1]` or
`models.get(sel - 1)` or `models.len()` switches to it. Step 6 lists each one.

---

## Step 1: `scoped_models` config field

### 1a. `vibe/core/config/vibe_schema.py`

Find:

```python
    allowed_models: Annotated[list[str], WithReplaceMerge()] = Field(
```

Insert **immediately above** it:

```python
    scoped_models: Annotated[list[str], WithReplaceMerge()] = Field(
        default_factory=list,
        description=(
            "Model aliases the /model picker shows by default, to keep the list"
            " short. Empty shows every model. Display only: any configured model"
            " can still be picked after pressing Tab in /model."
        ),
    )
```

### 1b. Schema snapshot

The JSON schema served by `config/schema` changes. Run:

```powershell
uv run pytest tests/core/config/test_vibe_config_schema.py -q
```

If a snapshot/golden test fails only because `scoped_models` is new, update that
snapshot the way its file says (look for an `--update` / `UPDATE_SNAPSHOTS` /
`regenerate` note at the top of the failing test file). If nothing says how,
stop and report the test name.

### 1c. Settings Manager / Le Chat allowlist note (AGENTS.md rule)

AGENTS.md requires checking whether a new config field needs the Settings
Manager managed-config UI or Le Chat Web's allowlist updated. `scoped_models` is
a personal display preference, not an org policy: **no update needed**. Record
that sentence in the final report.

### Gate for step 1

```powershell
uv run pytest tests/core/config/test_vibe_config_schema.py tests/core/config/test_config_resolution.py -q
uv run python -c "from vibe.core.config import VibeConfigSchema as V; print(V.model_fields['scoped_models'].default_factory())"
```

Expected: tests pass; prints `[]`.

---

## Step 2: Server projection (`provider_label`, `scoped`, `model_scope_active`)

### 2a. `vibe/app_server/config.py`, class `ModelConfigView`

Find:

```python
    # The group the model is listed under, from the highest-priority layer that
    # defines it; None from an older server (ADR 0014) or when no known layer
    # defines it.
    source: ModelSource | None = None
```

Replace with:

```python
    # The group the model is listed under, from the highest-priority layer that
    # defines it; None from an older server (ADR 0014) or when no known layer
    # defines it.
    source: ModelSource | None = None
    # Where the model runs, as `/model` shows it in parentheses: `mistral` for
    # the Mistral account, else the provider's name, with its type when it is
    # not OpenAI-compatible. None when the provider is not configured.
    provider_label: str | None = None
    # Whether the alias is in `scoped_models`; `/model` shows scoped models
    # first and, by default, only them.
    scoped: bool = False
```

### 2b. `vibe/app_server/config.py`, class `ConfigView`

Find (these two lines are adjacent in `ConfigView`):

```python
    default_model_alias: str
    default_agent: str = "accept-edits"
```

Replace with:

```python
    default_model_alias: str
    # True when `scoped_models` names at least one available model; `/model`
    # then shows only scoped models until Tab switches to all. Defaulted so an
    # older client tolerates its absence (ADR 0014).
    model_scope_active: bool = False
    default_agent: str = "accept-edits"

### 2c. `vibe/app_server/_projection.py`: label helper

Add this function directly **above** `def _project_model_config(`:

```python
def provider_label(config: VibeConfigSchema, model: ModelConfig) -> str | None:
    """Where ``model`` runs, as `/model` shows it: the provider's name.

    None when the provider is missing, so a stale model entry shows no label
    rather than a wrong one.
    """
    try:
        provider = config.get_provider_for_model(model)
    except ValueError:
        return None
    return provider.name
```

No new imports are needed: `VibeConfigSchema` and `ModelConfig` are already
imported in `_projection.py`, and this function uses no other type.

### 2d. `vibe/app_server/_projection.py`: pass the new fields

Find:

```python
def _project_model_config(
    model: ModelConfig,
    *,
    image_fallback: bool,
    source: ModelSource | None,
    route_facts: tuple[int | None, ThinkingLevel, list[ThinkingLevel]],
) -> ModelConfigView:
```

Replace with:

```python
def _project_model_config(
    model: ModelConfig,
    *,
    image_fallback: bool,
    source: ModelSource | None,
    route_facts: tuple[int | None, ThinkingLevel, list[ThinkingLevel]],
    provider_label: str | None = None,
    scoped: bool = False,
) -> ModelConfigView:
```

Then find, at the end of the same function:

```python
        thinking_levels=thinking_levels,
        source=source,
    )
```

Replace with:

```python
        thinking_levels=thinking_levels,
        source=source,
        provider_label=provider_label,
        scoped=scoped,
    )
```

Note: the parameter name `provider_label` shadows the helper function
`provider_label` inside `_project_model_config`. That is fine because the
function body does not call the helper. Do **not** call the helper inside
`_project_model_config`.

### 2e. `vibe/app_server/_projection.py`: compute them in `project_config_view`

In `project_config_view`, find:

```python
    available = config.available_models()
    # Grouped by source; a model no known layer defines keeps its place, last.
    models = [available[alias] for alias in sources if alias in available]
    models += [model for alias, model in available.items() if alias not in sources]
```

Replace with:

```python
    available = config.available_models()
    # Grouped by source; a model no known layer defines keeps its place, last.
    models = [available[alias] for alias in sources if alias in available]
    models += [model for alias, model in available.items() if alias not in sources]
    scoped = {alias for alias in config.scoped_models if alias in available}
```

Then, in the same function, the **active model** projection. Find:

```python
        active_model=_project_model_config(
            active_model,
            image_fallback=image_fallback,
            source=sources.get(active_model.alias),
            route_facts=_route_facts(config, active_model),
        ),
```

Replace with:

```python
        active_model=_project_model_config(
            active_model,
            image_fallback=image_fallback,
            source=sources.get(active_model.alias),
            route_facts=_route_facts(config, active_model),
            provider_label=provider_label(config, active_model),
            scoped=active_model.alias in scoped,
        ),
        model_scope_active=bool(scoped),
```

Then the **models list** projection. Find:

```python
        models=[
            _project_model_config(
                model,
                image_fallback=image_fallback,
                source=sources.get(model.alias),
                route_facts=_route_facts(config, model),
            )
            for model in models
        ],
```

Replace with:

```python
        models=[
            _project_model_config(
                model,
                image_fallback=image_fallback,
                source=sources.get(model.alias),
                route_facts=_route_facts(config, model),
                provider_label=provider_label(config, model),
                scoped=model.alias in scoped,
            )
            for model in models
        ],
```

`model_scope_active` must be passed exactly once in the `ConfigView(...)`
call. It was added in the active-model edit above; do not add it again.

### 2f. Other `_project_model_config` callers

Run:

```powershell
uv run python -c "import subprocess;print(subprocess.run(['git','grep','-n','_project_model_config('],capture_output=True,text=True).stdout)"
```

Any caller other than the two edited above keeps working (the new parameters
default to `None`/`False`). Do not edit them.

### 2g. Tests: create `tests/app_server/test_model_labels_and_scope.py`

```python
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
```

### Gate for step 2

```powershell
uv run pytest tests/app_server/test_model_labels_and_scope.py -q
uv run pytest tests/app_server -q -k "projection or config_view or runtime"
uv run ruff check --fix vibe/app_server tests/app_server/test_model_labels_and_scope.py
uv run ruff format vibe/app_server tests/app_server/test_model_labels_and_scope.py
uv run pyright vibe/app_server/_projection.py vibe/app_server/config.py tests/app_server/test_model_labels_and_scope.py
```

Expected: 5 new tests pass; the `-k` subset has no **new** failures (compare
against `git stash`-ed baseline if anything fails); pyright 0 errors on these
files.

If `make_config(..., scoped_models=[...])` raises because the fixture rejects
unknown kwargs, read `build_test_vibe_config` in `tests/conftest.py`: it passes
kwargs into the schema, so `scoped_models` is accepted once step 1 is done. If
it still fails, stop and report.

---

## Step 3: Rust wire types

### 3a. `vibe/cli-rust/src/server/proto_runtime.rs`, struct `ModelView`

Find:

```rust
pub struct ModelView {
    pub alias: String,
    pub display_name: Option<String>,
    pub thinking: Option<String>,
    pub supports_images: Option<bool>,
    #[serde(deserialize_with = "crate::utils::json::lenient")]
    pub thinking_levels: Vec<String>,
}
```

Replace with:

```rust
pub struct ModelView {
    pub alias: String,
    pub display_name: Option<String>,
    pub thinking: Option<String>,
    pub supports_images: Option<bool>,
    #[serde(deserialize_with = "crate::utils::json::lenient")]
    pub thinking_levels: Vec<String>,
    /// The name of the provider the model runs on (`mistral`, `shy`,
    /// `openrouter`); absent from an older server (ADR 0014).
    pub provider_label: Option<String>,
    /// Whether the alias is in `scoped_models`.
    pub scoped: bool,
}
```

The struct already has `#[serde(default, rename_all = "camelCase")]`, so
`providerLabel` and `scoped` map automatically and older servers deserialize.

### 3b. The config view struct

`ConfigView` is in `vibe/cli-rust/src/server/proto_runtime.rs`. Unlike
`ModelView`, it has **no** struct-level `#[serde(default)]`: each optional field
carries its own `#[serde(default)]`. So the new field needs one too. Find:

```rust
    #[serde(default)]
    pub default_model_alias: String,
```

Replace with:

```rust
    #[serde(default)]
    pub default_model_alias: String,
    /// True when `scoped_models` names an available model; `/model` then
    /// filters to scoped models until Tab. Absent from an older server.
    #[serde(default)]
    pub model_scope_active: bool,
```

### Gate for step 3

```powershell
cargo check --manifest-path vibe/cli-rust/Cargo.toml --lib
```

0 errors.

---

## Step 4: Picker model state

### 4a. `vibe/cli-rust/src/app.rs`, struct `ModelOption`

Find:

```rust
pub struct ModelOption {
    pub alias: String,
    pub display_name: String,
    /// The model's configured thinking level and the levels it offers.
    pub thinking: String,
    pub thinking_levels: Vec<String>,
}
```

Replace with:

```rust
pub struct ModelOption {
    pub alias: String,
    pub display_name: String,
    /// The model's configured thinking level and the levels it offers.
    pub thinking: String,
    pub thinking_levels: Vec<String>,
    /// Where the model runs, shown in parentheses after the name.
    pub provider_label: Option<String>,
    /// In `scoped_models`: shown by default when a scope is active.
    pub scoped: bool,
}
```

### 4b. `vibe/cli-rust/src/app.rs`, struct `ModelPicker`

Find:

```rust
    /// Alias the Default row resolves to (`defaultModelAlias`).
    pub default_alias: String,
}
```

(This is the last field of `ModelPicker`; it is followed by the
`/// The artifacts modal state` comment.) Replace with:

```rust
    /// Alias the Default row resolves to (`defaultModelAlias`).
    pub default_alias: String,
    /// The server says `scoped_models` names an available model.
    pub scope_active: bool,
    /// Show every model instead of only scoped ones; reset when `/model` opens.
    pub show_all: bool,
}
```

### 4c. `vibe/cli-rust/src/model_picker.rs`, `fn model_option`

Find:

```rust
    Some(ModelOption {
        display_name: model.display_name.unwrap_or_else(|| model.alias.clone()),
        alias: model.alias,
        thinking: model.thinking.unwrap_or_default(),
        thinking_levels,
    })
```

Replace with:

```rust
    Some(ModelOption {
        display_name: model.display_name.unwrap_or_else(|| model.alias.clone()),
        alias: model.alias,
        thinking: model.thinking.unwrap_or_default(),
        thinking_levels,
        provider_label: model.provider_label.filter(|label| !label.is_empty()),
        scoped: model.scoped,
    })
```

### 4d. `vibe/cli-rust/src/model_picker.rs`, `fn apply_runtime`

Find:

```rust
    app.model_picker.is_pinned = config.active_model_pinned;
    app.model_picker.default_alias = alias;
}
```

Replace with:

```rust
    app.model_picker.is_pinned = config.active_model_pinned;
    app.model_picker.default_alias = alias;
    app.model_picker.scope_active = config.model_scope_active;
}
```

### 4e. Other `ModelOption { ... }` constructions

Run:

```powershell
Select-String -Path vibe/cli-rust/src/**/*.rs,vibe/cli-rust/tests/**/*.rs -Pattern "ModelOption \{" -List
```

`ModelOption` derives `Default`. For every struct literal found other than the
one in 4c, if it lists fields explicitly and the compiler reports
`missing fields provider_label, scoped`, append `..Default::default()` as the
last line inside that literal. Change nothing else in those files.

### Gate for step 4

```powershell
cargo check --manifest-path vibe/cli-rust/Cargo.toml --lib --tests
```

0 errors.

---

## Step 5: Row labels

### 5a. `vibe/cli-rust/src/model_picker.rs`: add the label function

Add at the end of the file:

```rust
/// The row text: the display name, then where the model runs in parentheses.
///
/// A display name that already ends with ` (<label>)` is not labelled twice
/// (models a provider registered before labels existed are named that way).
pub fn row_label(model: &ModelOption) -> String {
    let Some(label) = model.provider_label.as_deref() else {
        return model.display_name.clone();
    };
    let suffix = format!(" ({label})");
    if model.display_name.ends_with(&suffix) {
        return model.display_name.clone();
    }
    // A name that already ends in this provider, e.g. "llama-3.1-8b (proxy)"
    // from a provider that registered it before labels existed.
    let base = model
        .display_name
        .strip_suffix(&format!(" ({label})"))
        .unwrap_or(&model.display_name);
    format!("{base}{suffix}")
}
```

### 5b. Use it in the picker row and the Default hint

In `vibe/cli-rust/src/ui/model_picker.rs`, `fn draw_option`, find:

```rust
    let label = if i == 0 {
        "Default"
    } else {
        &app.model_picker.models[i - 1].display_name
    };
    f.buffer_mut().set_string(x + 2, y, label, name_style);
```

Replace with:

```rust
    let label = if i == 0 {
        "Default".to_owned()
    } else {
        visible_models(app)
            .get(i - 1)
            .map(|model| crate::model_picker::row_label(model))
            .unwrap_or_default()
    };
    f.buffer_mut().set_string(x + 2, y, &label, name_style);
    if i > 0 && app.model_picker.show_all && app.model_picker.scope_active {
        if visible_models(app).get(i - 1).is_some_and(|model| model.scoped) {
            let star_x = x + 2 + label.chars().count() as u16;
            f.buffer_mut().set_string(star_x, y, " ★", dim);
        }
    }
```

In the same function, the Default-row hint line uses `label.chars().count()`;
it now reads the `String` above, which still works. No other change there.

Add `visible_models` to the `use crate::model_picker::{...}` line at the top of
`vibe/cli-rust/src/ui/model_picker.rs`. Find:

```rust
use crate::model_picker::{is_current, option_count};
```

Replace with:

```rust
use crate::model_picker::{is_current, option_count, visible_models};
```

### 5c. The Default row's `(currently …)` hint

In `vibe/cli-rust/src/model_picker.rs`, `fn apply_runtime`, find:

```rust
    app.model_picker.default_display_name = models
        .iter()
        .find(|model| model.alias == alias)
        .map_or_else(|| alias.clone(), |model| model.display_name.clone());
```

Replace with:

```rust
    app.model_picker.default_display_name = models
        .iter()
        .find(|model| model.alias == alias)
        .map_or_else(|| alias.clone(), row_label);
```

### Gate for step 5

Step 5 does not compile alone (it uses `visible_models`, added in step 6). Run
the gate after step 6.

---

## Step 6: Filtering and index safety

### 6a. `vibe/cli-rust/src/model_picker.rs`: the one visible-list function

Add directly **above** `pub fn option_count(`:

```rust
/// The model rows `/model` shows, in order, after the Default row.
///
/// With an active scope and `show_all` off: only scoped models, plus the
/// active model so the current marker always has a row. Otherwise: all.
pub fn visible_models(app: &App) -> Vec<&ModelOption> {
    let picker = &app.model_picker;
    if !picker.scope_active || picker.show_all {
        return picker.models.iter().collect();
    }
    picker
        .models
        .iter()
        .filter(|model| model.scoped || model.alias == picker.current_model)
        .collect()
}
```

### 6b. Replace every direct index into `models`

In `vibe/cli-rust/src/model_picker.rs`:

1. Find:

   ```rust
   pub fn option_count(app: &App) -> usize {
       app.model_picker.models.len() + 1
   }
   ```

   Replace with:

   ```rust
   pub fn option_count(app: &App) -> usize {
       visible_models(app).len() + 1
   }
   ```

2. Find:

   ```rust
   fn model_index(app: &App) -> Option<usize> {
       app.model_picker
           .models
           .iter()
           .position(|m| m.alias == app.model_picker.current_model)
   }
   ```

   Replace with:

   ```rust
   fn model_index(app: &App) -> Option<usize> {
       visible_models(app)
           .iter()
           .position(|m| m.alias == app.model_picker.current_model)
   }
   ```

3. Find:

   ```rust
       app.model_picker
           .models
           .get(sel - 1)
           .map(|m| m.alias.clone())
           .unwrap_or_default()
   ```

   Replace with:

   ```rust
       visible_models(app)
           .get(sel - 1)
           .map(|m| m.alias.clone())
           .unwrap_or_default()
   ```

4. In `pub fn select(`, the lookup
   `app.model_picker.models.iter().find(|m| m.alias == target)` searches by
   alias, not by index. **Leave it unchanged**: a target hidden by the scope
   (the Default row's model) must still resolve its thinking levels.

5. Search the rest of the crate for other direct indexing:

   ```powershell
   Select-String -Path vibe/cli-rust/src/**/*.rs -Pattern "model_picker\.models\[|model_picker\s*\.models\s*\.get\(|model_picker\.models\.len\(\)"
   ```

   For each hit that maps a **picker row number** to a model, replace it with
   `crate::model_picker::visible_models(app)` the same way. Hits that look a
   model up **by alias** (`.find(` / `.iter().any(` by alias) stay unchanged.
   Report every hit and what you did with it.

### 6c. Reset `show_all` on open

Find:

```rust
pub fn open(app: &mut App) {
    app.model_picker.list = crate::option_list::OptionList::new(current_index(app));
    app.model_picker.open = true;
}
```

Replace with:

```rust
pub fn open(app: &mut App) {
    app.model_picker.show_all = false;
    app.model_picker.list = crate::option_list::OptionList::new(current_index(app));
    app.model_picker.open = true;
}
```

### 6d. `Tab` toggles all/scoped

Add to `vibe/cli-rust/src/model_picker.rs`, below `pub fn cancel(`:

```rust
/// `Tab`: switch between the scoped list and all models, keeping the
/// highlight on the same model when it stays visible.
pub fn toggle_show_all(app: &mut App) {
    if !app.model_picker.scope_active {
        return;
    }
    let highlighted = selected_alias(app);
    app.model_picker.show_all = !app.model_picker.show_all;
    let index = visible_models(app)
        .iter()
        .position(|model| model.alias == highlighted)
        .map_or(0, |i| i + 1);
    app.model_picker.list = crate::option_list::OptionList::new(index);
}
```

### Gate for steps 5 and 6

```powershell
cargo check --manifest-path vibe/cli-rust/Cargo.toml --lib --tests
cargo clippy --manifest-path vibe/cli-rust/Cargo.toml --lib -- -D warnings
```

`check`: 0 errors. `clippy`: only the 5 pre-existing errors (in `update.rs`,
`event_handler.rs`, `connect.rs`, `grouping.rs`, `logging.rs`); none in
`model_picker.rs` or `ui/model_picker.rs`.

---

## Step 7: `*` stars a model (writes `scoped_models`)

### 7a. `vibe/cli-rust/src/model_picker.rs`: the toggle

This reuses the same path the `/config` screen uses to save a field
(`vibe/cli-rust/src/config.rs`, around line 282): `config_write::write` sends
`config/write`, the server re-derives and answers with the fresh runtime even
though `reloadRuntime` is false, and `model_picker::Event::Written(value)` hands
that runtime to `apply_runtime_value`, which refreshes the picker.
`crate::ui_event::spawn` delivers the event on the main thread and holds the
app busy until it lands (`model_picker::Event` already converts into `UiEvent`).

Add below `toggle_show_all`:

```rust
/// `*`: add the highlighted model to `scoped_models`, or remove it.
///
/// Writes the whole list through `config/write`; the runtime the server answers
/// with refreshes the picker. Ignored on the Default row.
pub fn toggle_scoped(app: &mut App, client: &Arc<Client>) {
    let alias = selected_alias(app);
    if alias.is_empty() {
        return;
    }
    let Some(session_id) = app.session.session_id.clone() else {
        return;
    };
    let mut scoped: Vec<String> = app
        .model_picker
        .models
        .iter()
        .filter(|model| model.scoped)
        .map(|model| model.alias.clone())
        .collect();
    match scoped.iter().position(|scoped_alias| *scoped_alias == alias) {
        Some(index) => {
            scoped.remove(index);
        }
        None => scoped.push(alias),
    }
    let ops = vec![crate::config_write::set_op(
        "/scoped_models",
        serde_json::Value::from(scoped),
    )];
    let client = client.clone();
    crate::ui_event::spawn(app, async move {
        match crate::config_write::write(&client, &session_id, ops, "model scope").await {
            Ok(value) => Event::Written(value),
            Err(error) => Event::Failed(format!("Failed to update the model scope: {error}")),
        }
    });
}
```

`set_op` takes `impl Into<Value>`; `serde_json::Value::from(Vec<String>)`
satisfies it. `Arc` and `Client` are already imported in `model_picker.rs`.

### 7b. Do not add a new write helper

`config_write::write` and `config_write::set_op` already exist
(`vibe/cli-rust/src/config_write.rs`). Use them exactly as in 7a. Do not add
`write_field` or any other helper.

### 7c. Key bindings

In `vibe/cli-rust/src/input.rs`, find:

```rust
        KeyCode::Char('s') => model_picker::select(app, client, Scope::Session),
        _ => {}
    }
}

/// The `/thinking` picker owns keys:
```

Replace with:

```rust
        KeyCode::Char('s') => model_picker::select(app, client, Scope::Session),
        KeyCode::Tab => model_picker::toggle_show_all(app),
        KeyCode::Char('*') => model_picker::toggle_scoped(app, client),
        _ => {}
    }
}

/// The `/thinking` picker owns keys:
```

### 7d. Title and hints

In `vibe/cli-rust/src/ui/model_picker.rs`, `fn draw_box`, find:

```rust
    f.buffer_mut().set_string(
        bx + 2,
        by + 1,
        "Select Model",
```

Replace with:

```rust
    let title = match (app.model_picker.scope_active, app.model_picker.show_all) {
        (false, _) => "Select Model",
        (true, false) => "Select Model (scoped · Tab for all)",
        (true, true) => "Select Model (all · Tab for scoped)",
    };
    f.buffer_mut().set_string(
        bx + 2,
        by + 1,
        title,
```

Then find, in the same function:

```rust
    hint_line::draw(f, bx + 2, by + h - 2, hint_line::PICK_HINTS);
```

Replace with:

```rust
    hint_line::draw(f, bx + 2, by + h - 2, hint_line::MODEL_PICK_HINTS);
```

In `vibe/cli-rust/src/ui/hint_line.rs`, find:

```rust
pub(crate) const PICK_HINTS: &[Hint] = &[
    hints::NAVIGATE,
    (key::ENTER, action::SAVE),
    ("s", action::SESSION_ONLY),
    hints::CANCEL,
];
```

Replace with:

```rust
pub(crate) const PICK_HINTS: &[Hint] = &[
    hints::NAVIGATE,
    (key::ENTER, action::SAVE),
    ("s", action::SESSION_ONLY),
    hints::CANCEL,
];

/// `/model`: the pick hints, plus switching the scope and starring a model.
pub(crate) const MODEL_PICK_HINTS: &[Hint] = &[
    hints::NAVIGATE,
    (key::ENTER, action::SAVE),
    ("s", action::SESSION_ONLY),
    (key::TAB, action::ALL_OR_SCOPED),
    ("*", action::SCOPE),
    hints::CANCEL,
];
```

In `vibe/cli-rust/src/hints.rs`, find:

```rust
    pub const SESSION_ONLY: &str = "session only";
```

Replace with:

```rust
    pub const SESSION_ONLY: &str = "session only";
    pub const ALL_OR_SCOPED: &str = "all/scoped";
    pub const SCOPE: &str = "scope";
```

Do not change `PICK_HINTS`: the thinking picker and others still use it.

### Gate for step 7

```powershell
cargo check --manifest-path vibe/cli-rust/Cargo.toml --lib --tests
cargo clippy --manifest-path vibe/cli-rust/Cargo.toml --lib -- -D warnings
```

Same expectations as step 6.

---

## Step 8: Rust unit tests

Create `vibe/cli-rust/tests/units/model_scope.rs`:

```rust
//! `/model` provider labels and the scoped list.

use vibe_rs::app::{App, ModelOption};
use vibe_rs::model_picker::{option_count, row_label, toggle_show_all, visible_models};

fn model(alias: &str, label: Option<&str>, scoped: bool) -> ModelOption {
    ModelOption {
        alias: alias.to_owned(),
        display_name: alias.to_owned(),
        provider_label: label.map(str::to_owned),
        scoped,
        ..ModelOption::default()
    }
}

fn app_with(models: Vec<ModelOption>, current: &str, scope_active: bool) -> App {
    let mut app = App::default();
    app.model_picker.models = models;
    app.model_picker.current_model = current.to_owned();
    app.model_picker.scope_active = scope_active;
    app
}

#[test]
fn a_row_shows_where_the_model_runs() {
    assert_eq!(
        row_label(&model("medium", Some("mistral"), false)),
        "medium (mistral)"
    );
    assert_eq!(row_label(&model("large", Some("shy"), false)), "large (shy)");
    assert_eq!(
        row_label(&model("glm", Some("openrouter"), false)),
        "glm (openrouter)"
    );
    assert_eq!(row_label(&model("bare", None, false)), "bare");
}

#[test]
fn a_name_already_carrying_its_provider_is_not_labelled_twice() {
    let mut named = model("x", Some("proxy"), false);
    named.display_name = "llama-3.1-8b (proxy)".to_owned();
    assert_eq!(row_label(&named), "llama-3.1-8b (proxy)");
}

#[test]
fn without_a_scope_every_model_is_listed() {
    let app = app_with(
        vec![model("a", None, false), model("b", None, false)],
        "a",
        false,
    );
    assert_eq!(visible_models(&app).len(), 2);
    assert_eq!(option_count(&app), 3);
}

#[test]
fn a_scope_lists_only_scoped_models_and_the_active_one() {
    let app = app_with(
        vec![
            model("a", None, true),
            model("b", None, false),
            model("c", None, false),
            model("d", None, true),
        ],
        "c",
        true,
    );
    let aliases: Vec<&str> = visible_models(&app)
        .iter()
        .map(|model| model.alias.as_str())
        .collect();
    assert_eq!(aliases, vec!["a", "c", "d"]);
}

#[test]
fn tab_shows_all_then_scoped_again() {
    let mut app = app_with(
        vec![model("a", None, true), model("b", None, false)],
        "a",
        true,
    );
    assert_eq!(visible_models(&app).len(), 1);
    toggle_show_all(&mut app);
    assert!(app.model_picker.show_all);
    assert_eq!(visible_models(&app).len(), 2);
    toggle_show_all(&mut app);
    assert_eq!(visible_models(&app).len(), 1);
}

#[test]
fn tab_does_nothing_without_a_scope() {
    let mut app = app_with(vec![model("a", None, false)], "a", false);
    toggle_show_all(&mut app);
    assert!(!app.model_picker.show_all);
}
```

Register it: in `vibe/cli-rust/tests/units.rs`, the model modules are
`mod model_enforcement;` (line ~202) and `mod model_picker;` (line ~204). Add
`mod model_scope;` on the line **after** `mod model_picker;`.

`App` derives `Default` (`vibe/cli-rust/src/app.rs`, `#[derive(Default)] pub
struct App`), and 352 existing unit-test call sites use `App::default()`, so the
test above is valid as written. `vibe_rs::app` and `vibe_rs::model_picker` are
both `pub mod` in `lib.rs`. `ModelOption` is `pub` with `#[derive(Clone,
Default)]`, so `..ModelOption::default()` works.

### Gate for step 8

```powershell
cargo test --manifest-path vibe/cli-rust/Cargo.toml --test units model_scope
cargo test --manifest-path vibe/cli-rust/Cargo.toml --test units
```

- `model_scope`: 6 passed.
- Full `units`: the **set of failing tests must be identical** to before this
  plan (18 pre-existing Windows failures). Passed count rises by 6 (plus 7 if
  plan 2's `provider_args` is in). Compare:

  ```powershell
  cargo test --manifest-path vibe/cli-rust/Cargo.toml --test units 2>&1 | Select-String -- "--- FAILED"
  ```

---

## Step 9: Goldens (client-e2e)

The `/model` screen is user-visible and changed (labels, title, hints). AGENTS.md
requires a client-e2e scenario and golden for every user-visible change.

### 9a. Existing goldens will change

Every `/model` golden now shows the new hint line (`Tab all/scoped`,
`* scope`), and rows with a `providerLabel` show it. Existing scenarios do not
send `providerLabel`, so their rows are unchanged; only the **hint line** moves.

### 9b. New scenario: `client-e2e/scenarios/model/model_scope.py`

Copy `client-e2e/scenarios/model/model_source_groups.py` to
`model_scope.py` and change it to:

```python
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
```

### 9c. Generate and review

```powershell
cargo build --manifest-path vibe/cli-rust/Cargo.toml
uv run --no-project --with "pyte==0.8.2" --with "rich==15.0.0" python client-e2e/store_golden.py model/model_scope
```

Then regenerate every `/model` golden whose hint line changed:

```powershell
Get-ChildItem client-e2e/scenarios/model/*.py | Where-Object { $_.Name -notlike "_*" } | ForEach-Object {
  uv run --no-project --with "pyte==0.8.2" --with "rich==15.0.0" python client-e2e/store_golden.py ("model/" + $_.BaseName)
}
```

Review with `git diff --stat client-e2e/goldens/` and open a few `.txt`
captures. **The only allowed change in pre-existing `/model` goldens is the
hint line** (gaining `Tab all/scoped` and `* scope`). If any other line moved,
stop and report the golden name.

Check the new golden's captures read, in order:
1. title `Select Model (scoped · Tab for all)`, rows `Default`,
   `Mistral Medium 3.5 (mistral)`, `large (shy)`;
2. title `Select Model (all · Tab for scoped)`, all four model rows
   (`(mistral)`, `(shy)`, `(openrouter)`, `(claude)`), the two scoped ones
   followed by ` ★`;
3. back to the scoped list.

If the store script's argument format differs (it may take a path, or a name
without the folder), read the top of `client-e2e/store_golden.py` for its
usage and adapt only the argument.

### Gate for step 9

```powershell
cargo build --manifest-path vibe/cli-rust/Cargo.toml
uv run --no-project --with "pyte==0.8.2" --with "rich==15.0.0" --with pytest --with pytest-xdist --with pytest-timeout python -m pytest client-e2e/test_golden.py -k "model" -q
```

All `model` goldens pass. If the golden harness cannot run on this Windows host
(it drives a PTY), say so in the report and list the scenarios you could not
verify; do not mark step 9 done.

---

## Step 10: Documentation

### 10a. `vibe/plugins/builtins/vibe/skills/vibe/SKILL.md`

The `/model` bullet is one long line (around line 1005) that starts with
``- `/model` - Select active model.`` and ends with
`before the working directory gets very short.` Append the text below on new
lines **directly after that line**, each line indented by two spaces so it
continues the same bullet:

```
  Each row ends with the name of the provider it runs on, in parentheses:
  `(mistral)` for your Mistral account, or the provider's own name such as
  `(shy)` or `(openrouter)`. Press `*` to add or remove the highlighted model
  from your scoped list (`scoped_models` in config.toml); while the list is
  non-empty `/model` shows only scoped models and the active one. Press `Tab` to
  switch between scoped and all models.
```

The only backticked slash word in this text is `` `/model` ``, a registered
command. Do not add any other backtick-slash token (the test
`builtin_skill_commands::every_documented_command_is_registered` fails on an
unregistered one, e.g. `` `/models` ``).

### 10b. Config reference in the same file

`allowed_models` appears inside a TOML example block in SKILL.md (around line
302), followed by `[[models]]` entries in the same block. Add the new key as a
TOML comment + line **inside that block, right after `allowed_models`**. Find:

```toml
allowed_models = ["mistral-vibe-cli-*"]

[[models]]
```

Replace with:

```toml
allowed_models = ["mistral-vibe-cli-*"]

# Model aliases /model shows by default, to keep the list short. Display only:
# every model stays selectable after Tab in /model. Empty shows all models.
# Edit it with * in /model.
scoped_models = ["mistral-medium-3.5", "shy-route/large"]

[[models]]
```

The command-doc test (`slash_tokens` in
`vibe/cli-rust/tests/units/builtin_skill_commands.rs`) matches only a backtick
followed directly by `/` (`` `/word ``). This comment has no backticks, so it is
not read as a command. The find text occurs exactly once in SKILL.md.

### Gate for step 10

```powershell
cargo test --manifest-path vibe/cli-rust/Cargo.toml --test units builtin_skill_commands
```

3 passed.

---

## Step 11: Final verification

```powershell
uv run ruff check .                      # All checks passed!
uv run ruff format --check .             # all files already formatted
uv run pyright                           # 31 errors (pre-existing baseline), not more
uv run pytest tests/app_server/test_model_labels_and_scope.py tests/app_server/test_custom_providers.py -q
uv run pytest tests/core/config -q
cargo test --manifest-path vibe/cli-rust/Cargo.toml --test units   # same 18 failures as baseline
cargo build --release --manifest-path vibe/cli-rust/Cargo.toml --bin vibe-rs
```

Install and try it:

```powershell
Copy-Item .\vibe\cli-rust\target\release\vibe-rs.exe "$env:USERPROFILE\.local\bin\vibe.exe" -Force
vibe
```

In the TUI:

```
/provider add shy https://<your-shy-route>/v1 <key> --type mistral
/model                 # rows end in (mistral), (shy), (openrouter), (claude)
*                      # on two rows: star them
Esc, /model            # title says scoped; only starred + active shown
Tab                    # all models, starred ones marked ★
```

## Final report the executor must give

1. Gate results per step (pass counts, the pyright total, the 18-failure set
   unchanged).
2. Confirmation that 7a used `config_write::write` + `set_op` with no new helper.
3. Every hit from 6b.5 and what was done.
4. Whether goldens were verified (step 9) or could not run on this host.
5. The line: "Settings Manager / Le Chat allowlist: no update needed
   (`scoped_models` is a personal display preference)."

## Things the executor must NOT do

- Do not filter models in `available_models()` or anywhere on the server:
  scoping is display-only, the server returns every model.
- Do not change `allowed_models`.
- Do not index `app.model_picker.models` by a picker row number anywhere; use
  `visible_models(app)`.
- Do not change `PICK_HINTS` (shared by other pickers).
- Do not print or hand-edit `~/.vibe/config.toml` (it holds MCP tokens).
- Do not commit.
