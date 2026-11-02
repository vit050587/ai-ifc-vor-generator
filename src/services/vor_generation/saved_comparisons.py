"""Persist display metadata independently of calculation caches and AI availability."""
import json
import os
import uuid
from pathlib import Path

FILENAME = 'Типовая_ВОР_результат.json'


def save(folder, result):
    target = Path(folder) / FILENAME
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def load(folder):
    from . import typical_fill, formation_cache
    folder = Path(folder)
    for path in (folder / FILENAME, folder / formation_cache.CACHE_FILENAME):
        try:
            result = json.loads(path.read_text(encoding='utf-8'))
            if path.name == formation_cache.CACHE_FILENAME:
                result = result['result']
            if (folder / Path(result['file']).name).is_file():
                result['file'] = str(folder / Path(result['file']).name)
                result['source_files'] = [dict(item, file=str(folder / Path(item['file']).name))
                    for item in result.get('source_files', [])
                    if (folder / Path(item['file']).name).is_file()]
                return result
        except (OSError, ValueError, KeyError, TypeError):
            pass
    # Older sessions may contain workbooks without any saved display metadata.
    bundle = folder / typical_fill.OUTPUT_FILENAME
    if not bundle.is_file():
        return None
    from openpyxl import load_workbook
    book = load_workbook(bundle, read_only=True, data_only=True)
    try:
        sheet = book['Сравнение'] if 'Сравнение' in book.sheetnames else book['Типовой ВОР — все строки']
        volumes = [(row[3], row[4]) for row in sheet.iter_rows(values_only=True)
                   if len(row) >= 5 and (isinstance(row[3], (int, float)) or isinstance(row[4], (int, float)))]
        numeric = lambda value: isinstance(value, (int, float))
        return {'file': str(bundle), 'template_rows': None,
            'filled_ours': sum(numeric(a) for a, b in volumes),
            'filled_vor': sum(numeric(b) for a, b in volumes),
            'filled_both': sum(numeric(a) and numeric(b) for a, b in volumes),
            'verdicts': {}, 'source_files': [dict(kind=kind, file=str(folder / name))
                for kind, name in (('generated', typical_fill.GENERATED_FILENAME), ('customer', typical_fill.CUSTOMER_FILENAME))
                if (folder / name).is_file()]}
    finally:
        book.close()
