"""Tool execution: dispatch a validated tool call to its implementation."""
from __future__ import annotations

import time
from pathlib import Path

from .. import responses
from .validate import parse_since


def execute_tool(name: str, args: dict, vault_dir: Path, db_conn, read_doc_max_chars: int = 4000) -> str:
    """Execute a tool call, return result string for LLM."""
    if name == "read_doc":
        fp = vault_dir / args["path"]
        if not fp.is_file():
            return responses._file_not_found_hint(args["path"], db_conn, tool="read_doc")
        content = fp.read_bytes().decode("utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        total = len(lines)
        offset = max(int(args.get("offset", 1)), 1)
        limit = args.get("limit")
        if limit is not None:
            limit = int(limit)
        if offset > total:
            return responses.read_offset_exceeds(offset, total)
        if limit is not None:
            selected = lines[offset - 1: offset - 1 + limit]
        else:
            selected = lines[offset - 1:]

        # Truncate by char budget (tail truncation, no partial lines)
        MAX_CHARS = read_doc_max_chars
        char_count = 0
        cut = len(selected)
        truncated = False
        for i, line in enumerate(selected):
            char_count += len(line)
            if char_count > MAX_CHARS:
                cut = i
                truncated = True
                break

        shown_lines = selected[:cut]
        text = "".join(shown_lines)
        end_line = offset + len(shown_lines) - 1

        if not truncated and offset == 1 and len(selected) == total:
            return f"File: {args['path']} ({total} lines, {len(content)} chars)\n<content>\n{text}</content>"

        header = f"File: {args['path']} (lines {offset}-{end_line} of {total})"
        if truncated:
            remaining = len(selected) - cut
            next_offset = offset + cut
            header += f"\n[truncated — {remaining} more lines, use read_doc with offset={next_offset} to continue]"
        return f"{header}\n<content>\n{text}</content>"

    elif name == "search_docs":
        import html as _html
        from ..ai import rag_search

        expand = args.get("expand", True)
        try:
            results = rag_search(str(vault_dir), args["query"], top_k=args.get("top_k", 5), expand=expand)
        except Exception as e:
            if "syntax error" in str(e).lower() or "fts5" in str(e).lower():
                return responses.fts_syntax_error(args["query"])
            raise
        if not results:
            return responses.search_no_match(args["query"])
        lines = [f'<results count="{len(results)}" query="{_html.escape(args["query"], quote=False)}">']
        for r in results:
            lines.append(f'  <result path="{_html.escape(r["path"], quote=False)}" score="{r["score"]:.2f}">')
            for seg in r["segments"]:
                heading = _html.escape(seg["heading_path"], quote=False) if seg["heading_path"] else ""
                kind = seg["kind"]
                if expand and seg["content"]:
                    content = _html.escape(seg["content"].replace("\n", " "), quote=False)
                    lines.append(f'    <segment heading="{heading}" kind="{kind}">{content}</segment>')
                else:
                    lines.append(f'    <segment heading="{heading}" kind="{kind}"/>')
            lines.append('  </result>')
        lines.append('</results>')
        return "\n".join(lines)

    elif name == "list_files":
        base = vault_dir / args.get("dir", "")
        files = [
            f.relative_to(vault_dir).as_posix()
            for f in base.rglob("*.md") if ".sync" not in f.parts
        ]
        return "\n".join(sorted(files)) if files else responses.list_empty(args.get("dir", ""))

    elif name == "write_doc":
        fp = vault_dir / args["path"]
        existed = fp.is_file()
        fp.parent.mkdir(parents=True, exist_ok=True)
        from ..write_registry import register_write

        register_write(args["path"], "agent")
        fp.write_bytes(args["content"].encode("utf-8"))
        if existed:
            return f"Saved: {args['path']} ({len(args['content'])} chars, overwritten)"
        return f"Saved: {args['path']} ({len(args['content'])} chars)"

    elif name == "delete_doc":
        fp = vault_dir / args["path"]
        if not fp.is_file():
            return responses._file_not_found_hint(args["path"], db_conn, tool="delete_doc")
        from ..write_registry import register_write
        from ..vault.file_ops import apply_change

        register_write(args["path"], "agent")
        fp.unlink()
        if db_conn:
            apply_change(db_conn, vault_dir, args["path"], "deleted", source="agent")
        return f"Deleted: {args['path']}"

    elif name == "move_docs":
        from ..vault.file_ops import _update_references_batch
        from ..write_registry import register_write

        moves = [(m["from_path"], m["to_path"]) for m in args["moves"]]

        # Phase 1: validate all sources exist, no destination conflicts
        for from_path, to_path in moves:
            src = vault_dir / from_path
            if not src.is_file():
                return responses._file_not_found_hint(from_path, db_conn, tool="move_docs")
            dst = vault_dir / to_path
            if dst.is_file():
                return responses.move_dest_exists(to_path)

        # Phase 2: single-pass reference update (before rename, so uniqueness checks work)
        ref_count = _update_references_batch(db_conn, vault_dir, moves, pre_rename=True)

        # Phase 3: perform all filesystem renames
        # DB update (files, change_log, FTS, graph) is handled by the snapshot
        # watcher via _on_moved → apply_change. We only register the write so
        # the watcher attributes the change to "agent" instead of "watchdog".
        for from_path, to_path in moves:
            src = vault_dir / from_path
            dst = vault_dir / to_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            register_write(to_path, "agent")
            src.rename(dst)

        lines = [f"Moved {len(moves)} file(s):"]
        for from_path, to_path in moves:
            lines.append(f"  {from_path} → {to_path}")
        if ref_count:
            lines.append(f"Updated references in {ref_count} file(s)")
        return "\n".join(lines)

    elif name == "edit_doc":
        from ..vault.file_ops import edit_file
        from ..write_registry import register_write

        fp = vault_dir / args["path"]
        if not fp.is_file():
            return responses._file_not_found_hint(args["path"], db_conn, tool="edit_doc")

        register_write(args["path"], "agent")
        result = edit_file(vault_dir, args["path"], args["old_string"], args["new_string"])
        return result

    elif name == "get_related_docs":
        from ..vault.graph import graph_get_related

        fp = vault_dir / args["path"]
        if not fp.is_file():
            return responses.related_no_file(args["path"])

        results = graph_get_related(db_conn, args["path"], hops=1)
        if not results:
            return responses.related_no_links(args["path"])
        lines = [f"{r['path']} (via {r['type']})" for r in results]
        return f"Related documents for {args['path']}:\n" + "\n".join(lines)

    elif name == "recent_changes":
        from ..vault.db.changes import get_changes

        if not db_conn:
            return "Error: database not available"
        since_raw = args.get("since")
        if isinstance(since_raw, str) and since_raw.strip().lower() == "none":
            since_raw = None
        since_ts = parse_since(since_raw) if since_raw else 0.0
        path_raw = args.get("path")
        if isinstance(path_raw, str) and path_raw.strip().lower() == "none":
            path_raw = None
        source_raw = args.get("source")
        if isinstance(source_raw, str) and source_raw.strip().lower() == "none":
            source_raw = None
        changes = get_changes(
            db_conn,
            since=since_ts,
            path=path_raw,
            source=source_raw,
            limit=args.get("limit", 20),
        )
        if not changes:
            return responses.changes_empty()
        lines = []
        for c in changes:
            ts_str = time.strftime("%H:%M", time.localtime(c["ts"]))
            path_str = c["path"]
            if c.get("from_path"):
                path_str += f" (was: {c['from_path']})"
            lines.append(f"{ts_str} {c['source']:<10} {c['action']:<8} {path_str}")
        return "\n".join(lines)

    return f"Error: unknown tool '{name}'"
