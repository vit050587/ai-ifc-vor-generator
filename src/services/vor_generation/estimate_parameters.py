"""Actual estimate-selection parameters, using examples from every template sheet."""
from functools import lru_cache
from pathlib import Path
import re

from openpyxl import load_workbook

from . import mapping


@lru_cache(maxsize=4)
def _requirements(path, modified, size):
    book = load_workbook(path, read_only=True, data_only=True)
    result = []
    try:
        for sheet in book:
            headings = [[str(value or '').replace('\n', ' ').lower() for value in row]
                        for row in sheet.iter_rows(min_row=2, max_row=3, values_only=True)]
            column = next((i for row in headings for i, value in enumerate(row)
                           if 'параметр' in value and ('тсн' in value or 'сметн' in value)), None)
            if column is None:
                continue
            element_kind = part = None
            for values in sheet.iter_rows(min_row=4, values_only=True):
                from .template import _numbering_row
                if _numbering_row(values):
                    continue
                name = str(values[1] or '') if len(values) > 1 else ''
                heading = str(values[0] or '')
                if heading and not name:
                    part = mapping.detect_part(heading) or part
                    if heading.startswith(('Раздел ', 'Подраздел ')):
                        element_kind = mapping.detect_kind(heading)
                    else:
                        element_kind = mapping.detect_kind(heading) or element_kind
                requirement = str(values[column] or '').strip() if column < len(values) else ''
                if name and requirement:
                    resource = bool(re.fullmatch(r'\d+[.,]\d+', heading))
                    category = mapping.classify_resource(name) if resource else mapping.classify_work(name)
                    result.append({'name': name, 'parameter': requirement, 'category': category,
                                   'resource': resource, 'kind': element_kind, 'part': part, 'sheet': sheet.title})
        return tuple(result)
    finally:
        book.close()


def requirements(path):
    path = Path(path)
    stamp = path.stat()
    return _requirements(str(path), stamp.st_mtime_ns, stamp.st_size)


def required_parameter(name, original, examples):
    resource = original.kind == 'resource'
    category = mapping.classify_resource(name) if resource else mapping.classify_work(name)
    candidates = [example for example in examples if example['resource'] == resource and example['category'] == category]
    if not candidates:
        return ''
    words = set(re.findall(r'[а-яa-z0-9]+', name.lower().replace('ё', 'е')))

    def score(example):
        other = set(re.findall(r'[а-яa-z0-9]+', example['name'].lower().replace('ё', 'е')))
        exact = name.casefold() == example['name'].casefold()
        kind = bool(original.element_kind and example['kind'] == original.element_kind)
        part = bool(original.part and example['part'] == original.part)
        return (exact, kind, part, len(words & other) / max(1, len(words | other)))

    selected = max(candidates, key=score)
    return selected['parameter'] if score(selected)[-1] > 0 or category != 'other' else ''


def actual_parameters(name, objects):
    category = mapping.classify_work(name)
    resource = mapping.classify_resource(name)
    values = []
    diameter, rebar_class = mapping.parse_rebar_spec(name)
    if diameter or rebar_class:
        if rebar_class:
            values.append('Класс арматуры: ' + str(rebar_class))
        if diameter:
            values.append(f'Диаметр: {diameter:g} мм')
    if category == 'rebar' or resource == 'rebar':
        classes = sorted({str(getattr(obj, 'rebar_class', '') or '') for obj in objects} - {''})
        if classes and not rebar_class:
            values.append('Класс арматуры: ' + ', '.join(classes))
        return '; '.join(values)
    for obj in objects:
        geometry = getattr(obj, 'geometry', {})
        thickness = geometry.get('thickness') or getattr(obj, 'thickness', None)
        section = geometry.get('section_label') or getattr(obj, 'section_label', None)
        if thickness:
            values.append(f'Толщина: {thickness:g} мм')
        elif getattr(obj, 'thickness_label', ''):
            values.append('Толщина: '+obj.thickness_label+' мм')
        if section:
            values.append('Сечение: ' + str(section))
        method = getattr(obj, 'construction_method', '')
        if method in ('precast', 'monolithic'):
            values.append('Технология: ' + ('сборная' if method == 'precast' else 'монолитная'))
        if category == 'concrete' or resource == 'concrete':
            spec = getattr(obj, 'concrete_spec', '') or getattr(obj, 'concrete_grade', '') or getattr(obj, 'grade', '')
            if spec:
                values.append('Бетон: ' + str(spec))
    return '; '.join(dict.fromkeys(values))


def fill(output, source_rows, objects, template_path=None):
    # Only actual model/document values are printed. Template examples specify
    # which parameters are relevant; their sample values never become defaults.
    from .template import DEFAULT_TEMPLATE_PATH
    examples = requirements(template_path or DEFAULT_TEMPLATE_PATH)
    by_index = {row.index: row for row in source_rows}
    for row in output:
        original = by_index.get(row.src_index)
        if original is None or original.kind not in ('work', 'resource', 'variant'):
            continue
        name = row.name_override or original.name
        parameter = required_parameter(name, original, examples)
        if parameter:
            row.extra[7] = parameter
        selected = objects
        if row.volume is not None:
            guids = set(getattr(row.volume, 'element_guids', ()))
            sources = set(getattr(row.volume, 'sources', ()))
            if guids and any(getattr(obj, 'guid', '') for obj in objects):
                selected = [obj for obj in objects if getattr(obj, 'guid', '') in guids]
            elif sources and any(getattr(getattr(obj, 'volume', None), 'sources', ()) for obj in objects):
                selected = [obj for obj in objects if sources.intersection(getattr(getattr(obj, 'volume', None), 'sources', ()))]
        value = actual_parameters(name, selected) if row.volume is not None else ''
        if value:
            row.extra[8] = value
