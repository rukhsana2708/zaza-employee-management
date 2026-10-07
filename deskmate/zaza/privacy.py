"""Privacy exclusions and window-title normalization.

Excluded applications: the window title is never *read* (the foreground
probe checks the process name first and skips ``GetWindowTextW``), and is
stored as ``Excluded / Private``. Hidden applications additionally have their
process name replaced with ``Excluded Application``.

Excluded domains: the domain is stored as ``Excluded / Private Site`` and the
window title (which, for a browser, is the page title) is stored as
``Excluded / Private``. Phase 2 has no domain detection at all; this is the
filter that any future domain source must pass through before persistence.

:meth:`PrivacyFilter.apply` is also run on every sample right before it is
persisted, as a second line of defence for probes that don't pre-filter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .config import AgentConfig
    from .window_watch import WindowSample

EXCLUDED_TITLE = "Excluded / Private"
EXCLUDED_APP_NAME = "Excluded Application"
EXCLUDED_SITE = "Excluded / Private Site"

MAX_TITLE_LENGTH = 256

_UNREAD_COUNTER = re.compile(r"^\s*[\(\[]\d+\+?[\)\]]\s*")
_MODIFIED_MARKER_PREFIX = re.compile(r"^\s*[●•*]\s*")
_MODIFIED_MARKER_SUFFIX = re.compile(r"\s*[●•*]\s*$")
_WHITESPACE = re.compile(r"\s+")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


def normalize_title(title: str | None) -> str:
    """Strip window-title noise that would otherwise fragment periods:
    unread counters like ``(3) Inbox``, unsaved-file markers (``●``, ``*``),
    control characters, and runs of whitespace. Capped at 256 characters."""
    if not title:
        return ""
    value = _CONTROL_CHARS.sub(" ", title)
    value = _UNREAD_COUNTER.sub("", value)
    value = _MODIFIED_MARKER_PREFIX.sub("", value)
    value = _MODIFIED_MARKER_SUFFIX.sub("", value)
    value = _WHITESPACE.sub(" ", value).strip()
    return value[:MAX_TITLE_LENGTH]


def title_key(title: str | None) -> str:
    return normalize_title(title).casefold()


def normalize_domain(value: str | None) -> str | None:
    """Reduce anything domain-like to a bare lowercase hostname, or None.

    If a full URL is passed by mistake, only the hostname survives — path,
    query, fragment, port, and credentials are dropped before anything is
    stored. Anything that isn't a plain hostname afterwards is rejected."""
    if not value:
        return None
    raw = value.strip().lower()
    if "://" in raw:
        raw = urlsplit(raw).hostname or ""
    raw = re.split(r"[/?#]", raw, maxsplit=1)[0]
    raw = raw.rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
    if raw.startswith("www."):
        raw = raw[4:]
    if not raw or not _HOSTNAME.match(raw):
        return None
    return raw


def _app_key(name: str | None) -> str:
    key = (name or "").strip().casefold()
    return key[:-4] if key.endswith(".exe") else key


class PrivacyFilter:
    def __init__(
        self,
        excluded_apps: Iterable[str] = (),
        hidden_app_names: Iterable[str] = (),
        excluded_domains: Iterable[str] = (),
    ) -> None:
        self._hidden = {_app_key(a) for a in hidden_app_names if _app_key(a)}
        # Hiding an app's name implies excluding its title too.
        self._excluded = {_app_key(a) for a in excluded_apps if _app_key(a)} | self._hidden
        self._domains = {d for d in (normalize_domain(x) for x in excluded_domains) if d}

    @classmethod
    def from_config(cls, config: AgentConfig) -> PrivacyFilter:
        return cls(config.excluded_apps, config.hidden_app_names, config.excluded_domains)

    def is_excluded_app(self, app_name: str | None) -> bool:
        return _app_key(app_name) in self._excluded

    def stored_app_name(self, app_name: str | None) -> str | None:
        if _app_key(app_name) in self._hidden:
            return EXCLUDED_APP_NAME
        return app_name

    def is_excluded_domain(self, domain: str | None) -> bool:
        host = normalize_domain(domain)
        if not host:
            return False
        return any(host == d or host.endswith("." + d) for d in self._domains)

    def filter_domain(self, domain: str | None) -> str | None:
        if self.is_excluded_domain(domain):
            return EXCLUDED_SITE
        return normalize_domain(domain)

    def apply(self, sample: WindowSample) -> WindowSample:
        """Return the version of ``sample`` that is allowed to be persisted.
        Idempotent: applying it to an already-filtered sample changes nothing."""
        if (
            sample.privacy_excluded
            and sample.window_title == EXCLUDED_TITLE
            and sample.domain in (None, EXCLUDED_SITE)
        ):
            return replace(sample, app_name=self.stored_app_name(sample.app_name))
        app_excluded = sample.privacy_excluded or self.is_excluded_app(sample.app_name)
        if app_excluded:
            return replace(
                sample,
                app_name=self.stored_app_name(sample.app_name),
                window_title=EXCLUDED_TITLE,
                domain=None,
                privacy_excluded=True,
            )
        if self.is_excluded_domain(sample.domain):
            return replace(sample, window_title=EXCLUDED_TITLE, domain=EXCLUDED_SITE, privacy_excluded=True)
        return replace(
            sample,
            window_title=normalize_title(sample.window_title),
            domain=normalize_domain(sample.domain),
            privacy_excluded=False,
        )
