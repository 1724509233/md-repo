"""config.yaml 文档读写底座（设置页，spec 2026-11-22 §4.1/§4.2）。

leaf 依赖规则（与 server/config/README.md 一致）：只 import stdlib +
ruamel.yaml，不碰 routes / main / agent——本模块对配置语义零知识，
只提供字节级 IO 原语与纯数据变换。

承重约束（spec §4.2.3）：`save_doc` 的临时文件**必须**落在目标同目录——
加载器唯一的 config-dir 锚是 bgagent prompt（`Path(config_path).parent`），
临时文件去 %TEMP% 会让 prompt 存在性校验假阴性。

Windows 写语义（spec §4.2.5）：`os.replace` 可能被无 FILE_SHARE_DELETE 的
瞬时目标句柄（杀软/索引器）挡出 PermissionError → 退避重试（初次 + 最多
2 次重试），全败清临时文件后 raise `SettingsWriteError`（如实「未保存」）。
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

# ---------------------------------------------------------------------------
# revision（乐观并发守卫的字节哈希面）
# ---------------------------------------------------------------------------


def revision_of(path: Path) -> str:
    """文件字节哈希，"sha256:<hex>" 形态（PUT 的 revision 守卫值）。"""
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# round-trip 读写（注释 / 引号 / CRLF 保真）
# ---------------------------------------------------------------------------

# open_roundtrip 侦测到的行分隔符记在 YAML 对象上的键名（模块私有约定）。
_MD_LINESEP = "_md_settings_linesep"


def open_roundtrip(path: Path) -> tuple[YAML, Any]:
    """读入 round-trip 文档，返回 (yaml_obj, doc)。

    `typ="rt"` + `preserve_quotes`；读 `read_bytes`：ruamel 对 CRLF **不**原生
    保真（结构行一律按 \\n 写回，注释处还会漏出杂散 \\r），故读侧侦测行分隔符
    记到 yaml_obj 上，`save_doc` 写侧做字节级行分隔符重编码——这是 CRLF 配置
    「逐字节在场」（review focus 4）的承重实现。
    """
    raw = Path(path).read_bytes()
    yaml_obj = YAML(typ="rt")
    yaml_obj.preserve_quotes = True
    doc = yaml_obj.load(raw)
    setattr(yaml_obj, _MD_LINESEP, "\r\n" if b"\r\n" in raw else "\n")
    return yaml_obj, doc


class SettingsWriteError(RuntimeError):
    """save_doc 退避重试全败：配置未落盘（tmp_path 已清理，值供记录）。"""

    def __init__(self, tmp_path: Path):
        self.tmp_path = Path(tmp_path)
        super().__init__(
            f"config.yaml write failed after retries "
            f"(temp file {tmp_path} removed)")


def save_doc(yaml_obj: YAML, doc: Any, target: Path) -> str:
    """原子落盘：同目录临时文件 dump → os.replace（退避重试）→ 新 revision。"""
    target = Path(target)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".config-", suffix=".yaml.tmp")
    tmp = Path(tmp_name)
    try:
        # dump 进 bytes 缓冲 → 行分隔符重编码（见 open_roundtrip 注记）。
        buf = io.BytesIO()
        yaml_obj.dump(doc, buf)
        out = buf.getvalue().replace(b"\r\n", b"\n")
        if getattr(yaml_obj, _MD_LINESEP, "\n") == "\r\n":
            out = out.replace(b"\n", b"\r\n")
        with os.fdopen(fd, "wb") as f:
            f.write(out)
        last_exc: PermissionError | None = None
        for attempt in range(3):  # 初次 + 退避重试 ≤2（spec §4.2.5）
            try:
                os.replace(tmp, target)
                break
            except PermissionError as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
        else:
            raise SettingsWriteError(tmp) from last_exc
        return revision_of(target)
    finally:
        # 成功路径 tmp 已被 replace 消耗；任何失败路径（含 SettingsWriteError）
        # 临时文件必须清掉。
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 备份（滚动留 3，spec §4.2.4）
# ---------------------------------------------------------------------------

def backup_file(path: Path) -> Path:
    """`config.yaml` → `config.yaml.bak-YYYYmmdd-HHMMSS-ffffff`（同目录）。

    微秒尾防同秒双保存撞名；多余即删，保留最近 3 份。
    """
    path = Path(path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, dest)
    backups = sorted(path.parent.glob(f"{path.name}.bak-*"))
    for old in backups[:-3]:
        old.unlink(missing_ok=True)
    return dest


# ---------------------------------------------------------------------------
# api_key 掩码（密钥铁律：明文永不出 GET / 422 / 日志）
# ---------------------------------------------------------------------------


def mask_api_key(key: str | None) -> dict:
    """`{"set": bool, "mask": str | None}`；set = key 非空。

    掩码 = `首4-…末4`；长度 < 8 时只回「已设置」不回片段（防短密钥拼图）。
    """
    if not key:
        return {"set": False, "mask": None}
    mask = f"{key[:4]}-…{key[-4:]}" if len(key) >= 8 else None
    return {"set": True, "mask": mask}


# ---------------------------------------------------------------------------
# bgagent 条目变换（GET 反归一出参 / PUT 密钥回填写回）
# ---------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """ruamel CommentedMap/Seq → 纯 dict/list（JSON 可序列化，去注释载体）。"""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _to_vault_relative_posix(value: Any, vault: Path) -> str | None:
    """target_dir / report_dir 反归一：vault 相对 posix；相对值直通。"""
    if value is None:
        return None
    p = Path(str(value))
    if p.is_absolute():
        return p.resolve().relative_to(Path(vault).resolve()).as_posix()
    return p.as_posix()


def bgagent_to_payload(entry: dict, vault: Path) -> dict:
    """raw yaml 条目 → GET payload：直通 + 路径反归一 + 掩码。

    - `target_dir` / `report_dir` 反归一为 vault 相对 posix（spec §4.1 路径口径）；
    - `report_dir` 等于缺省 `<target_dir>/.reports/<alias>` 时删键（PUT 缺席即缺省）；
    - `model.api_key` 换 `mask_api_key` 形态；未建模键（escalation 等）原样直通。
    """
    payload = _plain(dict(entry))

    target_posix = _to_vault_relative_posix(payload.get("target_dir"), vault)
    if target_posix is not None:
        payload["target_dir"] = target_posix

    alias = str(payload.get("alias", ""))
    if "report_dir" in payload:
        report_posix = _to_vault_relative_posix(payload["report_dir"], vault)
        if alias and target_posix is not None and \
                report_posix == f"{target_posix}/.reports/{alias}":
            del payload["report_dir"]
        else:
            payload["report_dir"] = report_posix

    model = payload.get("model")
    if isinstance(model, dict) and "api_key" in model:
        model["api_key"] = mask_api_key(model["api_key"])

    return payload


def _old_api_key(old_entry: dict | None) -> str | None:
    if not old_entry:
        return None
    model = old_entry.get("model")
    if not isinstance(model, dict):
        return None
    return model.get("api_key")


def payload_to_bgagent_entry(payload: dict, old_entry: dict | None) -> dict:
    """PUT payload 条目 → 写回 raw 条目：`model.api_key` 三态回填（spec §4.2.2）。

    - 键缺省或 `null` ≡ 保持：回填 `old_entry` 原明文；无处回填 → 不写键；
    - 键存在（含 `""`）= 替换（`""` 即显式清除）。
    """
    entry = _plain(dict(payload))
    model = entry.get("model")
    if not isinstance(model, dict):
        return entry

    if "api_key" in model and model["api_key"] is not None:
        return entry  # 显式值（含 ""）直通

    old_key = _old_api_key(old_entry)
    if "api_key" in model:
        del model["api_key"]  # null 且无处回填 → 不写键
    if old_key is not None:
        model["api_key"] = old_key
    return entry
