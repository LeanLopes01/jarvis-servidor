"""End-to-end smoke test: dispatch the agent into a fresh room and feed it audio.

Run by jarvis-deploy against the real worker before a release is activated. It
catches what tests on the PC cannot, such as native code that crashes on this CPU.
Reads LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET from the environment.
Exits 0 when the agent joins and stays connected while audio is streamed.
"""

import asyncio
import os
import sys
import time
import uuid

import numpy as np
from livekit import api, rtc

AGENT_NAME = os.environ.get("JARVIS_AGENT_NAME", "my-agent")
SAMPLE_RATE = 48_000
SAMPLES_PER_FRAME = 480  # 10 ms


async def main(seconds: float) -> int:
    room_name = f"jarvis-smoke-{uuid.uuid4().hex[:8]}"
    lkapi = api.LiveKitAPI()
    room = rtc.Room()
    agent_joined = asyncio.Event()
    agent_left = asyncio.Event()
    agent_audio_frames = 0
    readers: set[asyncio.Task[None]] = set()

    @room.on("participant_connected")
    def _joined(p: rtc.RemoteParticipant) -> None:
        print(f"participant joined: {p.identity} kind={p.kind}", flush=True)
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            agent_joined.set()

    @room.on("participant_disconnected")
    def _left(p: rtc.RemoteParticipant) -> None:
        print(f"participant left: {p.identity}", flush=True)
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            agent_left.set()

    @room.on("track_subscribed")
    def _subscribed(track: rtc.Track, *_: object) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return

        async def count() -> None:
            nonlocal agent_audio_frames
            async for _ in rtc.AudioStream(track):
                agent_audio_frames += 1

        task = asyncio.ensure_future(count())
        readers.add(task)
        task.add_done_callback(readers.discard)

    try:
        await lkapi.room.create_room(
            api.CreateRoomRequest(name=room_name, empty_timeout=60)
        )
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room_name)
        )
        token = (
            api.AccessToken()
            .with_identity("smoke-tester")
            .with_grants(api.VideoGrants(room_join=True, room=room_name))
            .to_jwt()
        )
        await room.connect(os.environ["LIVEKIT_URL"], token)
        print(f"connected to {room_name}", flush=True)
        if any(
            p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
            for p in room.remote_participants.values()
        ):
            agent_joined.set()

        source = rtc.AudioSource(SAMPLE_RATE, 1)
        track = rtc.LocalAudioTrack.create_audio_track("mic", source)
        await room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )

        started = time.monotonic()
        await asyncio.wait_for(agent_joined.wait(), 60)
        print(f"agent joined after {time.monotonic() - started:.1f}s", flush=True)

        # A quiet tone with gaps: enough to push frames through noise cancellation.
        offset = 0
        end = time.monotonic() + seconds
        while time.monotonic() < end and not agent_left.is_set():
            idx = np.arange(SAMPLES_PER_FRAME) + offset
            on = (offset // SAMPLE_RATE) % 2 == 0
            tone = np.sin(2 * np.pi * 220 * idx / SAMPLE_RATE) * (2000 if on else 0)
            offset += SAMPLES_PER_FRAME
            await source.capture_frame(
                rtc.AudioFrame(
                    tone.astype(np.int16).tobytes(),
                    SAMPLE_RATE,
                    1,
                    SAMPLES_PER_FRAME,
                )
            )

        still_there = any(
            p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
            for p in room.remote_participants.values()
        )
        print(
            f"streamed {seconds:.0f}s; agent still connected={still_there}; "
            f"agent audio frames received={agent_audio_frames}",
            flush=True,
        )
        return 0 if still_there else 1
    finally:
        await room.disconnect()
        try:
            await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
            print(f"deleted {room_name}", flush=True)
        finally:
            await lkapi.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 20)))
