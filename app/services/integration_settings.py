"""
Admin-managed AI provider settings.

API keys are encrypted with a JWT_SECRET-derived stream so they are not stored
as plain text. In production, prefer a dedicated secret manager/KMS.
"""

import base64
import hashlib
import hmac
import secrets
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.integration import AIProviderSetting

PROVIDERS = {
    "openai": {"label": "OpenAI", "default_model": settings.OPENAI_MODEL, "env_key": settings.OPENAI_API_KEY},
    "gemini": {"label": "Google Gemini", "default_model": settings.GEMINI_MODEL, "env_key": settings.GEMINI_API_KEY},
}


def _secret_key() -> bytes:
    return hashlib.sha256(settings.JWT_SECRET.encode("utf-8")).digest()


def _keystream(nonce: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    key = _secret_key()
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:length]


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    nonce = secrets.token_bytes(16)
    raw = value.encode("utf-8")
    masked = bytes(a ^ b for a, b in zip(raw, _keystream(nonce, len(raw))))
    return base64.urlsafe_b64encode(nonce + masked).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        data = base64.urlsafe_b64decode(value.encode("ascii"))
        nonce, masked = data[:16], data[16:]
        raw = bytes(a ^ b for a, b in zip(masked, _keystream(nonce, len(masked))))
        return raw.decode("utf-8")
    except Exception:
        return ""


def _get_row(provider: str, db: Session) -> Optional[AIProviderSetting]:
    return db.query(AIProviderSetting).filter_by(provider=provider).first()


def list_ai_provider_settings(db: Session) -> list[dict]:
    rows = {r.provider: r for r in db.query(AIProviderSetting).all()}
    result = []
    for provider, meta in PROVIDERS.items():
        row = rows.get(provider)
        env_configured = bool(meta["env_key"])
        db_configured = bool(row and row.api_key_encrypted)
        result.append({
            "provider": provider,
            "label": meta["label"],
            "model": (row.model if row and row.model else meta["default_model"]),
            "is_enabled": bool(row.is_enabled) if row else env_configured,
            "configured": db_configured or env_configured,
            "source": "database" if db_configured else ("env" if env_configured else "none"),
            "updated_by": row.updated_by if row else "",
            "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        })
    return result


def update_ai_provider_setting(
    provider: str,
    db: Session,
    model: str = "",
    api_key: Optional[str] = None,
    is_enabled: bool = True,
    clear_key: bool = False,
    updated_by: str = "",
) -> dict:
    if provider not in PROVIDERS:
        raise ValueError("Provider không hợp lệ")
    row = _get_row(provider, db)
    if not row:
        row = AIProviderSetting(provider=provider)
        db.add(row)
    row.model = model.strip() or PROVIDERS[provider]["default_model"]
    row.is_enabled = is_enabled
    row.updated_by = updated_by
    row.updated_at = datetime.now()
    if clear_key:
        row.api_key_encrypted = ""
    elif api_key:
        row.api_key_encrypted = encrypt_secret(api_key.strip())
    db.commit()
    return next(p for p in list_ai_provider_settings(db) if p["provider"] == provider)


def get_ai_provider_runtime_configs(db: Session) -> list[dict]:
    rows = {r.provider: r for r in db.query(AIProviderSetting).all()}
    configs = []
    for provider in ("openai", "gemini"):
        meta = PROVIDERS[provider]
        row = rows.get(provider)
        enabled = bool(row.is_enabled) if row else bool(meta["env_key"])
        key = decrypt_secret(row.api_key_encrypted) if row and row.api_key_encrypted else meta["env_key"]
        model = row.model if row and row.model else meta["default_model"]
        if enabled and key:
            configs.append({"provider": provider, "api_key": key, "model": model})
    return configs
