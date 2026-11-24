"""Opens a web page or a Google search where the user can see it.

On the PC it opens a new tab in Firefox; the phone does the same through the app.
Only http(s) addresses are opened, and anything that is not an address becomes a
Google search.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote_plus, urlparse

GOOGLE_SEARCH = "https://www.google.com/search?q="
_DOMAIN = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/?#]\S*)?$", re.IGNORECASE)


class BrowserOpenError(Exception):
    pass


def target_url(what: str) -> str:
    """A full URL stays, a bare domain gets https://, anything else is searched."""
    text = what.strip()
    if not text:
        raise BrowserOpenError("Tell me what to open or search for.")
    if re.match(r"^https?://", text, re.IGNORECASE):
        parsed = urlparse(text)
        if not parsed.netloc or parsed.username or parsed.password:
            raise BrowserOpenError("That address cannot be opened.")
        return text
    if " " not in text and _DOMAIN.match(text):
        return f"https://{text}"
    return GOOGLE_SEARCH + quote_plus(text)


def firefox_executable() -> str | None:
    candidates = [
        # Microsoft Store Firefox exposes this execution alias.
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WindowsApps/firefox.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Mozilla Firefox/firefox.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Mozilla Firefox/firefox.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("firefox")


def open_on_pc(what: str) -> dict[str, str]:
    url = target_url(what)
    firefox = firefox_executable()
    if firefox:
        subprocess.Popen([firefox, "-new-tab", url])
        browser = "Firefox"
    elif hasattr(os, "startfile"):
        os.startfile(url)  # the default browser
        browser = "the default browser"
    else:
        raise BrowserOpenError("No browser is available on this computer.")
    return {"url": url, "browser": browser}
