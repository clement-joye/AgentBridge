from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
import platform
import shutil
import stat
import sys

from .config import load_config


def cli() -> None:
    parser = argparse.ArgumentParser(prog="telegram-ai-bridge")
    parser.add_argument("command", choices=["init", "run", "doctor", "install-service"], nargs="?", default="run")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--io-logs",
        action="store_true",
        help="Show only bridge input/output logs plus warnings/errors from dependencies.",
    )
    args = parser.parse_args()

    _setup_logging(args.verbose, args.io_logs)

    if args.command == "init":
        _cmd_init(Path(args.config))
        return
    if args.command == "doctor":
        sys.exit(_cmd_doctor(Path(args.config)))
    if args.command == "install-service":
        sys.exit(_cmd_install_service(Path(args.config)))

    from .telegram_bot import BridgeBot

    cfg = load_config(args.config)
    bot = BridgeBot(cfg, config_path=args.config)
    app = bot.build()
    # Python 3.14+ may not have a default loop in main thread.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    logging.getLogger(__name__).info("Bridge starting with config: %s", args.config)
    app.run_polling(drop_pending_updates=True)


def _cmd_init(config_path: Path) -> None:
    if config_path.exists():
        print(f"Config already exists: {config_path}")
        return
    src = Path(__file__).resolve().parent.parent / "config.example.toml"
    if not src.exists():
        raise FileNotFoundError(f"Missing example config: {src}")
    config_path.write_text(src.read_text())
    print(f"Created config: {config_path}")
    print("Next: edit telegram_bot_token, allowed_user_ids, and allowed_chat_ids.")


def _cmd_doctor(config_path: Path) -> int:
    checks: list[tuple[str, bool, str]] = []
    ok = True

    checks.append(("config file exists", config_path.exists(), str(config_path)))
    if not config_path.exists():
        _print_checks(checks)
        return 1

    try:
        cfg = load_config(config_path)
        checks.append(("config parse", True, "ok"))
    except Exception as exc:
        checks.append(("config parse", False, str(exc)))
        _print_checks(checks)
        return 1

    checks.append(("telegram_bot_token present", bool(cfg.telegram_bot_token.strip()), "set"))
    checks.append(("allowed_user_ids non-empty", bool(cfg.allowed_user_ids), str(len(cfg.allowed_user_ids))))
    checks.append(("allowed_chat_ids non-empty", bool(cfg.allowed_chat_ids), str(len(cfg.allowed_chat_ids))))
    checks.append(("state_dir writable", _is_dir_writable(cfg.state_dir), str(cfg.state_dir)))
    checks.append(("codex CLI installed", shutil.which("codex") is not None, shutil.which("codex") or "missing"))
    checks.append(("copilot CLI installed", shutil.which("copilot") is not None, shutil.which("copilot") or "missing"))
    checks.append(("python pexpect installed", _module_available("pexpect"), "pexpect"))

    session_dir = Path.home() / ".codex" / "sessions"
    checks.append(("codex session dir readable", session_dir.exists() and os_access_read(session_dir), str(session_dir)))
    copilot_session_dir = Path.home() / ".copilot" / "session-state"
    checks.append(
        (
            "copilot session dir readable",
            copilot_session_dir.exists() and os_access_read(copilot_session_dir),
            str(copilot_session_dir),
        )
    )

    roots_valid = all(p.exists() and p.is_dir() for p in cfg.allowed_repo_roots) if cfg.allowed_repo_roots else True
    checks.append(("allowed_repo_roots valid", roots_valid, str(len(cfg.allowed_repo_roots))))
    blocked_valid = all(p.is_absolute() for p in cfg.blocked_paths)
    checks.append(("blocked_paths absolute", blocked_valid, str(len(cfg.blocked_paths))))

    auth_coherent = bool(cfg.allowed_user_ids and cfg.allowed_chat_ids)
    checks.append(("authorization policy coherent", auth_coherent, "allowlists configured"))

    for _, passed, _ in checks:
        ok = ok and passed
    _print_checks(checks)
    return 0 if ok else 1


def _cmd_install_service(config_path: Path) -> int:
    system = platform.system().lower()
    if system == "linux":
        return _install_systemd_user_service(config_path)
    if system == "darwin":
        return _install_launchd_service(config_path)
    print(f"Unsupported OS for service install: {platform.system()}")
    return 1


def _install_systemd_user_service(config_path: Path) -> int:
    target_dir = Path.home() / ".config" / "systemd" / "user"
    target_dir.mkdir(parents=True, exist_ok=True)
    unit_path = target_dir / "telegram-ai-bridge.service"
    path_value = os.environ.get("PATH", "")
    unit = f"""[Unit]
Description=Telegram AI Bridge
After=network-online.target

[Service]
Type=simple
WorkingDirectory={Path.cwd()}
ExecStart={shutil.which('telegram-ai-bridge') or 'telegram-ai-bridge'} run --config {config_path}
Environment=PATH={path_value}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""
    unit_path.write_text(unit)
    print(f"Installed systemd user service: {unit_path}")
    print("Run:")
    print("  systemctl --user daemon-reload")
    print("  systemctl --user enable --now telegram-ai-bridge.service")
    return 0


def _install_launchd_service(config_path: Path) -> int:
    target_dir = Path.home() / "Library" / "LaunchAgents"
    target_dir.mkdir(parents=True, exist_ok=True)
    plist_path = target_dir / "com.telegram-ai-bridge.plist"
    exec_bin = shutil.which("telegram-ai-bridge") or "telegram-ai-bridge"
    path_value = os.environ.get("PATH", "")
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.telegram-ai-bridge</string>
    <key>ProgramArguments</key>
    <array>
      <string>{exec_bin}</string>
      <string>run</string>
      <string>--config</string>
      <string>{config_path}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{Path.cwd()}</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key>
      <string>{path_value}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
  </dict>
</plist>
"""
    plist_path.write_text(plist)
    print(f"Installed launchd plist: {plist_path}")
    print("Run:")
    print(f"  launchctl load -w {plist_path}")
    return 0


def _is_dir_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test = path / ".write-test"
        test.write_text("ok")
        test.unlink()
        return True
    except Exception:
        return False


def os_access_read(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
        return bool(mode & stat.S_IRUSR)
    except Exception:
        return False


def _print_checks(checks: list[tuple[str, bool, str]]) -> None:
    for name, passed, detail in checks:
        status = "OK" if passed else "FAIL"
        print(f"[{status}] {name}: {detail}")


def _setup_logging(verbose: bool, io_logs: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    if io_logs:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if io_logs:
        logging.getLogger("telegram_ai_bridge").setLevel(logging.WARNING)
        logging.getLogger("telegram_ai_bridge.io").setLevel(logging.INFO)
        logging.getLogger("telegram_ai_bridge.telegram_bot").setLevel(logging.WARNING)
        logging.getLogger("telegram_ai_bridge.sessions.runner").setLevel(logging.WARNING)
        for noisy in ("telegram", "telegram.ext", "httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    cli()
