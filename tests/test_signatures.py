"""
Tests for HTTP signature creation and verification.
"""

import pytest

from mypub._exceptions import SignatureVerificationError
from mypub.crypto._keys import generate_rsa_keypair, export_public_key_pem
from mypub.crypto._signatures import (
    _build_digest,
    _build_signing_string,
    _parse_signature_header,
    sign_request,
    verify_request,
)


class TestDigest:
    def test_build_digest(self):
        body = b'{"hello": "world"}'
        digest = _build_digest(body)
        assert digest.startswith("SHA-256=")
        # Same input should produce the same digest
        assert _build_digest(body) == digest

    def test_empty_body_digest(self):
        digest = _build_digest(b"")
        assert digest.startswith("SHA-256=")


class TestSigningString:
    def test_build_signing_string(self):
        headers = {
            "Host": "example.com",
            "Date": "Thu, 01 Jan 2024 00:00:00 GMT",
            "Digest": "SHA-256=abc123",
        }

        result = _build_signing_string(
            "post",
            "/inbox",
            headers,
            ["(request-target)", "host", "date", "digest"],
        )

        lines = result.split("\n")
        assert lines[0] == "(request-target): post /inbox"
        assert lines[1] == "host: example.com"
        assert lines[2] == "date: Thu, 01 Jan 2024 00:00:00 GMT"
        assert lines[3] == "digest: SHA-256=abc123"

    def test_case_insensitive_headers(self):
        headers = {"HOST": "example.com", "DATE": "now"}
        result = _build_signing_string(
            "get", "/path", headers, ["host", "date"]
        )
        assert "host: example.com" in result
        assert "date: now" in result


class TestParseSignatureHeader:
    def test_parse_full_header(self):
        header = (
            'keyId="https://example.com/actor#main-key",'
            'algorithm="rsa-sha256",'
            'headers="(request-target) host date digest",'
            'signature="base64data=="'
        )
        parsed = _parse_signature_header(header)
        assert parsed["keyId"] == "https://example.com/actor#main-key"
        assert parsed["algorithm"] == "rsa-sha256"
        assert parsed["headers"] == "(request-target) host date digest"
        assert parsed["signature"] == "base64data=="

    def test_parse_minimal_header(self):
        header = 'keyId="k",signature="s"'
        parsed = _parse_signature_header(header)
        assert parsed["keyId"] == "k"
        assert parsed["signature"] == "s"


class TestSignAndVerify:
    def test_sign_request_with_body(self, rsa_keypair):
        private_key, _ = rsa_keypair
        body = b'{"type": "Follow"}'

        headers = sign_request(
            private_key=private_key,
            key_id="https://example.com/actor#main-key",
            method="POST",
            url="https://remote.example.com/inbox",
            body=body,
        )

        assert "Signature" in headers
        assert "Date" in headers
        assert "Digest" in headers
        assert "Host" in headers

        # Verify the signature parses correctly
        parsed = _parse_signature_header(headers["Signature"])
        assert parsed["keyId"] == "https://example.com/actor#main-key"
        assert parsed["algorithm"] == "rsa-sha256"
        assert "(request-target)" in parsed["headers"]

    def test_sign_request_without_body(self, rsa_keypair):
        private_key, _ = rsa_keypair

        headers = sign_request(
            private_key=private_key,
            key_id="https://example.com/actor#main-key",
            method="GET",
            url="https://remote.example.com/actor",
        )

        assert "Signature" in headers
        assert "Date" in headers
        assert "Digest" not in headers

    def test_sign_and_verify_roundtrip(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        body = b'{"type": "Create", "actor": "https://example.com/actor"}'

        headers = sign_request(
            private_key=private_key,
            key_id="https://example.com/actor#main-key",
            method="POST",
            url="https://remote.example.com/inbox",
            body=body,
        )

        # Verify should succeed
        result = verify_request(
            public_key=public_key,
            method="POST",
            path="/inbox",
            headers=headers,
            body=body,
        )
        assert result is True

    def test_verify_rejects_tampered_body(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        body = b'{"type": "Follow"}'

        headers = sign_request(
            private_key=private_key,
            key_id="https://example.com/actor#main-key",
            method="POST",
            url="https://remote.example.com/inbox",
            body=body,
        )

        # Tamper with the body
        tampered_body = b'{"type": "Delete"}'

        with pytest.raises(SignatureVerificationError, match="Digest mismatch"):
            verify_request(
                public_key=public_key,
                method="POST",
                path="/inbox",
                headers=headers,
                body=tampered_body,
            )

    def test_verify_rejects_tampered_headers(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        body = b'{"type": "Follow"}'

        headers = sign_request(
            private_key=private_key,
            key_id="https://example.com/actor#main-key",
            method="POST",
            url="https://remote.example.com/inbox",
            body=body,
        )

        # Tamper with the date header
        headers["Date"] = "Fri, 01 Jan 2099 00:00:00 GMT"

        with pytest.raises(SignatureVerificationError, match="verification failed"):
            verify_request(
                public_key=public_key,
                method="POST",
                path="/inbox",
                headers=headers,
                body=body,
            )

    def test_verify_missing_signature(self, public_key):
        with pytest.raises(
            SignatureVerificationError, match="Missing Signature"
        ):
            verify_request(
                public_key=public_key,
                method="POST",
                path="/inbox",
                headers={"Host": "example.com"},
                body=b"{}",
            )

    def test_verify_with_wrong_key(self):
        private_key1, _ = generate_rsa_keypair()
        _, public_key2 = generate_rsa_keypair()
        body = b'{"test": true}'

        headers = sign_request(
            private_key=private_key1,
            key_id="key1",
            method="POST",
            url="https://example.com/inbox",
            body=body,
        )

        with pytest.raises(SignatureVerificationError):
            verify_request(
                public_key=public_key2,
                method="POST",
                path="/inbox",
                headers=headers,
                body=body,
            )

    def test_sign_preserves_existing_headers(self, rsa_keypair):
        private_key, _ = rsa_keypair

        headers = sign_request(
            private_key=private_key,
            key_id="key",
            method="POST",
            url="https://example.com/inbox",
            body=b"{}",
            headers={
                "Content-Type": "application/activity+json",
                "Date": "Wed, 01 Jan 2025 12:00:00 GMT",
            },
        )

        assert headers["Content-Type"] == "application/activity+json"
        assert headers["Date"] == "Wed, 01 Jan 2025 12:00:00 GMT"
