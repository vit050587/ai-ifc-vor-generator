"""Follow the customer's explicit stair breakdown without splitting source totals.

Only an unambiguous policy for the construction type and building part is
applied. Mixed combined/separate source scopes retain separate template rows;
their totals are never distributed in proportion to IFC volumes.
"""
import re
from dataclasses import replace

from . import mapping

BAND_NAMES = {'first': 'Лестницы первого этажа', 'upper': 'Лестницы выше первого этажа',
              'general': 'Лестницы без разбивки по этажам'}


def source_band(text):
    text = str(text or '').lower().replace('ё', 'е')
    if re.search(r'выше\s*1\s*[- ]?\s*(?:го|ого)?\s*этажа|выше\s+первого', text):
        return 'upper'
    if re.search(r'(?<!\d)1\s*[- ]?\s*(?:го|ого)?\s*этажа|первого\s+этажа', text):
        return 'first'
    return 'general'


def element_band(element):
    text = (element.storey or '').lower()
    match = re.search(r'(-?\d+)\s*[_ -]*этаж|(?:level|floor)\s*(-?\d+)', text)
    number = int(next(value for value in match.groups() if value is not None)) if match else None
    return 'first' if number == 1 else 'upper' if number is not None and number > 1 else 'general'


def landing_role(row, levels):
    """Compare geometry and storey elevations in the same building-relative SI frame."""
    bottom, top = row.get('Отметка_низа_м'), row.get('Отметка_верха_м')
    if not isinstance(bottom, (float, int)) or not isinstance(top, (float, int)) or not levels:
        return 'unknown'
    if any(min(abs(bottom-level), abs(top-level)) <= .03 for level in levels):
        return 'storey'
    return 'intermediate' if min(levels) < bottom <= top < max(levels) else 'unknown'


def needs_bands(source_rows, kind, part):
    return kind in ('stair', 'precast') and part == 'above' and any(
        row.kind == kind and row.part == part and source_band(row.section) != 'general' for row in source_rows)


def band_heading(rows, band):
    from .template import OutputRow
    return OutputRow(src_index=rows[0].index, label=BAND_NAMES[band], name_override='',
                     extra={column:None for column in range(3,9)})


def band_rebar(rebar, band):
    return replace(rebar, rows=[row for row in rebar.rows if source_band(row.section) == band])



def combined_categories(customer_rows, kind, part):
    categories = {}
    for row in customer_rows:
        if row.kind != kind or row.part and part and row.part != part:
            continue
        if row.category not in ('concrete', 'formwork', 'rebar', 'count'):
            continue
        categories.setdefault(row.category, set()).add(row.component or 'combined')
    return {key for key, components in categories.items() if components == {'combined'}}


def adapt_template(rows, customer_rows):
    """One combined operation replaces its flight/landing template operations."""
    policies = {}
    output = []
    current_category = None
    current_component = ''
    skip_children = False
    for row in rows:
        if row.kind not in ('work', 'resource', 'variant', 'empty'):
            current_category, current_component, skip_children = None, '', False
        if row.element_kind not in ('stair', 'precast'):
            output.append(row)
            continue
        key = (row.element_kind, row.part)
        if key not in policies:
            policies[key] = combined_categories(customer_rows, *key)
        if row.kind == 'work':
            current_category = mapping.classify_work(row.name)
            from .precast_quantities import count_factor
            if count_factor(row.unit) is not None:
                current_category = 'count'
            current_component = mapping.stair_component(row.name) or current_component
            skip_children = current_category in policies[key] and current_component == 'landing'
        if skip_children:
            continue
        if row.kind == 'work' and current_category in policies[key] and current_component == 'flight':
            row = replace(row, name=re.sub(r'марш(?:ей|и|а|ам|ами|ах)?', 'маршей и площадок', row.name, flags=re.I))
        output.append(row)
    return output


def route_landings(index, customer_rows):
    """Landings included in customer floor rows belong there on the IFC side too."""
    for part in ('above', 'underground'):
        floor = [row for row in customer_rows if row.kind == 'slab' and row.component in ('landing', 'combined')
                 and row.part == part and row.category in ('concrete', 'formwork')]
        stairs = [row for row in customer_rows if row.kind == 'stair' and row.component in ('landing', 'combined')
                  and row.part == part and row.category in ('concrete', 'formwork')]
        if floor and not stairs:
            for element in index.elements:
                if element.kind == 'stair' and element.component == 'landing' and element.part == part:
                    element.kind = 'slab'
        # The customer's intermediate-landings scope excludes storey-level
        # slabs. Keep first-floor stairs intact when that is a separate scope.
        intermediate = any(row.kind == 'stair' and row.part == part and 'промежуточн' in row.section.lower()
                           for row in customer_rows)
        first_separate = any(row.kind == 'stair' and row.part == part and source_band(row.section) == 'first'
                             for row in customer_rows)
        if intermediate:
            for element in index.elements:
                if (element.kind == 'stair' and element.component == 'landing' and element.part == part
                        and element.landing_role == 'storey' and not (first_separate and element_band(element) == 'first')):
                    element.kind = 'slab'
    index._names_cache.clear()


def mixed_rows(template_rows, source_rows, kind, part, reference, generated=False):
    """Keep combined source totals visible alongside explicitly separate scopes.

    An additional model quantity is left empty: the model elements are already
    accounted for by the separate operations and cannot also be used here.
    """
    if kind not in ('stair', 'precast'):
        return []
    from .template import OutputRow
    from .precast_quantities import count_factor
    scoped = [row for row in source_rows if row.kind == kind and (not part or not row.part or part == row.part)]
    result = []
    used = set()
    next_number = max((int(row.number) for row in template_rows if row.kind == 'work' and row.number.isdigit()), default=0)
    for row in template_rows:
        if row.kind != 'work':
            continue
        category = 'count' if count_factor(row.unit) is not None else mapping.classify_work(row.name)
        if category not in ('concrete', 'formwork', 'count') or category in used:
            continue
        members = [source for source in scoped if source.category == category]
        components = {member.component or 'combined' for member in members}
        if 'combined' not in components or not components.intersection(('flight', 'landing')):
            continue
        used.add(category)
        combined = [member for member in members if member.component in ('', 'combined')]
        volume = None if generated else sum(member.volume for member in combined)
        factor = count_factor(row.unit)
        if volume is not None and factor is not None:
            volume = volume * (1 / factor)
        name = re.sub(r'марш(?:ей|и|а|ам|ами|ах)?|площад(?:ок|ки|ка|кам|ками|ках)', 'маршей и площадок', row.name, flags=re.I)
        name += ' (объединённые позиции заказчика)'
        next_number += 1
        reason = reference + ('. Для объединённых позиций заказчика нет отдельной привязки элементов IFC; повторный объём не добавляется.' if generated else '')
        result.append(OutputRow(src_index=row.index, label=str(next_number), name_override=name,
                                volume=round(volume, 2) if volume is not None else None, justification=reason))
    return result
