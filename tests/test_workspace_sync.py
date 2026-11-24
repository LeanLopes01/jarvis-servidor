"""Incremental copies of the managed project, their clean-up and the planner's RAG
hints (workspaces.py, agent_jobs.py, planning_context.py)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src.jarvis_desktop import agent_jobs, planning_context, workspaces
from src.jarvis_desktop.agent_jobs import JobManager


def _project(tmp_path: Path) -> workspaces.Project:
    root = tmp_path / "DiceVTT"
    (root / "Assets" / "Scripts").mkdir(parents=True)
    (root / "Assets" / "Scripts" / "A.cs").write_text("class A {}\n")
    (root / "Assets" / "Scripts" / "B.cs").write_text("class B {}\n")
    (root / "Assets" / "Art.png").write_bytes(b"\x89PNG" + b"x" * 100)
    (root / "Library").mkdir()
    (root / "Library" / "cache.bin").write_bytes(b"x")
    return workspaces.Project("dicevtt", "DiceVTT", root, "none")


def _touch(path: Path, text: str) -> None:
    """Rewrite a file with a clearly different modification time."""
    path.write_text(text)
    later = time.time() + 5
    os.utime(path, (later, later))


def test_a_second_sync_copies_only_what_changed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    copy = tmp_path / "work" / "reference"
    first = workspaces.copy_project(project, copy)
    assert first["copied"] == 3 and first["files"] == 3
    assert not (copy / "Library").exists()

    again = workspaces.copy_project(project, copy)
    assert again["copied"] == 0 and again["deleted"] == 0

    _touch(project.path / "Assets" / "Scripts" / "A.cs", "class A { int x; }\n")
    (project.path / "Assets" / "Scripts" / "C.cs").write_text("class C {}\n")
    (project.path / "Assets" / "Scripts" / "B.cs").unlink()
    third = workspaces.copy_project(project, copy)
    assert (third["copied"], third["deleted"]) == (2, 1)
    assert (copy / "Assets" / "Scripts" / "A.cs").read_text() == "class A { int x; }\n"
    assert not (copy / "Assets" / "Scripts" / "B.cs").exists()
    assert workspaces.load_manifest(copy) == workspaces.manifest(project.path)


def test_an_edit_in_the_copy_is_seen_and_undone_by_the_next_sync(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    copy = tmp_path / "reference"
    workspaces.copy_project(project, copy)
    expected = workspaces.load_manifest(copy)
    _touch(copy / "Assets" / "Scripts" / "A.cs", "hacked\n")
    (copy / "Assets" / "Extra.cs").write_text("new\n")
    now = workspaces.manifest(copy)
    assert now["Assets/Scripts/A.cs"] != expected["Assets/Scripts/A.cs"]
    assert "Assets/Extra.cs" in now

    healed = workspaces.copy_project(project, copy)
    assert (healed["copied"], healed["deleted"]) == (1, 1)
    assert workspaces.manifest(copy) == expected


def test_manifest_reads_only_files_that_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    copy = tmp_path / "reference"
    workspaces.copy_project(project, copy)
    read: list[Path] = []
    real_hash = workspaces._hash
    monkeypatch.setattr(workspaces, "_hash", lambda p: read.append(p) or real_hash(p))
    workspaces.manifest(copy)
    assert read == []
    _touch(copy / "Assets" / "Scripts" / "B.cs", "changed\n")
    workspaces.manifest(copy)
    assert [p.name for p in read] == ["B.cs"]


def test_a_run_copy_reuses_the_reference_hashes_and_still_finds_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    reference = tmp_path / "reference"
    workspaces.copy_project(project, reference)
    read: list[Path] = []
    real_hash = workspaces._hash
    monkeypatch.setattr(workspaces, "_hash", lambda p: read.append(p) or real_hash(p))
    work = tmp_path / "runs" / "r1" / "work"
    info = workspaces.copy_from_reference(reference, work)
    assert info["copied"] == 3 and info["source"] == str(project.path)
    assert read == []
    workspaces.keep_reference(work)

    _touch(work / "Assets" / "Scripts" / "A.cs", "agent\n")
    (work / "Assets" / "Scripts" / "N.cs").write_text("new\n")
    _touch(project.path / "Assets" / "Scripts" / "A.cs", "user edit\n")
    result = workspaces.apply_to_project(project, work, workspaces.load_manifest(work))
    assert result["conflicts"] == ["Assets/Scripts/A.cs"]
    assert result["applied"] == ["Assets/Scripts/N.cs"]
    assert (project.path / "Assets" / "Scripts" / "A.cs").read_text() == "user edit\n"


def test_read_only_files_can_be_replaced_and_removed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    script = project.path / "Assets" / "Scripts" / "A.cs"
    copy = tmp_path / "reference"
    workspaces.copy_project(project, copy)
    os.chmod(copy / "Assets" / "Scripts" / "A.cs", 0o444)
    _touch(script, "class A { }\n")
    workspaces.copy_project(project, copy)
    os.chmod(copy / "Assets" / "Scripts" / "A.cs", 0o444)
    workspaces.remove_tree(copy)
    assert not copy.exists()


def test_clean_copies_keeps_what_is_in_use_and_recent(tmp_path: Path) -> None:
    root = tmp_path / "dicevtt"
    for name in ("c1", "c2"):
        (root / "conversations" / name / "reference").mkdir(parents=True)
    now = time.time()
    # Newest first: r0, r2 ... r11 (hours old), then r1 (eight days old).
    for number in range(12):
        run = root / "runs" / f"r{number}"
        (run / "work").mkdir(parents=True)
        age = now - number * 3600 - (8 * 24 * 3600 if number == 1 else 0)
        os.utime(run, (age, age))
    in_use = {
        root / "conversations" / "c2" / "reference",
        root / "runs" / "r10" / "work",
    }
    removed = workspaces.clean_copies(root, in_use, now)
    # r1 is too old; r9 and r11 are beyond the eight newest; r10 is in use.
    assert sorted(removed) == ["conversations/c1", "runs/r1", "runs/r11", "runs/r9"]
    assert (root / "conversations" / "c2").exists() and (root / "runs" / "r10").exists()


def test_an_old_conversation_copy_seeds_the_shared_reference(tmp_path: Path) -> None:
    project = _project(tmp_path)
    old = tmp_path / "conversations" / "c1" / "reference"
    workspaces.copy_project(project, old)
    (old.parent / "reference.index.json").unlink()  # copies made before the index
    _touch(project.path / "Assets" / "Scripts" / "A.cs", "moved on\n")
    reference = tmp_path / "reference"
    assert workspaces.seed_reference(reference, old, project)
    assert not old.exists()
    synced = workspaces.copy_project(project, reference)
    assert synced["copied"] == 1  # only A.cs, changed since the old copy
    assert workspaces.load_manifest(reference) == workspaces.manifest(project.path)


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _project(tmp_path)
    monkeypatch.setattr(agent_jobs, "projects", lambda: {"dicevtt": project})
    monkeypatch.setenv("JARVIS_WORK_ROOT", str(tmp_path / "work"))
    return JobManager(tmp_path / "jobs"), project, tmp_path / "work" / "dicevtt"


def _job(kind: str, job_id: str, **extra) -> dict:
    return {
        "id": job_id,
        "kind": kind,
        "project": "dicevtt",
        "state": "preparing",
        "conversation_id": "conv-" + job_id,
        **extra,
    }


def test_conversations_share_one_reference_copy(manager) -> None:
    jobs, _, root = manager
    first = _job("coordinate", "j1")
    assert jobs._workspace(first) == root / "reference"
    assert first["copy"]["copied"] == 3
    jobs._close(first)
    second = _job("coordinate", "j2")
    assert jobs._workspace(second) == root / "reference"
    assert second["copy"]["copied"] == 0
    assert not (root / "conversations").exists()


def test_the_reference_is_not_synchronised_while_a_planner_reads_it(manager) -> None:
    jobs, project, root = manager
    reading = _job("coordinate", "j1")
    jobs._workspace(reading)
    _touch(project.path / "Assets" / "Scripts" / "A.cs", "changed meanwhile\n")
    other = _job("coordinate", "j2")
    jobs._workspace(other)
    assert other["copy"]["synced"] is False
    assert (root / "reference" / "Assets" / "Scripts" / "A.cs").read_text() == (
        "class A {}\n"
    )
    jobs._close(reading)
    jobs._close(other)
    later = _job("coordinate", "j3")
    jobs._workspace(later)
    assert later["copy"]["copied"] == 1


def test_a_run_gets_its_work_copy_from_the_synchronised_reference(manager) -> None:
    jobs, project, root = manager
    _touch(project.path / "Assets" / "Scripts" / "B.cs", "class B { }\n")
    task = _job("execute", "t1", run_id="r1", agent="claude")
    work = jobs._workspace(task)
    assert work == root / "runs" / "r1" / "work"
    assert (work / "Assets" / "Scripts" / "B.cs").read_text() == "class B { }\n"
    assert (root / "runs" / "r1" / "work.reference" / "Assets").is_dir()
    # A later task of the same run keeps the agents' edits.
    _touch(work / "Assets" / "Scripts" / "A.cs", "edited by task 1\n")
    assert jobs._workspace(_job("execute", "t2", run_id="r1", agent="codex")) == work
    assert (work / "Assets" / "Scripts" / "A.cs").read_text() == "edited by task 1\n"


PLAN_PROMPT = """You coordinate software work on the DiceVTT project.

Conversation so far (latest last):
- user (user): Quero trocar a skin dos dados 3D
pelo menu de opções.
- assistant (antigravity): Qual tela?
- user (user): a de opções
Latest plan, version 1 (draft):
{"title": "x"}

Decide on the latest user message. If it is a question...
"""


def test_the_rag_query_is_the_latest_request() -> None:
    assert planning_context.query_from(PLAN_PROMPT) == (
        "Quero trocar a skin dos dados 3D\npelo menu de opções.\na de opções"
    )
    long = PLAN_PROMPT.replace("a de opções", "x" * 100)
    assert planning_context.query_from(long) == "x" * 100
    report = PLAN_PROMPT.replace("Decide on the latest user message.", "Report.")
    assert planning_context.query_from(report) == ""


def test_rag_sources_become_short_hints() -> None:
    text = planning_context.render(
        {
            "sources": [
                {
                    "kind": "code_pointer",
                    "relpath": "Assets/Scripts/Dados3D.cs",
                    "text": "class Dados3D {" + "x" * 50_000,
                    "provenance": {"main_class": "Dados3D", "summary": "Rola dados"},
                },
                {
                    "kind": "documentation",
                    "relpath": "unity/materials.md",
                    "section": "Textures",
                    "text": "Assign a texture " * 200,
                    "provenance": {"doc_title": "Materials", "source_url": "https://u"},
                },
                {
                    "kind": "documentation",
                    "relpath": "unity/materials.md",
                    "text": "Another chunk of the same page",
                    "provenance": {"doc_title": "Materials", "source_url": "https://u"},
                },
            ]
        }
    )
    assert "Assets/Scripts/Dados3D.cs (class Dados3D): Rola dados" in text
    assert "Materials — Textures (https://u)" in text
    assert "class Dados3D {" not in text  # the file itself stays in the copy
    assert "Another chunk" not in text
    assert len(text) <= planning_context.CONTEXT_LIMIT
    assert planning_context.render({"sources": []}) == ""


async def test_the_planner_prompt_gets_the_hints_or_goes_without(tmp_path) -> None:
    async def hints(prompt: str) -> str:
        return "Hints: Assets/Scripts/Dados3D.cs"

    async def broken(prompt: str) -> str:
        raise ConnectionError("RAG offline")

    for context, used in ((hints, True), (broken, False)):
        jobs = JobManager(tmp_path / f"jobs-{used}", context=context)
        job = {"id": "j1", "kind": "coordinate", "agent": "antigravity", "prompt": "p"}
        await jobs._add_context(job)
        assert job["rag_context"]["used"] is used
        assert job["prompt"] == (
            "p\n\nHints: Assets/Scripts/Dados3D.cs" if used else "p"
        )
    saved = json.loads((tmp_path / "jobs-True" / "j1.json").read_text(encoding="utf-8"))
    assert saved["prompt"].endswith("Dados3D.cs")
