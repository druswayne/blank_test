"""
Лист вопросов A4:
1) Сохраняется полная UTF-8 HTML-страница рядом с PDF ({uuid}_questions.html).
2) PDF: Chromium через Playwright (корректные кириллица и CSS как в браузере).
3) Если Playwright недоступен — xhtml2pdf (добавляет свой @font-face для TTF).
"""
from __future__ import annotations

import base64
import re
from html import escape
from io import BytesIO
from pathlib import Path

import qrcode
from bs4 import BeautifulSoup, NavigableString, Tag
from xhtml2pdf import pisa

from ..models import TestBlank
from .qr_service import make_qr_payload


def _find_unicode_font_path() -> Path | None:
    candidates = [
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\arialuni.ttf"),
        Path(r"C:\Windows\Fonts\calibri.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
    ]
    for p in candidates:
        if p.is_file():
            return p
    pkg = Path(__file__).resolve().parent.parent / "fonts" / "DejaVuSans.ttf"
    if pkg.is_file():
        return pkg
    return None


def _font_face_css_pisa() -> str:
    p = _find_unicode_font_path()
    if p is None:
        return "body, .ql-editor { font-family: Helvetica, Arial, sans-serif !important; }"
    uri = p.as_uri()
    return f"""
@font-face {{
  font-family: 'PisaPdfFont';
  src: url('{uri}');
  font-weight: normal;
  font-style: normal;
}}
body, .ql-editor {{ font-family: PisaPdfFont, DejaVu Sans, Arial, sans-serif !important; }}
"""


def _font_face_embedded_base64() -> str:
    """
    Встраиваем только компактный шрифт из app/fonts (не системный Arial — иначе HTML > 1 МБ).
    Chromium и так рисует кириллицу через системный стек; встраивание — запас для Linux без шрифтов.
    """
    pkg = Path(__file__).resolve().parent.parent / "fonts" / "DejaVuSans.ttf"
    if not pkg.is_file() or pkg.stat().st_size > 900_000:
        return ""
    data = base64.b64encode(pkg.read_bytes()).decode("ascii")
    return f"""
@font-face {{
  font-family: 'EmbedPdfFont';
  src: url(data:font/ttf;charset=utf-8;base64,{data}) format('truetype');
  font-weight: normal;
  font-style: normal;
}}
"""


_DOC_FONT_STACK = '"Segoe UI", "Tahoma", "Noto Sans", "DejaVu Sans", "Liberation Sans", Arial, sans-serif'

_QUILL_PRINT_CSS = """
@page { size: A4; margin: 14mm; }
html, body { margin: 0; padding: 0; }
body {
  font-size: 11pt;
  line-height: 1.45;
  color: #111;
}
.page-header { margin-bottom: 12px; border-bottom: 1px solid #ccc; padding-bottom: 8px; }
.page-header h1 { font-size: 17pt; margin: 0 0 4px 0; font-weight: bold; }
.question-block { border: 1px solid #333; padding: 8px 10px; margin: 10px 0; page-break-inside: avoid; }
.ql-editor { box-sizing: border-box; outline: none; }
.ql-editor p, .ql-editor ol, .ql-editor ul, .ql-editor pre, .ql-editor blockquote,
.ql-editor h1, .ql-editor h2, .ql-editor h3, .ql-editor h4, .ql-editor h5, .ql-editor h6 { margin: 0 0 6px 0; }
.ql-editor h1 { font-size: 1.8em; font-weight: bold; }
.ql-editor h2 { font-size: 1.6em; font-weight: bold; }
.ql-editor h3 { font-size: 1.4em; font-weight: bold; }
.ql-editor h4 { font-size: 1.25em; font-weight: bold; }
.ql-editor h5 { font-size: 1.1em; font-weight: bold; }
.ql-editor h6 { font-size: 1em; font-weight: bold; }
.ql-editor .ql-size-small { font-size: 0.75em; }
.ql-editor .ql-size-large { font-size: 1.5em; }
.ql-editor .ql-size-huge { font-size: 2.2em; }
.ql-editor strong, .ql-editor b { font-weight: bold; }
.ql-editor em, .ql-editor i { font-style: italic; }
.ql-editor u { text-decoration: underline; }
.ql-editor s, .ql-editor strike { text-decoration: line-through; }
.ql-editor .ql-align-center { text-align: center; }
.ql-editor .ql-align-right { text-align: right; }
.ql-editor .ql-align-justify { text-align: justify; }
.ql-editor .ql-direction-rtl { direction: rtl; text-align: inherit; }
.ql-editor blockquote { border-left: 4px solid #ccc; padding-left: 12px; margin-left: 0; color: #444; }
.ql-editor pre, .ql-editor .ql-code-block-container {
  background: #f5f5f5; padding: 8px; border-radius: 4px;
  font-family: Consolas, "Liberation Mono", monospace !important;
  font-size: 0.9em; white-space: pre-wrap;
}
.ql-editor img { max-width: 100% !important; height: auto !important; display: inline-block; vertical-align: middle; }
.ql-editor a { color: #0645ad; text-decoration: underline; }
.ql-editor .ql-indent-1 { padding-left: 3em; }
.ql-editor .ql-indent-2 { padding-left: 6em; }
.ql-editor .ql-indent-3 { padding-left: 9em; }
.ql-editor .ql-indent-4 { padding-left: 12em; }
.ql-editor .ql-indent-5 { padding-left: 15em; }
.ql-editor .ql-indent-6 { padding-left: 18em; }
.ql-editor .ql-indent-7 { padding-left: 21em; }
.ql-editor .ql-indent-8 { padding-left: 24em; }
.ql-editor .ql-indent-9 { padding-left: 27em; }
.ql-editor .ql-font-serif { font-family: Georgia, "Times New Roman", serif !important; }
.ql-editor .ql-font-monospace { font-family: Consolas, "Liberation Mono", monospace !important; }
.option-line { font-size: 10.5pt; }
"""


def _strip_inline_font_family(html: str) -> str:
    s = re.sub(r"(?i)\bfont-family\s*:\s*[^;]+;?", "", html)
    s = re.sub(r";\s*;+", ";", s)
    s = re.sub(r'\sstyle="\s*"', "", s)
    return s


def _sanitize_user_html(fragment: str | None) -> str:
    if not fragment:
        return "<p><br/></p>"
    s = str(fragment)
    s = re.sub(r"(?is)<script[^>]*>.*?</script>", "", s)
    s = re.sub(r"(?is)<object[^>]*>.*?</object>", "", s)
    s = re.sub(r"(?is)<embed[^>]*/?>", "", s)
    s = re.sub(r"(?is)<iframe[^>]*>.*?</iframe>", '<span style="color:#666;font-style:italic">[Видео]</span>', s)
    s = re.sub(r"(?i)\son\w+\s*=", " data-removed=", s)
    s = re.sub(r"(?i)href\s*=\s*[\"']?\s*javascript:", 'href="#" data-x-', s)
    return _strip_inline_font_family(s)


def _strip_struct_prefix_in_html(fragment: str | None, pattern: str) -> str:
    """
    Удаляет служебный префикс структуры (№1, A, C* ...) в начале первого текстового узла.
    Нужен только для печати PDF, чтобы не дублировать маркеры вопроса/вариантов.
    """
    src = _sanitize_user_html(fragment)
    if not src.strip():
        return src
    soup = BeautifulSoup(src, "html.parser")
    rx = re.compile(pattern, re.IGNORECASE)

    def walk(node: Tag) -> bool:
        for ch in list(node.children):
            if isinstance(ch, NavigableString):
                new_val = rx.sub("", str(ch), count=1)
                if new_val != str(ch):
                    ch.replace_with(new_val)
                    return True
                if str(ch).strip():
                    return True
                continue
            if isinstance(ch, Tag) and walk(ch):
                return True
        return False

    root = soup
    walk(root)
    return str(soup)


def _pdf_question_html(fragment: str | None) -> str:
    return _strip_struct_prefix_in_html(
        fragment,
        r"^\s*(?:\(\s*)?(?:№\s*\d+|#\s*\d+)(?:\s*\))?\s*[.)\-:]?\s*",
    )


def _pdf_option_html(fragment: str | None) -> str:
    return _strip_struct_prefix_in_html(
        fragment,
        r"^\s*(?:\(\s*)?[ABCD](?:\s*\*)?(?:\s*\))?\s*[.)\-:]?\s*",
    )


def _qr_png_data_uri(qr_payload: str) -> str:
    buf = BytesIO()
    img = qrcode.make(qr_payload).convert("RGB")
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _document_font_css() -> str:
    """Шрифт основного текста: встроенный TTF при небольшом размере, иначе системный стек."""
    embed = _font_face_embedded_base64()
    if embed:
        return (
            embed
            + f"\nbody, .ql-editor {{ font-family: EmbedPdfFont, {_DOC_FONT_STACK} !important; }}\n"
        )
    return f"\nbody, .ql-editor {{ font-family: {_DOC_FONT_STACK} !important; }}\n"


def build_questions_print_html(*, blank: TestBlank, qr_payload: str) -> str:
    """Полная HTML-страница UTF-8 для просмотра в браузере и печати в PDF через Chromium."""
    qr_uri = _qr_png_data_uri(qr_payload)
    title_esc = escape((blank.title or "Тестовый бланк").strip())
    font_css = _document_font_css()

    parts: list[str] = [
        '<!DOCTYPE html><html lang="ru"><head>',
        '<meta charset="utf-8"/>',
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>',
        "<title>",
        title_esc,
        "</title><style>",
        font_css,
        _QUILL_PRINT_CSS,
        "</style></head><body>",
        '<div class="page-header">',
        '<table style="width:100%;border-collapse:collapse"><tr>',
        f'<td style="vertical-align:top;width:72%"><h1>{title_esc}</h1>',
        '<p style="font-size:9pt;margin:6px 0 0 0;color:#444">Для ответов используйте отдельный бланк A6 с QR-кодом.</p></td>',
        f'<td style="vertical-align:top;text-align:right;width:28%"><img src="{qr_uri}" width="76" height="76" alt="QR"/></td>',
        "</tr></table></div>",
    ]

    for q in sorted(blank.questions, key=lambda x: x.question_number):
        parts.append('<div class="question-block">')
        parts.append('<table style="width:100%;border-collapse:collapse;margin:0"><tr>')
        parts.append(f'<td style="vertical-align:top;width:32px;font-weight:bold;font-size:12pt">{q.question_number}.</td>')
        parts.append('<td style="vertical-align:top">')
        parts.append(f'<div class="ql-editor ql-snow">{_pdf_question_html(q.question_text)}</div>')
        parts.append("</td></tr></table>")
        for letter, html in (
            ("A", q.option_a),
            ("B", q.option_b),
            ("C", q.option_c),
            ("D", q.option_d),
        ):
            parts.append('<table style="width:100%;border-collapse:collapse;margin-top:6px"><tr>')
            parts.append(f'<td style="vertical-align:top;width:32px;font-weight:bold">{letter})</td>')
            parts.append('<td style="vertical-align:top">')
            parts.append(f'<div class="ql-editor ql-snow option-line">{_pdf_option_html(html)}</div>')
            parts.append("</td></tr></table>")
        parts.append("</div>")

    parts.append("</body></html>")
    return "".join(parts)


def _html_for_pisa(html: str) -> str:
    """Добавляет шрифт для xhtml2pdf (file://), не меняя файл на диске."""
    inj = f"<style>{_font_face_css_pisa()}</style>"
    if "<head>" in html:
        return html.replace("<head>", "<head>" + inj, 1)
    return inj + html


def _write_pdf_playwright(html: str, pdf_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load", timeout=60_000)
            page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"},
            )
        finally:
            browser.close()


def _write_pdf_pisa(html: str, pdf_path: Path) -> None:
    combined = _html_for_pisa(html)
    src = combined.encode("utf-8")
    with open(pdf_path, "wb") as out:
        result = pisa.CreatePDF(src, dest=out, encoding="utf-8")
    if result.err:
        raise RuntimeError("Ошибка генерации PDF (xhtml2pdf)")


def generate_questions_pdf_html(
    *,
    blank: TestBlank,
    pdf_dir: str,
    qr_secret: str,
    qr_payload_version: str,
) -> str:
    qr_payload = make_qr_payload(
        version=qr_payload_version,
        blank_uuid=blank.uuid,
        secret=qr_secret,
    )
    html = build_questions_print_html(blank=blank, qr_payload=qr_payload)
    pdf_dir_path = Path(pdf_dir)
    pdf_dir_path.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_dir_path / f"{blank.uuid}_questions.pdf"
    html_path = pdf_dir_path / f"{blank.uuid}_questions.html"

    html_path.write_text(html, encoding="utf-8")

    try:
        _write_pdf_playwright(html, pdf_path)
    except Exception:
        _write_pdf_pisa(html, pdf_path)

    return str(pdf_path)
