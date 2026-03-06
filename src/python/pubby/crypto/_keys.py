from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate_rsa_keypair(
    key_size: int = 2048,
) -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    """
    Generate a new RSA key pair.

    :param key_size: RSA key size in bits (default 2048).
    :return: A tuple of (private_key, public_key).
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
    )
    public_key = private_key.public_key()
    return private_key, public_key


def load_private_key(
    pem_data: str | bytes,
    password: bytes | None = None,
) -> rsa.RSAPrivateKey:
    """
    Load an RSA private key from PEM-encoded data.

    :param pem_data: PEM-encoded private key string or bytes.
    :param password: Optional password for encrypted keys.
    :return: The loaded RSA private key.
    """
    if isinstance(pem_data, str):
        pem_data = pem_data.encode("utf-8")
    key = serialization.load_pem_private_key(pem_data, password=password)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError(f"Expected RSA private key, got {type(key).__name__}")
    return key


def load_public_key(pem_data: str | bytes) -> rsa.RSAPublicKey:
    """
    Load an RSA public key from PEM-encoded data.

    :param pem_data: PEM-encoded public key string or bytes.
    :return: The loaded RSA public key.
    """
    if isinstance(pem_data, str):
        pem_data = pem_data.encode("utf-8")
    key = serialization.load_pem_public_key(pem_data)
    if not isinstance(key, rsa.RSAPublicKey):
        raise TypeError(f"Expected RSA public key, got {type(key).__name__}")
    return key


def export_public_key_pem(public_key: rsa.RSAPublicKey) -> str:
    """
    Export an RSA public key as a PEM-encoded string.

    :param public_key: The RSA public key to export.
    :return: PEM-encoded public key string.
    """
    pem_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return pem_bytes.decode("utf-8")


def export_private_key_pem(
    private_key: rsa.RSAPrivateKey,
    password: bytes | None = None,
) -> str:
    """
    Export an RSA private key as a PEM-encoded string.

    :param private_key: The RSA private key to export.
    :param password: Optional password to encrypt the key.
    :return: PEM-encoded private key string.
    """
    encryption = (
        serialization.BestAvailableEncryption(password)
        if password
        else serialization.NoEncryption()
    )
    pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    )
    return pem_bytes.decode("utf-8")
