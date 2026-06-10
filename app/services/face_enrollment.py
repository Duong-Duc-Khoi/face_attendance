"""Helpers for dashboard-started face enrollment sessions."""

import base64
import hashlib
import secrets

import cv2
import numpy as np


FACE_SESSION_ACTIVE_STATUSES = ("pending", "in_progress")
FACE_SESSION_TTL_MINUTES = 10


def generate_face_ticket() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, hash_face_ticket(raw)


def hash_face_ticket(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def decode_base64_frames(frames: list[str]) -> list:
    cv_images = []
    for item in frames:
        try:
            encoded = str(item or "").split(",", 1)[-1]
            img_bytes = base64.b64decode(encoded, validate=False)
            arr = np.frombuffer(img_bytes, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                cv_images.append(img)
        except Exception:
            continue
    return cv_images


def face_action_label(action: str) -> str:
    return "Đăng ký lại khuôn mặt" if action == "update_face" else "Đăng ký khuôn mặt"
