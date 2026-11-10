"""Physical counts for precast items, with explicit variants kept separate."""
import re

from .quantity import Quantity
from ..works_comparison.units import unit_and_factor


def count_factor(unit):
    name, factor = unit_and_factor(unit)
    return float(factor) if name == 'шт.' and factor is not None else None


def variant(text):
    text = str(text or '').lower().replace('ё', 'е')
    if re.search(r'трехслойн|3\s*[- ]?слойн', text): return 'three_layer'
    if re.search(r'двухслойн|2\s*[- ]?слойн', text): return 'two_layer'
    if 'лоджи' in text: return 'loggia'
    if 'внутренн' in text: return 'internal'
    return ''


def select_variant(items, name):
    requested = variant(name)
    if not requested:
        return list(items)
    def description(item):
        return ' '.join(str(getattr(item, field, '') or '') for field in ('name', 'group_name', 'mge_name', 'material'))
    return [item for item in items if variant(description(item)) == requested]


def count(items, unit='шт.'):
    """One physical element per GUID; absent IDs do not collapse distinct items."""
    unique = {}
    for item in items:
        unique.setdefault(getattr(item, 'guid', '') or id(item), item)
    factor = count_factor(unit)
    if factor is None:
        return None
    return Quantity(len(unique) / factor, expression=f'{len(unique)} / {factor:g}' if factor != 1 else str(len(unique)),
                    element_guids=[getattr(item, 'guid', '') for item in unique.values()])
