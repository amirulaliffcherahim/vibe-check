//! `/provider add` argument parsing.

use vibe_rs::commands::provider::{ProviderKind, parse_add};

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
    assert!(
        parse_add("p https://h/v1 --type grpc")
            .unwrap_err()
            .contains("grpc")
    );
    assert!(parse_add("p https://h/v1 --type").is_err());
    assert!(
        parse_add("p https://h/v1 --bogus")
            .unwrap_err()
            .contains("--bogus")
    );
}

#[test]
fn rejects_missing_positionals_and_extra_ones() {
    assert!(parse_add("").is_err());
    assert!(parse_add("only-a-name").is_err());
    assert!(parse_add("p https://h/v1 key extra").is_err());
}
