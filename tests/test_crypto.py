"""
Tests for RSA key generation, loading, and export.
"""

from cryptography.hazmat.primitives.asymmetric import rsa

from mypub.crypto._keys import (
    export_private_key_pem,
    export_public_key_pem,
    generate_rsa_keypair,
    load_private_key,
    load_public_key,
)


class TestKeyGeneration:
    def test_generate_keypair(self):
        private_key, public_key = generate_rsa_keypair()
        assert isinstance(private_key, rsa.RSAPrivateKey)
        assert isinstance(public_key, rsa.RSAPublicKey)

    def test_generate_keypair_key_size(self):
        private_key, _ = generate_rsa_keypair(key_size=4096)
        assert private_key.key_size == 4096


class TestKeyExportAndLoad:
    def test_export_and_load_public_key(self, rsa_keypair):
        _, public_key = rsa_keypair
        pem = export_public_key_pem(public_key)
        assert pem.startswith("-----BEGIN PUBLIC KEY-----")
        assert pem.strip().endswith("-----END PUBLIC KEY-----")

        loaded = load_public_key(pem)
        assert isinstance(loaded, rsa.RSAPublicKey)
        # Verify the loaded key matches
        assert (
            export_public_key_pem(loaded) == pem
        )

    def test_export_and_load_private_key(self, rsa_keypair):
        private_key, _ = rsa_keypair
        pem = export_private_key_pem(private_key)
        assert pem.startswith("-----BEGIN PRIVATE KEY-----")

        loaded = load_private_key(pem)
        assert isinstance(loaded, rsa.RSAPrivateKey)

    def test_export_and_load_private_key_with_password(self, rsa_keypair):
        private_key, _ = rsa_keypair
        password = b"test-password-123"
        pem = export_private_key_pem(private_key, password=password)
        assert pem.startswith("-----BEGIN ENCRYPTED PRIVATE KEY-----")

        loaded = load_private_key(pem, password=password)
        assert isinstance(loaded, rsa.RSAPrivateKey)

    def test_load_public_key_from_bytes(self, rsa_keypair):
        _, public_key = rsa_keypair
        pem = export_public_key_pem(public_key)
        loaded = load_public_key(pem.encode("utf-8"))
        assert isinstance(loaded, rsa.RSAPublicKey)

    def test_load_private_key_from_bytes(self, rsa_keypair):
        private_key, _ = rsa_keypair
        pem = export_private_key_pem(private_key)
        loaded = load_private_key(pem.encode("utf-8"))
        assert isinstance(loaded, rsa.RSAPrivateKey)


class TestSignVerifyRoundTrip:
    """Test that keys can sign data and verify it."""

    def test_sign_and_verify(self, rsa_keypair):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        private_key, public_key = rsa_keypair
        message = b"test message for signing"

        signature = private_key.sign(
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

        # Should not raise
        public_key.verify(
            signature,
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    def test_tampered_signature_rejected(self, rsa_keypair):
        import pytest
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.exceptions import InvalidSignature

        private_key, public_key = rsa_keypair
        message = b"test message"

        signature = private_key.sign(
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

        # Tamper with the signature
        tampered = bytearray(signature)
        tampered[0] ^= 0xFF
        tampered = bytes(tampered)

        with pytest.raises(InvalidSignature):
            public_key.verify(
                tampered,
                message,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )

    def test_wrong_message_rejected(self, rsa_keypair):
        import pytest
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.exceptions import InvalidSignature

        private_key, public_key = rsa_keypair
        signature = private_key.sign(
            b"original message",
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

        with pytest.raises(InvalidSignature):
            public_key.verify(
                signature,
                b"different message",
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
