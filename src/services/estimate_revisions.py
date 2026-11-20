"""Recognize explicit before/after columns without using project-specific offsets."""
import re


def norm(value):
    return re.sub(r'\s+', ' ', str(value or '').casefold().replace('ё', 'е')).strip()


def schema(parent, child):
    """Select the final state of a two-row comparative estimate header.

    Numeric values never fall back to old values or change columns. Only blank
    repeated code/name cells can reuse their declared before-change counterpart.
    """
    result = {}
    starts = [i for i, text in enumerate(parent) if norm(text)]
    for pos, start in enumerate(starts):
        text = norm(parent[start])
        end = starts[pos + 1] if pos + 1 < len(starts) else len(parent)
        field = ('code' if 'шифр' in text and not any(v in text for v in ('ссылк', 'документац')) else 'name' if 'наименован' in text else
                 'unit' if 'ед' in text and 'изм' in text else
                 'quantity' if 'объем' in text or 'колич' in text else
                 'total' if 'стоимост' in text and not any(v in text for v in ('разниц', 'единиц', 'увеличен', 'снижен')) else None)
        if field is None or field in result:
            continue
        if field == 'unit':
            result[field] = start
            continue
        final = next((i for i in range(start, min(end, len(child))) if 'с учетом изменен' in norm(child[i])), None)
        before = next((i for i in range(start, min(end, len(child))) if 'до изменен' in norm(child[i])), None)
        if final is not None:
            result[field] = final
            if field in ('code', 'name') and before is not None:
                result[field + '_before'] = before
            if field == 'total':
                result['priceLevel'] = 'current' if 'текущ' in text else 'unspecified'
    return result if all(key in result for key in ('code', 'name', 'unit', 'quantity', 'total')) else None
