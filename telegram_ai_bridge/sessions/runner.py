from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections import deque
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Callable, Final

from ..models import ActiveMode, LocalSession
from ..providers.base import SessionProvider
from ..utils.output_cleanup import remove_prompt_echo

import logging

try:
    import pexpect
except Exception:  # pragma: no cover
    pexpect = None

PROMPT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)^\s*(you|user|copilot)\s*>\s*$"),
    re.compile(r"(?im)^\s*(codex|assistant)\s*>\s*$"),
    re.compile(r"(?im)^\s*>\s*$"),
)
USER_PROMPT_LINE: Final[re.Pattern[str]] = re.compile(r"(?im)^\s*(?:you|user)\s*>\s*(.*)$")
CODEX_PROMPT_LINE: Final[re.Pattern[str]] = re.compile(r"(?im)^\s*›\s*(.*)$")
ASSISTANT_PROMPT_LINE: Final[re.Pattern[str]] = re.compile(r"(?im)^\s*(?:codex|assistant|copilot)\s*>\s*(.*)$")
BARE_PROMPT_LINE: Final[re.Pattern[str]] = re.compile(r"(?im)^\s*>\s*$")
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RunnerResult:
    ok: bool
    output: str
    changed_files: list[str]
    timed_out: bool = False
    detected_session_id: str | None = None
    progress_messages: list[str] | None = None
    tool_calls: list[str] | None = None
    reasoning_events: list[str] | None = None
    subagent_events: list[str] | None = None
    prompt_matched: bool = False


@dataclass(slots=True)
class CodexTerminalTurn:
    prompt_matched: bool
    completed: bool
    final_text: str | None
    progress_messages: list[str] | None = None
    tool_calls: list[str] | None = None
    reasoning_events: list[str] | None = None
    subagent_events: list[str] | None = None


@dataclass(slots=True)
class CodexExecResult:
    final_text: str | None
    detected_session_id: str | None
    progress_messages: list[str]
    tool_calls: list[str]
    reasoning_events: list[str]
    subagent_events: list[str]
    prompt_matched: bool


class AgentRunner:
    def __init__(self, timeout_seconds: int = 180, quiet_window_seconds: float = 1.2) -> None:
        self.timeout_seconds = timeout_seconds
        self.quiet_window_seconds = quiet_window_seconds
        self._lock = threading.Lock()
        self._children: dict[str, pexpect.spawn] = {}
        self._child_signatures: dict[str, tuple[str, str]] = {}
        self._codex_file_cache: dict[str, Path] = {}
        self._copilot_file_cache: dict[str, Path] = {}
        self._start_errors: dict[str, str] = {}

    def send_prompt(
        self,
        chat_key: str,
        provider: SessionProvider,
        session: LocalSession,
        prompt: str,
        mode: ActiveMode,
        progress_cb: Callable[[str], None] | None = None,
    ) -> RunnerResult:
        # Keep Codex sessions warm across prompts for CLI-like responsiveness.
        # Turn isolation now comes from JSONL turn binding instead of per-prompt
        # process restarts.
        before_files = self._changed_files(session.cwd)
        if provider.name == "codex":
            result = self._run_codex_exec(provider, session, prompt, mode, progress_cb=progress_cb)
        elif provider.name == "copilot":
            result = self._run_copilot_exec(provider, session, prompt, mode)
        elif pexpect is not None:
            result = self._run_pexpect(chat_key, provider, session, prompt, mode)
        else:
            result = self._run_fallback(provider, session, prompt, mode)
        after_files = self._changed_files(session.cwd)
        if after_files:
            result.changed_files = after_files
        elif before_files:
            result.changed_files = before_files
        return result

    def _run_pexpect(
        self, chat_key: str, provider: SessionProvider, session: LocalSession, prompt: str, mode: ActiveMode
    ) -> RunnerResult:
        started_at = time.time()
        logger.info(
            "Runner starting provider=%s mode=%s chat_key=%s session_id=%s cwd=%s prompt_chars=%d",
            provider.name,
            mode,
            chat_key,
            session.session_id,
            session.cwd,
            len(prompt),
        )
        child = self._ensure_child(chat_key, provider, session, mode)
        if child is None:
            detail = self._start_errors.pop(chat_key, "Failed to start session process")
            logger.error("Runner failed to start provider=%s chat_key=%s detail=%s", provider.name, chat_key, detail)
            return RunnerResult(ok=False, output=detail, changed_files=[])

        self._drain_stale_output(child)
        output_parts: list[str] = []
        deadline = time.monotonic() + self.timeout_seconds
        last_data_at = time.monotonic()
        last_wait_log_at = time.monotonic()
        saw_new_data = False
        codex_file: Path | None = None
        codex_start_offset: int | None = None
        copilot_file: Path | None = None
        copilot_start_offset: int | None = None
        if provider.name == "codex":
            codex_file = self._find_codex_session_file_for_turn(session, started_at)
            if codex_file is not None:
                try:
                    codex_start_offset = codex_file.stat().st_size
                except Exception:
                    codex_start_offset = None
        elif provider.name == "copilot":
            copilot_file = self._find_copilot_session_file(session)
            if copilot_file is not None:
                try:
                    copilot_start_offset = copilot_file.stat().st_size
                except Exception:
                    copilot_start_offset = None
            logger.warning(
                "Runner copilot setup chat_key=%s events_file=%s start_offset=%s",
                chat_key,
                copilot_file,
                copilot_start_offset,
            )
        try:
            self._send_terminal_prompt(child, prompt)

            # For copilot: use a combined loop that drains the PTY *and* watches
            # events.jsonl simultaneously.  Keeping the PTY drained is critical —
            # if the buffer (~4 KB) fills up copilot blocks on write and never
            # appends the assistant response to events.jsonl (deadlock).
            #
            # Resumed sessions never write new events to events.jsonl (copilot only
            # updates workspace.yaml / lock files).  For those, we fall back to
            # PTY output once the terminal goes quiet.
            if provider.name == "copilot":
                copilot_jsonl_pos = copilot_start_offset or 0
                prompt_norm = _norm_text(prompt)
                saw_matching_user = False
                copilot_final_text = ""
                copilot_last_log_at = time.monotonic()
                # Tracks when events.jsonl last had new data; used to detect the
                # "resumed session" case where events never appear.
                jsonl_last_event_at: float | None = None

                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        logger.error("Runner timed out provider=copilot chat_key=%s", chat_key)
                        return RunnerResult(ok=False, output="Command timed out", changed_files=[], timed_out=True)

                    if now - copilot_last_log_at >= 10:
                        logger.warning(
                            "Runner copilot still waiting chat_key=%s elapsed=%.1fs "
                            "saw_user=%s final_chars=%d pty_chars=%d jsonl_stale=%.1fs",
                            chat_key,
                            now - (deadline - self.timeout_seconds),
                            saw_matching_user,
                            len(copilot_final_text),
                            sum(len(p) for p in output_parts),
                            now - jsonl_last_event_at if jsonl_last_event_at is not None else -1,
                        )
                        copilot_last_log_at = now

                    # Drain PTY (non-blocking, short timeout to stay responsive)
                    try:
                        chunk = child.read_nonblocking(size=4096, timeout=0.1)
                        if chunk:
                            output_parts.append(chunk)
                            saw_new_data = True
                            last_data_at = time.monotonic()
                    except pexpect.TIMEOUT:
                        pass
                    except pexpect.EOF:
                        logger.warning("Runner copilot child EOF chat_key=%s", chat_key)
                        self.close_session(chat_key)
                        break

                    # PTY quiet-window fallback: if PTY went quiet AND events.jsonl
                    # has had no new data since we started, use the PTY output
                    # directly.  This handles resumed sessions where copilot does not
                    # append to events.jsonl.
                    if (
                        saw_new_data
                        and time.monotonic() - last_data_at >= self.quiet_window_seconds
                        and jsonl_last_event_at is None
                    ):
                        logger.warning(
                            "Runner copilot PTY fallback (no jsonl events) chat_key=%s pty_chars=%d",
                            chat_key,
                            sum(len(p) for p in output_parts),
                        )
                        break

                    # Poll events.jsonl for the assistant response
                    if copilot_file is not None:
                        new_events, copilot_jsonl_pos = _read_jsonl_from_offset(copilot_file, copilot_jsonl_pos)
                        if new_events:
                            jsonl_last_event_at = time.monotonic()
                        for obj in new_events:
                            t = obj.get("type")
                            data = obj.get("data", {})
                            if not isinstance(data, dict):
                                data = {}

                            logger.warning(
                                "Runner copilot event chat_key=%s type=%s saw_user=%s",
                                chat_key, t, saw_matching_user,
                            )

                            if t == "user.message":
                                content = data.get("content", "")
                                content_norm = _norm_text(content) if isinstance(content, str) else ""
                                if content_norm == prompt_norm:
                                    saw_matching_user = True
                                    logger.warning(
                                        "Runner copilot user message matched chat_key=%s", chat_key
                                    )
                                else:
                                    logger.warning(
                                        "Runner copilot user.message mismatch chat_key=%s "
                                        "expected=%r got=%r",
                                        chat_key, prompt_norm[:80], content_norm[:80],
                                    )
                                continue

                            if saw_matching_user and t == "assistant.message":
                                content = data.get("content")
                                if isinstance(content, str) and content.strip():
                                    copilot_final_text = content.strip()
                                continue

                            if saw_matching_user and t == "assistant.turn_end":
                                logger.warning(
                                    "Runner copilot turn_end chat_key=%s final_chars=%d",
                                    chat_key, len(copilot_final_text),
                                )
                                break  # inner for-loop
                        else:
                            # No turn_end yet — keep looping
                            continue
                        # turn_end was hit — exit the while loop
                        break

                raw_pty = "".join(output_parts).strip()
                pty_text = remove_prompt_echo(_sanitize_terminal_text(raw_pty), prompt).strip()
                final_text = copilot_final_text or pty_text or "No output"
                logger.warning(
                    "Runner copilot finished chat_key=%s pty_chars=%d saw_user=%s "
                    "jsonl_chars=%d pty_text_chars=%d source=%s",
                    chat_key,
                    len(raw_pty),
                    saw_matching_user,
                    len(copilot_final_text),
                    len(pty_text),
                    "jsonl" if copilot_final_text else "pty",
                )
                detected = _detect_session_id(raw_pty)
                return RunnerResult(
                    ok=True,
                    output=final_text.strip() or "No output",
                    changed_files=[],
                    detected_session_id=detected,
                    progress_messages=[],
                    tool_calls=[],
                    reasoning_events=[],
                    subagent_events=[],
                    prompt_matched=saw_matching_user,
                )

            while True:
                now = time.monotonic()
                if now - last_wait_log_at >= 10:
                    preview = _sanitize_terminal_text("".join(output_parts))[-500:].replace("\n", "\\n")
                    raw_preview = "".join(output_parts)[-500:].replace("\n", "\\n").replace("\r", "\\r")
                    logger.warning(
                        "Runner still waiting provider=%s chat_key=%s elapsed=%.1fs saw_output=%s raw_chars=%d "
                        "preview=%r raw_tail=%r",
                        provider.name,
                        chat_key,
                        now - (deadline - self.timeout_seconds),
                        saw_new_data,
                        sum(len(part) for part in output_parts),
                        preview,
                        raw_preview,
                    )
                    last_wait_log_at = now
                if now >= deadline:
                    logger.error("Runner timed out provider=%s chat_key=%s", provider.name, chat_key)
                    return RunnerResult(ok=False, output="Command timed out", changed_files=[], timed_out=True)
                if provider.name == "codex" and codex_file is None:
                    codex_file = self._find_codex_session_file_for_turn(session, started_at)
                    if codex_file is not None:
                        codex_start_offset = 0
                try:
                    chunk = child.read_nonblocking(size=4096, timeout=0.25)
                    if chunk:
                        output_parts.append(chunk)
                        last_data_at = time.monotonic()
                        saw_new_data = True
                        combined = "".join(output_parts)
                        if provider.name == "codex":
                            parsed = _parse_codex_terminal_turn(combined, prompt)
                            if parsed.completed and parsed.final_text:
                                break
                        elif self._looks_complete(combined):
                            break
                except pexpect.TIMEOUT:
                    if saw_new_data and time.monotonic() - last_data_at >= self.quiet_window_seconds:
                        if provider.name == "codex":
                            parsed = _parse_codex_terminal_turn("".join(output_parts), prompt)
                            if parsed.final_text:
                                break
                        else:
                            break
                except pexpect.EOF:
                    logger.warning("Runner child reached EOF provider=%s chat_key=%s", provider.name, chat_key)
                    self.close_session(chat_key)
                    break
            output = "".join(output_parts).strip()
            logger.info(
                "Runner terminal collection finished provider=%s chat_key=%s raw_chars=%d",
                provider.name,
                chat_key,
                len(output),
            )
            detected = _detect_session_id(output)
            final_text = output or "No output"
            prompt_matched = False
            if provider.name == "codex":
                parsed = _parse_codex_terminal_turn(output, prompt)
                if codex_file is not None and codex_start_offset is not None:
                    collected = self._collect_codex_turn_window(codex_file, codex_start_offset, prompt, 0.5)
                    if collected:
                        final_text = str(collected["final_text"])
                        detected = detected or self._session_id_from_codex_file(codex_file)
                        logger.info(
                            "Runner using Codex JSONL result chat_key=%s final_chars=%d prompt_matched=%s",
                            chat_key,
                            len(final_text),
                            bool(collected.get("prompt_matched", False)),
                        )
                        return RunnerResult(
                            ok=True,
                            output=final_text.strip() or "No output",
                            changed_files=[],
                            detected_session_id=detected,
                            progress_messages=_string_list(collected.get("progress_messages")),
                            tool_calls=_string_list(collected.get("tool_calls")),
                            reasoning_events=_string_list(collected.get("reasoning_events")),
                            subagent_events=_string_list(collected.get("subagent_events")),
                            prompt_matched=bool(collected.get("prompt_matched", False)),
                        )
                prompt_matched = parsed.prompt_matched
                if parsed.final_text:
                    final_text = parsed.final_text
                elif prompt_matched:
                    final_text = "No output"
                else:
                    final_text = "No response captured from terminal."
                logger.info(
                    "Runner using Codex terminal result chat_key=%s final_chars=%d prompt_matched=%s "
                    "progress=%d tools=%d reasoning=%d subagents=%d",
                    chat_key,
                    len(final_text),
                    prompt_matched,
                    len(parsed.progress_messages or []),
                    len(parsed.tool_calls or []),
                    len(parsed.reasoning_events or []),
                    len(parsed.subagent_events or []),
                )

            return RunnerResult(
                ok=True,
                output=final_text.strip() if provider.name == "codex" else _trim_output(final_text),
                changed_files=[],
                detected_session_id=detected,
                progress_messages=parsed.progress_messages if provider.name == "codex" else [],
                tool_calls=parsed.tool_calls if provider.name == "codex" else [],
                reasoning_events=parsed.reasoning_events if provider.name == "codex" else [],
                subagent_events=parsed.subagent_events if provider.name == "codex" else [],
                prompt_matched=prompt_matched,
            )
        except Exception as exc:
            self.close_session(chat_key)
            logger.exception("Runner error provider=%s chat_key=%s", provider.name, chat_key)
            return RunnerResult(ok=False, output=f"Runner error: {exc}", changed_files=[])

    def _run_codex_exec(
        self,
        provider: SessionProvider,
        session: LocalSession,
        prompt: str,
        mode: ActiveMode,
        progress_cb: Callable[[str], None] | None = None,
    ) -> RunnerResult:
        started_at = time.time()
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check"]
        if mode != "new" and not session.session_id.startswith("pending:"):
            cmd = ["codex", "exec", "resume", "--json", "--skip-git-repo-check"]
            if session.model:
                cmd.extend(["-m", session.model])
            cmd.extend([session.session_id, "-"])
        else:
            if session.model:
                cmd.extend(["-m", session.model])
            cmd.append("-")
        resolved = shutil.which(cmd[0])
        if resolved:
            cmd = [resolved, *cmd[1:]]
        logger.warning(
            "Codex exec starting mode=%s session_id=%s cwd=%s prompt_chars=%d",
            mode,
            session.session_id,
            session.cwd,
            len(prompt),
        )
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=session.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as exc:
            logger.exception("Codex exec failed to start mode=%s session_id=%s", mode, session.session_id)
            return RunnerResult(ok=False, output=f"Runner error: {exc}", changed_files=[])

        # Drain stderr in a background thread to prevent pipe-buffer deadlock.
        stderr_lines: list[str] = []

        def _read_stderr() -> None:
            try:
                stderr_lines.extend(proc.stderr.readlines())  # type: ignore[union-attr]
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        stdout_lines: list[str] = []
        timed_out = False
        deadline = time.monotonic() + self.timeout_seconds
        try:
            proc.stdin.write(prompt)  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            for raw_line in proc.stdout:  # type: ignore[union-attr]
                if time.monotonic() > deadline:
                    proc.terminate()
                    timed_out = True
                    break
                stdout_lines.append(raw_line)
                if progress_cb is None:
                    continue
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    t = obj.get("type")
                    payload = obj.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    ptype = str(payload.get("type", ""))
                    if t == "event_msg" and ptype == "agent_message":
                        msg = payload.get("message", "")
                        phase = str(payload.get("phase") or "").lower()
                        if isinstance(msg, str) and msg.strip():
                            progress_cb(_summarize_progress_message(msg, phase))
                    elif t == "response_item" and ptype in {"function_call", "custom_tool_call"}:
                        name = payload.get("name") or payload.get("tool_name") or "tool_call"
                        if isinstance(name, str):
                            progress_cb(_format_tool_call(name, payload))
                except Exception:
                    pass
        except Exception as exc:
            logger.exception("Codex exec stdout read error mode=%s session_id=%s", mode, session.session_id)
            return RunnerResult(ok=False, output=f"Runner error: {exc}", changed_files=[])
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            stderr_thread.join(timeout=2)

        if timed_out:
            stdout = "".join(stdout_lines)
            stderr = "".join(stderr_lines)
            logger.error(
                "Codex exec timed out mode=%s session_id=%s stdout_chars=%d stderr=%r",
                mode,
                session.session_id,
                len(stdout),
                stderr[-500:],
            )
            partial = _parse_codex_exec_output(stdout, prompt)
            if partial and partial.final_text:
                return _runner_result_from_codex_exec(partial, session.session_id, ok=True)
            return RunnerResult(ok=False, output="Command timed out", changed_files=[], timed_out=True)

        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        returncode = proc.returncode if proc.returncode is not None else -1
        logger.warning(
            "Codex exec finished mode=%s session_id=%s returncode=%s stdout_chars=%d stderr_chars=%d",
            mode,
            session.session_id,
            returncode,
            len(stdout),
            len(stderr),
        )
        parsed = _parse_codex_exec_output(stdout, prompt)
        detected = parsed.detected_session_id if parsed else None
        if not detected and mode == "new":
            detected = self._detect_recent_codex_session_id(session.cwd, started_at)
        if parsed and parsed.final_text:
            return _runner_result_from_codex_exec(parsed, detected or session.session_id, ok=returncode == 0)

        fallback = (stdout or stderr).strip()
        if not fallback:
            fallback = f"Codex exited with status {returncode} and produced no output."
        return RunnerResult(
            ok=returncode == 0,
            output=_trim_output(fallback),
            changed_files=[],
            detected_session_id=detected,
        )

    def _detect_recent_codex_session_id(self, cwd: str, started_at: float) -> str | None:
        found = self._find_latest_codex_session_file_for_cwd(cwd, started_at - 5)
        if found is None:
            return None
        return self._session_id_from_codex_file(found)

    def _run_copilot_exec(
        self, provider: SessionProvider, session: LocalSession, prompt: str, mode: ActiveMode
    ) -> RunnerResult:
        """Run a copilot prompt via subprocess (mirroring the codex exec strategy).

        Copilot does not append new events to events.jsonl for resumed sessions, so
        the pexpect + JSONL approach is unreliable.  Using subprocess.run with piped
        stdin is simpler and consistent with how codex exec works.
        """
        cmd = provider.new_command(session.cwd) if mode == "new" else provider.resume_command(session)
        resolved = shutil.which(cmd[0])
        if resolved:
            cmd = [resolved, *cmd[1:]]
        logger.warning(
            "Copilot exec starting mode=%s session_id=%s cwd=%s prompt_chars=%d cmd=%s",
            mode,
            session.session_id,
            session.cwd,
            len(prompt),
            cmd,
        )
        try:
            proc = subprocess.run(
                cmd,
                cwd=session.cwd,
                input=prompt + "\n",
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "").strip() if isinstance(exc.stdout, str) else ""
            logger.error(
                "Copilot exec timed out mode=%s session_id=%s stdout_chars=%d",
                mode,
                session.session_id,
                len(stdout),
            )
            if stdout:
                text = remove_prompt_echo(_sanitize_terminal_text(stdout), prompt).strip()
                if text:
                    return RunnerResult(ok=True, output=_trim_output(text), changed_files=[])
            return RunnerResult(ok=False, output="Command timed out", changed_files=[], timed_out=True)
        except Exception as exc:
            logger.exception("Copilot exec failed to start mode=%s session_id=%s", mode, session.session_id)
            return RunnerResult(ok=False, output=f"Runner error: {exc}", changed_files=[])

        logger.warning(
            "Copilot exec finished mode=%s session_id=%s returncode=%s stdout_chars=%d stderr_chars=%d",
            mode,
            session.session_id,
            proc.returncode,
            len(proc.stdout or ""),
            len(proc.stderr or ""),
        )
        stdout = proc.stdout or ""
        text = remove_prompt_echo(_sanitize_terminal_text(stdout), prompt).strip()
        if not text:
            # stderr sometimes carries the real response (e.g. non-TTY fallback)
            text = _sanitize_terminal_text(proc.stderr or "").strip()
        detected = _detect_session_id(stdout)
        return RunnerResult(
            ok=proc.returncode == 0,
            output=_trim_output(text) or "No output",
            changed_files=[],
            detected_session_id=detected,
            progress_messages=[],
            tool_calls=[],
            reasoning_events=[],
            subagent_events=[],
        )

    def _run_fallback(
        self, provider: SessionProvider, session: LocalSession, prompt: str, mode: ActiveMode
    ) -> RunnerResult:
        if provider.name == "codex":
            return RunnerResult(
                ok=False,
                output=(
                    "PTY support is required for Codex resume/new sessions, but `pexpect` is not installed.\n"
                    "Install it in the bridge venv: `./.venv/bin/pip install pexpect`.\n"
                    "Then restart the bridge process."
                ),
                changed_files=[],
            )
        cmd = provider.new_command(session.cwd) if mode == "new" else provider.resume_command(session)
        try:
            proc = subprocess.run(
                cmd,
                cwd=session.cwd,
                input=prompt + "\n",
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            text = proc.stdout.strip() or proc.stderr.strip() or "No output"
            return RunnerResult(
                ok=proc.returncode == 0,
                output=_trim_output(text),
                changed_files=[],
                detected_session_id=_detect_session_id(text),
            )
        except subprocess.TimeoutExpired:
            return RunnerResult(ok=False, output="Command timed out", changed_files=[], timed_out=True)

    def _changed_files(self, cwd: str) -> list[str]:
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                return []
            files: list[str] = []
            for line in proc.stdout.splitlines():
                if not line.strip():
                    continue
                path = line[3:].strip()
                if path:
                    files.append(path)
            return files
        except Exception:
            return []

    def close_session(self, chat_key: str) -> None:
        with self._lock:
            child = self._children.pop(chat_key, None)
            self._child_signatures.pop(chat_key, None)
        if child and child.isalive():
            child.close(force=True)
        self._start_errors.pop(chat_key, None)

    def abort_session(self, chat_key: str) -> bool:
        with self._lock:
            child = self._children.get(chat_key)
        if not child:
            return False
        if child.isalive():
            child.close(force=True)
        with self._lock:
            self._children.pop(chat_key, None)
            self._child_signatures.pop(chat_key, None)
            self._start_errors.pop(chat_key, None)
        return True

    def has_live_session(self, chat_key: str) -> bool:
        with self._lock:
            child = self._children.get(chat_key)
        return bool(child and child.isalive())

    def cleanup_dead_sessions(self) -> list[str]:
        removed: list[str] = []
        with self._lock:
            keys = list(self._children.keys())
            for key in keys:
                child = self._children.get(key)
                if child is None:
                    continue
                if not child.isalive():
                    self._children.pop(key, None)
                    self._child_signatures.pop(key, None)
                    self._start_errors.pop(key, None)
                    removed.append(key)
        return removed

    def close_all_sessions(self) -> None:
        with self._lock:
            keys = list(self._children.keys())
        for key in keys:
            self.close_session(key)

    def _ensure_child(
        self, chat_key: str, provider: SessionProvider, session: LocalSession, mode: ActiveMode
    ) -> pexpect.spawn | None:
        signature = (mode, f"{session.session_id}|{session.cwd}|{provider.name}")
        with self._lock:
            existing = self._children.get(chat_key)
            existing_sig = self._child_signatures.get(chat_key)
            if existing and existing.isalive() and existing_sig == signature:
                return existing
            if existing and existing.isalive():
                existing.close(force=True)
            cmd = provider.resume_command(session)
            if mode == "new":
                cmd = provider.new_command(session.cwd)
            resolved = shutil.which(cmd[0])
            if resolved:
                cmd = [resolved, *cmd[1:]]
            try:
                child = pexpect.spawn(
                    cmd[0],
                    cmd[1:],
                    cwd=session.cwd,
                    encoding="utf-8",
                    timeout=self.timeout_seconds,
                )
            except Exception as exc:
                guidance = ""
                if isinstance(exc, FileNotFoundError):
                    guidance = (
                        f" Executable `{cmd[0]}` was not found in the bridge process PATH."
                        " If the bot runs as a service, make sure the service environment includes the Codex CLI path."
                    )
                self._start_errors[chat_key] = f"Failed to start session process: {exc}.{guidance}".strip()
                return None
            self._children[chat_key] = child
            self._child_signatures[chat_key] = signature
            self._start_errors.pop(chat_key, None)
            return child

    def _drain_stale_output(self, child: pexpect.spawn) -> None:
        idle_reads = 0
        for _ in range(32):
            try:
                chunk = child.read_nonblocking(size=2048, timeout=0.05)
                if not chunk:
                    idle_reads += 1
                    if idle_reads >= 2:
                        break
                    continue
                idle_reads = 0
            except (pexpect.TIMEOUT, pexpect.EOF):
                idle_reads += 1
                if idle_reads >= 2:
                    break

    def _send_terminal_prompt(self, child: pexpect.spawn, prompt: str) -> None:
        # Codex's TUI expects carriage return. pexpect.sendline() can write only
        # "\n" on POSIX, which leaves some TUIs with text echoed but not submitted.
        child.send(prompt)
        child.send("\r")

    def _looks_complete(self, text: str) -> bool:
        tail = text[-1200:]
        for pat in PROMPT_PATTERNS:
            if pat.search(tail):
                return True
        return False

    def _find_codex_session_file(self, session_id: str) -> Path | None:
        if session_id.startswith("pending:"):
            return None
        cached = self._codex_file_cache.get(session_id)
        if cached and cached.exists():
            return cached
        root = Path.home() / ".codex" / "sessions"
        if not root.exists():
            return None
        candidates = sorted(root.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in candidates[:500]:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    line = fh.readline().strip()
                if not line:
                    continue
                obj = json.loads(line)
                payload = obj.get("payload", {}) if isinstance(obj, dict) else {}
                found = str(payload.get("id") or "")
                if found == session_id:
                    self._codex_file_cache[session_id] = path
                    return path
            except Exception:
                continue
        return None

    def _find_codex_session_file_for_turn(self, session: LocalSession, started_at: float) -> Path | None:
        found = self._find_codex_session_file(session.session_id)
        if found is not None:
            return found
        return self._find_latest_codex_session_file_for_cwd(session.cwd, started_at - 5)

    def _find_latest_codex_session_file_for_cwd(self, cwd: str, since_epoch: float) -> Path | None:
        root = Path.home() / ".codex" / "sessions"
        if not root.exists():
            return None
        candidates = sorted(root.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in candidates[:100]:
            try:
                if path.stat().st_mtime < since_epoch:
                    continue
                with path.open("r", encoding="utf-8") as fh:
                    line = fh.readline().strip()
                if not line:
                    continue
                obj = json.loads(line)
            except Exception:
                continue
            payload = obj.get("payload", {}) if isinstance(obj, dict) else {}
            if not isinstance(payload, dict):
                continue
            if str(payload.get("cwd") or "") == cwd:
                session_id = str(payload.get("id") or "")
                if session_id:
                    self._codex_file_cache[session_id] = path
                return path
        return None

    def _session_id_from_codex_file(self, path: Path) -> str | None:
        try:
            with path.open("r", encoding="utf-8") as fh:
                line = fh.readline().strip()
            obj = json.loads(line)
        except Exception:
            return None
        payload = obj.get("payload", {}) if isinstance(obj, dict) else {}
        if not isinstance(payload, dict):
            return None
        session_id = payload.get("id")
        return str(session_id) if session_id else None

    def _jsonl_last_timestamp(self, path: Path) -> datetime | None:
        try:
            tail = deque(path.open("r", encoding="utf-8"), maxlen=300)
        except Exception:
            return None
        latest: datetime | None = None
        for raw in tail:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            ts = _parse_iso_ts(obj.get("timestamp"))
            if ts is None:
                continue
            if latest is None or ts > latest:
                latest = ts
        return latest

    def _extract_codex_assistant_text(self, path: Path, since: datetime | None, prompt: str) -> str | None:
        normalized_prompt = _norm_text(prompt)
        seen_prompt = False
        try:
            fh = path.open("r", encoding="utf-8")
        except Exception:
            return None
        with fh:
            for raw in fh:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                ts = _parse_iso_ts(obj.get("timestamp"))
                if since is not None and ts is not None and ts <= since:
                    continue
                if obj.get("type") == "event_msg":
                    payload = obj.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    event_type = str(payload.get("type", "")).lower()
                    if event_type == "user_message":
                        maybe_text = payload.get("message")
                        if isinstance(maybe_text, str) and _norm_text(maybe_text) == normalized_prompt:
                            seen_prompt = True
                        continue
                    if event_type == "task_complete" and seen_prompt:
                        final = payload.get("last_agent_message")
                        if isinstance(final, str) and final.strip():
                            return final.strip()
                    continue

                if obj.get("type") != "response_item":
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") != "message":
                    continue
                phase = str(payload.get("phase", "")).lower()
                if phase == "commentary":
                    continue
                role = str(payload.get("role", "")).lower()
                if role == "user":
                    maybe_text = _extract_payload_text(payload)
                    if maybe_text and _norm_text(maybe_text) == normalized_prompt:
                        seen_prompt = True
                    continue
                if role != "assistant":
                    continue
                if not seen_prompt:
                    continue
                content = payload.get("content")
                if not isinstance(content, list):
                    continue
                text_blocks: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    txt = block.get("text")
                    if isinstance(txt, str) and txt.strip():
                        text_blocks.append(txt.strip())
                if text_blocks:
                    return "\n".join(text_blocks)
        return None

    def _wait_for_codex_assistant_text(
        self, path: Path, since: datetime | None, prompt: str, timeout_seconds: int
    ) -> str | None:
        deadline = time.monotonic() + max(1, timeout_seconds)
        last_seen_size = -1
        while time.monotonic() < deadline:
            extracted = self._extract_codex_assistant_text(path, since, prompt)
            if extracted:
                return extracted
            try:
                size = path.stat().st_size
            except Exception:
                size = -1
            # Poll faster only when file is moving.
            if size != last_seen_size:
                last_seen_size = size
                time.sleep(0.2)
            else:
                time.sleep(0.35)
        return None

    def _collect_codex_turn_window(
        self, codex_file: Path, start_offset: int | None, prompt: str, timeout_seconds: float
    ) -> dict[str, object] | None:
        """
        Stream new jsonl events and bind the completed answer to the turn that
        corresponds to the matching user prompt, rather than blindly taking the
        first task after the file offset.
        """
        if start_offset is None:
            return None
        deadline = time.monotonic() + max(0, timeout_seconds)
        turn_id: str | None = None
        prompt_norm = _norm_text(prompt)
        saw_matching_user = False
        last_started_turn_id: str | None = None
        agent_msgs: list[str] = []
        tool_calls: list[str] = []
        reasoning_events: list[str] = []
        subagent_events: list[str] = []
        last_pos = start_offset

        while time.monotonic() < deadline:
            new_events, last_pos = _read_jsonl_from_offset(codex_file, last_pos)
            if not new_events:
                time.sleep(0.2)
                continue

            for obj in new_events:
                t = obj.get("type")
                payload = obj.get("payload", {})
                if not isinstance(payload, dict):
                    payload = {}

                if t == "event_msg" and payload.get("type") == "task_started":
                    tid = payload.get("turn_id")
                    last_started_turn_id = str(tid) if tid else None
                    if saw_matching_user and turn_id is None:
                        turn_id = last_started_turn_id
                    continue

                if t == "event_msg" and payload.get("type") == "user_message":
                    user_text = payload.get("message")
                    if isinstance(user_text, str) and _norm_text(user_text) == prompt_norm:
                        saw_matching_user = True
                        if turn_id is None:
                            turn_id = last_started_turn_id
                    continue

                if turn_id is None:
                    continue

                if t == "event_msg" and payload.get("type") == "agent_message":
                    msg = payload.get("message")
                    if isinstance(msg, str) and msg.strip():
                        phase = str(payload.get("phase") or "").lower()
                        agent_msgs.append(_summarize_progress_message(msg, phase))
                    continue

                if t == "response_item" and payload.get("type") == "reasoning":
                    reasoning_events.append("thinking")
                    continue

                if t == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
                    name = payload.get("name") or payload.get("tool_name") or "tool_call"
                    if isinstance(name, str):
                        item = _format_tool_call(name, payload)
                        if _is_subagent_tool(name):
                            subagent_events.append(item)
                        else:
                            tool_calls.append(item)
                    continue

                if t == "event_msg" and payload.get("type") == "task_complete":
                    done_turn = payload.get("turn_id")
                    if turn_id is not None and str(done_turn) != turn_id:
                        continue
                    final = payload.get("last_agent_message")
                    final_text = final.strip() if isinstance(final, str) else ""
                    if not final_text:
                        return None
                    return {
                        "final_text": final_text,
                        "progress_messages": agent_msgs,
                        "tool_calls": _unique_preserve_order(tool_calls),
                        "reasoning_events": _unique_preserve_order(reasoning_events),
                        "subagent_events": _unique_preserve_order(subagent_events),
                        "prompt_matched": saw_matching_user,
                    }
        return None

    def _find_copilot_session_file(self, session: LocalSession) -> Path | None:
        """Find the events.jsonl file for a Copilot session."""
        session_id = session.session_id
        if session_id.startswith("pending:"):
            return None
        cached = self._copilot_file_cache.get(session_id)
        if cached and cached.exists():
            return cached
        root = Path.home() / ".copilot" / "session-state"
        if not root.exists():
            return None
        session_dir = root / session_id
        events_file = session_dir / "events.jsonl"
        if events_file.exists():
            self._copilot_file_cache[session_id] = events_file
            return events_file
        return None

    def _collect_copilot_turn_window(
        self, copilot_file: Path, start_offset: int | None, prompt: str, timeout_seconds: float
    ) -> dict[str, object] | None:
        """
        Stream new JSONL events from Copilot's events.jsonl and extract the assistant's
        final message that responds to the user prompt.
        """
        if start_offset is None:
            return None
        deadline = time.monotonic() + max(0, timeout_seconds)
        prompt_norm = _norm_text(prompt)
        saw_matching_user = False
        final_text = ""
        last_pos = start_offset

        while time.monotonic() < deadline:
            new_events, last_pos = _read_jsonl_from_offset(copilot_file, last_pos)
            if not new_events:
                time.sleep(0.2)
                continue

            for obj in new_events:
                t = obj.get("type")
                data = obj.get("data", {})
                if not isinstance(data, dict):
                    data = {}

                # Track user messages to ensure we're responding to the right one
                if t == "user.message":
                    content = data.get("content")
                    if isinstance(content, str) and _norm_text(content) == prompt_norm:
                        saw_matching_user = True
                    continue

                # Collect assistant messages after we've seen the matching user message
                if saw_matching_user and t == "assistant.message":
                    content = data.get("content")
                    if isinstance(content, str) and content.strip():
                        final_text = content.strip()
                    continue

                # Copilot signals turn end - return the collected message
                if saw_matching_user and t == "assistant.turn_end":
                    if final_text:
                        return {
                            "final_text": final_text,
                            "detected_session_id": None,
                        }
                    # No text found for this turn
                    return None

        # Timeout - return whatever we collected
        if saw_matching_user and final_text:
            return {
                "final_text": final_text,
                "detected_session_id": None,
            }
        return None



def _trim_output(text: str, max_chars: int = 3000) -> str:
    clean = _sanitize_terminal_text(text)
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 20] + "\n...[truncated]"


def _sanitize_terminal_text(text: str) -> str:
    ansi_escape: Final[re.Pattern[str]] = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
    osc_escape: Final[re.Pattern[str]] = re.compile(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)")
    stray_osc: Final[re.Pattern[str]] = re.compile(r"\]\d{1,2};[^\r\n]*")
    control_chars: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")

    no_osc = osc_escape.sub("", text)
    no_ansi = ansi_escape.sub("", no_osc)
    no_stray = stray_osc.sub("", no_ansi)
    no_ctrl = control_chars.sub("", no_stray)

    filtered_lines: list[str] = []
    for raw_line in no_ctrl.splitlines():
        line = raw_line.rstrip()
        if not line:
            filtered_lines.append("")
            continue
        if _looks_like_terminal_garbage(line):
            continue
        # Drop Codex TUI chrome and prompt rendering noise.
        if line.startswith(("╭", "╰", "│")):
            continue
        if line.startswith("Tip: "):
            continue
        if line.startswith(">_ OpenAI Codex"):
            continue
        if "gpt-5" in line and "· ~/" in line:
            continue
        if "Booting MCP server" in line:
            continue
        if set(line) <= {"─"} and len(line) >= 20:
            # Visual separators from terminal UI; keep one logical break.
            filtered_lines.append("")
            continue
        filtered_lines.append(line)

    clean = _unwrap_hard_wrapped_lines(filtered_lines).strip()
    return clean


def _detect_session_id(text: str) -> str | None:
    patterns = (
        re.compile(r"(?im)\bsession[_\s-]?id\s*[:=]\s*([A-Za-z0-9._:-]{6,})"),
        re.compile(r"(?im)\bresume\s+([A-Za-z0-9._:-]{6,})"),
        re.compile(r"(?im)\bid\s+([A-Za-z0-9._:-]{8,})"),
    )
    for pat in patterns:
        m = pat.search(text or "")
        if m:
            return m.group(1).strip()
    return None


def _parse_iso_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_payload_text(payload: dict) -> str | None:
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    content = payload.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t.strip())
        if parts:
            return "\n".join(parts)
    if isinstance(content, str) and content.strip():
        return content.strip()
    return None


def _norm_text(value: str) -> str:
    return " ".join(value.lower().split())


def _read_jsonl_from_offset(path: Path, offset: int) -> tuple[list[dict], int]:
    events: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            fh.seek(offset)
            data = fh.read()
            new_pos = fh.tell()
    except Exception:
        return [], offset
    if not data:
        return [], offset
    for raw in data.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events, new_pos


def _parse_codex_exec_output(text: str, prompt: str) -> CodexExecResult | None:
    events: list[dict] = []
    non_json_lines: list[str] = []
    detected_session_id: str | None = None
    final_text: str | None = None
    assistant_messages: list[str] = []
    progress: list[str] = []
    tools: list[str] = []
    reasoning: list[str] = []
    subagents: list[str] = []
    prompt_norm = _norm_text(prompt)
    prompt_matched = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            non_json_lines.append(raw)
            continue
        if not isinstance(obj, dict):
            continue
        events.append(obj)

    for obj in events:
        t = obj.get("type")
        payload = obj.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        if t == "thread.started":
            thread_id = obj.get("thread_id")
            if thread_id:
                detected_session_id = str(thread_id)
            continue

        if t in {"item.started", "item.completed"}:
            item = obj.get("item")
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "agent_message":
                text_value = item.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    final_text = text_value.strip()
                continue
            if item_type == "command_execution":
                if t == "item.completed":
                    command = item.get("command")
                    if isinstance(command, str) and command.strip():
                        tools.append(_format_command_execution(command, item.get("exit_code")))
                continue
            if item_type == "reasoning":
                reasoning.append("thinking")
                continue

        if t == "turn.completed":
            usage = obj.get("usage")
            if isinstance(usage, dict) and int(usage.get("reasoning_output_tokens") or 0) > 0:
                reasoning.append("thinking")
            continue

        if t == "session_meta":
            session_id = payload.get("id")
            if session_id:
                detected_session_id = str(session_id)
            continue

        event_type = payload.get("type")
        if t == "event_msg" and event_type == "user_message":
            msg = payload.get("message")
            if isinstance(msg, str) and _norm_text(msg) == prompt_norm:
                prompt_matched = True
            continue

        if t == "event_msg" and event_type == "agent_message":
            msg = payload.get("message")
            if isinstance(msg, str) and msg.strip():
                progress.append(_summarize_progress_message(msg, str(payload.get("phase") or "").lower()))
            continue

        if t == "event_msg" and event_type == "task_complete":
            final = payload.get("last_agent_message")
            if isinstance(final, str) and final.strip():
                final_text = final.strip()
            continue

        if t != "response_item":
            continue

        item_type = payload.get("type")
        if item_type == "reasoning":
            reasoning.append("thinking")
            continue

        if item_type in {"function_call", "custom_tool_call"}:
            name = payload.get("name") or payload.get("tool_name") or "tool_call"
            if isinstance(name, str):
                item = _format_tool_call(name, payload)
                if _is_subagent_tool(name):
                    subagents.append(item)
                else:
                    tools.append(item)
            continue

        if item_type == "message":
            role = str(payload.get("role") or "").lower()
            phase = str(payload.get("phase") or "").lower()
            if role == "user":
                user_text = _extract_payload_text(payload)
                if user_text and _norm_text(user_text) == prompt_norm:
                    prompt_matched = True
                continue
            if role != "assistant":
                continue
            text_value = _extract_payload_text(payload)
            if not text_value:
                continue
            if phase == "commentary":
                progress.append(_summarize_progress_message(text_value, phase))
            else:
                assistant_messages.append(text_value.strip())

    if not final_text and assistant_messages:
        final_text = assistant_messages[-1]
    if not final_text and non_json_lines:
        terminal = _parse_codex_terminal_turn("\n".join(non_json_lines), prompt)
        final_text = terminal.final_text
        progress.extend(terminal.progress_messages or [])
        tools.extend(terminal.tool_calls or [])
        reasoning.extend(terminal.reasoning_events or [])
        subagents.extend(terminal.subagent_events or [])
        prompt_matched = prompt_matched or terminal.prompt_matched

    if not events and not non_json_lines:
        return None
    return CodexExecResult(
        final_text=final_text.strip() if isinstance(final_text, str) and final_text.strip() else None,
        detected_session_id=detected_session_id,
        progress_messages=_unique_preserve_order(progress),
        tool_calls=_unique_preserve_order(tools),
        reasoning_events=_unique_preserve_order(reasoning),
        subagent_events=_unique_preserve_order(subagents),
        prompt_matched=prompt_matched,
    )


def _runner_result_from_codex_exec(parsed: CodexExecResult, session_id: str | None, ok: bool) -> RunnerResult:
    return RunnerResult(
        ok=ok,
        output=parsed.final_text or "No output",
        changed_files=[],
        detected_session_id=parsed.detected_session_id or session_id,
        progress_messages=parsed.progress_messages,
        tool_calls=parsed.tool_calls,
        reasoning_events=parsed.reasoning_events,
        subagent_events=parsed.subagent_events,
        prompt_matched=parsed.prompt_matched,
    )


def _unique_preserve_order(items: list[str]) -> list[str]:
    unique: list[str] = []
    for item in items:
        if item not in unique:
            unique.append(item)
    return unique


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _summarize_progress_message(message: str, phase: str) -> str:
    heading = "Progress"
    if phase == "commentary":
        heading = "Commentary"
    first_line = " ".join(message.strip().splitlines()[0].split())
    if len(first_line) > 160:
        first_line = first_line[:157].rstrip() + "..."
    return f"{heading}: {first_line}" if first_line else heading


def _format_tool_call(name: str, payload: dict) -> str:
    target = _tool_target(payload)
    return f"{name}: {target}" if target else name


def _format_command_execution(command: str, exit_code: object) -> str:
    target = _command_target(command)
    status = ""
    if isinstance(exit_code, int):
        status = " ok" if exit_code == 0 else f" exit {exit_code}"
    return f"command: {target}{status}" if target else f"command{status}"


def _tool_target(payload: dict) -> str | None:
    arguments = payload.get("arguments")
    data: object = arguments
    if isinstance(arguments, str):
        try:
            data = json.loads(arguments)
        except Exception:
            data = arguments
    if isinstance(data, dict):
        for key in ("path", "file", "workdir", "cwd"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        cmd = data.get("cmd") or data.get("command")
        if isinstance(cmd, str):
            return _command_target(cmd)
        items = data.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                value = item.get("path") or item.get("name")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    if isinstance(data, str):
        return _command_target(data)
    return None


def _command_target(command: str) -> str | None:
    tokens = command.strip().split()
    for token in reversed(tokens):
        clean = token.strip("'\"")
        if "/" in clean or "." in Path(clean).name:
            return clean
    return None


def _is_subagent_tool(name: str) -> bool:
    normalized = name.lower()
    return normalized in {"spawn_agent", "send_input", "wait_agent", "close_agent", "resume_agent"}


def _classify_terminal_activity(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped:
        return None
    normalized = stripped.lstrip("•*-✓✔⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ ").strip()
    lowered = normalized.lower()

    if lowered.startswith(("thinking", "reasoning")) or lowered in {"working", "processing"}:
        return "reasoning", "thinking"

    subagent_markers = (
        "spawn_agent",
        "subagent",
        "sub-agent",
        "sub agent",
        "worker agent",
        "explorer agent",
        "waiting for agent",
    )
    if any(marker in lowered for marker in subagent_markers):
        target = _first_path_like_token(normalized)
        return "subagent", f"subagent: {target}" if target else "subagent"

    tool_match = re.search(
        r"\b(exec_command|apply_patch|view_image|update_plan|write_stdin|read_mcp_resource|list_mcp_resources)\b",
        normalized,
    )
    if tool_match:
        target = _first_path_like_token(normalized)
        name = tool_match.group(1)
        return "tool", f"{name}: {target}" if target else name

    tool_prefixes = (
        "running ",
        "reading ",
        "searching ",
        "opening ",
        "listing ",
        "edited ",
        "editing ",
        "created ",
        "updated ",
        "deleted ",
        "patched ",
        "tool call",
        "function call",
    )
    if lowered.startswith(tool_prefixes):
        target = _first_path_like_token(normalized)
        heading = normalized.split(":", 1)[0][:80].strip()
        return "tool", f"{heading}: {target}" if target else heading

    progress_prefixes = ("inspecting ", "checking ", "looking at ", "i'll ", "i’m ", "i'm ")
    if lowered.startswith(progress_prefixes):
        return "progress", _summarize_progress_message(normalized, "")

    return None


def _first_path_like_token(text: str) -> str | None:
    for match in re.finditer(r"(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.@:+-]+|[A-Za-z0-9_.-]+\.[A-Za-z0-9_+-]+)", text):
        return match.group("path").strip("`'\".,:;)")
    return None


def _filter_terminal_answer_lines(lines: list[str]) -> tuple[str, list[str], list[str], list[str], list[str]]:
    answer_lines: list[str] = []
    progress: list[str] = []
    tools: list[str] = []
    reasoning: list[str] = []
    subagents: list[str] = []

    for line in lines:
        classified = _classify_terminal_activity(line)
        if classified is None:
            answer_lines.append(line)
            continue
        kind, item = classified
        if kind == "reasoning":
            reasoning.append(item)
        elif kind == "subagent":
            subagents.append(item)
        elif kind == "tool":
            tools.append(item)
        elif kind == "progress":
            progress.append(item)

    answer = "\n".join(answer_lines).strip()
    return (
        answer,
        _unique_preserve_order(progress),
        _unique_preserve_order(tools),
        _unique_preserve_order(reasoning),
        _unique_preserve_order(subagents),
    )


def _looks_like_terminal_garbage(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 6:
        return False
    if " " in stripped:
        return False
    return len(set(stripped)) == 1


def _unwrap_hard_wrapped_lines(lines: list[str]) -> str:
    """
    Merge terminal hard-wrap continuation lines while preserving semantic breaks.
    """
    out: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            out.append(current)
            current = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            if not out or out[-1] != "":
                out.append("")
            continue

        is_bullet = stripped.startswith(("- ", "* ", "• ", "1. ", "2. ", "3. ", "/"))
        is_codeish = line.startswith(("    ", "\t")) or stripped.startswith(("```", "{", "}", "[", "]"))
        continuation = line.startswith("  ") and not is_bullet and not is_codeish

        if current == "":
            current = stripped
            continue

        if continuation:
            current = f"{current} {stripped}"
        elif is_bullet or is_codeish:
            flush()
            current = line.rstrip()
        else:
            # Keep distinct statements separated unless it looks wrapped.
            if current.endswith(("-", "(", "/", ",")):
                current = f"{current} {stripped}"
            else:
                flush()
                current = stripped

    flush()
    # Trim leading/trailing empty lines and collapse 3+ empties to one.
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    normalized: list[str] = []
    empty_run = 0
    for item in out:
        if item == "":
            empty_run += 1
            if empty_run <= 1:
                normalized.append(item)
            continue
        empty_run = 0
        normalized.append(item)
    return "\n".join(normalized)


def _extract_codex_terminal_reply(text: str, prompt: str) -> str | None:
    return _parse_codex_terminal_turn(text, prompt).final_text


def _parse_codex_terminal_turn(text: str, prompt: str) -> CodexTerminalTurn:
    clean = _sanitize_terminal_text(text)
    if not clean:
        return CodexTerminalTurn(prompt_matched=False, completed=False, final_text=None)

    prompt_norm = _norm_text(prompt)
    lines = clean.splitlines()
    assistant_blocks: list[str] = []
    current_role: str | None = None
    current_lines: list[str] = []
    prompt_matched = False
    completed = False
    progress_messages: list[str] = []
    tool_calls: list[str] = []
    reasoning_events: list[str] = []
    subagent_events: list[str] = []

    def flush() -> None:
        nonlocal current_lines, current_role
        if current_role == "assistant" and prompt_matched:
            block, progress, tools, reasoning, subagents = _filter_terminal_answer_lines(current_lines)
            progress_messages.extend(progress)
            tool_calls.extend(tools)
            reasoning_events.extend(reasoning)
            subagent_events.extend(subagents)
            if block:
                assistant_blocks.append(block)
        current_role = None
        current_lines = []

    for line in lines:
        assistant_match = ASSISTANT_PROMPT_LINE.match(line)
        if assistant_match:
            flush()
            current_role = "assistant"
            inline = assistant_match.group(1).strip()
            if prompt_matched and inline:
                current_lines.append(inline)
            continue

        user_match = USER_PROMPT_LINE.match(line)
        codex_prompt_match = CODEX_PROMPT_LINE.match(line)
        if user_match or codex_prompt_match:
            flush()
            inline = (user_match.group(1) if user_match else codex_prompt_match.group(1)).strip()
            if inline and _norm_text(inline) == prompt_norm:
                prompt_matched = True
                completed = False
                assistant_blocks = []
                current_lines = []
            elif prompt_matched and assistant_blocks:
                completed = True
            current_role = "user"
            continue

        if BARE_PROMPT_LINE.match(line):
            if prompt_matched and assistant_blocks:
                completed = True
            flush()
            continue

        if current_role == "assistant" and prompt_matched:
            current_lines.append(line)
            continue

        if prompt_matched and current_role in {None, "user"}:
            current_role = "assistant"
            current_lines.append(line)

    flush()

    final_text = assistant_blocks[-1] if assistant_blocks else None
    if final_text:
        return CodexTerminalTurn(
            prompt_matched=prompt_matched,
            completed=completed,
            final_text=final_text,
            progress_messages=_unique_preserve_order(progress_messages),
            tool_calls=_unique_preserve_order(tool_calls),
            reasoning_events=_unique_preserve_order(reasoning_events),
            subagent_events=_unique_preserve_order(subagent_events),
        )

    if not prompt_matched:
        return CodexTerminalTurn(prompt_matched=False, completed=completed, final_text=None)

    fallback_text = remove_prompt_echo(clean, prompt).strip()
    fallback, progress, tools, reasoning, subagents = _filter_terminal_answer_lines(fallback_text.splitlines())
    return CodexTerminalTurn(
        prompt_matched=prompt_matched,
        completed=completed,
        final_text=fallback or None,
        progress_messages=_unique_preserve_order([*progress_messages, *progress]),
        tool_calls=_unique_preserve_order([*tool_calls, *tools]),
        reasoning_events=_unique_preserve_order([*reasoning_events, *reasoning]),
        subagent_events=_unique_preserve_order([*subagent_events, *subagents]),
    )
