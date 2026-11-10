"""Quantity origins are recorded alongside arithmetic, never inferred from a title."""
import math


def text(value):
    if not isinstance(value, str):
        return ''
    value = value.strip()
    return '' if value.lower() in ('nan', 'none', '-', '') else value


def joined(values):
    return '; '.join(dict.fromkeys(v for value in values if (v := text(value))))


def summed(values):
    value = joined(values)
    return f'Σ({value})' if value else ''


def divided(source, divisor, value, result):
    return f'Рассчитан из полей: {source}; {value:g} / {divisor:g} = {result}'


def aggregate_sources(items):
    return {key: summed((item.get('_sources') or {}).get(key) for item in items)
            for key in ('volume', 'area', 'fw_hint', 'rebar_kg')} | {
                'count': f'Количество элементов IFC (GlobalId): {len(items)}'}


def ar_entry_sources(entry, rows):
    """Only use aligned original rows, and validate totals against the saved inputs."""
    from .ifc_quantities import resolve_volume, number
    from .group_excel import _element_formwork_measure, _find_formwork_columns
    indices = entry.get('source_indices') or []
    if not indices or any(not isinstance(i, int) or i < 0 or i >= len(rows) for i in indices):
        return {}
    selected = [rows[i] for i in indices]
    expected = str((entry.get('element') or {}).get('global_id') or '')
    if not expected or str(selected[0].get('GlobalId') or '') != expected:
        return {}
    q = entry.get('group_quantity') or {}
    out = {}
    def save(key, values, sources, target):
        if target is not None and math.isclose(sum(values), target, abs_tol=.011, rel_tol=1e-8):
            source = summed(sources)
            if source:
                out[key] = source
    volumes = [resolve_volume(row) for row in selected]
    save('volume_m3', [v.value or 0 for v in volumes], [v.source for v in volumes if v.value is not None], q.get('volume_m3'))
    # The grouped area is the sum of one actual field, not a per-element maximum.
    from .works_final_selector import _pick_group_area_m2
    areas = {key: sum(number(row.get(key)) or 0 for row in selected)
             for key in dict.fromkeys(key for row in selected for key in row if 'площадь' in str(key).lower())}
    # Use the existing selector to retain its exact field priority.
    area_trace = {}
    _pick_group_area_m2(areas, trace=area_trace)
    area_key = area_trace.get('key')
    if area_key:
        save('area_m2', [areas[area_key]], [area_key], q.get('area_m2'))
    columns = list(dict.fromkeys(key for row in selected for key in row))
    perimeters, depths = _find_formwork_columns(columns)
    formwork = [_element_formwork_measure(row, perimeters, depths) for row in selected]
    save('formwork_area', [v for v, _ in formwork], [s for v, s in formwork if v > 0], entry.get('group_formwork_area_m2'))
    save('rebar_kg', [(number(row.get('ReinforcementVolumeRatio')) or 0) * (v.value or 0)
                     for row, v in zip(selected, volumes)],
         [f'ReinforcementVolumeRatio (кг/м³) × {v.source}' for row, v in zip(selected, volumes)
          if number(row.get('ReinforcementVolumeRatio')) and v.value], entry.get('group_rebar_mass_kg'))
    out['count'] = f'Количество элементов IFC (GlobalId): {len(selected)}'
    return out


def normalized_rows(folder):
    from pathlib import Path
    import pandas as pd
    path = Path(folder) / 'filtered_elements.xlsx'
    if not path.exists():
        return []
    try:
        frame = pd.read_excel(path, sheet_name='Данные')
    except ValueError:
        frame = pd.read_excel(path)
    return frame.to_dict('records')


def restore_metadata_sources(folder, data):
    """Replay saved arithmetic only; never change a saved quantity or call a model.

    Candidates are accepted by _rows only when they reproduce the stored number.
    Unmatched historical quantities must not acquire a guessed IFC source.
    """
    from .works_cost import safe_float
    entries = data.get('elements') or []
    if not entries or not any(not text(work.get('volume_source'))
                              for entry in entries for work in entry.get('selected_works', [])):
        return
    rows = normalized_rows(folder)
    if not rows:
        return
    from .works_final_selector import _entry_work_volume
    from .works_cost import _get_corrected_volume
    import pandas as pd
    candidates, works = [], []
    for index, entry in enumerate(entries):
        origins = ar_entry_sources(entry, rows)
        for work in entry.get('selected_works') or []:
            value, source = _entry_work_volume(entry, work, origins)
            # Exclude intermediate keys when original fields cannot be traced.
            has_origin = any(origin and origin in source for origin in origins.values())
            candidates.append({'_norm_group': index, 'Шифр ТСН': work.get('pressmark'),
                               'Наименование расценки/ресурса': work.get('title'),
                               'Объём работ': value, 'Источник объёма': source if has_origin else ''})
            works.append(work)
    if not candidates:
        return
    frame = pd.DataFrame(candidates)
    try:
        frame = _get_corrected_volume(frame)
    except (OSError, ValueError, KeyError):
        # Without the norm catalogue only uncorrected quantities can match.
        pass
    for work, (_, row) in zip(works, frame.iterrows()):
        if text(row.get('Источник объёма')):
            work['_replayed_quantity'] = safe_float(row['Объём работ'], default=None)
            work['_replayed_source'] = row['Источник объёма']
