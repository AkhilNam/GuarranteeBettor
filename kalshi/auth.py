"""
Kalshi RAPI v2 authentication.

Kalshi uses RSA or Ed25519 private key signing for REST requests.
Each request is signed over: timestamp_ms + method + path
Signature is base64-encoded and sent as a header.

Environment variables required:
    KALSHI_API_KEY_ID         — your API key ID from the Kalshi dashboard
    KALSHI_PRIVATE_KEY_PATH   — path to your PEM-encoded private key file
"""

from __future__ import annotations
import base64
import logging
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, ed25519
# Kalshi uses RSA-PSS with SHA-256 (NOT PKCS1v15)

log = logging.getLogger(__name__)


class KalshiAuth:
    """
    Generates signed request headers for Kalshi RAPI v2.
    Loaded once at startup; signing is CPU-bound but fast (~0.1ms).
    """

    __slots__ = ("_api_key_id", "_private_key", "_key_type")

    def __init__(self, api_key_id: str, private_key_path: str) -> None:
        self._api_key_id = api_key_id
        pem_bytes = Path(private_key_path).read_bytes()
        self._private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        # Detect key type to use correct signing mechanism
        if isinstance(self._private_key, ed25519.Ed25519PrivateKey):
            self._key_type = "ed25519"
        else:
            self._key_type = "rsa"
        log.info("KalshiAuth initialized with key_type=%s key_id=%s", self._key_type, api_key_id)

    def get_headers(self, method: str, path: str) -> dict[str, str]:
        """
        Build signed auth headers for a single request.
        method: uppercase HTTP verb ("GET", "POST", etc.)
        path:   URL path including query string, e.g. "/trade-api/v2/markets?limit=100"
        """
        timestamp_ms = str(int(time.time() * 1000))
        # Strip query string before signing (Kalshi signs path only, not query params)
        sign_path = path.split("?")[0]
        message = (timestamp_ms + method.upper() + sign_path).encode()
        signature_b64 = self._sign(message)
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature_b64,
            "Content-Type": "application/json",
        }

    def _sign(self, message: bytes) -> str:
        if self._key_type == "ed25519":
            sig = self._private_key.sign(message)
        else:
            # Kalshi requires RSA-PSS with SHA-256 (not PKCS1v15)
            sig = self._private_key.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
        return base64.b64encode(sig).decode()
