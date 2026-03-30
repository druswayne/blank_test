import hmac
import time
from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class QrPayload:
    version: str
    blank_uuid: str
    ts: int
    sig: str


def make_qr_payload(*, version: str, blank_uuid: str, secret: str) -> str:
    # Минимальный payload для повышения читаемости на маленьком A6 QR:
    # b:<uuid>
    # (без подписи/ts в строке QR; проверка идет существованием blank_uuid в БД)
    return f"b:{blank_uuid}"


def _make_legacy_payload(*, version: str, blank_uuid: str, secret: str) -> str:
    ts = int(time.time())
    base = f"{version}:{blank_uuid}:{ts}"
    sig = hmac.new(secret.encode("utf-8"), base.encode("utf-8"), sha256).hexdigest()
    return f"{base}:{sig}"


def verify_qr_payload(*, payload: str, secret: str, expected_version: str, ttl_seconds: int = 60 * 60 * 24 * 365) -> str:
    # Новый компактный формат: b:<blank_uuid>
    if payload.startswith("b:"):
        blank_uuid = payload[2:].strip()
        if not blank_uuid:
            raise ValueError("Invalid QR payload")
        return blank_uuid

    # Ожидаемый формат: v1:<blank_uuid>:<ts>:<sig>
    parts = payload.split(":")
    if len(parts) != 4:
        raise ValueError("Invalid QR payload format")

    version, blank_uuid, ts_s, sig = parts
    if version != expected_version:
        raise ValueError("Unsupported QR payload version")

    ts = int(ts_s)
    now = int(time.time())
    if now - ts > ttl_seconds:
        raise ValueError("QR payload expired")

    base = f"{version}:{blank_uuid}:{ts}"
    expected_sig = hmac.new(secret.encode("utf-8"), base.encode("utf-8"), sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        raise ValueError("Invalid QR signature")

    return blank_uuid

