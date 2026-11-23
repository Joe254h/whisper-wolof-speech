"""File exports for Kikao: Word, PowerPoint, HTML, plain text, subtitles and JSON."""

from __future__ import annotations

import html as htmllib
import io
import json
import re
import xml.etree.ElementTree as ET

import markdown as mdlib

INK = (0x16, 0x20, 0x3B)
MUTED = (0x5B, 0x66, 0x80)
TEAL = (0x13, 0x8A, 0x7A)
RED = (0xE0, 0x39, 0x3E)
PAPER = (0xF4, 0xF6, 0xFA)
LINE = (0xD6, 0xDC, 0xE7)
FONT = "Segoe UI"


# --------------------------------------------------------------------------- markdown


def md_to_html(md: str) -> str:
    return mdlib.markdown(md or "", extensions=["tables", "sane_lists", "nl2br"], output_format="xhtml")


def md_to_text(md: str) -> str:
    """Readable plain text: no #, **, or table pipes."""
    out = []
    for line in (md or "").splitlines():
        s = line.rstrip()
        if re.fullmatch(r"\|?\s*:?-{2,}.*", s):
            continue  # table divider
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            s = "  |  ".join(cells)
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            title = m.group(2)
            s = f"\n{title.upper()}" if len(m.group(1)) <= 2 else f"\n{title}"
        s = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda x: x.group(1) or x.group(2), s)
        s = re.sub(r"(?<!\w)[*_](.+?)[*_](?!\w)", r"\1", s)
        s = re.sub(r"`([^`]+)`", r"\1", s)
        s = re.sub(r"^\s*[-*+]\s+", "  • ", s)
        if s.strip() == "---":
            s = ""
        out.append(s)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def _xhtml_tree(md: str):
    html = md_to_html(md)
    # ElementTree only knows XML entities; turn named HTML entities into characters first.
    html = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)(\w+);",
                  lambda m: htmllib.unescape(m.group(0)), html)
    html = html.replace("&nbsp;", " ")
    try:
        return ET.fromstring(f"<div>{html}</div>")
    except ET.ParseError:
        return None


# --------------------------------------------------------------------------- HTML page

HTML_PAGE = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --ink:#16203b; --muted:#5b6680; --line:#d6dce7; --soft:#f4f6fa; }}
body {{ margin:0; background:#eef1f5; color:var(--ink); font:17px/1.65 Georgia, "Source Serif 4", serif; }}
main {{ max-width:760px; margin:40px auto; background:#fff; padding:56px 64px; border-radius:18px;
       border:1px solid var(--line); }}
h1,h2,h3,th {{ font-family:"Segoe UI", system-ui, sans-serif; line-height:1.2; }}
h1 {{ font-size:2rem; margin:0 0 .6em; letter-spacing:-.02em; }}
h2 {{ font-size:1.25rem; margin:2em 0 .5em; padding-top:.9em; border-top:1px solid var(--line); }}
h3 {{ font-size:1.05rem; margin:1.4em 0 .3em; }}
table {{ width:100%; border-collapse:collapse; font:15px/1.45 "Segoe UI", system-ui, sans-serif; margin:.5em 0 1.4em; }}
th, td {{ text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ background:var(--soft); }}
.meta {{ color:var(--muted); font:14px "Segoe UI", system-ui, sans-serif; margin-bottom:2em; }}
@media (max-width:700px) {{ main {{ margin:0; border-radius:0; padding:28px 20px; }} }}
@media print {{ body {{ background:#fff; }} main {{ border:0; margin:0; padding:0; max-width:none; }} }}
</style></head>
<body><main>
<p class="meta">{meta}</p>
{body}
</main></body></html>
"""


def report_html(title: str, meta_line: str, md: str, lang: str = "en") -> str:
    return HTML_PAGE.format(title=htmllib.escape(title), meta=htmllib.escape(meta_line), body=md_to_html(md),
                            lang=lang)


# --------------------------------------------------------------------------- Word


def _docx_base():
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    for name, size in (("Title", 24), ("Heading 1", 18), ("Heading 2", 14), ("Heading 3", 12)):
        st = styles[name]
        st.font.name = FONT
        st.font.size = Pt(size)
        st.font.bold = True
        st.font.color.rgb = RGBColor(*INK)
    for sec in doc.sections:
        from docx.shared import Cm
        sec.left_margin = sec.right_margin = Cm(2.3)
        sec.top_margin = sec.bottom_margin = Cm(2.2)
    return doc


def _shade(cell, hex_fill: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tc_pr.append(shd)


def _runs(paragraph, el, bold=False, italic=False):
    """Copy inline text (with <strong>/<em>/<code>/<br>) into Word runs."""
    from docx.shared import RGBColor

    def add(text, b, i, code=False):
        if text:
            r = paragraph.add_run(text)
            r.bold, r.italic = b or None, i or None
            if code:
                r.font.name = "Consolas"
                r.font.color.rgb = RGBColor(*MUTED)

    add(el.text, bold, italic)
    for child in el:
        tag = child.tag.lower()
        if tag == "br":
            paragraph.add_run().add_break()
        elif tag in ("strong", "b"):
            _runs(paragraph, child, True, italic)
        elif tag in ("em", "i"):
            _runs(paragraph, child, bold, True)
        elif tag == "code":
            add("".join(child.itertext()), bold, italic, code=True)
        elif tag == "a":
            _runs(paragraph, child, bold, italic)
        elif tag not in ("ul", "ol"):
            _runs(paragraph, child, bold, italic)
        add(child.tail, bold, italic)


def _blocks_to_docx(doc, root, list_level=0):
    for el in root:
        tag = el.tag.lower()
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = min(int(tag[1]), 3)
            _runs(doc.add_heading("", level=level), el)
        elif tag == "p":
            _runs(doc.add_paragraph(), el)
        elif tag in ("ul", "ol"):
            style = "List Bullet" if tag == "ul" else "List Number"
            if list_level:
                style += f" {min(list_level + 1, 3)}"
            for li in el:
                p = doc.add_paragraph(style=style)
                _runs(p, li)
                for sub in li:
                    if sub.tag.lower() in ("ul", "ol"):
                        _blocks_to_docx(doc, [sub], list_level + 1)
        elif tag == "table":
            rows = [r for r in el.iter() if r.tag.lower() == "tr"]
            if not rows:
                continue
            ncols = max(len(list(r)) for r in rows)
            table = doc.add_table(rows=0, cols=ncols)
            table.style = "Table Grid"
            for ri, r in enumerate(rows):
                cells = table.add_row().cells
                for ci, c in enumerate(list(r)[:ncols]):
                    para = cells[ci].paragraphs[0]
                    _runs(para, c, bold=(ri == 0 or c.tag.lower() == "th"))
                    if ri == 0:
                        _shade(cells[ci], "E8ECF4")
            doc.add_paragraph()
        elif tag == "blockquote":
            for child in el:
                _runs(doc.add_paragraph(style="Intense Quote" if "Intense Quote" in doc.styles else None), child)
        elif tag == "hr":
            doc.add_paragraph()
        else:
            text = "".join(el.itertext()).strip()
            if text:
                doc.add_paragraph(text)


def report_docx(title: str, meta_line: str, md: str) -> bytes:
    from docx.shared import Pt, RGBColor

    doc = _docx_base()
    meta = doc.add_paragraph()
    r = meta.add_run(meta_line)
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor(*MUTED)
    tree = _xhtml_tree(md)
    if tree is None:
        for line in md_to_text(md).splitlines():
            doc.add_paragraph(line)
    else:
        _blocks_to_docx(doc, tree)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def transcript_docx(title: str, meta_line: str, segments: list[dict]) -> bytes:
    from docx.shared import Pt, RGBColor

    doc = _docx_base()
    doc.add_heading(f"{title}: transcript", level=1)
    meta = doc.add_paragraph()
    r = meta.add_run(meta_line)
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor(*MUTED)
    for seg in segments:
        if not seg.get("text"):
            continue
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(4)
        ts = p.add_run(f"{clock(seg['start'])}   ")
        ts.font.size = Pt(9)
        ts.font.color.rgb = RGBColor(*TEAL)
        p.add_run(seg["text"])
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- subtitles


def clock(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _stamp(seconds: float, sep: str) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _cues(segments):
    cues = [s for s in segments if s.get("text")]
    for i, s in enumerate(cues):
        start = s["start"]
        end = s.get("end") or 0
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else start + 5
        if end <= start:
            end = max(start + 1, min(nxt, start + 8))
        yield start, end, s["text"]


def transcript_srt(segments: list[dict]) -> str:
    blocks = [f"{i}\n{_stamp(a, ',')} --> {_stamp(b, ',')}\n{t}\n"
              for i, (a, b, t) in enumerate(_cues(segments), 1)]
    return "\n".join(blocks)


def transcript_vtt(segments: list[dict]) -> str:
    return "WEBVTT\n\n" + "\n".join(f"{_stamp(a, '.')} --> {_stamp(b, '.')}\n{t}\n" for a, b, t in _cues(segments))


def transcript_json(meta: dict, segments: list[dict]) -> str:
    keep = {k: meta.get(k) for k in ("id", "title", "language", "created_at", "duration_seconds")}
    return json.dumps({"meeting": keep, "segments": segments}, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- slides


def slides_from_report(md: str, title: str, subtitle: str) -> dict:
    """Builds a skim deck straight from the report's sections. Used when the model's
    slide outline is missing or unreadable, so slides always exist."""
    slides = []
    current = None
    table_rows: list[list[str]] = []

    def flush():
        nonlocal current, table_rows
        if current is None:
            return
        if table_rows:
            current["table"] = {"columns": table_rows[0], "rows": table_rows[1:8]}
        current["bullets"] = current["bullets"][:5]
        if current["bullets"] or current.get("table"):
            slides.append(current)
        current, table_rows = None, []

    for raw in (md or "").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            flush()
            current = {"title": _plain(line[3:]), "bullets": []}
            continue
        if current is None or not line or line.startswith("#") and not line.startswith("### "):
            continue
        if re.fullmatch(r"\|?\s*:?-{2,}.*", line):
            continue
        if line.startswith("|"):
            table_rows.append([_plain(c) for c in line.strip("|").split("|")])
        elif line.startswith("### "):
            current["bullets"].append(_plain(line[4:]))
        elif re.match(r"^([-*+]|\d+[.)])\s+", line):
            current["bullets"].append(_short(_plain(re.sub(r"^([-*+]|\d+[.)])\s+", "", line))))
        elif not current["bullets"]:
            current["bullets"].extend(_short(s) for s in re.split(r"(?<=[.!?])\s+", _plain(line)) if s)
    flush()
    return {"title": title, "subtitle": subtitle, "slides": slides[:10]}


def _plain(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda x: x.group(1) or x.group(2), s)
    s = re.sub(r"(?<!\w)[*_](.+?)[*_](?!\w)", r"\1", s)
    return re.sub(r"`([^`]+)`", r"\1", s).strip()


def _short(s: str, limit: int = 140) -> str:
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + "…"


def normalise_deck(raw, title: str, subtitle: str) -> dict | None:
    """Validates the model's JSON deck. Returns None if it is unusable."""
    if not isinstance(raw, dict) or not isinstance(raw.get("slides"), list):
        return None
    slides = []
    for s in raw["slides"][:12]:
        if not isinstance(s, dict) or not str(s.get("title", "")).strip():
            continue
        item = {"title": _short(str(s["title"]).strip(), 80),
                "bullets": [_short(str(b).strip()) for b in (s.get("bullets") or []) if str(b).strip()][:6]}
        if s.get("lead"):
            item["lead"] = _short(str(s["lead"]).strip(), 180)
        t = s.get("table")
        if isinstance(t, dict) and isinstance(t.get("rows"), list) and t["rows"]:
            cols = [str(c) for c in (t.get("columns") or [])][:4]
            rows = [[_short(str(c), 90) for c in r][: len(cols) or 4] for r in t["rows"] if isinstance(r, list)][:8]
            if rows:
                item["table"] = {"columns": cols or [f"Column {i + 1}" for i in range(len(rows[0]))], "rows": rows}
        if item["bullets"] or item.get("table") or item.get("lead"):
            slides.append(item)
    if not slides:
        return None
    return {"title": str(raw.get("title") or title).strip()[:120],
            "subtitle": str(raw.get("subtitle") or subtitle).strip()[:160], "slides": slides}


def parse_json_block(text: str):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None


def slides_pptx(deck: dict) -> bytes:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.oxml.ns import qn
    from pptx.util import Emu, Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    W, H = prs.slide_width, prs.slide_height
    total = len(deck["slides"])

    def rgb(c):
        return RGBColor(*c)

    def fill_bg(slide, color):
        bg = slide.background.fill
        bg.solid()
        bg.fore_color.rgb = rgb(color)

    def text(slide, x, y, w, h, value, size, color, bold=False, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
        box = slide.shapes.add_textbox(x, y, w, h)
        tf = box.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = anchor
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        p = tf.paragraphs[0]
        p.alignment = align
        r = p.add_run()
        r.text = value
        r.font.size, r.font.bold, r.font.name = Pt(size), bold, FONT
        r.font.color.rgb = rgb(color)
        return box

    def dot(slide, x, y, d, color):
        shp = slide.shapes.add_shape(MSO_SHAPE.OVAL, x, y, d, d)
        shp.fill.solid()
        shp.fill.fore_color.rgb = rgb(color)
        shp.line.fill.background()
        return shp

    def bullet(p, color):
        pPr = p._p.get_or_add_pPr()
        pPr.set("marL", str(Emu(Inches(0.38))))
        pPr.set("indent", str(-Emu(Inches(0.38))))
        for tag in ("a:buNone", "a:buChar", "a:buClr"):
            for el in pPr.findall(qn(tag)):
                pPr.remove(el)
        clr = pPr.makeelement(qn("a:buClr"), {})
        srgb = clr.makeelement(qn("a:srgbClr"), {"val": "%02X%02X%02X" % color})
        clr.append(srgb)
        pPr.append(clr)
        pPr.append(pPr.makeelement(qn("a:buChar"), {"char": "●"}))

    # title slide
    s = prs.slides.add_slide(blank)
    fill_bg(s, INK)
    dot(s, Inches(0.9), Inches(0.85), Inches(0.32), RED)
    text(s, Inches(1.35), Inches(0.83), Inches(6), Inches(0.4), "Meeting brief", 14, (0xC9, 0xD0, 0xE3))
    text(s, Inches(0.9), Inches(2.3), Inches(11.4), Inches(2.4), deck["title"], 46, (0xFF, 0xFF, 0xFF), bold=True,
         anchor=MSO_ANCHOR.BOTTOM)
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.9), Inches(4.95), Inches(1.1), Inches(0.07))
    bar.fill.solid()
    bar.fill.fore_color.rgb = rgb((0x63, 0xD0, 0xC8))
    bar.line.fill.background()
    text(s, Inches(0.9), Inches(5.25), Inches(11.4), Inches(0.8), deck.get("subtitle", ""), 18, (0xC9, 0xD0, 0xE3))
    text(s, Inches(0.9), Inches(6.6), Inches(11.4), Inches(0.4), "About two minutes to read", 12,
         (0x97, 0xA1, 0xBB))

    for idx, sl in enumerate(deck["slides"], 1):
        s = prs.slides.add_slide(blank)
        fill_bg(s, (0xFF, 0xFF, 0xFF))
        side = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(0.18), H)
        side.fill.solid()
        side.fill.fore_color.rgb = rgb(INK)
        side.line.fill.background()
        text(s, Inches(0.9), Inches(0.55), Inches(11.5), Inches(1.0), sl["title"], 38, INK, bold=True)
        y = Inches(1.65)
        if sl.get("lead"):
            rule = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.9), y + Inches(0.08), Inches(0.08), Inches(1.15))
            rule.fill.solid()
            rule.fill.fore_color.rgb = rgb(TEAL)
            rule.line.fill.background()
            text(s, Inches(1.2), y, Inches(11.1), Inches(1.4), sl["lead"], 28, INK, bold=True)
            y += Inches(1.75)
        if sl.get("table"):
            t = sl["table"]
            ncols = len(t["columns"])
            rows = len(t["rows"]) + 1
            row_h = Inches(0.68)
            shape = s.shapes.add_table(rows, ncols, Inches(0.9), y, Inches(11.5), row_h * rows)
            tbl = shape.table
            for ci, col in enumerate(t["columns"]):
                cell = tbl.cell(0, ci)
                cell.fill.solid()
                cell.fill.fore_color.rgb = rgb(INK)
                cell.text = col
                run = cell.text_frame.paragraphs[0].runs[0]
                run.font.size, run.font.bold, run.font.name = Pt(17), True, FONT
                run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            for ri, row in enumerate(t["rows"], 1):
                for ci in range(ncols):
                    cell = tbl.cell(ri, ci)
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = rgb(PAPER if ri % 2 == 0 else (0xFF, 0xFF, 0xFF))
                    cell.text = row[ci] if ci < len(row) else ""
                    for run in cell.text_frame.paragraphs[0].runs:
                        run.font.size, run.font.name = Pt(17), FONT
                        run.font.color.rgb = rgb(INK)
                    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            y += row_h * rows + Inches(0.3)
        if sl.get("bullets"):
            box = s.shapes.add_textbox(Inches(0.9), y, Inches(11.4), H - y - Inches(1.0))
            tf = box.text_frame
            tf.word_wrap = True
            size = 26 if len(sl["bullets"]) <= 4 else 22
            for bi, b in enumerate(sl["bullets"]):
                p = tf.paragraphs[0] if bi == 0 else tf.add_paragraph()
                p.space_after = Pt(18)
                bullet(p, TEAL)
                r = p.add_run()
                r.text = b
                r.font.size, r.font.name = Pt(size), FONT
                r.font.color.rgb = rgb(INK)
        text(s, Inches(0.9), Inches(6.85), Inches(9), Inches(0.35), deck["title"], 11, MUTED)
        text(s, Inches(10.4), Inches(6.85), Inches(2.0), Inches(0.35), f"{idx + 1} / {total + 1}", 11, MUTED,
             align=PP_ALIGN.RIGHT)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
