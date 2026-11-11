"""
Host platform + Docker detection.
File: lang2sim/runtime/platform.py

Answers: which OS family are we on (linux / wsl / macos / windows), is there a
display, and can we reach a Docker daemon (and does it have the NVIDIA runtime).
"""

from __future__ import annotations

import json
import os
import platform as _platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

HostOS = Literal["linux", "wsl", "macos", "windows"]
Arch = Literal["amd64", "arm64", "unknown"]

_ARCH_ALIASES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


@dataclass(frozen=True)
class DockerInfo:
    """Docker CLI and daemon introspection results."""

    cli_path: str | None = None
    server_reachable: bool = False
    server_version: str | None = None
    operating_system: str | None = None  # "Docker Desktop", "Ubuntu 24.04 LTS", ...
    arch: Arch = "unknown"
    runtimes: list[str] = field(default_factory=list)
    mem_total_mb: int | None = None  # memory the daemon's VM/host gives containers
    cpus: int | None = None
    error: str | None = None

    @property
    def installed(self) -> bool:
        """Report whether a Docker CLI executable was found."""
        return self.cli_path is not None

    @property
    def is_desktop(self) -> bool:
        """Report whether the Docker daemon identifies itself as Docker Desktop."""
        return bool(self.operating_system and "Docker Desktop" in self.operating_system)

    @property
    def has_nvidia_runtime(self) -> bool:
        """Report whether the Docker daemon exposes an NVIDIA runtime."""
        return "nvidia" in self.runtimes


@dataclass(frozen=True)
class HostPlatform:
    """Resolved host platform: OS family, architecture, display env vars, and Docker info."""

    os: HostOS
    arch: Arch
    env: dict[str, str] = field(default_factory=dict)  # DISPLAY / WAYLAND_DISPLAY / XAUTHORITY
    has_wslg: bool = False
    has_x11_socket_dir: bool = False
    docker: DockerInfo = field(default_factory=DockerInfo)

    @property
    def display(self) -> str | None:
        """Return the host DISPLAY value, or ``None`` when it is unset."""
        return self.env.get("DISPLAY") or None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def normalize_arch(machine: str) -> Arch:
    """Normalize machine architecture string into a standard Arch token."""
    return _ARCH_ALIASES.get(machine.lower(), "unknown")  # type: ignore[return-value]


def classify_os(system: str, release: str, wsl_interop: bool = False) -> HostOS:
    """Classify host operating system into linux, macos, windows, or wsl."""
    s = system.lower()
    if s == "darwin":
        return "macos"
    if s == "windows":
        return "windows"
    if s == "linux" and (wsl_interop or "microsoft" in release.lower()):
        return "wsl"
    return "linux"


def parse_docker_info(info_json: str, cli_path: str | None = None) -> DockerInfo:
    """Parse `docker info --format '{{json .}}'`."""
    try:
        d = json.loads(info_json)
    except json.JSONDecodeError as e:
        return DockerInfo(cli_path=cli_path, error=f"bad docker info output: {e}")
    if d.get("ServerErrors"):
        return DockerInfo(cli_path=cli_path, error="; ".join(d["ServerErrors"]))
    return DockerInfo(
        cli_path=cli_path,
        server_reachable=True,
        server_version=d.get("ServerVersion"),
        operating_system=d.get("OperatingSystem"),
        arch=normalize_arch(d.get("Architecture", "")),
        runtimes=sorted((d.get("Runtimes") or {}).keys()),
        mem_total_mb=int(d["MemTotal"]) // (1024 * 1024) if d.get("MemTotal") else None,
        cpus=d.get("NCPU"),
    )


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def detect_docker(docker: str = "docker", timeout: float = 10.0) -> DockerInfo:
    """Inspect local Docker installation, server version, and daemon status."""
    cli = shutil.which(docker)
    if not cli:
        return DockerInfo(error="docker CLI not found on PATH")
    try:
        out = subprocess.run(
            [cli, "info", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return DockerInfo(cli_path=cli, error="`docker info` timed out (daemon starting?)")
    except OSError as e:
        return DockerInfo(cli_path=cli, error=str(e))
    if out.returncode != 0 or not out.stdout.strip().startswith("{"):
        msg = (out.stderr or out.stdout).strip().splitlines()
        return DockerInfo(cli_path=cli, error=msg[-1] if msg else "docker daemon not reachable")
    return parse_docker_info(out.stdout, cli)


def detect(docker: str = "docker", probe_docker: bool = True) -> HostPlatform:
    """Inspect and resolve the host operating system, architecture, displays, and Docker."""
    host_os = classify_os(
        _platform.system(),
        _platform.release(),
        wsl_interop=Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists(),
    )
    env = {
        k: os.environ[k]
        for k in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR")
        if os.environ.get(k)
    }
    return HostPlatform(
        os=host_os,
        arch=normalize_arch(_platform.machine()),
        env=env,
        has_wslg=Path("/mnt/wslg").is_dir(),
        has_x11_socket_dir=Path("/tmp/.X11-unix").is_dir(),
        docker=detect_docker(docker) if probe_docker else DockerInfo(),
    )
