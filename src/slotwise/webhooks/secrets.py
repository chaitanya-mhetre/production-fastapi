"""Webhook signing secrets must be recoverable (we sign with them), so they are encrypted at
rest with Fernet (AES-128-CBC + HMAC) rather than hashed. Rotate keys with MultiFernet."""

import secrets

from cryptography.fernet import Fernet

from slotwise.config import Settings


def new_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(32)}"


def encrypt(settings: Settings, secret: str) -> str:
    return Fernet(settings.fernet_key.encode()).encrypt(secret.encode()).decode()


def decrypt(settings: Settings, token: str) -> str:
    return Fernet(settings.fernet_key.encode()).decrypt(token.encode()).decode()
