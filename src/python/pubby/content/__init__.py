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

# Matches ``#hashtags`` in free text, Mastodon's grammar
# (``Tag::HASHTAG_NAME_PAT`` in ``tag.rb``).  Unicode ``\w`` covers
# letters/digits/``_`` in every script; ``_HASHTAG_MARK`` adds the
# combining marks (\p{M}) ``re`` can't express — Indic matras, Arabic
# harakat, decomposed accents — and the separator set adds Mastodon's
# mid-tag separators (``·``, ``・``, ZWNJ/ZWJ).  A name starts and ends
# on a word char: separators can't lead or trail.  The lookbehind keeps
# matches from starting inside words, HTML entities (``&#``) or a run
# of ``#``/``＃``s.  Mirrors Rust ``HASHTAG_RE``.
_HASHTAG_MARK = (
    "\u0300-\u036f"  # combining diacriticals (decomposed accents)
    "\u0483-\u0489\u0591-\u05c7"  # Cyrillic, Hebrew points
    "\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed"  # Arabic
    "\u0711\u0730-\u074a\u07a6-\u07b0\u07eb-\u07f3\u07fd"  # Syriac, Thaana, NKo
    "\u0816-\u082d\u0859-\u085b\u0890-\u0891\u0898-\u089f\u08ca-\u0902"
    "\u093a-\u094f\u0951-\u0957\u0962-\u0963"  # Devanagari
    "\u0981-\u0983\u09bc-\u09cd\u09d7\u09e2-\u09e3\u09fe"  # Bengali
    "\u0a01-\u0a03\u0a3c\u0a3e-\u0a4d\u0a51\u0a70-\u0a71\u0a75"  # Gurmukhi
    "\u0a81-\u0a83\u0abc-\u0acd\u0ae2-\u0ae3\u0afa-\u0aff"  # Gujarati
    "\u0b01-\u0b03\u0b3c\u0b3e-\u0b4d\u0b55-\u0b57\u0b62-\u0b63"  # Oriya
    "\u0b82\u0bbe-\u0bcd\u0bd7"  # Tamil
    "\u0c00-\u0c04\u0c3c\u0c3e-\u0c4d\u0c55-\u0c56\u0c62-\u0c63"  # Telugu
    "\u0c81-\u0c83\u0cbc\u0cbe-\u0ccd\u0cd5-\u0cd6\u0ce2-\u0ce3"  # Kannada
    "\u0d00-\u0d03\u0d3b-\u0d3c\u0d3e-\u0d4d\u0d57\u0d62-\u0d63"  # Malayalam
    "\u0d81-\u0d83\u0dca\u0dcf-\u0dd4\u0dd6\u0dd8-\u0ddf\u0df2-\u0df3"  # Sinhala
    "\u0e31-\u0e3a\u0e47-\u0e4e"  # Thai
    "\u0eb1-\u0ebc\u0ec8-\u0ece"  # Lao
    "\u0f18-\u0f19\u0f35\u0f37\u0f39\u0f3e-\u0f84\u0f86-\u0f87\u0f8d-\u0fbc\u0fc6"  # Tibetan
    "\u102b-\u103e\u1056-\u106d\u1071-\u1074\u1082-\u108d\u108f\u109a-\u109d"  # Myanmar
    "\u135d-\u135f"  # Ethiopic
    "\u1712-\u1715\u1732-\u1734\u1752-\u1753\u1772-\u1773"  # Philippine
    "\u17b4-\u17d3\u17dd"  # Khmer
    "\u180b-\u180d\u180f\u1885-\u1886\u18a9"  # Mongolian
    "\u1920-\u192b\u1930-\u193b\u1a17-\u1a1b\u1a55-\u1a5e\u1a60-\u1a7c\u1a7f"
    "\u1ab0-\u1ace"  # combining diacritical marks extended
    "\u1b00-\u1b04\u1b34-\u1b44\u1b6b-\u1b73"  # Balinese
    "\u1b80-\u1b82\u1ba1-\u1bad\u1be6-\u1bf3\u1c24-\u1c37"  # Sundanese, Batak, Lepcha
    "\u1cd0-\u1ce8\u1ced\u1cf2-\u1cf4\u1cf7-\u1cf9"  # Vedic
    "\u1dc0-\u1dff\u20d0-\u20f0"  # combining marks supplements
    "\u2cef-\u2cf1\u2d7f\u2de0-\u2dff\u302a-\u302f"  # CJK, Cyrillic ext
    "\u3099-\u309a"  # Japanese dakuten/handakuten
    "\ua66f-\ua672\ua674-\ua67d\ua69e-\ua69f\ua6f0-\ua6f1"
    "\ua802\ua806\ua80b\ua823-\ua827\ua82c\ua880-\ua881\ua8b4-\ua8c5\ua8e0-\ua8f1\ua8ff"
    "\ua926-\ua92d\ua947-\ua953\ua980-\ua983\ua9b3-\ua9c0\ua9e5"
    "\uaa29-\uaa36\uaa43\uaa4c-\uaa4d\uaa7b-\uaa7d\uaab0\uaab2-\uaab4\uaab7-\uaab8\uaabe-\uaabf\uaac1"
    "\uaaeb-\uaaef\uaaf5-\uaaf6\uabe3-\uabea\uabec-\uabed\ufb1e"
    "\ufe00-\ufe0f\ufe20-\ufe2f"  # variation selectors, combining half marks
)
_HASHTAG_WORD = rf"[\w{_HASHTAG_MARK}]"
_HASHTAG_RE = (
    rf"(?<![\w&#＃])[#＃]{_HASHTAG_WORD}"
    rf"(?:[\w{_HASHTAG_MARK}\u00b7\u30fb\u200c\u200d]*{_HASHTAG_WORD})?"
)

# Combined token matcher for post content.  The URL alternative comes first
# so a ``#fragment`` inside a URL is consumed by the URL match instead of
# being treated as a hashtag.
_HANDLE_RE = r"(?<![\w&@])@[A-Za-z0-9_][A-Za-z0-9_.-]*@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}"
_POST_TOKEN_RE = re.compile(rf"https?://[^\s<>\"']+|{_HASHTAG_RE}")
_BIO_TOKEN_RE = re.compile(rf"https?://[^\s<>\"']+|{_HASHTAG_RE}|{_HANDLE_RE}")

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


def render_bio_html(bio: str, hashtag_url: Callable[[str], str]) -> RenderedContent:
    """
    Render profile bio text for an actor ``summary`` field.

    The text is HTML-escaped, http(s) URLs become anchors with scheme-less
    link text, ``#hashtags`` become ``rel="tag"`` links produced by the
    supplied ``hashtag_url`` callback, and ``@user@domain`` handles become
    links to ``https://{domain}/@{user}`` — the same serialization Mastodon
    emits in ``summary``.  Newlines become ``<br>`` elements so line breaks
    survive remote HTML rendering.  Matches that fail validation are left
    as escaped text.

    :param hashtag_url: Function that receives a normalized hashtag name
        and returns its (absolute) local URL.
    :return: A :class:`RenderedContent` — the ``hashtags`` carry the
        normalized names so the caller can also emit ``Hashtag`` entries
        in the actor's ``tag`` array.
    """
    parts: List[str] = []
    hashtags: List[str] = []
    seen = set()
    pos = 0

    for match in _BIO_TOKEN_RE.finditer(bio):
        token = match.group(0)
        parts.append(_escape_text(bio[pos : match.start()]))
        pos = match.end()

        if token.startswith(("#", "＃")):
            normalized = _hashtag_name(token[1:])
            if normalized is None:
                parts.append(html.escape(token))
            else:
                parts.append(
                    render_link_anchor(hashtag_url(normalized), rel="tag", label=token)
                )
                if normalized not in seen:
                    seen.add(normalized)
                    hashtags.append(normalized)
            continue

        if token.startswith("@"):
            user, _, domain = token[1:].partition("@")
            if domain:
                parts.append(
                    render_link_anchor(f"https://{domain}/@{user}", label=token)
                )
            else:
                parts.append(html.escape(token))
            continue

        url, trailing = _split_trailing_punctuation(token)
        if is_linkable_url(url):
            parts.append(render_link_anchor(url))
            parts.append(html.escape(trailing))
        else:
            parts.append(html.escape(token))

    parts.append(_escape_text(bio[pos:]))
    return RenderedContent("".join(parts), hashtags)


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

        if token.startswith(("#", "＃")):
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
    name: str, url: str, label: Optional[str] = None, render_html: bool = True
) -> Dict[str, str]:
    """
    Build a ``PropertyValue`` attachment dict for ``ActorConfig.attachment``.

    With ``render_html=True`` (the default) the returned ``value`` is an HTML
    anchor with ``rel="me"`` when ``url`` is valid, or escaped plain text
    otherwise — the Mastodon-style shape.  With ``render_html=False`` the
    ``value`` is the bare URL, the shape Funkwhale emits and the only one its
    ``AttachmentSerializer`` accepted as an ``URLField`` up to release 2.0.x —
    consumers on those releases reject HTML-valued attachments on actor
    documents during fetches.

    This helper does not perform the actual verification: remote servers
    verify by fetching the linked page and checking for a backlink.

    :param name: Field label (e.g. ``"Website"``).
    :param url: Link URL.
    :param label: Optional link text; defaults to the scheme-less display URL.
    :param render_html: Whether to render ``value`` as a ``rel="me"`` anchor.
    :return: A ``{"type": "PropertyValue", "name": ..., "value": ...}`` dict.
    """
    return {
        "type": "PropertyValue",
        "name": name,
        "value": render_verified_link(url, label=label) if render_html else url,
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


# ``xsd:duration`` restricted to the dayTime components media objects use
# (``PnDTnHnMnS``); year/month/week components are rejected.
_XSD_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def parse_duration(value: Any) -> Optional[float]:
    """
    Parse an ActivityStreams ``duration`` value into seconds.

    The AS2 vocabulary defines ``duration`` as an ``xsd:duration`` string
    (e.g. ``PT3M25S``) — what spec-compliant servers emit — while some
    producers send bare seconds as an int, float or numeric string.  All
    three shapes are accepted; unparseable values and non-strings that
    aren't numbers (including ``bool``) return ``None``.

    Examples::

        parse_duration("PT3S")     -> 3.0
        parse_duration("PT1H2M3S") -> 3723.0
        parse_duration("P1DT1H")   -> 90000.0
        parse_duration(63.9)       -> 63.9
        parse_duration("PT")       -> None

    :param value: The ``duration`` value of a media object.
    :return: The duration in seconds, or ``None`` when unparseable.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # A bare numeric string is seconds (legacy producers).
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    match = _XSD_DURATION_RE.match(text)
    if match is None or not any(match.groups()):
        return None
    days = float(match.group("days") or 0)
    hours = float(match.group("hours") or 0)
    minutes = float(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


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
