"""Comparison sheets based on the saved VOR, with quantities shown first."""
from copy import copy
from pathlib import Path
import re

from openpyxl import load_workbook
from openpyxl.formula.translate import Translator
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.dimensions import ColumnDimension


def copy_cell_style(source, target):
    # Register components in the destination workbook, including customer styles.
    for field in ('font', 'fill', 'border', 'alignment', 'protection'):
        setattr(target, field, copy(getattr(source, field)))
    target.number_format = source.number_format


def difference_format(percent):
    suffix = '' if percent is None else f' ({percent:+.2f}%)'
    return f'+0.###"{suffix}";-0.###"{suffix}";0"{suffix}"'


def compact_calculation(value, quantity=None, unit=''):
    if not isinstance(value, str) or value.startswith('='):
        return value
    text = re.sub(r'\s+', ' ', re.sub(r'\[[^\]]*\]', '', value)).strip()
    if len(text) <= 220:
        return text
    total = '' if quantity is None else f' Итого: {quantity:g} {unit}'.rstrip()
    return text[:180].rstrip() + '…' + total


def reorder_comparison_sheet(sheet, order):
    """Move cells and their styles together, retaining section headings."""
    merged = list(sheet.merged_cells.ranges)
    for area in merged:
        sheet.unmerge_cells(str(area))
    dimensions = {index: copy(sheet.column_dimensions.get(get_column_letter(index))) for index in order}
    for cells in list(sheet.iter_rows(max_col=len(order))):
        source = [copy(cell) for cell in cells]
        heading = source[0].value is not None and all(cell.value is None for cell in source[1:])
        for column, original in enumerate(order, 1):
            cell = sheet.cell(cells[0].row, column)
            old = source[original - 1]
            cell.value = old.value
            cell._style = copy(old._style)
            cell.comment = copy(old.comment)
            cell.hyperlink = copy(old.hyperlink)
        if heading:
            for cell in sheet[cells[0].row]:
                cell.value = None
            sheet.cell(cells[0].row, 1).value = source[0].value
            sheet.cell(cells[0].row, 1)._style = copy(source[0]._style)
    sheet.column_dimensions.clear()
    for column, original in enumerate(order, 1):
        letter = get_column_letter(column)
        dimension = dimensions[original] or ColumnDimension(sheet, index=letter,
                                                           width=sheet.sheet_format.defaultColWidth or 13)
        dimension.index = letter
        dimension.min = dimension.max = column
        sheet.column_dimensions[letter] = dimension
    for area in merged:
        if area.min_row == 1 or area.max_col > area.min_col:
            sheet.merge_cells(start_row=area.min_row, end_row=area.max_row, start_column=1, end_column=len(order))
        else:
            column = order.index(area.min_col) + 1
            sheet.merge_cells(start_row=area.min_row, end_row=area.max_row, start_column=column, end_column=column)


def _heading_level(value):
    value = value.lower()
    if value.startswith('подраздел '):
        return 2
    if value.startswith('раздел '):
        return 1
    if value.startswith(('подземная часть', 'надземная часть', 'автостоянка', 'часть здания:')):
        return 0
    return 3


def typical_views(typical):
    """Build the discrepancy and full VOR sheets using source cell styles."""
    sources = typical.get('sourceFiles') or []
    if len(sources) < 2:
        raise ValueError('Для оформления сравнения нужны исходные файлы типового ВОР')
    book = load_workbook(Path(sources[0]))
    customer = load_workbook(Path(sources[1]))
    sheet = book['ВОР КР_ЖС']
    for other in list(book):
        if other != sheet:
            book.remove(other)
    sheet.title = 'По типовому ВОР'
    rows = typical['rows']
    include_ai = 'aiReasonVersion' in typical or any(row.get('aiReason') for row in rows)
    by_source = {row['generatedSourceRow']: row for row in rows if row.get('generatedSourceRow') is not None}
    # Each record retains the source cell objects until both views are written.
    records = []
    ancestors = {}
    for number in range(1, sheet.max_row + 1):
        cells = [copy(sheet.cell(number, column)) for column in range(1, 7)]
        value = str(cells[0].value or '')
        if number > 3 and value and not cells[1].value:
            level = _heading_level(value)
            ancestors = {key: index for key, index in ancestors.items() if key < level}
            ancestors[level] = len(records)
        records.append(dict(cells=cells, dimension=copy(sheet.row_dimensions[number]), source=number,
                            comparison=by_source.get(number), ancestors=list(ancestors.values())))
    # Keep unmatched resources beside their owning operation. A different
    # concrete grade is a real discrepancy, not a reason to move it to the tail.
    display_order = list(range(len(records)))
    generated_indices = {record['source']: i for i, record in enumerate(records)}
    generated_rows = [(i, row) for i, row in enumerate(rows) if row.get('generatedSourceRow') is not None]
    insertions = {}
    contexts = {}
    customer_sheet = customer['ВОР КР_ЖС']
    customer_headings = {}
    active_headings = {}
    for number in range(4, customer_sheet.max_row + 1):
        value = str(customer_sheet.cell(number, 1).value or '')
        if value and not customer_sheet.cell(number, 2).value:
            level = _heading_level(value)
            active_headings = {key: index for key, index in active_headings.items() if key < level}
            active_headings[level] = number
        customer_headings[number] = list(active_headings.values())
    def context_of(row):
        return tuple(row.get(key) or '' for key in ('part', 'section', 'subsection', 'elementGroup'))

    for position, row in enumerate(rows):
        if row.get('generatedSourceRow') is not None or row.get('customerSourceRow') is None or row.get('isSeparator'):
            continue
        if customer_sheet.row_dimensions[row['customerSourceRow']].hidden:
            row['sourceOnly'] = True
            continue
        context = context_of(row)
        exact = [(i, candidate) for i, candidate in generated_rows if context_of(candidate) == context]
        prefix = 4
        candidates = exact
        while not candidates and prefix:
            prefix -= 1
            candidates = [(i, candidate) for i, candidate in generated_rows if context_of(candidate)[:prefix] == context[:prefix]]
        previous = [(i, candidate) for i, candidate in candidates if i < position]
        anchor_row = (previous[-1] if previous else candidates[0]) if candidates else None
        after = bool(previous) or prefix < 4
        if prefix < 4 and candidates:
            anchor_row = candidates[-1]
        anchor = generated_indices[anchor_row[1]['generatedSourceRow']] if anchor_row else len(display_order)-1
        insertion_key = (anchor, after)
        additions = insertions.setdefault(insertion_key, [])
        context_key = (context, insertion_key)
        if context_key not in contexts:
            ancestors = {_heading_level(str(records[index]['cells'][0].value or '')):index
                         for index in records[anchor]['ancestors']
                         if _heading_level(str(records[index]['cells'][0].value or '')) < prefix}
            for heading in customer_headings[row['customerSourceRow']]:
                level = _heading_level(str(customer_sheet.cell(heading, 1).value or ''))
                if level < prefix:
                    continue
                cells = [copy(customer_sheet.cell(heading, col)) for col in range(1, 7)]
                ancestors[level] = len(records)
                additions.append(len(records))
                records.append(dict(cells=cells, dimension=copy(customer_sheet.row_dimensions[heading]),
                                    source=None, comparison=None, ancestors=list(ancestors.values())))
            contexts[context_key] = dict(ancestors)
        ancestors = contexts[context_key]
        cells = [copy(customer_sheet.cell(row['customerSourceRow'], col)) for col in range(1, 7)]
        cells[3].value = None
        cells[4].value = None
        cells[5].value = None
        additions.append(len(records))
        records.append(dict(cells=cells, dimension=copy(customer_sheet.row_dimensions[row['customerSourceRow']]),
                            source=None, comparison=row, ancestors=list(ancestors.values())))
    display_order = [index for original in display_order
                     for index in [*insertions.get((original, False), []), original, *insertions.get((original, True), [])]]
    # A many-to-one match is displayed on an existing work row. The source
    # sheets retain the individual quantities; no additional total works or
    # headings belong in the typical VOR form.
    hidden = set()
    for row in rows:
        if not row.get('isAggregate'):
            continue
        anchor = generated_indices[row['generatedSourceRows'][0]]
        record = records[anchor]
        record['comparison'] = row
        record['cells'][3].value = row.get('generatedQuantity')
        record['cells'][4].value = row.get('generatedCalculation')
        record['cells'][5].value = row.get('modelReason')
        # The displayed quantity covers the customer scope, rather than just
        # the first IFC thickness contributing to it. Reuse its existing header.
        heading = next((i for i in reversed(record['ancestors'])
                        if _heading_level(str(records[i]['cells'][0].value or '')) == 3), None)
        if heading is not None:
            records[heading]['cells'][0].value = row.get('comparisonObject') or row.get('customerElementGroup') or records[heading]['cells'][0].value
        generated_members = set(row['generatedSourceRows'])
        customer_members = set(row['customerSourceRows'])
        for index in display_order:
            member = records[index].get('comparison') or {}
            if index != anchor and member.get('coveredByAggregate') and (
                    member.get('generatedSourceRow') in generated_members
                    or member.get('customerSourceRow') in customer_members):
                hidden.add(index)
    display_order = [index for index in display_order if index not in hidden]
    merges = list(sheet.merged_cells.ranges)
    different = set(range(min(3, len(records))))
    populated = set(different)
    for index, record in enumerate(records):
        if index in hidden:
            continue
        row = record['comparison']
        has_quantity = row and (row.get('generatedQuantity') is not None or row.get('customerQuantity') is not None)
        if has_quantity:
            populated.add(index)
            populated.update(record['ancestors'])
        if has_quantity and row.get('isDifferent', row.get('status') != 'matched'):
            different.add(index)
            different.update(record['ancestors'])
    full = book.copy_worksheet(sheet)
    full.title = 'Типовой ВОР — все строки'
    full.views = copy(sheet.views)
    full.print_title_rows = sheet.print_title_rows
    full.print_title_cols = sheet.print_title_cols
    if sheet.print_area:
        full.print_area = str(sheet.print_area)
    for target, indices in ((sheet, [i for i in display_order if i in different]),
                            (full, [i for i in display_order if i in populated])):
        for merged in list(target.merged_cells.ranges):
            target.unmerge_cells(str(merged))
        target.delete_rows(1, target.max_row)
        target.row_dimensions.clear()
        row_map = {}
        for number, index in enumerate(indices, 1):
            record = records[index]
            if record['source'] is not None:
                row_map[record['source']] = number
            dimension = copy(record['dimension']); dimension.index = number
            target.row_dimensions[number] = dimension
            for column, source in enumerate(record['cells'], 1):
                cell = target.cell(number, column)
                cell.value = Translator(source.value, origin=source.coordinate).translate_formula(cell.coordinate) if source.data_type == 'f' else source.value
                copy_cell_style(source, cell)
                if source.comment:
                    cell.comment = copy(source.comment)
                if source.hyperlink:
                    cell.hyperlink = copy(source.hyperlink)
            row = record['comparison']
            for column, field in ((7, 'customerQuantity'), (8, 'difference')):
                cell = target.cell(number, column)
                copy_cell_style(record['cells'][3], cell)
                cell.value = row.get(field) if row else None
                if number == 2:
                    cell.value = 'Объём заказчика' if column == 7 else 'Расхождение'
                    alignment = copy(cell.alignment)
                    alignment.wrap_text = True
                    cell.alignment = alignment
            if row:
                target.cell(number, 8).number_format = difference_format(row.get('differencePercent'))
                calculation = target.cell(number, 5)
                calculation.value = compact_calculation(calculation.value, row.get('generatedQuantity'), row.get('unit', ''))
                alignment = copy(calculation.alignment); alignment.wrap_text = True
                calculation.alignment = alignment
        for merged in merges:
            mapped = [row_map.get(row) for row in range(merged.min_row, merged.max_row + 1)]
            if all(value is not None for value in mapped) and mapped[-1] - mapped[0] == len(mapped) - 1:
                target.merge_cells(start_row=mapped[0], end_row=mapped[-1], start_column=merged.min_col, end_column=merged.max_col)
                if merged.min_col == merged.max_col == 4 and merged.min_row == 2:
                    for column in (7, 8):
                        target.merge_cells(start_row=mapped[0], end_row=mapped[-1], start_column=column, end_column=column)
        for number, column in ((7, 'G'), (8, 'H')):
            volume_dimension = target.column_dimensions.get('D')
            target.column_dimensions[column] = copy(volume_dimension) if volume_dimension else ColumnDimension(
                target, index=column, width=target.sheet_format.defaultColWidth or 13)
            target.column_dimensions[column].index = column
            target.column_dimensions[column].min = number
            target.column_dimensions[column].max = number
        target.freeze_panes = None
        target.auto_filter.ref = None
        reorder_comparison_sheet(target, (1, 2, 3, 4, 7, 8, 5, 6))
        for column, title in enumerate(('№ п/п', 'Наименование работы', 'Ед. изм.', 'Объём сформированной ВОР', 'Объём ВОР заказчика',
                                        'Расхождение', 'Расчёт объёма', 'Обоснование объёма работ'), 1):
            target.cell(2, column).value = title
            alignment = copy(target.cell(2, column).alignment); alignment.wrap_text = True
            target.cell(2, column).alignment = alignment
        for column, field, title in ((9, 'generatedVolumeSource', 'Источник объёма сформированного ВОР'),
                                     (10, 'customerVolumeSource', 'Источник объёма ВОР заказчика')):
            copy_cell_style(target.cell(2, 8), target.cell(2, column))
            target.cell(2, column).value = title
            target.column_dimensions[get_column_letter(column)].width = 65
            for number, index in enumerate(indices, 1):
                row = records[index]['comparison']
                if not row:
                    continue
                cell = target.cell(number, column)
                copy_cell_style(target.cell(number, 8), cell)
                cell.value = row.get(field) or (row.get('modelReason') if column == 9 and row.get('generatedSourceRow') is not None else '')
                cell.data_type = 's'
                alignment = copy(cell.alignment); alignment.wrap_text = True; alignment.vertical = 'top'
                cell.alignment = alignment
                if cell.value:
                    target.row_dimensions[number].height = max(target.row_dimensions[number].height or 30,
                                                              min(300, 18 * (len(cell.value) // 55 + 2)))
        from .estimate_parameter_comparison import explanation
        parameter_columns = (
            (11, 'Параметр от ВОР к ТСН', lambda r: '\n'.join(dict.fromkeys(filter(None, (
                r.get('generatedEstimateParameters'), r.get('customerEstimateParameters')))))),
            (12, 'Значения параметров сформированного ВОР', lambda r: r.get('generatedEstimateParameterValues')),
            (13, 'Значения параметров ВОР заказчика', lambda r: r.get('customerEstimateParameterValues')),
            (14, 'Сравнение сметных параметров', lambda r: explanation(r.get('parameterComparison') or {'status':'not_applicable'})),
        )
        for column, title, value in parameter_columns:
            copy_cell_style(target.cell(2, 8), target.cell(2, column))
            target.cell(2, column, title)
            target.column_dimensions[get_column_letter(column)].width = 45
            for number, index in enumerate(indices, 1):
                row = records[index]['comparison']
                if row:
                    cell = target.cell(number, column)
                    copy_cell_style(target.cell(number, 8), cell)
                    cell.value = value(row) or None
                    cell.data_type = 's'
                    cell.alignment = copy(target.cell(number, 9).alignment)
        if include_ai:
            from .comparison_ai_reason import reason_text
            target.cell(2, 15).value = 'Обоснование ИИ'
            copy_cell_style(target.cell(2, 8), target.cell(2, 15))
            target.column_dimensions['O'].width = 85
            for number, index in enumerate(indices, 1):
                row = records[index]['comparison']
                if not row:
                    continue
                cell = target.cell(number, 15)
                copy_cell_style(target.cell(number, 8), cell)
                cell.value = reason_text(row)
                cell.data_type = 's'
                alignment = copy(cell.alignment); alignment.wrap_text = True; alignment.vertical = 'top'
                cell.alignment = alignment
                if cell.value:
                    target.row_dimensions[number].height = max(target.row_dimensions[number].height or 30,
                                                              min(300, 18 * (len(cell.value) // 65 + 2)))
        for merged in list(target.merged_cells.ranges):
            if merged.min_col == 1 and merged.max_col == 8 and merged.min_row == merged.max_row:
                target.unmerge_cells(str(merged))
                target.merge_cells(start_row=merged.min_row, end_row=merged.max_row, start_column=1, end_column=15 if include_ai else 14)
        for number, index in enumerate(indices,1):
            record = records[index]
            if number > 3 and record['cells'][0].value and not record['comparison'] and not any(target.cell(number,c).value for c in range(2,15 if include_ai else 14)):
                target.merge_cells(start_row=number,end_row=number,start_column=1,end_column=15 if include_ai else 14)
                alignment = copy(target.cell(number,1).alignment); alignment.wrap_text=True; alignment.horizontal='left'
                target.cell(number,1).alignment=alignment
        target.column_dimensions['G'].width = 60
        for column in ('D', 'E', 'F'):
            target.column_dimensions[column].width = 24
        if target.print_area:
            target.print_area = f"A1:{'O' if include_ai else 'N'}{target.max_row}"
    customer.close()
    book.active = 0
    return book
