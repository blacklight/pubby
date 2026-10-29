//! Plain-text renderers for outbound ActivityPub content — mirrors
//! `pubby.content`.
//!
//! Turns trusted-ish local plain text (profile bios, post bodies, profile
//! links) into safe HTML that remote servers will render. Everything is
//! HTML-escaped, and only validated `http`/`https` URLs become anchors.

use std::collections::HashSet;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{json, Value};

/// Matches http(s) URLs in free text. It stops before whitespace and
/// HTML-breaking characters so matches can never break out of the escaped
/// text around them.
static URL_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"https?://[^\s<>"']+"#).unwrap());

/// Matches `#hashtags` in free text (candidate matches — the boundary
/// check equivalent to Python's `(?<![\w&#])` lookbehind is applied
/// separately in [`hashtag_start_ok`]).
static HASHTAG_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"#[A-Za-z0-9_]+").unwrap());

/// `xsd:duration` restricted to the dayTime components media objects use
/// (`PnDTnHnMnS`); year/month/week components are rejected.
static XSD_DURATION_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    )
    .unwrap()
});

/// Closing brackets that often wrap a URL in prose; stripped only when
/// they are unmatched inside the URL itself.
const URL_BRACKET_PAIRS: &[(char, char)] = &[(')', '('), (']', '['), ('}', '{')];

/// Escape plain text for HTML output (no newline handling).
pub fn html_escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            _ => out.push(c),
        }
    }
    out
}

/// The result of rendering a plain-text post.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RenderedContent {
    /// Safe HTML for an ActivityPub `content` or `summary` field.
    pub html: String,
    /// Normalized, deduplicated hashtag names, in first-seen order.
    pub hashtags: Vec<String>,
}

/// Return `true` when `url` is a well-formed http(s) URL safe to link.
///
/// Values containing whitespace or quote/angle-bracket characters — or
/// URLs stored before validation existed — must not become anchors:
/// remote servers render these fields as HTML.
pub fn is_linkable_url(url: &str) -> bool {
    if url.is_empty()
        || url
            .chars()
            .any(|c| c.is_whitespace() || matches!(c, '"' | '\'' | '<' | '>'))
    {
        return false;
    }
    match url::Url::parse(url) {
        Ok(parsed) => {
            matches!(parsed.scheme(), "http" | "https")
                && parsed.host_str().map(|h| !h.is_empty()).unwrap_or(false)
        }
        Err(_) => false,
    }
}

/// Return `url` without its http(s) scheme, for use as link text.
pub fn display_url(url: &str) -> String {
    // Only the first scheme separator is dropped (Python:
    // `url.split("://", 1)[-1]`).
    let display = url.split_once("://").map(|(_, rest)| rest).unwrap_or(url);
    let mut display = display.to_string();
    if display.ends_with('/') && display.matches('/').count() == 1 {
        display.pop();
    }
    display
}

/// Render a validated URL as an HTML anchor.
///
/// :param rel: Optional `rel` attribute (e.g. `"tag"` or `"me"`).
/// :param label: Optional link text; defaults to the scheme-less display
///     URL.
pub fn render_link_anchor(url: &str, rel: Option<&str>, label: Option<&str>) -> String {
    let href = html_escape(url);
    let text = html_escape(label.unwrap_or(&display_url(url)));
    let rel_attr = rel
        .map(|r| format!(" rel=\"{}\"", html_escape(r)))
        .unwrap_or_default();
    format!("<a href=\"{href}\"{rel_attr}>{text}</a>")
}

/// Split sentence punctuation off the end of a URL matched in prose.
fn split_trailing_punctuation(url: &str) -> (String, String) {
    let mut url = url.to_string();
    let mut trailing = String::new();
    while let Some(last) = url.chars().last() {
        let unmatched_close = URL_BRACKET_PAIRS
            .iter()
            .find(|(close, _)| *close == last)
            .map(|(close, open)| url.matches(*close).count() > url.matches(*open).count())
            .unwrap_or(false);
        if matches!(last, '.' | ',' | ';' | ':' | '!' | '?' | '\'' | '"') || unmatched_close {
            trailing.insert(0, last);
            url.pop();
        } else {
            break;
        }
    }
    (url, trailing)
}

/// Escape plain text for HTML output, preserving line breaks.
///
/// `\r\n` and `\r` are normalized to `\n`, and newlines are emitted as
/// `<br>` elements: ActivityPub `content`/`summary` are HTML fields, so a
/// literal newline would collapse to a space on remote renderers.
fn escape_text(text: &str) -> String {
    let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
    html_escape(&normalized).replace('\n', "<br>")
}

/// Matches fediverse handles `@user@domain` in free text (the boundary
/// check equivalent to the hashtag lookbehind is applied separately in
/// [`handle_start_ok`]).
static HANDLE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"@[A-Za-z0-9_][A-Za-z0-9_.-]*@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}").unwrap()
});

/// Whether `text[..start]` ends with a character that disqualifies a
/// handle match at `start`.
fn handle_start_ok(text: &str, start: usize) -> bool {
    match text[..start].chars().last() {
        None => true,
        Some(c) => !(c.is_alphanumeric() || c == '_' || c == '&' || c == '@'),
    }
}

#[derive(Debug, Clone, Copy)]
enum BioToken {
    Url,
    Tag,
    Handle,
}

/// `(start, end, kind)` spans for bio rendering — URL matches win over
/// hashtag/handle candidates inside them.
fn bio_tokens(text: &str) -> Vec<(usize, usize, BioToken)> {
    let url_matches: Vec<(usize, usize)> = URL_RE
        .find_iter(text)
        .map(|m| (m.start(), m.end()))
        .collect();
    let in_url = |pos: usize| url_matches.iter().any(|(s, e)| pos >= *s && pos < *e);
    let mut tokens: Vec<(usize, usize, BioToken)> = url_matches
        .iter()
        .map(|(s, e)| (*s, *e, BioToken::Url))
        .collect();
    for m in HASHTAG_RE.find_iter(text) {
        if hashtag_start_ok(text, m.start()) && !in_url(m.start()) {
            tokens.push((m.start(), m.end(), BioToken::Tag));
        }
    }
    for m in HANDLE_RE.find_iter(text) {
        if handle_start_ok(text, m.start()) && !in_url(m.start()) {
            tokens.push((m.start(), m.end(), BioToken::Handle));
        }
    }
    tokens.sort_by_key(|(s, _, _)| *s);
    tokens
}

/// Render profile bio text for an actor `summary` field.
///
/// The text is HTML-escaped, http(s) URLs become anchors with scheme-less
/// link text, `#hashtags` become `rel="tag"` links to `/tags/{name}`, and
/// `@user@domain` handles become links to `https://{domain}/@{user}` —
/// the same serialization Mastodon emits in `summary`. Newlines become
/// `<br>` elements. Matches that fail validation are left as escaped
/// text.
pub fn render_bio_html(bio: &str) -> String {
    let mut parts: Vec<String> = Vec::new();
    let mut pos = 0usize;

    for (start, end, kind) in bio_tokens(bio) {
        let token = &bio[start..end];
        parts.push(escape_text(&bio[pos..start]));
        pos = end;
        match kind {
            BioToken::Tag => match hashtag_name(&token[1..]) {
                Some(name) => parts.push(render_link_anchor(
                    &format!("/tags/{name}"),
                    Some("tag"),
                    Some(token),
                )),
                None => parts.push(html_escape(token)),
            },
            BioToken::Handle => match token[1..].split_once('@') {
                Some((user, domain)) => parts.push(render_link_anchor(
                    &format!("https://{domain}/@{user}"),
                    None,
                    Some(token),
                )),
                None => parts.push(html_escape(token)),
            },
            BioToken::Url => {
                let (url, trailing) = split_trailing_punctuation(token);
                if is_linkable_url(&url) {
                    parts.push(render_link_anchor(&url, None, None));
                    parts.push(html_escape(&trailing));
                } else {
                    parts.push(html_escape(token));
                }
            }
        }
    }

    parts.push(escape_text(&bio[pos..]));
    parts.concat()
}

fn hashtag_name(name: &str) -> Option<String> {
    let normalized = name.to_lowercase();
    if normalized.chars().any(|c| c.is_alphabetic()) {
        Some(normalized)
    } else {
        None
    }
}

/// Whether `text[..start]` ends with a character that disqualifies a
/// hashtag match at `start` (equivalent to the `(?<![\w&#])` lookbehind).
fn hashtag_start_ok(text: &str, start: usize) -> bool {
    match text[..start].chars().last() {
        None => true,
        Some(c) => !(c.is_alphanumeric() || c == '_' || c == '&' || c == '#'),
    }
}

/// Find `(start, end, is_hashtag)` token spans for post rendering.
/// A hashtag candidate inside a URL match is consumed by the URL.
fn post_tokens(text: &str) -> Vec<(usize, usize, bool)> {
    let url_matches: Vec<(usize, usize)> = URL_RE
        .find_iter(text)
        .map(|m| (m.start(), m.end()))
        .collect();
    let mut tokens: Vec<(usize, usize, bool)> =
        url_matches.iter().map(|(s, e)| (*s, *e, false)).collect();

    for m in HASHTAG_RE.find_iter(text) {
        if !hashtag_start_ok(text, m.start()) {
            continue;
        }
        if url_matches
            .iter()
            .any(|(s, e)| m.start() >= *s && m.start() < *e)
        {
            continue;
        }
        tokens.push((m.start(), m.end(), true));
    }

    tokens.sort_by_key(|(s, _, _)| *s);
    tokens
}

/// Render post body text as safe ActivityPub HTML.
///
/// The text is HTML-escaped, http(s) URLs become anchors with scheme-less
/// link text, and `#hashtags` become `rel="tag"` links produced by the
/// supplied `hashtag_url` callback. Matches that fail validation are
/// emitted as escaped text. Newlines become `<br>` elements.
///
/// :param hashtag_url: Function that receives a normalized hashtag name
///     and returns its local URL.
pub fn render_post_html(text: &str, hashtag_url: impl Fn(&str) -> String) -> RenderedContent {
    let mut parts: Vec<String> = Vec::new();
    let mut hashtags: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    let mut pos = 0usize;

    for (start, end, is_hashtag) in post_tokens(text) {
        let token = &text[start..end];
        parts.push(escape_text(&text[pos..start]));
        pos = end;

        if is_hashtag {
            match hashtag_name(&token[1..]) {
                Some(normalized) => {
                    parts.push(render_link_anchor(
                        &hashtag_url(&normalized),
                        Some("tag"),
                        Some(token),
                    ));
                    if seen.insert(normalized.clone()) {
                        hashtags.push(normalized);
                    }
                }
                None => parts.push(html_escape(token)),
            }
            continue;
        }

        let (url, trailing) = split_trailing_punctuation(token);
        if is_linkable_url(&url) {
            parts.push(render_link_anchor(&url, None, None));
            parts.push(html_escape(&trailing));
        } else {
            parts.push(html_escape(token));
        }
    }

    parts.push(escape_text(&text[pos..]));
    RenderedContent {
        html: parts.concat(),
        hashtags,
    }
}

/// Build ActivityPub `Hashtag` tag values from normalized hashtag names.
///
/// Preserves input order and does **not** deduplicate.
pub fn build_hashtag_tags(
    names: impl IntoIterator<Item = String>,
    hashtag_url: impl Fn(&str) -> String,
) -> Vec<Value> {
    names
        .into_iter()
        .map(|name| {
            json!({
                "type": "Hashtag",
                "name": format!("#{name}"),
                "href": hashtag_url(&name),
            })
        })
        .collect()
}

/// Render a profile link URL as an HTML anchor, or escaped plain text.
///
/// `rel="me"` marks the anchor as an identity link so remote servers can
/// verify it. Invalid URLs are emitted as escaped text so they stay inert.
pub fn render_verified_link(url: &str, label: Option<&str>) -> String {
    if !is_linkable_url(url) {
        return html_escape(url);
    }
    render_link_anchor(url, Some("me"), label)
}

/// Build a `PropertyValue` attachment dict for `ActorConfig.attachment`.
///
/// With `render_html=true` the `value` is an HTML anchor with `rel="me"`
/// when `url` is valid, or escaped plain text otherwise — the
/// Mastodon-style shape. With `render_html=false` the `value` is the bare
/// URL, the shape Funkwhale emits.
pub fn property_value_attachment(
    name: &str,
    url: &str,
    label: Option<&str>,
    render_html: bool,
) -> Value {
    json!({
        "type": "PropertyValue",
        "name": name,
        "value": if render_html { render_verified_link(url, label) } else { url.to_string() },
    })
}

/// Format a number of seconds as an ISO-8601 duration string.
///
/// Suitable for the `duration` field of media objects (`Audio`,
/// `Video`). The value is truncated toward zero; hours and minutes are
/// only emitted when non-zero. Negative values are treated as zero.
///
/// Non-finite input (`NaN`, `±∞`) raises
/// [`crate::error::ActivityPubError::InvalidValue`], mirroring the Python
/// `int(seconds)` `ValueError` — silently clamping would produce a
/// plausible-looking wrong duration.
pub fn format_duration(seconds: f64) -> crate::error::Result<String> {
    if !seconds.is_finite() {
        return Err(crate::error::ActivityPubError::InvalidValue(format!(
            "cannot format non-finite duration: {seconds}"
        )));
    }
    let total = seconds as i64;
    if total <= 0 {
        return Ok("PT0S".to_string());
    }
    let (hours, rem) = (total / 3600, total % 3600);
    let (minutes, secs) = (rem / 60, rem % 60);
    let mut duration = "PT".to_string();
    if hours > 0 {
        duration += &format!("{hours}H");
    }
    if minutes > 0 {
        duration += &format!("{minutes}M");
    }
    if secs > 0 {
        duration += &format!("{secs}S");
    }
    Ok(duration)
}

/// Parse an ActivityStreams `duration` value into seconds.
///
/// Accepts `xsd:duration` strings (`PT3M25S`), bare seconds as a number or
/// numeric string. Unparseable values return `None`.
pub fn parse_duration(value: &Value) -> Option<f64> {
    match value {
        Value::Null => None,
        Value::Bool(_) => None,
        Value::Number(n) => n.as_f64().map(|f| f.max(0.0)),
        Value::String(s) => {
            let text = s.trim();
            if text.is_empty() {
                return None;
            }
            if let Ok(f) = text.parse::<f64>() {
                return Some(f.max(0.0));
            }
            let m = XSD_DURATION_RE.captures(text)?;
            if m.iter().skip(1).all(|g| g.is_none()) {
                return None;
            }
            let group = |name: &str| -> f64 {
                m.name(name)
                    .and_then(|g| g.as_str().parse::<f64>().ok())
                    .unwrap_or(0.0)
            };
            Some(
                group("days") * 86400.0
                    + group("hours") * 3600.0
                    + group("minutes") * 60.0
                    + group("seconds"),
            )
        }
        _ => None,
    }
}

/// Set `obj["content"]` from rendered plain text and merge detected
/// hashtags into `obj["tag"]`.
///
/// The text is rendered via [`render_post_html`]. Hashtags found in the
/// text are appended to the object's `tag` list as `Hashtag` dicts,
/// deduplicating (case-insensitively) against existing tag names.
///
/// No-op when `text` is empty or blank or `obj` is not a JSON object.
pub fn set_object_content(obj: &mut Value, text: &str, hashtag_url: impl Fn(&str) -> String) {
    if text.trim().is_empty() {
        return;
    }
    let rendered = render_post_html(text, &hashtag_url);
    let Some(map) = obj.as_object_mut() else {
        return;
    };
    if !rendered.html.is_empty() {
        map.insert("content".to_string(), json!(rendered.html));
    }
    if rendered.hashtags.is_empty() {
        return;
    }
    let tags = map
        .entry("tag".to_string())
        .or_insert_with(|| Value::Array(Vec::new()));
    let Some(tags) = tags.as_array_mut() else {
        return;
    };
    let seen: HashSet<String> = tags
        .iter()
        .filter_map(|t| {
            t.as_object()
                .and_then(|o| o.get("name"))
                .and_then(|n| n.as_str())
                .map(|s| s.to_lowercase())
        })
        .collect();
    let new_names: Vec<String> = rendered
        .hashtags
        .into_iter()
        .filter(|name| !seen.contains(&format!("#{name}")))
        .collect();
    tags.extend(build_hashtag_tags(new_names, hashtag_url));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn format_duration_basic_and_nonfinite() {
        assert_eq!(format_duration(3723.0).unwrap(), "PT1H2M3S");
        assert!(format_duration(f64::NAN).is_err());
        assert!(format_duration(f64::INFINITY).is_err());
    }

    #[test]
    fn display_url_first_scheme_only() {
        assert_eq!(display_url("https://a.com/x://y"), "a.com/x://y");
        assert_eq!(display_url("https://a.com/"), "a.com");
    }

    #[test]
    fn link_anchor_escapes_rel() {
        let a = render_link_anchor("https://e.com", Some(r#"x"onclick="y"#), None);
        assert!(!a.contains(r#"rel="x"onclick"#), "{a}");
    }
}

#[cfg(test)]
mod bio_tests {
    use super::render_bio_html;

    #[test]
    fn bio_renders_hashtags_handles_and_urls() {
        let html = render_bio_html("see #Yonder or @fabio@manganiello.eu — https://e.com");
        assert!(
            html.contains(r#"<a href="/tags/yonder" rel="tag">#Yonder</a>"#),
            "{html}"
        );
        assert!(
            html.contains(r#"<a href="https://manganiello.eu/@fabio">@fabio@manganiello.eu</a>"#),
            "{html}"
        );
        assert!(
            html.contains(r#"<a href="https://e.com">e.com</a>"#),
            "{html}"
        );
        // A `#` mid-word and an email are left alone.
        let html = render_bio_html("a#b x@y.zz user@example.com");
        assert!(!html.contains("<a"), "{html}");
    }
}
