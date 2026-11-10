"""Compare declared estimate-selection parameters independently of quantities."""
import re
from decimal import Decimal


LABELS = {'concrete': 'Класс бетона', 'water': 'Водонепроницаемость W',
          'frost': 'Морозостойкость F', 'rebar': 'Класс арматуры',
          'diameter': 'Диаметр', 'thickness': 'Толщина', 'section': 'Сечение',
          'method': 'Технология'}


def normalized(text):
    text = str(text or '').lower().replace('ё', 'е').replace(',', '.')
    return re.sub(r'\s+', ' ', text.translate(str.maketrans({'а':'a','в':'b','с':'c','х':'x'}))).strip()


def values(text, name=''):
    original = str(text or '') + '; ' + str(name or '')
    result = {}
    patterns = {
        'concrete': r'(?:бетон\w*[^;]*?|(?<!\w))[вb]\s*(\d+(?:[.,]\d+)?)',
        'water': r'(?<![\w])[wш]\s*(\d+)', 'frost': r'(?<![\w])[fф]\s*(\d+)',
        'rebar': r'(?<!\w)[аa]\s*([1-6]\d{2}[сc]?)',
        'diameter': r'(?:диаметр\s*[:=]?|\bd\s*=?)\s*(\d+(?:[.,]\d+)?)\s*(?:мм)?',
        'thickness': r'толщин\w*\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*мм',
        'section': r'сечени\w*\s*[:=]?\s*([^;]+)',
        'method': r'технология\s*:\s*([^;]+)',
    }
    for key, pattern in patterns.items():
        matches = {normalized(match) for match in re.findall(pattern, original, re.I)}
        if key in ('concrete', 'water', 'frost', 'diameter', 'thickness'):
            matches = {format(Decimal(match).normalize(), 'f') for match in matches}
        matches = sorted(matches)
        if matches:
            result[key] = ', '.join(matches)
    return result


def required(text):
    text = str(text or '').lower().replace('ё', 'е')
    result = set()
    if re.search(r'(?<!\w)[вb](?!\w)', text): result.add('concrete')
    if re.search(r'(?<!\w)w(?!\w)', text): result.add('water')
    if re.search(r'(?<!\w)f(?!\w)', text): result.add('frost')
    if 'класс' in text: result.add('concrete' if 'бетон' in text else 'rebar')
    if 'диаметр' in text: result.add('diameter')
    if 'толщин' in text: result.add('thickness')
    if 'сечени' in text: result.add('section')
    return result


def compare(generated, customer):
    if generated is None or customer is None:
        return {'status': 'not_applicable', 'differences': [], 'missingGenerated': [], 'missingCustomer': []}
    ours = values(generated.get('estimateParameterValues'), generated.get('work'))
    theirs = values(customer.get('estimateParameterValues'), customer.get('work'))
    keys = required(generated.get('estimateParameters')) | required(customer.get('estimateParameters'))
    # Even older workbooks can contain comparable values in names. No sample
    # value or missing value is used as an actual project parameter.
    keys |= ours.keys() & theirs.keys()
    differences = [{'parameter': key, 'label': LABELS[key], 'generated': ours[key], 'customer': theirs[key]}
                   for key in sorted(keys) if key in ours and key in theirs and ours[key] != theirs[key]]
    missing_g = [LABELS[key] for key in sorted(keys) if key not in ours]
    missing_c = [LABELS[key] for key in sorted(keys) if key not in theirs]
    status = 'mismatch' if differences else 'missing_data' if missing_g or missing_c else 'matched' if keys else 'not_applicable'
    return {'status': status, 'differences': differences, 'missingGenerated': missing_g, 'missingCustomer': missing_c}


def explanation(result):
    if result['status'] == 'mismatch':
        return 'Различаются сметные параметры: ' + '; '.join(
            f"{item['label']}: модель — {item['generated']}, заказчик — {item['customer']}" for item in result['differences'])
    if result['status'] == 'missing_data':
        return '; '.join(label + ': ' + ', '.join(result[key]) for key, label in (
            ('missingGenerated', 'Нет значений в сформированной ВОР'), ('missingCustomer', 'Нет значений в ВОР заказчика')) if result[key])
    return 'Сметные параметры совпадают' if result['status'] == 'matched' else ''


def add_sheet(book, rows):
    records = [row for row in rows if not row.get('isSeparator') and (row.get('parameterComparison') or {}).get('status') not in (None, 'not_applicable')]
    if not records:
        return
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter
    sheet = book.create_sheet('Сметные параметры')
    sheet.append(['Часть здания', 'Раздел', 'Подраздел', 'Конструкция', 'Работа', 'Параметр от ВОР к ТСН',
                  'Значения модели', 'Значения заказчика', 'Результат сравнения', 'Строка модели', 'Строка заказчика'])
    for row in records:
        sheet.append([row.get(key) for key in ('part','section','subsection','elementGroup','work','generatedEstimateParameters',
                      'generatedEstimateParameterValues','customerEstimateParameterValues')] + [explanation(row['parameterComparison']), row.get('generatedSourceRow'), row.get('customerSourceRow')])
    for cell in sheet[1]: cell.font = Font(bold=True)
    for i in range(1,12): sheet.column_dimensions[get_column_letter(i)].width = 45 if i in (5,7,8,9) else 25
    for row in sheet:
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical='top')
            if isinstance(cell.value, str): cell.data_type = 's'
        sheet.row_dimensions[row[0].row].height = 65
    sheet.auto_filter.ref = sheet.dimensions
