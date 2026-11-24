"""The report runner is an optional member of the helper's RAG stack."""

from __future__ import annotations

from pathlib import Path

from src.jarvis_desktop.rag_service import RagStack


def make_root(tmp_path: Path, diagnostics: bool) -> Path:
    root = tmp_path / "Orquestrador"
    (root / ".venv-rag" / "Scripts").mkdir(parents=True)
    (root / ".venv-rag" / "Scripts" / "python.exe").write_text("")
    (root / ".local").mkdir()
    (root / ".local" / "rag.toml").write_text("")
    (root / ".local" / "ingestion.toml").write_text("")
    if diagnostics:
        (root / ".local" / "diagnostics.toml").write_text("")
    return root


async def started_services(root: Path, monkeypatch) -> list[str]:
    stack = RagStack(root)
    for service in stack.services.values():
        service.port = 9  # nothing listens: never adopted, never "port in use"
    spawned: list[str] = []
    monkeypatch.setattr(stack, "_spawn", lambda service: spawned.append(service.name))
    assert stack.available
    await stack.start()
    return spawned


async def test_stack_starts_without_the_report_runner(tmp_path, monkeypatch) -> None:
    spawned = await started_services(
        make_root(tmp_path, diagnostics=False), monkeypatch
    )
    assert spawned == ["rag", "ingestion"]


async def test_report_runner_starts_with_the_stack_when_configured(
    tmp_path, monkeypatch
) -> None:
    spawned = await started_services(make_root(tmp_path, diagnostics=True), monkeypatch)
    assert spawned == ["rag", "ingestion", "diagnostics"]


def test_report_runner_shuts_down_through_its_own_route(tmp_path) -> None:
    service = RagStack(make_root(tmp_path, diagnostics=True)).services["diagnostics"]
    assert service.health.replace("health", "shutdown") == "/v1/diagnostics/shutdown"
