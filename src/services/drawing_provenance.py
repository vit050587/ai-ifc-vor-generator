"""Drawing-only labels for calculated work quantities; IFC sources are untouched."""
import json
import math
import re
from pathlib import Path

FILENAME = 'drawing_quantity_context.json'


def save_context(folder, filename, floor_height):
    Path(folder).mkdir(parents=True, exist_ok=True)
    payload = {'source_type': 'pdf', 'filename': Path(filename).name,
               'floor_height_m': floor_height, 'height_basis': 'принята по умолчанию'}
    (Path(folder) / FILENAME).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def load_context(folder):
    folder = Path(folder)
    for directory in (folder, folder.parent):
        path = directory / FILENAME
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return {}
            if not isinstance(data, dict):
                return {}
            return data if data.get('source_type') == 'pdf' else {}
    # Historical drawings did not save the calculation height. Never infer it
    # from a later UI setting or from the current default in the calculator.
    for directory in (folder, folder / 'original', folder.parent / 'original', folder.parent):
        if list(directory.glob('original_*.ifc')):
            return {}
        files = list(directory.glob('original_*.pdf'))
        if files:
            return {'source_type': 'pdf'}
    return {}


def quantity_source(context, quantity, title, source='', facts=None, code=''):
    if not context or quantity is None:
        return source
    try:
        if not math.isfinite(float(quantity)):
            return source
    except (TypeError, ValueError):
        return source
    source = source if isinstance(source, str) else ''
    # Quantities imported from the customer's documents are not drawing-derived.
    if re.search(r'ВОР заказчика|DOC\.volume_for|исходный тоннаж', source, re.I):
        return source
    source = source.replace('элементов IFC', 'элементов чертежа')
    label = 'Рассчитано из чертежа'
    if label not in source:
        source = '; '.join(filter(None, (label, source)))
    concrete = re.search(r'бетонирован|укладк\w*\s+бетон|устройств\w*.*(?:бетонн|железобетонн)', title, re.I)
    if concrete and not str(code).startswith(('1.', '2.')) and 'Высота этажа' not in source:
        facts = facts or {}
        name = str(facts.get('name') or facts.get('Имя') or '')
        cls = facts.get('ifc_class') or facts.get('Тип элемента')
        if cls in ('IfcSlab', 'IfcFooting') or re.search(r'плит|перекрыт|фундамент', name, re.I):
            detail = 'Высота этажа не используется: объём по площади и толщине'
        else:
            height = context.get('floor_height_m')
            if isinstance(height, (int, float)) and math.isfinite(height) and height > 0:
                value = f'{height:g}'.replace('.', ',')
                detail = f'Высота этажа в расчёте: {value} м ({context.get("height_basis") or "принята в расчёте"})'
            else:
                detail = 'Высота этажа в исходном расчёте не сохранена'
        source += '; ' + detail
    return source
