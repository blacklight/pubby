"""
HTTP Signatures implementation (draft-cavage-http-signatures-12).

Signs and verifies HTTP requests using RSA-SHA256. This is a self-contained
implementation that depends only on the ``cryptography`` library — no external
``httpsig`` dependency needed.
"""

import base64
import enum
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from .._exceptions import ActivityPubError, SignatureVerificationError

logger = logging.getLogger(__name__)

# Headers that must be signed for outgoing requests
DEFAULT_SIGNED_HEADERS = ["(request-target)", "host", "date", "digest"]


class SignatureScheme(str, enum.Enum):
    """
    Wire format for outgoing HTTP signatures.

    Inbound verification never needs configuring: a request carrying
    ``Signature-Input`` is verified as RFC 9421, anything else as
    draft-cavage.
    """

    DRAFT_CAVAGE = "draft-cavage"
    RFC9421 = "rfc9421"


RFC9421_ALG = "rsa-v1_5-sha256"
RFC9421_ALG_ED25519 = "ed25519"
RFC9421_DEFAULT_COMPONENTS = ["@method", "@authority", "@path"]
_RFC9421_LABEL = "sig1"
_RFC9421_REQUIRED_COMPONENTS = ("@method", "@authority", "@path")
_MAX_SIGNED_CREATED_SKEW_SECS = 300


def _build_digest(body: bytes) -> str:
    """
    Build the SHA-256 digest of a request body.

    :param body: Raw request body bytes.
    :return: The ``Digest`` header value (``SHA-256=<base64>``).
    """
    sha256 = hashlib.sha256(body).digest()
    return f"SHA-256={base64.b64encode(sha256).decode('ascii')}"


def _build_signing_string(
    method: str,
    path: str,
    headers: dict[str, str],
    signed_headers: list[str],
) -> str:
    """
    Build the signing string per draft-cavage-http-signatures-12.

    :param method: HTTP method (lowercase).
    :param path: Request path including query string.
    :param headers: Dictionary of header name → value (case-insensitive keys).
    :param signed_headers: List of header names to include in the signature.
    :return: The signing string.
    """
    # Normalize header keys to lowercase for lookup
    lower_headers = {k.lower(): v for k, v in headers.items()}
    parts = []
    for h in signed_headers:
        h_lower = h.lower()
        if h_lower == "(request-target)":
            parts.append(f"(request-target): {method.lower()} {path}")
        else:
            value = lower_headers.get(h_lower, "")
            parts.append(f"{h_lower}: {value}")
    return "\n".join(parts)


def sign_request(
    private_key: rsa.RSAPrivateKey,
    key_id: str,
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    signed_headers: list[str] | None = None,
) -> dict[str, str]:
    """
    Sign an outgoing HTTP request and return the headers to include.

    Returns a dictionary with ``Date``, ``Digest`` (if body is present),
    ``Host``, and ``Signature`` headers.

    :param private_key: RSA private key for signing.
    :param key_id: The key ID to include in the signature
        (typically ``<actor_id>#main-key``).
    :param method: HTTP method (e.g. ``POST``).
    :param url: Full request URL.
    :param body: Request body bytes (optional; required for POST).
    :param headers: Existing headers to include in signing. If ``Date`` or
        ``Host`` are not provided they will be generated.
    :param signed_headers: List of headers to sign. Defaults to
        ``["(request-target)", "host", "date", "digest"]``.
    :return: Dictionary of headers to add to the request.
    """
    if signed_headers is None:
        signed_headers = list(DEFAULT_SIGNED_HEADERS)

    # Remove digest from signed headers if there's no body
    if body is None or len(body) == 0:
        signed_headers = [h for h in signed_headers if h.lower() != "digest"]

    parsed = urlparse(url)
    path = parsed.path
    if parsed.query:
        path = f"{path}?{parsed.query}"

    host = parsed.hostname or ""
    if parsed.port and parsed.port not in (80, 443):
        host = f"{host}:{parsed.port}"

    out_headers: dict[str, str] = dict(headers or {})

    if "Host" not in out_headers and "host" not in out_headers:
        out_headers["Host"] = host

    if "Date" not in out_headers and "date" not in out_headers:
        out_headers["Date"] = datetime.now(timezone.utc).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )

    if body and "Digest" not in out_headers and "digest" not in out_headers:
        out_headers["Digest"] = _build_digest(body)

    signing_string = _build_signing_string(method, path, out_headers, signed_headers)

    signature_bytes = private_key.sign(
        signing_string.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    sig_b64 = base64.b64encode(signature_bytes).decode("ascii")

    header_list = " ".join(signed_headers)
    out_headers["Signature"] = (
        f'keyId="{key_id}",'
        f'algorithm="rsa-sha256",'
        f'headers="{header_list}",'
        f'signature="{sig_b64}"'
    )

    return out_headers


def _parse_signature_header(header: str) -> dict[str, str]:
    """
    Parse a Signature header value into its component fields.

    :param header: The raw Signature header string.
    :return: Dictionary with keys like ``keyId``, ``algorithm``,
        ``headers``, ``signature``.
    """
    result: dict[str, str] = {}
    # Simple parser that handles quoted values
    remaining = header.strip()
    while remaining:
        eq_idx = remaining.find("=")
        if eq_idx == -1:
            break
        key = remaining[:eq_idx].strip().lstrip(",").strip()
        remaining = remaining[eq_idx + 1 :].strip()

        if remaining.startswith('"'):
            # Quoted value
            end_quote = remaining.find('"', 1)
            if end_quote == -1:
                result[key] = remaining[1:]
                break
            result[key] = remaining[1:end_quote]
            remaining = remaining[end_quote + 1 :].strip()
            if remaining.startswith(","):
                remaining = remaining[1:].strip()
        else:
            # Unquoted value (until comma or end)
            comma_idx = remaining.find(",")
            if comma_idx == -1:
                result[key] = remaining.strip()
                break
            result[key] = remaining[:comma_idx].strip()
            remaining = remaining[comma_idx + 1 :].strip()

    return result


def verify_request(
    public_key: rsa.RSAPublicKey,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes | None = None,
) -> bool:
    """
    Verify the HTTP signature on an incoming request.

    :param public_key: The sender's RSA public key.
    :param method: HTTP method (e.g. ``POST``).
    :param path: Request path including query string.
    :param headers: Dictionary of request headers.
    :param body: Request body bytes (for digest verification).
    :return: True if the signature is valid.
    :raises SignatureVerificationError: If the signature is missing or invalid.
    """
    # Find the Signature header (case-insensitive)
    lower_headers = {k.lower(): v for k, v in headers.items()}
    sig_header = lower_headers.get("signature")
    if not sig_header:
        raise SignatureVerificationError("Missing Signature header")

    parsed = _parse_signature_header(sig_header)
    if "signature" not in parsed:
        raise SignatureVerificationError("Signature header has no signature field")

    signed_headers = parsed.get("headers", "date").split()
    sig_bytes = base64.b64decode(parsed["signature"])

    # Verify the digest if present and body provided
    if body is not None and "digest" in lower_headers:
        expected_digest = _build_digest(body)
        actual_digest = lower_headers["digest"]
        if expected_digest != actual_digest:
            raise SignatureVerificationError(
                f"Digest mismatch: expected {expected_digest}, got {actual_digest}"
            )

    signing_string = _build_signing_string(method, path, headers, signed_headers)

    try:
        public_key.verify(
            sig_bytes,
            signing_string.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception as e:
        raise SignatureVerificationError(f"Signature verification failed: {e}") from e

    return True


# ---------------------------------------------------------------------------
# RFC 9421 HTTP Message Signatures (rsa-v1_5-sha256)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ed25519Identity:
    """
    An Ed25519 signing key and the ``keyId`` it is advertised under
    (``<actor_id>#ed25519-key``).
    """

    key_id: str
    key: ed25519.Ed25519PrivateKey


def signing_identity(
    scheme: "SignatureScheme | str",
    private_key: rsa.RSAPrivateKey,
    key_id: str,
    ed25519_identity: Ed25519Identity | None,
) -> tuple[rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey, str]:
    """
    The ``(private_key, key_id)`` to sign an outgoing request with.

    An Ed25519 identity is used only for RFC 9421 requests; draft-cavage
    is RSA-only.
    """
    if (
        ed25519_identity is not None
        and SignatureScheme(scheme) is SignatureScheme.RFC9421
    ):
        return ed25519_identity.key, ed25519_identity.key_id
    return private_key, key_id


@dataclass
class ParsedSignature:
    """One signature parsed out of ``Signature-Input`` and ``Signature``."""

    label: str
    components: list[str]
    params: list[tuple[str, "str | int"]]
    value: bytes = field(default=b"")

    @property
    def key_id(self) -> str | None:
        return self._str_param("keyid")

    def _str_param(self, name: str) -> str | None:
        for k, v in self.params:
            if k == name and isinstance(v, str):
                return v
        return None

    def _int_param(self, name: str) -> int | None:
        for k, v in self.params:
            if k == name and isinstance(v, int):
                return v
        return None


def build_content_digest(body: bytes) -> str:
    """
    Build the ``Content-Digest`` value of a body (RFC 9530, ``sha-256``).

    :param body: Raw body bytes.
    :return: ``sha-256=:<base64>:``.
    """
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
    return f"sha-256=:{digest}:"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(raw: str) -> str | None:
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return None
    out: list[str] = []
    escaped = False
    for c in raw[1:-1]:
        if escaped:
            out.append(c)
            escaped = False
        elif c == "\\":
            escaped = True
        elif c == '"':
            return None
        else:
            out.append(c)
    return None if escaped else "".join(out)


def _split_top(s: str, sep: str) -> list[str]:
    """Split on ``sep`` outside double quotes and parentheses."""
    parts: list[str] = []
    depth = 0
    in_quotes = escaped = False
    start = 0
    for i, c in enumerate(s):
        if in_quotes:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_quotes = False
        elif c == '"':
            in_quotes = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == sep and depth == 0:
            parts.append(s[start:i])
            start = i + 1
    parts.append(s[start:])
    return parts


def _serialize_params(
    components: list[str], params: list[tuple[str, "str | int"]]
) -> str:
    inner = " ".join(_quote(c) for c in components)
    out = f"({inner})"
    for k, v in params:
        out += f";{k}={_quote(v) if isinstance(v, str) else v}"
    return out


def _parse_item(raw: str) -> "str | int":
    if raw.startswith('"'):
        value = _unquote(raw)
        if value is None:
            raise SignatureVerificationError(f"malformed string parameter {raw}")
        return value
    try:
        return int(raw)
    except ValueError as e:
        raise SignatureVerificationError(
            f"unsupported signature parameter value {raw}"
        ) from e


def _parse_input_member(member: str) -> ParsedSignature:
    label, sep, rest = member.partition("=")
    rest = rest.strip()
    close = rest.find(")")
    if not sep or not rest.startswith("(") or close < 0:
        raise SignatureVerificationError(f"malformed Signature-Input member {member}")

    components = []
    for token in rest[1:close].split():
        name = _unquote(token)
        if name is None:
            raise SignatureVerificationError(f"unsupported covered component {token}")
        components.append(name)

    params = []
    for p in _split_top(rest[close + 1 :], ";"):
        p = p.strip()
        if not p:
            continue
        k, psep, v = p.partition("=")
        if not psep:
            raise SignatureVerificationError(f"malformed signature parameter {p}")
        params.append((k.strip(), _parse_item(v.strip())))

    return ParsedSignature(label=label.strip(), components=components, params=params)


def parse_signatures_rfc9421(headers: dict[str, str]) -> list[ParsedSignature]:
    """
    Parse the ``Signature-Input`` and ``Signature`` fields of a request.

    :param headers: Request headers with lowercased names.
    :return: One entry per ``Signature-Input`` member, in order; empty when
        the request carries no RFC 9421 signature.
    :raises SignatureVerificationError: If the fields are malformed.
    """
    signature_input = headers.get("signature-input")
    if not signature_input:
        return []
    signature = headers.get("signature")
    if not signature:
        raise SignatureVerificationError(
            "Signature-Input present without a Signature field"
        )

    values: dict[str, bytes] = {}
    for member in _split_top(signature, ","):
        label, sep, value = member.strip().partition("=")
        if not sep or len(value) < 2 or not (value[0] == ":" and value[-1] == ":"):
            raise SignatureVerificationError("malformed Signature member")
        try:
            values[label.strip()] = base64.b64decode(value[1:-1], validate=True)
        except ValueError as e:
            raise SignatureVerificationError(f"invalid base64 signature: {e}") from e

    result = []
    for member in _split_top(signature_input, ","):
        member = member.strip()
        if not member:
            continue
        sig = _parse_input_member(member)
        if sig.label not in values:
            raise SignatureVerificationError(f"no Signature for label {sig.label}")
        sig.value = values[sig.label]
        result.append(sig)
    return result


def _rfc9421_component(
    name: str,
    method: str,
    authority: str,
    path_and_query: str,
    headers: dict[str, str],
) -> str:
    path, _, query = path_and_query.partition("?")
    if name == "@method":
        return method.upper()
    if name == "@authority":
        return authority
    if name == "@path":
        return path or "/"
    if name == "@query":
        return f"?{query}"
    if name.startswith("@"):
        raise SignatureVerificationError(f"unsupported derived component {name}")
    if name not in headers:
        raise SignatureVerificationError(f"covered component {name} is missing")
    return headers[name].strip()


def _rfc9421_signature_base(
    components: list[str],
    params: list[tuple[str, "str | int"]],
    method: str,
    authority: str,
    path_and_query: str,
    headers: dict[str, str],
) -> str:
    """Build the RFC 9421 §2.5 signature base from lowercased headers."""
    lines = [
        f"{_quote(c)}: "
        + _rfc9421_component(c, method, authority, path_and_query, headers)
        for c in components
    ]
    lines.append(
        f'{_quote("@signature-params")}: {_serialize_params(components, params)}'
    )
    return "\n".join(lines)


def _has_header(headers: dict[str, str], name: str) -> bool:
    return any(k.lower() == name for k in headers)


def sign_request_rfc9421(
    private_key: rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey,
    key_id: str,
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    signed_headers: list[str] | None = None,
    *,
    created: int | None = None,
) -> dict[str, str]:
    """
    Sign an outgoing request per RFC 9421 (``rsa-v1_5-sha256``).

    Returns ``Signature-Input`` and ``Signature``, plus ``Content-Digest``
    when a body is present (RFC 9530).

    :param private_key: RSA private key for signing.
    :param key_id: The ``keyid`` parameter (typically ``<actor_id>#main-key``).
    :param method: HTTP method (e.g. ``POST``).
    :param url: Full request URL.
    :param body: Request body bytes (optional).
    :param headers: Existing headers to merge into the returned dictionary.
    :param signed_headers: Covered components. Defaults to
        ``["@method", "@authority", "@path"]``; ``content-digest`` is added
        automatically when a body is sent.
    :param created: Signature creation time (Unix seconds); defaults to now.
    :return: Dictionary of headers to add to the request.
    :raises ActivityPubError: If ``url`` is not an absolute HTTP(S) URL.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ActivityPubError(f"invalid URL for signing: {url}")
    default_port = 443 if parsed.scheme == "https" else 80
    authority = parsed.hostname.lower()
    if parsed.port is not None and parsed.port != default_port:
        authority = f"{authority}:{parsed.port}"
    path = parsed.path or "/"
    path_and_query = f"{path}?{parsed.query}" if parsed.query else path

    components = [
        c.lower()
        for c in (
            signed_headers if signed_headers is not None else RFC9421_DEFAULT_COMPONENTS
        )
    ]
    out = dict(headers or {})
    if body:
        if "content-digest" not in components:
            components.append("content-digest")
        if not _has_header(out, "content-digest"):
            out["Content-Digest"] = build_content_digest(body)
    else:
        components = [c for c in components if c != "content-digest"]

    params: list[tuple[str, str | int]] = [
        ("created", int(time.time()) if created is None else created),
        ("keyid", key_id),
        (
            "alg",
            (
                RFC9421_ALG_ED25519
                if isinstance(private_key, ed25519.Ed25519PrivateKey)
                else RFC9421_ALG
            ),
        ),
    ]
    lower = {k.lower(): v for k, v in out.items()}
    base = _rfc9421_signature_base(
        components, params, method, authority, path_and_query, lower
    )
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        signature = private_key.sign(base.encode("utf-8"))
    else:
        signature = private_key.sign(
            base.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
        )
    out["Signature-Input"] = f"{_RFC9421_LABEL}={_serialize_params(components, params)}"
    out["Signature"] = (
        f"{_RFC9421_LABEL}=:{base64.b64encode(signature).decode('ascii')}:"
    )
    return out


def verify_request_rfc9421(
    public_key: rsa.RSAPublicKey | ed25519.Ed25519PublicKey,
    signature: ParsedSignature,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes | None = None,
    *,
    now: int | None = None,
) -> bool:
    """
    Verify one parsed RFC 9421 signature against a request.

    Checks the algorithm, required components, the ``created``/``expires``
    window, ``Content-Digest`` when a body is present, and the RSA signature
    over the rebuilt signature base.

    :param public_key: The signer's RSA public key.
    :param signature: A signature from :func:`parse_signatures_rfc9421`.
    :param method: HTTP method of the request.
    :param path: Request target as received (path plus optional ``?query``).
    :param headers: Request headers (names are lowercased internally).
    :param body: Request body bytes, if any.
    :param now: Current time (Unix seconds); defaults to the wall clock.
    :return: ``True`` if the signature is valid.
    :raises SignatureVerificationError: On any verification failure.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    key_alg = (
        RFC9421_ALG_ED25519
        if isinstance(public_key, ed25519.Ed25519PublicKey)
        else RFC9421_ALG
    )
    alg = signature._str_param("alg")
    if alg is not None and alg != key_alg:
        raise SignatureVerificationError(
            f"algorithm {alg} does not match the signing key"
        )
    for required in _RFC9421_REQUIRED_COMPONENTS:
        if required not in signature.components:
            raise SignatureVerificationError(f"signature does not cover {required}")

    now = int(time.time()) if now is None else now
    created = signature._int_param("created")
    if created is None:
        raise SignatureVerificationError("signature has no created parameter")
    if abs(now - created) > _MAX_SIGNED_CREATED_SKEW_SECS:
        raise SignatureVerificationError(
            f"signature created {abs(now - created)}s off "
            f"(max {_MAX_SIGNED_CREATED_SKEW_SECS}s)"
        )
    expires = signature._int_param("expires")
    if expires is not None and now > expires:
        raise SignatureVerificationError("signature has expired")

    if body:
        if "content-digest" not in signature.components:
            raise SignatureVerificationError(
                "request has a body but content-digest is not covered"
            )
        expected = build_content_digest(body)
        actual = lower.get("content-digest", "").strip()
        if actual != expected:
            raise SignatureVerificationError(
                f"Content-Digest mismatch: expected {expected}, got {actual}"
            )

    host = lower.get("host")
    if not host:
        raise SignatureVerificationError("request has no Host header for @authority")
    base = _rfc9421_signature_base(
        signature.components,
        signature.params,
        method,
        host.strip().lower(),
        path,
        lower,
    )
    try:
        if isinstance(public_key, ed25519.Ed25519PublicKey):
            public_key.verify(signature.value, base.encode("utf-8"))
        else:
            public_key.verify(
                signature.value,
                base.encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
    except Exception as e:
        raise SignatureVerificationError(f"Signature verification failed: {e}") from e
    return True
