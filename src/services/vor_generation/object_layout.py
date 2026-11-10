"""Keep all compared works of a bound object under a single output heading."""
from collections import OrderedDict
from copy import copy


def regroup(records, order):
    from ..comparison_layout import _heading_level, copy_cell_style
    from ..comparison_typical import norm
    packets = OrderedDict()
    for index in order:
        row = records[index].get('comparison') or {}
        if row.get('comparisonObjectKey'):
            packets.setdefault(tuple(row['comparisonObjectKey']), []).append(index)
    if not packets:
        return order
    clusters, moved = {}, set()
    for key, indices in packets.items():
        names = {norm(records[i]['comparison'].get('generatedElementGroup')) for i in indices
                 if records[i]['comparison'].get('generatedSourceRow') is not None}
        if names == {norm(records[indices[0]]['comparison']['comparisonObject'])}:
            continue
        sample = next((i for i in indices if records[i].get('source') is not None), indices[0])
        record = records[sample]
        ancestors = [i for i in record['ancestors'] if _heading_level(str(records[i]['cells'][0].value or '')) < 3]
        old_heading = next((i for i in reversed(record['ancestors'])
                            if _heading_level(str(records[i]['cells'][0].value or '')) == 3), None)
        destination = old_heading if old_heading is not None else sample
        cells = [copy(cell) for cell in record['cells']]
        # Source rows of customer-only records are ordinary cells as well.
        for cell, source in zip(cells, records[destination]['cells']):
            cell._style = copy(cell._style)
            copy_cell_style(source,cell)
            cell.value = None
        cells[0].value = record['comparison']['comparisonObject']
        heading = len(records)
        records.append(dict(cells=cells,dimension=copy(records[destination]['dimension']),
                            source=None,comparison=None,ancestors=list(ancestors)))
        for index in indices:
            records[index]['ancestors'] = [*ancestors,heading]
        # Several packets may start at the same former source heading.
        clusters.setdefault(destination,[]).extend([heading,*indices])
        moved.update(indices)
    return [item for index in order for item in [*clusters.get(index,()), *(() if index in moved else (index,))]]
