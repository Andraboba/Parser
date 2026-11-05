# -*- coding: utf-8 -*-
"""Небольшой конструктор документов Word для планов реализации."""

from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

ACCENT = RGBColor(0x1F, 0x3B, 0x63)
ACCENT2 = RGBColor(0x2E, 0x4A, 0x73)
MUTED = RGBColor(0x59, 0x59, 0x59)
DARK = RGBColor(0x22, 0x22, 0x22)
BODY = "Times New Roman"
MONO = "Consolas"

CONTENT_WIDTH_CM = 16.6


def _set_run_font(run, name, size=None, bold=None, italic=None, color=None):
    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), name)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if color is not None:
        run.font.color.rgb = color


def _cell_shade(cell, hex_fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tc_pr.append(shd)


def _repeat_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    hdr = OxmlElement("w:tblHeader")
    hdr.set(qn("w:val"), "true")
    tr_pr.append(hdr)


def _page_number(paragraph):
    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_end)


class Doc:
    def __init__(self, footer_text):
        self.d = Document()
        self._setup_page(footer_text)
        self._setup_styles()

    # ---------- page / styles ----------
    def _setup_page(self, footer_text):
        for section in self.d.sections:
            section.page_width = Cm(21.0)
            section.page_height = Cm(29.7)
            section.top_margin = Cm(2.0)
            section.bottom_margin = Cm(2.0)
            section.left_margin = Cm(2.2)
            section.right_margin = Cm(2.2)
            section.footer_distance = Cm(1.0)

            footer_p = section.footer.paragraphs[0]
            footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = footer_p.add_run(footer_text + "  ·  стр. ")
            _set_run_font(r, BODY, size=8.5, color=MUTED)
            _page_number(footer_p)

    def _setup_styles(self):
        styles = self.d.styles

        normal = styles["Normal"]
        normal.font.name = BODY
        normal.font.size = Pt(12)
        rpr = normal.element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = OxmlElement("w:rFonts")
            rpr.insert(0, rfonts)
        for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            rfonts.set(qn(attr), BODY)
        pf = normal.paragraph_format
        pf.space_before = Pt(0)
        pf.space_after = Pt(6)
        pf.line_spacing = 1.12

        for name, size, color, before, after in (
            ("Heading 1", 15.5, ACCENT, 18, 8),
            ("Heading 2", 13.0, ACCENT2, 14, 6),
        ):
            st = styles[name]
            st.font.name = BODY
            st.font.size = Pt(size)
            st.font.bold = True
            st.font.italic = False
            st.font.color.rgb = color
            st.paragraph_format.space_before = Pt(before)
            st.paragraph_format.space_after = Pt(after)
            st.paragraph_format.keep_with_next = True
            st.paragraph_format.line_spacing = 1.05
            srpr = st.element.get_or_add_rPr()
            srfonts = srpr.find(qn("w:rFonts"))
            if srfonts is None:
                srfonts = OxmlElement("w:rFonts")
                srpr.insert(0, srfonts)
            for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
                srfonts.set(qn(attr), BODY)
            # убрать «тему» шрифта, иначе Word может подставить свой
            for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
                if srfonts.get(qn(attr)) is not None:
                    del srfonts.attrib[qn(attr)]

    # ---------- blocks ----------
    def title(self, text, subtitle=None):
        p = self.d.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(4)
        r = p.add_run(text)
        _set_run_font(r, BODY, size=22, bold=True, color=ACCENT)

        if subtitle:
            p2 = self.d.add_paragraph()
            p2.paragraph_format.space_after = Pt(10)
            r2 = p2.add_run(subtitle)
            _set_run_font(r2, BODY, size=12, italic=True, color=MUTED)

    def meta(self, rows):
        t = self.d.add_table(rows=0, cols=2)
        t.style = "Table Grid"
        t.alignment = WD_TABLE_ALIGNMENT.LEFT
        t.autofit = False
        for key, value in rows:
            row = t.add_row()
            c0, c1 = row.cells
            c0.width = Cm(4.3)
            c1.width = Cm(12.3)
            _cell_shade(c0, "EEF2F8")
            p0 = c0.paragraphs[0]
            p0.paragraph_format.space_after = Pt(1)
            r0 = p0.add_run(key)
            _set_run_font(r0, BODY, size=9.5, bold=True, color=ACCENT2)
            p1 = c1.paragraphs[0]
            p1.paragraph_format.space_after = Pt(1)
            r1 = p1.add_run(value)
            _set_run_font(r1, BODY, size=9.5, color=DARK)
        self.spacer(6)

    def h1(self, text):
        self.d.add_heading(text, level=1)

    def h2(self, text):
        self.d.add_heading(text, level=2)

    def p(self, text, italic=False, bold=False, size=None, color=None, space_after=6):
        par = self.d.add_paragraph()
        par.paragraph_format.space_after = Pt(space_after)
        par.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        r = par.add_run(text)
        _set_run_font(r, BODY, size=size, bold=bold, italic=italic, color=color or DARK)
        return par

    def note(self, text):
        par = self.d.add_paragraph()
        par.paragraph_format.space_before = Pt(4)
        par.paragraph_format.space_after = Pt(8)
        par.paragraph_format.left_indent = Cm(0.3)
        par.paragraph_format.right_indent = Cm(0.3)
        par.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        ppr = par._p.get_or_add_pPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), "EAF1E9")
        ppr.append(shd)
        r = par.add_run(text)
        _set_run_font(r, BODY, size=11, italic=False, color=DARK)
        return par

    def bullets(self, items, size=None):
        for item in items:
            par = self.d.add_paragraph(style="List Bullet")
            par.paragraph_format.space_after = Pt(3)
            par.paragraph_format.left_indent = Cm(0.75)
            par.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            r = par.add_run(item)
            _set_run_font(r, BODY, size=size or 12, color=DARK)

    def num(self, items, start=1):
        for i, item in enumerate(items, start=start):
            par = self.d.add_paragraph()
            par.paragraph_format.left_indent = Cm(0.85)
            par.paragraph_format.first_line_indent = Cm(-0.85)
            par.paragraph_format.space_after = Pt(3)
            par.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            r = par.add_run(f"{i}.  ")
            _set_run_font(r, BODY, size=12, bold=True, color=ACCENT2)
            r2 = par.add_run(item)
            _set_run_font(r2, BODY, size=12, color=DARK)

    def table(self, headers, rows, widths=None, font_size=9.5, header_fill="DCE5F2"):
        t = self.d.add_table(rows=1, cols=len(headers))
        t.style = "Table Grid"
        t.autofit = False
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        n = len(headers)
        if widths is None:
            widths = [CONTENT_WIDTH_CM / n] * n

        hdr = t.rows[0]
        _repeat_header(hdr)
        for i, text in enumerate(headers):
            cell = hdr.cells[i]
            cell.width = Cm(widths[i])
            _cell_shade(cell, header_fill)
            par = cell.paragraphs[0]
            par.paragraph_format.space_before = Pt(2)
            par.paragraph_format.space_after = Pt(2)
            r = par.add_run(text)
            _set_run_font(r, BODY, size=font_size, bold=True, color=ACCENT)

        for row_data in rows:
            row = t.add_row()
            for i, text in enumerate(row_data):
                cell = row.cells[i]
                cell.width = Cm(widths[i])
                par = cell.paragraphs[0]
                par.paragraph_format.space_before = Pt(2)
                par.paragraph_format.space_after = Pt(2)
                par.alignment = WD_ALIGN_PARAGRAPH.LEFT
                r = par.add_run(str(text))
                _set_run_font(r, BODY, size=font_size, color=DARK)
        self.spacer(8)
        return t

    def code(self, text, size=8.8):
        par = self.d.add_paragraph()
        par.paragraph_format.space_before = Pt(4)
        par.paragraph_format.space_after = Pt(8)
        par.paragraph_format.left_indent = Cm(0.2)
        par.paragraph_format.line_spacing = 1.0
        ppr = par._p.get_or_add_pPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), "F4F4F4")
        ppr.append(shd)
        lines = text.strip("\n").split("\n")
        for i, line in enumerate(lines):
            r = par.add_run(line)
            _set_run_font(r, MONO, size=size, color=RGBColor(0x1A, 0x1A, 0x1A))
            if i < len(lines) - 1:
                r.add_break()
        return par

    def spacer(self, pts=6):
        par = self.d.add_paragraph()
        par.paragraph_format.space_after = Pt(0)
        par.paragraph_format.space_before = Pt(0)
        par.paragraph_format.line_spacing = 1.0
        r = par.add_run("")
        _set_run_font(r, BODY, size=pts)

    def page_break(self):
        self.d.add_page_break()

    def save(self, path):
        self.d.save(path)
        return path
