"""
Plain-text renderers for outbound ActivityPub content.

This module turns trusted-ish local plain text (profile bios, post bodies,
profile links) into safe HTML that remote servers will render.  Everything is
HTML-escaped, and only validated ``http``/``https`` URLs become anchors.

It is intentionally independent of :mod:`pubby.render`, which sanitises inbound
HTML; ``pubby.content`` produces outbound HTML from plaintext using only the
stdlib.
"""

import html
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

# Matches http(s) URLs in free text.  It stops before whitespace and
# HTML-breaking characters so matches can never break out of the escaped
# text around them.
_URL_RE = re.compile(r"https?://[^\s<>\"']+")

# Matches ``#hashtags`` in free text.  The lookbehind keeps matches from
# starting inside words, HTML entities (``&#``) or a run of ``#``s.
_HASHTAG_RE = r"(?<![\w&#])#[A-Za-z0-9_]+"

# Combined token matcher for post content.  The URL alternative comes first
# so a ``#fragment`` inside a URL is consumed by the URL match instead of
# being treated as a hashtag.
_POST_TOKEN_RE = re.compile(rf"https?://[^\s<>\"']+|{_HASHTAG_RE}")

# Closing brackets that often wrap a URL in prose; stripped only when they
# are unmatched inside the URL itself.
_URL_BRACKET_PAIRS = {
    ")": "(",
    "]": "[",
    "}": "{",
}


@dataclass(frozen=True)
class RenderedContent:
    """
    The result of rendering a plain-text post.

    :param html: Safe HTML for an ActivityPub ``content`` or ``summary`` field.
    :param hashtags: Normalized, deduplicated hashtag names, in first-seen order.
    """

    html: str
    hashtags: List[str]


def is_linkable_url(url: str) -> bool:
    """
    Return ``True`` when ``url`` is a well-formed http(s) URL safe to link.

    Values containing whitespace or quote/angle-bracket characters — or URLs
    stored before validation existed — must not become anchors: remote servers
    render these fields as HTML.
    """
    if not url or any(c.isspace() or c in "\"'<>" for c in url):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def display_url(url: str) -> str:
    """Return ``url`` without its http(s) scheme, for use as link text."""
    display = url.split("://", 1)[-1]
    if display.endswith("/") and display.count("/") == 1:
        display = display[:-1]
    return display


def render_link_anchor(
    url: str, rel: Optional[str] = None, label: Optional[str] = None
) -> str:
    """
    Render a validated URL as an HTML anchor.

    :param url: The validated URL to link to.
    :param rel: Optional ``rel`` attribute (e.g. ``"tag"`` or ``"me"``).
    :param label: Optional link text; defaults to the scheme-less display URL.
    :return: An HTML ``<a>`` element with escaped ``href`` and label.
    """
    href = html.escape(url, quote=True)
    text = html.escape(label if label is not None else display_url(url))
    rel_attr = f' rel="{rel}"' if rel else ""
    return f'<a href="{href}"{rel_attr}>{text}</a>'


def _split_trailing_punctuation(url: str) -> Tuple[str, str]:
    """
    Split sentence punctuation off the end of a URL matched in prose.

    ``Visit https://example.com.`` should link ``https://example.com`` and keep
    the final period as text.  Closing brackets are stripped only when they are
    unmatched inside the URL, so ``https://example.com/a_(b)`` keeps its
    balanced parentheses.
    """
    trailing = ""
    while url:
        last = url[-1]
        if last in ".,;:!?'\"" or (
            last in _URL_BRACKET_PAIRS
            and url.count(last) > url.count(_URL_BRACKET_PAIRS[last])
        ):
            trailing = last + trailing
            url = url[:-1]
        else:
            break
    return url, trailing


def _escape_text(text: str) -> str:
    """
    Escape plain text for HTML output, preserving line breaks.

    ``\r\n`` and ``\r`` are normalized to ``\n``, and newlines are emitted
    as ``<br>`` elements: ActivityPub ``content``/``summary`` are HTML
    fields, so a literal newline would collapse to a space on remote
    renderers (e.g. Mastodon).
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return html.escape(normalized).replace("\n", "<br>")


def render_bio_html(bio: str) -> str:
    """
    Render profile bio text for an actor ``summary`` field.

    The text is HTML-escaped and any http(s) URLs are turned into anchors so
    remote servers render them as clickable links.  Newlines become ``<br>``
    elements so line breaks survive remote HTML rendering.  Matches that
    fail :func:`is_linkable_url` are left as escaped text.
    """
    parts: List[str] = []
    pos = 0

    for match in _URL_RE.finditer(bio):
        url, trailing = _split_trailing_punctuation(match.group(0))
        if not is_linkable_url(url):
            continue
        parts.append(_escape_text(bio[pos : match.start()]))
        parts.append(render_link_anchor(url))
        parts.append(html.escape(trailing))
        pos = match.end()

    parts.append(_escape_text(bio[pos:]))
    return "".join(parts)


def _hashtag_name(name: str) -> Optional[str]:
    """Return the normalized (lower-cased) hashtag name, or None if invalid."""
    normalized = name.lower()
    if not any(c.isalpha() for c in normalized):
        return None
    return normalized


def render_post_html(text: str, hashtag_url: Callable[[str], str]) -> RenderedContent:
    """
    Render post body text as safe ActivityPub HTML.

    The text is HTML-escaped, http(s) URLs become anchors with scheme-less link
    text, and ``#hashtags`` become ``rel="tag"`` links produced by the supplied
    ``hashtag_url`` callback.  Matches that fail validation are emitted as
    escaped text.  Newlines become ``<br>`` elements so line breaks survive
    remote HTML rendering.

    :param text: The raw post text.
    :param hashtag_url: Callable that receives a normalized hashtag name and
        returns its local URL.  No default route is assumed.
    :return: A :class:`RenderedContent` tuple of ``(html, hashtags)``.
    """
    parts: List[str] = []
    hashtags: List[str] = []
    seen: set = set()
    pos = 0

    for match in _POST_TOKEN_RE.finditer(text):
        token = match.group(0)
        parts.append(_escape_text(text[pos : match.start()]))
        pos = match.end()

        if token.startswith("#"):
            normalized = _hashtag_name(token[1:])
            if normalized is None:
                parts.append(html.escape(token))
                continue
            parts.append(
                render_link_anchor(hashtag_url(normalized), rel="tag", label=token)
            )
            if normalized not in seen:
                seen.add(normalized)
                hashtags.append(normalized)
            continue

        url, trailing = _split_trailing_punctuation(token)
        if is_linkable_url(url):
            parts.append(render_link_anchor(url))
            parts.append(html.escape(trailing))
        else:
            parts.append(html.escape(token))

    parts.append(_escape_text(text[pos:]))
    return RenderedContent("".join(parts), hashtags)


def build_hashtag_tags(
    names: Iterable[str], hashtag_url: Callable[[str], str]
) -> List[Dict[str, str]]:
    """
    Build ActivityPub ``Hashtag`` tag dicts from normalized hashtag names.

    This is a dumb mapper: it preserves input order and does **not**
    deduplicate.  Deduplication is the renderer's job; callers that merge
    app-generated tags (e.g. genre hashtags) keep control.

    :param names: Iterable of normalized hashtag names (without the leading ``#``).
    :param hashtag_url: Callable that maps a normalized name to its URL.
    :return: A list of ``{"type": "Hashtag", "name": "#name", "href": ...}`` dicts.
    """
    return [
        {"type": "Hashtag", "name": f"#{name}", "href": hashtag_url(name)}
        for name in names
    ]


def render_verified_link(url: str, label: Optional[str] = None) -> str:
    """
    Render a profile link URL as an HTML anchor, or escaped plain text.

    Mastodon and friends render ``PropertyValue.value`` as (sanitised) HTML;
    a bare URL would show up as plain text instead of a clickable link.
    ``rel="me"`` marks the anchor as an identity link so remote servers can
    verify it.  Invalid URLs are emitted as escaped text so they stay inert.

    :param url: The raw link URL.
    :param label: Optional link text; defaults to the scheme-less display URL.
    :return: An ``<a rel="me" ...>`` element, or escaped text if invalid.
    """
    if not is_linkable_url(url):
        return html.escape(url, quote=True)
    return render_link_anchor(url, rel="me", label=label)


def property_value_attachment(
    name: str, url: str, label: Optional[str] = None
) -> Dict[str, str]:
    """
    Build a ``PropertyValue`` attachment dict for ``ActorConfig.attachment``.

    The returned ``value`` is an HTML anchor with ``rel="me"`` when ``url`` is
    valid, or escaped plain text otherwise.  This helper does not perform the
    actual verification: remote servers verify by fetching the linked page and
    checking for a backlink.

    :param name: Field label (e.g. ``"Website"``).
    :param url: Link URL.
    :param label: Optional link text; defaults to the scheme-less display URL.
    :return: A ``{"type": "PropertyValue", "name": ..., "value": ...}`` dict.
    """
    return {
        "type": "PropertyValue",
        "name": name,
        "value": render_verified_link(url, label=label),
    }


def format_duration(seconds: float) -> str:
    """
    Format a number of seconds as an ISO-8601 duration string.

    Suitable for the ``duration`` field of media objects (``Audio``,
    ``Video``).  The value is truncated toward zero; hours and minutes are
    only emitted when non-zero.  Negative values are treated as zero.

    Examples::

        format_duration(3.0)    -> "PT3S"
        format_duration(63.9)   -> "PT1M3S"
        format_duration(3723.0) -> "PT1H2M3S"
        format_duration(0)      -> "PT0S"

    :param seconds: Duration in seconds.
    :return: An ISO-8601 ``PT[h]H[m]M[s]S`` string.
    """
    total = int(seconds)
    if total <= 0:
        return "PT0S"

    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)

    duration = "PT"
    if hours:
        duration += f"{hours}H"
    if minutes:
        duration += f"{minutes}M"
    if secs:
        duration += f"{secs}S"
    return duration


def set_object_content(
    obj: Dict[str, Any], text: str, hashtag_url: Callable[[str], str]
) -> None:
    """
    Set ``obj['content']`` from rendered plain text and merge detected
    hashtags into ``obj['tag']``.

    The text is rendered via :func:`render_post_html` — escaped, with URLs
    and ``#hashtags`` linkified.  Hashtags found in the text are appended
    to the object's ``tag`` list as ``Hashtag`` dicts, deduplicating
    (case-insensitively) against existing tag names so app-generated tags
    (e.g. genre hashtags) are preserved.

    This is a no-op when ``text`` is empty or blank.

    :param obj: The ActivityPub object dictionary to modify in place.
    :param text: Plain-text description or caption.
    :param hashtag_url: Callable that maps a normalized hashtag name to
        its local URL.
    """
    if not text or not text.strip():
        return

    rendered = render_post_html(text, hashtag_url)
    if rendered.html:
        obj["content"] = rendered.html
    if not rendered.hashtags:
        return

    tags = obj.setdefault("tag", [])
    seen = {str(tag.get("name", "")).lower() for tag in tags if isinstance(tag, dict)}
    new_names = [name for name in rendered.hashtags if f"#{name}" not in seen]
    tags.extend(build_hashtag_tags(new_names, hashtag_url))
