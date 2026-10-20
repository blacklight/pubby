//! Render ActivityPub interactions as HTML — mirrors `pubby.render`.
//!
//! Uses `minijinja` (Jinja2-compatible) with the same bundled templates
//! as the Python implementation.

use std::sync::LazyLock;

use minijinja::{Environment, Value as MjValue};
use serde_json::Value;

use crate::model::{Interaction, InteractionType};

const INTERACTION_TEMPLATE: &str = include_str!("templates/interaction.html");
const INTERACTIONS_TEMPLATE: &str = include_str!("templates/interactions.html");

// HTML sanitisation: tags and attributes allowed in interaction content.
const ALLOWED_TAGS: &[&str] = &[
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
];
const ALLOWED_ATTRS: &[&str] = &["href", "class", "rel", "translate", "title", "lang", "dir"];

static TAG_RE: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(r"(?s)<(/?)(\w+)([^>]*)>").unwrap());
static ATTR_RE: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(r#"(\w[\w-]*)=["']([^"']*)["']"#).unwrap());
static ENTITY_RE: LazyLock<regex::Regex> = LazyLock::new(|| {
    regex::Regex::new(r"&(?:#([0-9]+)|#[xX]([0-9a-fA-F]+)|([a-zA-Z][a-zA-Z0-9]*));").unwrap()
});

/// Decode HTML entities in `s` — numeric (`&#NN;`/`&#xHH;`) and the
/// common named references. Decoding is applied repeatedly (bounded) so
/// double-encoded payloads normalize fully before validation.
fn decode_html_entities(s: &str) -> String {
    fn named(name: &str) -> Option<char> {
        Some(match name.to_ascii_lowercase().as_str() {
            "amp" => '&',
            "lt" => '<',
            "gt" => '>',
            "quot" => '"',
            "apos" => '\'',
            "nbsp" => '\u{a0}',
            "sol" => '/',
            "lpar" => '(',
            "rpar" => ')',
            "lsqb" => '[',
            "rsqb" => ']',
            "lcub" => '{',
            "rcub" => '}',
            "num" => '#',
            "colon" => ':',
            "semi" => ';',
            "equals" => '=',
            "quest" => '?',
            "newline" => '\n',
            "tab" => '\t',
            _ => return None,
        })
    }
    let mut decoded = s.to_string();
    for _ in 0..5 {
        let next = ENTITY_RE
            .replace_all(&decoded, |caps: &regex::Captures| {
                if let Some(d) = caps.get(1) {
                    d.as_str()
                        .parse::<u32>()
                        .ok()
                        .and_then(char::from_u32)
                        .map(|c| c.to_string())
                        .unwrap_or_else(|| caps[0].to_string())
                } else if let Some(h) = caps.get(2) {
                    u32::from_str_radix(h.as_str(), 16)
                        .ok()
                        .and_then(char::from_u32)
                        .map(|c| c.to_string())
                        .unwrap_or_else(|| caps[0].to_string())
                } else {
                    named(&caps[3])
                        .map(|c| c.to_string())
                        .unwrap_or_else(|| caps[0].to_string())
                }
            })
            .to_string();
        if next == decoded {
            break;
        }
        decoded = next;
    }
    decoded
}

/// Whether an `href` value is safe to emit verbatim.
///
/// The value is HTML-entity-decoded before validation so encodings like
/// `jav&#x61;script:` can't smuggle a disallowed scheme past the check.
/// Allowed: `http`/`https` URLs and relative references (`/path`,
/// `#frag`, `page.html`). Rejected: any other scheme (`javascript:`,
/// `data:`, …) and protocol-relative `//host` URLs.
fn href_is_safe(value: &str) -> bool {
    let decoded = decode_html_entities(value);
    let v = decoded.trim();
    if v.is_empty() {
        return true;
    }
    if v.chars().any(|c| c.is_control() && c != '\t') {
        return false;
    }
    match url::Url::parse(v) {
        Ok(u) => matches!(u.scheme().to_lowercase().as_str(), "http" | "https"),
        Err(_) => {
            // Relative reference — reject protocol-relative URLs and
            // anything carrying a scheme-ish `x:` prefix a browser could
            // still resolve.
            if v.starts_with("//") {
                return false;
            }
            let seg_end = v.find(['/', '?', '#']).unwrap_or(v.len());
            !v[..seg_end].contains(':')
        }
    }
}

/// Strip disallowed tags and attributes from `html`.
pub fn sanitize_html(html: &str) -> String {
    TAG_RE
        .replace_all(html, |caps: &regex::Captures| {
            let slash = &caps[1];
            let tag = caps[2].to_lowercase();
            if !ALLOWED_TAGS.contains(&tag.as_str()) {
                return String::new();
            }
            if !slash.is_empty() {
                return format!("</{tag}>");
            }
            let attrs_str = &caps[3];
            let mut safe_attrs = Vec::new();
            for am in ATTR_RE.captures_iter(attrs_str) {
                let attr_name = am[1].to_lowercase();
                if !ALLOWED_ATTRS.contains(&attr_name.as_str()) {
                    continue;
                }
                if attr_name == "href" && !href_is_safe(am[2].trim()) {
                    continue;
                }
                safe_attrs.push(format!("{}=\"{}\"", &am[1], &am[2]));
            }
            let attr_str = if safe_attrs.is_empty() {
                String::new()
            } else {
                format!(" {}", safe_attrs.join(" "))
            };
            format!("<{tag}{attr_str}>")
        })
        .to_string()
}

/// Template helper functions — mirrors `TemplateUtils`.
pub mod template_utils {
    /// `format_date` — "%b %d, %Y".
    pub fn format_date(d: &str) -> String {
        crate::model::parse_dt_str(d)
            .map(|dt| dt.format("%b %d, %Y").to_string())
            .unwrap_or_else(|| d.to_string())
    }

    /// `format_datetime` — "%b %d, %Y at %H:%M".
    pub fn format_datetime(dt: &str) -> String {
        crate::model::parse_dt_str(dt)
            .map(|dt| dt.format("%b %d, %Y at %H:%M").to_string())
            .unwrap_or_else(|| dt.to_string())
    }

    /// `hostname` of a URL.
    pub fn hostname(url: &str) -> String {
        url::Url::parse(url)
            .ok()
            .and_then(|u| u.host_str().map(String::from))
            .unwrap_or_default()
    }

    /// `safe_url` — return the URL only when it is a well-formed
    /// http(s) URL free of attribute-breaking characters, else "".
    ///
    /// The caller auto-escapes the returned value; quotes, whitespace and
    /// angle brackets are rejected up front so a remote actor's `url`,
    /// `icon` or `id` cannot break out of `href="…"`/`src="…"`.
    pub fn safe_url(url: &str) -> String {
        let u = url.trim();
        if crate::content::is_linkable_url(u) {
            u.to_string()
        } else {
            String::new()
        }
    }

    /// `actor_fqn` — derive `@user@domain` from an actor URL.
    pub fn actor_fqn(actor_url: &str) -> String {
        let url = actor_url.trim();
        if url.is_empty() {
            return String::new();
        }
        let Ok(parsed) = url::Url::parse(url) else {
            return String::new();
        };
        let domain = parsed.host_str().unwrap_or_default();
        let path = parsed.path().trim_end_matches('/');
        let user = if let Some(rest) = path.strip_prefix("/@") {
            rest.to_string()
        } else if path.contains("/users/") {
            path.rsplit("/users/")
                .next()
                .unwrap_or_default()
                .to_string()
        } else {
            path.rsplit('/').next().unwrap_or_default().to_string()
        };
        if user.is_empty() || domain.is_empty() {
            return String::new();
        }
        format!("@{user}@{domain}")
    }
}

fn to_mj_value(interaction: &Interaction) -> MjValue {
    let mut map = serde_json::Map::new();
    map.insert(
        "interaction_type".into(),
        serde_json::json!({"value": interaction.interaction_type.as_str()}),
    );
    map.insert(
        "author_name".into(),
        serde_json::json!(interaction.author_name),
    );
    map.insert(
        "author_url".into(),
        serde_json::json!(interaction.author_url),
    );
    map.insert(
        "author_photo".into(),
        serde_json::json!(interaction.author_photo),
    );
    map.insert("content".into(), serde_json::json!(interaction.content));
    map.insert(
        "source_actor_id".into(),
        serde_json::json!(interaction.source_actor_id),
    );
    map.insert("object_id".into(), serde_json::json!(interaction.object_id));
    map.insert(
        "target_resource".into(),
        serde_json::json!(interaction.target_resource),
    );
    map.insert(
        "published".into(),
        interaction
            .published
            .as_ref()
            .map(|dt| serde_json::json!(crate::model::dt_to_iso(dt)))
            .unwrap_or(Value::Null),
    );
    MjValue::from_serialize(Value::Object(map))
}

fn build_env() -> Environment<'static> {
    let mut env = Environment::new();
    env.set_auto_escape_callback(|name| {
        // Escape embedded templates, string templates (`<string>`) and
        // unnamed contexts — matching Python's
        // `select_autoescape(default_for_string=True)`.
        if name.is_empty()
            || name == "<string>"
            || name.ends_with(".html")
            || name.ends_with(".htm")
            || name.ends_with(".xml")
        {
            minijinja::AutoEscape::Html
        } else {
            minijinja::AutoEscape::None
        }
    });
    env.add_template("interaction.html", INTERACTION_TEMPLATE)
        .expect("embedded template must parse");
    env.add_template("interactions.html", INTERACTIONS_TEMPLATE)
        .expect("embedded template must parse");
    // `safe_url` output is deliberately *not* marked safe: it renders
    // inside quoted attributes, so the auto-escaper must still apply.
    env.add_function("safe_url", |v: String| template_utils::safe_url(&v));
    env.add_function("hostname", |v: String| template_utils::hostname(&v));
    env.add_function("actor_fqn", |v: String| template_utils::actor_fqn(&v));
    env.add_function("format_date", |v: String| template_utils::format_date(&v));
    env.add_function("format_datetime", |v: String| {
        template_utils::format_datetime(&v)
    });
    env.add_function("sanitize_html", |v: String| {
        MjValue::from_safe_string(sanitize_html(&v))
    });
    env
}

/// ActivityPub interactions renderer.
///
/// Renders interactions (replies, likes, boosts) into HTML using the
/// bundled Jinja2-compatible templates.
pub struct InteractionsRenderer {
    env: Environment<'static>,
}

impl Default for InteractionsRenderer {
    fn default() -> Self {
        Self::new()
    }
}

impl InteractionsRenderer {
    /// Create a renderer with the bundled templates.
    pub fn new() -> Self {
        InteractionsRenderer { env: build_env() }
    }

    fn render_with(
        &self,
        template_source: Option<&str>,
        default: &str,
        context: MjValue,
    ) -> String {
        let result = match template_source {
            Some(src) => self
                .env
                .template_from_str(src)
                .and_then(|t| t.render(context)),
            None => self
                .env
                .get_template(default)
                .and_then(|t| t.render(context)),
        };
        result.unwrap_or_else(|e| {
            // Python lets the render error propagate; we can't change the
            // return type without breaking the API, so surface it loudly.
            log::warn!("Interaction template render failed: {e}");
            String::new()
        })
    }

    /// Render a single interaction as HTML.
    ///
    /// `template` is an optional template source overriding the bundled
    /// `interaction.html`.
    pub fn render_interaction(&self, interaction: &Interaction, template: Option<&str>) -> String {
        self.render_with(
            template,
            "interaction.html",
            minijinja::context!(interaction => to_mj_value(interaction)),
        )
    }

    /// Render a list of interactions as HTML.
    pub fn render_interactions(
        &self,
        interactions: &[Interaction],
        template: Option<&str>,
    ) -> String {
        let mut sorted: Vec<&Interaction> = interactions.iter().collect();
        sorted.sort_by_key(|i| std::cmp::Reverse(i.created_at.or(i.published).unwrap_or_default()));
        let rendered: Vec<MjValue> = sorted
            .iter()
            .map(|i| MjValue::from_safe_string(self.render_interaction(i, None)))
            .collect();
        let mut counts = serde_json::Map::new();
        for k in ["likes", "boosts", "replies", "quotes", "mentions"] {
            counts.insert(k.into(), serde_json::json!(0));
        }
        for i in &sorted {
            let key = match i.interaction_type {
                InteractionType::Like => "likes",
                InteractionType::Boost => "boosts",
                InteractionType::Reply => "replies",
                InteractionType::Quote => "quotes",
                InteractionType::Mention => "mentions",
            };
            let cur = counts[key].as_i64().unwrap_or(0);
            counts.insert(key.into(), serde_json::json!(cur + 1));
        }
        self.render_with(
            template,
            "interactions.html",
            minijinja::context!(interactions => rendered, counts => Value::Object(counts)),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_url_rejects_dangerous_urls() {
        assert_eq!(
            template_utils::safe_url("https://e.com/a"),
            "https://e.com/a"
        );
        assert_eq!(template_utils::safe_url("javascript:alert(1)"), "");
        assert_eq!(
            template_utils::safe_url("https://e.com/\" onmouseover=\"x"),
            ""
        );
        assert_eq!(template_utils::safe_url("//evil.example"), "");
    }

    #[test]
    fn sanitize_decodes_href_entities() {
        let out = sanitize_html(r#"<a href="jav&#x61;script:alert(1)">x</a>"#);
        assert!(!out.contains("href"));
        let out = sanitize_html(r#"<a href="https://e.com">x</a>"#);
        assert!(out.contains(r#"href="https://e.com""#));
    }
}
