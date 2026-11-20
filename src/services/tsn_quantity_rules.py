"""Project-dependent concrete measures, TSN 2001.3-6 §§2.1–2.7.

IFC physical solids remain source data. Explicit project measures can confirm
normative bounds; unspecified construction details are never guessed.
"""
import re


def concrete_volume(row, physical):
    from .ifc_quantities import Volume, number
    from .construction_method import construction_method
    if construction_method(row) == 'precast':
        return physical

    def field(name):
        keys = (f'ТСН::{name}', f'Свойство::ТСН::{name}')
        direct = next((row[k] for k in keys if k in row), None)
        if direct is not None:
            return direct
        if name == 'Масса_жёсткой_арматуры_т':
            for key, raw in row.items():
                label = str(key).lower().replace('ё', 'е')
                if re.search(r'жестк.*арматур|rigid.*reinforc|steel.*core', label) and re.search(r'масс|mass|weight', label):
                    amount = number(raw)
                    if amount is not None and re.search(r'кг|kg', label):
                        return amount / 1000
                    if amount is not None and re.search(r'(?:_т|тонн|tonne|ton)$', label):
                        return amount
        return None

    def value(name):
        return number(field(name))

    project = value('Объём_бетона_м3')
    if project is not None:
        return Volume(project, 'ТСН::Объём_бетона_м3 (проект, пп. 2.1–2.7, 2.10)', 'calculated')
    result = physical
    cls = str(row.get('IfcClass') or row.get('Тип элемента') or '')
    text = ' '.join(str(row.get(k) or '') for k in ('Name', 'Имя', 'ObjectType'))
    column = cls == 'IfcColumn' or (cls in ('IfcWall', 'IfcWallStandardCase') and
                                   re.search(r'пилон|колонн', text, re.I))
    section = value('Площадь_сечения_м2')
    height = value('Высота_колонны_по_проекту_м')
    span = value('Длина_балки_по_проекту_м')
    # The supplied normative length includes wall bearings, excludes column
    # supports; the supplied section excludes slab thickness for ribbed floors.
    if column and section is not None and height is not None:
        result = Volume(section * height + (value('Объём_консолей_м3') or 0),
                        'ТСН п. 2.2: сечение × проектная высота + консоли', 'calculated')
    elif cls == 'IfcBeam' and section is not None and span is not None:
        result = Volume(section * span + (value('Объём_вутов_м3') or 0),
                        'ТСН п. 2.3: проектное сечение × проектная длина + вуты', 'calculated')
    elif (cls == 'IfcBeam' or column) and result.value is not None:
        result = Volume(result.value, result.source + '; ТСН пп. 2.2–2.5: границы '
                        'с опорами/плитами не подтверждены проектными данными', 'estimated')

    if cls in ('IfcSlab', 'IfcSlabStandardCase') and not re.search(r'фундамент', text, re.I) and result.value is not None:
        confirmed = str(field('Стыки_и_опорные_части_учтены') or '').lower()
        if confirmed not in ('да', 'true', '1'):
            result = Volume(result.value, result.source + '; ТСН пп. 2.4–2.5: опорные '
                            'части, вуты и отсутствие задвоения с балками не подтверждены проектными данными',
                            'estimated')
    if cls in ('IfcWall', 'IfcWallStandardCase') and not column and result.value is not None:
        gross = number(row.get('QTO::Qto_WallBaseQuantities::GrossSideArea'))
        net = number(row.get('QTO::Qto_WallBaseQuantities::NetSideArea'))
        openings = row.get('Количество::Есть_проёмы') or (gross is not None and net is not None and gross > net)
        confirmed = str(field('Проёмы_по_наружному_обводу_коробок') or '').lower()
        if openings and confirmed not in ('да', 'true', '1'):
            result = Volume(result.value, result.source + '; ТСН п. 2.6: наружный обвод '
                            'коробок проёмов не подтверждён проектными данными', 'estimated')
    if row.get('Количество::Есть_анкерные_полости') and result.value is not None:
        result = Volume(result.value, result.source + '; ТСН п. 2.1: исключение '
                        'для пробок анкерных болтов требует проектных данных', 'estimated')

    rigid = bool(re.search(r'ж[её]стк.*арматур|стальн.*сердеч|сталебетон', text, re.I))
    mass = value('Масса_жёсткой_арматуры_т')
    if mass is not None or rigid:
        excluded = str(field('Жёсткая_арматура_исключена') or '').lower()
        if excluded in ('да', 'true', '1'):
            return result
        if excluded not in ('нет', 'false', '0') or mass is None or result.value is None:
            return Volume(result.value, result.source + '; Оценка: ТСН п. 2.7 — '
                          'вычет жёсткой арматуры не подтверждён; повторный вычет недопустим',
                          'estimated' if result.value is not None else 'missing_data')
        concrete = result.value - mass / 7.85
        if concrete < -1e-9:
            return Volume(None, 'ТСН п. 2.7: объём жёсткой арматуры больше исходного объёма',
                          'conflicting_data')
        result = Volume(max(0, concrete), result.source + f'; ТСН п. 2.7: − {mass:g} / 7,85 м³',
                        result.status)
    return result
