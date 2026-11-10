"""Combine equal estimate positions inside one template subsection.

Resource ownership is resolved before merging. Each resource retains its own
rate and corrections; equal resource codes with different corrections remain
separate, even under a combined parent work.
"""
from copy import deepcopy
import json

from .estimate_calculation import number, sum_known


def merge_rows(target, other):
    """Sum source quantities and control amounts before monetary calculation."""
    if 'source_rows' not in target:
        target['source_rows'] = [target['source_row']]
        target['merged_sources'] = [{key: target.get(key) for key in
                                     ('source_row','group','title','quantity_exact')}]
    target['source_rows'].extend(other.get('source_rows') or [other['source_row']])
    from .ifc_quantities import SOURCE
    def sources(row):
        value = (row.get('work') or {}).get('volume_source') or (row.get('facts') or {}).get(SOURCE)
        return row.get('volume_sources') or ([str(value)] if value else [])
    target['volume_sources'] = list(dict.fromkeys([*sources(target), *sources(other)]))
    target['merged_sources'].extend(other.get('merged_sources') or [
        {key: other.get(key) for key in ('source_row','group','title','quantity_exact')}])
    target['quantity_exact'] = float(number(target['quantity_exact']) + number(other['quantity_exact']))
    target['source_total'] = sum_known([target.get('source_total'), other.get('source_total')])
    for key in target.get('amounts', {}):
        target['amounts'][key] = sum_known([target['amounts'][key], other['amounts'][key]])
    for key, component in target.get('components', {}).items():
        component['source_amount'] = sum_known([component.get('source_amount'), other['components'][key].get('source_amount')])
        component['notes'] = list(dict.fromkeys([*component.get('notes', []), *other['components'][key].get('notes', [])]))


def signature(row):
    """Compare effective factor sets, prices and units, never height labels."""
    decisions = set()
    for item in row.get('decisions', []):
        if item.get('decision') != 'да' or not item.get('why') or item.get('ambiguous'):
            continue
        coefficients = {}
        for field, value in (item.get('coefficients') or {}).items():
            if field not in ('labor_and_wages','machine_operation','material_consumption'):
                continue
            value = number(value)
            if value is None or value <= 0 or value == 1:
                continue
            if field == 'material_consumption' and item.get('resource'):
                if not row['code'].startswith('1.'):
                    continue
                if str(item['resource']).casefold() not in str(row.get('title') or '').casefold():
                    continue
            coefficients[field] = str(value.normalize())
        if coefficients:
            decisions.add(json.dumps(coefficients, sort_keys=True))
    decisions = sorted(decisions)
    detail = row.get('catalog_detail') or {}
    fields = ('salary','operationOfMachines','costOfMaterialResources','salaryDrivers','fixedTime',
              'salaryInflRate','machinesInflRate','materialInflRate','baseWinterRisePrice',
              'baseWinterRisePriceMat','winterRisePrice','winterRisePriceMat','rateOverhead','rateProfit')
    payload = [row['code'], row.get('unit'), decisions,
               {key: value.get('correction') for key, value in row.get('components', {}).items()},
               row.get('driver_correction'), {key: detail.get(key) for key in fields}]
    return json.dumps(payload, sort_keys=True, default=str)


def consolidate(rows):
    from .estimate_export import _position_groups
    from .work_sections import sectioned_positions
    positions = list(sectioned_positions(_position_groups(rows)))
    result, lookup, children_by_parent, lists_by_parent = [], {}, {}, {}
    heading = ''
    for record, resources in positions:
        if 'heading' in record:
            heading = record['heading']
            continue
        scope = tuple(record.get('section_path') or ())
        # No inferred cross-group merge when a subsection cannot be identified.
        scope = scope or ('source', record.get('group_id'), record.get('group') or heading)
        key = (scope, signature(record))
        if record.get('quantity_exact') is None:
            key += (record['source_row'],)
        if key not in lookup:
            parent = deepcopy(record)
            parent['estimate_id'] = len(lookup) + 1
            parent.pop('estimate_parent_id', None)
            lookup[key] = parent
            children_by_parent[id(parent)] = {}
            lists_by_parent[id(parent)] = []
            result.append((heading, parent, lists_by_parent[id(parent)]))
        else:
            parent = lookup[key]
            merge_rows(parent, record)
        children = children_by_parent[id(parent)]
        output_children = lists_by_parent[id(parent)]
        for resource in resources:
            resource_key = signature(resource)
            if resource.get('quantity_exact') is None:
                resource_key += f':source:{resource["source_row"]}'
            if resource_key not in children:
                child = deepcopy(resource)
                child['estimate_parent_id'] = parent['estimate_id']
                children[resource_key] = child
                output_children.append(child)
            else:
                merge_rows(children[resource_key], resource)
    output = []
    for heading, parent, children in result:
        if heading:
            output.append({'heading': heading})
        output.extend([parent, *children])
    return output
