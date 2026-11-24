"""What the DiceVTT RAG knows about a request, for the planner's prompt.

Before a planner (Antigravity or Codex) looks at the reference copy, the helper asks
the local RAG (127.0.0.1:8767, retrieval only: no generation, so it takes about a
second) which scripts and documents match the user's latest message. The planner
gets their paths, summaries and short excerpts, and still reads the files in the
copy itself before deciding: the RAG only tells it where to start.

Only plan prompts are enriched (they end with the user's message to decide on);
reports are written from the run's results. Any failure means no extra context.
"""

from __future__ import annotations

import re
from typing import Any

from .rag_service import RagStack

PLAN_MARKER = "Decide on the latest user message."
# "- user (user): text" lines of the conversation, as the server writes them.
USER_LINE = re.compile(
    r"^- user \([^)]*\): (.*?)(?=^- \w+ \([^)]*\): |^Latest plan, version |\Z)",
    re.MULTILINE | re.DOTALL,
)
QUERY_LIMIT = 1_500
SHORT_MESSAGE = 80  # "sim, pode fazer": the message before says what it is about
SUMMARY_LIMIT = 400
EXCERPT_LIMIT = 700
CONTEXT_LIMIT = 6_000


def query_from(prompt: str) -> str:
    """The user's latest request, read from a plan prompt; "" for other prompts."""
    if PLAN_MARKER not in prompt:
        return ""
    conversation = prompt.split(PLAN_MARKER, 1)[0]
    messages = [m.strip() for m in USER_LINE.findall(conversation) if m.strip()]
    if not messages:
        return ""
    query = messages[-1]
    if len(query) < SHORT_MESSAGE and len(messages) > 1:
        query = f"{messages[-2]}\n{query}"
    return query[:QUERY_LIMIT]


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def render(result: dict[str, Any]) -> str:
    """The retrieval result as a prompt section, within CONTEXT_LIMIT."""
    code, docs = [], []
    pages: set[str] = set()  # several chunks of one page: the best one is enough
    for source in result.get("sources") or []:
        provenance = source.get("provenance") or {}
        if source.get("kind") == "code_pointer":
            line = f"- {source.get('relpath')}"
            if provenance.get("main_class"):
                line += f" (class {provenance['main_class']})"
            if provenance.get("summary"):
                line += f": {_clip(provenance['summary'], SUMMARY_LIMIT)}"
            code.append(line)
        else:
            title = provenance.get("doc_title") or source.get("relpath")
            where = provenance.get("source_url") or source.get("relpath")
            if where in pages:
                continue
            pages.add(where)
            section = f" — {source['section']}" if source.get("section") else ""
            docs.append(
                f"- {title}{section} ({where}):\n  "
                + _clip(source.get("text"), EXCERPT_LIMIT)
            )
    if not code and not docs:
        return ""
    parts = [
        "Hints from the project's local search index (RAG), for the latest user "
        "message. They show where to start; they may be incomplete or out of date, "
        "so read the files in the copy before relying on them."
    ]
    if code:
        parts += ["Scripts that look relevant (paths in the copy):", *code]
    if docs:
        parts += ["Documentation excerpts (Unity and libraries):", *docs]
    text = "\n".join(parts)
    return text if len(text) <= CONTEXT_LIMIT else text[:CONTEXT_LIMIT] + "\n[…]"


def rag_context(rag: RagStack):
    """The JobManager's `context` hook, asking this helper's RAG stack."""

    async def context(prompt: str) -> str:
        query = query_from(prompt)
        if not query or not rag.available:
            return ""
        status, result = await rag.request(
            "rag",
            "POST",
            "/v1/rag/retrieve",
            {"query": query, "text_limit": EXCERPT_LIMIT * 2},
            timeout=25.0,
        )
        if status != 200:
            raise RuntimeError(
                f"RAG answered {status}: {result.get('message') or result}"[:200]
            )
        return render(result)

    return context
