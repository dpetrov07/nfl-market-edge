"""Shared Kalshi authentication and REST client helpers."""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


API_BASE = "https://external-api.kalshi.com"
API_ROOT = "/trade-api/v2"
REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"
WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_local_env(path: Path = Path(".env")) -> None:
    """Load only collector settings, including an unquoted multiline PEM."""
    if not path.exists():
        return
    text = path.read_text()
    keys = (
        "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PEM",
        "KALSHI_PRIVATE_KEY_B64", "KALSHI_PRIVATE_KEY_PATH", "KALSHI_GAME",
        "KALSHI_GAMES", "KALSHI_GAME_DATE", "KALSHI_OUTPUT_DIR",
        "KALSHI_CHANNELS", "KALSHI_HEARTBEAT_SECONDS", "KALSHI_TOP_SIZE_CHANGE",
    )
    for key in keys:
        if key in os.environ:
            continue
        match = re.search(rf"(?m)^{re.escape(key)}=(.*)$", text)
        if not match:
            continue
        value = match.group(1).strip()
        if "PRIVATE_KEY" in key and ("-----BEGIN" in value or not value):
            start = text.find("-----BEGIN", match.start(1))
            if start >= 0:
                end = re.search(r"-----END (?:RSA )?PRIVATE KEY-----", text[start:])
                if end:
                    value = text[start : start + end.end()].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value.replace("\\n", "\n")


def load_private_key(args: argparse.Namespace):
    value = os.getenv("KALSHI_PRIVATE_KEY_PEM") or os.getenv("KALSHI_PRIVATE_KEY")
    if value and "BEGIN" in value:
        pem_bytes = value.replace("\\n", "\n").encode()
    else:
        path = args.private_key_path or os.getenv("KALSHI_PRIVATE_KEY_PATH") or value
        if path:
            pem_bytes = Path(path).expanduser().read_bytes()
        else:
            encoded = os.getenv("KALSHI_PRIVATE_KEY_B64")
            if not encoded:
                raise SystemExit(
                    "set KALSHI_PRIVATE_KEY, KALSHI_PRIVATE_KEY_PATH, or "
                    "KALSHI_PRIVATE_KEY_B64"
                )
            try:
                pem_bytes = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SystemExit("KALSHI_PRIVATE_KEY_B64 is not valid base64") from exc
    return serialization.load_pem_private_key(pem_bytes, password=None)


def auth_headers(key_id: str, private_key) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}GET{WS_PATH}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


class KalshiClient:
    def __init__(self, authenticated: bool):
        self.key_id = None
        self.private_key = None
        self.denied_paths = set()
        self.sessions = threading.local()
        if authenticated:
            load_local_env()
            self.key_id = os.getenv("KALSHI_API_KEY_ID")
            if self.key_id:
                self.private_key = load_private_key(
                    argparse.Namespace(private_key_path=None)
                )

    def get(self, path: str, params: dict | None = None, auth: bool = False):
        if path in self.denied_paths:
            return {}
        url = API_BASE + path
        if not hasattr(self.sessions, "session"):
            self.sessions.session = requests.Session()
        for attempt in range(6):
            try:
                headers = {"User-Agent": "nfl-market-edge/combo-discovery"}
                if auth:
                    if not self.key_id or not self.private_key:
                        return {}
                    timestamp = str(int(time.time() * 1000))
                    signature = self.private_key.sign(
                        f"{timestamp}GET{path}".encode(),
                        padding.PSS(
                            mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.DIGEST_LENGTH,
                        ),
                        hashes.SHA256(),
                    )
                    headers.update(
                        {
                            "KALSHI-ACCESS-KEY": self.key_id,
                            "KALSHI-ACCESS-TIMESTAMP": timestamp,
                            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(
                                signature
                            ).decode(),
                        }
                    )
                response = self.sessions.session.get(
                    url, params=params, headers=headers, timeout=120
                )
                response.raise_for_status()
                return response.json()
            except requests.HTTPError as exc:
                status = exc.response.status_code
                if auth and status in (401, 403):
                    self.denied_paths.add(path)
                    return {}
                if status not in (429, 500, 502, 503, 504) or attempt == 5:
                    raise
                time.sleep(float(exc.response.headers.get("Retry-After") or 2**attempt))
            except requests.RequestException:
                if attempt == 5:
                    raise
                time.sleep(2**attempt)
        return {}

    def paginate(
        self,
        path: str,
        params: dict,
        field: str,
        auth: bool = False,
        row_filter=None,
        progress_label: str | None = None,
    ):
        rows, cursor, pages, scanned = [], None, 0, 0
        seen_cursors = set()
        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = self.get(path, page_params, auth=auth)
            page_rows = payload.get(field, [])
            scanned += len(page_rows)
            rows.extend(
                row for row in page_rows if row_filter is None or row_filter(row)
            )
            pages += 1
            if progress_label and pages % 25 == 0:
                print(
                    f"{progress_label}: {scanned} markets scanned ({pages} pages), "
                    f"{len(rows)} Sunday traded 2/3-leg combos retained",
                    file=sys.stderr,
                    flush=True,
                )
            cursor = payload.get("cursor")
            if not cursor:
                return rows, pages, scanned
            if cursor in seen_cursors:
                raise RuntimeError(f"repeated pagination cursor from {path}")
            seen_cursors.add(cursor)
