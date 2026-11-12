"""
Tests for RFC 9421 HTTP Message Signatures (rsa-v1_5-sha256) and Ed25519
per-actor keys.
"""

import json
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from pubby import ActivityPubHandler
from pubby._exceptions import SignatureVerificationError
from pubby.crypto import (
    Ed25519Identity,
    SignatureScheme,
    generate_ed25519_private_key,
    load_any_public_key,
    signing_identity,
)
from pubby.crypto._keys import export_public_key_pem, generate_rsa_keypair
from pubby.crypto._signatures import (
    _rfc9421_signature_base,
    build_content_digest,
    parse_signatures_rfc9421,
    sign_request,
    sign_request_rfc9421,
    verify_request_rfc9421,
)
from pubby.handlers._inbox import InboxProcessor, _public_key_pem_for
from pubby.handlers._outbox import deliver_activity
from pubby.storage.adapters.file import FileActivityPubStorage

URL = "https://remote.example/ap/users/bob/inbox?x=1"
PATH = "/ap/users/bob/inbox?x=1"
KEY_ID = "https://remote.example/ap/actor#main-key"
BODY = b'{"type":"Follow"}'


def _received(private_key, body=BODY, key_id=KEY_ID):
    """Headers as a receiver sees them: signed fields plus the Host header."""
    headers = sign_request_rfc9421(private_key, key_id, "POST", URL, body)
    headers["Host"] = "remote.example"
    return headers


def _verify(public_key, headers, body=BODY, path=PATH, method="POST", **kw):
    sig = parse_signatures_rfc9421({k.lower(): v for k, v in headers.items()})[0]
    return verify_request_rfc9421(public_key, sig, method, path, headers, body, **kw)


class TestSignatureScheme:
    def test_values(self):
        assert SignatureScheme.DRAFT_CAVAGE.value == "draft-cavage"
        assert SignatureScheme.RFC9421.value == "rfc9421"
        assert SignatureScheme("rfc9421") is SignatureScheme.RFC9421


class TestSign:
    def test_headers_and_content_digest(self, rsa_keypair):
        private_key, _ = rsa_keypair
        headers = sign_request_rfc9421(private_key, KEY_ID, "POST", URL, BODY)
        assert headers["Content-Digest"] == build_content_digest(BODY)
        assert headers["Signature-Input"].startswith("sig1=(")
        assert headers["Signature"].startswith("sig1=:") and headers[
            "Signature"
        ].endswith(":")
        assert "Signature-Input" in headers and "Date" not in headers

    def test_signature_base_matches_rfc_serialization(self):

        base = _rfc9421_signature_base(
            ["@method", "@authority", "@path", "content-digest"],
            [("created", 1618884473), ("keyid", "test-key-rsa-pss")],
            "post",
            "example.com",
            "/foo?param=value&pet=dog",
            {"content-digest": "sha-256=:X:"},
        )
        assert base == (
            '"@method": POST\n'
            '"@authority": example.com\n'
            '"@path": /foo\n'
            '"content-digest": sha-256=:X:\n'
            '"@signature-params": ("@method" "@authority" "@path" "content-digest")'
            ';created=1618884473;keyid="test-key-rsa-pss"'
        )

    def test_default_port_omitted_from_authority(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        headers = sign_request_rfc9421(
            private_key, KEY_ID, "GET", "https://remote.example:443/ap/actor"
        )
        headers["Host"] = "remote.example"
        sig = parse_signatures_rfc9421({k.lower(): v for k, v in headers.items()})[0]
        assert verify_request_rfc9421(public_key, sig, "GET", "/ap/actor", headers)

    def test_non_default_port_kept_in_authority(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        headers = sign_request_rfc9421(
            private_key, KEY_ID, "GET", "https://remote.example:8443/ap/actor"
        )
        headers["Host"] = "remote.example:8443"
        sig = parse_signatures_rfc9421({k.lower(): v for k, v in headers.items()})[0]
        assert verify_request_rfc9421(public_key, sig, "GET", "/ap/actor", headers)

    def test_custom_covered_components(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        headers = sign_request_rfc9421(
            private_key, KEY_ID, "GET", URL, None, signed_headers=["@method", "@path"]
        )
        headers["Host"] = "remote.example"
        sig = parse_signatures_rfc9421({k.lower(): v for k, v in headers.items()})[0]
        assert sig.components == ["@method", "@path"]
        # @authority is not covered, so the required-component check rejects it
        with pytest.raises(SignatureVerificationError, match="@authority"):
            verify_request_rfc9421(public_key, sig, "GET", PATH, headers)


class TestVerify:
    def test_round_trip(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        assert _verify(public_key, _received(private_key)) is True

    def test_round_trip_without_body(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        headers = sign_request_rfc9421(private_key, KEY_ID, "GET", URL)
        headers["Host"] = "remote.example"
        assert "Content-Digest" not in headers
        assert _verify(public_key, headers, body=None, method="GET")

    def test_tampered_body_rejected(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        with pytest.raises(SignatureVerificationError, match="Content-Digest"):
            _verify(public_key, _received(private_key), body=b"{}")

    def test_altered_target_rejected(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        with pytest.raises(SignatureVerificationError):
            _verify(public_key, _received(private_key), path="/ap/users/eve/inbox?x=1")

    def test_wrong_key_rejected(self, rsa_keypair):

        private_key, _ = rsa_keypair
        _, other_public = generate_rsa_keypair(1024)
        with pytest.raises(SignatureVerificationError, match="verification failed"):
            _verify(other_public, _received(private_key))

    def test_stale_created_rejected(self, rsa_keypair):

        private_key, public_key = rsa_keypair
        stale = int(time.time()) - 3600
        headers = sign_request_rfc9421(
            private_key, KEY_ID, "POST", URL, BODY, created=stale
        )
        headers["Host"] = "remote.example"
        with pytest.raises(SignatureVerificationError, match="created"):
            _verify(public_key, headers)

    def test_expired_signature_rejected(self, rsa_keypair):

        private_key, public_key = rsa_keypair
        headers = sign_request_rfc9421(private_key, KEY_ID, "POST", URL, BODY)
        headers["Host"] = "remote.example"
        sig = parse_signatures_rfc9421({k.lower(): v for k, v in headers.items()})[0]
        sig.params.append(("expires", int(time.time()) - 1))
        with pytest.raises(SignatureVerificationError, match="expired"):
            verify_request_rfc9421(public_key, sig, "POST", PATH, headers, BODY)

    def test_body_without_content_digest_coverage_rejected(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        headers = sign_request_rfc9421(
            private_key,
            KEY_ID,
            "POST",
            URL,
            None,
            signed_headers=["@method", "@authority", "@path"],
        )
        headers["Host"] = "remote.example"
        with pytest.raises(SignatureVerificationError, match="content-digest"):
            _verify(public_key, headers, body=BODY)

    def test_unsupported_algorithm_rejected(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        headers = _received(private_key)
        sig = parse_signatures_rfc9421({k.lower(): v for k, v in headers.items()})[0]
        sig.params = [(k, "rsa-pss-sha512" if k == "alg" else v) for k, v in sig.params]
        with pytest.raises(SignatureVerificationError, match="algorithm"):
            verify_request_rfc9421(public_key, sig, "POST", PATH, headers, BODY)


class TestParse:
    def test_rfc_style_header(self):
        headers = {
            "signature-input": 'sig-b22=("@authority" "@path" "content-digest")'
            ';created=1618884473;keyid="test-key-rsa-pss";alg="rsa-v1_5-sha256"',
            "signature": "sig-b22=:AAAA:",
        }
        (sig,) = parse_signatures_rfc9421(headers)
        assert sig.label == "sig-b22"
        assert sig.components == ["@authority", "@path", "content-digest"]
        assert sig.key_id == "test-key-rsa-pss"
        assert sig._int_param("created") == 1618884473

    def test_no_signature_input_means_not_rfc9421(self):
        assert parse_signatures_rfc9421({"signature": 'keyId="x"'}) == []

    def test_signature_input_without_signature_rejected(self):
        with pytest.raises(SignatureVerificationError):
            parse_signatures_rfc9421({"signature-input": 'sig1=("@method")'})

    def test_malformed_signature_input_rejected(self):
        with pytest.raises(SignatureVerificationError):
            parse_signatures_rfc9421(
                {"signature-input": "sig1=@method", "signature": "sig1=:AAAA:"}
            )


class TestInboxDispatch:
    ACTOR = "https://remote.example/ap/actor"

    @staticmethod
    def _local_key():

        return generate_rsa_keypair(1024)[0]

    def _processor(self, scheme=SignatureScheme.DRAFT_CAVAGE):
        return InboxProcessor(
            storage=MagicMock(),
            actor_id="https://yonder.example/ap/actor",
            private_key=self._local_key(),
            key_id="https://yonder.example/ap/actor#main-key",
            signature_scheme=scheme,
        )

    @pytest.mark.parametrize(
        "scheme", [SignatureScheme.DRAFT_CAVAGE, SignatureScheme.RFC9421]
    )
    def test_both_schemes_verify_on_the_inbox(self, rsa_keypair, scheme):
        private_key, public_key = rsa_keypair
        doc = {
            "id": self.ACTOR,
            "publicKey": {
                "id": KEY_ID,
                "publicKeyPem": export_public_key_pem(public_key),
            },
        }
        headers = {}
        if scheme is SignatureScheme.RFC9421:
            headers = _received(private_key, key_id=KEY_ID)
        else:
            headers = sign_request(
                private_key=private_key,
                key_id=KEY_ID,
                method="POST",
                url=URL,
                body=BODY,
            )
            headers["Host"] = "remote.example"
        processor = self._processor()
        with patch.object(InboxProcessor, "_fetch_actor", return_value=doc):
            actor = processor.verify_signature(
                "POST", PATH, headers, BODY, expected_actor=self.ACTOR
            )
            assert actor == self.ACTOR
            with pytest.raises(SignatureVerificationError):
                processor.verify_signature(
                    "POST", PATH, headers, b"{}", expected_actor=self.ACTOR
                )

    def test_rfc9421_request_without_key_id_rejected(self, rsa_keypair):
        private_key, _ = rsa_keypair
        headers = _received(private_key, key_id=KEY_ID)
        headers["Signature-Input"] = headers["Signature-Input"].replace(
            f';keyid="{KEY_ID}"', ""
        )
        processor = self._processor()
        with pytest.raises(SignatureVerificationError, match="keyid"):
            processor.verify_signature("POST", PATH, headers, BODY)


class TestDeliverActivity:
    ACTIVITY = {"id": "https://yonder.example/a/1", "type": "Create"}  # noqa: RUF012

    def _post(self, **kwargs):
        with patch("pubby.handlers._outbox.requests.post") as post:
            post.return_value.status_code = 202
            deliver_activity(
                self.ACTIVITY,
                "https://remote.example/inbox",
                key_id=KEY_ID,
                private_key=kwargs.pop("private_key"),
                **kwargs,
            )
        return post.call_args.kwargs["headers"]

    def test_default_scheme_is_draft_cavage(self, rsa_keypair):
        private_key, _ = rsa_keypair
        headers = self._post(private_key=private_key)
        assert "keyId=" in headers["Signature"]
        assert "Signature-Input" not in headers

    def test_explicit_draft_cavage_is_unchanged(self, rsa_keypair):
        private_key, _ = rsa_keypair
        headers = self._post(
            private_key=private_key, signature_scheme=SignatureScheme.DRAFT_CAVAGE
        )
        assert (
            'headers="(request-target) host date digest content-type content-length"'
            in (headers["Signature"])
        )

    def test_rfc9421_scheme(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        headers = self._post(private_key=private_key, signature_scheme="rfc9421")
        assert "Signature-Input" in headers
        assert "keyId=" not in headers["Signature"]
        assert headers["Content-Digest"] == build_content_digest(
            json.dumps(self.ACTIVITY).encode("utf-8")
        )
        headers["Host"] = "remote.example"
        sig = parse_signatures_rfc9421({k.lower(): v for k, v in headers.items()})[0]
        assert verify_request_rfc9421(
            public_key,
            sig,
            "POST",
            "/inbox",
            headers,
            json.dumps(self.ACTIVITY).encode("utf-8"),
        )


RFC_ED25519_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAJrQLj5P/89iXES9+vFgrIy29clF9CC/oPPsw3c5D0bs=
-----END PUBLIC KEY-----"""


class TestEd25519:
    def _ed25519_key(self):

        return ed25519.Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)

    def test_rfc9421_appendix_b26_example(self):
        """RFC 9421 Appendix B.2.6 — ed25519 signature over the test request."""

        key = load_any_public_key(RFC_ED25519_PUBLIC_KEY)
        headers = {
            "signature-input": 'sig-b26=("date" "@method" "@path" "@authority" '
            '"content-type" "content-length");created=1618884473;'
            'keyid="test-key-ed25519"',
            "signature": "sig-b26=:wqcAqbmYJ2ji2glfAMaRy4gruYYnx2nEFN2HN6jrnDnQ"
            "CK1u02Gb04v9EDgwUPiu4A0w6vuQv5lIp5WPpBKRCw==:",
            "date": "Tue, 20 Apr 2021 02:07:55 GMT",
            "host": "example.com",
            "content-type": "application/json",
            "content-length": "18",
        }
        (sig,) = parse_signatures_rfc9421(headers)
        assert sig.key_id == "test-key-ed25519"
        assert verify_request_rfc9421(
            key, sig, "POST", "/foo?param=value&pet=dog", headers, now=1618884473
        )

    def test_round_trip(self, rsa_keypair):

        private = self._ed25519_key()
        public = private.public_key()
        headers = sign_request_rfc9421(private, KEY_ID, "POST", URL, BODY)
        assert 'alg="ed25519"' in headers["Signature-Input"]
        headers["Host"] = "remote.example"
        assert isinstance(public, ed25519.Ed25519PublicKey)
        assert _verify(public, headers)

    def test_tampered_body_rejected(self):
        private = self._ed25519_key()
        headers = sign_request_rfc9421(private, KEY_ID, "POST", URL, BODY)
        headers["Host"] = "remote.example"
        with pytest.raises(SignatureVerificationError, match="Content-Digest"):
            _verify(private.public_key(), headers, body=b"{}")

    def test_ed25519_signature_rejected_under_rsa_key(self, rsa_keypair):
        _, rsa_public = rsa_keypair
        private = self._ed25519_key()
        headers = sign_request_rfc9421(private, KEY_ID, "POST", URL, BODY)
        headers["Host"] = "remote.example"
        with pytest.raises(SignatureVerificationError, match="does not match"):
            _verify(rsa_public, headers)

    def test_inbox_accepts_ed25519_actor(self):

        private = self._ed25519_key()
        public_pem = (
            private.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        doc = {
            "id": "https://remote.example/ap/actor",
            "publicKey": {"id": KEY_ID, "publicKeyPem": public_pem},
        }
        headers = sign_request_rfc9421(private, KEY_ID, "POST", URL, BODY)
        headers["Host"] = "remote.example"
        processor = InboxProcessor(
            storage=MagicMock(),
            actor_id="https://yonder.example/ap/actor",
            private_key=TestInboxDispatch._local_key(),
            key_id="https://yonder.example/ap/actor#main-key",
        )
        with patch.object(InboxProcessor, "_fetch_actor", return_value=doc):
            assert (
                processor.verify_signature(
                    "POST", PATH, headers, BODY, expected_actor=doc["id"]
                )
                == doc["id"]
            )


class TestEd25519ActorKeys:
    """Per-actor Ed25519 keys: advertisement, selection, and key lookup."""

    BASE = "https://yonder.example"
    ACTOR = f"{BASE}/ap/actor"
    ED_KEY_ID = f"{ACTOR}#ed25519-key"

    @staticmethod
    def _handler(ed25519_private_key=None, signature_scheme="draft-cavage"):

        private_key, _ = generate_rsa_keypair()
        return ActivityPubHandler(
            storage=FileActivityPubStorage(tempfile.mkdtemp(), auto_migrate=True),
            actor_config={"base_url": TestEd25519ActorKeys.BASE, "username": "yonder"},
            private_key=private_key,
            async_delivery=False,
            signature_scheme=signature_scheme,
            ed25519_private_key=ed25519_private_key,
        )

    def test_actor_document_advertises_ed25519_key(self):

        handler = self._handler(ed25519.Ed25519PrivateKey.generate())
        doc = handler.get_actor_document()
        (entry,) = doc["additionalPublicKeys"]
        assert entry["id"] == self.ED_KEY_ID
        assert entry["owner"] == self.ACTOR
        assert isinstance(
            load_any_public_key(entry["publicKeyPem"]), ed25519.Ed25519PublicKey
        )
        assert doc["publicKey"]["id"] == f"{self.ACTOR}#main-key"

    def test_actor_document_without_ed25519_key_is_unchanged(self):
        assert "additionalPublicKeys" not in self._handler().get_actor_document()

    def test_ed25519_key_accepts_pem(self):

        key = generate_ed25519_private_key()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        handler = self._handler(pem)
        assert handler.ed25519.key_id == self.ED_KEY_ID
        assert handler.ed25519.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ) == key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def test_signing_identity_prefers_ed25519_only_for_rfc9421(self, rsa_keypair):

        rsa_private, _ = rsa_keypair
        identity = Ed25519Identity(self.ED_KEY_ID, ed25519.Ed25519PrivateKey.generate())
        assert signing_identity(
            SignatureScheme.RFC9421, rsa_private, "k", identity
        ) == (
            identity.key,
            self.ED_KEY_ID,
        )
        assert signing_identity(
            SignatureScheme.DRAFT_CAVAGE, rsa_private, "k", identity
        ) == (
            rsa_private,
            "k",
        )
        assert signing_identity(SignatureScheme.RFC9421, rsa_private, "k", None) == (
            rsa_private,
            "k",
        )

    def test_delivery_is_signed_with_ed25519_under_rfc9421(self, rsa_keypair):

        private_key, _ = rsa_keypair
        ed_private = ed25519.Ed25519PrivateKey.generate()
        activity = {"id": "https://yonder.example/a/1", "type": "Create"}
        with patch("pubby.handlers._outbox.requests.post") as post:
            post.return_value.status_code = 202
            deliver_activity(
                activity,
                "https://remote.example/inbox",
                key_id=KEY_ID,
                private_key=private_key,
                signature_scheme="rfc9421",
                ed25519=Ed25519Identity(self.ED_KEY_ID, ed_private),
            )
        headers = post.call_args.kwargs["headers"]
        assert f'keyid="{self.ED_KEY_ID}"' in headers["Signature-Input"]
        assert 'alg="ed25519"' in headers["Signature-Input"]
        body = json.dumps(activity).encode("utf-8")
        headers["Host"] = "remote.example"
        sig = parse_signatures_rfc9421({k.lower(): v for k, v in headers.items()})[0]
        assert verify_request_rfc9421(
            ed_private.public_key(), sig, "POST", "/inbox", headers, body
        )

    def test_inbound_key_lookup_finds_additional_public_key(self, rsa_keypair):

        _, rsa_public = rsa_keypair
        ed_pem = self._ed_pem()
        actor = {
            "id": self.ACTOR,
            "publicKey": {
                "id": f"{self.ACTOR}#main-key",
                "publicKeyPem": export_public_key_pem(rsa_public),
            },
            "additionalPublicKeys": [{"id": self.ED_KEY_ID, "publicKeyPem": ed_pem}],
        }
        assert _public_key_pem_for(actor, self.ED_KEY_ID) == ed_pem
        # Unknown key id falls back to the primary publicKey, as before.
        assert (
            _public_key_pem_for(actor, "https://other/#k")
            == actor["publicKey"]["publicKeyPem"]
        )

    def test_inbox_verifies_ed25519_additional_key(self, rsa_keypair):

        _, rsa_public = rsa_keypair
        ed_private = ed25519.Ed25519PrivateKey.generate()
        actor = {
            "id": self.ACTOR,
            "publicKey": {
                "id": f"{self.ACTOR}#main-key",
                "publicKeyPem": export_public_key_pem(rsa_public),
            },
            "additionalPublicKeys": [
                {"id": self.ED_KEY_ID, "publicKeyPem": self._ed_pem(ed_private)}
            ],
        }
        headers = sign_request_rfc9421(ed_private, self.ED_KEY_ID, "POST", URL, BODY)
        headers["Host"] = "remote.example"
        processor = InboxProcessor(
            storage=MagicMock(),
            actor_id="https://yonder.example/ap/actor",
            private_key=TestInboxDispatch._local_key(),
            key_id="https://yonder.example/ap/actor#main-key",
        )
        with patch.object(InboxProcessor, "_fetch_actor", return_value=actor):
            assert (
                processor.verify_signature(
                    "POST", PATH, headers, BODY, expected_actor=self.ACTOR
                )
                == self.ACTOR
            )

    @staticmethod
    def _ed_pem(private_key=None):

        private_key = private_key or ed25519.Ed25519PrivateKey.generate()
        return (
            private_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
