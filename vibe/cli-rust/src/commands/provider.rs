//! `/provider`: custom Mistral-compatible providers beside the Mistral account.
//!
//! Every mutation ends with `config/reload`, so a provider's models appear in
//! `/model` without restarting Vibe.

use std::sync::Arc;

use serde_json::{json, Value};

use super::event::{dispatch, reload_result, CommandEvent};
use crate::app::App;
use crate::server::request_error::describe;
use crate::server::{method, Client};

const USAGE: &str = "## /provider\n\n\
- `/provider` or `/provider list`: show every provider and its models\n\
- `/provider add <name> <api_base> [api_key] [--type T] [--no-fetch]`: add a provider, then list its models. `T` is `openai` (default), `mistral`, `openai-responses`, `reasoning`, or `anthropic`\n\
- `/provider set-key <name> <key>`: store a provider's API key\n\
- `/provider fetch-models <name>`: register every model the provider lists\n\
- `/provider add-model <provider> <model> [--alias A] [--context N] [--input-price N] [--output-price N]`\n\
- `/provider remove <name>`: remove a provider, its models, and its key";

/// Run a `/provider` subcommand. `value` is the whole command line.
pub fn run(app: &mut App, client: &Arc<Client>, value: &str) {
    let args = arguments(value);
    let (subcommand, rest) = match args.split_once(char::is_whitespace) {
        Some((subcommand, rest)) => (subcommand, rest.trim()),
        None => (args, ""),
    };
    match subcommand {
        "" | "list" => list(app, client),
        "add" => add(app, client, rest),
        "set-key" | "setkey" => set_key(app, client, rest),
        "fetch-models" | "fetchmodels" | "fetch" => fetch_models(app, client, rest),
        "add-model" | "addmodel" => add_model(app, client, rest),
        "remove" | "rm" => remove(app, client, rest),
        "help" | "--help" | "-h" => reply(app, CommandEvent::Result(USAGE.to_owned())),
        other => reply(
            app,
            CommandEvent::Error(format!(
                "Unknown `/provider` subcommand `{other}`.\n\n{USAGE}"
            )),
        ),
    }
}

/// The text after the command word.
fn arguments(value: &str) -> &str {
    value
        .split_once(char::is_whitespace)
        .map(|(_, rest)| rest)
        .unwrap_or_default()
        .trim()
}

fn reply(app: &mut App, event: CommandEvent) {
    let Some(tx) = app.command_tx.clone() else {
        return;
    };
    app.commit_started();
    let _ = tx.try_send(event);
}

fn list(app: &mut App, client: &Arc<Client>) {
    let Some((_session_id, tx)) = dispatch(app) else {
        return;
    };
    let client = client.clone();
    tokio::spawn(async move {
        let event = match client.request(method::PROVIDERS_LIST, json!({})).await {
            Ok(value) => CommandEvent::Result(render_providers(&value)),
            Err(error) => CommandEvent::Error(format!(
                "Failed to read providers: {}",
                describe(&error)
            )),
        };
        let _ = tx.try_send(event);
    });
}

/// The `/provider list` body.
fn render_providers(value: &Value) -> String {
    let providers = value
        .get("providers")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if providers.is_empty() {
        return "## Providers\n\nNo providers are configured.".to_owned();
    }
    let mut lines = vec!["## Providers".to_owned(), String::new()];
    for provider in providers {
        let field = |key: &str| provider.get(key).and_then(Value::as_str).unwrap_or("");
        let name = field("name");
        let marker = if provider.get("isDefault").and_then(Value::as_bool) == Some(true) {
            " (Mistral account)"
        } else {
            ""
        };
        lines.push(format!("### `{name}`{marker}"));
        lines.push(String::new());
        lines.push(format!("- **API base**: `{}`", field("apiBase")));
        let kind = if field("backend") == "mistral" {
            "Mistral-compatible".to_owned()
        } else {
            match field("apiStyle") {
                "openai" | "" => "OpenAI-compatible".to_owned(),
                other => format!("OpenAI-family (`{other}`)"),
            }
        };
        lines.push(format!("- **Type**: {kind}"));
        let env_var = field("apiKeyEnvVar");
        if env_var.is_empty() {
            lines.push("- **Credential**: none required".to_owned());
        } else {
            let state = if provider.get("hasApiKey").and_then(Value::as_bool) == Some(true) {
                match field("credentialSource") {
                    "os_keyring" => "stored in the OS keyring".to_owned(),
                    "vibe_home_env_file" => "stored in `~/.vibe/.env`".to_owned(),
                    "process_env" => "from the process environment".to_owned(),
                    _ => "present".to_owned(),
                }
            } else {
                format!("missing, set `{env_var}` or run `/provider set-key {name} <key>`")
            };
            lines.push(format!("- **Credential** (`{env_var}`): {state}"));
        }
        let models = provider
            .get("models")
            .and_then(Value::as_array)
            .map(|models| {
                models
                    .iter()
                    .filter_map(Value::as_str)
                    .map(|model| format!("`{model}`"))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        if models.is_empty() {
            lines.push("- **Models**: none registered".to_owned());
        } else {
            lines.push(format!("- **Models** ({}): {}", models.len(), models.join(", ")));
        }
        lines.push(String::new());
    }
    lines.push(
        "Switch models with `/model`. Add another provider with \
         `/provider add <name> <api_base> [api_key]`."
            .to_owned(),
    );
    lines.join("\n")
}

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
                kind = ProviderKind::parse(value)
                    .ok_or_else(|| format!("Unknown provider type `{value}`.\n\n{ADD_USAGE}"))?;
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
    mutate(app, client, method::PROVIDERS_ADD, params, move |value| {
        let discovered = value
            .get("discoveredModels")
            .and_then(Value::as_array)
            .map(|models| {
                models
                    .iter()
                    .filter_map(Value::as_str)
                    .map(|model| format!("`{model}`"))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let mut lines = vec![format!("Added provider `{name}`.")];
        if let Some(error) = value.get("fetchError").and_then(Value::as_str) {
            lines.push(format!(
                "Could not list its models: {error}\n\
                 Run `/provider fetch-models {name}` once it is reachable."
            ));
        } else if discovered.is_empty() {
            lines.push(format!(
                "The endpoint listed no models. Add one with \
                 `/provider add-model {name} <model>`."
            ));
        } else {
            lines.push(format!(
                "Registered {} model(s): {}",
                discovered.len(),
                discovered.join(", ")
            ));
            lines.push("Pick one with `/model`.".to_owned());
        }
        if let Some(outcome) = value.get("keyOutcome").and_then(Value::as_str) {
            if outcome != "completed" {
                lines.push(format!(
                    "The API key was not stored ({outcome}); \
                     set it with `/provider set-key {name} <key>`."
                ));
            }
        }
        lines.join("\n\n")
    });
}

fn set_key(app: &mut App, client: &Arc<Client>, rest: &str) {
    let mut parts = rest.split_whitespace();
    let (Some(name), Some(api_key)) = (parts.next(), parts.next()) else {
        reply(
            app,
            CommandEvent::Error("Usage: `/provider set-key <name> <key>`".to_owned()),
        );
        return;
    };
    let name = name.to_owned();
    let params = json!({"name": name, "apiKey": api_key});
    mutate(app, client, method::PROVIDERS_SET_KEY, params, move |value| {
        let outcome = value.get("outcome").and_then(Value::as_str).unwrap_or("");
        if outcome == "completed" {
            format!("Stored the API key for `{name}`.")
        } else {
            format!(
                "Could not store the API key for `{name}` ({outcome}). \
                 Check the OS keyring, or set the variable in `~/.vibe/.env`."
            )
        }
    });
}

fn fetch_models(app: &mut App, client: &Arc<Client>, rest: &str) {
    let name = rest.split_whitespace().next().unwrap_or_default();
    if name.is_empty() {
        reply(
            app,
            CommandEvent::Error("Usage: `/provider fetch-models <name>`".to_owned()),
        );
        return;
    }
    let name = name.to_owned();
    let params = json!({"name": name});
    mutate(
        app,
        client,
        method::PROVIDERS_FETCH_MODELS,
        params,
        move |value| {
            let discovered = value
                .get("discoveredModels")
                .and_then(Value::as_array)
                .map(|models| {
                    models
                        .iter()
                        .filter_map(Value::as_str)
                        .map(|model| format!("`{model}`"))
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            if discovered.is_empty() {
                format!("`{name}` listed no models.")
            } else {
                format!(
                    "Registered {} model(s) from `{name}`: {}\n\nPick one with `/model`.",
                    discovered.len(),
                    discovered.join(", ")
                )
            }
        },
    );
}

fn add_model(app: &mut App, client: &Arc<Client>, rest: &str) {
    let mut positionals: Vec<&str> = Vec::new();
    let mut alias: Option<&str> = None;
    let mut context: Option<u64> = None;
    let mut input_price: Option<f64> = None;
    let mut output_price: Option<f64> = None;

    let mut words = rest.split_whitespace();
    while let Some(word) = words.next() {
        match word {
            "--alias" => alias = words.next(),
            "--context" => context = words.next().and_then(|value| value.parse().ok()),
            "--input-price" => input_price = words.next().and_then(|value| value.parse().ok()),
            "--output-price" => output_price = words.next().and_then(|value| value.parse().ok()),
            other => positionals.push(other),
        }
    }
    let (Some(provider), Some(model)) = (
        positionals.first().map(|value| (*value).to_owned()),
        positionals.get(1).map(|value| (*value).to_owned()),
    ) else {
        reply(
            app,
            CommandEvent::Error(
                "Usage: `/provider add-model <provider> <model> \
                 [--alias A] [--context N] [--input-price N] [--output-price N]`"
                    .to_owned(),
            ),
        );
        return;
    };
    let params = json!({
        "provider": provider,
        "model": model,
        "alias": alias,
        "maxContextLength": context,
        "inputPrice": input_price,
        "outputPrice": output_price,
    });
    mutate(
        app,
        client,
        method::PROVIDERS_ADD_MODEL,
        params,
        move |value| {
            let alias = value
                .get("alias")
                .and_then(Value::as_str)
                .map(str::to_owned)
                .unwrap_or(model);
            format!("Registered `{alias}`.\n\nPick it with `/model`.")
        },
    );
}

fn remove(app: &mut App, client: &Arc<Client>, rest: &str) {
    let name = rest.split_whitespace().next().unwrap_or_default();
    if name.is_empty() {
        reply(
            app,
            CommandEvent::Error("Usage: `/provider remove <name>`".to_owned()),
        );
        return;
    }
    let name = name.to_owned();
    let params = json!({"name": name, "removeKey": true});
    mutate(app, client, method::PROVIDERS_REMOVE, params, move |value| {
        let removed = value
            .get("removedModels")
            .and_then(Value::as_array)
            .map(|models| {
                models
                    .iter()
                    .filter_map(Value::as_str)
                    .map(|model| format!("`{model}`"))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        if removed.is_empty() {
            format!("Removed provider `{name}` and its stored key.")
        } else {
            format!(
                "Removed provider `{name}`, its stored key, and {} model(s): {}",
                removed.len(),
                removed.join(", ")
            )
        }
    });
}

/// Call one provider mutation, then reload config so `/model` sees the change.
fn mutate<F>(
    app: &mut App,
    client: &Arc<Client>,
    rpc: &'static str,
    params: Value,
    describe_result: F,
) where
    F: FnOnce(&Value) -> String + Send + 'static,
{
    let Some((session_id, tx)) = dispatch(app) else {
        return;
    };
    let client = client.clone();
    tokio::spawn(async move {
        let value = match client.request(rpc, params).await {
            Ok(value) => value,
            Err(error) => {
                let _ = tx.try_send(CommandEvent::Error(describe(&error)));
                return;
            }
        };
        let text = describe_result(&value);
        // The models and providers the runtime offers changed: reload so the
        // `/model` picker and the footer read the new config.
        let reload = client
            .request(
                method::CONFIG_RELOAD,
                json!({"sessionId": session_id, "reloadRuntime": true}),
            )
            .await;
        let event = match reload {
            Ok(runtime) => match reload_result(runtime) {
                // Keep the runtime so the picker refreshes, and report what
                // changed.
                CommandEvent::Runtime(value, _) => CommandEvent::Runtime(value, text),
                other => other,
            },
            // The mutation landed; only the refresh failed.
            Err(error) => CommandEvent::Result(format!(
                "{text}\n\nReload the config to see the change: {}",
                describe(&error)
            )),
        };
        let _ = tx.try_send(event);
    });
}
