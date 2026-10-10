"""read_doc tool."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .tool import DocTool, ToolContext, _check_md_path


class ReadDocArgs(BaseModel):
    """Read a markdown document. By default returns the full content. Use offset/limit to read a specific line range (e.g. after an edit failure, re-read the affected section)."""
    path: str = Field(description="Relative path, e.g. 'notes/Skill-Scanner.md'")
    offset: int = Field(default=1, description="Line number to start from (1-indexed). Default 1.")
    limit: int | None = Field(default=None, description="Number of lines to read. Default: all remaining lines.")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v): return _check_md_path(v)


class ReadDocTool(DocTool):
    name = "read_doc"
    args_model = ReadDocArgs

    @staticmethod
    def read_offset_exceeds(offset: int, total: int) -> str:
        return f"Error: offset {offset} exceeds file length ({total} lines). Use read_doc with offset <= {total}."

    def _execute(self, args, ctx: ToolContext) -> str:
        fp = ctx.vault_dir / args.path
        if not fp.is_file():
            return self._file_not_found_hint(args.path, ctx.db_conn)
        content = fp.read_bytes().decode("utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        total = len(lines)
        offset = max(int(args.offset), 1)
        limit = args.limit
        if limit is not None:
            limit = int(limit)
        if offset > total:
            return self.read_offset_exceeds(offset, total)
        if limit is not None:
            selected = lines[offset - 1: offset - 1 + limit]
        else:
            selected = lines[offset - 1:]

        # Truncate by char budget (tail truncation, no partial lines)
        char_count = 0
        cut = len(selected)
        truncated = False
        for i, line in enumerate(selected):
            char_count += len(line)
            if char_count > ctx.read_doc_max_chars:
                cut = i
                truncated = True
                break

        shown_lines = selected[:cut]
        text = "".join(shown_lines)
        end_line = offset + len(shown_lines) - 1

        if not truncated and offset == 1 and len(selected) == total:
            return f"File: {args.path} ({total} lines, {len(content)} chars)\n<content>\n{text}</content>"

        header = f"File: {args.path} (lines {offset}-{end_line} of {total})"
        if truncated:
            remaining = len(selected) - cut
            next_offset = offset + cut
            header += f"\n[truncated — {remaining} more lines, use read_doc with offset={next_offset} to continue]"
        return f"{header}\n<content>\n{text}</content>"
