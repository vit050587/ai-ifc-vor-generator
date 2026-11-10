"""Shared physical volume selection for IFC, work lists, VOR and the viewer.

Raw properties remain intact. Canonical columns are Excel-safe and retain the
selected property and status. Gross volume is deliberately a separate measure.
"""
import math
import re
from dataclasses import dataclass

VALUE = 'Количество::Объём_м3'
SOURCE = 'Количество::Источник_объёма'
STATUS = 'Количество::Статус_объёма'


@dataclass(frozen=True)
class Volume:
    value: float | None
    source: str = ''
    status: str = 'missing_data'


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(re.sub(r'[\s\u00a0\u202f]', '', str(value)).replace(',', '.'))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def volume_rank(key):
    key = str(key).lower().replace('ё', 'е').replace('³', '3')
    if any(word in key for word in ('gross', 'ratio', 'reinforcement', 'коэффициент')):
        return None
    name = re.sub(r'[\s_-]', '', key.rsplit('::', 1)[-1])
    if name in ('netvolume', 'чистыйобъем', 'объемнетто'):
        return 0
    if name in ('volume', 'объем'):
        return 1 if key.startswith('qto::') else 2
    if key.startswith('qto_bbox::') and 'объем' in key:
        return 3
    if re.search(r'(объем|volume).*?(м3|m3|литр|lit[er])', key):
        return 2
    return None


def volume_columns(columns):
    return sorted((c for c in columns if volume_rank(c) is not None),
                  key=lambda c: (volume_rank(c), str(c)))


def unit_factor(unit):
    unit = str(unit).lower().replace('³', '3').replace(' ', '').replace('m', 'м')
    return {'м3': 1., 'мм3': 1e-9, 'см3': 1e-6, 'дм3': .001,
            'l': .001, 'л': .001, 'литр': .001, 'литры': .001}.get(unit)


def resolve_volume(row, units=None):
    """Choose once per element; preserve zero, reject contradictory peer fields.

    Legacy raw dumps assume m3 when no unit metadata exists. Newly extracted IFC
    rows use stamp_ifc_volume and therefore explicit/project units instead.
    """
    if STATUS in row and str(row[STATUS]) not in ('', '-', 'nan', 'None'):
        value = number(row.get(VALUE))
        status = str(row[STATUS])
        if value is None and status in ('calculated', 'estimated'):
            status = 'missing_data'
        return Volume(value, str(row.get(SOURCE) or ''), status)
    candidates = []
    for key in volume_columns(row.keys()):
        value = number(row[key])
        if value is None:
            continue
        if units is not None and str(key).startswith('QTO::'):
            factor = unit_factor(units.get(key, ''))
            if factor is None:
                candidates.append((volume_rank(key), str(key), None))
                continue
        else:
            label = str(key).lower()
            factor = .001 if 'литр' in label or 'liter' in label else 1.
        candidates.append((volume_rank(key), str(key), value * factor))
    if not candidates:
        return Volume(None)
    rank = min(item[0] for item in candidates)
    peers = [item for item in candidates if item[0] == rank]
    source, value = peers[0][1:]
    if any(item[2] is None for item in peers):
        return Volume(None, '; '.join(item[1] for item in peers), 'missing_units')
    if any(not math.isclose(value, item[2], rel_tol=1e-7, abs_tol=1e-9) for item in peers):
        return Volume(None, '; '.join(item[1] for item in peers), 'conflicting_data')
    return Volume(value, source, 'estimated' if rank == 3 else 'calculated')


def stamp_volume(row, volume):
    row.update({VALUE: volume.value if volume.value is not None else '',
                SOURCE: volume.source, STATUS: volume.status})
    return row


def ifc_volume_value(qty, model):
    """Convert a typed quantity using its own unit or the project volume unit."""
    from ifcopenshell.util.unit import get_property_unit, get_prefix_multiplier

    def factor(unit):
        if unit is None:
            return None
        if unit.is_a('IfcSIUnit') and unit.Name == 'CUBIC_METRE':
            return get_prefix_multiplier(unit.Prefix) ** 3
        if unit.is_a('IfcConversionBasedUnit'):
            nested = factor(unit.ConversionFactor.UnitComponent)
            return None if nested is None else float(unit.ConversionFactor.ValueComponent.wrappedValue) * nested
        return None

    nominal = getattr(qty, 'NominalValue', None)
    value = number(qty.VolumeValue if qty.is_a('IfcQuantityVolume') else getattr(nominal, 'wrappedValue', None))
    scale = factor(get_property_unit(qty, model))
    return value * scale if value is not None and scale is not None else None


def stamp_ifc_volume(row, element):
    """Read typed IFC quantities and their actual units, with instance priority."""
    from .ifc_raw_dump import _iter_type_propsets
    values = {}
    unknown_units = set()
    try:
        model = element.file
        definitions = [rel.RelatingPropertyDefinition for rel in getattr(element, 'IsDefinedBy', ())
                       if rel.is_a('IfcRelDefinesByProperties')]
        definitions.extend(_iter_type_propsets(element))
        for qto in definitions:
            is_qto = qto.is_a('IfcElementQuantity')
            if not is_qto and not qto.is_a('IfcPropertySet'):
                continue
            for qty in (qto.Quantities if is_qto else qto.HasProperties) or ():
                nominal = getattr(qty, 'NominalValue', None)
                typed_property = qty.is_a('IfcPropertySingleValue') and nominal is not None and nominal.is_a('IfcVolumeMeasure')
                if not qty.is_a('IfcQuantityVolume') and not typed_property:
                    continue
                prefix = 'QTO' if is_qto else 'Свойство'
                key = f'{prefix}::{qto.Name or ""}::{qty.Name}'
                if volume_rank(key) is None or key in values:
                    continue
                value = number(qty.VolumeValue if is_qto else nominal.wrappedValue)
                normalized = ifc_volume_value(qty, model)
                values[key] = normalized if normalized is not None else ''
                if value is not None and normalized is None:
                    unknown_units.add(key)
    except (AttributeError, RuntimeError, TypeError, KeyError):
        return stamp_volume(row, Volume(None, status='missing_data'))
    if values:
        primary_rank = min(volume_rank(key) for key in values)
        primary_unknown = sorted(key for key in unknown_units if volume_rank(key) == primary_rank)
        if primary_unknown:
            return stamp_volume(row, Volume(None, '; '.join(primary_unknown), 'missing_units'))
        return stamp_volume(row, resolve_volume(values))
    # Only explicitly labelled model geometry is eligible as a fallback.
    geometry = {key: value for key, value in row.items() if str(key).startswith('QTO_bbox::')}
    return stamp_volume(row, resolve_volume(geometry))
