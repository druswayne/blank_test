from __future__ import annotations

import re
from html import escape, unescape

from bs4 import BeautifulSoup, NavigableString, Tag

from .pdf_service import html_to_plain

QUESTION_RE = re.compile(r"^\s*(?:\(\s*)?№\s*(\d+)(?:\s*\))?\s*[.\):]?\s*(.*)$", re.IGNORECASE)
QUESTION_HASH_RE = re.compile(r"^\s*(?:\(\s*)?#\s*(\d+)(?:\s*\))?\s*[.\):]?\s*(.*)$")
OPTION_RE = re.compile(r"^\s*(?:\(\s*)?([ABCD])(\*?)(?:\s*\))?\s*[.\):]?\s*(.*)$", re.IGNORECASE)


def quill_html_to_plain_lines(html: str) -> str:
    """
    Превращает HTML из Quill в многострочный plain-текст для разбора № / A–D.
    Теги убираются, блочные границы и <br> → перевод строки, картинки/видео → пробел.
    """
    if not html or not str(html).strip():
        return ""
    s = unescape(str(html))
    s = re.sub(r"(?is)<script.*?>.*?</script>", "", s)
    s = re.sub(r"(?is)<style.*?>.*?</style>", "", s)
    s = re.sub(r"(?i)<\s*br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</\s*(p|div|h[1-6]|blockquote|li|pre|tr|ul|ol)\s*>", "\n", s)
    s = re.sub(r"(?i)<\s*img[^>]*>", " ", s)
    s = re.sub(r"(?i)<\s*iframe[^>]*>.*?</\s*iframe\s*>", " ", s, flags=re.DOTALL)
    s = re.sub(r"(?i)<\s*video[^>]*>.*?</\s*video\s*>", " ", s, flags=re.DOTALL)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t\f\v]+\n", "\n", s)
    s = re.sub(r"\n[ \t\f\v]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def looks_like_quill_html(s: str) -> bool:
    t = (s or "").lstrip()
    if not t.startswith("<"):
        return False
    low = s[:4000].lower()
    markers = ("<p", "<div", "<br", "<span", "<img", "<h1", "<h2", "<h3", "<strong", "<em", "<iframe", "<video", "ql-")
    return any(m in low for m in markers)


def normalize_test_body_input(raw: str) -> str:
    """Вход из формы: либо plain (старый вид), либо HTML из Quill."""
    if not raw:
        return ""
    if looks_like_quill_html(raw):
        return quill_html_to_plain_lines(raw)
    return raw


def _split_tag_on_br_inner_html(tag: Tag) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    for child in tag.children:
        if isinstance(child, Tag) and child.name and str(child.name).lower() == "br":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(str(child))
    parts.append("".join(buf))
    return parts


def _wrap_tag_clone(name: str, attrs: dict, inner_html: str) -> str:
    soup = BeautifulSoup("", "html.parser")
    new_t = soup.new_tag(name)
    for k, v in attrs.items():
        if k == "class" and isinstance(v, (list, tuple)):
            new_t["class"] = list(v)
        elif v is None:
            continue
        elif v is True:
            new_t[k] = ""
        else:
            new_t[k] = v
    inner_soup = BeautifulSoup(inner_html, "html.parser")
    for node in list(inner_soup.contents):
        new_t.append(node)
    return str(new_t).strip()


def _expand_quill_block(block: Tag) -> list[tuple[str, str]]:
    name = (block.name or "").lower()

    if name in ("ol", "ul"):
        lis = block.find_all("li", recursive=False)
        if lis:
            out: list[tuple[str, str]] = []
            for li in lis:
                h = str(li)
                pl = quill_html_to_plain_lines(h)
                pl_joined = "\n".join(ln.rstrip() for ln in pl.splitlines())
                out.append((pl_joined, h))
            return out
        h = str(block).strip()
        pl = quill_html_to_plain_lines(h)
        pl_joined = "\n".join(ln.rstrip() for ln in pl.splitlines())
        return [(pl_joined, h)]

    if name == "pre":
        h = str(block).strip()
        pl = quill_html_to_plain_lines(h)
        pl_joined = "\n".join(ln.rstrip() for ln in pl.splitlines())
        return [(pl_joined, h)]

    if name in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"):
        inner_chunks = _split_tag_on_br_inner_html(block)
        ref_lines = [ln.rstrip() for ln in quill_html_to_plain_lines(str(block)).splitlines()]
        if not ref_lines and all(not (c or "").strip() for c in inner_chunks) and len(inner_chunks) <= 1:
            return []
        if all(not (c or "").strip() for c in inner_chunks) and len(inner_chunks) >= 2:
            return [("", str(block).strip())]
        if len(ref_lines) == 1 and len(inner_chunks) > 1:
            h = str(block).strip()
            return [(ref_lines[0], h)]
        pairs: list[tuple[str, str]] = []
        for inner in inner_chunks:
            h = _wrap_tag_clone(block.name or "p", dict(block.attrs), inner)
            pl = quill_html_to_plain_lines(h)
            pl_one = "\n".join(ln.rstrip() for ln in pl.splitlines())
            pairs.append((pl_one, h))
        gen_lines = [p[0] for p in pairs]
        if ref_lines == gen_lines:
            return pairs
        if len(ref_lines) == len(inner_chunks):
            out: list[tuple[str, str]] = []
            for inner, pl in zip(inner_chunks, ref_lines):
                out.append((pl, _wrap_tag_clone(block.name or "p", dict(block.attrs), inner)))
            return out
        h = str(block).strip()
        pl_joined = "\n".join(ref_lines)
        return [(pl_joined, h)]

    if name == "div":
        cls_set = set(block.get("class") or [])
        if cls_set & {"ql-video-wrapper", "ql-video", "ql-tooltip"}:
            h = str(block).strip()
            pl = quill_html_to_plain_lines(h)
            pl_joined = "\n".join(ln.rstrip() for ln in pl.splitlines())
            return [(pl_joined, h)]
        sub = [ch for ch in block.children if isinstance(ch, Tag)]
        allowed = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ol", "ul", "blockquote", "pre", "div"}
        if sub and all((ch.name or "").lower() in allowed for ch in sub):
            acc: list[tuple[str, str]] = []
            for ch in sub:
                acc.extend(_expand_quill_block(ch))
            return acc if acc else _single_block_pair(block)
        return _single_block_pair(block)

    return _single_block_pair(block)


def _single_block_pair(block: Tag) -> list[tuple[str, str]]:
    h = str(block).strip()
    pl = quill_html_to_plain_lines(h)
    pl_joined = "\n".join(ln.rstrip() for ln in pl.splitlines())
    return [(pl_joined, h)]


def quill_html_to_rich_line_pairs(html: str) -> list[tuple[str, str]]:
    """
    Список (plain-строка, HTML-фрагмент) в том же порядке, что даёт quill_html_to_plain_lines + splitlines.
    Нужен, чтобы при разборе № / A–D в БД попадал исходный HTML (цвета, жирный, картинки).
    """
    raw = (html or "").strip()
    if not raw:
        return []
    soup = BeautifulSoup(f'<div id="__qroot">{raw}</div>', "html.parser")
    root = soup.find("div", id="__qroot")
    if root is None:
        return []
    inner = root
    kids = [c for c in inner.children if isinstance(c, Tag)]
    if (
        len(kids) == 1
        and (kids[0].name or "").lower() == "div"
        and "ql-editor" in (kids[0].get("class") or [])
    ):
        inner = kids[0]

    out: list[tuple[str, str]] = []
    for child in inner.children:
        if isinstance(child, NavigableString):
            t = str(child).strip()
            if t:
                out.append((t, f"<p>{escape(t)}</p>"))
            continue
        if isinstance(child, Tag):
            out.extend(_expand_quill_block(child))
    return out


def _html_fragment_only_spacer(html: str) -> bool:
    """
    True, если фрагмент — только «пустой» абзац/перенос без медиа (его можно пропустить как пустую строку).
    Если в HTML есть картинка/видео и т.п., plain может быть пустым — фрагмент нельзя выкидывать.
    """
    if not (html or "").strip():
        return True
    low = str(html).lower()
    if any(
        tag in low
        for tag in ("<img", "<iframe", "<video", "<object", "<embed", "<svg", "<canvas", "<picture")
    ):
        return False
    return quill_html_to_plain_lines(html).strip() == ""


def _parse_structured_from_line_pairs(pairs: list[tuple[str, str]], *, max_questions: int) -> list[dict]:
    lines = [ln.rstrip() for ln, _ in pairs]
    htmls = [h for _, h in pairs]
    i = 0
    questions: list[dict] = []

    def skip_spacer_lines() -> None:
        nonlocal i
        while i < len(lines) and not lines[i].strip() and _html_fragment_only_spacer(htmls[i]):
            i += 1

    def is_question_start(s: str) -> bool:
        return bool(QUESTION_RE.match(s) or QUESTION_HASH_RE.match(s))

    def match_question(s: str):
        m = QUESTION_RE.match(s)
        if m:
            return m
        return QUESTION_HASH_RE.match(s)

    def is_option_start(s: str) -> bool:
        return bool(OPTION_RE.match(s))

    while True:
        skip_spacer_lines()
        if i >= len(lines):
            break
        line = lines[i]
        if not is_question_start(line):
            raise ValueError(
                f"Строка {i + 1}: ожидался заголовок вопроса (например «(№1) Текст» или «(#1) Текст»), "
                f"получено: {line[:120]!r}"
            )
        mq = match_question(line)
        if mq is None:
            raise ValueError(f"Строка {i + 1}: некорректный заголовок вопроса.")
        qnum = int(mq.group(1))
        first = (mq.group(2) or "").strip()
        i += 1
        q_parts: list[str] = []
        q_html_parts: list[str] = [htmls[i - 1]]
        if first:
            q_parts.append(first)
        while True:
            skip_spacer_lines()
            if i >= len(lines):
                break
            nxt = lines[i]
            if is_question_start(nxt) or is_option_start(nxt):
                break
            q_parts.append(nxt)
            q_html_parts.append(htmls[i])
            i += 1
        q_body = "\n".join(q_parts).strip()
        q_html_joined = "".join(q_html_parts)
        if not q_body and not any(
            t in q_html_joined.lower()
            for t in ("<img", "<iframe", "<video", "<object", "<embed", "<svg", "<canvas", "<picture")
        ):
            raise ValueError(f"Вопрос №{qnum}: пустой текст вопроса")

        opts: dict[str, str] = {}
        opts_html: dict[str, str] = {}
        corr: int | None = None
        for _ in range(4):
            skip_spacer_lines()
            if i >= len(lines):
                raise ValueError(f"Вопрос №{qnum}: не хватает вариантов ответа (нужно 4 строки A–D)")
            nxt = lines[i]
            if is_question_start(nxt):
                raise ValueError(f"Вопрос №{qnum}: не хватает вариантов перед следующим вопросом")
            om = OPTION_RE.match(nxt)
            if not om:
                raise ValueError(
                    f"Строка {i + 1}: ожидался вариант вида «(A) текст» или «(C*) верный», получено: {nxt[:120]!r}"
                )
            letter = om.group(1).upper()
            star = om.group(2)
            body = (om.group(3) or "").strip()
            if letter in opts:
                raise ValueError(f"Вопрос №{qnum}: вариант {letter} указан дважды")
            opts[letter] = body
            opts_html[letter] = htmls[i]
            if star == "*":
                if corr is not None:
                    raise ValueError(f"Вопрос №{qnum}: несколько верных отметок «*»")
                corr = ord(letter) - ord("A")
            i += 1

        missing = [L for L in ("A", "B", "C", "D") if L not in opts]
        if missing:
            raise ValueError(f"Вопрос №{qnum}: не хватает вариантов: {', '.join(missing)}")
        if corr is None:
            raise ValueError(
                f"Вопрос №{qnum}: отметьте ровно один верный вариант звёздочкой сразу после буквы, например: (C*) текст"
            )

        q_html = "".join(q_html_parts).strip() or "<p></p>"
        questions.append(
            {
                "text": q_html,
                "options": [
                    opts_html["A"].strip() or "<p></p>",
                    opts_html["B"].strip() or "<p></p>",
                    opts_html["C"].strip() or "<p></p>",
                    opts_html["D"].strip() or "<p></p>",
                ],
                "correct": corr,
            }
        )

    if not questions:
        raise ValueError("Не найдено ни одного вопроса. Начните со строки вида «(№1) Текст вопроса».")

    if len(questions) > max_questions:
        raise ValueError(f"Не больше {max_questions} вопросов.")

    skip_spacer_lines()
    if i < len(lines):
        raise ValueError(f"Строка {i + 1}: лишний текст после последнего вопроса: {lines[i][:120]!r}")

    return questions


def parse_structured_quill_html(html: str, *, max_questions: int = 10) -> list[dict]:
    """
    Разбор теста из HTML Quill с сохранением форматирования в полях вопроса/вариантов.
    Структура всегда валидируется по тем же строковым правилам, что и plain-текст,
    но в результате сохраняются rich HTML-фрагменты (оформление/картинки).
    """
    if not (html or "").strip():
        raise ValueError("Пустой текст теста.")
    pairs = quill_html_to_rich_line_pairs(html)
    if not pairs:
        raise ValueError("Не удалось разобрать содержимое редактора. Проверьте структуру строк вида (№1), (A)–(D), (C*).")
    return _parse_structured_from_line_pairs(pairs, max_questions=max_questions)


def _paragraph_html(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return "<p></p>"
    return "<p>" + escape(t).replace("\n", "<br/>") + "</p>"


def parse_structured_test_body(text: str, *, max_questions: int = 10) -> list[dict]:
    """
    Разбор одного текста вида:

    №1 Текст вопроса
    A Вариант 1
    B Вариант 2
    C* Вариант 3
    D Вариант 4

    Звёздочка сразу после буквы (A–D) отмечает верный ответ.
    Допускается заголовок вопроса через # вместо №.
    Рекомендуемый формат: (№1) ... и (A)/(B*) ...
    """
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    i = 0
    questions: list[dict] = []

    def skip_blanks() -> None:
        nonlocal i
        while i < len(lines) and not lines[i].strip():
            i += 1

    def is_question_start(s: str) -> bool:
        return bool(QUESTION_RE.match(s) or QUESTION_HASH_RE.match(s))

    def match_question(s: str):
        m = QUESTION_RE.match(s)
        if m:
            return m
        return QUESTION_HASH_RE.match(s)

    def is_option_start(s: str) -> bool:
        return bool(OPTION_RE.match(s))

    while True:
        skip_blanks()
        if i >= len(lines):
            break
        line = lines[i]
        if not is_question_start(line):
            raise ValueError(
                f"Строка {i + 1}: ожидался заголовок вопроса (например «(№1) Текст» или «(#1) Текст»), "
                f"получено: {line[:120]!r}"
            )
        mq = match_question(line)
        if mq is None:
            raise ValueError(f"Строка {i + 1}: некорректный заголовок вопроса.")
        qnum = int(mq.group(1))
        first = (mq.group(2) or "").strip()
        heading = "№" if QUESTION_RE.match(line) else "#"
        i += 1
        q_parts: list[str] = []
        if first:
            q_parts.append(first)
        while True:
            skip_blanks()
            if i >= len(lines):
                break
            nxt = lines[i]
            if is_question_start(nxt) or is_option_start(nxt):
                break
            q_parts.append(nxt)
            i += 1
        q_body = "\n".join(q_parts).strip()
        if not q_body:
            raise ValueError(f"Вопрос №{qnum}: пустой текст вопроса")

        opts: dict[str, str] = {}
        corr: int | None = None
        for _ in range(4):
            skip_blanks()
            if i >= len(lines):
                raise ValueError(f"Вопрос №{qnum}: не хватает вариантов ответа (нужно 4 строки A–D)")
            nxt = lines[i]
            if is_question_start(nxt):
                raise ValueError(f"Вопрос №{qnum}: не хватает вариантов перед следующим вопросом")
            om = OPTION_RE.match(nxt)
            if not om:
                raise ValueError(
                    f"Строка {i + 1}: ожидался вариант вида «(A) текст» или «(C*) верный», получено: {nxt[:120]!r}"
                )
            letter = om.group(1).upper()
            star = om.group(2)
            body = (om.group(3) or "").strip()
            if letter in opts:
                raise ValueError(f"Вопрос №{qnum}: вариант {letter} указан дважды")
            opts[letter] = body
            if star == "*":
                if corr is not None:
                    raise ValueError(f"Вопрос №{qnum}: несколько верных отметок «*»")
                corr = ord(letter) - ord("A")
            i += 1

        missing = [L for L in ("A", "B", "C", "D") if L not in opts]
        if missing:
            raise ValueError(f"Вопрос №{qnum}: не хватает вариантов: {', '.join(missing)}")
        if corr is None:
            raise ValueError(
                f"Вопрос №{qnum}: отметьте ровно один верный вариант звёздочкой сразу после буквы, например: (C*) текст"
            )

        # В БД храним строки с теми же префиксами (№/#) и (A–D*), что видит Quill — иначе после fallback-разбора
        # blank_to_editor_html теряет маркеры и повторное сохранение ломает структуру.
        q_stored_plain = f"({heading}{qnum}) {q_body}".strip()
        opt_stored = {
            L: (f"({L}*) {opts[L]}" if corr == ord(L) - ord("A") else f"({L}) {opts[L]}").strip()
            for L in "ABCD"
        }

        questions.append(
            {
                "text": _paragraph_html(q_stored_plain),
                "options": [
                    _paragraph_html(opt_stored["A"]),
                    _paragraph_html(opt_stored["B"]),
                    _paragraph_html(opt_stored["C"]),
                    _paragraph_html(opt_stored["D"]),
                ],
                "correct": corr,
            }
        )

    if not questions:
        raise ValueError("Не найдено ни одного вопроса. Начните со строки вида «(№1) Текст вопроса».")

    if len(questions) > max_questions:
        raise ValueError(f"Не больше {max_questions} вопросов.")

    skip_blanks()
    if i < len(lines):
        raise ValueError(f"Строка {i + 1}: лишний текст после последнего вопроса: {lines[i][:120]!r}")

    return questions


def blank_to_structured_body(blank) -> str:
    """Plain-текст с разметкой (№) / (A–D) из сохранённого HTML (префиксы не дублируются)."""
    qs = sorted(blank.questions, key=lambda x: x.question_number)
    parts: list[str] = []
    for q in qs:
        qt_lines = [ln.rstrip() for ln in html_to_plain(q.question_text).splitlines()]
        if not qt_lines:
            qt_lines = [f"(№{q.question_number})"]
        fq = qt_lines[0]
        if not (QUESTION_RE.match(fq) or QUESTION_HASH_RE.match(fq)):
            qt_lines[0] = f"(№{q.question_number}) {fq}".strip()
        parts.extend(qt_lines)
        opts = [q.option_a, q.option_b, q.option_c, q.option_d]
        for j, L in enumerate("ABCD"):
            olines = [ln.rstrip() for ln in html_to_plain(opts[j]).splitlines()]
            if not olines:
                olines = [""]
            fo = olines[0]
            if not OPTION_RE.match(fo):
                star = "*" if q.correct_index == j else ""
                marker = f"({L}*)" if star else f"({L})"
                olines[0] = f"{marker} {fo}".strip()
            parts.extend(olines)
        parts.append("")
    return "\n".join(parts).rstrip()


def blank_to_editor_html(blank) -> str:
    """Собрать сохранённые HTML-фрагменты в одно тело для Quill при открытии редактора."""
    qs = sorted(blank.questions, key=lambda x: x.question_number)
    chunks: list[str] = []
    for q in qs:
        qt = (q.question_text or "").strip()
        if qt:
            chunks.append(qt)
        for opt in (q.option_a, q.option_b, q.option_c, q.option_d):
            ot = (opt or "").strip()
            if ot:
                chunks.append(ot)
        chunks.append("<p><br/></p>")
    return "".join(chunks)
