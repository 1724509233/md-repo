"""Client sync logic: local scan, one sync cycle, polling loop."""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime
from pathlib import Path

import httpx

from .config import ClientConfig
from .state import load_state, save_state


def _iso_to_ts(iso_str: str) -> float:
    """Convert an ISO 8601 string to a Unix timestamp. Returns 0.0 on failure."""
    if not iso_str:
        return 0.0
    try:
        return datetime.fromisoformat(iso_str).timestamp()
    except (ValueError, TypeError):
        return 0.0

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_stamp() -> str:
    return datetime.now().strftime(DATE_FORMAT)


class _LogCollector(logging.Handler):
    """Collects log records as {level, message, timestamp} dicts."""

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.entries.append(
            {
                "level": record.levelname,
                "message": record.getMessage(),
                "timestamp": datetime.fromtimestamp(record.created).strftime(
                    DATE_FORMAT
                ),
            }
        )


def _setup_logging(log_path: Path) -> logging.Logger:
    """Configure the root logger to write to sync.log and stdout."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.handlers = [file_handler, stream_handler]
    return logger


def scan_local_files(vault_dir: Path) -> dict[str, str]:
    """Walk vault_dir for .md files (skipping .sync/) and hash them.

    Returns {relative_path_with_forward_slashes: sha256_hash}.
    """
    result: dict[str, str] = {}
    if not vault_dir.is_dir():
        return result
    for f in vault_dir.rglob("*.md"):
        if ".sync" in f.parts:
            continue
        try:
            content = f.read_bytes()
        except OSError:
            continue
        rel = f.relative_to(vault_dir).as_posix()
        result[rel] = _hash_bytes(content)
    return result


def sync_once(config: ClientConfig, state: dict) -> dict:
    """Perform one sync cycle against the server.

    Returns the updated state dict. On 409 (server busy) returns the
    state unchanged. Network errors propagate to the caller.
    """
    logger = logging.getLogger()
    collector = _LogCollector()
    logger.addHandler(collector)

    try:
        vault_dir = config.vault_dir
        local_files = scan_local_files(vault_dir)
        last_sync_ts = _iso_to_ts(state.get("last_sync", ""))

        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{config.server_url}/api/sync",
                json={
                    "device_name": config.device_name,
                    "local_files": local_files,
                    "logs": collector.entries,
                    "last_sync": last_sync_ts,
                },
            )

            if resp.status_code == 409:
                logger.info("sync in progress, retry next poll")
                return state

            resp.raise_for_status()
            result = resp.json()

        to_pull: list[str] = result.get("to_pull", [])
        to_push: list[str] = result.get("to_push", [])
        to_delete: list[str] = result.get("to_delete", [])
        conflicts: list[str] = result.get("conflicts", [])

        notifications: list[dict] = result.get("notifications", [])
        for n in notifications:
            logger.info(
                "[bg:%s] %s：%s（报告 %s）",
                n.get("alias", "?"),
                "整理完成" if n.get("status") == "ok" else f"运行 {n.get('status')}",
                n.get("summary", ""),
                n.get("report_path", ""),
            )

        with httpx.Client(timeout=60) as client:
            for path in to_pull:
                try:
                    r = client.get(f"{config.server_url}/api/docs/{path}")
                    r.raise_for_status()
                    dest = vault_dir / path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(r.content)
                    logger.info("pulled %s", path)
                except (httpx.HTTPError, OSError) as e:
                    logger.error("failed to pull %s: %s", path, e)

            for path in to_push:
                try:
                    src = vault_dir / path
                    content = src.read_bytes()
                    filename = Path(path).name
                    r = client.post(
                        f"{config.server_url}/api/docs/{path}",
                        files={"file": (filename, content, "text/markdown")},
                    )
                    r.raise_for_status()
                    logger.info("pushed %s", path)
                except (httpx.HTTPError, OSError) as e:
                    logger.error("failed to push %s: %s", path, e)

            for path in conflicts:
                try:
                    r = client.get(f"{config.server_url}/api/docs/{path}")
                    r.raise_for_status()
                    p = Path(path)
                    conflict_path = vault_dir / str(
                        p.with_name(f"{p.stem}.conflict-{config.device_name}{p.suffix}")
                    )
                    conflict_path.parent.mkdir(parents=True, exist_ok=True)
                    conflict_path.write_bytes(r.content)
                    logger.warning(
                        "conflict on %s, saved server version as %s",
                        path,
                        conflict_path.relative_to(vault_dir).as_posix(),
                    )
                except (httpx.HTTPError, OSError) as e:
                    logger.error("failed to save conflict %s: %s", path, e)

        # Delete local files that were renamed on the server (stale pre-rename copies).
        for path in to_delete:
            try:
                fp = vault_dir / path
                if fp.is_file():
                    fp.unlink()
                    logger.info("deleted local %s (renamed on server)", path)
            except OSError as e:
                logger.error("failed to delete %s: %s", path, e)

        new_files = scan_local_files(vault_dir)
        updated_state = {
            "last_sync": _now_iso(),
            "files": new_files,
        }
        logger.info(
            "sync complete: %d pulled, %d pushed, %d deleted, %d conflicts",
            len(to_pull),
            len(to_push),
            len(to_delete),
            len(conflicts),
        )
        return updated_state
    finally:
        logger.removeHandler(collector)


def run_polling_loop(config: ClientConfig) -> None:
    """Infinite polling loop: sync, save state, sleep. Never crashes on errors."""
    vault_dir = config.vault_dir
    sync_dir = vault_dir / ".sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    _setup_logging(sync_dir / "logs" / "sync.log")
    logger = logging.getLogger()

    state_path = sync_dir / "state.json"

    while True:
        try:
            state = load_state(state_path)
            state = sync_once(config, state)
            save_state(state_path, state)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.warning("sync failed, will retry next poll: %s", e)
        time.sleep(config.poll_interval)
