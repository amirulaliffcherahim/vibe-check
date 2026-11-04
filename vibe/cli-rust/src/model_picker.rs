//! `/model` picker state: a leading Default
//! (unpinned) row plus one row per configured model, current row marked, no live
//! preview. A pick chains straight into `/thinking`, which writes both at once.

use std::sync::Arc;

use serde_json::Value;

use crate::app::{App, ModelOption, ToastSeverity};
use crate::config_write::Scope;
use crate::event_handler;
use crate::server::Client;
use crate::thinking_picker::{self, ModelPick, THINKING_LEVELS};

/// Alias persisted for the Default (unpinned) row.
pub const UNPINNED_ACTIVE_MODEL: &str = "";

/// A `/config` field write's server answer, applied on the main thread.
pub enum Event {
    /// Runtime returned by `config/write`.
    Written(Value),
    /// The write failed; the message names the field and the server's reason.
    Failed(String),
}

pub(crate) const FAILED_TOAST_SECS: u64 = 8;

/// Apply the server's answer and release the commit that was in flight.
pub fn apply_event(app: &mut App, event: Event) {
    match event {
        Event::Written(runtime) => event_handler::apply_runtime_value(app, &runtime),
        Event::Failed(message) => app.show_toast(message, ToastSeverity::Error, FAILED_TOAST_SECS),
    }
}

/// Hand the highlighted model to the thinking picker, opened at once on its
/// levels; nothing is written until that picker commits or is dismissed. A
/// model that cannot be resolved has no levels to offer and is written alone.
pub fn select(app: &mut App, client: &Arc<Client>, scope: Scope) {
    let alias = selected_alias(app);
    app.model_picker.open = false;
    let target = match alias.as_str() {
        UNPINNED_ACTIVE_MODEL => app.model_picker.default_alias.clone(),
        _ => alias.clone(),
    };
    let option = app.model_picker.models.iter().find(|m| m.alias == target);
    let Some((thinking, levels)) = option.map(|m| (m.thinking.clone(), m.thinking_levels.clone()))
    else {
        let pick = ModelPick {
            alias,
            target: String::new(),
            scope,
        };
        thinking_picker::commit_model(app, client, &pick);
        return;
    };
    let pick = ModelPick {
        alias,
        target,
        scope,
    };
    thinking_picker::open_for_model(app, pick, thinking, levels);
}

/// Open the picker highlighted on the current choice.
pub fn open(app: &mut App) {
    app.model_picker.show_all = false;
    app.model_picker.list = crate::option_list::OptionList::new(current_index(app));
    app.model_picker.open = true;
}

/// Pre-selected row: pinned model `+1` (offset by Default), else `0` (Default).
fn current_index(app: &App) -> usize {
    if app.model_picker.is_pinned {
        if let Some(i) = model_index(app) {
            return i + 1;
        }
    }
    0
}

/// Index into `models` of the currently active alias, if any.
fn model_index(app: &App) -> Option<usize> {
    visible_models(app)
        .iter()
        .position(|m| m.alias == app.model_picker.current_model)
}

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

/// Row count: the Default row plus one per model.
pub fn option_count(app: &App) -> usize {
    visible_models(app).len() + 1
}

/// Move the highlight up/down by one, wrapping at the ends.
pub fn navigate(app: &mut App, down: bool) {
    let count = option_count(app);
    app.model_picker.list.step(down, count, true, |_| true);
}

/// Whether row `i` is the current choice: Default when unpinned, else the pinned model.
pub fn is_current(app: &App, i: usize) -> bool {
    if i == 0 {
        return !app.model_picker.is_pinned;
    }
    app.model_picker.is_pinned && model_index(app) == Some(i - 1)
}

/// The alias to persist for the highlighted row (`""` for Default).
pub fn selected_alias(app: &App) -> String {
    let sel = app.model_picker.list.selected();
    if sel == 0 {
        return UNPINNED_ACTIVE_MODEL.to_owned();
    }
    visible_models(app)
        .get(sel - 1)
        .map(|m| m.alias.clone())
        .unwrap_or_default()
}

/// Close the picker without persisting (Esc / cancel).
pub fn cancel(app: &mut App) {
    app.model_picker.open = false;
}

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
        .strip_suffix(&suffix)
        .unwrap_or(&model.display_name);
    format!("{base}{suffix}")
}

/// Refresh the model snapshot from a `runtime/read`/`runtime/updated` value
/// (shape `{runtime: {config: {...}}}`), so the picker reflects live config.
pub fn apply_runtime(app: &mut App, response: &Value) {
    let Some(config) = crate::server::RuntimeSnapshot::config(response) else {
        return;
    };
    let models: Vec<ModelOption> = config.models.into_iter().filter_map(model_option).collect();
    let alias = config.default_model_alias;
    app.model_picker.default_display_name = models
        .iter()
        .find(|model| model.alias == alias)
        .map_or_else(|| alias.clone(), row_label);
    app.model_picker.models = models;
    app.model_picker.current_model = config.active_model.map(|m| m.alias).unwrap_or_default();
    app.model_picker.is_pinned = config.active_model_pinned;
    app.model_picker.default_alias = alias;
    app.model_picker.scope_active = config.model_scope_active;
}

fn model_option(model: crate::server::ModelView) -> Option<ModelOption> {
    if model.alias.is_empty() {
        return None;
    }
    // Older servers omit the per-model set; fall back to the canonical five (ADR 0014).
    let thinking_levels = match model.thinking_levels.is_empty() {
        true => THINKING_LEVELS.iter().map(|it| it.to_string()).collect(),
        false => model.thinking_levels,
    };
    Some(ModelOption {
        display_name: model.display_name.unwrap_or_else(|| model.alias.clone()),
        alias: model.alias,
        thinking: model.thinking.unwrap_or_default(),
        thinking_levels,
        provider_label: model.provider_label.filter(|label| !label.is_empty()),
        scoped: model.scoped,
    })
}
