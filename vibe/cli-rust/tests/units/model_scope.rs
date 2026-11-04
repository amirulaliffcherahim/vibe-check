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
