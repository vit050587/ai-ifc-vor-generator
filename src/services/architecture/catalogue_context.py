"""Optional composition from the locally loaded TSN, matched by exact code."""
def enrich(work):
    if any(work.get(k) for k in ('composition', 'workComposition', 'work_composition')):
        return work
    from src.services import tsn_coeffs
    tsn_coeffs._load()
    code = str(work.get('pressmark') or '')
    info = tsn_coeffs._BOOK.get(code) or tsn_coeffs._BOOK.get(code.removeprefix('3.')) or {}
    if info.get('composition'):
        return dict(work, composition=info['composition'],
                    composition_source=info.get('doc'),
                    materials_not_included=info.get('not_included') or [])
    return work
