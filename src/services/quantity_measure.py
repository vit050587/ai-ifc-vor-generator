"""Shared work-unit and operation interpretation for all calculation paths."""
import math
import re


def work_unit(work):
    return str(work.get('unit_of_measure') or work.get('unitOfMeasure') or work.get('unit') or '').strip()


def is_formwork(work):
    title = str(work.get('title') or work.get('name') or '').lower()
    unit = work_unit(work).lower().replace('²', '2').replace(' ', '')
    return 'опалубк' in title and ('м2' in unit or 'm2' in unit)


def work_divisor(work):
    # An explicit multiplier in the chosen unit defines the priced quantity.
    match = re.match(r'^(\d+(?:[.,]\d+)?)\s*(?=[^\d.,])', work_unit(work))
    if match:
        value = float(match.group(1).replace(',', '.'))
        if math.isfinite(value) and value > 0:
            return value
    try:
        value = float(str(work.get('okeiValue') or '').replace(',', '.'))
    except ValueError:
        return 1.0
    return value if math.isfinite(value) and value > 1 else 1.0


def work_unit_label(work):
    original = work_unit(work)
    unit = original.lower().replace(' ', '').replace('²', '2').replace('³', '3')
    if any(value in unit for value in ('м3', 'm3', 'м[3', 'm[3')):
        base = 'м³'
    elif any(value in unit for value in ('м2', 'm2', 'м[2', 'm[2')):
        base = 'м²'
    elif 'шт' in unit:
        base = 'шт'
    else:
        return original
    divisor = work_divisor(work)
    return f'{divisor:g} {base}' if divisor != 1 else base
