"""
HTTP Signatures implementation (draft-cavage-http-signatures-12).

Signs and verifies HTTP requests using RSA-SHA256. This is a self-contained
implementation that depends only on the ``cryptography`` library — no external
``httpsig`` dependency needed.
"""

import base64
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .._exceptions import SignatureVerificationError

logger = logging.getLogger(__name__)

# Headers that must be signed for outgoing requests
DEFAULT_SIGNED_HEADERS = ["(request-target)", "host", "date", "digest"]


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
