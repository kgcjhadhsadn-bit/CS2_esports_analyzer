"""Selector configuration: the production YAML, locator-spec validation, loading errors (no browser)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from cs2bot.config import BetclicSettings, Settings
from cs2bot.executor.selectors import (
    LocatorSpec,
    SelectorConfigError,
    SelectorSet,
    fill_params,
    load_selectors,
    text_matcher,
)
from cs2bot.utils.paths import project_root


def _minimal_data() -> dict[str, Any]:
    data = load_selectors(settings=Settings()).model_dump(mode="json")
    return data


def test_production_yaml_loads_from_any_working_directory() -> None:
    # tests run in a temp CWD: the repo-relative default path falls back to the project root
    selectors = load_selectors(settings=Settings())
    assert selectors.version == 1
    assert set(selectors.elements) >= SelectorSet.REQUIRED_ELEMENTS
    assert (project_root() / "config" / "betclic_selectors.yaml").is_file()


def test_production_yaml_has_several_fallbacks_and_documentation() -> None:
    selectors = load_selectors(settings=Settings())
    for key in SelectorSet.REQUIRED_ELEMENTS:
        element = selectors.elements[key]
        assert len(element.locators) >= 2, f"{key} needs fallbacks"
        assert element.description, f"{key} needs a description"
    for key, element in selectors.elements.items():
        assert element.description, key
        assert element.verified is False, f"{key}: nothing was verified against the live site yet"


def test_production_yaml_uses_polish_ui_texts() -> None:
    raw = (project_root() / "config" / "betclic_selectors.yaml").read_text(encoding="utf-8")
    for text in ("Zaloguj się", "Postaw zakład", "Moje zakłady", "Kupon", "Wyczyść", "DevTools"):
        assert text.lower() in raw.lower(), text


def test_production_yaml_texts_are_normalised_and_classify() -> None:
    selectors = load_selectors(settings=Settings())
    assert "niewystarczajace srodki" in selectors.texts["insufficient_balance"]
    order = ("insufficient_balance", "stake_limit", "market_suspended", "odds_changed")
    assert selectors.classify("Niewystarczające środki na koncie", order) == "insufficient_balance"
    assert selectors.classify("Maksymalna stawka wynosi 50,00 zł", order) == "stake_limit"
    assert selectors.classify("Rynek jest chwilowo ZAWIESZONY", order) == "market_suspended"
    assert selectors.classify("Kursy uległy zmianie", order) == "odds_changed"
    assert selectors.classify("Miłego dnia", order) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Zakład przyjęty! Numer zakładu: BC1001", "BC1001"),
        ("Nr kuponu: 2026-1122-0042", "2026-1122-0042"),
        ("Kupon #123456789 przyjęty", "123456789"),
        ("Identyfikator: A1B2C3", "A1B2C3"),
        ("widoczny element", None),  # 'id' inside a word is not a label
        ("ID: 12", None),  # too short / no real id
    ],
)
def test_production_bet_id_patterns(text: str, expected: str | None) -> None:
    from cs2bot.executor.textutils import first_regex_group

    selectors = load_selectors(settings=Settings())
    assert first_regex_group(text, selectors.compiled_patterns("bet_id")) == expected


def test_load_selectors_from_explicit_and_configured_paths(tmp_path: Path) -> None:
    target = tmp_path / "sel.yaml"
    target.write_text(yaml.safe_dump(_minimal_data(), allow_unicode=True), encoding="utf-8")
    assert load_selectors(target).has("slip_place_button")
    settings = Settings(betclic=BetclicSettings(selectors_file=target))
    assert load_selectors(settings=settings).has("bet_receipt")


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (None, "not found"),
        ("elements: [unclosed", "not valid YAML"),
        ("- just\n- a list\n", "must contain a mapping"),
        ("version: 1\nelements: {}\n", "missing required selector elements"),
    ],
)
def test_load_selectors_errors(tmp_path: Path, content: str | None, match: str) -> None:
    path = tmp_path / "broken.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(SelectorConfigError, match=match):
        load_selectors(path)


def test_selector_set_requires_texts_and_valid_patterns() -> None:
    data = _minimal_data()
    data["texts"].pop("insufficient_balance")
    with pytest.raises(ValueError, match="missing required texts"):
        SelectorSet.model_validate(data)
    data = _minimal_data()
    data["patterns"]["bet_id"] = ["(unclosed"]
    with pytest.raises(ValueError, match="invalid regex"):
        SelectorSet.model_validate(data)


def test_unknown_top_level_keys_are_rejected() -> None:
    data = _minimal_data()
    data["selectors"] = {}
    with pytest.raises(ValueError):
        SelectorSet.model_validate(data)


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"css": ".a", "text": "b"},
        {"name": "x"},
        {"text": "x", "has_text": "y"},
        {"role": "buton", "name": "x"},
        {"role": "button", "name": "/(unclosed/"},
        {"css": ".a", "unknown": 1},
    ],
)
def test_locator_spec_validation(spec: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        LocatorSpec.model_validate(spec)


def test_locator_spec_describe() -> None:
    assert LocatorSpec(css=".a", has_text="b").describe() == "css=.a has_text='b'"
    assert LocatorSpec(role="button", name="Postaw zakład").describe() == "role=button[name='Postaw zakład']"
    assert LocatorSpec(role="dialog").describe() == "role=dialog"
    assert LocatorSpec(text="Wyczyść", exact=True).describe() == "text='Wyczyść' exact"
    assert LocatorSpec(xpath="//a").describe() == "xpath=//a"
    assert LocatorSpec(xpath="//a").kind == "xpath"


def test_text_matcher() -> None:
    assert text_matcher("Zaloguj się") == "Zaloguj się"
    pattern = text_matcher("/^postaw/i")
    assert isinstance(pattern, re.Pattern)
    assert pattern.search("POSTAW ZAKŁAD")
    assert text_matcher("/a.b/s").flags & re.DOTALL


def test_fill_params() -> None:
    assert fill_params("a[href$='-m{event_id}']", {"event_id": "3002201001"}) == "a[href$='-m3002201001']"
    assert fill_params("no placeholders", None) == "no placeholders"
    with pytest.raises(SelectorConfigError, match="no value"):
        fill_params("a[href*='{event_id}']", {})
    with pytest.raises(ValueError, match="unsafe"):
        fill_params("a[href*='{event_id}']", {"event_id": "1'] , script"})


def test_element_lookup_errors() -> None:
    selectors = load_selectors(settings=Settings())
    with pytest.raises(SelectorConfigError, match="not defined"):
        selectors.element("does_not_exist")
    assert not selectors.has("does_not_exist")
    assert selectors.compiled_patterns("does_not_exist") == []
    assert selectors.id_attributes("market_id")
