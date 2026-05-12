from __future__ import annotations

from datetime import datetime, timezone
from collections import deque
import json
from pathlib import Path

from .base import SessionProvider
from ..models import LocalSession


class CodexProvider(SessionProvider):
    name = "codex"

    def __init__(self, sessions_dir: Path | None = None) -> None:
        self.sessions_dir = sessions_dir or (Path.home() / ".codex" / "sessions")

    def discover_sessions(self, limit: int) -> list[LocalSession]:
        if not self.sessions_dir.exists():
            return []
        session_files = list(self.sessions_dir.glob("**/*.json"))
        session_files.extend(self.sessions_dir.glob("**/*.jsonl"))
        session_files = sorted(session_files, key=lambda p: p.stat().st_mtime, reverse=True)
        out: list[LocalSession] = []
        for path in session_files:
            item = self._parse_session_jsonl_file(path) if path.suffix == ".jsonl" else self._parse_session_file(path)
            if item:
                out.append(item)
            if len(out) >= limit:
                break
        return out

    def _parse_session_file(self, path: Path) -> LocalSession | None:
        try:
            data = json.loads(path.read_text())
        except Exception:
            return None

        session_id = str(data.get("id") or data.get("session_id") or path.stem)
        cwd = str(data.get("cwd") or data.get("working_directory") or "")
        if not cwd:
            return None

        last_user = data.get("last_user_prompt")
        last_final = data.get("last_final_answer")
        model = data.get("model")
        ts = data.get("updated_at") or data.get("last_active_at")
        dt = _parse_dt(ts) if ts else datetime.fromtimestamp(path.stat().st_mtime)
        return LocalSession(
            index=0,
            provider="codex",
            session_id=session_id,
            cwd=cwd,
            repo_name=None,
            branch=None,
            last_active_at=dt,
            last_user_prompt=last_user,
            last_final_answer=last_final,
            resumable=True,
            source_path=str(path),
            model=str(model) if model else None,
        )

    def resume_command(self, session: LocalSession) -> list[str]:
        return ["codex", "resume", "--no-alt-screen", session.session_id]

    def new_command(self, cwd: str) -> list[str]:
        return ["codex", "--no-alt-screen"]

    def _parse_session_jsonl_file(self, path: Path) -> LocalSession | None:
        try:
            with path.open("r", encoding="utf-8") as fh:
                first = fh.readline().strip()
        except Exception:
            return None
        if not first:
            return None
        try:
            first_obj = json.loads(first)
        except Exception:
            return None

        payload = first_obj.get("payload", {}) if isinstance(first_obj, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        session_id = str(payload.get("id") or path.stem)
        cwd = str(payload.get("cwd") or "")
        if not cwd:
            return None

        last_user: str | None = None
        last_final: str | None = None
        last_ts: datetime | None = _parse_dt(first_obj.get("timestamp"))
        model = _extract_model(first_obj)

        try:
            with path.open("r", encoding="utf-8") as fh:
                tail_lines = deque(fh, maxlen=300)
        except Exception:
            tail_lines = deque()

        for raw in tail_lines:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue

            ts = _parse_dt(obj.get("timestamp"))
            if ts is not None:
                if last_ts is None or ts > last_ts:
                    last_ts = ts

            found_model = _extract_model(obj)
            if found_model:
                model = found_model

            completed = _extract_completed_turn_final(obj)
            if completed:
                prompt_text, final_text = completed
                if prompt_text:
                    last_user = prompt_text
                last_final = final_text
                continue

            text = _extract_text(obj)
            if not text:
                continue
            role = _extract_role(obj)
            if role == "user":
                last_user = text

        dt = last_ts or datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return LocalSession(
            index=0,
            provider="codex",
            session_id=session_id,
            cwd=cwd,
            repo_name=None,
            branch=None,
            last_active_at=dt,
            last_user_prompt=last_user,
            last_final_answer=last_final,
            resumable=True,
            source_path=str(path),
            model=model,
        )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_role(obj: dict) -> str | None:
    payload = obj.get("payload")
    if isinstance(payload, dict):
        event_type = payload.get("type")
        if isinstance(event_type, str) and event_type.lower() == "user_message":
            return "user"
        role = payload.get("role")
        if isinstance(role, str):
            return role.lower()
    item_type = obj.get("type")
    if item_type == "user_message":
        return "user"
    if item_type == "assistant_message":
        return "assistant"
    return None


def _extract_text(obj: dict) -> str | None:
    payload = obj.get("payload")
    if isinstance(payload, dict):
        if payload.get("type") == "user_message":
            msg = payload.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        content = payload.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                txt = block.get("text")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
            if parts:
                return "\n".join(parts)
        if isinstance(content, str) and content.strip():
            return content.strip()
        args = payload.get("arguments")
        if isinstance(args, str) and args.strip():
            return args.strip()
    return None


def _extract_completed_turn_final(obj: dict) -> tuple[str | None, str] | None:
    if obj.get("type") != "event_msg":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "task_complete":
        return None
    final = payload.get("last_agent_message")
    if not isinstance(final, str) or not final.strip():
        return None
    user_prompt = payload.get("user_message")
    prompt_text = user_prompt.strip() if isinstance(user_prompt, str) and user_prompt.strip() else None
    return prompt_text, final.strip()


def _extract_model(obj: dict) -> str | None:
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    for key in ("model", "model_slug"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    settings = payload.get("settings")
    if isinstance(settings, dict):
        value = settings.get("model")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
