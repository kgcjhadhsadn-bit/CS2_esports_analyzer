"""Betclic DOM selectors (``config/betclic_selectors.yaml``) and the resolution helpers.

Every logical page element (login button, market box, bet-slip stake input, ...) maps to an ORDERED list
of locator specs; the first spec that matches wins. A spec is exactly one of::

    {css: "..."}                     # CSS selector (optionally narrowed with has_text)
    {role: button, name: "..."}      # ARIA role + accessible name (Polish UI texts)
    {text: "..."}                    # visible text
    {xpath: "..."}                   # XPath (use ".//" for element-scoped lookups)

``name`` / ``text`` / ``has_text`` are case-insensitive substrings unless ``exact: true``; write
``/regex/i`` for a regular expression. ``{event_id}``-style placeholders are filled from ``params``.

Resolution polls ALL specs of an element in priority order until one matches or the timeout expires,
then raises :class:`SelectorNotFoundError` listing every spec tried (fail loud; the caller fails safe).
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

import yaml
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from cs2bot.config import Settings, get_settings
from cs2bot.executor.base import ExecutorError
from cs2bot.executor.textutils import classify_text, normalise_text
from cs2bot.utils.logging import get_logger
from cs2bot.utils.paths import resolve_path

log = get_logger(__name__)

ElementState = Literal["attached", "visible"]
Root = Page | Locator

# Roles accepted by Playwright's get_by_role (ARIA 1.2).
ARIA_ROLES: frozenset[str] = frozenset(
    {
        "alert",
        "alertdialog",
        "application",
        "article",
        "banner",
        "blockquote",
        "button",
        "caption",
        "cell",
        "checkbox",
        "code",
        "columnheader",
        "combobox",
        "complementary",
        "contentinfo",
        "definition",
        "deletion",
        "dialog",
        "directory",
        "document",
        "emphasis",
        "feed",
        "figure",
        "form",
        "generic",
        "grid",
        "gridcell",
        "group",
        "heading",
        "img",
        "insertion",
        "link",
        "list",
        "listbox",
        "listitem",
        "log",
        "main",
        "marquee",
        "math",
        "menu",
        "menubar",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "meter",
        "navigation",
        "none",
        "note",
        "option",
        "paragraph",
        "presentation",
        "progressbar",
        "radio",
        "radiogroup",
        "region",
        "row",
        "rowgroup",
        "rowheader",
        "scrollbar",
        "search",
        "searchbox",
        "separator",
        "slider",
        "spinbutton",
        "status",
        "strong",
        "subscript",
        "superscript",
        "switch",
        "tab",
        "table",
        "tablist",
        "tabpanel",
        "term",
        "textbox",
        "time",
        "timer",
        "toolbar",
        "tooltip",
        "tree",
        "treegrid",
        "treeitem",
    }
)

_REGEX_LITERAL = re.compile(r"^/(?P<body>.+)/(?P<flags>[imsx]*)$", re.DOTALL)
_PARAM_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_SAFE_PARAM_VALUE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}


class SelectorConfigError(ExecutorError):
    """The selector YAML is missing, malformed or lacks a required element."""


class SelectorNotFoundError(ExecutorError):
    """No locator spec of an element matched within the timeout."""

    def __init__(self, key: str, tried: Sequence[str], *, state: str, timeout_s: float) -> None:
        self.key = key
        self.tried = list(tried)
        self.state = state
        self.timeout_s = timeout_s
        listing = "; ".join(f"{i + 1}) {t}" for i, t in enumerate(self.tried)) or "no locators"
        super().__init__(
            f"selector '{key}' did not resolve ({state}) within {timeout_s:g}s - tried: {listing}. "
            f"Update config/betclic_selectors.yaml (see the header of that file)."
        )


def text_matcher(value: str) -> str | re.Pattern[str]:
    """``"/regex/i"`` -> compiled pattern, anything else unchanged (substring match in Playwright)."""
    m = _REGEX_LITERAL.match(value)
    if not m:
        return value
    flags = 0
    for ch in m.group("flags"):
        flags |= _FLAG_MAP[ch]
    return re.compile(m.group("body"), flags)


def fill_params(template: str, params: Mapping[str, str] | None) -> str:
    """Replace ``{name}`` placeholders; values must match ``[A-Za-z0-9_.:-]`` (no selector injection)."""

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if params is None or name not in params:
            raise SelectorConfigError(f"selector placeholder '{{{name}}}' has no value")
        value = str(params[name])
        if not _SAFE_PARAM_VALUE.fullmatch(value):
            raise ValueError(f"unsafe selector parameter value for {name!r}")
        return value

    return _PARAM_RE.sub(_sub, template)


class LocatorSpec(BaseModel):
    """One way of finding an element. Exactly one of ``css`` / ``role`` / ``text`` / ``xpath``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    css: str | None = None
    role: str | None = None
    name: str | None = None
    text: str | None = None
    xpath: str | None = None
    exact: bool = False
    has_text: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _check(self) -> LocatorSpec:
        kinds = [k for k in ("css", "role", "text", "xpath") if getattr(self, k)]
        if len(kinds) != 1:
            raise ValueError("a locator needs exactly one of: css, role, text, xpath")
        if self.name is not None and self.role is None:
            raise ValueError("'name' is only valid together with 'role'")
        if self.has_text is not None and not (self.css or self.xpath):
            raise ValueError("'has_text' is only valid together with 'css' or 'xpath'")
        if self.role is not None and self.role not in ARIA_ROLES:
            raise ValueError(f"unknown ARIA role {self.role!r}")
        for value in (self.name, self.text, self.has_text):
            if value:
                try:
                    text_matcher(value)
                except re.error as exc:
                    raise ValueError(f"invalid regex {value!r}: {exc}") from exc
        return self

    @property
    def kind(self) -> str:
        return next(k for k in ("css", "role", "text", "xpath") if getattr(self, k))

    def describe(self) -> str:
        if self.css:
            out = f"css={self.css}"
        elif self.xpath:
            out = f"xpath={self.xpath}"
        elif self.role:
            out = f"role={self.role}" + (f"[name={self.name!r}]" if self.name else "")
        else:
            out = f"text={self.text!r}"
        if self.has_text:
            out += f" has_text={self.has_text!r}"
        if self.exact:
            out += " exact"
        return out

    def build(self, root: Root, params: Mapping[str, str] | None = None) -> Locator:
        """Playwright locator for this spec, relative to ``root`` (a page or a container element)."""
        has_text = text_matcher(self.has_text) if self.has_text else None
        if self.css:
            return root.locator(fill_params(self.css, params), has_text=has_text)
        if self.xpath:
            return root.locator("xpath=" + fill_params(self.xpath, params), has_text=has_text)
        if self.role:
            role = cast(Any, self.role)  # validated against ARIA_ROLES
            if self.name:
                return root.get_by_role(
                    role, name=text_matcher(fill_params(self.name, params)), exact=self.exact
                )
            return root.get_by_role(role)
        assert self.text is not None
        return root.get_by_text(text_matcher(fill_params(self.text, params)), exact=self.exact)


class ElementSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    scope: str | None = None  # documentation: the element key this one is resolved inside
    verified: bool = False  # true once checked against the live betclic.pl DOM
    optional: bool = False  # the flow copes without it
    locators: list[LocatorSpec] = Field(min_length=1)


class SiteUrls(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str = "/"  # page on which the login form is opened (path or absolute betclic URL)
    cs2_listing: str | None = None  # None -> settings.betclic.cs2_listing_path
    bet_history: str | None = None  # None -> click the "Moje zakłady" link (nav_my_bets)


class SelectorBehaviour(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # True: clicking "accept new odds" submits the bet (never click "Postaw zakład" again).
    # False: accepting only acknowledges; the executor clicks the place button once more (bounded).
    odds_change_accept_places_bet: bool = True
    dismiss_cookie_banner: bool = True
    dismiss_popups: bool = True
    # True: a market name shown in the bet slip that does not fit the ordered market aborts the bet
    # (SELECTOR_ERROR). False: only a warning is logged.
    require_slip_market_match: bool = True
    # True: bet-slip selections that are in the DOM but hidden count as stale selections and abort the
    # bet (a hidden second selection would turn the single into a combination bet). Set to False only
    # if the page is known to keep hidden duplicates of the visible slip.
    count_hidden_slip_items: bool = True


@dataclass(frozen=True)
class ResolvedElement:
    """The winning locator of an element (all its matches, already state-filtered)."""

    key: str
    locator: Locator
    spec: LocatorSpec
    index: int  # 0 = primary locator; > 0 = a fallback was needed
    count: int

    @property
    def first(self) -> Locator:
        return self.locator.first

    @property
    def used_fallback(self) -> bool:
        return self.index > 0

    def all(self, limit: int | None = None) -> list[Locator]:
        n = self.locator.count()
        if limit is not None:
            n = min(n, limit)
        return [self.locator.nth(i) for i in range(n)]


def _page_of(root: Root) -> Page:
    return root if isinstance(root, Page) else root.page


class SelectorSet(BaseModel):
    """Validated content of ``config/betclic_selectors.yaml``."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    urls: SiteUrls = Field(default_factory=SiteUrls)
    behaviour: SelectorBehaviour = Field(default_factory=SelectorBehaviour)
    attributes: dict[str, list[str]] = Field(default_factory=dict)
    texts: dict[str, list[str]] = Field(default_factory=dict)
    patterns: dict[str, list[str]] = Field(default_factory=dict)
    elements: dict[str, ElementSpec]

    _reported_fallbacks: set[tuple[str, int]] = PrivateAttr(default_factory=set)

    REQUIRED_ELEMENTS: ClassVar[frozenset[str]] = frozenset(
        {
            "login_button",
            "logged_in_indicator",
            "header_balance",
            "event_link",
            "event_link_by_id",
            "market_container",
            "market_title",
            "selection_button",
            "selection_label",
            "selection_odds",
            "slip_item",
            "slip_item_selection",
            "slip_item_odds",
            "slip_item_remove",
            "slip_clear_button",
            "slip_stake_input",
            "slip_place_button",
            "slip_error",
            "odds_changed_notice",
            "odds_changed_accept",
            "bet_receipt",
            "nav_my_bets",
            "history_item",
        }
    )
    REQUIRED_TEXTS: ClassVar[frozenset[str]] = frozenset(
        {"insufficient_balance", "market_suspended", "stake_limit", "odds_changed", "login_required"}
    )

    @field_validator("texts")
    @classmethod
    def _normalise_texts(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key, phrases in value.items():
            cleaned = [normalise_text(p) for p in phrases]
            out[key] = [p for p in cleaned if p]
        return out

    @field_validator("patterns")
    @classmethod
    def _check_patterns(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for key, patterns in value.items():
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"patterns.{key}: invalid regex {pattern!r}: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _check_required(self) -> SelectorSet:
        missing = sorted(self.REQUIRED_ELEMENTS - set(self.elements))
        if missing:
            raise ValueError(f"missing required selector elements: {missing}")
        missing_texts = sorted(k for k in self.REQUIRED_TEXTS if not self.texts.get(k))
        if missing_texts:
            raise ValueError(f"missing required texts: {missing_texts}")
        return self

    # ------------------------------------------------------------------ lookups

    def has(self, key: str) -> bool:
        return key in self.elements

    def element(self, key: str) -> ElementSpec:
        try:
            return self.elements[key]
        except KeyError:
            raise SelectorConfigError(f"selector element '{key}' is not defined") from None

    def phrases(self, key: str) -> list[str]:
        return list(self.texts.get(key, []))

    def id_attributes(self, key: str) -> list[str]:
        return list(self.attributes.get(key, []))

    @cached_property
    def _compiled(self) -> dict[str, list[re.Pattern[str]]]:
        return {k: [re.compile(p, re.IGNORECASE) for p in v] for k, v in self.patterns.items()}

    def compiled_patterns(self, key: str) -> list[re.Pattern[str]]:
        return list(self._compiled.get(key, []))

    def classify(self, text: str | None, categories: Sequence[str]) -> str | None:
        """First category in ``categories`` whose phrase (``texts`` section) occurs in ``text``."""
        return classify_text(text, self.texts, categories)

    # ------------------------------------------------------------------ resolution

    def probe(
        self,
        root: Root,
        key: str,
        *,
        state: ElementState = "visible",
        params: Mapping[str, str] | None = None,
    ) -> ResolvedElement | None:
        """One non-waiting pass over the specs; ``None`` if nothing matches (or the key is undefined)."""
        if key not in self.elements:
            return None
        return self._probe(root, key, self.elements[key], state, params, {})

    def resolve(
        self,
        root: Root,
        key: str,
        timeout_s: float,
        *,
        state: ElementState = "visible",
        params: Mapping[str, str] | None = None,
        poll_s: float = 0.15,
    ) -> ResolvedElement:
        """Try the element's specs in order, polling until one matches; raise when the timeout expires."""
        element = self.element(key)
        errors: dict[int, str] = {}
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            found = self._probe(root, key, element, state, params, errors)
            if found is not None:
                if found.used_fallback:
                    self._report_fallback(key, found, element)
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                tried = [
                    spec.describe() + (f" [error: {errors[i]}]" if i in errors else "")
                    for i, spec in enumerate(element.locators)
                ]
                raise SelectorNotFoundError(key, tried, state=state, timeout_s=timeout_s)
            _page_of(root).wait_for_timeout(min(poll_s, remaining) * 1000)

    def try_resolve(
        self,
        root: Root,
        key: str,
        timeout_s: float,
        *,
        state: ElementState = "visible",
        params: Mapping[str, str] | None = None,
        poll_s: float = 0.15,
    ) -> ResolvedElement | None:
        """:meth:`resolve` returning ``None`` when nothing matched (or the key is undefined)."""
        if key not in self.elements:
            return None
        try:
            return self.resolve(root, key, timeout_s, state=state, params=params, poll_s=poll_s)
        except SelectorNotFoundError:
            return None

    def resolve_all(
        self,
        root: Root,
        key: str,
        timeout_s: float,
        *,
        state: ElementState = "visible",
        params: Mapping[str, str] | None = None,
        limit: int | None = None,
    ) -> list[Locator]:
        """Every element matched by the winning spec (raises like :meth:`resolve`)."""
        return self.resolve(root, key, timeout_s, state=state, params=params).all(limit)

    def _report_fallback(self, key: str, found: ResolvedElement, element: ElementSpec) -> None:
        """Log (once per element and fallback) that the primary locator no longer matches."""
        marker = (key, found.index)
        if marker in self._reported_fallbacks:
            return
        self._reported_fallbacks.add(marker)
        log.warning(
            "selector '%s' matched fallback #%d (%s); the primary locator %s did not match - consider "
            "updating config/betclic_selectors.yaml",
            key,
            found.index + 1,
            found.spec.describe(),
            element.locators[0].describe(),
        )

    def _probe(
        self,
        root: Root,
        key: str,
        element: ElementSpec,
        state: ElementState,
        params: Mapping[str, str] | None,
        errors: dict[int, str],
    ) -> ResolvedElement | None:
        for index, spec in enumerate(element.locators):
            try:
                locator = spec.build(root, params)
                if state == "visible":
                    locator = locator.filter(visible=True)
                count = locator.count()
            except PlaywrightError as exc:  # e.g. an invalid CSS selector: report, try the next spec
                errors[index] = (
                    str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__
                )
                continue
            if count > 0:
                return ResolvedElement(key=key, locator=locator, spec=spec, index=index, count=count)
        return None

    # ------------------------------------------------------------------ loading

    @classmethod
    def from_yaml(cls, path: str | Path) -> SelectorSet:
        p = Path(path)
        if not p.is_file():
            raise SelectorConfigError(f"selector file not found: {p}")
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise SelectorConfigError(f"selector file {p} is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise SelectorConfigError(f"selector file {p} must contain a mapping")
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise SelectorConfigError(f"invalid selector file {p}: {exc}") from exc


def load_selectors(path: str | Path | None = None, *, settings: Settings | None = None) -> SelectorSet:
    """Load and validate the selector file (default: ``settings.betclic.selectors_file``)."""
    target = path if path is not None else (settings or get_settings()).betclic.selectors_file
    return SelectorSet.from_yaml(resolve_path(target))


def resolve(
    root: Root,
    selectors: SelectorSet,
    key: str,
    timeout_s: float,
    *,
    state: ElementState = "visible",
    params: Mapping[str, str] | None = None,
) -> ResolvedElement:
    """Module-level shortcut for :meth:`SelectorSet.resolve`."""
    return selectors.resolve(root, key, timeout_s, state=state, params=params)
