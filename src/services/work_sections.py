"""Shared customer-template hierarchy for estimate and model presentation.

Every sheet contributes examples. The compact VOR sheet supplies preferred
headings; other sheets supply additional construction types and parameters.
Unknown objects keep their original group instead of inventing a classification.
This module never changes quantities, resource ownership or group membership.
"""
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from openpyxl import load_workbook

from .construction_method import construction_method
from .vor_generation import mapping, template


def norm(value):
    return re.sub(r'\s+', ' ', str(value or '').lower().replace('ё', 'е')).strip()


def heading_key(value):
    return re.sub(r'^(?:подраздел|раздел)\s+\d+[.]?\s*', '', norm(value))


@dataclass(frozen=True)
class Section:
    path: tuple
    kind: str | None
    part: str | None
    method: str
    order: int
    sheet: str


class SectionCatalog:
    def __init__(self, sections):
        self.sections = sections

    def resolve(self, facts, source_path=(), title=''):
        facts = dict(facts or {})
        source_path = tuple(str(v) for v in source_path if v)
        text = ' '.join(str(facts.get(k) or '') for k in ('Name', 'Имя', 'ObjectType', 'name', 'typeName'))
        text = text or title or ' '.join(source_path)
        context = ' '.join(source_path)
        part_text = str(facts.get('part') or facts.get('Тип_этажа') or facts.get('levelType') or '')
        part = mapping.detect_part(part_text) or mapping.detect_part(context)
        if not part:
            storey = str(facts.get('Этаж') or facts.get('level') or '')
            if storey:
                from .vor_generation.elements import _storey_part
                _, resolved = _storey_part(storey, facts.get('Отметка_этажа', ''))
                part = resolved
        kind = mapping.detect_kind(text)
        if re.search(r'лестничн.*(?:площад|клетк)|(?:площад|клетк).*лестничн', norm(text)):
            kind = 'stair'
        if re.search(r'приям', norm(text)):
            kind = 'foundation_slab'
        if not kind:
            cls = facts.get('IfcClass') or facts.get('Тип элемента') or facts.get('ifc_class') or facts.get('ifcClass')
            kind = {'IfcWall': 'wall', 'IfcWallStandardCase': 'wall', 'IfcSlab': 'slab',
                    'IfcColumn': 'column', 'IfcBeam': 'beam', 'IfcStair': 'stair',
                    'IfcStairFlight': 'stair', 'IfcFooting': 'foundation_slab', 'IfcPile': 'pile'}.get(cls)
        kind = kind or mapping.detect_kind(context) or mapping.detect_kind(title)
        method = construction_method(facts)
        if method == 'unknown':
            method = construction_method({'Name': context})
        if method == 'precast':
            kind = {'wall': 'precast_panel', 'stair': 'precast', 'parapet': 'precast_parapet'}.get(kind, kind)
        if not kind or not part or method == 'conflicting':
            return None
        section_kind = 'foundation_slab' if kind == 'crane_foundation' else kind
        candidates = [s for s in self.sections if s.kind == section_kind and s.part == part
                      and (method == 'unknown' or s.method in ('unknown', method))]
        if not candidates:
            return None
        # Explicit source hierarchy and specific construction words outrank the
        # preferred sheet. Stable workbook order resolves equivalent examples.
        source_keys = {heading_key(v) for v in source_path}
        tokens = set(re.findall(r'[а-яa-z]{4,}', norm(text)))

        def score(section):
            exact = sum(heading_key(v) in source_keys for v in section.path)
            specific = len(tokens & set(re.findall(r'[а-яa-z]{4,}', norm(section.path[-1]))))
            return (-exact, -specific, section.order)

        selected = min(candidates, key=score)
        prefix = re.match(r'Подраздел\s+\d+[.]?\s*', selected.path[-1], re.I)
        prefix = prefix.group(0) if prefix else ''
        if kind in ('foundation_slab', 'crane_foundation'):
            path = (*selected.path[:-1], prefix + 'Фундаментная плита')
            if kind == 'crane_foundation':
                path += ('Фундамент под башенный кран',)
            elif re.search(r'приям', norm(text)):
                path += ('Приямки',)
            return replace(selected, path=path)
        if kind in ('stair', 'precast'):
            path = (*selected.path[:-1], prefix + 'Лестницы')
            cls = facts.get('ifc_class') or facts.get('IfcClass') or facts.get('ifcClass')
            if re.search(r'марш|\brun\b', norm(text)) or cls == 'IfcStairFlight':
                path += ('Лестничные марши',)
            elif re.search(r'площад|клетк|\blanding\b', norm(text)) or cls == 'IfcSlab':
                path += ('Лестничные площадки',)
            return replace(selected, path=path)
        return selected


@lru_cache(maxsize=4)
def _load(path, modified, size):
    book = load_workbook(path, read_only=True, data_only=True)
    sections, seen = [], set()
    try:
        names = sorted(book.sheetnames, key=lambda name: name != template.DEFAULT_SHEET)
        for sheet_name in names:
            part_text = section = subsection = ''
            part = None
            for values in book[sheet_name].iter_rows(min_row=4, max_col=3, values_only=True):
                a, b, c = (str(v or '').strip() for v in values)
                if not a or b or c:
                    continue
                if a.startswith('Подраздел '):
                    subsection = a
                elif a.startswith('Раздел '):
                    section, subsection = a, ''
                    continue
                elif mapping.detect_part(a):
                    part_text, part, section, subsection = a, mapping.detect_part(a), '', ''
                    continue
                else:
                    continue
                path_parts = (part_text, section, subsection)
                key = tuple(heading_key(v) for v in path_parts)
                if key in seen:
                    continue
                seen.add(key)
                kind = mapping.detect_kind(subsection)
                method = construction_method({'Name': section + ' ' + subsection})
                sections.append(Section(path_parts, kind, part, method, len(sections), sheet_name))
        return SectionCatalog(sections)
    finally:
        book.close()


def catalog(path=None):
    source = Path(path or template.DEFAULT_TEMPLATE_PATH)
    stamp = source.stat()
    return _load(str(source), stamp.st_mtime_ns, stamp.st_size)


def group_facts(rows):
    """Do not classify a mixed group from its first element alone."""
    methods = {construction_method(row) for row in rows}
    facts = dict(rows[0]) if rows else {}
    if len(methods) > 1:
        facts['Технология конструкции'] = 'conflicting'
    return facts


def sectioned_positions(positions, section_catalog=None, *, show_groups=True):
    """Reorder complete work/resource pairs without recalculating any amount."""
    section_catalog = section_catalog or catalog()
    blocks, heading, used = [], '', True
    def retain_empty():
        if heading and not used and not re.fullmatch(r'(?:подземная|надземная|цокольная|цоколь)\s*(?:часть)?', norm(heading)):
            blocks.append((None, heading, None, []))
    for record, resources in positions:
        if 'heading' in record:
            retain_empty()
            heading = record['heading']
            used = False
            continue
        used = True
        facts = record.get('facts') or {}
        source_path = facts.get('path') or ([facts['part']] if facts.get('part') else [])
        section = section_catalog.resolve(facts, source_path, record.get('title') or heading)
        record['section_path'] = list(section.path) if section else []
        for resource in resources:
            resource['section_path'] = list(record['section_path'])
        blocks.append((section, heading, record, resources))
    retain_empty()
    # Unclassified source groups retain their original order and heading.
    blocks.sort(key=lambda b: (b[0].order, b[0].path) if b[0] else (len(section_catalog.sections), ()))
    previous_path, previous_group = (), None
    for section, heading, record, resources in blocks:
        path = section.path if section else ()
        if path != previous_path:
            common = 0
            while common < min(len(path), len(previous_path)) and path[common] == previous_path[common]:
                common += 1
            for level in range(common, len(path)):
                yield {'heading': path[level], 'heading_level': ('part', 'section', 'subsection', 'subgroup')[min(level, 3)]}, []
            previous_group = None
        group_key = (path, (record or {}).get('group_id'), heading)
        if heading and (show_groups or not section) and (group_key != previous_group or record is None):
            yield {'heading': heading,
                   'heading_level': 'group' if section else 'source'}, []
        if record is not None:
            yield record, resources
        previous_path, previous_group = path, group_key
