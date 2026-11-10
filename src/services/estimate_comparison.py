"""Compare two estimates independently of VOR quantities and model selection.

Input adapters produce atomic work/resource packages. Matching never uses a
project name, a row offset or the order of neighbouring estimates. Amounts are
read from saved estimates; no price, factor or unknown amount is invented.
"""
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from .works_comparison.units import unit_and_factor
from .vor_generation import mapping

FILENAME = 'Сравнение_смет.xlsx'
DATA_FILENAME = 'Сравнение_смет.json'
LABELS = {'matched': 'Совпадает', 'different': 'Различается', 'only_generated': 'Только в нашей ЛСР',
          'only_customer': 'Только в смете заказчика', 'missing_cost': 'Стоимость отсутствует или неполна',
          'price_basis_difference': 'Разный уровень или период цен', 'price_basis_unknown': 'Уровень или период цен не указан',
          'multiple_candidates': 'Несколько позиций с этим шифром'}
LABELS['matched'] = 'Количество и стоимость совпадают по доступным данным'
LABELS['missing_quantity'] = 'Количество отсутствует'


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        text = re.sub(r'[\s\u00a0\u202f]', '', str(value)).replace(',', '.')
        result = Decimal(text)
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def norm(value):
    return re.sub(r'\s+', ' ', str(value or '').lower().replace('ё', 'е')).strip()


def known_sum(values):
    values = [number(value) for value in values]
    return sum(values, Decimal(0)) if values and all(value is not None for value in values) else None


def package(record, children=()):
    quantity = number(record.get('quantity_exact', record.get('quantity')))
    unit, factor = unit_and_factor(record.get('unit'))
    quantity = quantity * factor if quantity is not None and factor is not None else None
    records = [record, *children]
    total = known_sum(row.get('total') for row in records)
    incomplete = any(row.get('incomplete', False) for row in records)
    components = {}
    for key in ('labor', 'machines', 'materials', 'overhead', 'profit'):
        amounts = []
        for row in records:
            value = (row.get('components') or {}).get(key)
            if isinstance(value, dict): value = value.get('value')
            if value is None: value = row.get(key)
            amounts.append(value)
        components[key] = known_sum(amounts)
    factors = {}
    for key, settings in (record.get('calculation') or {}).items():
        factors[key] = {field: settings.get(field) for field in ('correction', 'index', 'current_winter')}
    factors.update(record.get('factors') or {})
    for key in ('overhead_rate', 'profit_rate'):
        if record.get(key) is not None: factors[key] = record[key]
    return {'code': str(record.get('code') or ''), 'name': str(record.get('title') or record.get('name') or ''),
            'unit': unit, 'quantity': quantity, 'total': total, 'incomplete': incomplete,
            'components': components, 'factors': factors,
            'scope': ' > '.join([*(record.get('section_path') or []), *([str(record['group'])] if record.get('group') else [])]),
            'source': record.get('source') or {'row': record.get('estimate_row'), 'sourceRow': record.get('source_row')},
            'resources': [{'code': row.get('code'), 'quantity': row.get('quantity_exact'), 'unit': row.get('unit'),
                           'total': row.get('total')} for row in children]}


def from_data(data, filename=''):
    from .estimate_export import _position_groups
    if not isinstance(data, dict) or not isinstance(data.get('rows'), list) or not all(isinstance(row, dict) for row in data['rows']):
        raise ValueError('Расчётный JSON должен содержать список позиций rows в формате ЛСР сервиса.')
    rows = data.get('rows') or []
    if not any('total' in row for row in rows if row.get('code')):
        raise ValueError('В документе нет стоимости позиций сметы. ВОР сравнивается отдельно от ЛСР.')
    positions = [package(record, children) for record, children in _position_groups(rows) if record.get('code')]
    for position in positions:
        if not isinstance(position['source'], dict):
            raise ValueError('Источник позиции в расчётном JSON должен быть объектом.')
        position['source'].update(file=filename, sheet='Смета по перечню')
    return {'positions': positions, 'period': str((data.get('period') or {}).get('title') or ''),
            'priceLevel': 'current', 'file': filename}


def _schema(values):
    result = {}
    for i, value in enumerate(values):
        text = norm(value)
        if text.startswith('исходн') or 'для проверки' in text:
            continue
        compact = re.sub(r'[^а-яa-z0-9]', '', text)
        if 'шифр' in text or compact in ('код', 'кодрасценки', 'обоснование'):
            result.setdefault('code', i)
        elif 'наименован' in text:
            result.setdefault('name', i)
        elif ('ед' in text and 'изм' in text) or compact == 'единица':
            result.setdefault('unit', i)
        elif any(word in compact for word in ('колич', 'колво', 'объем')) and not any(word in text for word in ('стоим', 'расчет', 'цен')):
            result.setdefault('quantity', i)
        elif ('всего' in text or 'стоимость' in text or 'сумма' in text) and not any(word in text for word in ('базис', 'единиц', 'на ед', 'за ед')):
            if 'текущ' in text or 'total' not in result:
                result['total'] = i
                result['priceLevel'] = 'current' if 'текущ' in text else 'unspecified'
        elif compact in ('зп','эм','мр'):
            result[{'зп':'labor','эм':'machines','мр':'materials'}[compact]]=i
        elif 'коэффициент' in text and 'поправ' in text:
            result['correction'] = i
        elif 'зимн' in text:
            result['winter_current' if 'текущ' in text else 'winter_base'] = i
        elif 'индекс' in text:
            result['index'] = i
    return result if all(key in result for key in ('code', 'name', 'unit', 'quantity', 'total')) else None


def read_xlsx(path):
    """Recognize headers and block markers, never fixed sheet/row positions."""
    book = load_workbook(path, read_only=True, data_only=True)
    positions, periods, levels, document_totals = [], set(), set(), []
    try:
        # A file can contain both a public list and its calculation source.
        # Only sheets with money columns qualify. Hidden duplicate source sheets
        # are read only when no visible estimate sheet exists.
        sheets = [sheet for sheet in book if sheet.sheet_state == 'visible']
        for sheet in sheets:
            schema, active, hierarchy, group = None, None, [], ''
            rows = list(sheet.iter_rows(values_only=True))
            block_layout = any(any(norm(value).startswith('всего по позиции') for value in row if isinstance(value, str)) for row in rows)
            for row_index, values in enumerate(rows, 1):
                line = ' '.join(str(value) for value in values if value is not None)
                match = re.search(r'(?:текущий период тсн|период цен|период расчета)\s*:\s*(.+)', line, re.I)
                if match: periods.add(match[1].strip())
                candidate = _schema(values)
                if candidate is None and row_index < len(rows) and any('наименован' in norm(v) for v in values):
                    candidate = _schema([' '.join(str(v or '') for v in pair) for pair in zip(values, rows[row_index])])
                if candidate:
                    schema = candidate
                    levels.add(schema['priceLevel'])
                    continue
                if schema is None:
                    continue
                def cell(key):
                    index = schema.get(key)
                    return values[index] if index is not None and index < len(values) else None
                code, name = str(cell('code') or '').strip(), str(cell('name') or '').strip()
                if code and not name and not cell('unit') and number(cell('quantity')) is None:
                    code = ''  # A full-width heading can occupy the code column.
                title = norm(name or next((v for v in values if isinstance(v, str) and v.strip()), ''))
                if title.startswith(('итого по смете', 'всего по смете')):
                    document_totals.append(number(cell('total')))
                if not code:
                    if active is not None:
                        component_key = {'зп':'labor', 'эм':'machines', 'мр':'materials'}.get(title)
                        if title.startswith('нр '): component_key = 'overhead'
                        if title.startswith('сп '): component_key = 'profit'
                        if component_key:
                            active['components'][component_key] = number(cell('total'))
                            if component_key in ('labor', 'machines', 'materials'):
                                active['factors'][component_key] = {'correction': cell('correction'), 'index': cell('index'),
                                                                  'current_winter': cell('winter_current')}
                            else:
                                active['factors'][component_key + '_rate'] = cell('quantity')
                        if title.startswith('всего по позиции'):
                            # Native printed position totals include its separate
                            # resources. Do not add those children a second time.
                            active['total'] = number(cell('total'))
                            active['source']['costRow'] = row_index
                            active['hasFooter'] = True
                            active['incomplete'] = 'известным суммам' in title
                            active = None
                            continue
                    first = next((str(v).strip() for v in values if isinstance(v, str) and v.strip()), '')
                    if first.startswith(('Раздел ', 'Подраздел ')) or 'часть здания' in first.lower() or first.startswith('Раздел: '):
                        if first.startswith('Раздел '): hierarchy = hierarchy[:1] + [first]
                        elif first.startswith('Подраздел '): hierarchy = hierarchy[:2] + [first]
                        elif 'часть здания' in first.lower(): hierarchy = [first]
                        else: hierarchy, group = [], first[8:]
                        if not first.startswith('Раздел: '): group = ''
                        active = None
                    elif first and (not name or all(value is None or str(value).strip() == name for value in values)) and not title.startswith(('итого', 'всего')):
                        group = first
                        active = None
                    continue
                unit, factor = unit_and_factor(cell('unit'))
                # Component labels and numbered column captions are not rates.
                if factor is None or not name or not re.search(r'[.-]', code):
                    continue
                quantity = number(cell('quantity'))
                position = {'code':code, 'name':name, 'unit':unit,
                            'quantity':quantity * factor if quantity is not None else None,
                            'total':number(cell('total')),
                            'components':{key:number(cell(key)) for key in ('labor','machines','materials') if key in schema},
                            'factors':({key:cell(key) for key in ('correction','index','winter_current','winter_base') if key in schema and cell(key) not in (None,'')} if not block_layout else {}),
                            'incomplete':False,
                            'scope':' > '.join([*hierarchy, *([group] if group else [])]), 'resources':[],
                            'source':{'file':Path(path).name, 'sheet':sheet.title, 'row':row_index}}
                if block_layout and code.startswith('1.') and active is not None:
                    active['resources'].append(position)
                else:
                    positions.append(position)
                    active = position if not code.startswith('1.') else None
        if not positions:
            raise ValueError('Не распознана смета с шифром, наименованием, единицей, количеством и стоимостью. ВОР не содержит денежных данных и сравнивается отдельно.')
        for position in positions:
            if position['resources'] and not position.get('hasFooter'):
                # A top-row total can already include its resources; absent
                # explicit footer semantics it is retained, never multiplied.
                if position['total'] is None:
                    position['incomplete'] = True
            if position.get('hasFooter') and position['resources'] and number(position['components'].get('materials')) is not None:
                resources_total = known_sum(child['total'] for child in position['resources'])
                position['components']['materials'] = position['components']['materials'] + resources_total if resources_total is not None else None
            position['components'] = {key:value for key,value in position['components'].items()}
        return {'positions':positions, 'period':next(iter(periods)) if len(periods) == 1 else '',
                'priceLevel':next(iter(levels)) if len(levels) == 1 else 'unspecified',
                'file':Path(path).name, 'documentTotal':known_sum(document_totals)}
    finally:
        book.close()


def read_customer(path, *, llm=None):
    path = Path(path)
    if path.suffix.lower() == '.json':
        return from_data(json.loads(path.read_text(encoding='utf-8')), path.name)
    from .customer_documents import read_estimate
    if path.suffix.lower() == '.xlsx':
        try:
            result = read_xlsx(path)
            # A recognized first sheet must not conceal unrecognized later sheets.
            from .customer_documents.estimate import validate_native_coverage
            if validate_native_coverage(path,result):
                return result
        except ValueError:
            pass
        return read_estimate(path,llm=llm)
    if path.suffix.lower() == '.pdf':
        return read_estimate(path,llm=llm)
    raise ValueError('Для сравнения смет загрузите PDF, Excel XLSX или расчётный JSON сметы.')


def _scope(position):
    return re.sub(r'кол-во:\s*[\d.,]+', '', norm(position.get('scope'))).strip()


def _compatible(a, b):
    from .construction_method import construction_method as detect
    left_method = detect({'Name': a.get('scope', '') + ' ' + a['name']})
    right_method = detect({'Name': b.get('scope', '') + ' ' + b['name']})
    if left_method in ('monolithic', 'precast') and right_method in ('monolithic', 'precast') and left_method != right_method:
        return False
    for detector in (mapping.detect_part, mapping.detect_kind):
        left, right = detector(a['name']), detector(b['name'])
        if left and right and left != right: return False
        left, right = detector(a.get('scope', '') + ' ' + a['name']), detector(b.get('scope', '') + ' ' + b['name'])
        if left and right and left != right: return False
    return True


def _aggregate(positions):
    groups = {}
    for position in positions:
        key = (norm(position['code']), position['unit'], _scope(position), norm(position['name']))
        groups.setdefault(key, []).append(position)
    result = []
    for members in groups.values():
        if len(members) == 1:
            result.append(members[0]); continue
        record = dict(members[0])
        for field in ('total', 'quantity'): record[field] = known_sum(row[field] for row in members)
        record['incomplete'] = any(row.get('incomplete') for row in members)
        record['source'] = {'positions':[row['source'] for row in members]}
        record['components'] = {key:known_sum(row['components'].get(key) for row in members)
                                for key in set().union(*(row['components'] for row in members))}
        record['factors'] = {}
        for key in set().union(*(row['factors'] for row in members)):
            values = {json.dumps(_canonical_factors(row['factors'].get(key)),sort_keys=True):row['factors'].get(key) for row in members}
            record['factors'][key] = next(iter(values.values())) if len(values) == 1 else [values[k] for k in sorted(values)]
        result.append(record)
    return result


def compare_documents(generated, customer):
    ours, theirs = _aggregate(generated['positions']), _aggregate(customer['positions'])
    used, rows = set(), []
    period_same = bool(generated.get('period') and norm(generated['period']) == norm(customer.get('period')))
    basis_known = generated.get('priceLevel') == customer.get('priceLevel') == 'current' and period_same
    basis_different = bool(generated.get('period') and customer.get('period') and not period_same) or (
        generated.get('priceLevel') not in (None, 'unspecified') and customer.get('priceLevel') not in (None, 'unspecified')
        and generated['priceLevel'] != customer['priceLevel'])

    def append(a, b, ambiguous=False):
        result = {'generated':a, 'customer':b, 'quantityDifference':None, 'costDifference':None,
                  'quantityEffect':None, 'priceEffect':None, 'componentDifferences':{}, 'factorDifferences':{},
                  'missingDetails': []}
        if not a: status = 'only_customer'
        elif not b: status = 'multiple_candidates' if ambiguous else 'only_generated'
        else:
            qg, qc, tg, tc = (number(v) for v in (a['quantity'], b['quantity'], a['total'], b['total']))
            for field in ('components', 'factors'):
                for key in set(a[field]) | set(b[field]):
                    for side, position in (('generated', a), ('customer', b)):
                        if position[field].get(key) is None:
                            result['missingDetails'].append({'side':side, 'field':field, 'key':key})
            result['quantityDifference'] = qc-qg if qc is not None and qg is not None else None
            if tg is None or tc is None or a.get('incomplete') or b.get('incomplete'):
                status = 'missing_cost'
            elif basis_different:
                status = 'price_basis_difference'
            else:
                result['costDifference'] = tc-tg if basis_known else None
                if basis_known and qg not in (None, 0) and qc not in (None, 0):
                    result['quantityEffect'] = (qc-qg)*(tg/qg)
                    result['priceEffect'] = (tc-tg)-result['quantityEffect']
                for key in set(a['components']) & set(b['components']):
                    ag, bc = number(a['components'][key]), number(b['components'][key])
                    if ag is not None and bc is not None: result['componentDifferences'][key] = bc-ag
                for key in set(a['factors']) & set(b['factors']):
                    ag, bc = a['factors'][key], b['factors'][key]
                    if _canonical_factors(ag) != _canonical_factors(bc): result['factorDifferences'][key] = {'generated':ag,'customer':bc}
                different = abs(tc-tg) > Decimal('.01') or (result['quantityDifference'] is not None and abs(result['quantityDifference']) > Decimal('.000001'))
                different |= bool(result['factorDifferences']) or any(abs(v)>Decimal('.01') for v in result['componentDifferences'].values())
                status = ('different' if different else 'missing_quantity' if qg is None or qc is None else 'matched') if basis_known else 'price_basis_unknown'
        result['status'] = status
        rows.append(result)

    # Reserve exact correspondences before considering a unique broader scope;
    # the result must not depend on which side lists a less detailed row first.
    def has_exact(a):
        return any(norm(a['code']) == norm(b['code']) and a['unit'] == b['unit']
                   and _scope(a) == _scope(b) and norm(a['name']) == norm(b['name']) and _compatible(a,b)
                   for b in theirs)
    for a in sorted(ours, key=lambda row: not has_exact(row)):
        candidates = [i for i,b in enumerate(theirs) if i not in used and norm(a['code']) == norm(b['code'])
                      and a['unit'] == b['unit'] and _compatible(a,b)]
        exact = [i for i in candidates if _scope(a) == _scope(theirs[i]) and norm(a['name']) == norm(theirs[i]['name'])]
        if exact: candidates = exact
        if len(candidates) == 1:
            i = candidates[0]; used.add(i); append(a,theirs[i])
        else: append(a,None,ambiguous=bool(candidates))
    for i,b in enumerate(theirs):
        if i not in used: append(None,b)
    total_g, total_c = known_sum(p['total'] for p in ours), known_sum(p['total'] for p in theirs)
    return {'version':1, 'generatedFile':generated.get('file'), 'customerFile':customer.get('file'),
            'generatedPeriod':generated.get('period'), 'customerPeriod':customer.get('period'),
            'priceBasisComparable':basis_known, 'rows':rows,
            'summary':{'byStatus':dict(Counter(r['status'] for r in rows)), 'generatedTotal':total_g, 'customerTotal':total_c,
                       'difference':total_c-total_g if total_g is not None and total_c is not None and basis_known
                       and not any(p.get('incomplete') for p in [*ours,*theirs]) else None}}


def _canonical_factors(value):
    if isinstance(value, dict): return {key:_canonical_factors(v) for key,v in value.items() if v is not None}
    if isinstance(value, list): return sorted((_canonical_factors(v) for v in value), key=lambda v:json.dumps(v,sort_keys=True))
    numeric = number(value)
    return str(numeric.normalize()) if numeric is not None else norm(value)


def write_report(result, path):
    book = Workbook()
    summary = book.active; summary.title = 'Итоги'
    summary.append(['Показатель','Наша ЛСР','Смета заказчика'])
    summary.append(['Файл',result['generatedFile'],result['customerFile']])
    summary.append(['Период цен',result['generatedPeriod'],result['customerPeriod']])
    summary.append(['Итого по позициям',result['summary']['generatedTotal'],result['summary']['customerTotal']])
    summary.append(['Разница при сопоставимом периоде',result['summary']['difference']])
    if not result['priceBasisComparable']:
        summary.append(['Уровень или период цен различается либо не указан. Итоги показаны отдельно; общий вывод о равенстве стоимости не формируется.'])
    sheet = book.create_sheet('Сравнение смет')
    sheet.append(['Раздел / конструкция','Шифр нашей ЛСР','Шифр заказчика','Работа','Ед. без множителя','Количество нашей ЛСР',
                  'Количество заказчика','Разница количества','Стоимость нашей ЛСР','Стоимость заказчика','Разница стоимости',
                  'Влияние количества','Влияние стоимости единицы','Результат','Источник нашей ЛСР','Источник заказчика'])
    detail = book.create_sheet('Начисления и коэффициенты')
    detail.append(['Шифр','Работа','Разница ЗП','Разница ЭМ','Разница МР','Разница НР','Разница СП','Различия коэффициентов и индексов',
                   'Недостающие сведения для подробного сравнения', 'Состав затрат нашей ЛСР', 'Состав затрат заказчика',
                   'Коэффициенты нашей ЛСР', 'Коэффициенты заказчика', 'Ресурсы нашей ЛСР', 'Ресурсы заказчика'])
    for row in result['rows']:
        a,b = row['generated'] or {}, row['customer'] or {}
        sheet.append([a.get('scope') or b.get('scope'),a.get('code'),b.get('code'),a.get('name') or b.get('name'),a.get('unit') or b.get('unit'),
                      a.get('quantity'),b.get('quantity'),row['quantityDifference'],a.get('total'),b.get('total'),row['costDifference'],
                      row['quantityEffect'],row['priceEffect'],LABELS[row['status']],json.dumps(a.get('source') or {},ensure_ascii=False),json.dumps(b.get('source') or {},ensure_ascii=False)])
        detail.append([a.get('code') or b.get('code'),a.get('name') or b.get('name'),
                       *[row['componentDifferences'].get(k) for k in ('labor','machines','materials','overhead','profit')],
                       *[json.dumps(value,ensure_ascii=False,default=str) for value in (
                           row['factorDifferences'],row['missingDetails'],a.get('components'),b.get('components'),
                           a.get('factors'),b.get('factors'),a.get('resources'),b.get('resources'))]])
    for current in book:
        for cell in current[1]: cell.font = Font(bold=True)
        for i in range(1,current.max_column+1): current.column_dimensions[get_column_letter(i)].width = 30
        for cells in current:
            for cell in cells:
                if isinstance(cell.value,Decimal): cell.value=float(cell.value);cell.number_format='#,##0.00######'
                if isinstance(cell.value,str): cell.data_type='s'
                cell.alignment=Alignment(wrap_text=True,vertical='top')
            current.row_dimensions[cells[0].row].height=65
        current.auto_filter.ref=current.dimensions
    sheet.column_dimensions['D'].width=65
    detail.column_dimensions['H'].width=80
    book.save(path);book.close()


def build(folder, customer_path, customer_name=None):
    from .estimate_export import DATA_FILENAME as SOURCE_DATA
    folder=Path(folder)
    own=folder/SOURCE_DATA
    if not own.is_file(): raise ValueError('Сначала сформируйте нашу ЛСР по перечню работ.')
    generated=from_data(json.loads(own.read_text(encoding='utf-8')), 'Смета_по_сформированному_перечню.xlsx')
    customer=read_customer(customer_path)
    if customer_name:
        customer['file']=customer_name
        for row in customer['positions']: row['source']['file']=customer_name
    result=compare_documents(generated,customer)
    path=folder/FILENAME
    temporary=folder/(FILENAME+'.tmp.xlsx')
    try:
        write_report(result,temporary);temporary.replace(path)
    finally: temporary.unlink(missing_ok=True)
    (folder/DATA_FILENAME).write_text(json.dumps(result,ensure_ascii=False,default=str),encoding='utf-8')
    return {'file':str(path),'data_file':str(folder/DATA_FILENAME),'summary':result['summary'],
            'positions':len(result['rows']),'priceBasisComparable':result['priceBasisComparable']}
