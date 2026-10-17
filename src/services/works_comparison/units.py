"""Convert rate units and quantities before grouping; retain original evidence."""
import copy
import re
from decimal import Decimal, InvalidOperation

ALIASES = {'сборных конструкций':'шт.','сборные конструкции':'шт.','м2':'м²','кв м':'м²','квм':'м²','м3':'м³','куб м':'м³','кубм':'м³','м':'м','пог м':'м','погм':'м','мп':'м','пм':'м','мпог':'м','шт':'шт.','штук':'шт.','т':'т','тонна':'т','тонн':'т','кг':'кг','компл':'компл.','комплект':'компл.','маш-ч':'маш.-ч','чел-ч':'чел.-ч','га':'га','прибор':'шт.','прибора':'шт.','приборов':'шт.','изделий':'шт.','изделие':'шт.','блоков':'шт.','блок':'шт.','пара':'пар','пар':'пар'}


def unit_and_factor(value):
    text = str(value or '').strip().lower().replace('²','2').replace('³','3').replace('\u00a0',' ')
    text = re.sub(r'(?<!\d)\.|\.(?!\d)', '', text)
    text = re.sub(r'\s+', ' ', text)
    if text.startswith('тыс '):
        text = '1000 ' + text[4:]
    match = re.fullmatch(r'(?:(\d+(?:[.,]\d+)?)\s*(тыс\s*)?)?(.+)', text)
    if not match:
        return text, None
    number, thousand, unit = match.groups()
    unit = unit.strip()
    if re.fullmatch(r'м[23] (?:деталей|оклеиваемой поверхности|покрытия|поверхности)', unit):
        unit = unit[:2]
    if unit not in ALIASES:
        return text, None
    factor = Decimal(number.replace(',', '.')) if number else Decimal(1)
    if thousand:
        factor *= 1000
    return ALIASES[unit.strip()], factor if factor > 0 else None


def normalize_positions(positions):
    result = []
    for original in positions:
        p = copy.deepcopy(original)
        if 'originalUnit' not in p:
            p['originalUnit'], p['originalQuantity'] = p.get('unit'), p.get('quantity')
        unit, factor = unit_and_factor(p['originalUnit'])
        p['unit'] = unit
        p['unitFactor'] = float(factor) if factor is not None else None
        p['unitStatus'] = 'converted' if factor is not None else 'unresolved'
        raw = p['originalQuantity']
        try:
            number = Decimal(str(raw).replace('\u00a0','').replace(' ','').replace(',','.'))
            p['quantity'] = float(number * factor) if factor is not None and number.is_finite() else None
        except (InvalidOperation, TypeError, ValueError):
            p['quantity'] = None
        result.append(p)
    return result
