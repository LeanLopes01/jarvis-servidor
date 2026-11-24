"""Copies of a managed project (DiceVTT) that the coding agents may read or edit.

The real project folder is never edited by an agent and no version-control state is
created: no Plastic branch, workspace or Git worktree. Instead:

- one reference copy per project, shared by every conversation and read by the
  planner; it is synchronised with the real project before each plan, so only the
  files changed since the last sync are copied again;
- a work copy per approved run, where the agents edit, made from the reference;
- a manifest of file hashes taken with each copy, so the changes can be listed and
  shown as a diff, and later applied to the real project only where the real file
  is still exactly as it was when the copy was made (anything else is a conflict).

Each copy keeps an index (size, modification time and hash of every file). A file
whose size and time still match the index keeps its hash without being read again:
this turns copying and hashing 8,000 files into a quick check. Writing a file
changes its time, so an agent's edit is always seen.

Only the parts of a Unity project an agent needs are copied: Assets, Packages,
ProjectSettings (with every .meta file) and small files at the root. Library, Logs,
Temp, Builds, UserSettings, caches and the .plastic folder are left out.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .credentials import app_dir, local_dir

COPIED_DIRS = ("Assets", "Packages", "ProjectSettings")
ROOT_SUFFIXES = (".md", ".txt", ".json", ".conf", ".asmdef", ".slnx", ".sln")
SKIPPED_DIRS = {
    "Library",
    "Logs",
    "Temp",
    "obj",
    "Builds",
    "UserSettings",
    ".plastic",
    ".git",
    "ProfilerCaptures",
}
DIFF_FILE_LIMIT = 400_000
DIFF_TOTAL_LIMIT = 1_500_000
# Work copies of runs are kept a week (they can still be applied), at most this many.
RUN_KEEP_SECONDS = 7 * 24 * 3600
RUN_KEEP_COUNT = 8


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True)
class Project:
    key: str
    name: str
    path: Path
    vcs: str


def projects() -> dict[str, Project]:
    """Managed projects; %APPDATA%/JarvisDesktop/projects.json overrides the default."""
    config = app_dir() / "projects.json"
    raw: dict[str, Any] = {
        "dicevtt": {
            "name": "DiceVTT",
            "path": r"C:\ProjetoTablepRPG\DiceVTT",
            "vcs": "plastic",
        }
    }
    if config.exists():
        raw.update(json.loads(config.read_text(encoding="utf-8")))
    return {
        key: Project(
            key,
            str(value.get("name", key)),
            Path(value["path"]).resolve(),
            str(value.get("vcs", "none")),
        )
        for key, value in raw.items()
    }


def work_root() -> Path:
    return Path(os.environ.get("JARVIS_WORK_ROOT") or local_dir() / "work")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    found: list[Path] = []
    for name in COPIED_DIRS:
        top = root / name
        if not top.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRS]
            found += [Path(dirpath) / f for f in filenames]
    found += [
        p for p in root.iterdir() if p.is_file() and p.suffix.lower() in ROOT_SUFFIXES
    ]
    return found


def _stat(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns


def _index_path(target: Path) -> Path:
    return target.parent / f"{target.name}.index.json"


def _load_index(target: Path) -> dict[str, list[Any]]:
    """relative path -> [source size, source mtime, copy mtime, sha256]."""
    try:
        return json.loads(_index_path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def manifest(root: Path) -> dict[str, str]:
    """relative path (forward slashes) -> sha256, for the copied parts.

    A file that still matches the copy's index keeps its recorded hash; only the
    others (edited or added since the copy) are read.
    """
    index = _load_index(root)
    files: dict[str, str] = {}
    for path in _files(root):
        relative = path.relative_to(root).as_posix()
        entry = index.get(relative)
        if entry and _stat(path) == (entry[0], entry[2]):
            files[relative] = entry[3]
        else:
            files[relative] = _hash(path)
    return files


def manifest_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]


def _inside(base: Path, relative: str) -> Path:
    target = (base / relative).resolve()
    if base.resolve() not in target.parents:
        raise WorkspaceError(f"{relative} is outside the project")
    return target


def _writable(function: Any, path: str, _exc: Any) -> None:
    """rmtree error handler: Plastic can leave files read-only."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, onerror=_writable)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


def sync_copy(
    source: Path,
    target: Path,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Make `target` an exact copy of the copied parts of `source`.

    Only files that are new, changed in the source, or touched in the copy are
    copied; files gone from the source are deleted. When the source is itself a
    copy with an index (a run made from the reference), its hashes are reused.
    """
    if not source.is_dir():
        raise WorkspaceError(f"{source} does not exist")
    index = _load_index(target) if target.is_dir() else {}
    target.mkdir(parents=True, exist_ok=True)
    known = _load_index(source)
    sources = {p.relative_to(source).as_posix(): p for p in _files(source)}
    present = {p.relative_to(target).as_posix(): p for p in _files(target)}
    gone = present.keys() - sources.keys()
    for relative in gone:
        _unlink(present[relative])
    fresh: dict[str, list[Any]] = {}
    pending: list[tuple[str, Path, int, int]] = []
    for relative, path in sources.items():
        size, mtime = _stat(path)
        entry = index.get(relative)
        copy = present.get(relative)
        if (
            entry
            and copy is not None
            and (entry[0], entry[1]) == (size, mtime)
            and _stat(copy) == (size, entry[2])
        ):
            fresh[relative] = entry
        else:
            pending.append((relative, path, size, mtime))
    if on_progress:
        on_progress(0, len(pending))
    for number, (relative, path, size, mtime) in enumerate(pending, 1):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            _unlink(destination)
        shutil.copy2(path, destination)
        reused = known.get(relative)
        digest = (
            reused[3]
            if reused and (reused[0], reused[2]) == (size, mtime)
            else _hash(destination)
        )
        fresh[relative] = [size, mtime, _stat(destination)[1], digest]
        if on_progress:
            on_progress(number, len(pending))
    files = {relative: entry[3] for relative, entry in fresh.items()}
    _index_path(target).write_text(json.dumps(fresh), encoding="utf-8")
    (target.parent / f"{target.name}.manifest.json").write_text(
        json.dumps(files), encoding="utf-8"
    )
    return {
        "files": len(files),
        "copied": len(pending),
        "deleted": len(gone),
        "manifest_digest": manifest_digest(files),
    }


def copy_project(
    project: Project,
    target: Path,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Synchronise a copy with the real project; returns where it came from."""
    if not project.path.is_dir():
        raise WorkspaceError(f"{project.path} does not exist")
    info = {
        "source": str(project.path),
        **sync_copy(project.path, target, on_progress=on_progress),
        "revision": revision(project),
    }
    (target.parent / f"{target.name}.info.json").write_text(
        json.dumps(info, indent=1), encoding="utf-8"
    )
    return info


def copy_from_reference(
    reference: Path,
    work: Path,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """A run's work copy, made from the just synchronised reference copy (its
    hashes are those of the real project at that moment)."""
    try:
        origin = json.loads(
            (reference.parent / f"{reference.name}.info.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        origin = {}
    info = {**origin, **sync_copy(reference, work, on_progress=on_progress)}
    (work.parent / f"{work.name}.info.json").write_text(
        json.dumps(info, indent=1), encoding="utf-8"
    )
    return info


def seed_reference(reference: Path, old: Path, project: Project) -> bool:
    """Turn an old per-conversation copy into the shared reference, so the first sync
    after the update copies only what changed. Its files were made with copy2 (same
    times as the project) and checked unchanged after every plan, so a file whose
    size and time still match the project keeps its old hash; the rest is copied."""
    try:
        hashes = json.loads(
            (old.parent / f"{old.name}.manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    reference.parent.mkdir(parents=True, exist_ok=True)
    old.rename(reference)
    index: dict[str, list[Any]] = {}
    for path in _files(reference):
        relative = path.relative_to(reference).as_posix()
        real = project.path / relative
        if relative in hashes and real.is_file():
            size, mtime = _stat(real)
            if _stat(path) == (size, mtime):
                index[relative] = [size, mtime, mtime, hashes[relative]]
    _index_path(reference).write_text(json.dumps(index), encoding="utf-8")
    return True


def clean_copies(root: Path, in_use: set[Path], now: float | None = None) -> list[str]:
    """Delete copies nobody needs: the per-conversation copies made before the shared
    reference, and run copies older than a week or beyond the most recent ones."""
    now = time.time() if now is None else now
    busy = {p.resolve() for p in in_use}
    removed: list[str] = []

    def used(folder: Path) -> bool:
        folder = folder.resolve()
        return any(folder == p or folder in p.parents for p in busy)

    conversations = root / "conversations"
    if conversations.is_dir():
        for folder in conversations.iterdir():
            if folder.is_dir() and not used(folder):
                remove_tree(folder)
                removed.append(f"conversations/{folder.name}")
    runs = root / "runs"
    if runs.is_dir():
        folders = sorted(
            (f for f in runs.iterdir() if f.is_dir()),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for position, folder in enumerate(folders):
            old = now - folder.stat().st_mtime > RUN_KEEP_SECONDS
            if (old or position >= RUN_KEEP_COUNT) and not used(folder):
                remove_tree(folder)
                removed.append(f"runs/{folder.name}")
    return removed


def load_manifest(target: Path) -> dict[str, str]:
    return json.loads(
        (target.parent / f"{target.name}.manifest.json").read_text(encoding="utf-8")
    )


def revision(project: Project) -> dict[str, Any]:
    """The version-control position of the real project, read-only."""
    if project.vcs != "plastic":
        return {"vcs": project.vcs}
    cm = shutil.which("cm") or r"C:\Program Files\PlasticSCM5\client\cm.exe"
    info: dict[str, Any] = {"vcs": "plastic"}
    try:
        info["workspace"] = subprocess.run(
            [cm, "wi", "."],
            cwd=project.path,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()[:300]
        pending = subprocess.run(
            [cm, "status", "--short", "."],
            cwd=project.path,
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout.splitlines()
        info["pending_changes"] = len(pending)
    except (OSError, subprocess.SubprocessError) as exc:
        info["error"] = str(exc)[:200]
    return info


def changes(work: Path, base: dict[str, str]) -> dict[str, Any]:
    """What the agents changed in a work copy, with a readable diff for text files."""
    now = manifest(work)
    added = sorted(set(now) - set(base))
    deleted = sorted(set(base) - set(now))
    modified = sorted(p for p in set(now) & set(base) if now[p] != base[p])
    diff_parts: list[str] = []
    size = 0
    reference = work.parent / f"{work.name}.reference"
    for path in modified + added:
        file = work / path
        if file.stat().st_size > DIFF_FILE_LIMIT:
            diff_parts.append(f"--- {path}: too large for a text diff\n")
            continue
        try:
            new = file.read_text(encoding="utf-8").splitlines(keepends=True)
            old_file = reference / path
            old = (
                old_file.read_text(encoding="utf-8").splitlines(keepends=True)
                if old_file.exists()
                else []
            )
        except UnicodeDecodeError:
            diff_parts.append(f"--- {path}: binary file changed\n")
            continue
        chunk = "".join(difflib.unified_diff(old, new, f"a/{path}", f"b/{path}"))
        size += len(chunk)
        if size > DIFF_TOTAL_LIMIT:
            diff_parts.append("[diff truncated at the size limit]\n")
            break
        diff_parts.append(chunk)
    return {
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "diff": "".join(diff_parts),
    }


def keep_reference(work: Path) -> None:
    """A pristine copy of the text files beside the work copy, for diffs."""
    reference = work.parent / f"{work.name}.reference"
    remove_tree(reference)
    for path in load_manifest(work):
        source = work / path
        if source.stat().st_size <= DIFF_FILE_LIMIT and source.suffix.lower() not in {
            ".png",
            ".jpg",
            ".fbx",
            ".wav",
            ".mp3",
            ".asset",
            ".unity3d",
        }:
            target = reference / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def apply_to_project(
    project: Project, work: Path, base: dict[str, str]
) -> dict[str, Any]:
    """Copy the agents' changes into the real project where it is safe.

    A file is applied only when the real file still has the hash it had when the
    work copy was made (or, for a new file, still does not exist). Every other file
    is reported as a conflict and left alone. Deletions are reported, not applied.
    """
    result = changes(work, base)
    applied: list[str] = []
    conflicts: list[str] = []
    for path in result["modified"] + result["added"]:
        real = _inside(project.path, path)
        expected = base.get(path)
        current = _hash(real) if real.exists() else None
        if current != expected:
            conflicts.append(path)
            continue
        real.parent.mkdir(parents=True, exist_ok=True)
        temporary = real.with_name(real.name + ".jarvis-tmp")
        shutil.copy2(work / path, temporary)
        os.replace(temporary, real)
        applied.append(path)
    return {
        "applied": applied,
        "conflicts": conflicts,
        "not_applied_deletions": result["deleted"],
    }
