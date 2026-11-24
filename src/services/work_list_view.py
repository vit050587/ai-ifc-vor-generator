"""Public quantity/source view; original calculation rows retain stable addresses."""
from pathlib import Path
from copy import deepcopy
import re

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.comments import Comment
from openpyxl.workbook.defined_name import DefinedName

from .estimate_export import _metadata, _rows, _position_groups, public_group_name
from .estimate_consolidation import merge_rows
from .quantity_provenance import text, joined
from .construction_method import construction_method
from .work_sections import sectioned_positions


SHEET = 'Перечень работ'
HEADERS = ['Шифр ТСН', 'Наименование работ и ресурсов', 'Ед. изм.', 'Количество', 'Источник объёма']
REBAR_SHEET = 'Армирование по секциям'
REBAR_HEADERS = HEADERS
VERSION = '6'
VERSION_NAME = '_work_list_view_version'
_SHARED = re.compile(r'\s*[—–-]\s*общий\s+объ[её]м\s+армирования\s+по\s+ВОР\s+заказчика:\s*', re.I)


def reinforcement_group(name):
    """Separate the construction name from its original section context."""
    parts = _SHARED.split(public_group_name(name), maxsplit=1)
    if len(parts) > 1 and re.search('приям', parts[1], re.I):
        parts[0] = re.sub(r'фундаментная плита', 'Приямки', parts[0], flags=re.I)
    return parts[0].strip(), parts[1].strip() if len(parts) > 1 else ''


def _is_rebar(item):
    from .final_quantity_rules import is_rebar
    return is_rebar(item)


def _source(item):
    work = item.get('work') or {}
    return (joined(item.get('volume_sources') or []) or text(work.get('volume_source')) or
            ('Источник исходного числа не сохранён в этом расчёте' if item['quantity_exact'] is not None
             else 'Объём не определён'))


def _merge_public_rows(target, other):
    """Collapse missing public quantities without turning them into zero.

    Calculation and estimate rows remain separate on their original sheets.
    Known and missing values have different keys and are never added together.
    """
    if target.get('quantity_exact') is None and other.get('quantity_exact') is None:
        target.setdefault('source_rows', [target['source_row']]).extend(
            other.get('source_rows') or [other['source_row']])
        target['volume_sources'] = list(dict.fromkeys([
            *(target.get('volume_sources') or [_source(target)]),
            *(other.get('volume_sources') or [_source(other)])]))
        return
    merge_rows(target, other)


def ensure_current(path):
    """Refresh an older saved public view without rerunning rate selection."""
    path = Path(path)
    if path.name not in ('Финальный_перечень_работ.xlsx','ОБЩИЙ_Финальный_перечень_работ.xlsx'):
        return
    from .vor_generation.formation_cache import session_lock
    with session_lock(path):
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            marker = book.defined_names.get(VERSION_NAME)
            current = SHEET in book.sheetnames and marker is not None and marker.attr_text == VERSION
            if current:
                rows = book[SHEET].iter_rows(values_only=True)
                current = list(next(rows, ())) == HEADERS
                if current:
                    current = not any(re.search(r'\[(?:Высота от земли:|Этажи:)', str(row[1] or '')) or (row[0] and (str(row[4] or '').startswith('Расчётные данные:') or str(row[4]).lower() == 'nan')) for row in rows)
        finally:
            book.close()
        from .drawing_provenance import load_context
        if load_context(path.parent):
            # Drawing sources also refresh historical public views.
            current = False
        if not current:
            build(path)


def list_positions(records, *, resource_links=None):
    """Collapse internal height partitions; keep the original construction groups."""
    groups = {}
    heading = ''
    for item, resources in _position_groups(records, resource_links=resource_links):
        if 'heading' in item:
            heading = public_group_name(item['heading'])
            continue
        facts = item.get('facts') or {}
        name = facts.get('work_list_group') or public_group_name(item.get('group') or heading)
        name, source_scope = reinforcement_group(name)
        clean_facts = dict(facts, name=name, Name=name, ObjectType=name, work_list_group=name) if source_scope else facts
        scope = (facts.get('part'), tuple(facts.get('path') or ()), name, construction_method(clean_facts))
        group = groups.setdefault(scope, {'heading': name, 'works': {}})
        key = (item['code'], item.get('unit'))
        if item['code'].startswith('1.'):
            key += (item.get('title'),)
        if item.get('quantity_exact') is None:
            key += ('missing', item.get('title'))
        works = group['works']
        if key not in works:
            works[key] = (deepcopy(item), {})
            if source_scope:
                # The main sheet groups construction totals; section names
                # belong to the separate reinforcement detail sheet only.
                parent = works[key][0]
                parent['group'] = name
                parent['facts'] = clean_facts
        else:
            _merge_public_rows(works[key][0], item)
        parent, children = works[key]
        for child in resources:
            child_key = (child['code'], child.get('unit'), child.get('title'))
            if child.get('quantity_exact') is None:
                child_key += ('missing',)
            if child_key not in children:
                children[child_key] = deepcopy(child)
            else:
                _merge_public_rows(children[child_key], child)
    for group_id, group in enumerate(groups.values()):
        yield {'heading': group['heading']}, []
        for parent, children in group['works'].values():
            # Displayed groups have one identity even when their calculation
            # rows came from different IFC height/storey partitions.
            parent['group_id'] = group_id
            for child in children.values():
                child['group_id'] = group_id
            yield parent, list(children.values())


def _write_rebar_detail(book, records):
    from .estimate_export import _resource_links
    resource_links = _resource_links()
    if REBAR_SHEET in book.sheetnames:
        del book[REBAR_SHEET]
    sheet = book.create_sheet(REBAR_SHEET, 1)
    sheet.append(REBAR_HEADERS)
    locations = {}
    groups = {}
    for item in records:
        if 'code' not in item or not _is_rebar(item):
            continue
        facts = item.get('facts') or {}
        name, scope = reinforcement_group(facts.get('work_list_group') or item.get('group') or '')
        # Retain the source's scope as written: no invented allocation to
        # individual sections from a combined "sections 1–8" source total.
        if not scope:
            scope = (text(facts.get('source_context')) or text(facts.get('Этаж')) or
                     text(facts.get('level')) or 'Без разбивки по секциям в исходных данных')
        clean_facts = dict(facts, name=name, Name=name, ObjectType=name, work_list_group=name)
        key = (facts.get('part'), tuple(facts.get('path') or ()), name, construction_method(clean_facts))
        if key not in groups:
            representative = deepcopy(item)
            representative.update(group=name, facts=clean_facts)
            groups[key] = dict(name=name, representative=representative, scopes={})
        groups[key]['scopes'].setdefault(scope, []).append(item)
    positions = []
    for group in groups.values():
        group['representative']['detail_scopes'] = group['scopes']
        positions.extend([({'heading': group['name']}, []), (group['representative'], [])])
    for record, _ in sectioned_positions(positions):
        if 'heading' in record:
            _heading(sheet, record['heading'])
            continue
        for scope, members in record['detail_scopes'].items():
            _heading(sheet, scope, scope=True)
            for parent, resources in list_positions(members, resource_links=resource_links):
                if 'heading' in parent:
                    continue
                for item in (parent, *resources):
                    sheet.append([item['code'], item['title'], item['unit'], item['quantity_exact'], _source(item)])
                    source_rows = item.get('source_rows') or [item['source_row']]
                    for source_row in source_rows:
                        locations[source_row] = sheet.max_row
                    sheet.cell(sheet.max_row, 5).hyperlink = f"#'Данные'!D{item['source_row']}"
                    sheet.cell(sheet.max_row, 5).comment = Comment(
                        'Расчётные строки: ' + ', '.join(f'Данные!D{r}' for r in source_rows), 'Сервис')
    _format(sheet, (18, 85, 14, 18, 65), quantity_column=4)
    return locations


def _heading(sheet, title, *, scope=False):
    sheet.append(['', title])
    row = sheet.max_row
    sheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    for cell in sheet[row]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='F2F4F7' if scope else 'E7EDF4')
    if scope:
        sheet.cell(row, 2).alignment = Alignment(indent=1, vertical='top', wrap_text=True)


def _format(sheet, widths, quantity_column):
    from openpyxl.utils import get_column_letter
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='D5E0ED')
    for column, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(column)].width = width
    for cells in sheet:
        lines = 2
        for cell in cells:
            cell.alignment = Alignment(vertical='top', wrap_text=True, indent=cell.alignment.indent)
            width = widths[cell.column - 1]
            lines = max(lines, sum(max(1, (len(part) + int(width) - 1) // int(width))
                                   for part in str(cell.value or '').splitlines()))
        sheet.row_dimensions[cells[0].row].height = min(409, lines * 15)
        sheet.cell(cells[0].row, quantity_column).number_format = '#,##0.########'
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    sheet.print_title_rows = '1:1'
    sheet.page_setup.orientation = 'landscape'


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
        rebar_locations = _write_rebar_detail(book, records)
        for record, resources in sectioned_positions(list_positions(records)):
            if 'heading' in record:
                _heading(sheet, record['heading'])
                continue
            for item in (record, *resources):
                source = _source(item)
                quantity, unit = item['quantity_exact'], item['unit']
                sheet.append([item['code'], item['title'], unit, quantity, source])
                sheet.cell(sheet.max_row, 5).comment = Comment(
                    'Расчётные строки: ' + ', '.join(f"Данные!D{r}" for r in item.get('source_rows') or [item['source_row']]), 'Сервис')
                sheet.cell(sheet.max_row, 5).hyperlink = f"#'Данные'!D{item['source_row']}"
                if _is_rebar(item) and item['source_row'] in rebar_locations:
                    sheet.cell(sheet.max_row, 5).hyperlink = f"#'{REBAR_SHEET}'!A{rebar_locations[item['source_row']]}"
        _format(sheet, (18, 85, 14, 18, 65), quantity_column=4)
        # All readers use this stable sheet explicitly. Costs remain available
        # to estimate calculation while the public work list displays sources.
        if 'Данные' in book.sheetnames:
            book['Данные'].sheet_state = 'hidden'
        book.active = 0
        book.defined_names.add(DefinedName(VERSION_NAME, attr_text=VERSION))
        book.save(path)
    finally:
        book.close()
    return str(path)
