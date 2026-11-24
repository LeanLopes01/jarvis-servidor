"""One-shot screenshots of a VS Code window, taken only when the agent asks.

Only windows whose title ends with "Visual Studio Code" can be captured; never the
whole screen or other apps. PrintWindow with PW_RENDERFULLCONTENT renders the window
even when other windows cover it. Each capture plays a short system sound on the
PC, and `python -m jarvis_desktop capture off` stops offering the action.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
from typing import Any

from .credentials import app_dir

MAX_WIDTH = 1600
JPEG_QUALITY = 80
VSCODE_SUFFIX = "Visual Studio Code"


class CaptureError(Exception):
    pass


def _settings_path():
    return app_dir() / "settings.json"


def capture_enabled() -> bool:
    try:
        return bool(json.loads(_settings_path().read_text()).get("capture", True))
    except (OSError, ValueError):
        return True


def set_capture_enabled(enabled: bool) -> None:
    path = _settings_path()
    try:
        settings = json.loads(path.read_text())
    except (OSError, ValueError):
        settings = {}
    settings["capture"] = enabled
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2))


def capture_available() -> bool:
    return sys.platform == "win32" and capture_enabled()


def _vscode_windows() -> list[tuple[int, str]]:
    """Visible VS Code windows, topmost first."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd: int, _: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value.endswith(VSCODE_SUFFIX):
                    found.append((hwnd, buffer.value))
        return True

    user32.EnumWindows(callback_type(collect), 0)
    return found


def pick_window(windows: list[tuple[int, str]], hint: str) -> tuple[int, str]:
    if not windows:
        raise CaptureError("VS Code is not open on the computer.")
    hint = hint.strip().casefold()
    if hint:
        matches = [w for w in windows if hint in w[1].casefold()]
        if not matches:
            titles = "; ".join(title for _, title in windows)
            raise CaptureError(
                f"No VS Code window matches {hint!r}. Open windows: {titles}"
            )
        return matches[0]
    return windows[0]


def _grab(hwnd: int):
    import ctypes
    from ctypes import wintypes

    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    # Real pixels instead of DPI-virtualised ones on scaled displays.
    with contextlib.suppress(AttributeError, OSError):
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    if user32.IsIconic(hwnd):
        raise CaptureError("The VS Code window is minimized.")
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise CaptureError("The VS Code window has no visible area.")

    class BitmapInfoHeader(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory_dc, 2):  # PW_RENDERFULLCONTENT
            raise CaptureError("Windows could not capture the VS Code window.")
        header = BitmapInfoHeader(
            ctypes.sizeof(BitmapInfoHeader), width, -height, 1, 32, 0, 0, 0, 0, 0, 0
        )
        pixels = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(memory_dc, bitmap, 0, height, pixels, ctypes.byref(header), 0)
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)
    return Image.frombuffer("RGB", (width, height), pixels, "raw", "BGRX", 0, 1)


def encode(image) -> tuple[str, int, int]:
    """Downscale to MAX_WIDTH and return a base64 JPEG."""
    # A locked or sleeping session renders as one flat colour.
    if all(high - low < 4 for low, high in image.getextrema()):
        raise CaptureError(
            "The capture came back blank; the computer may be locked or asleep."
        )
    if image.width > MAX_WIDTH:
        image = image.resize(
            (MAX_WIDTH, round(image.height * MAX_WIDTH / image.width)), resample=3
        )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode(), image.width, image.height


def capture_vscode(hint: str = "") -> dict[str, Any]:
    if not capture_available():
        raise CaptureError("Screen capture is turned off on the computer.")
    hwnd, title = pick_window(_vscode_windows(), hint)
    image = _grab(hwnd)
    data, width, height = encode(image)
    try:
        import winsound

        winsound.MessageBeep(
            winsound.MB_ICONASTERISK
        )  # tells the user a capture happened
    except (ImportError, RuntimeError):
        pass
    return {"title": title, "width": width, "height": height, "jpeg_base64": data}
