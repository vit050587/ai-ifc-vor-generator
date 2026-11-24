"""Traceable, element-specific document facts; never distribute VOR totals by guess."""
import copy
import json
import re
from pathlib import Path

from .table_matching import location


FILES = {'ВОР_ведомость_объемов_работ.json': 'ВОР', 'ПД_характеристики_элементов.json': 'ПД',
         'ПОС_глобальные_константы.json': 'ПОС', 'ПЗ_глобальные_константы.json': 'ПЗ'}


def load(folder):
    records, conditions = [], []
    for filename, source in FILES.items():
        path = Path(folder) / filename
        if not path.is_file():
            continue
        data = json.loads(path.read_text())
        document = data.get('document') or {}
        origin = document.get('file_name') or document.get('name') or filename
        for row in data.get('items') or []:
            name = str(row.get('name') or row.get('element') or '')
            quote = str(row.get('quote') or '')
            if name or quote:
                records.append(dict(source=source, file=origin, name=name, quote=quote,
                                    page=row.get('page'), unit=row.get('unit'), volume=row.get('volume')))
        for key, item in (data.get('constants') or {}).items():
            if not isinstance(item, dict) or not item.get('found') or item.get('source') == 'шаблон':
                continue
            conditions.append(dict(source=source, file=origin, key=key, name=item.get('title') or key,
                                   value=item.get('value'), page=item.get('page'), quote=item.get('quote') or ''))
    return records, conditions


def _tokens(text):
    # Distinctive content only. Standard operation verbs or building-wide terms
    # cannot attach a document's work or material to a different construction.
    stop = ('устрой', 'монтаж', 'строит', 'работ', 'матери', 'покрыт', 'констр', 'базов', 'этаж',
            'надзем', 'подзем', 'проект', 'основ', 'слой', 'толщи', 'наруж', 'внутр')
    return {w[:6] for w in re.findall(r'[а-яёa-z]{4,}', text.lower()) if not w.startswith(stop)}


def _marks(text):
    return set(re.findall(r'(?<![0-9а-яёa-z])[а-яёa-z]{1,5}[-_.]\d+(?:[.-]\d+)*(?![0-9а-яёa-z])', text.lower()))


def enrich(element, records, conditions=()):
    result = copy.deepcopy(element)
    kind = location(element)
    identity = str(element.get('name') or '')
    content = identity + ' ' + str(element.get('material') or '')
    words, marks = _tokens(content), _marks(identity)
    matched = []
    for record in records:
        text = record['name'] + ' ' + record['quote']
        record_kind = location({'name': text})
        if record_kind != 'other' and kind != 'other' and kind != record_kind:
            continue
        exact = bool(marks & _marks(text))
        if marks and _marks(text) and not exact:
            continue
        overlap = words & _tokens(text)
        if exact or (len(overlap) >= 2 and record_kind == kind and kind != 'other'):
            match = dict(record, match_basis='Марка конструкции' if exact else 'Назначение и признаки конструкции',
                         match_strength='exact_type' if exact else 'context', matched_terms=sorted(overlap))
            matched.append(match)
    result['document_evidence'] = matched
    result['project_conditions'] = list(conditions)
    # Document composition may fill a genuinely absent material only when a
    # unique construction mark confirms the scope; broad similarity is context.
    exact = [r for r in matched if r['match_strength'] == 'exact_type']
    if str(element.get('material') or '').strip() in ('', '-', 'не указано') and len(exact) == 1:
        result['material'] = exact[0]['name'] + ': ' + exact[0]['quote']
        result['material_source'] = {k: exact[0][k] for k in ('source', 'file', 'page', 'quote', 'match_basis')}
    # All document volumes remain source totals. No division by IFC count, no
    # assignment to each member of a group without an explicit quantity link.
    return result
