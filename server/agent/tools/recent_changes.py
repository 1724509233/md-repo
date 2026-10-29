"""recent_changes tool."""
from __future__ import annotations

import time

from pydantic import BaseModel, Field, field_validator

from .tool import Tool, ToolContext, _check_optional_path


class RecentChangesArgs(BaseModel):
    """Query the vault change log. Shows who modified what and when (agent/web/watchdog/mcp). All params optional."""
    path: str | None = Field(default=None, description="Filter by document path")
    since: str | None = Field(default=None, description='Time filter: "5m", "1h", "2d", or ISO timestamp')
    source: str | None = Field(default=None, description="Filter by source: agent/web/watchdog/mcp")
    limit: int = Field(default=20, description="Max results")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_optional_path(v)


class RecentChangesTool(Tool):
    name = "recent_changes"
    args_model = RecentChangesArgs

    @staticmethod
    def changes_empty() -> str:
        return 'No recent changes found. Tip: check the path/source filters, or widen since (e.g. since="7d").'

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...vault.db.changes import get_changes
        from ...vault.db.conn import get_conn
        from ..validate import parse_since

        conn = get_conn()
        if not conn:
            return "Error: database not available"
        since_raw = args.since
        if isinstance(since_raw, str) and since_raw.strip().lower() == "none":
            since_raw = None
        since_ts = parse_since(since_raw) if since_raw else 0.0
        path_raw = args.path
        if isinstance(path_raw, str) and (path_raw.strip().lower() == "none" or path_raw.strip() == ""):
            path_raw = None
        source_raw = args.source
        if isinstance(source_raw, str) and (source_raw.strip().lower() == "none" or source_raw.strip() == ""):
            source_raw = None
        changes = get_changes(
            conn,
            since=since_ts,
            path=path_raw,
            source=source_raw,
            limit=args.limit,
        )
        if not changes:
            return self.changes_empty()
        lines = []
        for c in changes:
            ts_str = time.strftime("%H:%M", time.localtime(c["ts"]))
            path_str = c["path"]
            if c.get("from_path"):
                path_str += f" (was: {c['from_path']})"
            lines.append(f"{ts_str} {c['source']:<10} {c['action']:<8} {path_str}")
        return "\n".join(lines)
