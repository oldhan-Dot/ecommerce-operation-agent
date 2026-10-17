"""上传文件的私有存储：只接受系统生成的文件名，不公开原始文件或本机路径。"""

import re
from pathlib import Path

from app.core.config import BACKEND_DIR
from app.csv_preview import MAX_BYTES, CsvFormatError


def upload_root(settings):
    root = Path(settings.upload_dir).expanduser()
    return (root if root.is_absolute() else BACKEND_DIR / root).resolve()


def upload_path(name, settings):
    """路径解析后再次核验目录，拒绝路径穿越和指向目录外的符号链接。"""
    if not re.fullmatch(r"[0-9a-f]{32}\.csv", name):
        raise ValueError("上传文件标识无效")
    root = upload_root(settings)
    path = (root / name).resolve()
    if path.parent != root:
        raise ValueError("上传文件路径无效")
    return path


def read_upload(name, settings):
    with upload_path(name, settings).open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise CsvFormatError("文件不能超过 10 MB")
    return raw
