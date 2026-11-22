"""SelectorSet.resolve on real Chromium (local HTML via set_content): order, fallbacks, scoping, errors."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page, sync_playwright

from cs2bot.executor.selectors import SelectorNotFoundError, SelectorSet, resolve
from tests.executor.conftest import CHROMIUM, requires_chromium

pytestmark = [pytest.mark.browser, requires_chromium]

HTML = """<!doctype html><html lang="pl"><head><meta charset="utf-8"></head><body>
<header><span class="header_balance">1 234,56 zł</span></header>
<sports-markets-single-market class="marketBox" data-market-id="M1">
  <div class="marketBox_head"><h2 class="marketBox_headTitle">Zwycięzca meczu</h2></div>
  <sports-selections-selection data-selection-id="S1">
    <span class="oddValue">1,85</span>
  </sports-selections-selection>
</sports-markets-single-market>
<sports-markets-single-market class="marketBox" data-market-id="M2">
  <div class="marketBox_head"><h2 class="marketBox_headTitle">Zwycięzca mapy 1</h2></div>
  <sports-selections-selection data-selection-id="S2">
    <span class="oddValue">2,10</span>
  </sports-selections-selection>
</sports-markets-single-market>
<a href="/e-sport-s102/iem-c1/navi-vitality-m3002201001">NAVI - Vitality</a>
<button>Postaw zakład</button>
<div class="hiddenThing" style="display:none">Ukryty</div>
</body></html>"""


def _selectors(elements: dict[str, list[dict[str, Any]]]) -> SelectorSet:
    """A SelectorSet with dummy required entries plus the elements under test."""
    filler = {
        key: {"description": "x", "locators": [{"css": ".never-matches"}]}
        for key in SelectorSet.REQUIRED_ELEMENTS
    }
    filler.update({key: {"description": "test", "locators": locs} for key, locs in elements.items()})
    texts = {key: ["x"] for key in SelectorSet.REQUIRED_TEXTS}
    return SelectorSet.model_validate({"elements": filler, "texts": texts})


@pytest.fixture(scope="module")
def page() -> Iterator[Page]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM, headless=True)
        pg = browser.new_page()
        pg.set_content(HTML)
        yield pg
        browser.close()


def test_primary_locator_wins_when_it_matches(page: Page) -> None:
    sel = _selectors({"thing": [{"css": ".header_balance"}, {"text": "zł"}]})
    found = sel.resolve(page, "thing", 1.0)
    assert found.index == 0 and not found.used_fallback and found.count == 1
    assert found.first.inner_text() == "1 234,56 zł"


def test_fallbacks_are_tried_in_order(page: Page, caplog: pytest.LogCaptureFixture) -> None:
    sel = _selectors(
        {
            "place": [
                {"css": "[data-qa='place-bet']"},
                {"role": "button", "name": "Postaw zakład"},
                {"text": "Postaw"},
            ]
        }
    )
    with caplog.at_level("WARNING"):
        found = sel.resolve(page, "place", 1.0)
        sel.resolve(page, "place", 1.0)
    assert found.index == 1 and found.used_fallback
    assert found.spec.role == "button"
    assert sum("matched fallback #2" in r.message for r in caplog.records) == 1  # reported once


def test_scoped_resolution_and_resolve_all(page: Page) -> None:
    sel = _selectors(
        {
            "market": [{"css": "sports-markets-single-market"}],
            "title": [{"css": ".marketBox_headTitle"}],
            "odds": [{"css": ".oddValue"}],
        }
    )
    markets = sel.resolve_all(page, "market", 1.0)
    assert len(markets) == 2
    titles = [sel.resolve(m, "title", 1.0).first.inner_text() for m in markets]
    assert titles == ["Zwycięzca meczu", "Zwycięzca mapy 1"]
    assert sel.resolve(markets[1], "odds", 1.0).count == 1  # only inside that market
    assert sel.resolve(markets[1], "odds", 1.0).first.inner_text() == "2,10"
    assert len(sel.resolve(page, "market", 1.0).all(limit=1)) == 1


def test_params_and_regex_names(page: Page) -> None:
    sel = _selectors(
        {
            "event": [{"css": "a[href$='-m{event_id}']"}],
            "place": [{"role": "button", "name": "/^postaw zak/i"}],
            "xp": [{"xpath": "//h2[contains(., 'mapy')]"}],
        }
    )
    assert sel.resolve(page, "event", 1.0, params={"event_id": "3002201001"}).count == 1
    assert sel.try_resolve(page, "event", 0.2, params={"event_id": "999"}) is None
    assert sel.resolve(page, "place", 1.0).count == 1
    assert sel.resolve(page, "xp", 1.0).first.inner_text() == "Zwycięzca mapy 1"
    assert resolve(page, sel, "place", 1.0).count == 1  # module-level helper


def test_visible_versus_attached(page: Page) -> None:
    sel = _selectors({"hidden": [{"css": ".hiddenThing"}]})
    assert sel.probe(page, "hidden") is None
    assert sel.probe(page, "hidden", state="attached") is not None
    assert sel.probe(page, "undefined_key") is None
    assert sel.try_resolve(page, "undefined_key", 0.1) is None


def test_timeout_error_lists_every_locator_and_invalid_css(page: Page) -> None:
    sel = _selectors(
        {"missing": [{"css": "div[[["}, {"role": "button", "name": "Wyczyść"}, {"text": "Kupon"}]}
    )
    started = time.monotonic()
    with pytest.raises(SelectorNotFoundError) as err:
        sel.resolve(page, "missing", 0.5)
    assert 0.4 <= time.monotonic() - started < 5
    message = str(err.value)
    assert "selector 'missing'" in message
    assert "1) css=div[[[ [error:" in message
    assert "2) role=button[name='Wyczyść']" in message and "3) text='Kupon'" in message
    assert err.value.key == "missing" and len(err.value.tried) == 3


def test_resolve_waits_for_late_elements(page: Page) -> None:
    sel = _selectors({"late": [{"css": "#late"}]})
    page.evaluate(
        "setTimeout(() => { const d = document.createElement('div'); d.id = 'late'; d.textContent = 'x';"
        " document.body.appendChild(d); }, 300)"
    )
    found = sel.resolve(page, "late", 3.0)
    assert found.count == 1
