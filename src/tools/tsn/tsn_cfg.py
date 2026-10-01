"""Номер сборника ТСН, с которым работают все модули разбора.

Задаётся переменной окружения TSN_N (build.py ставит её сам по PDF).
Шифры в книге: «<N>-<таблица>-<расценка>», например 15-96-1 или 6-97-3."""
import os, re

N = os.environ.get("TSN_N", "15")
NE = re.escape(N)


def detect(pdf_path):
    """Номер и название сборника по первой странице PDF (или по имени файла)."""
    n = title = code = None
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            txt = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])
        m = re.search(r"Сборник\s+(\d+)\s*\n\s*([^\n]+)", txt)
        if m:
            n, title = m.group(1), m.group(2).strip()
        m = re.search(r"(ТСН-\d{4}\.\d+-\d+)", txt)
        if m:
            code = m.group(1)
    except Exception:
        pass
    base = os.path.basename(pdf_path)
    if n is None:
        m = re.search(r"Сборник\s*(\d+)", base) or re.search(r"ТСН-\d{4}\.\d+-(\d+)", base)
        n = m.group(1) if m else "15"
    if title is None:
        m = re.search(r"Сборник\s*\d+\.\s*(.+?)\.pdf$", base, re.I)
        title = m.group(1) if m else ""
    if code is None:
        m = re.search(r"(ТСН-\d{4}\.\d+-\d+)", base)
        code = m.group(1) if m else f"ТСН-2001.3-{n}"
    return n, title, code
