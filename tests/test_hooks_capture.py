"""Exact approval detection through agent hooks, and VS Code window capture."""

import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from livekit.agents.llm import ToolError
from PIL import Image

from src.jarvis_desktop import agent_hook, hooks_setup
from src.jarvis_desktop.capture import CaptureError, encode, pick_window
from src.jarvis_desktop.coding import activity_report, parse_claude, parse_codex
from src.vscode_view import VSCodeViewTools, to_video_frame
from tests.test_coding import NOW, claude_records, codex_records, write


def pending_bash(offset: float) -> list[dict]:
    return claude_records(last="pending", offset=offset)


def test_permission_request_is_exact_approval(tmp_path: Path) -> None:
    path = write(tmp_path / "s.jsonl", pending_bash(offset=-90))
    event = {
        "event": "PermissionRequest",
        "tool": "Bash",
        "detail": "npm test",
        "ts": NOW - 89,
    }
    report = parse_claude(path, NOW, event)
    assert report is not None
    assert (report.state, report.detection) == ("approval", "exact")
    assert "approval to run Bash: npm test" in report.detail


def test_hooked_session_without_request_is_just_running(tmp_path: Path) -> None:
    path = write(tmp_path / "s.jsonl", pending_bash(offset=-90))
    earlier = {"event": "UserPromptSubmit", "ts": NOW - 200}
    report = parse_claude(path, NOW, earlier)
    assert report is not None
    assert report.state == "working" and "running Bash for 90 s" in report.detail
    # Without hooks the same log is only an estimate.
    guess = parse_claude(path, NOW, None)
    assert (
        guess is not None and guess.state == "waiting" and guess.detection == "estimate"
    )


def test_old_permission_request_does_not_apply_to_a_new_call(tmp_path: Path) -> None:
    path = write(tmp_path / "s.jsonl", pending_bash(offset=-30))
    stale = {"event": "PermissionRequest", "tool": "Edit", "ts": NOW - 100}
    report = parse_claude(path, NOW, stale)
    assert report is not None and report.state == "working"


def test_question_states(tmp_path: Path) -> None:
    records = claude_records(last="none")
    records.append(
        {
            "type": "assistant",
            "timestamp": "2027-01-15T08:00:00Z",
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "q1",
                        "name": "AskUserQuestion",
                        "input": {},
                    }
                ],
            },
        }
    )
    report = parse_claude(write(tmp_path / "q.jsonl", records), NOW)
    assert report is not None and report.state == "question"

    finished = write(tmp_path / "f.jsonl", claude_records(last="finished", offset=-60))
    asked = {
        "event": "Notification",
        "notification_type": "elicitation_dialog",
        "ts": NOW - 10,
    }
    report = parse_claude(finished, NOW, asked)
    assert report is not None and report.state == "question"


def test_codex_permission_request(tmp_path: Path) -> None:
    path = write(tmp_path / "c.jsonl", codex_records(finished=False))
    event = {
        "event": "PermissionRequest",
        "tool": "exec_command",
        "detail": "git push",
        "ts": NOW - 95,
    }
    report = parse_codex(path, NOW, event)
    assert report is not None and (report.state, report.detection) == (
        "approval",
        "exact",
    )


def test_hook_writes_latest_event_per_session(tmp_path: Path) -> None:
    payload = {
        "session_id": "abc/../123",
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf build", "description": "Clean build"},
        "transcript_path": "C:\\x\\abc.jsonl",
    }
    target = agent_hook.record("claude", payload, tmp_path)
    assert target is not None and target.name == "claude-abc____123.json"
    saved = json.loads(target.read_text())
    assert (saved["event"], saved["tool"], saved["detail"]) == (
        "PermissionRequest",
        "Bash",
        "Clean build",
    )
    assert agent_hook.record("claude", {"hook_event_name": "Stop"}, tmp_path) is None


def test_report_uses_hook_events(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    session = write(
        claude / "projects" / "c--Projetos-DiceVTT" / "sess-1.jsonl", pending_bash(-90)
    )
    events = tmp_path / "events"
    agent_hook.record(
        "claude",
        {
            "session_id": "sess-1",
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "transcript_path": str(session),
        },
        events,
    )
    for event_file in events.glob("*.json"):  # pretend it was written 89 s ago
        data = json.loads(event_file.read_text())
        data["ts"] = NOW - 89
        event_file.write_text(json.dumps(data))
    report = activity_report(
        now=NOW, claude_root=claude, codex_root=tmp_path, events_root=events
    )
    assert report["sessions"][0]["state"] == "approval"


def test_install_keeps_existing_hooks_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    settings = tmp_path / "claude" / "settings.json"
    settings.parent.mkdir()
    personal = {"hooks": [{"type": "command", "command": "python notes_hook.py"}]}
    settings.write_text(
        json.dumps({"effortLevel": "high", "hooks": {"UserPromptSubmit": [personal]}})
    )

    hooks_setup.install()
    hooks_setup.install()
    config = json.loads(settings.read_text())
    assert config["effortLevel"] == "high"
    assert config["hooks"]["UserPromptSubmit"][0] == personal
    for event in hooks_setup.CLAUDE_EVENTS:
        ours = [g for g in config["hooks"][event] if "agent_hook.py" in json.dumps(g)]
        assert len(ours) == 1 and ours[0]["hooks"][0]["async"] is True
    codex = json.loads((tmp_path / "codex" / "hooks.json").read_text())
    assert set(codex["hooks"]) == set(hooks_setup.CODEX_EVENTS)
    agy_hooks = tmp_path / "antigravity" / "config" / "hooks.json"
    agy = json.loads(agy_hooks.read_text())
    assert set(agy[hooks_setup.ANTIGRAVITY_HOOK]) == set(hooks_setup.ANTIGRAVITY_EVENTS)
    assert list(settings.parent.glob("settings.json.jarvis-backup-*"))

    # Preserves other Antigravity hooks (such as dicevtt-rag-hook) on uninstall.
    agy["dicevtt-rag-hook"] = {"PreInvocation": [{"command": "python rag_hook.py antigravity"}]}
    agy_hooks.write_text(json.dumps(agy))
    hooks_setup.uninstall()
    config = json.loads(settings.read_text())
    assert config["hooks"] == {"UserPromptSubmit": [personal]}
    assert "hooks" not in json.loads((tmp_path / "codex" / "hooks.json").read_text())
    assert set(json.loads(agy_hooks.read_text())) == {"dicevtt-rag-hook"}


def test_antigravity_hook_records_camelcase_payload_and_outbox(tmp_path: Path) -> None:
    from src.jarvis_desktop.outbox import build_event

    transcript = (
        tmp_path
        / "antigravity"
        / "brain"
        / "agy-conv-7"
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    write(
        transcript,
        [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "created_at": "2027-01-15T08:00:00Z",
                "content": "<USER_REQUEST>Corrigir deploy</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": "2027-01-15T08:01:00Z",
                "content": "Deploy corrigido sem docker cp.",
            },
        ],
    )
    events = tmp_path / "events"
    outbox = tmp_path / "outbox"
    pre_payload = {
        "conversationId": "agy-conv-7",
        "workspacePaths": [r"D:\Projetos\Orquestrador"],
        "transcriptPath": str(transcript),
        "invocationNum": 1,
    }
    target = agent_hook.record("antigravity", pre_payload, events, "PreInvocation")
    assert target is not None
    stop_payload = {
        **pre_payload,
        "executionNum": 1,
        "terminationReason": "NO_TOOL_CALL",
    }
    target = agent_hook.record("antigravity", stop_payload, events, "Stop")
    assert target is not None
    saved = json.loads(target.read_text())
    assert saved["turn_started_at"] is not None
    queued = agent_hook.queue_event("antigravity", stop_payload, saved, outbox)
    assert queued is not None
    built = build_event(json.loads(queued.read_text()), NOW)
    assert built["agent"] == "antigravity"
    assert built["project"] == "Orquestrador"
    assert built["title"] == "Antigravity finished · Orquestrador"
    assert "Deploy corrigido" in (built["report"] or "")


def test_pick_window_prefers_hint_then_topmost() -> None:
    windows = [
        (1, "a.py - jarvis - Visual Studio Code"),
        (2, "x.cs - DiceVTT - Visual Studio Code"),
    ]
    assert pick_window(windows, "")[0] == 1
    assert pick_window(windows, "dicevtt")[0] == 2
    with pytest.raises(CaptureError, match="No VS Code window matches"):
        pick_window(windows, "site")
    with pytest.raises(CaptureError, match="not open"):
        pick_window([], "")


def test_encode_rejects_blank_and_downscales() -> None:
    with pytest.raises(CaptureError, match="blank"):
        encode(Image.new("RGB", (800, 600), (0, 0, 0)))
    image = Image.new("RGB", (3200, 1800), (30, 30, 30))
    image.paste((200, 200, 200), (100, 100, 900, 300))
    data, width, height = encode(image)
    assert (width, height) == (1600, 900)
    assert Image.open(io.BytesIO(base64.b64decode(data))).format == "JPEG"


class FakeCapture:
    def __init__(self, result: dict | Exception) -> None:
        self.result = result

    async def capture(self, window: str) -> dict:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_look_at_vscode_pushes_frame_into_the_model() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 32), (10, 120, 200)).save(buffer, format="JPEG")
    shot = {
        "title": "agent.py - jarvis - Visual Studio Code",
        "jpeg_base64": base64.b64encode(buffer.getvalue()).decode(),
    }
    pushed = []
    realtime = SimpleNamespace(push_video=pushed.append)
    context = SimpleNamespace(
        session=SimpleNamespace(
            current_agent=SimpleNamespace(realtime_llm_session=realtime)
        )
    )

    reply = await VSCodeViewTools(FakeCapture(shot)).look_at_vscode(context, "jarvis")  # type: ignore[arg-type]
    assert "agent.py - jarvis" in reply and "video input" in reply
    assert len(pushed) == 1 and (pushed[0].width, pushed[0].height) == (64, 32)

    broken = VSCodeViewTools(FakeCapture({"title": "x", "jpeg_base64": "not-an-image"}))
    with pytest.raises(ToolError, match="unreadable"):
        await broken.look_at_vscode(context)  # type: ignore[arg-type]
    failing = VSCodeViewTools(
        FakeCapture(ToolError("The VS Code window is minimized."))
    )
    with pytest.raises(ToolError, match="minimized"):
        await failing.look_at_vscode(context)  # type: ignore[arg-type]
    assert VSCodeViewTools(None).tools == []


def test_video_frame_conversion() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), (255, 0, 0)).save(buffer, format="PNG")
    frame = to_video_frame(buffer.getvalue())
    assert (frame.width, frame.height) == (40, 20)


def test_command_running_finds_the_tool_process() -> None:
    import subprocess
    import sys
    import time

    from src.jarvis_desktop.coding import command_running

    marker = "jarvis-approval-check-4821"
    script = f"import time; time.sleep(15)  # {marker}"
    started = time.time()
    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        # A brand-new process may not expose its command line for a moment.
        deadline = time.time() + 5
        found = False
        while not found and time.time() < deadline:
            found = command_running(
                f"python -c 'import time; time.sleep(15)  # {marker}'", started
            )
        assert found
        assert not command_running("npm run some-other-command-xyz", started)
    finally:
        process.kill()


def test_approved_and_running_is_not_reported_as_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.jarvis_desktop import coding

    path = write(tmp_path / "s.jsonl", pending_bash(offset=-90))
    event = {
        "event": "Notification",
        "notification_type": "permission_prompt",
        "ts": NOW - 89,
    }
    monkeypatch.setattr(
        coding, "command_running", lambda command, since: command == "npm test"
    )
    report = parse_claude(path, NOW, event)
    assert report is not None
    assert (report.state, report.detection) == ("working", "exact")
    assert "running Bash for 90 s" in report.detail


def test_codex_command_extraction() -> None:
    from src.jarvis_desktop.coding import _codex_command

    payload = {"input": 'text(await tools.exec_command({cmd:"git push origin main"}))'}
    assert _codex_command(payload) == "git push origin main"
    assert _codex_command({"input": "text(await tools.apply_patch(`...`))"}) is None
