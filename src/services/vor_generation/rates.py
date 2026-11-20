"""Automatic TSN references from the approved ВОР КР_ЖС sheet.

Only the generated work list is consulted. Project example columns and the
customer estimate are deliberately excluded from the candidate catalogue.
"""
from copy import copy
from functools import lru_cache
from pathlib import Path
import re

from openpyxl import load_workbook
from openpyxl.styles import Alignment

from . import mapping
from ..estimate_parameter_comparison import values as parameter_values
from ..works_comparison.units import unit_and_factor

CATALOG_PATH = Path(__file__).resolve().parents[3] / 'data' / 'Типовой ВОР по КР с расценками.xlsx'
SHEET = 'ВОР КР_ЖС'
HEADERS = ('Шифр ТСН', 'Наименование расценки / ресурса ТСН', 'Ед. изм. расценки',
           'Объём в единицах расценки', 'Источник расценки')


def norm(value):
    return re.sub(r'\s+', ' ', str(value or '').lower().replace('ё', 'е')).strip()


def words(value):
    return set(re.findall(r'[а-яa-z]+', norm(value))) - {'по', 'и', 'из', 'в', 'для', 'мм', 'м', 'т', 'с', 'на'}


def category(name, resource=False):
    """Operation identity, not merely its shared quantity family (count/tonnes)."""
    text = norm(name)
    if resource:
        return mapping.classify_resource(name)
    for fragment, value in (('вырубк', 'pile_cutting'), ('погруж', 'pile_driving'),
                            ('буронабив', 'bored_piles'), ('бурени', 'drilling'),
                            ('испытан', 'testing'), ('подготов', 'preparation'),
                            ('уход за бетон', 'curing')):
        if fragment in text:
            return value
    value = mapping.classify_work(name)
    if value == 'rebar':
        return 'rebar_frames' if 'каркас' in text else 'rebar_releases' if 'выпуск' in text else 'rebar_bars'
    return value


@lru_cache(maxsize=4)
def _catalog(path, modified, size):
    book = load_workbook(path, read_only=True, data_only=True)
    result = []
    part = section = subsection = parent = owner = ''
    kind = None
    try:
        for index, row in enumerate(book[SHEET].values, 1):
            if index < 5:
                continue
            row = tuple(row) + (None,) * 18
            number, name, unit = (str(v or '').strip() for v in row[:3])
            if number and not name and not unit:
                if number.startswith('Подраздел '):
                    subsection, owner, parent = number, '', ''
                    kind = mapping.detect_kind(number) or mapping.detect_kind(section)
                elif number.startswith('Раздел '):
                    section, subsection, owner, parent = number, '', '', ''
                    kind = mapping.detect_kind(number)
                else:
                    part = mapping.detect_part(number) or part
                continue
            if name:
                owner = name
                if re.fullmatch(r'\d+', number):
                    parent = name
            code, title, rate_unit = (str(v or '').strip() for v in row[5:8])
            if not re.fullmatch(r'\d+\.\d+-\d+-\d+(?:[.,]\d+)?', code) or not title:
                continue
            resource = code.startswith('1.')
            # Continuation lines of montage/dismantling belong to the same work.
            # A distinct operation (e.g. curing) uses its own title as identity.
            label = owner or title
            if resource and not name:
                # Material alternatives occupy continuation rows in ЖС. Their
                # identity comes from the resource description, not from the
                # neighbouring example material or operation's concrete grade.
                spec = mapping.parse_concrete_spec(title)
                if spec and ('бетон' in norm(title) or 'смес' in norm(title)):
                    label = mapping.concrete_label(spec)
                else:
                    label = title
                    diameter = re.search(r'диаметр\s*(?:от\s*)?(\d+)', title, re.I)
                    if diameter and mapping.is_rebar_text(title):
                        label += f'; d={diameter[1]} мм'
            if not name and not resource and category(title) != category(parent):
                label = title
            operation = category(label, resource)
            result.append(dict(code=code, title=title, unit=rate_unit, work=label, resource=resource,
                               category=operation, part=part, kind=kind, section=section, subsection=subsection,
                               parent=parent, row=index))
    finally:
        book.close()
    return tuple(result)


def catalog(path=CATALOG_PATH):
    path = Path(path)
    stamp = path.stat()
    return _catalog(str(path), stamp.st_mtime_ns, stamp.st_size)


def selected_records(folder):
    from .. import vor_comparison as vc
    from ..estimate_export import _metadata
    path = vc._find_session_dir(str(folder), vc.FINAL_WORKS_JSON)
    if not path:
        return []
    _, records = _metadata(Path(path).parent)
    return records


def relevant_codes(records, part, kind, resource, name):
    result = set()
    for header, work, facts in records:
        code = str(work.get('pressmark') or work.get('code') or '')
        if not code or code.startswith('1.') != resource:
            continue
        text = ' '.join(str(v or '') for v in (header, facts.get('name'), facts.get('part'), ' '.join(map(str, facts.get('path') or []))))
        source_part = facts.get('part') if facts.get('part') in ('above', 'underground', 'parking') else mapping.detect_part(text)
        source_kind = mapping.detect_kind(text)
        if part and source_part and part != source_part or kind and source_kind and kind != source_kind:
            continue
        title = str(work.get('title') or '')
        if category(name, resource) != category(title, resource):
            continue
        result.add(code)
    return result


def choose(name, *, resource=False, part=None, kind=None, parent='', parameters='', selected=(), entries=None):
    entries = catalog() if entries is None else entries
    operation = category(name, resource)
    if kind in ('crane_foundation', 'equipment_foundation') and not any(r['kind'] == kind for r in entries):
        kind = 'foundation_slab'
    candidates = [r for r in entries if r['resource'] == resource and r['category'] == operation
                  and (not kind or not r['kind'] or r['kind'] == kind)]
    if kind and any(r['kind'] == kind for r in candidates):
        candidates = [r for r in candidates if r['kind'] == kind]
    if part and any(r['part'] == part for r in candidates):
        candidates = [r for r in candidates if r['part'] == part]
    if not candidates:
        return []
    target_words = words(name)
    target_params = parameter_values(parameters, name)

    def score(row):
        common = target_words & words(row['work'])
        similarity = len(common) / max(1, len(target_words | words(row['work'])))
        source_params = parameter_values(row['work'], row['title'])
        equal = sum(target_params[k] == source_params[k] for k in target_params.keys() & source_params.keys())
        conflict = sum(target_params[k] != source_params[k] for k in target_params.keys() & source_params.keys())
        # Actual list codes lead among compatible operations. Parameters outrank
        # incidental wording; examples from other projects never enter this score.
        return (row['code'] in selected, -conflict, equal, norm(name) == norm(row['work']),
                row['part'] == part, row['kind'] == kind,
                len(words(parent) & words(row['parent'])), similarity, -row['row'])

    if operation == 'other':
        candidates = [r for r in candidates if len(target_words & words(r['work'])) / max(1, len(target_words | words(r['work']))) >= .3]
    if not candidates:
        return []
    best = max(candidates, key=score)
    result = [best]
    if not resource and 'монтаж' in norm(name) and 'демонтаж' in norm(name):
        # A combined VOR operation needs both independent installation rates.
        opposite = [r for r in candidates if ('демонтаж' in norm(r['title'])) != ('демонтаж' in norm(best['title']))
                    and r['work'] == best['work'] and r['kind'] == best['kind']]
        if opposite:
            result.append(max(opposite, key=score))
        result.sort(key=lambda r: 'демонтаж' in norm(r['title']))
    return result


def rate_quantity(quantity, vor_unit, rate_unit):
    clean = re.sub(r'\s+(?:в деле|конструктивного объема сваи)$', '', rate_unit)
    clean = re.sub(r'^(\d+)\s+свая$', r'\1 шт.', clean)
    a, af = unit_and_factor(vor_unit)
    b, bf = unit_and_factor(clean)
    if quantity is None or a != b or af is None or bf is None:
        return None
    return float(quantity) * float(af / bf)


def aggregate_references(references):
    """Sum quantities by rate within a displayed aggregate, never reuse its first row."""
    groups = {}
    for reference in references:
        codes = str(reference.get('code') or '').splitlines()
        columns = {key: str(reference.get(key) or '').splitlines() for key in ('title', 'unit', 'source')}
        raw = reference.get('quantity')
        quantities = str(raw).splitlines() if raw is not None else []
        for i, code in enumerate(codes):
            unit = columns['unit'][i] if i < len(columns['unit']) else ''
            group = groups.setdefault((code, unit), dict(title=columns['title'][i] if i < len(columns['title']) else '', quantities=[], sources=[]))
            from ..estimate_comparison import number
            group['quantities'].append(number(quantities[i]) if i < len(quantities) else None)
            group['sources'].extend(columns['source'])
    if not groups:
        return {}
    from ..estimate_comparison import known_sum
    totals = [known_sum(group['quantities']) for group in groups.values()]
    return dict(code='\n'.join(key[0] for key in groups), unit='\n'.join(key[1] for key in groups),
                title='\n'.join(group['title'] for group in groups.values()),
                quantity=float(totals[0]) if len(totals) == 1 and totals[0] is not None else
                         '\n'.join('' if v is None else format(v, 'f') for v in totals),
                source='\n'.join(dict.fromkeys(source for group in groups.values() for source in group['sources'])))


def enrich(path, *, records=None, catalog_path=CATALOG_PATH):
    """Append rate references to our completed VOR, preserving A:H and quantities."""
    from ..comparison_typical import read_typical_workbook
    from .template import DEFAULT_SHEET
    rows = read_typical_workbook(path)
    records = selected_records(Path(path).parent) if records is None else records
    entries = catalog(catalog_path)
    book = load_workbook(path)
    sheet = book[DEFAULT_SHEET]
    for column, header in enumerate(HEADERS, 9):
        cell = sheet.cell(2, column, header)
        cell._style = copy(sheet.cell(2, 8)._style)
        cell.alignment = Alignment(wrap_text=True, vertical='center')
    for row in rows:
        if row.get('isSeparator') or not row.get('work'):
            continue
        context = ' '.join(str(row.get(k) or '') for k in ('part', 'section', 'subsection', 'elementGroup'))
        part = mapping.detect_part(context)
        kind = next((detected for key in ('elementGroup', 'subsection', 'section')
                     if (detected := mapping.detect_kind(str(row.get(key) or '')))), None)
        kind = kind or mapping.detect_kind(row['work'])
        resource = row['kind'] == 'resource'
        selected = relevant_codes(records, part, kind, resource, row['work'])
        chosen = choose(row['work'], resource=resource, part=part, kind=kind,
                        parent=row.get('parentWork', ''), parameters=row.get('estimateParameterValues', ''),
                        selected=selected, entries=entries)
        if not chosen:
            continue
        quantities = [rate_quantity(row['quantity'], row['unit'], r['unit']) for r in chosen]
        values = ['\n'.join(r['code'] for r in chosen), '\n'.join(r['title'] for r in chosen),
                  '\n'.join(r['unit'] for r in chosen),
                  quantities[0] if len(chosen) == 1 else '\n'.join('' if q is None else f'{q:.8g}' for q in quantities),
                  '\n'.join(f'{SHEET}, строка {r["row"]}' for r in chosen)]
        for col, value in enumerate(values, 9):
            cell = sheet.cell(row['sourceRow'], col, value)
            cell._style = copy(sheet.cell(row['sourceRow'], 8)._style)
            cell.alignment = Alignment(wrap_text=True, vertical='top')
            if isinstance(value, str): cell.data_type = 's'
            if col == 12: cell.number_format = '0.00000###'
        width = 58
        lines = sum(max(1, (len(line) + width - 1) // width) for line in values[1].splitlines())
        sheet.row_dimensions[row['sourceRow']].height = max(sheet.row_dimensions[row['sourceRow']].height or 15, lines * 15)
    for column, width in (('I', 22), ('J', 65), ('K', 22), ('L', 25), ('M', 40)):
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = 'D4'
    sheet.print_area = f'A1:M{sheet.max_row}'
    from .model_links import workbook_digest, save as save_links
    import json
    trace_path = Path(path).with_suffix('.model-links.json')
    trace = None
    if trace_path.is_file():
        candidate = json.loads(trace_path.read_text(encoding='utf-8'))
        if candidate.get('sha256') == workbook_digest(path):
            trace = candidate
    try:
        book.save(path)
        if trace is not None:
            save_links(path, trace['sheet'], trace['rows'])
    finally:
        book.close()
