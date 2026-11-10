"""Read source cells and their merged headers; never ask a model to copy numbers."""
from dataclasses import dataclass, field
from pathlib import Path
import re


def clean(value):
    return re.sub(r'\s+', ' ', str(value if value is not None else '')).strip()


@dataclass
class Table:
    file: str
    location: str
    index: int
    rows: list
    expanded: list
    boxes: dict = field(default_factory=dict)
    page: int | None = None
    sheet: str = ''
    row_offset: int = 0
    context: str = ''

    def source(self, row, column):
        return dict(file=self.file, page=self.page, sheet=self.sheet, table=self.index,
                    row=row + 1 + self.row_offset, column=column + 1,
                    cell=f'R{row + 1 + self.row_offset}C{column + 1}',
                    bbox=self.boxes.get((row, column)), text=clean(self.rows[row][column]))


def read_tables(path):
    path = Path(path)
    if path.suffix.lower() == '.pdf':
        yield from _pdf(path)
    elif path.suffix.lower() == '.xlsx':
        yield from _xlsx(path)
    else:
        raise ValueError('Документ заказчика должен быть PDF или XLSX.')


def embedded_grid(rows):
    """A drawing-frame cell contains a whole flattened table, not a work name."""
    for row in rows:
        for value in row:
            text=str(value or '')
            if len(text)<500 or text.count('\n')<8:
                continue
            normalized=clean(text).lower().replace('ё','е')
            if (re.search(r'наименован|описание\s+работ',normalized)
                    and re.search(r'ед\.?\s*изм|количество|об[ъь]ем',normalized)
                    and len(re.findall(r'(?<!\w)\d+(?:[.,]\d+)?(?!\w)',normalized))>=12):
                return True
    return False


def _grid_header(rows):
    return any(len(clean(cell))<160 and re.search(r'наименован|описание\s+работ|^марка$',clean(cell),re.I)
               for row in rows[:50] for cell in row if cell)


def _page_tables(page):
    """Recover the inner grid before dropping a drawing frame or calling a model."""
    tables=list(page.find_tables())
    def extracted():
        return [(table,table.extract()) for table in tables]
    found=extracted()
    frames=[table for table,rows in found if rows and embedded_grid(rows)]
    usable=[table for table,rows in found if rows and not embedded_grid(rows) and _grid_header(rows)]
    def nested(inner,outer):
        return (inner.bbox[0]>=outer.bbox[0] and inner.bbox[1]>=outer.bbox[1]
                and inner.bbox[2]<=outer.bbox[2] and inner.bbox[3]<=outer.bbox[3])
    uncovered=[frame for frame in frames if not any(nested(grid,frame) for grid in usable)]
    if uncovered:
        # A changed PDF renderer/library tolerance may find only the border.
        # Read actual ruling lines again; never reconstruct quantities from LLM text.
        settings=[dict(vertical_strategy='lines',horizontal_strategy='lines',
                       snap_tolerance=3,join_tolerance=3,intersection_tolerance=3),
                  dict(vertical_strategy='lines',horizontal_strategy='lines',
                       snap_tolerance=1,join_tolerance=1,intersection_tolerance=1),
                  dict(vertical_strategy='lines_strict',horizontal_strategy='lines_strict',
                       snap_tolerance=2,join_tolerance=2,intersection_tolerance=3)]
        for options in settings:
            candidates=page.find_tables(table_settings=options)
            recovered=[table for table in candidates if (rows:=table.extract())
                       and not embedded_grid(rows) and _grid_header(rows)
                       and any(nested(table,frame) and (table.bbox[2]-table.bbox[0])*(table.bbox[3]-table.bbox[1])
                               <(frame.bbox[2]-frame.bbox[0])*(frame.bbox[3]-frame.bbox[1])*.99 for frame in uncovered)]
            if recovered:
                tables.extend(recovered)
                found=extracted()
                break
        else:
            # Keep the unresolved frame as an explicit diagnostic, so later
            # readable pages are still imported without invoking an LLM for it.
            pass
    result=[]
    for index,(table,rows) in enumerate(found):
        if not rows:
            continue
        if embedded_grid(rows):
            # A nested grid replaces this frame; unresolved frames are reported.
            if not any(other is not table and other_rows and not embedded_grid(other_rows) and _grid_header(other_rows) and nested(other,table)
                       for other,other_rows in found):
                result.append((index,table,rows))
            continue
        x0,y0,x1,y1=table.bbox
        # Only a genuine nested document grid can replace its enclosing frame.
        if any(other is not table and other_rows and not embedded_grid(other_rows) and _grid_header(other_rows)
               and other.bbox[0]>=x0 and other.bbox[1]>=y0 and other.bbox[2]<=x1 and other.bbox[3]<=y1
               and (other.bbox[2]-other.bbox[0])*(other.bbox[3]-other.bbox[1])<(x1-x0)*(y1-y0)*.99
               for other,other_rows in found):
            continue
        result.append((index,table,rows))
    return result


def _pdf(path):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for index, table, rows in _page_tables(page):
                width = max(map(len, rows))
                rows = [row + [None]*(width-len(row)) for row in rows]
                xs = sorted({round(v, 5) for box in table.cells for v in (box[0], box[2])})
                centers = [(a+b)/2 for a,b in zip(xs,xs[1:])]
                expanded, boxes = [], {}
                for ri, (row, physical) in enumerate(zip(rows, table.rows)):
                    expanded_row = list(row)
                    for ci in range(width):
                        box = physical.cells[ci] if ci < len(physical.cells) else None
                        if box:
                            boxes[(ri,ci)] = list(box)
                        if row[ci] is None and ci < len(centers):
                            for start, span in enumerate(physical.cells):
                                if span and span[0] <= centers[ci] <= span[2]:
                                    expanded_row[ci] = row[start]
                                    break
                    expanded.append(expanded_row)
                yield Table(path.name, f'стр. {page.page_number}', index, rows, expanded, boxes, page.page_number, context=page.extract_text() or '')


def _xlsx(path):
    from openpyxl import load_workbook
    book = load_workbook(path, data_only=True)
    try:
        for index, sheet in enumerate(book):
            if sheet.sheet_state != 'visible':
                continue
            rows = [list(row) for row in sheet.iter_rows(values_only=True)]
            if not rows:
                continue
            expanded = [list(row) for row in rows]
            for area in sheet.merged_cells.ranges:
                value = sheet.cell(area.min_row, area.min_col).value
                for ri in range(area.min_row-1, area.max_row):
                    for ci in range(area.min_col-1, area.max_col):
                        expanded[ri][ci] = value
            yield Table(path.name, f'лист {sheet.title}', index, rows, expanded, sheet=sheet.title)
    finally:
        book.close()
