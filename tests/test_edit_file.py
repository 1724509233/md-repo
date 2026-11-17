"""edit_file: strict-decode fidelity + atomic write (review F10/F11)."""
from pathlib import Path

from server.vault.fs.vault_ops import edit_file


def test_edit_file_rejects_non_utf8_without_touching_bytes(tmp_path):
    fp = tmp_path / "gbk.md"
    raw = b"name \xd6\xd0\xce\xc4\nold_marker\n"  # GBK 字节，非合法 UTF-8
    fp.write_bytes(raw)
    out = edit_file(tmp_path, "gbk.md", "old_marker", "new_marker")
    assert out.startswith("Error")
    assert "UTF-8" in out
    assert fp.read_bytes() == raw  # 逐字节不变


def test_edit_file_atomic_no_tmp_stray(tmp_path):
    (tmp_path / "a.md").write_bytes(b"body old_marker tail")
    out = edit_file(tmp_path, "a.md", "old_marker", "new_marker")
    assert out.startswith("Edited")
    assert (tmp_path / "a.md").read_bytes() == b"body new_marker tail"
    strays = [p.name for p in tmp_path.iterdir()
              if p.name.startswith(".") and p.name.endswith(".writing-tmp")]
    assert strays == []
