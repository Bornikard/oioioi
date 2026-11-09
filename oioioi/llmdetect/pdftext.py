"""Text of problem statements typeset with LaTeX, from their PDF.

General PDF text extraction loses what matters in a statement: exponents
(``10^9`` comes out as ``109``, a wrong limit), indices (``a_i`` as ``ai``),
symbols of some LaTeX fonts (``≤`` as ``‹`` with the ``polski`` package,
Polish letters as ``ƒ`` or ``(cid:243)`` with bitmap fonts) and the layout of
examples. This rebuilds the text from the characters and their positions:
lines from baselines, columns from gaps, and smaller raised or lowered
characters as ``^`` and ``_``. Only pdfminer is needed.
"""

import io
import statistics

from pdfminer.converter import PDFPageAggregator
from pdfminer.layout import LTChar, LTContainer
from pdfminer.pdffont import PDFType3Font
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage

MAX_PAGES = 30

# T1 (Cork) encoding of Polish letters: bitmap fonts of TeX installations
# without cm-super come without a character map.
_CORK = {
    0x81: "Ą", 0x82: "Ć", 0x86: "Ę", 0x8A: "Ł", 0x8B: "Ń", 0x91: "Ś", 0x99: "Ź", 0x9B: "Ż",
    0xA1: "ą", 0xA2: "ć", 0xA6: "ę", 0xAA: "ł", 0xAB: "ń", 0xB1: "ś", 0xB9: "ź", 0xBB: "ż",
    0xD3: "Ó", 0xF3: "ó",
}  # fmt: skip
# Characters of the math fonts of the polski package (PL fonts).
_PL_MATH = {"‹": "≤", "›": "≥", "̸": "≠"}
# Big operators of the TeX math extension fonts.
_MATH_EXTENSION = {"P": "∑", "X": "∑", "Q": "∏", "Y": "∏", "R": "∫", "Z": "∫"}
_BIG_OPERATORS = frozenset(_MATH_EXTENSION.values())


class _Aggregator(PDFPageAggregator):
    """Keeps the character code and font of each character, to fix the
    text of fonts without a usable character map.
    """

    def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):
        advance = super().render_char(matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate)
        item = self.cur_item._objs[-1]
        item.text_fixed = _fix(item.get_text(), font, cid)
        # The baseline: where the text cursor was, not the bottom of the glyph.
        item.baseline = matrix[5] + rise
        return advance


def _fix(text, font, cid):
    name = (font.fontname or "").split("+")[-1] if isinstance(font.fontname, str) else ""
    if isinstance(font, PDFType3Font) and cid in _CORK:
        return _CORK[cid]
    if "MathExtension" in name or name.upper().startswith(("CMEX", "LMMATHEXTENSION")):
        return _MATH_EXTENSION.get(chr(cid) if text.startswith("(cid:") else text, text)
    if name.startswith("PLMath"):
        return _PL_MATH.get(text, text)
    if text.startswith("(cid:"):
        return "?"
    return text


def _chars(item):
    if isinstance(item, LTChar):
        yield item
    elif isinstance(item, LTContainer):
        for child in item:
            yield from _chars(child)


def _lines(chars):
    """Characters grouped into lines, each a [baseline, size, chars].

    Characters of the usual size make the lines; big operators (a sum with
    its limits) join the line nearest to them; smaller characters join the
    nearest line around them, marked ``^`` when raised and ``_`` when lowered.
    """
    if not chars:
        return []
    normal = statistics.median(c.size for c in chars)
    operators = [c for c in chars if c.text_fixed in _BIG_OPERATORS]
    small = [c for c in chars if c.size < 0.85 * normal and c.text_fixed not in _BIG_OPERATORS]
    regular = [c for c in chars if c.size >= 0.85 * normal and c.text_fixed not in _BIG_OPERATORS]
    lines = []
    for char in sorted(regular, key=lambda c: (-c.baseline, c.x0)):
        for line in lines:
            if abs(line[0] - char.baseline) <= 0.25 * min(line[1], char.size):
                line[2].append(char)
                break
        else:
            lines.append([char.baseline, char.size, [char]])
    for char in operators:
        # The glyphs of the extension fonts hang below the cursor.
        middle = (char.y0 + char.y1) / 2
        near = [line for line in lines if abs(line[0] - middle) <= 0.9 * max(line[1], char.y1 - char.y0)]
        if near:
            min(near, key=lambda line: abs(line[0] - middle))[2].append(char)
        else:
            lines.append([char.baseline, char.size, [char]])
    for char in small:
        best = None
        for line in lines:
            offset = char.baseline - line[0]
            if -0.6 * line[1] <= offset <= 0.9 * line[1] and (best is None or abs(offset) < abs(best[0])):
                best = (offset, line)
        if best is None:
            lines.append([char.baseline, char.size, [char]])
            continue
        offset, line = best
        char.script = "^" if offset > 0.15 * line[1] else "_" if offset < -0.08 * line[1] else ""
        line[2].append(char)
    for line in lines:
        line[2].sort(key=lambda c: c.x0)
    lines.sort(key=lambda line: -line[0])
    return lines


def _line_text(chars, size):
    out = []
    previous = None
    script = ""
    run = []

    def flush():
        if run:
            body = "".join(run)
            out.append(f"{script}{{{body}}}" if len(body) > 1 else f"{script}{body}")
            run.clear()

    for char in chars:
        text = char.text_fixed
        if previous is not None:
            gap = char.x0 - previous.x1
            if gap > 0.9 * size:
                flush()
                out.append("    ")
            elif gap > 0.1 * size and not (getattr(char, "script", "") and getattr(char, "script", "") == script):
                flush()
                out.append(" ")
        char_script = getattr(char, "script", "")
        if char_script != script:
            flush()
            script = char_script
        if script:
            run.append(text)
        else:
            out.append(text)
        previous = char
    flush()
    return "".join(out).rstrip()


def pdf_text(data, max_pages=MAX_PAGES):
    """The text of a PDF, one line of text per line of the page."""
    manager = PDFResourceManager()
    device = _Aggregator(manager)
    interpreter = PDFPageInterpreter(manager, device)
    pages = []
    for number, page in enumerate(PDFPage.get_pages(io.BytesIO(data))):
        if number >= max_pages:
            break
        interpreter.process_page(page)
        chars = [c for c in _chars(device.get_result()) if c.get_text().strip() or c.text_fixed.strip()]
        lines = _lines(chars)
        gaps = [a[0] - b[0] for a, b in zip(lines, lines[1:], strict=False)]
        usual = statistics.median(gaps) if gaps else 0
        text = []
        previous = None
        for baseline, size, line_chars in lines:
            # A paragraph break or a heading: more space than between lines.
            if previous is not None and previous - baseline > max(1.3 * usual, 1.1 * size):
                text.append("")
            text.append(_line_text(line_chars, size))
            previous = baseline
        # A lone page number at the bottom.
        if text and text[-1].strip().isdigit():
            text.pop()
        pages.append("\n".join(text))
    return "\n\n".join(pages)
