"""Privacy read-boundary shared primitives (spec 2026-11-18 §2.2).

Single definition point for the directory-level privacy semantics used by
the agent policy, the gates, the SQL filters and the web annotation
surface:

- ``is_privacy``        — Python predicate (first-segment hit).
- ``privacy_clause``    — SQL predicate builder (NOT (col LIKE ? ESCAPE ...)).
- ``INVISIBLE_MSG``     — one uniform model-facing refusal wording.
- ``PrivacyConfig``     — mtime-cache loader shared by the main server and
                          the MCP stdio process (config edits apply to the
                          next query, no restart; §2.7).
- ``detect_privacy_refs`` — §2.6 explicit-reference detection for the
                          save-time advisory (never reads privacy content).

Case semantics are ASCII-insensitive on BOTH sides (§2.2): the Python side
casefolds, the SQL side relies on SQLite LIKE's ASCII-insensitive default
(CJK has no case concept). Path comparison keeps the repo's backslash-
normalization convention; LIKE prefixes use the forward-slash form (the
files-table storage form, §4-4).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

# Unified model-facing refusal wording (§2.2): every read refusal says the
# same thing, so the refusal surface behaves as one constant.
INVISIBLE_MSG = "Path is invisible to agents"

# 隐私打开文档的固定引导文案（2026-11-18 语义升级）：编辑器自动带上隐私目录
# 内文档时不再静默跳过，而是注入这条逐字常量教 agent 引导用户。前缀与
# DocPathUserReminder._PREFIX 同族（[当前打开: ）——strip() 按该前缀清理旧
# 提醒，新提醒天然覆盖旧条，不堆叠。绝不内插真实路径（文案即常量本体）。
DOC_PRIVACY_NOTICE = (
    "[当前打开: （隐私目录内文档，模型不可见）勿尝试读取或推测其内容。"
    "若用户需要处理它，按此引导：优先建议用户把相关片段粘贴到对话中；"
    "或由其自行在配置中将该目录移出隐私名单。不可代用户修改名单或绕开限制。]"
)

# First segments that must never be privacy-scoped (§2.1): system dirs with
# their own exclusion semantics — folding them into the privacy filter
# would create behavioral ambiguity.
_RESERVED_FIRST = {".sync", ".trash", "suggestions"}


def parse_privacy_dirs(raw: object) -> tuple[str, ...]:
    """Validate + normalize the ``privacy.dirs`` list (fail-fast, §2.1).

    Rejects ValueError: non-list raw; non-string or empty entries;
    absolute paths (leading ``/`` or a drive-qualified start); ``..``
    segments; entries containing ``/`` (first-segment granularity only —
    the check runs on the backslash-normalized form, so ``dir\\x`` shapes
    are caught here too); first segment in ``_RESERVED_FIRST``
    (casefolded). Duplicates are dropped, first-occurrence order kept.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(
            f"privacy.dirs must be a list, got {type(raw).__name__}")
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            raise ValueError(
                f"privacy.dirs entries must be non-empty strings, got {entry!r}")
        norm = entry.replace("\\", "/")
        if ".." in norm.split("/"):
            raise ValueError(
                f"privacy.dirs entry must not contain '..': {entry!r}")
        if re.match(r"^[A-Za-z]:", norm) or norm.startswith("/"):
            raise ValueError(
                f"privacy.dirs entries must be vault-relative dir names, "
                f"not absolute paths: {entry!r}")
        if "/" in norm:
            raise ValueError(
                f"privacy.dirs entries are first-segment dir names "
                f"(no '/'): {entry!r}")
        if norm.casefold() in _RESERVED_FIRST:
            raise ValueError(
                f"privacy.dirs must not target a system directory: {entry!r}")
        if entry not in seen:
            seen.add(entry)
            out.append(entry)
    return tuple(out)


def _first_segment(p: str) -> str:
    """First path segment, backslashes normalized.

    Same semantics as ``server.agent.policy._first_segment`` — deliberately
    duplicated: the config/privacy layer must not depend on the agent layer
    (plan Task 2 Step 3: copy semantics, no cross-layer import).
    """
    return p.replace("\\", "/").split("/", 1)[0]


def is_privacy(dirs: tuple[str, ...], path: str) -> bool:
    """True when ``path``'s first segment hits the privacy list.

    ASCII-case-insensitive both sides (§2.2); backslashes normalized;
    deep same-names (``notes/diary/x`` vs list entry ``diary``) do NOT hit.
    Empty dirs → always False (mechanism stays lazy, §4-1).
    """
    if not dirs:
        return False
    return _first_segment(path).casefold() in {d.casefold() for d in dirs}


def _like_escape(seg: str) -> str:
    """Escape LIKE wildcards for an ``ESCAPE '\\'`` pattern (order matters:
    backslash first, then % and _)."""
    return seg.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def privacy_clause(dirs: tuple[str, ...], col: str) -> tuple[str, list]:
    """SQL predicate builder: ``NOT (col LIKE ? ESCAPE '\\' OR ...)``.

    Empty dirs → ``("", [])`` — the caller short-circuits (zero-change
    default, §4-1). Parameters are the forward-slash dir prefix + ``/%``
    with ``%_`` escaped (§5-7 injection pin).
    """
    if not dirs:
        return ("", [])
    parts = [f"{col} LIKE ? ESCAPE '\\'" for _ in dirs]
    params = [_like_escape(d.replace("\\", "/")) + "/%" for d in dirs]
    return ("NOT (" + " OR ".join(parts) + ")", params)


class PrivacyConfig:
    """mtime-cache loader for ``privacy.dirs`` (§2.2).

    Shared by the main server (WebContext) and the MCP stdio process: both
    read the same config.yaml and re-read it when its mtime changes, so a
    config edit applies to the next query without restarting either process
    (the §2.7 zero-derived-state promise rests on this live view). Absent
    privacy section in an OTHERWISE VALID config → ``()`` (user cleared it
    explicitly; lazily-off otherwise). Malformed privacy.dirs raises
    ValueError at the read (fail-loud: a broken privacy list must never
    silently degrade to "nothing is private").

    Transient-read fail-safe (review P1): an unreadable file (OSError — the
    file vanished mid-edit), an EMPTY/non-dict parse, or a valid YAML dict
    MISSING the ``server`` section (a torn/truncated write — the real
    config always carries a server section, and ``load_server_config``
    refuses without one) means "config in flight", NOT "privacy is off":
    keep the last-known dirs and do NOT cache the mtime, so the next access
    retries. The window between "config half-written" and "config complete"
    must never read as an empty privacy list.
    """

    def __init__(self, config_path: str | Path) -> None:
        self._path = Path(config_path)
        self._mtime_ns: int | None = None
        self._dirs: tuple[str, ...] = ()

    @property
    def dirs(self) -> tuple[str, ...]:
        try:
            mtime_ns = self._path.stat().st_mtime_ns
        except OSError:
            # 文件暂时不可见（编辑工具原子替换/清理瞬态）：保留上次名单，
            # 且不动 mtime 缓存——文件回来后下次访问立即重试。
            return self._dirs
        if mtime_ns != self._mtime_ns:
            loaded = self._load()
            if loaded is not None:
                self._mtime_ns = mtime_ns
                self._dirs = loaded
            # loaded is None = 瞬态（读失败/截断写）：保留旧值、不缓存该
            # mtime，下次访问重试。
        return self._dirs

    def _load(self) -> tuple[str, ...] | None:
        """Read + validate. ``None`` = transient (keep last dirs, retry on
        the next access); a tuple = authoritative result (cache the mtime).
        """
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except OSError:
            return None
        if not isinstance(data, dict) or not data.get("server"):
            # 空文件 / 非 mapping / 缺 server 节：截断写瞬态——一份真实
            # config 必有 server 节（load_server_config 缺之即拒）。
            return None
        privacy = data.get("privacy")
        raw = privacy.get("dirs") if isinstance(privacy, dict) else None
        return parse_privacy_dirs(raw)


# Explicit-reference detection (§2.6): wikilinks and bare path strings —
# pure prefix-segment string matching over the written content, never
# reading any privacy file.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]*)\]\]")
# Slash-bearing token, brackets/quotes/whitespace excluded so markdown
# wrappers ([[..]]/(..)/<..>) don't glue onto the captured path.
_BARE_PATH_RE = re.compile(r"""[^\s\[\]()<>{}"'`|]+/[^\s\[\]()<>{}"'`|]*""")


def detect_privacy_refs(content: str, dirs: tuple[str, ...]) -> list[str]:
    """Privacy-dir references in ``content``: ``[[目录/...]]`` wikilinks and
    bare path strings, in order of first appearance, deduplicated (§2.6).

    First-segment semantics only (``工作/日记/note.md`` is not a reference
    to ``日记``). Returned strings are the reference as written (a wikilink
    keeps its brackets). Advisory input only — never blocks a save.
    """
    if not dirs or not content:
        return []
    priv = {d.casefold() for d in dirs}
    wiki_spans = [(m.start(), m.end()) for m in _WIKILINK_RE.finditer(content)]

    hits: list[tuple[int, str]] = []
    for m in _WIKILINK_RE.finditer(content):
        if _first_segment(m.group(1)).casefold() in priv:
            hits.append((m.start(), m.group(0)))
    for m in _BARE_PATH_RE.finditer(content):
        if any(a <= m.start() < b for a, b in wiki_spans):
            continue  # already accounted for as the wikilink itself
        if _first_segment(m.group(0)).casefold() in priv:
            hits.append((m.start(), m.group(0)))

    out: list[str] = []
    seen: set[str] = set()
    for _, ref in sorted(hits, key=lambda t: t[0]):
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out
