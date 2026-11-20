"""Filterable estimate reconciliation, with independent quantity/cost/factor verdicts."""
from collections import Counter, defaultdict
from copy import copy
from decimal import Decimal
import math

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.views import Selection

from .estimate_comparison import number, known_sum, cost_coverage, building_part, norm, _canonical_factors

COLORS = {'equal': 'E7F1E8', 'within': 'FFF2CC', 'different': 'F8DEDC', 'unknown': 'EEF0F3'}
VERDICTS = {'equal': 'Совпадает', 'within': 'В пределах 3%', 'different': 'Различается',
            'unknown': 'Недостаточно данных'}
NAMES = {'labor': 'ЗП', 'machines': 'ЭМ', 'materials': 'МР', 'overhead': 'НР', 'profit': 'СП', 'drivers': 'ЗП машинистов',
         'correction': 'Поправочный коэффициент', 'index': 'Индекс', 'current_winter': 'Зимнее удорожание (тек.)',
         'winter_current': 'Зимнее удорожание (тек.)', 'winter_base': 'Зимнее удорожание (баз.)',
         'overhead_rate': 'Норма НР', 'profit_rate': 'Норма СП'}


def metric(ours, theirs, *, exact=False, comparable=True):
    """Percent is relative to our estimate. Zero is valid; a missing value is not zero."""
    if ours is None or theirs is None or not comparable:
        return None, None, 'unknown'
    if exact:
        return None, None, 'equal' if _canonical_factors(ours) == _canonical_factors(theirs) else 'different'
    ours, theirs = number(ours), number(theirs)
    if ours is None or theirs is None:
        return None, None, 'unknown'
    delta = theirs - ours
    percent = delta / abs(ours) if ours else None
    # Match the comparison engine's numeric precision, without rounding the 3% boundary.
    equal = abs(delta) <= Decimal('0.000001')
    return delta, percent, 'equal' if equal else 'within' if percent is not None and abs(percent) <= Decimal('.03') else 'different'


def flatten(value, prefix=''):
    if not isinstance(value, dict):
        return {prefix: value}
    result = {}
    for key, child in value.items():
        result.update(flatten(child, ' / '.join(filter(None, [prefix, NAMES.get(key, key)]))))
    return result


def factor_metrics(a, b):
    ours, theirs = flatten(a.get('factors') or {}), flatten(b.get('factors') or {})
    keys = sorted(ours.keys() | theirs.keys())
    return [(key, ours.get(key), theirs.get(key), metric(ours.get(key), theirs.get(key), exact=True)[2]) for key in keys]


def worst(states):
    return next((state for state in ('different', 'unknown', 'within', 'equal') if state in states), 'unknown')


def source_text(value):
    if not isinstance(value, dict):
        return str(value or '')
    if 'positions' in value:
        return '; '.join(source_text(v) for v in value['positions'])
    return ', '.join(str(v) if k in ('file', 'sheet') else f'строка {v}' if k in ('row', 'sourceRow') else f'{k}: {v}'
                     for k, v in value.items() if v is not None)


def display(value):
    if isinstance(value, (list, tuple)):
        return '; '.join(str(display(v)) for v in value)
    if isinstance(value, dict):
        return '; '.join(f'{NAMES.get(k,k)}: {display(v)}' for k, v in value.items())
    return value


def unit_cost(position):
    quantity, total = number(position.get('quantity')), number(position.get('total'))
    return total / quantity if quantity and total is not None else None


def coverage_text(ours, theirs):
    issues = []
    for label, coverage in (('У нас', ours), ('У застройщика', theirs)):
        if coverage['missing'] or coverage['partial']:
            issues.append(f"{label}: без стоимости — {coverage['missing']}; с неполной стоимостью — {coverage['partial']}")
    return '; '.join(issues) or 'Стоимость всех позиций заполнена'


def paint(sheet, row, columns, state):
    for col in columns:
        sheet.cell(row, col).fill = PatternFill('solid', fgColor=COLORS[state])


def finish(sheet, widths, template, *, percent_columns=()):
    """Use the actual typical VOR cell styles and three-row header layout."""
    from .comparison_layout import copy_cell_style
    # The report has its own data columns, but no independent visual theme.
    sheet.views = copy(template.views)
    sheet.freeze_panes = None
    sheet.sheet_view.selection = [Selection(activeCell='A1', sqref='A1')]
    sheet.page_setup = copy(template.page_setup)
    sheet.page_margins = copy(template.page_margins)
    sheet.print_options = copy(template.print_options)
    for col in range(1, sheet.max_column + 1):
        sheet.column_dimensions[get_column_letter(col)].width = widths.get(col, 19)
    for cells in sheet:
        lines = 1
        for cell in cells:
            verdict_fill = copy(cell.fill) if cell.fill.fgColor.rgb in {'00' + color for color in COLORS.values()} else None
            sample = template.cell(2, 2) if cell.row == 1 else template.cell(8, 4 if isinstance(cell.value, (int, float, Decimal)) else 2)
            copy_cell_style(sample, cell)
            if verdict_fill is not None:
                cell.fill = verdict_fill
            if isinstance(cell.value, Decimal):
                cell.value = float(cell.value)
            if isinstance(cell.value, str):
                cell.data_type = 's'
                lines = max(lines, sum(max(1, math.ceil(len(line) / max(8, widths.get(cell.column, 19) - 2)))
                                       for line in cell.value.split('\n')))
            alignment = copy(cell.alignment)
            alignment.wrap_text = True
            cell.alignment = alignment
            if isinstance(cell.value, (float, int)):
                cell.number_format = '+0.00%;-0.00%;0.00%' if cell.column in percent_columns else '#,##0.00####'
        sheet.row_dimensions[cells[0].row].height = max(15, lines * 15)
    # Like the typical VOR: title on row 1, column headings on rows 2–3.
    merged = list(sheet.merged_cells.ranges)
    for area in merged:
        sheet.unmerge_cells(str(area))
    heights = {r: dimension.height for r, dimension in sheet.row_dimensions.items()}
    sheet.insert_rows(1)
    sheet.insert_rows(3)
    sheet.row_dimensions.clear()
    for r, height in heights.items():
        if r > 1:
            sheet.row_dimensions[r + 2].height = height
    for area in merged:
        sheet.merge_cells(start_row=area.min_row + 2, end_row=area.max_row + 2,
                          start_column=area.min_col, end_column=area.max_col)
    copy_cell_style(template.cell(1, 1), sheet.cell(1, 1))
    sheet.cell(1, 1).value = sheet.title
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=sheet.max_column)
    sheet.row_dimensions[1].height = template.row_dimensions[1].height
    sheet.row_dimensions[2].height = template.row_dimensions[2].height or 15
    sheet.row_dimensions[3].height = template.row_dimensions[3].height or 39
    for col in range(1, sheet.max_column + 1):
        copy_cell_style(template.cell(2, 2), sheet.cell(2, col))
        copy_cell_style(template.cell(3, 2), sheet.cell(3, col))
        alignment = copy(sheet.cell(2, col).alignment)
        alignment.wrap_text = True
        sheet.cell(2, col).alignment = alignment
        sheet.merge_cells(start_row=2, end_row=3, start_column=col, end_column=col)
    sheet.print_title_rows = '1:3'
    sheet.print_area = sheet.dimensions
    sheet.auto_filter.ref = f'A2:{get_column_letter(sheet.max_column)}{sheet.max_row}'


def write(result, path):
    book = Workbook()
    from .vor_generation.template import DEFAULT_TEMPLATE_PATH, DEFAULT_SHEET
    style_book = load_workbook(DEFAULT_TEMPLATE_PATH)
    template = style_book[DEFAULT_SHEET]
    book.loaded_theme = style_book.loaded_theme
    summary = book.active
    summary.title = 'Итоги'
    main = book.create_sheet('Сравнение смет')
    detail = book.create_sheet('Начисления и коэффициенты')
    resources = book.create_sheet('Ресурсы')
    main.append(['Часть здания', 'Раздел / конструкция', 'Наличие шифра', 'Шифр', 'Работа у нас', 'Работа у застройщика',
                 'Ед. изм.', 'Объём у нас', 'Объём застройщика', 'Разница объёма', 'Разница объёма, %',
                 'Стоимость у нас, руб.', 'Стоимость застройщика, руб.', 'Разница стоимости, руб.', 'Разница стоимости, %',
                 'Коэффициенты', 'Результат', 'Уточнение', 'Источник у нас', 'Источник застройщика'])
    detail.append(['Часть здания', 'Раздел / конструкция', 'Шифр', 'Работа', 'Показатель', 'У нас', 'У застройщика',
                   'Разница', 'Разница, %', 'Результат'])
    resources.append(['Часть здания', 'Раздел / конструкция', 'Шифр работы', 'Шифр ресурса', 'Ед. изм.',
                      'Объём у нас', 'Объём застройщика', 'Разница объёма', 'Разница объёма, %',
                      'Стоимость у нас, руб.', 'Стоимость застройщика, руб.', 'Разница стоимости, руб.',
                      'Разница стоимости, %', 'Наличие', 'Результат'])
    parts = defaultdict(list)
    code_presence = {side: {(building_part(row[side]), norm(row[side].get('code')))
                            for row in result['rows'] if row[side]} for side in ('generated', 'customer')}
    ordered = sorted(enumerate(result['rows']), key=lambda pair: (
        {'Подземная часть': 0, 'Надземная часть': 1, 'Автостоянка': 2}.get(building_part(pair[1]['generated'] or pair[1]['customer']), 3),
        (pair[1]['generated'] or pair[1]['customer']).get('scope') or '', pair[0]))
    for _, row in ordered:
        a, b = row['generated'] or {}, row['customer'] or {}
        ref = a or b
        part, scope, code, name = building_part(ref), ref.get('scope'), ref.get('code'), ref.get('name')
        key = part, norm(code)
        present = 'У обеих сторон' if all(key in codes for codes in code_presence.values()) else 'Только у нас' if a else 'Только у застройщика'
        q = metric(a.get('quantity'), b.get('quantity'))
        money = metric(a.get('total'), b.get('total'))
        factors = factor_metrics(a, b)
        fs = worst([item[3] for item in factors])
        state = worst([q[2], money[2], fs]) if a and b else 'different'
        if a and b and (not result['priceBasisComparable'] or a.get('incomplete') or b.get('incomplete')):
            state = worst([state, 'unknown'])
        note = []
        if present == 'У обеих сторон' and not (a and b):
            note.append('Шифр есть у обеих сторон в этой части здания; позиции не сопоставлены по конструкции, единице или неоднозначности')
        if not result['priceBasisComparable']:
            note.append(('Разница рассчитана по указанным суммам; сопоставимость периода и уровня цен не подтверждена'
                         if money[0] is not None else 'Сопоставимость периода и уровня цен не подтверждена'))
        if not factors:
            note.append('Коэффициенты не указаны')
        if a.get('incomplete'):
            note.append('Наша стоимость неполная' + ('; разница рассчитана по известной сумме' if money[0] is not None else ''))
        if b.get('incomplete'):
            note.append('Стоимость застройщика неполная' + ('; разница рассчитана по известной сумме' if money[0] is not None else ''))
        if a and number(a.get('total')) is None:
            note.append('Стоимость у нас отсутствует')
        if b and number(b.get('total')) is None:
            note.append('Стоимость застройщика отсутствует')
        if row['status'] == 'multiple_candidates':
            note.append('Несколько позиций с этим шифром; пара не назначена')
        main.append([part, scope, present, code, a.get('name'), b.get('name'), ref.get('unit'),
                     a.get('quantity'), b.get('quantity'), q[0], q[1], a.get('total'), b.get('total'), money[0], money[1],
                     VERDICTS[fs], VERDICTS[state], '; '.join(note), source_text(a.get('source')), source_text(b.get('source'))])
        n = main.max_row
        paint(main, n, range(1, 21), state)
        if a and b:
            paint(main, n, range(8, 12), q[2]); paint(main, n, range(12, 16), money[2]); paint(main, n, [16], fs)
        detail_states = []
        for key, av, bv, verdict in factors:
            detail.append([part, scope, code, name, key, display(av), display(bv), None, None, VERDICTS[verdict]])
            paint(detail, detail.max_row, range(1, 11), verdict)
        for key in sorted((a.get('components') or {}).keys() | (b.get('components') or {}).keys()):
            av, bv = (a.get('components') or {}).get(key), (b.get('components') or {}).get(key)
            delta, percent, verdict = metric(av, bv)
            detail_states.append(verdict)
            detail.append([part, scope, code, name, 'Стоимость ' + NAMES.get(key, key), av, bv, delta, percent, VERDICTS[verdict]])
            paint(detail, detail.max_row, range(1, 11), verdict)
        for key, av, bv in (('Стоимость единицы, руб.', unit_cost(a), unit_cost(b)),):
            delta, percent, verdict = metric(av, bv)
            detail.append([part, scope, code, name, key, av, bv, delta, percent, VERDICTS[verdict]])
            paint(detail, detail.max_row, range(1, 11), verdict)
        for label, key in (('Влияние изменения объёма, руб.', 'quantityEffect'),
                           ('Влияние изменения цены единицы, руб.', 'priceEffect')):
            if row.get(key) is not None:
                detail.append([part, scope, code, name, label, None, None, row[key], None,
                               'Разложение разницы стоимости'])
        rg, rc = resource_groups(a), resource_groups(b)
        for rcode, unit in sorted(rg.keys() | rc.keys()):
            ar, br = rg.get((rcode, unit), {}), rc.get((rcode, unit), {})
            qr = metric(ar.get('quantity'), br.get('quantity'))
            cr = metric(ar.get('total'), br.get('total'))
            rs = worst([qr[2], cr[2]]) if ar and br else 'different'
            detail_states.append(rs)
            resources.append([part, scope, code, rcode, unit, ar.get('quantity'), br.get('quantity'), qr[0], qr[1],
                              ar.get('total'), br.get('total'), cr[0], cr[1],
                              'У обеих сторон' if ar and br else 'Только у нас' if ar else 'Только у застройщика', VERDICTS[rs]])
            paint(resources, resources.max_row, range(1, 16), rs)
            if ar and br:
                paint(resources, resources.max_row, range(6, 10), qr[2]); paint(resources, resources.max_row, range(10, 14), cr[2])
        if a and b and detail_states:
            state = worst([state, *detail_states])
            main.cell(n, 17).value = VERDICTS[state]
            paint(main, n, [*range(1, 8), *range(17, 21)], state)
        parts[part].append((row, present, state))
    summary.append(['Часть здания', 'Только у нас, шифров', 'Только у застройщика, шифров', 'У обеих сторон, шифров',
                    'Совпадает, позиций', 'В пределах 3%, позиций', 'Различается, позиций', 'Недостаточно данных, позиций', 'Известная стоимость у нас, руб.',
                    'Известная стоимость застройщика, руб.', 'Разница известных сумм, руб.', 'Полнота стоимости'])
    for part, members in parts.items():
        presence = Counter(label for _, label in {((r['generated'] or r['customer'])['code'], label) for r, label, _ in members})
        states = Counter(v[2] for v in members)
        coverage = [cost_coverage(r[side] for r, _, _ in members if r[side]) for side in ('generated', 'customer')]
        totals = [value['knownTotal'] for value in coverage]
        delta = totals[1] - totals[0] if all(v is not None for v in totals) else None
        summary.append([part, presence['Только у нас'], presence['Только у застройщика'], presence['У обеих сторон'],
                        *[states[k] for k in ('equal', 'within', 'different', 'unknown')], *totals, delta, coverage_text(*coverage)])
    coverage_all = [cost_coverage(r[side] for r in result['rows'] if r[side]) for side in ('generated', 'customer')]
    totals_all = [coverage['knownTotal'] for coverage in coverage_all]
    delta_all = totals_all[1] - totals_all[0] if all(v is not None for v in totals_all) else None
    summary.append(['Всего', *[sum(summary.cell(r, c).value for r in range(2, summary.max_row + 1)) for c in range(2, 9)],
                    *totals_all, delta_all, coverage_text(*coverage_all)])
    end = summary.max_row
    summary.append([])
    summary.append(['Файл у нас', result['generatedFile']]); summary.append(['Файл застройщика', result['customerFile']])
    summary.append(['Период у нас', result['generatedPeriod']]); summary.append(['Период застройщика', result['customerPeriod']])
    summary.append(['Правила сравнения', 'Разница = застройщик − мы. Процент считается от абсолютного значения нашей позиции. При нашем нуле ненулевая разница превышает допуск.'])
    summary.append(['Стоимость', 'Разница сумм показывается независимо от периода цен. Если период или уровень цен не подтверждён, это отмечено у позиции. Известная стоимость — сумма доступных значений; отсутствующие суммы не заменяются нулями. Полнота указана отдельно.'])
    summary.append(['Цвета', 'Зелёный — совпадает. Жёлтый — отклонение до 3% включительно. Красный — свыше 3%, любое различие коэффициентов или позиция только с одной стороны. Серый — недостаточно данных.'])
    summary.append(['Коэффициенты', 'Допуск 3% не применяется. Отсутствующий коэффициент не считается равным 1. Общий результат учитывает худшее из сравнений.'])
    summary.append(['Наличие шифров', 'Наличие определяется по шифру внутри части здания независимо от сопоставления отдельных позиций. Один шифр может повторяться в нескольких конструкциях. В сводке шифры и позиции считаются отдельно.'])
    summary.append(['Как работать', 'На листе «Сравнение смет» отфильтруйте часть здания, наличие шифра и результат. Коэффициенты и состав стоимости раскрыты по показателям на отдельном листе. Ресурсы сравниваются внутри родительской работы.'])
    for r in range(end + 2, summary.max_row + 1):
        summary.merge_cells(start_row=r, start_column=2, end_row=r, end_column=12)
    for r in range(end + 2, summary.max_row + 1): summary.row_dimensions[r].height = 45
    for col in range(2, 9):
        for r in range(2, end + 1): summary.cell(r, col).number_format = '0'
    finish(summary, {1: 29, **{i: 19 for i in range(2, 12)}, 12: 65}, template)
    summary.auto_filter.ref = f'A2:L{end + 2}'
    for r in range(end + 4, summary.max_row + 1): summary.row_dimensions[r].height = 45
    for col in range(2, 9):
        for r in range(4, end + 3): summary.cell(r, col).number_format = '0'
    finish(main, {1: 25, 2: 48, 3: 25, 4: 19, 5: 68, 6: 68, 16: 25, 17: 25, 18: 65, 19: 50, 20: 50}, template, percent_columns=(11, 15))
    finish(detail, {1: 25, 2: 48, 4: 68, 5: 48, 10: 25}, template, percent_columns=(9,))
    finish(resources, {1: 25, 2: 48, 14: 25, 15: 25}, template, percent_columns=(9, 13))
    try:
        book.save(path)
    finally:
        book.close()
        style_book.close()


def resource_groups(position):
    from .works_comparison.units import unit_and_factor
    groups = defaultdict(list)
    for row in position.get('resources') or []:
        unit, factor = unit_and_factor(row.get('unit'))
        quantity = number(row.get('quantity_exact', row.get('quantity')))
        groups[(str(row.get('code') or ''), unit)].append({
            'quantity': quantity * factor if quantity is not None and factor is not None else None,
            'total': row.get('total')})
    return {key: {field: known_sum(r[field] for r in rows) for field in ('quantity', 'total')} for key, rows in groups.items()}
