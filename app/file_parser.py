"""
file_parser.py — 上传文件文本提取（共享知识库注入前置处理）

支持的格式：
  - 纯文本     .txt / .md / .csv / .log        （utf-8 优先，失败回退 gbk）
  - Word       .docx                            （python-docx：段落 + 表格）
  - Excel      .xlsx                            （openpyxl：所有工作表逐格转文本）
  - PDF        .pdf                             （pypdf：逐页提取文本）
  - 图片       .png/.jpg/.jpeg/.bmp/.webp       （RapidOCR：图片 OCR）

设计约定：
  - 所有第三方依赖（docx / openpyxl / pypdf / rapidocr）均为**懒加载**，
    缺失时抛 `ParseError` 给出可读提示，不阻塞其它格式。
  - 上传有大小上限（MAX_UPLOAD_BYTES）与提取文本字符上限（MAX_TEXT_CHARS），
    超出部分截断并在 meta 中标注。
  - 提取结果统一为 `(text, meta)`，meta 含 `ext / source / truncated / chars`。
"""
import io
from pathlib import Path

# ---- 限制 ----
MAX_UPLOAD_BYTES = 20 * 1024 * 1024   # 单文件 20MB 上限
MAX_TEXT_CHARS = 200_000               # 提取文本字符上限（超出截断）

# ---- 格式表：扩展名 -> 说明 ----
SUPPORTED_EXTS = {
    "txt": "纯文本", "md": "Markdown", "csv": "CSV", "log": "日志",
    "docx": "Word 文档", "xlsx": "Excel 表格", "pdf": "PDF 文档",
    "png": "图片(OCR)", "jpg": "图片(OCR)", "jpeg": "图片(OCR)",
    "bmp": "图片(OCR)", "webp": "图片(OCR)",
}


class ParseError(Exception):
    """解析失败（含依赖缺失 / 格式不支持 / 文件损坏等），message 可直接展示给用户。"""


def _normalize_ext(filename: str) -> str:
    return Path(filename).suffix.lower().lstrip(".")


def _clip(text: str, meta: dict) -> str:
    if len(text) > MAX_TEXT_CHARS:
        meta["truncated"] = True
        return text[:MAX_TEXT_CHARS]
    return text


# ---------------- 各格式提取 ----------------

def _parse_text(data: bytes) -> str:
    """纯文本：utf-8 优先，失败回退 gbk。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _parse_docx(data: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError:
        raise ParseError("解析 .docx 需要 python-docx，请执行 pip install python-docx") from None
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as e:
        raise ParseError("无法解析该 .docx 文件（可能已损坏或不是 Word 文档）") from e

    lines = []
    # 正文段落
    for p in document.paragraphs:
        t = p.text.strip()
        if t:
            lines.append(t)
    # 表格（逐行拼接）
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def _parse_xlsx(data: bytes) -> str:
    try:
        import openpyxl
    except ImportError:
        raise ParseError("解析 .xlsx 需要 openpyxl，请执行 pip install openpyxl") from None
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        raise ParseError("无法解析该 .xlsx 文件（可能已损坏或不是 Excel 表格）") from e

    lines = []
    for sheet in wb.worksheets:
        lines.append(f"## 工作表：{sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cells:
                lines.append(" | ".join(cells))
    wb.close()
    return "\n".join(lines)


def _parse_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ParseError("解析 .pdf 需要 pypdf，请执行 pip install pypdf") from None
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:
        raise ParseError("无法解析该 .pdf 文件（可能已损坏、加密或不是 PDF）") from e

    lines = []
    for i, page in enumerate(reader.pages, 1):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        t = "\n".join(line.strip() for line in t.splitlines() if line.strip())
        if t:
            lines.append(f"## 第 {i} 页\n{t}")
    return "\n\n".join(lines)


_OCR = None  # RapidOCR 单例（懒加载，复用进程内实例避免重复初始化）


def _get_ocr():
    global _OCR
    if _OCR is not None:
        return _OCR
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        raise ParseError("解析图片需要 RapidOCR，请执行 pip install rapidocr_onnxruntime") from None
    try:
        _OCR = RapidOCR()
    except Exception as e:
        raise ParseError(f"RapidOCR 初始化失败：{e}") from e
    return _OCR


def _parse_image(data: bytes) -> str:
    import numpy as np
    try:
        from PIL import Image
    except ImportError:
        raise ParseError("解析图片需要 Pillow，请执行 pip install pillow") from None

    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:
        raise ParseError("无法解析该图片文件（格式不受支持或已损坏）") from e

    try:
        result, _ = _get_ocr()(np.array(img))
    except Exception as e:
        raise ParseError(f"OCR 识别失败：{e}") from e
    if not result:
        return ""
    return "\n".join(str(item[1]) for item in result)


# ---------------- 对外入口 ----------------

def parse_file(filename: str, data: bytes) -> tuple:
    """提取文件文本。

    返回 `(text, meta)`：
      - text: 提取出的纯文本（可能为空字符串，说明文件没有可读内容）
      - meta: dict，含 `ext / source / chars / truncated` 等说明

    异常：ParseError（依赖缺失 / 格式不支持 / 文件损坏 / 超过大小上限）
    """
    ext = _normalize_ext(filename)
    source = filename
    meta = {"ext": ext, "source": source, "truncated": False}

    if len(data) > MAX_UPLOAD_BYTES:
        raise ParseError(f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")

    if ext not in SUPPORTED_EXTS:
        supported = " / ".join(sorted(SUPPORTED_EXTS))
        raise ParseError(f"不支持的文件类型 .{ext}，支持：{supported}")

    if ext in ("txt", "md", "csv", "log"):
        text = _parse_text(data)
    elif ext == "docx":
        text = _parse_docx(data)
    elif ext == "xlsx":
        text = _parse_xlsx(data)
    elif ext == "pdf":
        text = _parse_pdf(data)
    else:  # 图片 OCR
        text = _parse_image(data)

    text = _clip(text.strip(), meta)
    meta["chars"] = len(text)
    return text, meta


def split_paragraphs(text: str, source: str = "") -> list:
    """按空行 / 换行切分为待注入的知识块（供共享向量库逐段索引）。"""
    chunks = [t.strip() for t in text.split("\n") if t.strip()]
    return chunks or ([text] if text.strip() else [])
