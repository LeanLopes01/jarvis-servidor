from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    internal_token: str
    agent_name: str = "my-agent"
    agent_unit: str = "jarvis-agent.service"
    ready_file: Path = Path("/run/jarvis-agent/ready")
    db_path: Path = Path("/var/lib/jarvis-control/control.db")
    maintenance_file: Path = Path("/var/lib/jarvis-control/maintenance")
    public_host: str = "127.0.0.1"
    public_port: int = 8090
    internal_host: str = "127.0.0.1"
    internal_port: int = 8091
    # n8n gateway for personal data (/etc/jarvis/personal.env); empty = disabled.
    personal_n8n_url: str = ""
    personal_n8n_secret: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        def get(name: str, default: str | None = None) -> str:
            value = env.get(name, default)
            if value is None or value == "":
                raise SystemExit(f"jarvis-control: {name} is not set")
            return value

        state_dir = Path(env.get("JARVIS_CONTROL_STATE_DIR", "/var/lib/jarvis-control"))
        return cls(
            livekit_url=get("LIVEKIT_URL"),
            livekit_api_key=get("LIVEKIT_API_KEY"),
            livekit_api_secret=get("LIVEKIT_API_SECRET"),
            internal_token=get("JARVIS_INTERNAL_TOKEN"),
            agent_name=env.get("JARVIS_AGENT_NAME", "my-agent"),
            agent_unit=env.get("JARVIS_AGENT_UNIT", "jarvis-agent.service"),
            ready_file=Path(env.get("JARVIS_READY_FILE", "/run/jarvis-agent/ready")),
            db_path=state_dir / "control.db",
            maintenance_file=state_dir / "maintenance",
            public_port=int(env.get("JARVIS_CONTROL_PORT", "8090")),
            internal_port=int(env.get("JARVIS_CONTROL_INTERNAL_PORT", "8091")),
            personal_n8n_url=env.get("JARVIS_PERSONAL_N8N_URL", "").strip(),
            personal_n8n_secret=env.get("JARVIS_PERSONAL_N8N_SECRET", "").strip(),
        )
