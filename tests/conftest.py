"""
Shared test fixtures.
"""

import pytest

from pubby.crypto._keys import generate_rsa_keypair, export_public_key_pem


@pytest.fixture
def rsa_keypair():
    """Generate an RSA key pair for tests."""
    private_key, public_key = generate_rsa_keypair(key_size=2048)
    return private_key, public_key


@pytest.fixture
def private_key(rsa_keypair):
    return rsa_keypair[0]


@pytest.fixture
def public_key(rsa_keypair):
    return rsa_keypair[1]


@pytest.fixture
def public_key_pem(public_key):
    return export_public_key_pem(public_key)


@pytest.fixture
def actor_config():
    """Standard test actor configuration."""
    return {
        "base_url": "https://blog.example.com",
        "username": "blog",
        "name": "Test Blog",
        "summary": "A test blog",
        "icon_url": "https://blog.example.com/icon.png",
    }
