"""Opening pages and Google searches on the PC or the phone."""

import pytest

from src.jarvis_desktop.browser_open import BrowserOpenError, target_url


@pytest.mark.parametrize(
    ("what", "url"),
    [
        ("https://example.com/a?b=1", "https://example.com/a?b=1"),
        ("youtube.com", "https://youtube.com"),
        ("g1.globo.com/economia", "https://g1.globo.com/economia"),
        (
            "previsão do tempo Lorena",
            "https://www.google.com/search?q=previs%C3%A3o+do+tempo+Lorena",
        ),
        ("python 3.13 release", "https://www.google.com/search?q=python+3.13+release"),
    ],
)
def test_addresses_open_and_words_are_searched(what: str, url: str) -> None:
    assert target_url(what) == url


@pytest.mark.parametrize("bad", ["", "   ", "https://user:pw@example.com", "https://"])
def test_bad_targets_are_refused(bad: str) -> None:
    with pytest.raises(BrowserOpenError):
        target_url(bad)


def test_non_web_schemes_become_searches() -> None:
    assert target_url("file:///etc/passwd").startswith(
        "https://www.google.com/search?q="
    )
    assert target_url("javascript:alert(1)").startswith(
        "https://www.google.com/search?q="
    )
