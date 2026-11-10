"""Native Excel calculations for the separate NIAC estimate (TSN addition 83)."""
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from openpyxl.styles import Font, Alignment
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.workbook.properties import CalcProperties

from .estimate_calculation import number, money, overhead, sum_known

MAIN = 'Смета по перечню'
BLUE = '0000FF'
GREEN = '008000'
METHOD = 'ТСН-2001.12, дополнение 83, пп. 3.4.19, 3.4.22–3.4.23'
COMPONENT_NAMES = {'labor':'зарплата рабочих', 'machines':'эксплуатация машин',
                   'materials':'материалы', 'drivers':'зарплата машинистов'}
FIELDS = {
    'labor':('salary','salaryInflRate','curSalary'),
    'machines':('operationOfMachines','machinesInflRate','curOperationOfMachines'),
    'materials':('costOfMaterialResources','materialInflRate','curCostOfMaterialResources'),
    'drivers':('salaryDrivers','salaryInflRate','curSalaryDrivers'),
}


def _round_money(expression):
    # Normalize floating-point noise at the final half-kopeck, never unit prices.
    return f'SIGN({expression})*ROUND(ROUND(ABS({expression})*100,6),0)/100'


def _sum_formula(refs, rounded=True):
    refs = ','.join(refs)
    expression = f'ROUND(SUM({refs}),2)' if rounded else f'SUM({refs})'
    return f'=IF(COUNT({refs})=0,"",{expression})'


def round_costs(data):
    """Calculate from catalog base prices; keep source totals for control only."""
    warnings = []
    for record in data['rows']:
        if 'code' not in record:
            continue
        detail = record.get('catalog_detail') or record.get('work',{}).get('_price_detail') or {}
        record['catalog_detail'] = detail
        quantity = number(record['quantity_exact'])
        resource = record['code'].startswith('1.')
        calculation = {}
        base_values = {}
        current_values = {}
        for key,(field,index_field,current_field) in FIELDS.items():
            owner = record['components']['machines' if key == 'drivers' else key]
            rate = number(detail.get(field))
            if resource and key != 'materials':
                rate = number(0)
            index = number(detail.get(index_field))
            index = index if index is not None and index > 0 else None
            correction = number(record.get('driver_correction',owner['correction']) if key == 'drivers' else owner['correction'])
            current_winter = number(detail.get('winterRisePriceMat' if key == 'materials' else 'winterRisePrice'))
            base_winter = number(detail.get('baseWinterRisePriceMat' if key == 'materials' else 'baseWinterRisePrice'))
            current_winter = current_winter if current_winter is not None and current_winter > 0 else None
            base_winter = base_winter if base_winter is not None and base_winter > 0 else None
            fallback = None if key == 'drivers' else owner['source_amount']
            if key == 'drivers' and number(detail.get(current_field)) == 0:
                fallback = 0
            base = current = None
            if rate is not None and quantity is not None and correction is not None and correction > 0:
                base = money(rate*quantity*correction*(base_winter if base_winter is not None else number(1)))
                if rate == 0 or quantity == 0:
                    current = 0
                elif index is not None:
                    current = money(rate*quantity*correction*(current_winter if current_winter is not None else number(1))*index)
            if current is None:
                current = fallback
            complete = (quantity is not None and rate is not None and correction is not None and correction > 0
                        and (rate == 0 or (index is not None and current_winter is not None)))
            calculation[key] = {
                'base_price':float(rate) if rate is not None else None,
                'index':float(index) if index is not None else None,
                'correction':float(correction) if correction is not None else None,
                'base_winter':float(base_winter) if base_winter is not None else None,
                'current_winter':float(current_winter) if current_winter is not None else None,
                'reference_price':detail.get(current_field),'fallback':fallback,
                'complete':complete,
            }
            reference = number(detail.get(current_field))
            if rate is not None and index is not None and reference is not None and abs(rate*index-reference) > number('.005'):
                warnings.append(f"{record['code']}: {COMPONENT_NAMES[key]} — текущая цена в справочнике отличается от базисной цены, умноженной на индекс. В смете выполнен расчёт по методике НИАЦ")
            base_values[key],current_values[key] = base,current
            if key != 'drivers':
                owner['value'] = current
            if not complete and key != 'drivers':
                warnings.append(f"{record['code']}: {COMPONENT_NAMES[key]} — недостаточно данных для полного расчёта. Если в перечне есть готовая сумма, она сохранена; проверьте пояснение к позиции")
        # A coefficient applying only to operator wages adds/subtracts that wage
        # difference inside EM; NR/SP then account for it through the same FOT.
        machine,driver = calculation['machines'],calculation['drivers']
        delta = number(driver['correction'])-number(machine['correction'])
        if delta and quantity is not None:
            for target,winter_field in ((base_values,'base_winter'),(current_values,'current_winter')):
                index = number(1) if target is base_values else number(driver['index'])
                price = number(driver['base_price'])
                winter = number(driver[winter_field])
                if price is None or index is None:
                    target['machines'] = None
                elif target['machines'] is not None:
                    # Reconstruct the full machine expression so rounding occurs
                    # after adding the wage supplement, not between operations.
                    mprice,mwinter = number(machine['base_price']),number(machine[winter_field])
                    mindex = number(1) if target is base_values else number(machine['index'])
                    if mprice is None or mindex is None:
                        target['machines'] = None
                    else:
                        main = mprice*quantity*number(machine['correction'])*(mwinter if mwinter is not None else number(1))*mindex
                        target['machines'] = money(main+price*quantity*delta*(winter if winter is not None else number(1))*index)
        record['components']['machines']['value'] = current_values['machines']
        record['calculation'] = calculation
        record['drivers'] = current_values['drivers']
        record['overhead'] = 0 if resource else overhead(current_values['labor'],current_values['drivers'],record['overhead_rate'])
        record['profit'] = 0 if resource else overhead(current_values['labor'],current_values['drivers'],record['profit_rate'])
        values = [current_values[k] for k in ('labor','machines','materials')]
        record['direct'] = sum_known(values)
        record['total'] = sum_known([*values,record['overhead'],record['profit']])
        if resource:
            record['direct'] = record['total'] = current_values['materials']
        record['incomplete'] = (any(v is None for v in [*values,record['overhead'],record['profit']])
                                or not all(calculation[k]['complete'] for k in ('labor','machines','materials')))
        if record['incomplete']:
            warnings.append(f"{record['code']}: итог рассчитан по известным суммам; позиция неполная")
        if not resource and (record['overhead'] is None or record['profit'] is None):
            warnings.append(f"{record['code']}: нет данных для полного расчёта НР/СП выбранного периода")
        base = {'components':base_values}
        base['overhead'] = 0 if resource else overhead(base_values['labor'],base_values['drivers'],record['overhead_rate'])
        base['profit'] = 0 if resource else overhead(base_values['labor'],base_values['drivers'],record['profit_rate'])
        values = [base_values[k] for k in ('labor','machines','materials')]
        base['direct'] = sum_known(values)
        base['total'] = sum_known([*values,base['overhead'],base['profit']])
        if resource:
            base['direct'] = base['total'] = base_values['materials']
        record['base'] = base
        hours = number(detail.get('fixedTime'))
        record['labor_hours'] = {}
        for level,winter_field in (('base','base_winter'),('current','current_winter')):
            c = calculation['labor']
            winter = number(c[winter_field])
            record['labor_hours'][level] = (money(hours*quantity*number(c['correction'])*(winter if winter is not None else number(1)))
                                            if hours is not None and quantity is not None and number(c['correction']) is not None else None)
        record['labor_hours']['unit'] = float(hours) if hours is not None else None
    data['method'] = METHOD
    # Do not retain warnings about the abandoned reconstruction of current prices.
    previous = [w for w in data.get('warnings',[]) if not any(text in w for text in
                ('не подтверждено, какие множители учтены ранее', 'цена API расходится', '— неполные исходные данные для расчёта по базисной цене и индексу'))]
    data['warnings'] = list(dict.fromkeys([*previous,*warnings]))


def apply(book, data, layout):
    """Author live formulas and return independently calculated initial caches."""
    from .estimate_export import _comment
    round_costs(data)
    sheet = book[MAIN]
    cache = {MAIN:{}}
    editable = set()

    def formula(address,expression,value):
        sheet[address] = expression
        sheet[address].alignment = Alignment(horizontal='right',vertical='top',wrap_text=True)
        sheet[address].number_format = '#,##0.00'
        cache[MAIN][address] = value

    def input_cell(address,value,money_value=False):
        editable.add(address)
        sheet[address] = value
        sheet[address].font = Font(name='Arial',size=10,color=BLUE)
        sheet[address].alignment = Alignment(horizontal='right',vertical='top',wrap_text=True)
        sheet[address].number_format = '#,##0.00' if money_value else '0.00########'

    sheet['F16'] = 'Базисная цена на единицу измерения, руб.'
    sheet['H16'] = 'Коэффициент зимнего удорожания (базисный)'
    sheet['J16'] = 'Индексы пересчёта; нормативы НР и СП, %'
    sheet['L16'] = 'Коэффициент зимнего удорожания (текущий)'
    sheet['L16']._style = sheet['H16']._style
    sheet['L17'] = 12
    sheet.column_dimensions['F'].width = 23
    sheet.column_dimensions['L'].width = 20
    sheet.column_dimensions['L'].hidden = True
    sheet['A12'] = 'Расчёт по '+METHOD+'. Стоимость рассчитана в базисных ценах и ценах выбранного периода с учётом поправок и зимнего удорожания.'
    sheet['A13'] = 'Денежные суммы округляются до копеек после вычислений. Цены сохраняют исходную точность, отображаются с двумя знаками.'
    sheet['A14'] = 'Синие значения можно менять — связанные суммы пересчитаются. Если данных недостаточно, в итог включены известные суммы; недостающие затраты отмечены в пояснениях.'
    sheet.row_dimensions[12].height = 38
    sheet.row_dimensions[13].height = 34
    sheet.row_dimensions[14].height = 34

    positive = DataValidation(type='decimal',operator='greaterThan',formula1=0,allow_blank=True)
    positive.showErrorMessage = True
    positive.error = 'Коэффициент или индекс должен быть больше нуля.'
    sheet.add_data_validation(positive)
    for col,title in zip('OPQRSTU',('Исходный объём','Исходная текущая сумма','Исходная поправка','Исходный текущий зимний коэффициент','Текущая цена справочника для проверки','Исходная базисная цена','Исходный индекс')):
        sheet[f'{col}16'] = title
        sheet.column_dimensions[col].hidden = True
    records = [entry['record'] for entry in layout]
    positions = [entry['row'] for entry in layout]

    for entry in layout:
        p,record = entry['row'],entry['record']
        resource = record['code'].startswith('1.')
        children = entry['resources']
        quantity = f'$E${p}'
        parent = entry['parent']
        if parent is not None:
            parent_quantity = number(parent['record']['quantity_exact'])
            resource_quantity = number(record['quantity_exact'])
            if parent_quantity is not None and parent_quantity > 0 and resource_quantity is not None:
                ratio = float(resource_quantity / parent_quantity)
                input_cell(f'V{p}',ratio)
                formula(f'E{p}',f'=IF(ISNUMBER(E{parent["row"]}),E{parent["row"]}*V{p},"")',record['quantity_exact'])
                editable.add(f'E{p}')
        sheet[quantity].font = Font(name='Arial',size=10,color=BLUE)
        if sheet[quantity].data_type == 'f':
            sheet[quantity].number_format = '0.00########'
        source_rows = ', '.join(str(row) for row in record.get('source_rows', [record['source_row']]))
        _comment(sheet[quantity],f"Количество единиц расценки из строк {source_rows} сформированного перечня. "+
                 ('Объём не определён: укажите количество для расчёта.' if record['quantity_exact'] is None else 'Повторное деление на 100 и норму расхода не выполняется.')+
                 ' Объём редактируемый; оба уровня цен пересчитываются.')
        if parent is not None and sheet[f'E{p}'].data_type == 'f':
            _comment(sheet[f'E{p}'],'Количество ресурса связано с объёмом работы и пересчитывается вместе с ним. Расход на единицу работы определён по исходным количествам перечня; повторно норма расхода не применяется. Для отдельного изменения количества замените формулу числом.')
        refs = []
        complete_checks = [f'ISNUMBER({quantity})']
        for key,delta in ([('materials',0)] if resource else [('labor',1),('machines',2),('drivers',3),('materials',4)]):
            r = p+delta
            c = record['calculation'][key]
            for col,value in [('F',c['base_price']),('G',c['correction']),('H',c['base_winter']),('J',c['index']),('L',c['current_winter'])]:
                input_cell(f'{col}{r}',value,col == 'F')
                if col in ('G','H','J','L'):
                    positive.add(sheet[f'{col}{r}'])
            if key == 'drivers':
                if 'driver_correction' not in record:
                    formula(f'G{r}',f'=IF(G{p+2}="",1,G{p+2})',c['correction'])
                for col,owner in (('H',p+2),('L',p+2),('J',p+1)):
                    formula(f'{col}{r}',f'=IF({col}{owner}="","",{col}{owner})',c['base_winter' if col == 'H' else 'current_winter' if col == 'L' else 'index'])
            for col,value in zip('OPQRSTU',(record['quantity_exact'],c['fallback'],c['correction'],c['current_winter'],c['reference_price'],c['base_price'],c['index'])):
                sheet[f'{col}{r}'] = value
            correction = f'IF(ISNUMBER(G{r}),G{r},1)'
            winter_base = f'IF(ISNUMBER(H{r}),H{r},1)'
            winter_current = f'IF(ISNUMBER(L{r}),L{r},1)'
            valid_c = f'OR(G{r}="",AND(ISNUMBER(G{r}),G{r}>0))'
            valid_b = f'AND({valid_c},OR(H{r}="",AND(ISNUMBER(H{r}),H{r}>0)))'
            valid_t = f'AND({valid_c},OR(L{r}="",AND(ISNUMBER(L{r}),L{r}>0)))'
            base_product = f'F{r}*{quantity}*{correction}*{winter_base}'
            current_product = f'F{r}*{quantity}*{correction}*{winter_current}*J{r}'
            zero_current = '0'
            if key == 'machines':
                d = p+3
                delta = f'IF(ISNUMBER(G{d}),G{d},1)-{correction}'
                for level in ('base','current'):
                    w = f'H{d}' if level == 'base' else f'L{d}'
                    ix = '1' if level == 'base' else f'J{d}'
                    valid = f'AND(ISNUMBER(F{d}),ISNUMBER(G{d}),G{d}>0,OR({w}="",AND(ISNUMBER({w}),{w}>0))'
                    if level == 'current':valid += f',ISNUMBER(J{d}),J{d}>0'
                    valid += ')'
                    supplement = f'IF(({delta})=0,0,IF({valid},F{d}*{quantity}*({delta})*IF(ISNUMBER({w}),{w},1)*{ix},NA()))'
                    if level == 'base':
                        base_product += '+'+supplement
                        valid_b = f'AND({valid_b},OR(({delta})=0,{valid}))'
                    else:
                        current_product += '+'+supplement
                        valid_t = f'AND({valid_t},OR(({delta})=0,{valid}))'
                        zero_current = f'IF({quantity}=0,0,{_round_money(supplement)})'
            formula(f'I{r}',f'=IF(AND(ISNUMBER(F{r}),ISNUMBER({quantity}),{valid_b}),{_round_money(base_product)},"")',record['base']['components'][key])
            same_quantity = f'OR(AND(ISNUMBER({quantity}),ISNUMBER(O{r}),{quantity}=O{r}),AND(NOT(ISNUMBER({quantity})),NOT(ISNUMBER(O{r}))))'
            same_settings = f'AND({correction}=IF(ISNUMBER(Q{r}),Q{r},1),{winter_current}=IF(ISNUMBER(R{r}),R{r},1))'
            same_base = f'OR(AND(NOT(ISNUMBER(T{r})),F{r}=""),AND(ISNUMBER(T{r}),ISNUMBER(F{r}),T{r}=F{r}))'
            same_index = f'OR(AND(NOT(ISNUMBER(U{r})),J{r}=""),AND(ISNUMBER(U{r}),ISNUMBER(J{r}),U{r}=J{r}))'
            fallback = f'IF(AND(ISNUMBER(P{r}),{same_quantity},{same_settings},{same_base},{same_index}),P{r},"")'
            if key == 'machines':
                fallback = f'IF(({delta})=0,{fallback},"")'
            expression = (f'=IF({valid_t},IF(AND(ISNUMBER(F{r}),ISNUMBER({quantity})),'
                          f'IF(OR(F{r}=0,{quantity}=0),{zero_current},IF(AND(ISNUMBER(J{r}),J{r}>0),{_round_money(current_product)},{fallback})),{fallback}),"")')
            current = record['drivers'] if key == 'drivers' else record['components'][key]['value']
            formula(f'K{r}',expression,current)
            _comment(sheet[f'F{r}'],f"Базисная цена на единицу расценки из справочника ТСН за период {data['period']['title']}. "
                     'В расчёте используется полное значение цены, даже если на экране видны только два знака после запятой.')
            _comment(sheet[f'J{r}'],'Переводит базисную стоимость в цены выбранного периода. '+
                     ('Для зарплаты машинистов применяется тот же индекс, что и для зарплаты рабочих.' if key == 'drivers' else
                      'Используется только для расчёта текущей стоимости.'))
            _comment(sheet[f'H{r}'],'Зимнее удорожание для расчёта в базисных ценах. '+
                     ('Значение взято из расценки выбранного периода.' if c['base_winter'] is not None else
                      'В справочнике значение не указано. Дополнительная надбавка не начислена; необходимость её применения требует проверки.'))
            _comment(sheet[f'L{r}'],'Зимнее удорожание для расчёта в ценах выбранного периода. Может отличаться от коэффициента для базисных цен. '+
                     ('Значение взято из расценки выбранного периода.' if c['current_winter'] is not None else
                      'В справочнике значение не указано. Дополнительная надбавка не начислена; необходимость её применения требует проверки.'))
            _comment(sheet[f'I{r}'],'Базисная стоимость: количество × базисная цена × поправочный коэффициент × зимний коэффициент для базисных цен. Результат округляется до копеек.')
            _comment(sheet[f'K{r}'],'Стоимость в ценах выбранного периода: количество × базисная цена × поправочный коэффициент × текущий зимний коэффициент × индекс пересчёта. Результат округляется до копеек. '+
                     ('Недостаточно данных для расчёта. Исходная текущая сумма из перечня сохранена. После изменения объёма, цены, индекса или коэффициентов она не используется, пока не будет достаточно данных для пересчёта.'
                      if c['fallback'] is not None and any(value is None for value in
                          (c['base_price'], c['index'], record['quantity_exact'])) else
                      'Для полного расчёта не хватает данных. Уточните количество, цену, индекс и коэффициенты.' if not c['complete'] else ''))
            if key == 'drivers':
                _comment(sheet[f'G{r}'],'Обычно совпадает с поправкой к эксплуатации машин. Если для зарплаты машинистов предусмотрена отдельная поправка, укажите её здесь. Разница будет учтена в стоимости эксплуатации машин и в фонде оплаты труда по п. 3.4.15 ТСН-2001.12.')
            if key == 'machines':
                sheet[f'K{r}'].comment.text += ' Если поправка к зарплате машинистов отличается от поправки к эксплуатации машин, стоимость машин корректируется на разницу зарплаты. Эта разница пересчитывается по индексу зарплаты рабочих.'
            if key != 'drivers':
                refs.append(f'F{r}')
                complete_checks.append(f'AND(ISNUMBER(F{r}),OR(F{r}=0,AND(ISNUMBER(J{r}),J{r}>0,ISNUMBER(L{r}),L{r}>0)))')
        if resource:
            if parent is None:
                formula(f'AA{p}',f'=IF(AND({",".join(complete_checks)}),0,1)',int(record['incomplete']))
                for col,countcol,valuecol,initial in (('K','AB','AC',record['total']),('I','AD','AE',record['base']['total'])):
                    formula(f'{countcol}{p}',f'=COUNT({col}{p})',int(initial is not None))
                    formula(f'{valuecol}{p}',f'=IF(ISNUMBER({col}{p}),{col}{p},"")',initial)
            continue
        footer = entry['footer']
        known = [number(record['calculation'][k]['base_price']) for k in ('labor','machines','materials') if record['calculation'][k]['base_price'] is not None]
        formula(f'F{p}',_sum_formula(refs,False),float(sum(known)) if known else None)
        _comment(sheet[f'F{p}'],'Базисная цена единицы работы: зарплата рабочих + эксплуатация машин + материалы, до применения поправок. Зарплата машинистов уже входит в стоимость машин.')
        for delta,field in ((0,'overhead'),(1,'profit')):
            r = footer+delta
            input_cell(f'J{r}',record[field+'_rate'])
            formula(f'E{r}',f'=IF(ISNUMBER(J{r}),J{r},"")',record[field+'_rate'])
            for col,initial in (('I',record['base'][field]),('K',record[field])):
                expression = f'=IF(AND(ISNUMBER({col}{p+1}),ISNUMBER({col}{p+3}),ISNUMBER(J{r})),{_round_money(f"SUM({col}{p+1},{col}{p+3})*J{r}/100")},"")'
                formula(f'{col}{r}','=0' if record['code'].startswith('1.') else expression,initial)
                charge = 'Накладные расходы' if field == 'overhead' else 'Сметная прибыль'
                _comment(sheet[f'{col}{r}'],charge+' = (зарплата рабочих + зарплата машинистов) × норматив / 100. '+
                         f"Норматив взят из расценки за период {data['period']['title']}. Основание: п. 3.4.22 ТСН-2001.12. "+
                         ('Для отдельной строки материала это начисление не применяется.' if record['code'].startswith('1.') else
                          'Недостаточно данных: проверьте зарплату рабочих, зарплату машинистов и норматив. Пустое значение не означает нулевые затраты.' if record[field] is None else ''))
        hours_row = footer+2
        sheet[f'C{hours_row}'] = 'ЗТР'
        sheet[f'D{hours_row}'] = 'чел.-ч'
        input_cell(f'E{hours_row}',record['labor_hours']['unit'])
        _comment(sheet[f'E{hours_row}'],'Количество часов труда рабочих на единицу расценки по справочнику ТСН. Итоговые трудозатраты зависят от объёма, поправки к труду рабочих и зимнего коэффициента. В денежную сумму не входят.')
        for col,w,initial in (('I',f'H{p+1}',record['labor_hours']['base']),('K',f'L{p+1}',record['labor_hours']['current'])):
            product = f'E{hours_row}*{quantity}*IF(ISNUMBER(G{p+1}),G{p+1},1)*IF(ISNUMBER({w}),{w},1)'
            valid = f'AND(ISNUMBER(E{hours_row}),ISNUMBER({quantity}),OR(G{p+1}="",AND(ISNUMBER(G{p+1}),G{p+1}>0)),OR({w}="",AND(ISNUMBER({w}),{w}>0)))'
            formula(f'{col}{hours_row}',f'=IF({valid},{_round_money(product)},"")',initial)
        direct,total = footer+3,footer+4
        for col,values in (('I',record['base']),('K',record)):
            resource_amounts = [child['record']['base']['total'] if col == 'I' else child['record']['total'] for child in children]
            formula(f'{col}{direct}',_sum_formula([f'{col}{p+d}' for d in (1,2,4)]+[f'{col}{child["row"]}' for child in children]),sum_known([values['direct'],*resource_amounts]))
            formula(f'{col}{total}',_sum_formula([f'{col}{direct}',f'{col}{footer}',f'{col}{footer+1}']),sum_known([values['total'],*resource_amounts]))
            _comment(sheet[f'{col}{total}'],'Сумма зарплаты рабочих, эксплуатации машин, материалов, накладных расходов и сметной прибыли. Зарплата машинистов уже входит в стоимость машин, а трудозатраты выражены в часах. Недостающие затраты в итог не включены.')
        for child in children:
            q=child['row']
            complete_checks.append(f'AND(ISNUMBER(E{q}),ISNUMBER(F{q}),ISNUMBER(K{q}),OR(F{q}=0,AND(ISNUMBER(J{q}),J{q}>0,ISNUMBER(L{q}),L{q}>0)))')
        cost_refs = ','.join([f'K{p+d}' for d in (1,2,4)]+[f'K{footer}',f'K{footer+1}'])
        incomplete = any(item['record']['incomplete'] for item in [entry,*children])
        formula(f'AA{total}',f'=IF(AND(COUNT({cost_refs})=5,{",".join(complete_checks)}),0,1)',int(incomplete))
        for col,countcol,valuecol,initial in (('K','AB','AC',sum_known([record['total'],*[child['record']['total'] for child in children]])),('I','AD','AE',sum_known([record['base']['total'],*[child['record']['base']['total'] for child in children]]))):
            formula(f'{countcol}{total}',f'=COUNT({col}{total})',int(initial is not None))
            formula(f'{valuecol}{total}',f'=IF(ISNUMBER({col}{total}),{col}{total},"")',initial)
        formula(f'C{total}',f'=IF(AA{total}=0,"ВСЕГО по позиции:","ВСЕГО по позиции (по известным суммам):")',
                'ВСЕГО по позиции (по известным суммам):' if incomplete else 'ВСЕГО по позиции:')

    start = 19
    sections = [r for r in range(19,sheet.max_row+1) if str(sheet.cell(r,1).value or '').startswith('Итого по разделу')]
    for r in sections:
        label = sheet[f'A{r}'].value
        formula(f'A{r}',f'=IF(SUM(AA{start}:AA{r-1})>0,"Итого по разделу (по известным суммам): есть неполные позиции","Итого по разделу:")',label)
        subset = [record for p,record in zip(positions,records) if start <= p < r]
        for col,countcol,valuecol,value in (('K','AB','AC',sum_known(x['total'] for x in subset)),('I','AD','AE',sum_known(x['base']['total'] for x in subset))):
            formula(f'{col}{r}',f'=IF(SUM({countcol}{start}:{countcol}{r-1})=0,"",ROUND(SUM({valuecol}{start}:{valuecol}{r-1}),2))',value)
        start = r+1
    grand = next(r for r in range(19,sheet.max_row+1) if str(sheet.cell(r,1).value or '').startswith('ИТОГО ПО СМЕТЕ'))
    label = sheet[f'A{grand}'].value
    formula(f'A{grand}',f'=IF(SUM(AA19:AA{grand-1})>0,"ИТОГО ПО СМЕТЕ (по известным суммам): есть неполные позиции","ИТОГО ПО СМЕТЕ:")',label)
    for col,countcol,valuecol,value in (('K','AB','AC',sum_known(x['total'] for x in records)),('I','AD','AE',sum_known(x['base']['total'] for x in records))):
        formula(f'{col}{grand}',f'=IF(SUM({countcol}19:{countcol}{grand-1})=0,"",ROUND(SUM({valuecol}19:{valuecol}{grand-1}),2))',value)
    current_total = sum_known(x['total'] for x in records)
    base_total = sum_known(x['base']['total'] for x in records)
    sheet['A7'] = 'Сметная стоимость в текущем уровне цен, тыс. руб. (по известным суммам)'
    formula('K7',f'=IF(ISNUMBER(K{grand}),{_round_money(f"K{grand}/1000")},"")',money(number(current_total)/1000) if current_total is not None else None)
    sheet.merge_cells('A9:H9')
    sheet['A9'] = 'Сметная стоимость в базисном уровне цен, тыс. руб. (по известным суммам)'
    formula('I9',f'=IF(ISNUMBER(I{grand}),{_round_money(f"I{grand}/1000")},"")',money(number(base_total)/1000) if base_total is not None else None)
    for col in ('V','AA','AB','AC','AD','AE'):
        sheet.column_dimensions[col].hidden = True
    sheet['V16'] = 'Расход ресурса на единицу работы по исходным количествам'
    sheet.print_area = f'A1:K{sheet.max_row}'
    for row in sheet:
        for cell in row:
            if cell.column in (6,9,11):
                cell.number_format = '#,##0.00'
            if cell.coordinate in editable and cell.column in (7,8,10,12):
                cell.number_format = '0.00########'
            if cell.data_type == 'f':
                cell.font = Font(name='Arial',size=10,bold=cell.font.b,color=BLUE if cell.coordinate in editable else GREEN)
    for cell in sheet[17][:12]:
        cell.number_format = '0'
    book.calculation = CalcProperties(calcMode='auto',fullCalcOnLoad=True,forceFullCalc=True)
    return cache


def cache_results(path, cache):
    """Store initial results for previews; recalculate on open in Excel/Calc.

    openpyxl writes formulas without cached values. The cache is the independently
    calculated export snapshot, not a second calculation engine or an override.
    """
    namespace = {'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    tag = '{'+namespace['s']+'}'
    target = Path(path)
    temporary = target.with_name(target.name+'.cache')
    try:
        with ZipFile(target) as source, ZipFile(temporary,'w') as destination:
            for info in source.infolist():
                payload = source.read(info.filename)
                match = next((values for index,values in enumerate(cache.values(),1)
                              if info.filename == f'xl/worksheets/sheet{index}.xml'),None)
                if match:
                    root = ET.fromstring(payload)
                    for cell in root.findall('.//s:sheetData/s:row/s:c',namespace):
                        address = cell.get('r')
                        if address not in match or cell.find(tag+'f') is None:
                            continue
                        value = match[address]
                        node = cell.find(tag+'v')
                        if node is None:
                            node = ET.SubElement(cell,tag+'v')
                        if isinstance(value,str) or value is None:
                            cell.set('t','str')
                            node.text = value or ''
                        else:
                            cell.attrib.pop('t',None)
                            node.text = str(value)
                    payload = ET.tostring(root,encoding='utf-8',xml_declaration=True)
                destination.writestr(info,payload)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
