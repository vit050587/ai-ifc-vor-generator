"""Build both session VORs with the original IFC/document builders."""
import os
from pathlib import Path

from . import builder, customer_vor, documents, template


def _copy_source(book, path, title):
    from copy import copy
    from openpyxl import load_workbook
    from ..comparison_layout import copy_cell_style
    source_book = load_workbook(path)
    try:
        source = source_book[template.DEFAULT_SHEET]
        target = book.create_sheet(title)
        for row in source:
            for cell in row:
                destination = target.cell(cell.row, cell.column, cell.value)
                copy_cell_style(cell, destination)
                if cell.comment:
                    destination.comment = copy(cell.comment)
        for area in source.merged_cells.ranges:
            target.merge_cells(str(area))
        for key, dimension in source.column_dimensions.items():
            target.column_dimensions[key] = copy(dimension)
        for key, dimension in source.row_dimensions.items():
            target.row_dimensions[key] = copy(dimension)
        target.sheet_properties = copy(source.sheet_properties)
        target.page_setup = copy(source.page_setup)
        target.print_options = copy(source.print_options)
        if source.print_area:
            target.print_area = str(source.print_area)
    finally:
        source_book.close()


def _compare_sources(generated, customer, output_path, llm):
    from ..comparison_typical import compare_workbooks
    from ..comparison_layout import typical_views
    from ..comparison_ai_reason import add_ai_reasons
    from . import reconcile
    typical = compare_workbooks(generated, customer)
    summary = reconcile.explain(typical['rows'])
    add_ai_reasons(typical, llm=llm)
    book = typical_views(typical)
    try:
        _copy_source(book, generated, 'Сформированная ВОР')
        _copy_source(book, customer, 'ВОР заказчика')
        reconcile.write_excel(typical['rows'], summary, str(output_path), workbook=book)
    finally:
        book.close()
    rows = [row for row in typical['rows'] if not row.get('isSeparator')]
    return {'file':str(output_path), 'comparisonApproach':'original_workbooks',
            'aiReasonSummary':typical.get('aiReasonSummary', {}),
            'template_rows':len(rows), 'verdicts':dict(summary['counts']),
            'filled_ours':sum(row.get('generatedQuantity') is not None for row in rows),
            'filled_vor':sum(row.get('customerQuantity') is not None for row in rows),
            'filled_both':sum(row.get('generatedQuantity') is not None and row.get('customerQuantity') is not None for row in rows)}


def _build(run_dir, output_path=None, template_path=None, session=None, llm=None):
    from .. import vor_comparison as vc
    from .typical_fill import OUTPUT_FILENAME, GENERATED_FILENAME, CUSTOMER_FILENAME

    root = Path(run_dir)
    raw = vc._find_session_dir(str(root), 'IFC_исходные_параметры.json')
    if not raw:
        raise ValueError('Для формирования типовых ВОР нужны исходные элементы IFC. Повторите обработку модели.')
    vor_rows, _ = vc._load_vor_rows(str(root))
    if not vor_rows:
        raise ValueError('ВОР заказчика не разобран: проверьте состояние загруженного документа')
    session = session or {}
    paths = {kind: session.get(f'{kind}_file_path') for kind in ('pos','pz','pd')}
    docs = documents.load_documents(paths, use_llm=False)
    docs.vor_rows = vor_rows
    docs.rebar = documents.extract_rebar(vor_rows)
    vor_path = session.get('vor_file_path') or vc._find_vor_pdf(str(root)) or 'ВОР заказчика'
    docs.file_names['vor'] = os.path.basename(vor_path)
    for row in vor_rows:
        docs.add_rows([documents.DocumentRow(
            name=str(row.get('name') or ''), unit=str(row.get('unit') or ''),
            volume=documents.pick_volume(row.get('volume_now'), row.get('volume_was')),
            section=str(row.get('section') or ''), source='ВОР', file_name=docs.file_names['vor'])])

    from .elements import ElementIndex
    index = ElementIndex.from_raw_dump(str(raw))
    index.apply_vor_foundation_changes(vor_rows)
    prepared_rows = documents.customer_rows(docs)
    prepared_template = template.parse_template(template_path)
    generated, customer = root / GENERATED_FILENAME, root / CUSTOMER_FILENAME
    # Use the original rules: IFC quantities and concrete, document reinforcement,
    # and source rows for joints, waterproofing, and thermal insulation.
    generated_stats = builder.build_vor(str(raw), docs, str(generated), template_path=template_path,
                                        ifc_name=session.get('original_filename') or 'Модель IFC',
                                        prepared_index=index, prepared_template=prepared_template)
    customer_stats = customer_vor.build_customer_vor(
        str(vor_path), str(customer), template_path=template_path, elements_json_path=str(raw), raw_rows=vor_rows,
        prepared_rows=prepared_rows, prepared_rebar=docs.rebar, prepared_index=index, prepared_template=prepared_template)
    result = _compare_sources(generated, customer, output_path or root / OUTPUT_FILENAME, llm)
    result.update(source_files=[{'kind':'generated','file':str(generated)},
                                {'kind':'customer','file':str(customer)}],
                  generated=generated_stats, customer=customer_stats, formation='ifc_documents')
    return result


def build(run_dir, output_path=None, template_path=None, session=None, llm=None, force=False):
    """Original IFC/document formation and workbook comparison, reusing unchanged results."""
    from . import formation_cache
    root = Path(run_dir)
    root.mkdir(parents=True, exist_ok=True)
    template_path = template_path or template.DEFAULT_TEMPLATE_PATH
    # Custom clients are intentionally not reused as a persistent model identity.
    use_cache = llm is None and output_path is None
    with formation_cache.session_lock(root):
        key = formation_cache.fingerprint(root, template_path, session) if use_cache else None
        if use_cache and not force:
            cached = formation_cache.load(root, key)
            if cached is not None:
                return cached
        result = _build(run_dir, output_path=output_path, template_path=template_path, session=session, llm=llm)
        result['cacheHit'] = False
        if use_cache:
            formation_cache.save(root, key, result)
        return result
