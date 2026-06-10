"""In-memory registry for kiosk WebSocket connections."""

import asyncio
from dataclasses import dataclass
from datetime import datetime

from fastapi import WebSocket


@dataclass
class KioskConnection:
    kiosk_id: str
    branch_id: int | None
    websocket: WebSocket
    connected_at: datetime
    last_seen_at: datetime


_kiosks: dict[str, KioskConnection] = {}
_lock = asyncio.Lock()


async def register_kiosk(kiosk_id: str, branch_id: int | None, websocket: WebSocket) -> None:
    now = datetime.now()
    async with _lock:
        _kiosks[kiosk_id] = KioskConnection(
            kiosk_id=kiosk_id,
            branch_id=branch_id,
            websocket=websocket,
            connected_at=now,
            last_seen_at=now,
        )


async def touch_kiosk(kiosk_id: str) -> None:
    async with _lock:
        if kiosk_id in _kiosks:
            _kiosks[kiosk_id].last_seen_at = datetime.now()


async def unregister_kiosk(kiosk_id: str, websocket: WebSocket | None = None) -> None:
    async with _lock:
        current = _kiosks.get(kiosk_id)
        if current and (websocket is None or current.websocket is websocket):
            _kiosks.pop(kiosk_id, None)


async def list_online_kiosks(branch_id: int | None = None) -> list[dict]:
    async with _lock:
        rows = []
        for item in _kiosks.values():
            if branch_id is not None and item.branch_id != branch_id:
                continue
            rows.append({
                "kiosk_id": item.kiosk_id,
                "branch_id": item.branch_id,
                "connected_at": item.connected_at.isoformat(),
                "last_seen_at": item.last_seen_at.isoformat(),
            })
        return sorted(rows, key=lambda row: row["connected_at"])


async def send_to_kiosk(kiosk_id: str, payload: dict) -> bool:
    async with _lock:
        item = _kiosks.get(kiosk_id)
    if not item:
        return False
    try:
        await item.websocket.send_json(payload)
        await touch_kiosk(kiosk_id)
        return True
    except Exception:
        await unregister_kiosk(kiosk_id, item.websocket)
        return False
