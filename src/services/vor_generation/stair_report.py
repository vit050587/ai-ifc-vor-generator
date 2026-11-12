"""A single object-wide stair quantity sheet, independent of work-row repetition."""
from collections import defaultdict

TITLE = 'Объёмы лестниц'


def records(index):
    groups = defaultdict(list)
    seen = set()
    for element in index.elements:
        if not (element.kind in ('stair', 'precast') or element.component in ('flight', 'landing')):
            continue
        if element.guid and element.guid in seen:
            continue
        if element.guid:
            seen.add(element.guid)
        component = {'flight': 'Лестничные марши', 'landing': 'Лестничные площадки'}.get(element.component, 'Лестничные клетки')
        groups[(element.storey or 'Этаж не указан', component, element.construction_method)].append(element)
    result = []
    for (storey, component, method), elements in sorted(groups.items()):
        known = [e.quantity('volume') for e in elements if 'volume' in e.quantities]
        result.append({'storey': storey, 'component': component, 'method': method,
                       'count': len(elements), 'volume': sum(known) if known else None,
                       'missing': len(elements) - len(known), 'part': elements[0].part if hasattr(elements[0], 'part') else ''})
    return result


def write(book, rows):
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter
    if TITLE in book.sheetnames:
        del book[TITLE]
    sheet = book.create_sheet(TITLE)
    sheet.append(['Объёмы лестниц по всему объекту'])
    sheet.append(['Объём бетона по IFC, м³. Каждый элемент учтён один раз; марши и площадки показаны отдельно.'])
    sheet.append(['Этаж', 'Конструкция', 'Технология', 'Количество элементов', 'Объём, м³', 'Без объёма в IFC'])
    methods = {'precast': 'Сборная', 'monolithic': 'Монолитная', 'unknown': 'Не указана'}
    for row in rows:
        sheet.append([row['storey'], row['component'], methods.get(row['method'], row['method']),
                      row['count'], row['volume'], row['missing'], row.get('part', '')])
    last = sheet.max_row
    sheet.append(['ИТОГО по объекту', None, None, sum(r['count'] for r in rows),
                  sum(r['volume'] for r in rows if r['volume'] is not None) if any(r['volume'] is not None for r in rows) else None,
                  sum(r['missing'] for r in rows)])
    sheet.append([])
    sheet.append(['Итого по этажам', 'Объём, м³', 'Без объёма в IFC'])
    for storey in sorted({r['storey'] for r in rows}):
        selected = [r for r in rows if r['storey'] == storey]
        known = [r['volume'] for r in selected if r['volume'] is not None]
        sheet.append([storey, sum(known) if known else None, sum(r['missing'] for r in selected)])
    sheet.column_dimensions['G'].hidden = True
    sheet.freeze_panes = 'A4'
    sheet.auto_filter.ref = f'A3:F{max(3, last)}'
    for col, width in enumerate((28, 34, 24, 24, 22, 25), 1):
        sheet.column_dimensions[get_column_letter(col)].width = width
    for row in sheet:
        for cell in row:
            cell.alignment = Alignment(vertical='top', wrap_text=True)
            if isinstance(cell.value, (int, float)):
                cell.number_format = '0.000' if cell.column == 5 or (cell.column == 2 and cell.row > last) else '0'
    for number in (1, 3, last + 1, last + 3):
        for cell in sheet[number]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='24476B')
    sheet.row_dimensions[2].height = 34
    return sheet


def floor_number(text):
    import re
    text = str(text or '').lower().replace('ё', 'е')
    for pattern in (r'(-?\d+)[_\s-]*(?:(?:го|ого|й)[_\s-]*)?этаж', r'этаж\s*[_ -]*(-?\d+)'):
        match = re.search(pattern, text)
        if match:
            return int(match[1])
    return None


def customer_records(rows):
    """Use source concrete once; never spread a source total over IFC floors."""
    from .stair_grouping import source_band
    grouped = defaultdict(list)
    for row in rows:
        if (row.kind not in ('stair', 'precast') and row.component not in ('flight', 'landing', 'combined')):
            continue
        if row.category != 'concrete' or row.unit_family != 'm3':
            continue
        text = ' '.join((row.section, row.block, row.source_context))
        band, number = source_band(text), floor_number(text)
        if band == 'upper':
            storey = 'Выше 1-го этажа'
        elif number is not None:
            storey = f'{number} этаж'
        elif row.part == 'underground':
            storey = 'Подземные этажи'
        else:
            storey = 'Без разбивки по этажам'
        component = {'flight': 'Лестничные марши', 'landing': 'Лестничные площадки'}.get(row.component, 'Марши и площадки вместе')
        grouped[(storey, component, row.part or '')].append(row)
    return [{'storey': storey, 'component': component, 'part': part,
             'method': '', 'count': 0, 'missing': 0,
             'volume': float(sum(row.volume for row in members))}
            for (storey, component, part), members in sorted(grouped.items())]


def read_records(path):
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        if TITLE not in book.sheetnames:
            return None
        result = []
        for values in book[TITLE].iter_rows(min_row=4, max_col=7, values_only=True):
            if values[0] == 'ИТОГО по объекту':
                break
            storey, component, method, count, volume, missing, part = values
            if storey and component:
                result.append(dict(storey=storey, component=component, method=method or '',
                                   count=count or 0, volume=volume, missing=missing or 0, part=part or ''))
        return result
    finally:
        book.close()


def _sum(rows):
    values = [row['volume'] for row in rows if row['volume'] is not None]
    return sum(values) if values else None


def _in_scope(row, scope, part=''):
    number = floor_number(row['storey'])
    if scope == 'Без разбивки по этажам':
        return not part or row.get('part') == part
    if scope == 'Выше 1-го этажа':
        return number is not None and number > 1
    if scope == 'Подземные этажи':
        return row.get('part') == 'underground' or number is not None and number <= 0
    return number is not None and number == floor_number(scope)


def write_comparison(book, generated, customer):
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter
    if TITLE in book.sheetnames:
        del book[TITLE]
    sheet = book.create_sheet(TITLE)
    sheet.append(['Сравнение объёмов лестниц'])
    sheet.append(['Объёмы в м³. Разница = ВОР заказчика − сформированная ВОР. Общие объёмы заказчика по нескольким этажам не распределяются по отдельным этажам.'])
    headers = ['Этаж / группа этажей', 'Конструкция', 'Сформированная ВОР, м³', 'ВОР заказчика, м³',
               'Разница, м³', 'Разница, %', 'Примечание']
    sheet.append(headers)
    def add(scope, component, ours, theirs, note=''):
        g, c = _sum(ours), _sum(theirs)
        difference = c - g if c is not None and g is not None else None
        percent = difference / g if difference is not None and g else None
        if any(row.get('missing') for row in ours):
            note = (note + '; ' if note else '') + 'В IFC есть элементы без объёма: сумма по известным данным'
        sheet.append([scope, component, g, c, difference, percent, note])
    add('ИТОГО по объекту', 'Все лестницы', generated, customer)
    for component in sorted({row['component'] for row in generated + customer}):
        add('По всему объекту', component, [r for r in generated if r['component'] == component],
            [r for r in customer if r['component'] == component])
    sheet.append([])
    section_row = sheet.max_row + 1
    sheet.append(['Сравнение по этажам и группам этажей ВОР заказчика'])
    scopes = sorted({(r['storey'], r.get('part', '')) for r in customer})
    for scope, part in scopes:
        theirs = [r for r in customer if r['storey'] == scope and r.get('part', '') == part]
        ours = [r for r in generated if _in_scope(r, scope, part)]
        label = scope + (' (подземная часть)' if scope == 'Без разбивки по этажам' and part == 'underground' else
                         ' (надземная часть)' if scope == 'Без разбивки по этажам' and part == 'above' else '')
        add(label, 'Всего', ours, theirs)
        for component in sorted({r['component'] for r in ours + theirs}):
            add(label, component, [r for r in ours if r['component'] == component],
                [r for r in theirs if r['component'] == component])
    sheet.append([])
    detail_row = sheet.max_row + 1
    sheet.append(['Поэтажная детализация'])
    floors = {f'{floor_number(r["storey"])} этаж' if floor_number(r['storey']) is not None else r['storey'] for r in generated}
    for storey in sorted(floors):
        ours = [r for r in generated if (floor_number(r['storey']) == floor_number(storey)
                 if floor_number(storey) is not None else r['storey'] == storey)]
        theirs = [r for r in customer if floor_number(r['storey']) is not None
                  and r['storey'] != 'Выше 1-го этажа' and floor_number(r['storey']) == floor_number(storey)]
        note = '' if theirs else 'В ВОР заказчика нет отдельного объёма этого этажа; сравнение общего объёма приведено выше'
        add(storey, 'Всего', ours, theirs, note)
    sheet.freeze_panes = 'C4'
    sheet.auto_filter.ref = f'A3:G{sheet.max_row}'
    for column, width in enumerate((34, 34, 25, 25, 22, 20, 75), 1):
        sheet.column_dimensions[get_column_letter(column)].width = width
    for cells in sheet:
        for cell in cells:
            cell.alignment = Alignment(vertical='top', wrap_text=True)
            if cell.column in (3, 4, 5):
                cell.number_format = '0.000'
            if cell.column == 6:
                cell.number_format = '0.00%'
    for number in (1, 3, 4, section_row, detail_row):
        for cell in sheet[number]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='24476B')
    sheet.row_dimensions[2].height = 48
    sheet.row_dimensions[3].height = 32
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.fitToWidth = 1
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    return sheet
