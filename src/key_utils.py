from __future__ import annotations

from typing import Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def generate_ed25519_keypair() -> Tuple[str, str, str]:
    """
    Generate an Ed25519 keypair compatible with Binance Self-Generated API Keys.
    Returns: (public_pem, private_pem, single_line_env_private_key)
    """
    priv_key = Ed25519PrivateKey.generate()
    pub_key = priv_key.public_key()

    public_pem = (
        pub_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
        .strip()
    )
    private_pem = (
        priv_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode("ascii")
        .strip()
    )
    single_line_env = private_pem.replace("\r\n", "\n").replace("\n", "\\n")
    return public_pem, private_pem, single_line_env
