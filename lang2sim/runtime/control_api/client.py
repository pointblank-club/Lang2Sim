"""
Async control API client. Runs on the HOST (TUI / agent tools / doctor).
File: lang2sim/runtime/control_api/client.py

    async with ControlClient() as api:
        await api.wait_until_ready(timeout=60)
        await api.sim_start(SimStartRequest(...))
        print(await api.sim_status())

Uses httpx; pass `transport=httpx.ASGITransport(app)` in tests to talk to the
server in-process.
"""

from __future__ import annotations

import asyncio
import importlib
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from lang2sim.core.models import (
    NavGoalRequest,
    NavInitialPoseRequest,
    NavStatus,
    NavWaypointsRequest,
    Pose2D,
    SimStartRequest,
    SimStatus,
    SimStatusResponse,
    SimStopRequest,
    TelemetryLive,
    TelemetryRecordRequest,
    TelemetryRecordResult,
)
from lang2sim.runtime.control_api import API_PREFIX

# NB: `from lang2sim.core import config` yields the Config *instance* (core/__init__
# re-exports it under the module's name), so load the module explicitly.
cfg = importlib.import_module("lang2sim.core.config")


class ControlAPIError(RuntimeError):
    """Base exception for HTTP errors returned by the simulation control API."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ControlAPIUnavailable(ControlAPIError):
    """Container not up / API not listening yet."""


def default_base_url() -> str:
    """Return the default HTTP URL for the host control API connection."""
    return f"http://127.0.0.1:{cfg.DEFAULT_CONTROL_PORT}"


class ControlClient:
    """Asynchronous HTTP/WebSocket client for communicating with the simulation control API."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or default_base_url()).rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url + API_PREFIX, timeout=timeout, transport=transport
        )

    async def __aenter__(self) -> ControlClient:
        """Return this client for use in an async context manager."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the HTTP client when leaving an async context manager."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        **kw: Any,  # noqa: ANN401 - httpx request options vary
    ) -> Any:  # noqa: ANN401 - endpoint JSON shapes and HTTP options vary
        try:
            r = await self._http.request(method, path, **kw)
        except (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.ConnectTimeout,
        ) as e:
            raise ControlAPIUnavailable(f"control API not reachable at {self.base_url}: {e}") from e
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ControlAPIError(f"{method} {path} -> {r.status_code}: {detail}", r.status_code)
        return r.json()

    # --- endpoints -------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        """Fetch the API health response."""
        return await self._request("GET", "/health")

    async def info(self) -> dict[str, Any]:
        """Fetch API and simulation environment metadata."""
        return await self._request("GET", "/info")

    async def sim_status(self) -> SimStatusResponse:
        """Return the current simulation container status."""
        return SimStatusResponse.model_validate(await self._request("GET", "/sim/status"))

    async def sim_start(self, req: SimStartRequest) -> SimStatusResponse:
        """Start the simulation with the supplied launch configuration."""
        data = await self._request("POST", "/sim/start", json=req.model_dump(mode="json"))
        return SimStatusResponse.model_validate(data)

    async def sim_stop(self, force: bool = False) -> SimStatusResponse:
        """Stop the simulation, optionally forcing immediate shutdown."""
        data = await self._request(
            "POST", "/sim/stop", json=SimStopRequest(force=force).model_dump(mode="json")
        )
        return SimStatusResponse.model_validate(data)

    async def sim_logs(self, tail: int = 200) -> list[str]:
        """Return up to ``tail`` recent simulation log lines."""
        return (await self._request("GET", "/sim/logs", params={"tail": tail}))["lines"]

    # --- Part 4 world mutation endpoints ---------------------------------
    async def world_spawn(self, req: dict[str, Any]) -> dict[str, Any]:
        """Spawn an entity in the active simulation world."""
        return await self._request("POST", "/world/spawn", json=req)

    async def world_light(self, req: dict[str, Any]) -> dict[str, Any]:
        """Change a light's properties in the active simulation world."""
        return await self._request("POST", "/world/light", json=req)

    async def world_reset(self, req: dict[str, Any] | None = None) -> dict[str, Any]:
        """Reset world state using the supplied options or the API defaults."""
        return await self._request("POST", "/world/reset", json=req or {})

    async def world_chassis(self, req: dict[str, Any]) -> dict[str, Any]:
        """Apply chassis configuration changes to the simulation."""
        return await self._request("POST", "/world/chassis", json=req)

    async def world_health(self) -> dict[str, Any]:
        """Return health information for the simulation world."""
        return await self._request("GET", "/world/health")

    async def world_entities(self) -> dict[str, Any]:
        """List entities currently present in the simulation world."""
        return await self._request("GET", "/world/entities")

    # --- navigation and telemetry -----------------------------------------------
    async def nav_initial_pose(self, pose: Pose2D) -> NavStatus:
        """Set the robot's initial pose estimate for navigation."""
        data = await self._request(
            "POST",
            "/nav/initial_pose",
            json=NavInitialPoseRequest(pose=pose).model_dump(mode="json"),
        )
        return NavStatus.model_validate(data)

    async def nav_goal(self, goal: Pose2D, timeout_seconds: float = 120.0) -> NavStatus:
        """Navigate to one goal pose and return the resulting navigation status."""
        body = NavGoalRequest(goal_pose=goal, timeout_seconds=timeout_seconds).model_dump(
            mode="json"
        )
        return NavStatus.model_validate(await self._request("POST", "/nav/goal", json=body))

    async def nav_waypoints(
        self, waypoints: Sequence[Pose2D], timeout_seconds: float = 180.0
    ) -> NavStatus:
        """Navigate through the supplied waypoint sequence."""
        body = NavWaypointsRequest(
            waypoints=list(waypoints), timeout_seconds=timeout_seconds
        ).model_dump(mode="json")
        return NavStatus.model_validate(await self._request("POST", "/nav/waypoints", json=body))

    async def nav_cancel(self) -> NavStatus:
        """Cancel the active navigation task."""
        return NavStatus.model_validate(await self._request("POST", "/nav/cancel"))

    async def nav_status(self) -> NavStatus:
        """Return the current navigation task status."""
        return NavStatus.model_validate(await self._request("GET", "/nav/status"))

    async def telemetry(self) -> SimStatus:
        """Return the current simulation telemetry summary."""
        return SimStatus.model_validate(await self._request("GET", "/telemetry"))

    # --- telemetry engine ------------------------------------------------
    async def telemetry_live(self) -> TelemetryLive:
        """Return the latest live telemetry snapshot."""
        return TelemetryLive.model_validate(await self._request("GET", "/telemetry/live"))

    async def telemetry_setup(self) -> dict[str, Any]:
        """Return telemetry topic and recorder configuration."""
        return await self._request("GET", "/telemetry/setup")

    async def record_start(
        self, label: str = "run", goal: Pose2D | None = None, rate_hz: float = 20.0
    ) -> TelemetryLive:
        """Start telemetry recording and return the initial live snapshot."""
        req = TelemetryRecordRequest(label=label, goal=goal, rate_hz=rate_hz)
        return TelemetryLive.model_validate(
            await self._request("POST", "/telemetry/record/start", json=req.model_dump(mode="json"))
        )

    async def record_stop(self) -> TelemetryRecordResult:
        """Stop telemetry recording and return the recorded trajectory and metadata."""
        return TelemetryRecordResult.model_validate(
            await self._request("POST", "/telemetry/record/stop")
        )

    async def nav_feedback(self, hz: float = 5.0) -> AsyncIterator[dict[str, Any]]:
        """Stream {"nav": NavStatus, "telemetry": SimStatus, "sim": {...}} dicts from WS /v1/nav/feedback.

        async for msg in api.nav_feedback():
            print(msg["nav"]["distance_remaining"])
        """
        import json

        import websockets

        url = self.base_url.replace("http", "ws", 1) + API_PREFIX + f"/nav/feedback?hz={hz}"
        try:
            async with websockets.connect(url) as ws:
                async for raw in ws:
                    yield json.loads(raw)
        except OSError as e:
            raise ControlAPIUnavailable(f"feedback stream not reachable at {url}: {e}") from e

    async def wait_nav_done(self, poll_s: float = 1.0, timeout: float = 1800.0) -> NavStatus:
        """Poll /nav/status until the current task is no longer active."""
        deadline = time.monotonic() + timeout
        while True:
            st = await self.nav_status()
            if not st.is_active or time.monotonic() > deadline:
                return st
            await asyncio.sleep(poll_s)

    # --- helpers ---------------------------------------------------------
    async def wait_until_ready(
        self, timeout: float = 60.0, interval: float = 0.5
    ) -> dict[str, Any]:
        """Poll /health until it answers (container boot + uvicorn start)."""
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return await self.health()
            except ControlAPIUnavailable as e:
                last = e
                await asyncio.sleep(interval)
        raise ControlAPIUnavailable(f"control API not ready after {timeout:.0f}s ({last})")
