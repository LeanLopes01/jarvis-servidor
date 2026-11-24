"""Where this PC keeps its pairing: %APPDATA%\\JarvisDesktop\\config.json, with the
device token encrypted by Windows DPAPI for the current user."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def app_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "JarvisDesktop"


def local_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "JarvisDesktop"


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("Credential protection needs Windows.")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = _Blob()
    ui_forbidden = 0x1
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    description = "jarvis-desktop" if protect else None
    if not function(
        ctypes.byref(source),
        description,
        None,
        None,
        None,
        ui_forbidden,
        ctypes.byref(target),
    ):
        raise OSError("Windows could not protect or read the stored credential.")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(target.data)


@dataclass
class Pairing:
    api_url: str
    device_id: str
    name: str
    token: str

    def save(self) -> None:
        directory = app_dir()
        directory.mkdir(parents=True, exist_ok=True)
        protected = base64.b64encode(_dpapi(self.token.encode(), protect=True)).decode()
        payload = {
            "api_url": self.api_url,
            "device_id": self.device_id,
            "name": self.name,
            "token_dpapi": protected,
        }
        (directory / "config.json").write_text(json.dumps(payload, indent=2))

    @classmethod
    def load(cls) -> Pairing | None:
        path = app_dir() / "config.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text())
        token = _dpapi(base64.b64decode(payload["token_dpapi"]), protect=False).decode()
        return cls(payload["api_url"], payload["device_id"], payload["name"], token)
