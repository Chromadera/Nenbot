"""
vault.py — Fernet symmetric encryption for user agent keys.

Master key is loaded from NENBOT_VAULT_KEY env var.
Never stores plaintext keys on disk.

Usage:
    from vault import Vault
    v = Vault()
    token = v.encrypt("0xABCDEF...")
    plain  = v.decrypt(token)
"""
from __future__ import annotations

import os
import base64
from cryptography.fernet import Fernet, InvalidToken


class VaultError(Exception):
    pass


class Vault:
    def __init__(self):
        raw = os.environ.get("NENBOT_VAULT_KEY", "")
        if not raw:
            raise VaultError(
                "NENBOT_VAULT_KEY not set. "
                "Generate one with: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        try:
            self._fernet = Fernet(raw.encode() if isinstance(raw, str) else raw)
        except Exception as e:
            raise VaultError(f"Invalid NENBOT_VAULT_KEY: {e}")

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext string, returns base64 token string."""
        if not plaintext:
            raise VaultError("Cannot encrypt empty string")
        try:
            return self._fernet.encrypt(plaintext.encode()).decode()
        except Exception as e:
            raise VaultError(f"Encryption failed: {e}")

    def decrypt(self, token: str) -> str:
        """Decrypt a token string, returns plaintext."""
        if not token:
            raise VaultError("Cannot decrypt empty token")
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            raise VaultError("Decryption failed: invalid or tampered token")
        except Exception as e:
            raise VaultError(f"Decryption failed: {e}")

    @staticmethod
    def generate_key() -> str:
        """Generate a new Fernet key. Run once, store in .env."""
        return Fernet.generate_key().decode()


if __name__ == "__main__":
    # Quick self-test
    key = Vault.generate_key()
    print(f"Generated key: {key}")
    os.environ["NENBOT_VAULT_KEY"] = key
    v = Vault()
    test = "0xTEST_AGENT_KEY_12345"
    encrypted = v.encrypt(test)
    decrypted = v.decrypt(encrypted)
    assert decrypted == test, "Vault self-test FAILED"
    print(f"Original:  {test}")
    print(f"Encrypted: {encrypted}")
    print(f"Decrypted: {decrypted}")
    print("Vault self-test PASSED")
