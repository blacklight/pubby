"""
Render ActivityPub interactions as HTML — same pattern as webmentions renderer.
"""

import datetime
import os
from pathlib import Path
from typing import Callable, Collection, Union
from urllib.parse import urlparse

from jinja2 import Environment, PackageLoader, Template, select_autoescape
from markupsafe import Markup

from .._model import Interaction

TemplateLike = Union[str, Path, Template]


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
        rendered = [self.render_interaction(i) for i in interactions]
        return self._get_markup(
            template, default="interactions.html", interactions=rendered
        )
