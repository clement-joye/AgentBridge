from __future__ import annotations

from datetime import datetime, timezone
from collections import deque
import json
from pathlib import Path

from .base import SessionProvider
from ..models import LocalSession


class CopilotProvider(SessionProvider):
    name = "copilot"

    def __init__(self, sessions_dir: Path | None = None) -> None:
        self.sessions_dir = sessions_dir or (Path.home() / ".copilot" / "session-state")

    def discover_sessions(self, limit: int) -> list[LocalSession]:
        if not self.sessions_dir.exists():
            return []
        event_files = sorted(
            self.sessions_dir.glob("*/events.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        out: list[LocalSession] = []
        for path in event_files:
            item = self._parse_session_events(path)
            if item is not None:
                out.append(item)
            if len(out) >= limit:
                break
        return out

    def resume_command(self, session: LocalSession) -> list[str]:
        return ["copilot", "--silent", "--no-color", f"--resume={session.session_id}"]

    def new_command(self, cwd: str) -> list[str]:
        _ = cwd
        return ["copilot", "--silent", "--no-color"]

    def _parse_session_events(self, path: Path) -> LocalSession | None:
        session_dir = path.parent
        workspace = _parse_workspace_yaml(session_dir / "workspace.yaml")
        session_id = str(
            workspace.get("id")
            or session_dir.name
        )
        cwd = str(workspace.get("cwd") or "")
        model = None
        last_user: str | None = None
        last_final: str | None = None
        last_ts = _parse_dt(str(workspace.get("updated_at") or "")) or datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        )

        try:
            with path.open("r", encoding="utf-8") as fh:
                tail_lines = deque(fh, maxlen=400)
        except Exception:
            return None

        for raw in tail_lines:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            ts = _parse_dt(obj.get("timestamp"))
            if ts is not None and ts > last_ts:
                last_ts = ts

            event_type = obj.get("type")
            data = obj.get("data")
            if not isinstance(data, dict):
                continue

            if event_type == "session.start":
                event_session = data.get("sessionId")
                if isinstance(event_session, str) and event_session.strip():
                    session_id = event_session.strip()
                context = data.get("context")
                if isinstance(context, dict):
                    event_cwd = context.get("cwd")
                    if isinstance(event_cwd, str) and event_cwd.strip():
                        cwd = event_cwd.strip()
                selected_model = data.get("selectedModel")
                if isinstance(selected_model, str) and selected_model.strip():
                    model = selected_model.strip()
                continue

            if event_type == "session.model_change":
                new_model = data.get("newModel")
                if isinstance(new_model, str) and new_model.strip():
                    model = new_model.strip()
                continue

            if event_type == "user.message":
                content = data.get("content")
                if isinstance(content, str) and content.strip():
                    last_user = content.strip()
                continue

            if event_type == "assistant.message":
                content = data.get("content")
                if isinstance(content, str) and content.strip():
                    last_final = content.strip()
                continue

        if not cwd:
            return None

        return LocalSession(
            index=0,
            provider="copilot",
            session_id=session_id,
            cwd=cwd,
            repo_name=None,
            branch=None,
            last_active_at=last_ts,
            last_user_prompt=last_user,
            last_final_answer=last_final,
            resumable=True,
            source_path=str(path),
            model=model,
        )


def _parse_workspace_yaml(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        k = key.strip()
        v = value.strip()
        if not k:
            continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in {"'", '"'}:
            v = v[1:-1]
        out[k] = v
    return out


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
