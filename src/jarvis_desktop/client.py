"""Outbound WebSocket connection from this PC to jarvis-control."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import OrderedDict
from typing import Any

import aiohttp

from .actions import ActionError, Handler

logger = logging.getLogger("jarvis_desktop")

REMEMBERED_RESULTS = 200
UNAUTHORIZED_RETRY_SECONDS = 300.0
OUTBOX_POLL_SECONDS = 1.0
OUTBOX_STATUS_SECONDS = 60.0


def websocket_url(api_url: str) -> str:
    base = api_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return base + "/v1/desktop/ws"


class DesktopClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        actions: dict[str, Handler],
        outbox: Any = None,
        jobs: Any = None,
    ) -> None:
        self._url = websocket_url(api_url)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._actions = actions
        # Results by action id: a repeated id gets the stored answer instead of
        # running twice. Results that could not be sent go out after reconnecting.
        self._done: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._undelivered: list[dict[str, Any]] = []
        self._tasks: set[asyncio.Future[None]] = set()
        # Running actions by id, so the server can stop one.
        self._running: dict[str, asyncio.Task[Any]] = {}
        # Agent events from the hooks' durable outbox, sent while connected and
        # deleted only when the server acknowledges them.
        self._outbox = outbox
        # Results of the coding agents' jobs, repeated until the server stores them.
        self._jobs = jobs

    async def run_forever(self, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            try:
                await self._connect_once(stop)
                delay = 1.0
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status == 401:
                    logger.error("server rejected this PC's credential; pair it again")
                    delay = UNAUTHORIZED_RETRY_SECONDS
                else:
                    logger.warning("server refused the connection: %s", exc.status)
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                logger.warning("connection failed: %s", exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)
            delay = min(delay * 2, 60.0)

    async def _connect_once(self, stop: asyncio.Event) -> None:
        async with (
            aiohttp.ClientSession() as http,
            http.ws_connect(self._url, headers=self._headers, heartbeat=25) as ws,
        ):
            await ws.send_json(
                {
                    "type": "hello",
                    "capabilities": sorted(self._actions),
                    "results": self._undelivered,
                }
            )
            self._undelivered = []
            logger.info("connected; offering %s", sorted(self._actions))
            stop_task = asyncio.ensure_future(stop.wait())
            pump = asyncio.ensure_future(self._pump(ws))
            try:
                while not stop.is_set():
                    receive = asyncio.ensure_future(ws.receive())
                    done, _ = await asyncio.wait(
                        {receive, stop_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if receive not in done:
                        receive.cancel()
                        break
                    message = receive.result()
                    if message.type != aiohttp.WSMsgType.TEXT:
                        break
                    data = json.loads(message.data)
                    if data.get("type") == "action":
                        task = asyncio.ensure_future(self.handle(ws, data))
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
                    elif data.get("type") == "cancel":
                        running = self._running.get(str(data.get("id", "")))
                        if running is not None and not running.done():
                            logger.info("cancelling action %s", data.get("id"))
                            running.cancel()
                    elif data.get("type") == "job_ack" and self._jobs is not None:
                        self._jobs.acknowledge(str(data.get("job_id", "")))
                    elif data.get("type") == "agent_event_ack" and self._outbox:
                        if self._outbox.acknowledge(data):
                            logger.info("server stored event %s", data.get("event_id"))
            finally:
                stop_task.cancel()
                pump.cancel()
                if self._outbox is not None:
                    self._outbox.forget_inflight()
            logger.info("disconnected (code %s)", ws.close_code)

    async def _pump(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Send due outbox events every second and the outbox state every minute."""
        if self._outbox is None and self._jobs is None:
            return
        last_status = 0.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                if self._outbox is not None:
                    for message in await asyncio.to_thread(self._outbox.due):
                        await ws.send_json(message)
                        logger.info("sent event: %s", message["event"]["title"])
                    if loop.time() - last_status >= OUTBOX_STATUS_SECONDS:
                        await ws.send_json(await asyncio.to_thread(self._outbox.status))
                        last_status = loop.time()
                if self._jobs is not None:
                    for message in await asyncio.to_thread(self._jobs.due):
                        await ws.send_json(message)
                        logger.info(
                            "sent job %s: %s",
                            message["job"]["id"][:8],
                            message["job"]["state"],
                        )
            except (ConnectionError, RuntimeError):
                return
            except Exception:
                # A broken outbox file must not stop the helper's other work.
                logger.exception("outbox send failed")
            await asyncio.sleep(OUTBOX_POLL_SECONDS)

    async def run_action(self, data: dict[str, Any]) -> dict[str, Any]:
        action_id = str(data.get("id", ""))
        if action_id in self._done:
            return self._done[action_id]
        handler = self._actions.get(str(data.get("action", "")))
        reply: dict[str, Any] = {"type": "result", "id": action_id}
        if handler is None:
            reply.update(ok=False, error="This computer does not support that action.")
        else:
            ttl = min(max(float(data.get("ttl", 25)), 1.0), 120.0)
            work = asyncio.ensure_future(
                asyncio.wait_for(handler(dict(data.get("params") or {})), ttl)
            )
            self._running[action_id] = work
            try:
                result = await work
                reply.update(ok=True, result=result)
            except asyncio.CancelledError:
                if not work.cancelled():
                    raise  # the helper itself is shutting down
                reply.update(ok=False, cancelled=True, error="Stopped on request.")
            except asyncio.TimeoutError:
                reply.update(ok=False, error="The computer took too long.")
            except ActionError as exc:
                reply.update(ok=False, error=str(exc))
            except Exception:
                logger.exception("action %s failed", data.get("action"))
                reply.update(ok=False, error="Unexpected error on the computer.")
        self._running.pop(action_id, None)
        logger.info(
            "action %s -> %s",
            data.get("action"),
            "ok"
            if reply["ok"]
            else ("cancelled" if reply.get("cancelled") else "failed"),
        )
        self._done[action_id] = reply
        while len(self._done) > REMEMBERED_RESULTS:
            self._done.popitem(last=False)
        return reply

    async def handle(
        self, ws: aiohttp.ClientWebSocketResponse, data: dict[str, Any]
    ) -> None:
        reply = await self.run_action(data)
        try:
            await ws.send_json(reply)
        except (ConnectionError, RuntimeError):
            self._undelivered.append(reply)
