from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.tools import BrowserTools, duckduckgo_search_url
from src.web_search import (
    SearchError,
    WebSearch,
    google_search_url,
    grounded_result,
    overview_text,
)


def test_duckduckgo_search_url_encodes_query() -> None:
    assert (
        duckduckgo_search_url("  current weather in New York  ")
        == "https://duckduckgo.com/?q=current+weather+in+New+York"
    )


def test_duckduckgo_search_url_rejects_empty_query() -> None:
    with pytest.raises(ValueError, match="query cannot be empty"):
        duckduckgo_search_url("  ")


def response(*, sources=True, queries=True):
    metadata = SimpleNamespace(
        grounding_chunks=[
            SimpleNamespace(
                web=SimpleNamespace(uri="https://example.com/science", title="Science")
            )
        ]
        if sources
        else [],
        web_search_queries=["why the sky is blue"] if queries else [],
    )
    return SimpleNamespace(
        text="Light is scattered.",
        candidates=[SimpleNamespace(grounding_metadata=metadata)],
    )


def test_grounding_never_masquerades_as_google_overview():
    result = grounded_result(response())
    assert result["provider"] == "gemini_google_search"
    assert result["ai_overview"]["available"] is False
    assert result["sources"][0]["url"] == "https://example.com/science"


@pytest.mark.parametrize(("sources", "queries"), [(False, True), (True, False)])
def test_ungrounded_model_answer_is_rejected(sources, queries):
    with pytest.raises(SearchError, match="verifiable"):
        grounded_result(response(sources=sources, queries=queries))


def test_overview_paragraphs_and_lists():
    assert (
        overview_text(
            [
                {"snippet": "Summary."},
                {"list": [{"snippet": "First."}, {"snippet": "Second."}]},
            ]
        )
        == "Summary.\nFirst.\nSecond."
    )
    assert overview_text([]) == ""


async def test_search_does_not_touch_any_browser(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    browser = AsyncMock()
    tools = BrowserTools(browser)
    tools.search._grounded = AsyncMock(return_value=grounded_result(response()))
    result = await tools.search_the_web(None, "why is the sky blue")
    assert result["opened_on_device"] is False
    assert result["search_url"].startswith("https://www.google.com/search?")
    browser.open_url.assert_not_called()


async def test_missing_overview_falls_back_honestly(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")
    search = WebSearch()
    search._overview = AsyncMock(return_value=None)
    search._grounded = AsyncMock(return_value=grounded_result(response()))
    result = await search.search("sky")
    assert result["ai_overview"] == {
        "available": False,
        "reason": "not_present_for_query",
    }


async def test_actual_overview_takes_priority(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")
    search = WebSearch()
    search._overview = AsyncMock(
        return_value={
            "provider": "google_ai_overview",
            "answer": "Actual overview",
            "ai_overview": {"available": True},
        }
    )
    search._grounded = AsyncMock()
    result = await search.search("sky")
    assert result["ai_overview"]["available"] is True
    search._grounded.assert_not_called()


def test_google_query_is_encoded():
    assert "q=a%26b" in google_search_url(" a&b ")
    with pytest.raises(SearchError):
        google_search_url(" ")


async def test_google_failure_uses_labelled_backup(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    search = WebSearch()
    search._grounded = AsyncMock(side_effect=SearchError("quota exceeded"))
    search._fallback = AsyncMock(
        return_value={
            "provider": "bing_web_results",
            "ai_overview": {"available": False},
        }
    )
    result = await search.search("sky")
    assert result["provider"] == "bing_web_results"
    assert result["ai_overview"]["available"] is False
    search._fallback.assert_awaited_once_with("sky")
