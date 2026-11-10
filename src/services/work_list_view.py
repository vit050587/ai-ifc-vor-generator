"""Public quantity/source view; original calculation rows retain stable addresses."""
from pathlib import Path
from copy import deepcopy
import re

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .estimate_export import _metadata, _rows, _position_groups, public_group_name
from .estimate_consolidation import merge_rows
from .ifc_quantities import SOURCE
from .construction_method import construction_method
from .work_sections import sectioned_positions


SHEET = 'Перечень работ'
HEADERS = ['Шифр ТСН', 'Наименование работ и ресурсов', 'Ед. изм.', 'Количество', 'Источник объёма']


def ensure_current(path):
    """Refresh an older saved public view without rerunning rate selection."""
    path = Path(path)
    if path.name not in ('Финальный_перечень_работ.xlsx','ОБЩИЙ_Финальный_перечень_работ.xlsx'):
        return
    from .vor_generation.formation_cache import session_lock
    with session_lock(path):
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            current = SHEET in book.sheetnames
            if current:
                rows = book[SHEET].iter_rows(values_only=True)
                current = list(next(rows, ())) == HEADERS
                if current:
                    current = not any(re.search(r'\[(?:Высота от земли:|Этажи:)', str(row[1] or '')) for row in rows)
        finally:
            book.close()
        if not current:
            build(path)


def list_positions(records):
    """Collapse internal height partitions; keep the original construction groups."""
    groups = {}
    heading = ''
    for item, resources in _position_groups(records):
        if 'heading' in item:
            heading = public_group_name(item['heading'])
            continue
        facts = item.get('facts') or {}
        name = facts.get('work_list_group') or public_group_name(item.get('group') or heading)
        scope = (facts.get('part'), tuple(facts.get('path') or ()), name, construction_method(facts))
        group = groups.setdefault(scope, {'heading': name, 'works': {}})
        key = (item['code'], item.get('unit'))
        if item.get('quantity_exact') is None:
            key += (item['source_row'],)
        works = group['works']
        if key not in works:
            works[key] = (deepcopy(item), {})
        else:
            merge_rows(works[key][0], item)
        parent, children = works[key]
        for child in resources:
            child_key = (child['code'], child.get('unit'))
            if child.get('quantity_exact') is None:
                child_key += (child['source_row'],)
            if child_key not in children:
                children[child_key] = deepcopy(child)
            else:
                merge_rows(children[child_key], child)
    for group in groups.values():
        yield {'heading': group['heading']}, []
        for parent, children in group['works'].values():
            yield parent, list(children.values())


def build(path):
    path = Path(path)
    _, metadata = _metadata(path.parent)
    records = _rows(path, metadata)
    book = load_workbook(path)
    try:
        if SHEET in book.sheetnames:
            del book[SHEET]
        sheet = book.create_sheet(SHEET, 0)
        sheet.append(HEADERS)
        for record, resources in sectioned_positions(list_positions(records)):
            if 'heading' in record:
                sheet.append(['', record['heading']])
                row = sheet.max_row
                sheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
                for cell in sheet[row]:
                    cell.font = Font(bold=True)
                    cell.fill = PatternFill('solid', fgColor='E7EDF4')
                continue
            for item in (record, *resources):
                work, facts = item.get('work') or {}, item.get('facts') or {}
                source = '; '.join(item.get('volume_sources') or []) or work.get('volume_source') or facts.get(SOURCE)
                source = str(source) if source else f"Расчётные данные: Данные!D{item['source_row']}"
                if item.get('source_rows'):
                    source += '\nСумма строк: ' + ', '.join(f"Данные!D{r}" for r in item['source_rows'])
                quantity, unit = item['quantity_exact'], item['unit']
                sheet.append([item['code'], item['title'], unit, quantity, source])
                sheet.cell(sheet.max_row, 5).hyperlink = f"#'Данные'!D{item['source_row']}"
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill('solid', fgColor='D5E0ED')
        for column, width in zip('ABCDE', (18, 85, 14, 18, 65)):
            sheet.column_dimensions[column].width = width
        for row in sheet:
            for cell in row:
                cell.alignment = Alignment(vertical='top', wrap_text=True)
            sheet.row_dimensions[row[0].row].height = 42
        sheet.auto_filter.ref = sheet.dimensions
        sheet.print_title_rows = '1:1'
        sheet.page_setup.orientation = 'landscape'
        # All readers use this stable sheet explicitly. Costs remain available
        # to estimate calculation while the public work list displays sources.
        if 'Данные' in book.sheetnames:
            book['Данные'].sheet_state = 'hidden'
        book.active = 0
        book.save(path)
    finally:
        book.close()
    return str(path)
