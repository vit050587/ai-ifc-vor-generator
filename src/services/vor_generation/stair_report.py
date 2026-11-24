"""A single object-wide stair quantity sheet, independent of work-row repetition."""
from collections import defaultdict

TITLE = 'Объёмы лестниц'
UNALLOCATED = 'Не разделено на марши и площадки'
CONTROL_TITLE = 'Контроль лестниц'


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
        component = {'flight': 'Лестничные марши', 'landing': 'Лестничные площадки'}.get(element.component, UNALLOCATED)
        groups[(element.storey or 'Этаж не указан', component, element.construction_method)].append(element)
    result = []
    for (storey, component, method), elements in sorted(groups.items()):
        known = [e.quantity('volume') for e in elements if 'volume' in e.quantities]
        from .builder import _formwork_area
        formwork = [_formwork_area('stair', {}, [e]) for e in elements] if method != 'precast' else []
        result.append({'storey': storey, 'component': component, 'method': method,
                       'count': len(elements), 'volume': sum(known) if known else None,
                       'formwork': sum(v for v in formwork if v is not None) if any(v is not None for v in formwork) else None,
                       'missing_formwork': sum(v is None for v in formwork),
                       'missing': len(elements) - len(known), 'part': elements[0].part if hasattr(elements[0], 'part') else ''})
    return result


def write(book, rows):
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter
    if TITLE in book.sheetnames:
        del book[TITLE]
    sheet = book.create_sheet(TITLE)
    sheet.append(['Объёмы лестниц по всему объекту'])
    sheet.append(['Объём бетона по IFC, м³. Каждый элемент учтён один раз. Неразделённый остаток не распределяется между маршами и площадками. Контроль полноты — на листе «Контроль лестниц».'])
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


def completeness(rows, rebar=None):
    """Independent operation totals; absent quantities remain absent, never zero.

    Scope includes the building part and technology. Reinforcement keeps its
    source scope: an overall stair quantity is not proportionally allocated.
    """
    from . import mapping
    result = []
    components = ('Лестничные марши', 'Лестничные площадки', UNALLOCATED)
    groups = defaultdict(list)
    for row in rows:
        groups[(row.get('part', ''), row.get('method', 'unknown'))].append(row)
    for (part, method), members in sorted(groups.items()):
        for operation, unit, value_key, missing_key in (
                ('Бетонирование', 'м³', 'volume', 'missing'),
                ('Опалубка', 'м²', 'formwork', 'missing_formwork')):
            if operation == 'Опалубка' and method == 'precast':
                continue
            values = [_sum_values([r for r in members if r['component'] == c], value_key) for c in components]
            missing = sum(r.get(missing_key, 0) for r in members)
            unallocated = [r for r in members if r['component'] == UNALLOCATED]
            notes = []
            if unallocated:
                notes.append(f'Не разделены {sum(r.get("count", 0) for r in unallocated)} элементов; общий объём не распределён')
            if missing:
                notes.append(f'Без величины для этой работы: {missing} элементов; итог только по известным данным')
            if method in ('unknown', 'conflicting'):
                notes.append('Технология не подтверждена; применимость монолитных работ требует проверки')
            result.append(dict(operation=operation, unit=unit, scope=part, method=method,
                               total=_sum_values(members, value_key), flight=values[0], landing=values[1],
                               unallocated=values[2], missing=missing,
                               status='Неполно' if notes else 'Разделено', note='; '.join(notes)))
    rebar_groups = defaultdict(list)
    for row in (rebar.rows if rebar is not None else []):
        kind = row.source_kind or mapping.detect_kind(row.section)
        if kind not in ('stair', 'precast'):
            continue
        rebar_groups[(row.source_part or '', row.section, kind)].append(row)
    for (part, section, kind), members in sorted(rebar_groups.items()):
        buckets = defaultdict(list)
        for row in members:
            component = row.component or mapping.stair_component(' '.join((row.section, row.group, row.source_context)))
            buckets[component if component in ('flight', 'landing') else 'combined'].append(row.volume)
        values = [sum(buckets[c]) if buckets[c] else None for c in ('flight', 'landing', 'combined')]
        result.append(dict(operation='Армирование', unit='т', scope=' / '.join(filter(None, (part, section))),
                           method='precast' if kind == 'precast' else 'source', total=sum(r.volume for r in members),
                           flight=values[0], landing=values[1], unallocated=values[2], missing=0,
                           status='Неполно' if buckets['combined'] else 'Разделено',
                           note='Общая арматура сохранена без распределения между маршами и площадками' if buckets['combined'] else 'По исходным строкам ВОР заказчика'))
    if rows and not rebar_groups:
        result.append(dict(operation='Армирование', unit='т', scope='Лестницы', method='source',
                           total=None, flight=None, landing=None, unallocated=None, missing=None,
                           status='Нет данных', note='В исходных документах нет подтверждённых строк арматуры лестниц; масса не рассчитывается из объёма бетона'))
    return result


def _sum_values(rows, key):
    known = [r.get(key) for r in rows if r.get(key) is not None]
    return sum(known) if known else None


def write_control(book, rows):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    if CONTROL_TITLE in book.sheetnames:
        del book[CONTROL_TITLE]
    sheet = book.create_sheet(CONTROL_TITLE)
    sheet.append(['Контроль полноты разделения лестниц'])
    sheet.append(['Итого = марши + площадки + известный неразделённый остаток. Пустые величины означают отсутствие данных. Работы и ресурсы повторно не суммируются.'])
    sheet.append(['Работа', 'Область исходных данных', 'Технология', 'Ед. изм.', 'Итого известно',
                  'Марши', 'Площадки', 'Не разделено', 'Элементов без величины', 'Статус', 'Примечание'])
    methods = {'monolithic': 'Монолитная', 'precast': 'Сборная', 'unknown': 'Не подтверждена',
               'conflicting': 'Противоречие', 'source': 'По документу'}
    for row in rows:
        scope = {'above': 'Надземная часть', 'underground': 'Подземная часть'}.get(row['scope'], row['scope'])
        sheet.append([row['operation'], scope, methods.get(row['method'], row['method']), row['unit'],
                      row['total'], row['flight'], row['landing'], row['unallocated'], row['missing'], row['status'], row['note']])
    sheet.freeze_panes = 'E4'
    sheet.auto_filter.ref = f'A3:K{max(3, sheet.max_row)}'
    for i, width in enumerate((25, 42, 23, 12, 20, 20, 20, 22, 25, 22, 85), 1):
        sheet.column_dimensions[get_column_letter(i)].width = width
    for cells in sheet:
        for cell in cells:
            cell.alignment = Alignment(vertical='top', wrap_text=True)
            if cell.row == 3:
                cell.font = Font(bold=True, color='FFFFFF')
                cell.fill = PatternFill('solid', fgColor='24476B')
            if cell.row > 3 and cell.column in (5, 6, 7, 8):
                cell.number_format = '0.000'
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
        if any(row.get('component') in (UNALLOCATED, 'Лестничные клетки', 'Марши и площадки вместе') for row in ours + theirs):
            note = (note + '; ' if note else '') + 'Есть объём без разделения на марши и площадки; раздельное сравнение неполно'
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
