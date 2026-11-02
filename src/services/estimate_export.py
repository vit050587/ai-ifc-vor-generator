"""Separate NIAC estimate from catalog base prices, factors and period indices.

The original final workbook is never changed. Quantity corrections and current
price totals already included upstream are retained as control references only.
"""
import hashlib
import json
import logging
import os
import re
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter

from .estimate_calculation import money, number, sum_known

logger = logging.getLogger(__name__)
FILENAME = 'Смета_по_сформированному_перечню.xlsx'
DATA_FILENAME = 'Смета_по_перечню_данные.json'
STATUS = {'unchanged':'без дополнительных множителей','applied':'добавлены только неучтённые множители',
          'already_applied':'коэффициенты уже учтены ранее; повторно не применяются',
          'unverified_prior_factors':'не удалось проверить ранее учтённые множители; исходная сумма сохранена',
          'missing_amount':'исходная сумма отсутствует','invalid_factor':'некорректный коэффициент; исходная сумма сохранена'}
HEADERS = ['№ п/п','Шифр расценки и коды ресурсов','Наименование работ и затрат','Ед. изм.',
           'Кол-во единиц','Цена на ед. изм., руб.','Поправочные коэффициенты',
           'Коэффициенты зимних удорожаний','Всего в базисных ценах, руб.',
           'Индексы пересчёта, нормы НР и СП','Всего в текущем уровне цен, руб.']


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def source_file(folder):
    folder = Path(folder)
    for name in ('Финальный_перечень_работ.xlsx','ОБЩИЙ_Финальный_перечень_работ.xlsx'):
        if (folder / name).is_file():
            return folder / name
    raise ValueError('Нет готового Excel со сформированным перечнем работ')


def _norm(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip().casefold()


def _metadata(folder):
    data = _json(folder / 'Финальный_перечень_работ.json')
    records = []
    for group in data.get('groups', []):
        header = group.get('element', '') + (f" ({group['material']})" if group.get('material') else '') + f"  Кол-во: {group.get('count')}"
        if group.get('subsection'):
            header += f"  [{group['subsection']}]"
        if group.get('height_context'):
            from .work_height import label
            header += '  [' + label(group['height_context']) + ']'
        for work in group.get('rows', []):
            records.append((header, work, {'name':group.get('element'),
                           'height_context':group.get('height_context'), **(group.get('quantity') or {})}))
    for entry in data.get('elements', []):
        el = entry.get('element') or {}
        facts = dict(el.get('row') or {}, **{k:v for k,v in el.items() if k != 'row'})
        facts['part'] = entry.get('part')
        facts['height_context'] = entry.get('height_context')
        for work in entry.get('selected_works', []):
            records.append((entry.get('group_header') or el.get('name'), work, facts))
    return data, records


def _period(folder, metadata):
    candidates = [metadata, _json(folder / 'Подобранные_работы.json'),
                  _json(folder / 'period.json'), _json(folder.parent / 'period.json')]
    for data in candidates:
        identifier = data.get('period_id') or data.get('id')
        if identifier:
            return {'id':identifier, 'title':data.get('period') or data.get('title') or str(identifier)}
    return {'id':None, 'title':'Не указан в сохранённых данных перечня'}


def _rows(path, metadata):
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book['Данные'] if 'Данные' in book.sheetnames else book.active
        header = [str(c or '').strip() for c in next(sheet.iter_rows(values_only=True))]
        required = ['Шифр ТСН','Наименование расценки/ресурса','Ед. изм.','Объём работ','ЗП','ЭМ','МР','Стоимость']
        if not all(name in header for name in required):
            raise ValueError('Не распознаны столбцы сформированного перечня')
        group = ''
        group_id = 0
        rows = []
        for source_row, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), 2):
            d = {name: row[header.index(name)] for name in required}
            code, title = str(d['Шифр ТСН'] or '').strip(), str(d['Наименование расценки/ресурса'] or '').strip()
            if not code:
                if title and not title.upper().startswith('ИТОГО'):
                    group = title
                    group_id = source_row
                    rows.append({'heading':title})
                continue
            matches = [(w,f) for h,w,f in metadata if str(w.get('pressmark') or '') == code and _norm(h) == _norm(group)]
            if len(matches) > 1:
                matches = [(w,f) for w,f in matches if _norm(w.get('title')) == _norm(title)]
            work, facts = matches[0] if len(matches) == 1 else ({}, {'name':group})
            rows.append({'code':code,'title':title,'unit':d['Ед. изм.'],
                         'quantity_exact':float(number(d['Объём работ'])) if number(d['Объём работ']) is not None else None,
                         'amounts':{k:money(number(d[c])) for k,c in [('labor','ЗП'),('machines','ЭМ'),('materials','МР')]},
                         'source_total':money(number(d['Стоимость'])), 'group':group,'group_id':group_id,'source_row':source_row,
                         'work':work,'facts':facts})
        return rows
    finally:
        book.close()


def _details(folder, rows, period):
    """Fetch base prices and indices strictly for the saved selected period."""
    details = {}
    rates = _json(folder / 'Подобранные_работы.json')
    ids = {str(w.get('pressmark')):w.get('id') for table in rates.get('tables', []) for w in table.get('works', [])}
    for row in rows:
        if 'code' not in row:
            continue
        work = row['work']
        if work.get('_price_detail') and str(work.get('_price_period_id')) == str(period['id']):
            details[row['code']] = work['_price_detail']
        ids[row['code']] = work.get('work_id') or ids.get(row['code'])
    cache_path = folder / '.estimate_rate_details.json'
    cached = _json(cache_path)
    if str(cached.get('period_id')) == str(period['id']):
        for code,detail in (cached.get('details') or {}).items():
            if code not in details and (not cached.get('ids') or str(cached['ids'].get(code)) == str(ids.get(code))):
                details[code]=detail
    wanted = {row['code']:ids.get(row['code']) for row in rows if 'code' in row and row['code'] not in details}
    if period['id'] and any(wanted.values()):
        from .works_fetcher import fetch_work_details
        fetched = fetch_work_details(sorted({int(v) for v in wanted.values() if v}), int(period['id']))
        for code, wid in wanted.items():
            detail = fetched.get(int(wid)) if wid else None
            if detail and str(detail.get('pressmark') or code) == code:
                details[code] = detail
        cache_path.write_text(json.dumps({'period_id':period['id'],'details':details,'ids':ids},ensure_ascii=False),encoding='utf-8')
    return details


def _extra(folder):
    result = {}
    selection = _json(folder / 'Подобранные_таблицы_работ.json')
    resolved = selection.get('global_constants') or {}
    applied = [item.get('applied_constants') or {} for item in selection.get('elements', [])]
    common = {}
    for key in {key for values in applied for key in values}:
        values = [values.get(key) for values in applied]
        if values and values[0] is not None and all(value == values[0] for value in values):
            common[key] = values[0]
    if common or resolved:
        result['Параметры расчёта модели и документов'] = dict(common,**resolved)
    for name in ('works_constants.json','IFC_глобальные_константы.json','ПОС_глобальные_константы.json','ПЗ_глобальные_константы.json','pd_full.json'):
        path = folder / name
        if not path.exists():
            path = folder.parent / name
        value = _json(path)
        if value:
            result[name] = value
    return result


def _coefficients(row, extra, model_state=None):
    from . import tsn_coeffs
    match = re.match(r'^3\.(\d+)-',row['code'])
    if match and not any(str(doc.get('code','')).endswith('-'+match[1]) for doc in tsn_coeffs.docs()):
        raise ValueError('Не загружен сборник ТСН для проверки поправок этой расценки')
    def ask(prompt):
        if model_state is not None and model_state.get('failed'):
            raise RuntimeError('Модель недоступна; повторите поиск коэффициентов позднее')
        from .pd_full import ask as model_ask
        try:
            return model_ask(prompt,timeout=60)
        except Exception:
            if model_state is not None:
                model_state['failed']=True
            raise
    decisions = row['work'].get('tsn_coeffs') or tsn_coeffs.decide(
        row['code'], row['title'], row['facts'], row['work'].get('quantity') or {}, extra, ask_fn=ask)
    canonical = {(doc,str(item.get('clause'))):item for doc,item in tsn_coeffs.candidates(row['code'])}
    out = []
    for decision in decisions:
        item = canonical.get((decision.get('doc'),str(decision.get('clause'))))
        if item:
            ambiguous = item.get('from_text') and len(set(tsn_coeffs._COEF_RE.findall(item.get('condition') or ''))) > 1
            out.append(dict(decision, coefficients=item.get('coefficients') or {}, ambiguous=bool(ambiguous),
                            resource=item.get('applies_to_resource'), source_page=item.get('source_page')))
    return out


def _row_decisions(rows, extra, provider, model_state, warnings):
    decisions={}
    scoped={}
    for index,row in enumerate(rows):
        if 'code' not in row or row['code'].startswith('1.'):
            continue
        try:
            found=provider(row,extra) if provider else _coefficients(row,extra,model_state)
        except Exception as exc:
            logger.warning('Поиск поправок %s: %s',row['code'],exc)
            found=[]
            warnings.append(f"{row['code']}: не удалось завершить проверку поправочных коэффициентов. Повторите поиск; до проверки возможные дополнительные поправки не учтены.")
        if not provider:
            from . import tsn_coeffs
            from .work_height import height_decisions
            found = height_decisions(row['code'],row['title'],row['facts'],tsn_coeffs.candidates(row['code']),found)
        decisions[index]=found
        scoped.setdefault(row.get('group_id',row['group']),[]).extend(d for d in found if d.get('resource'))
    for index,row in enumerate(rows):
        if 'code' not in row or not row['code'].startswith('1.'):
            continue
        decisions[index]=[dict(d,coefficients={'material_consumption':d.get('coefficients',{}).get('material_consumption')})
                          for d in scoped.get(row.get('group_id',row['group']),[]) if _norm(d['resource']) in _norm(row['title'])]
    return decisions


def _corrections(decisions, row):
    products = {k:Decimal(1) for k in ('labor','machines','materials')}
    notes = {k:[] for k in products}
    fields = {'labor_and_wages':'labor','machine_operation':'machines','material_consumption':'materials'}
    seen = set()
    for d in decisions:
        identity = (d.get('doc'),d.get('clause'),d.get('resource'))
        if identity in seen:
            continue
        seen.add(identity)
        if d.get('decision') == 'нет':
            continue
        for field,key in fields.items():
            factor = number(d.get('coefficients', {}).get(field))
            if factor is None:
                continue
            why = d.get('why') or 'Условие не подтверждено исходными данными'
            if d.get('height_check'):
                parts = why.replace('IFC','модели здания').split(';')
                if len(parts) > 1 and re.search(r'[A-Za-z_]',parts[1]):
                    parts[1] = ' отметки взяты из модели здания'
                why = ';'.join(parts)
            factor_text = str(factor).replace('.',',')
            text = f"Коэффициент: {factor_text}. Сборник: {d.get('doc')}, пункт {d.get('clause')}" + (f", стр. {d['source_page']}" if d.get('source_page') else '')
            text += f". Условие: {d.get('condition')}. Основание: {why}."
            eligible = d.get('decision') == 'да' and bool(d.get('why')) and factor > 0 and not d.get('ambiguous')
            if d.get('ambiguous'):
                text += ' В текстовом пункте несколько коэффициентов; требуется уточнить соответствие условия и значения.'
            is_resource=row['code'].startswith('1.') or bool((row.get('work') or {}).get('is_resource'))
            if key == 'materials' and d.get('resource') and (not is_resource or _norm(d['resource']) not in _norm(row['title'])):
                eligible = False
                text += ' Поправка относится к отдельному ресурсу; к общей сумме МР не применяется.'
            text += ' Условие подтверждено.' if eligible else ' Не применяется автоматически; требуется проверка.'
            notes[key].append(text)
            if eligible:
                products[key] *= factor
    return products, notes


def _unit_rate(row, detail, key):
    fields = {'labor':('cur_salary','curSalary'), 'machines':('cur_operation_of_machines','curOperationOfMachines'),
              'materials':('cur_cost_of_material_resources','curCostOfMaterialResources')}
    snake, camel = fields[key]
    if row['work'].get(snake) is not None:
        return row['work'][snake]
    return detail.get(camel)


def prepare(folder, *, project_name='', detail_provider=None, coefficient_provider=None):
    folder = Path(folder)
    source = source_file(folder)
    data, metadata = _metadata(folder)
    period = _period(folder, data)
    source_rows = _rows(source, metadata)
    if not any('code' in row for row in source_rows):
        raise ValueError('Сформированный перечень пока не содержит работ для сметной формы')
    details = (detail_provider or _details)(folder, source_rows, period)
    extra = _extra(folder)
    records = []
    warnings = []
    model_state = {}
    decisions_by_row=_row_decisions(source_rows,extra,coefficient_provider,model_state,warnings)
    for row_index,row in enumerate(source_rows):
        if 'code' not in row:
            records.append(row)
            continue
        detail = details.get(row['code']) or {}
        decisions=decisions_by_row.get(row_index,[])
        factors, notes = _corrections(decisions, row)
        if any(d.get('decision') == 'неясно' for d in decisions):
            warnings.append(f"{row['code']}: есть поправки с неподтверждёнными условиями; они не применены")
        calculated = {}
        resource = row['code'].startswith('1.')
        for key in factors:
            winter_key = 'winterRisePriceMat' if key == 'materials' else 'winterRisePrice'
            winter = detail.get(winter_key)
            winter = winter if number(winter) is not None and number(winter) > 0 else 1
            amount = 0 if resource and key != 'materials' else row['amounts'][key]
            result = {'value':amount,'status':'unchanged','applied':[]}
            result.update(correction=float(factors[key]), winter=float(winter), notes=notes[key], source_amount=amount,
                          winter_source=number(detail.get(winter_key)) is not None and number(detail.get(winter_key)) > 0)
            calculated[key] = result
        row.update(components=calculated, drivers=None, overhead=None, profit=None,
                   overhead_rate=detail.get('rateOverhead'), profit_rate=detail.get('rateProfit'),
                   decisions=decisions, catalog_detail=detail,
                   unit_rates={key:_unit_rate(row,detail,key) for key in factors},
                   driver_unit_rate=0 if resource else detail.get('curSalaryDrivers'))
        records.append(row)
    result = {'source':str(source),'project_name':project_name,'period':period,'rows':records,
              'warnings':list(dict.fromkeys(warnings))}
    from .estimate_formulas import round_costs
    round_costs(result)
    return result


def _comment(cell, text):
    if text:
        cell.comment = Comment(text, 'Пояснение')
        cell.comment.width,cell.comment.height = 550,240


def write_workbook(data, path):
    """Use the application's Excel engine and add editable native Excel formulas."""
    from .estimate_formulas import round_costs
    round_costs(data)
    book = Workbook()
    sheet = book.active
    sheet.title = 'Смета по перечню'
    sheet.sheet_view.showGridLines = False
    for row,text in [(2,'Форма № 4б'),(3,data['project_name'] or 'Сформированный перечень работ'),
                     (6,'СМЕТА ПО СФОРМИРОВАННОМУ ПЕРЕЧНЮ РАБОТ'),
                     (8,'Основание: готовый перечень работ по модели IFC и документам'),
                     (10,'Текущий период ТСН: '+str(data['period']['title'])),
                     (12,'Базисная и текущая стоимость рассчитываются по ТСН-2001.12 из базисных цен и индексов выбранного периода.'),
                     (13,'Цена единицы сохраняет исходную точность; итоговые денежные суммы округляются до копеек.')]:
        sheet.merge_cells(start_row=row,start_column=1,end_row=row,end_column=11)
        sheet.cell(row,1,text).alignment = Alignment(wrap_text=True, vertical='center',horizontal='center' if row in (3,6) else 'left')
        sheet.row_dimensions[row].height = 32 if row in (3,10,12,13) else 22
    sheet.cell(2,1).alignment=Alignment(horizontal='right',vertical='center')
    sheet.cell(6,1).font = Font(name='Arial',size=13,bold=True)
    for c,title in enumerate(HEADERS,1):
        cell=sheet.cell(16,c,title)
        cell.font=Font(name='Arial',size=10,bold=True)
        cell.alignment=Alignment(wrap_text=True,vertical='center',horizontal='center')
        cell.border=Border(*( [Side(style='thin',color='444444')]*4 ))
        sheet.cell(17,c,c)
    sheet.row_dimensions[16].height=95
    grand_total=sum_known([r['total'] for r in data['rows'] if 'code' in r])
    incomplete_count=sum(r.get('incomplete',r['total'] is None) for r in data['rows'] if 'code' in r)
    sheet.merge_cells('A7:J7')
    sheet.cell(7,1,'Сметная стоимость в текущем уровне цен, руб.')
    sheet.cell(7,11,grand_total).number_format='#,##0.00'
    if incomplete_count:
        _comment(sheet.cell(7,11),f'Итог по известным суммам. Неполных позиций: {incomplete_count}. Отсутствующие затраты не включены; значения в исходных строках остаются пустыми.')
    i,position=19,0
    totals=[]
    section_totals=[]
    def finish_section():
        nonlocal i
        if section_totals:
            sheet.merge_cells(start_row=i,start_column=1,end_row=i,end_column=8)
            sheet.cell(i,1,'Итого по разделу:' if not any(partial for _,partial in section_totals) else 'Итого по разделу (по известным суммам): есть неполные позиции')
            sheet.cell(i,11,sum_known(value for value,_ in section_totals))
            if any(partial for _,partial in section_totals):
                _comment(sheet.cell(i,11),'Сумма всех известных затрат раздела, включая неполные позиции. Отсутствующие затраты не включены.')
            sheet.cell(i,1).font=Font(name='Arial',size=10,bold=True)
            i+=2
            section_totals.clear()
    for record in data['rows']:
        if 'heading' in record:
            finish_section()
            sheet.merge_cells(start_row=i,start_column=1,end_row=i,end_column=11)
            sheet.cell(i,1,'Раздел: '+record['heading']).font=Font(name='Arial',size=11,bold=True)
            sheet.cell(i,1).alignment=Alignment(wrap_text=True,vertical='center')
            sheet.row_dimensions[i].height=32
            i+=1
            continue
        position+=1
        for column,value in enumerate([position,record['code'],record['title'],record['unit'],record['quantity_exact']],1):
            sheet.cell(i,column,value)
        sheet.row_dimensions[i].height=max(42,14*(len(record['title'])//45+1))
        _comment(sheet.cell(i,5),f"Количество уже приведено к единице расценки в исходном перечне, строка {record['source_row']}. Повторное умножение на объём и норму расхода не выполняется.")
        i+=1
        _,comment_notes=_corrections(record.get('decisions',[]),record)
        for key,label in [('labor','ЗП'),('machines','ЭМ'),('materials','МР')]:
            item=record['components'][key]
            sheet.cell(i,3,label)
            sheet.cell(i,7,item['correction'] if item['correction'] != 1 else 'См. комментарий' if comment_notes[key] else None)
            sheet.cell(i,8,item['winter'] if item['winter'] != 1 else None)
            sheet.cell(i,11,item['value'])
            explanation='\n\n'.join(comment_notes[key])
            _comment(sheet.cell(i,7),explanation)
            winter_note=(f"Зимнее удорожание из параметров расценки {record['code']} за выбранный период {data['period']['title']}. {STATUS[item['status']]}."
                         if item.get('winter_source',item['winter'] != 1) else 'Данные зимнего удорожания выбранного периода не предоставлены. Зимний коэффициент не применялся.')
            _comment(sheet.cell(i,8),winter_note)
            added=', '.join({'correction':'поправочный коэффициент','winter':'зимний коэффициент'}[key] for key in item['applied']) or 'нет'
            _comment(sheet.cell(i,11),f"Исходная сумма из текущего перечня: {item['source_amount']}; поправка: {item['correction']}; зимний коэффициент: {item['winter']}. {STATUS[item['status']]}. Добавленные множители: {added}.\n{explanation}")
            i+=1
            if key=='machines':
                sheet.cell(i,3,'в т.ч. ЗПМ');sheet.cell(i,11,record['drivers'])
                _comment(sheet.cell(i,11),'Зарплата машинистов уже входит в ЭМ; повторно в итог не суммируется.')
                i+=1
        for label,field in [('НР от ФОТ','overhead'),('СП от ФОТ','profit')]:
            sheet.cell(i,3,label);sheet.cell(i,4,'%');sheet.cell(i,5,record[field+'_rate']);sheet.cell(i,10,record[field+'_rate']);sheet.cell(i,11,record[field])
            if record[field] is None:
                _comment(sheet.cell(i,11),'Нет подтверждённых норм НР/СП или зарплаты машинистов выбранного периода. Значение не заменяется нулём.')
            else:
                _comment(sheet.cell(i,11),f"(ЗП {record['components']['labor']['value']} + ЗПМ {record['drivers']}) × {record[field+'_rate']} / 100. Норма из параметров расценки за период {data['period']['title']}. ЗПМ уже входит в ЭМ и в прямые затраты повторно не добавляется.")
            i+=1
        sheet.cell(i,3,'ЗТР');sheet.cell(i,4,'чел.-ч');i+=1
        sheet.cell(i,3,'ИТОГО прямых затрат по позиции:');sheet.cell(i,11,record['direct']);i+=1
        sheet.cell(i,3,'ВСЕГО по позиции:' if not record.get('incomplete',record['total'] is None) else 'ВСЕГО по позиции (по известным суммам):')
        sheet.cell(i,11,record['total'])
        if record.get('incomplete',record['total'] is None):
            _comment(sheet.cell(i,11),'Позиция неполная. Суммированы известные ЗП, ЭМ, МР, НР и СП; отсутствующие значения не включены. ЗПМ повторно не добавляется.')
        sheet.cell(i,3).font=sheet.cell(i,11).font=Font(name='Arial',size=10,bold=True)
        totals.append(record['total']);section_totals.append((record['total'],record.get('incomplete',record['total'] is None)));i+=2
    finish_section()
    sheet.merge_cells(start_row=i,start_column=1,end_row=i,end_column=8)
    sheet.cell(i,1,'ИТОГО ПО СМЕТЕ:' if not incomplete_count else 'ИТОГО ПО СМЕТЕ (по известным суммам): есть неполные позиции')
    sheet.cell(i,11,sum_known(totals));sheet.cell(i,1).font=Font(name='Arial',size=11,bold=True)
    if incomplete_count:
        _comment(sheet.cell(i,11),f'Неполных позиций: {incomplete_count}. Включены все известные суммы всех разделов. Недостающие затраты требуют уточнения.')
    for warning in data['warnings']:
        i+=1;sheet.merge_cells(start_row=i,start_column=1,end_row=i,end_column=11)
        sheet.cell(i,1,'При формировании: '+warning).alignment=Alignment(wrap_text=True,vertical='center');sheet.row_dimensions[i].height=28
    for column,width in enumerate([7,17,65,13,14,15,17,16,19,19,22],1):
        sheet.column_dimensions[get_column_letter(column)].width=width
    for cells in sheet.iter_rows(min_row=18,max_row=i):
        for cell in cells:
            if not cell.has_style:
                cell.font=Font(name='Arial',size=10)
            cell.alignment=Alignment(wrap_text=True,vertical='top',horizontal='right' if isinstance(cell.value,(int,float)) else 'left')
            if cell.column in (5,7,8) and isinstance(cell.value,(int,float)):
                places=max(0,-number(cell.value).normalize().as_tuple().exponent)
                cell.number_format='#,##0'+('.'+'0'*min(places,8) if places else '')
            if cell.column in (6,9,11):cell.number_format='#,##0.00'
        if cells[2].value and len(str(cells[2].value)) > 60:
            sheet.row_dimensions[cells[0].row].height=max(sheet.row_dimensions[cells[0].row].height or 15,28)
    for cells in sheet.iter_rows(min_row=1,max_row=17):
        for cell in cells:
            if cell.font.name == 'Calibri':
                cell.font=Font(name='Arial',size=10)
    for cells in sheet:
        for cell in cells:
            if isinstance(cell.value,str):
                cell.data_type='s'
    sheet.print_title_rows='16:17'
    sheet.print_area=f'A1:K{i}'
    sheet.page_setup.orientation='landscape';sheet.page_setup.paperSize=sheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth=1;sheet.page_setup.fitToHeight=0
    sheet.sheet_properties.pageSetUpPr.fitToPage=True
    from .estimate_formulas import apply, cache_results
    formula_cache = apply(book,data)
    try:
        book.save(path)
    finally:
        book.close()
    cache_results(path,formula_cache)


def _fingerprint(folder, project_name):
    from . import tsn_coeffs
    names = ('Финальный_перечень_работ.json','Подобранные_работы.json','Подобранные_таблицы_работ.json','period.json',
             'works_constants.json','IFC_глобальные_константы.json','ПОС_глобальные_константы.json',
             'ПЗ_глобальные_константы.json','pd_full.json','.estimate_rate_details.json')
    paths = {source_file(folder),Path(__file__),Path(__file__).with_name('estimate_calculation.py'),Path(__file__).with_name('estimate_formulas.py'),Path(__file__).with_name('work_height.py'),Path(tsn_coeffs.__file__)}
    paths.update(p/name for p in (folder,folder.parent) for name in names if (p/name).is_file())
    paths.update(Path(tsn_coeffs.TSN_DIR).glob('*.json'))
    digests=[]
    for path in sorted(paths):
        with path.open('rb') as stream:
            digests.append((str(path),hashlib.file_digest(stream,'sha256').hexdigest()))
    return hashlib.sha256(json.dumps([project_name,digests],ensure_ascii=False).encode()).hexdigest()


def build(folder, *, project_name='', detail_provider=None, coefficient_provider=None, force=False):
    from .vor_generation.formation_cache import session_lock
    folder=Path(folder)
    with session_lock(folder):
        target=folder/FILENAME
        manifest=folder/'.estimate_export_cache.json'
        use_cache=detail_provider is None and coefficient_provider is None
        if use_cache and not force and target.is_file():
            cached=_json(manifest)
            if cached.get('key') == _fingerprint(folder,project_name):
                with target.open('rb') as stream:
                    if cached.get('output_digest') == hashlib.file_digest(stream,'sha256').hexdigest():
                        return dict(cached['result'],cacheHit=True)
        data=prepare(folder,project_name=project_name,detail_provider=detail_provider,coefficient_provider=coefficient_provider)
        temporary=folder/(FILENAME+'.tmp.xlsx')
        try:
            write_workbook(data,temporary)
            os.replace(temporary,target)
        finally:
            temporary.unlink(missing_ok=True)
        (folder/DATA_FILENAME).write_text(json.dumps(data,ensure_ascii=False,default=str),encoding='utf-8')
        result={'file':str(target),'positions':sum('code' in r for r in data['rows']), 'warnings':data['warnings'],'period':data['period'],'cacheHit':False}
        if use_cache and not data['warnings']:
            with target.open('rb') as stream:
                digest=hashlib.file_digest(stream,'sha256').hexdigest()
            manifest.write_text(json.dumps({'key':_fingerprint(folder,project_name),'output_digest':digest,'result':result},ensure_ascii=False),encoding='utf-8')
        else:
            manifest.unlink(missing_ok=True)
        return result
