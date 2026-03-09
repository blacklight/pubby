"""
Render ActivityPub interactions as HTML — same pattern as webmentions renderer.
"""

import datetime
import os
import re
from pathlib import Path
from typing import Callable, Collection, Union
from urllib.parse import urlparse

from jinja2 import Environment, PackageLoader, Template, select_autoescape
from markupsafe import Markup

from .._model import Interaction

TemplateLike = Union[str, Path, Template]

# HTML sanitisation: tags and attributes allowed in interaction content.
_ALLOWED_TAGS = frozenset(
    {
        "a",
        "p",
        "br",
        "span",
        "strong",
        "em",
        "b",
        "i",
        "u",
        "s",
        "del",
        "blockquote",
        "pre",
        "code",
        "ul",
        "ol",
        "li",
    }
)
_ALLOWED_ATTRS = frozenset(
    {
        "href",
        "class",
        "rel",
        "translate",
        "title",
        "lang",
        "dir",
    }
)
_TAG_RE = re.compile(r"<(/?)(\w+)([^>]*)>", re.DOTALL)
_ATTR_RE = re.compile(r'(\w[\w-]*)=["\']([^"\']*)["\']')


def _sanitize_html(html: str) -> Markup:
    """
    Strip disallowed tags and attributes from *html*, returning a
    :class:`Markup` instance safe for rendering.
    """

    def _replace_tag(m: re.Match) -> str:
        slash, tag, attrs_str = m.group(1), m.group(2).lower(), m.group(3)
        if tag not in _ALLOWED_TAGS:
            return ""
        if slash:
            return f"</{tag}>"
        # Filter attributes
        safe_attrs = []
        for am in _ATTR_RE.finditer(attrs_str):
            attr_name = am.group(1).lower()
            if attr_name in _ALLOWED_ATTRS:
                # Extra check: only allow safe href schemes
                if attr_name == "href":
                    val = am.group(2).strip()
                    parsed = urlparse(val)
                    if parsed.scheme.lower() not in ("http", "https", ""):
                        continue
                safe_attrs.append(f'{am.group(1)}="{am.group(2)}"')
        attr_str = (" " + " ".join(safe_attrs)) if safe_attrs else ""
        return f"<{tag}{attr_str}>"

    return Markup(_TAG_RE.sub(_replace_tag, html))


class TemplateUtils:
    """
    Collection of Jinja2 template helper functions.
    """

    @staticmethod
    def format_date(d: object) -> str:
        if not d:
            return ""
        if isinstance(d, datetime.datetime):
            return d.strftime("%b %d, %Y")
        if isinstance(d, str):
            return datetime.datetime.fromisoformat(d.replace("Z", "+00:00")).strftime(
                "%b %d, %Y"
            )
        return str(d)

    @staticmethod
    def format_datetime(dt: object) -> str:
        if not dt:
            return ""
        if isinstance(dt, datetime.datetime):
            return dt.strftime("%b %d, %Y at %H:%M")
        if isinstance(dt, str):
            return datetime.datetime.fromisoformat(dt.replace("Z", "+00:00")).strftime(
                "%b %d, %Y at %H:%M"
            )
        return str(dt)

    @staticmethod
    def hostname(url: str) -> str:
        if not url:
            return ""
        return urlparse(url).hostname or ""

    @staticmethod
    def safe_url(url: object) -> str:
        if not url:
            return ""
        u = str(url).strip()
        parsed = urlparse(u)
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        if not parsed.netloc:
            return ""
        return u

    @staticmethod
    def sanitize_html(html: object) -> Markup:
        """Sanitize HTML content, returning a safe :class:`Markup` instance."""
        if not html:
            return Markup("")
        return _sanitize_html(str(html))

    @staticmethod
    def actor_fqn(actor_url: object) -> str:
        """Derive ``@user@domain`` from an ActivityPub actor URL."""
        if not actor_url:
            return ""
        url = str(actor_url).strip()
        parsed = urlparse(url)
        domain = parsed.hostname or ""
        path = parsed.path.rstrip("/")
        # Common patterns: /@user, /users/user, /user
        if path.startswith("/@"):
            user = path[2:]
        elif "/users/" in path:
            user = path.split("/users/")[-1]
        else:
            user = path.rsplit("/", 1)[-1] if "/" in path else ""
        if not user or not domain:
            return ""
        return f"@{user}@{domain}"

    @classmethod
    def to_dict(cls) -> dict[str, Callable]:
        helpers: dict[str, Callable] = {}
        for name in dir(cls):
            if name.startswith("_"):
                continue
            value = getattr(cls, name)
            if callable(value):
                helpers[name] = value
        return helpers


class InteractionsRenderer:
    """
    ActivityPub interactions renderer.

    Renders interactions (replies, likes, boosts) into HTML
    using Jinja2 templates.
    """

    def _get_template(self, template: TemplateLike | None, *, default: str) -> Template:
        env = Environment(
            loader=PackageLoader("pubby", "templates"),
            autoescape=select_autoescape(enabled_extensions=("html", "xml")),
        )

        template_obj = None
        if template is None:
            template_obj = env.get_template(default)
        elif isinstance(template, Path) or (
            isinstance(template, str) and os.path.isfile(template)
        ):
            template_path = Path(template)
            template_obj = env.from_string(template_path.read_text(encoding="utf-8"))
        elif isinstance(template, str):
            template_obj = env.from_string(template)
        elif isinstance(template, Template):
            template_obj = template

        if not template_obj:
            raise ValueError(f"Invalid template: {template}")

        return template_obj

    def _get_markup(
        self, template: TemplateLike | None, *, default: str, **kwargs
    ) -> Markup:
        template_obj = self._get_template(template, default=default)
        return Markup(template_obj.render(**kwargs, **TemplateUtils.to_dict()))

    def render_interaction(
        self,
        interaction: Interaction,
        template: TemplateLike | None = None,
    ) -> Markup:
        """
        Render a single interaction as HTML.

        :param interaction: The interaction to render.
        :param template: Optional custom template.
        :return: Rendered HTML markup.
        """
        return self._get_markup(
            template, default="interaction.html", interaction=interaction
        )

    def render_interactions(
        self,
        interactions: Collection[Interaction],
        template: TemplateLike | None = None,
    ) -> Markup:
        """
        Render a list of interactions as HTML.

        :param interactions: The interactions to render.
        :param template: Optional custom template.
        :return: Rendered HTML markup.
        """

        def _sort_key(interaction: Interaction):
            return (
                interaction.created_at
                or interaction.published
                or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
            )

        sorted_interactions = sorted(
            interactions,
            key=_sort_key,
            reverse=True,
        )
        rendered = [self.render_interaction(i) for i in sorted_interactions]
        counts = {"likes": 0, "boosts": 0, "replies": 0, "quotes": 0, "mentions": 0}
        for i in sorted_interactions:
            itype = getattr(i, "interaction_type", None)
            if itype is not None:
                type_val = itype.value if hasattr(itype, "value") else str(itype)
                if type_val == "like":
                    counts["likes"] += 1
                elif type_val == "boost":
                    counts["boosts"] += 1
                elif type_val == "reply":
                    counts["replies"] += 1
                elif type_val == "quote":
                    counts["quotes"] += 1
                else:
                    counts["mentions"] += 1
        return self._get_markup(
            template,
            default="interactions.html",
            interactions=rendered,
            counts=counts,
        )
