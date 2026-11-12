"""Сборка ВОР по типовому шаблону.

Алгоритм:

1. Разбираем типовой шаблон (:mod:`template`) — получаем строки с контекстом
   (часть здания, раздел, подраздел, тип конструкции, геометрия).
2. Строим индекс элементов IFC из сырого дампа (:mod:`elements`).
3. Извлекаем данные из документов (:mod:`documents`): из ВОР застройщика —
   арматуру и прочие ресурсы, из ПОС/ПЗ/ПД — дополнительные сведения.
4. Заполняем строки:

   * **целочисленные** строки-работы — объёмом из модели IFC
     (опалубка → площадь, бетонирование → объём и т. д.); объём арматуры
     («Устройство арматурных стержней»/«каркасов») — **только из ВОР
     застройщика**;
   * **дробные** строки-ресурсы — объёмом соответствующего ресурса:
     марка бетона — из модели IFC, арматура (диаметр/класс) — из ВОР
     застройщика, прочие материалы — из документов (ВОР/ПОС/ПЗ/ПД);

   Приоритет источников объёма: модель IFC → документы.
5. Группируем элементы по геометрии, если в начале подраздела стоит заголовок
   «Плита толщиной … мм» / «Стены толщиной … мм» / «Колонны сечением ахb мм»
   и т. п.: для каждого значения формируется свой блок строк.
6. Рендерим заполненный файл в копию шаблона (:func:`template.render_template`).

Строки, для которых данных нет, остаются с прочерком (пустой столбец «Объем»).
"""

import os
import re
from typing import Callable, Dict, List, Optional, Tuple

from src.core.logger import setup_logger
from . import documents, mapping, template
from .documents import Documents
from .elements import Element, ElementIndex, aggregate
from .quantity import Quantity

logger = setup_logger(__name__)

Progress = Optional[Callable[[str], None]]

_DASH = "—"

# Источник, объёмы которого не переносятся в типовой ВОР: из ВОР застройщика
# берётся только арматура (§4.3), остальные работы/ресурсы ВОР не подставляются.
_VOR_SOURCE = "ВОР"

# Виды заголовков, после которых начинается новый блок/раздел.
_HEADER_KINDS = ("part", "section", "subsection", "block")


def _say(progress: Progress, message: str) -> None:
    if progress is not None:
        try:
            progress(message)
        except Exception:  # pragma: no cover
            pass


# =====================================================================
#  Форматирование заголовков геометрических блоков
# =====================================================================

def _block_text(row: template.TemplateRow) -> str:
    """Текст блочного/геометрического заголовка шаблона.

    У строк-заголовков шаблона текст записан в колонке A (``number``), а не в
    колонке B (``name``), поэтому для заголовков используется ``number``.
    """
    return row.number or row.name


def _format_header(text: str, param: Optional[str], label: Optional[str]) -> str:
    """Подставляет фактическое значение геометрии в заголовок блока.

    Если данных для группировки нет (``label`` пуст), заполнитель заменяется
    прочерком — структура шаблона сохраняется, а отсутствие данных видно.
    """
    label = label or _DASH
    if param == "section":
        # «Колонны сечением ахb мм» → «Колонны сечением 400х400 мм»
        replaced = re.sub(r"а\s*х\s*b|a\s*[xх×]\s*b", label, text, flags=re.IGNORECASE)
        return replaced if replaced != text else f"{text} {label}".strip()
    # «…» или «...» — заполнитель значения.
    if "…" in text:
        return text.replace("…", label)
    if "..." in text:
        return text.replace("...", label)
    return f"{text} {label}".strip()


def _geometry_int(label: Optional[str]) -> Optional[int]:
    """Числовое значение геометрической подписи («1400» → 1400)."""
    if not label:
        return None
    m = re.match(r"\d+", label)
    return int(m.group(0)) if m else None


def _dominant_group_name(elements: List[Element]) -> str:
    """Преобладающее наименование группы элементов модели (``ObjectType``)."""
    counts: Dict[str, int] = {}
    for el in elements:
        name = ElementIndex.group_label(el)
        if name:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda item: item[1])[0]


def _group_header(elements: List[Element], param: Optional[str], label: Optional[str],
                  fallback_text: str) -> str:
    """Заголовок геометрического блока ВОР.

    Для групп, выделенных по геометрии, вместо текста шаблона («Плита толщиной
    … мм») подставляется наименование группы элементов модели
    (``Фундаментная плита:ADSK_Бетон В30_1000 мм``); при отсутствии данных —
    текст шаблона с фактическим значением геометрии.
    """
    name = _dominant_group_name(elements)
    if name:
        if param == "thickness" and label:
            from .thickness import parse
            named = parse(name,bare=True)
            if not named or named["mode"]!="exact" or abs(named["values"][0]-float(label))>.01:
                return name+f" (толщина по IFC: {label} мм)"
        return name
    return _format_header(fallback_text, param, label)


# =====================================================================
#  Расчёт объёмов
# =====================================================================

def _model_quantity(el, value, expression=None):
    source = ', '.join(filter(None, [el.name or el.group_name or el.mge_name, el.storey,
                                    el.quantity_sources.get('volume')]))
    return Quantity(value, f'{expression or f"{value:g}"} [{source}]', (source,), (el.guid,))


def _foundation_formwork_area(elements: List[Element]) -> Optional[float]:
    """Площадь опалубки фундаментной плиты — только торцы (периметр × толщина).

    Фундаментная плита бетонируется на бетонную подготовку, поэтому опалубка
    устраивается только по боковой поверхности (торцам): периметр контура ×
    толщину плиты. Площади нижней и верхней поверхностей в опалубку не входят.
    Периметр берётся из QTO или внешнего контура IFC; bbox остаётся явно
    обозначенной оценкой, если геометрию контура получить не удалось.
    """
    values = []
    for el in elements:
        perimeter = el.quantity("perimeter")       # мм
        thickness = el.geometry.get("thickness")   # мм
        if perimeter > 0 and thickness and thickness > 0:
            values.append(_model_quantity(el, perimeter * thickness / 1e6,
                          f'({perimeter:g} / 1000 × {thickness:g} / 1000)'))
    return sum(values) if values else None


def _formwork_area(kind: Optional[str], agg: Dict[str, object],
                   elements: List[Element]) -> Optional[float]:
    """Площадь опалубки по агрегатам группы элементов."""
    if kind in ("foundation_slab", "crane_foundation"):
        # Опалубка фундаментной плиты — только торцы (периметр × толщина).
        return _foundation_formwork_area(elements)
    def measured(el):
        if kind == 'stair' or el.component in ('flight', 'landing') and kind == 'slab':
            # TSN 2001.3-6 §2.8 uses horizontal projection, not skin area.
            key = 'gross_projection_area' if el.component == 'landing' else 'projection_area'
            return el.quantity(key) if key in el.quantities else None
        if kind == 'slab':
            if el.quantity('gross_area') > 0:
                return el.quantity('gross_area')
            return el.quantity('underside_area') if el.quantity('underside_area') > 0 else None
        if 'formwork_area' in el.quantities and not (kind in ('wall', 'parapet') and el.quantity_sources.get('formwork_area') == 'QTO_geom::Площадь_опалубки_м2'):
            return el.quantity('formwork_area')
        side, area, surface = (el.quantity(key) for key in ('side_area', 'area', 'surface_area'))
        if kind in ("wall", "parapet"):
            if side or surface:
                return side or surface
            if "side_area" in el.quantities:
                return side
            depth = el.geometry.get("thickness") or 0
            volume = el.quantity("volume")
            # Same one-face area fallback as zero_step for a wall without area QTO.
            return volume / (depth / 1000) if volume > 0 and depth > 0 else None
        if kind == "beam":
            return el.quantity('formwork_area') if 'formwork_area' in el.quantities else None
        if kind == "column":
            return surface or side if any(key in el.quantities for key in ("side_area", "surface_area")) else None
        return side or surface or area if any(key in el.quantities for key in ("side_area", "surface_area", "area")) else None
    values = [measured(el) for el in elements]
    known = [_model_quantity(el, value,
              (f'{value:g} (горизонтальная проекция, ТСН-2001.3-6 п. 2.8)'
               if el.component in ('flight', 'landing') and kind in ('stair', 'slab') else
               (f'{value:g} (GrossArea перекрытия)' if el.quantity('gross_area') > 0 else
                f'{value:g} (нижняя поверхность перекрытия по геометрии IFC, без торцов)') if kind == 'slab' else
               f'{el.quantity("underside_area"):g} + {el.quantity("edge_area"):g}'
               if 'underside_area' in el.quantities and 'edge_area' in el.quantities else
               f'{value:g} ({", ".join(el.quantity_sources.values())})'))
             for el, value in zip(elements, values) if value is not None and value >= 0]
    return sum(known) if known else None



def _norm_grade(grade: Optional[str]) -> Optional[str]:
    if not grade:
        return None
    return grade.upper().replace("В", "B").replace(" ", "")


def _concrete_volume(elements: List[Element], grade: Optional[str]) -> Optional[float]:
    """Объём бетона группы (при указании марки — только элементов этой марки)."""
    if not elements:
        return None
    if grade:
        target = _norm_grade(grade)
        matched = [el for el in elements if _norm_grade(el.concrete_grade) == target]
        if not matched:
            return None
        return sum(_model_quantity(el, el.quantity('volume')) for el in matched)
    return sum(_model_quantity(el, el.quantity('volume')) for el in elements)


_THERMO_THICKNESS_RE = re.compile(r"(\d{2,3})\s*мм")


def _thermoliner_volume(elements: List[Element]) -> Optional[float]:
    """Объём термовкладышей: площадь × толщина (из наименования элемента)."""
    if not elements:
        return None
    total = 0.0
    for el in elements:
        area = el.quantity("area")
        if not area:
            continue
        m = _THERMO_THICKNESS_RE.search(el.name or "")
        thickness_m = float(m.group(1)) / 1000.0 if m else 0.2
        total += area * thickness_m
    return total


# =====================================================================
#  Заполнение строк
# =====================================================================

class _Context:
    """Общий контекст заполнения (ссылки на источники, индекс, документы)."""

    def __init__(self, index: ElementIndex, docs: Documents, ifc_name: str):
        self.index = index
        self.docs = docs
        self.ifc_name = ifc_name
        self.vor_name = docs.file_names.get("vor")
        self.ref_ifc = f"Модель IFC: {ifc_name}" if ifc_name else "Модель IFC"
        self.ref_vor = f"ВОР застройщика: {self.vor_name}" if self.vor_name else "ВОР застройщика"

    def ref_doc(self, source: str, file_name: str = "") -> str:
        label = {"ВОР": "ВОР застройщика", "ПОС": "ПОС", "ПЗ": "ПЗ", "ПД": "ПД"}.get(source, source)
        return f"{label}: {file_name}" if file_name else label


def _fill_work(row: template.TemplateRow, elements: List[Element],
               agg: Dict[str, object], ctx: _Context,
               share: float = 1.0,
               thickness: Optional[int] = None,
               component: str = "") -> Tuple[Optional[float], Optional[str]]:
    """Вычисляет объём и обоснование для строки-работы (целочисленной).

    Приоритет источников: модель IFC → документы (ВОР/ПОС/ПЗ/ПД). Если данных
    нет ни в модели, ни в документах — объём не заполняется (прочерк).
    ``share`` — доля группы в общем объёме конструкций (при разбивке по
    геометрии); применяется к объёмам, распределяемым по разделу (арматура).
    ``thickness`` — толщина конструкции блока (мм) для точного отбора арматуры.
    ``component`` — подтип лестницы (``flight``/``landing``) для разделения
    работ маршей и площадок.
    """
    qk = mapping.classify_work(row.name)
    from .precast_quantities import count_factor
    if count_factor(row.unit) is not None and row.element_kind and row.element_kind.startswith('precast'):
        qk = 'count'
        from .precast_quantities import select_variant
        elements = select_variant(elements, row.name)
        agg = aggregate(elements)
    unit = (row.unit or "").lower()
    result: Tuple[Optional[float], Optional[str]] = (None, None)

    if qk == "rebar":
        # Арматура — исключительно из ВОР застройщика (по разделу ВОР).
        if ctx.vor_name:
            t = documents.rebar_thickness(ctx.docs.rebar, row.element_kind, row.part, thickness)
            total = ctx.docs.rebar.section_total(row.element_kind, row.part, t, component)
            if total > 0:
                return round(total, 2), ctx.ref_vor
        return None, None

    if elements:
        if qk == "formwork":
            area = _formwork_area(row.element_kind, agg, elements)
            missing = sum(_formwork_area(row.element_kind, aggregate([el]), [el]) is None for el in elements)
            if missing:
                note = 'Показана известная часть площади.' if area is not None else 'Площадь не рассчитана.'
                return (round(area, 2) if area is not None else None), f"{ctx.ref_ifc}. Неполные данные: нет площади у {missing} из {len(elements)} элементов. {note}"
            if area is not None and area >= 0:
                estimated = sum('formwork_area' not in el.quantities and not (el.quantity("side_area") or el.quantity("surface_area")) for el in elements) if row.element_kind == "wall" else 0
                wall_note = f". Для {estimated} элементов без площади QTO применено объём / толщина (одна сторона стены), как в расчёте по элементам." if estimated else ""
                foundation_note = ''
                if row.element_kind in ('foundation_slab', 'crane_foundation'):
                    if any(el.quantity_sources.get('perimeter', '').startswith('QTO_bbox') for el in elements):
                        foundation_note = '. Периметр оценён по габаритам IFC (bbox); площадь опалубки является оценкой.'
                    elif any(el.quantity_sources.get('perimeter', '').startswith('QTO_geom') for el in elements):
                        foundation_note = '. По внешнему контуру плит × толщина.'
                geometry_note = ''
                if row.element_kind not in ('wall', 'parapet') and any(el.quantity_sources.get('formwork_area') == 'QTO_geom::Площадь_опалубки_м2' for el in elements):
                    geometry_note = '. По граням IFC: стороны, торцы, откосы и низ перемычек; основание исключено.' if row.element_kind in ('wall', 'parapet') else '. По вертикальным граням IFC.' if row.element_kind == 'column' else '. По геометрии IFC: нижняя поверхность и вертикальные торцы.'
                if row.element_kind == 'slab':
                    fallback_count = sum(el.quantity('gross_area') <= 0 and el.quantity('underside_area') > 0 for el in elements)
                    geometry_note = (f'. Для {fallback_count} элементов без GrossArea взята площадь нижней поверхности по геометрии IFC, без торцов и верхней грани.' if fallback_count else '')
                stair_note = ''
                if row.element_kind == 'stair':
                    geometry_note = '. ТСН-2001.3-6, п. 2.8: горизонтальная проекция маршей; для площадок — проекция перекрытий без вычета отверстий. Вертикальные грани и наклонная площадь низа не прибавляются.'
                beam_note = ''
                if row.element_kind == 'beam' and any(el.quantity_sources.get('formwork_area') == 'QTO_geom::Площадь_опалубки_м2' for el in elements):
                    beam_note = '. Оценка площади поверхностей балки: низ и все вертикальные грани, включая торцы. Примыкания к стенам, плитам и опорам не исключены; это не подтверждённая площадь контакта с опалубкой.'
                    geometry_note = ''
                result = (round(area, 2), ctx.ref_ifc + wall_note + foundation_note + geometry_note + stair_note + beam_note)
        elif qk == "concrete":
            volume = _concrete_volume(elements, None)
            missing = sum('volume' not in el.quantities for el in elements)
            if missing:
                note = 'Показана известная часть объёма.' if volume else 'Объём не рассчитан.'
                return (round(volume, 2) if volume else None), f"{ctx.ref_ifc}. Неполные данные: нет объёма у {missing} из {len(elements)} элементов. {note}"
            if volume:
                result = (round(volume, 2), ctx.ref_ifc)
        elif qk == "thermoliner":
            # Термовкладыши относятся к плитам перекрытий; для других
            # подразделов (парапеты и т. п.) данных модели недостаточно.
            if row.element_kind == "slab":
                part = row.part if row.part in ("underground", "above") else None
                volume = _thermoliner_volume(ctx.index.select("thermoliner", part))
                if volume:
                    result = (round(volume, 2), ctx.ref_ifc)
        elif qk == "volume":
            if not unit.strip():
                return None, None  # A structural caption without a unit is not a measurable operation.
            volume = float(agg.get("volume") or 0.0)
            if volume:
                key = "volume" if agg.get("volume") else "gross_volume"
                traced = Quantity(volume, element_guids=[el.guid for el in elements if el.quantity(key)])
                result = (round(traced, 2), ctx.ref_ifc)
        elif qk == "hydro_area":
            area = float(agg.get("area") or 0.0)
            if area:
                traced = Quantity(area, element_guids=[el.guid for el in elements if el.quantity("area")])
                result = (round(traced, 2), ctx.ref_ifc)
        elif qk == "count" and count_factor(row.unit) is not None and row.element_kind:
            # Штучный объём — количество элементов конкретного типа конструкции;
            # без распознанного типа конструкции брать все элементы части здания
            # нельзя (иначе штучная работа получила бы число всех элементов).
            from .precast_quantities import count
            result = (count(elements, row.unit), ctx.ref_ifc + '. Подсчёт экземпляров изделий в модели.')

    if result[0] is not None:
        if row.element_kind == 'stair':
            flights = sum(el.component == 'flight' for el in elements)
            landings = sum(el.component == 'landing' for el in elements)
            result = (result[0], result[1] + f'. Состав выборки IFC: маршей {flights}, площадок {landings}. Родительские сборки не суммируются повторно.')
        return result

    # Данных модели нет (или работа не покрывается моделью) — ищем в документах.
    # ВОР застройщика в качестве источника объёмов не используется (из него
    # берётся только арматура) — см. §4.3.
    doc = ctx.docs.lookup(row.name, row.unit, exclude_sources=(_VOR_SOURCE,))
    if doc and doc.volume:
        return doc.volume, ctx.ref_doc(doc.source, doc.file_name)
    return None, None


def _fill_resource(row: template.TemplateRow, elements: List[Element],
                   agg: Dict[str, object], ctx: _Context,
                   share: float = 1.0,
                   thickness: Optional[int] = None,
                   component: str = "") -> Tuple[Optional[float], Optional[str]]:
    """Вычисляет объём и обоснование для строки-ресурса (дробной).

    Бетон — только из модели IFC (по марке); арматура — только из ВОР
    застройщика (по разделу и диаметру/классу); прочие материалы — из
    документов, кроме ВОР застройщика (из него берётся только арматура).
    ``component`` — подтип лестницы (``flight``/``landing``).
    """
    category = mapping.classify_resource(row.name)
    if elements and row.element_kind and row.element_kind.startswith('precast'):
        from .precast_quantities import select_variant, count, count_factor
        members = select_variant(elements, row.name)
        if count_factor(row.unit) is not None:
            return (count(members, row.unit), ctx.ref_ifc + '. Подсчёт экземпляров изделий в модели.') if members else (None, None)
        if row.unit.replace('³', '3').replace(' ', '').rstrip('.') == 'м3' and mapping.detect_kind(row.name) == row.element_kind:
            volume = _concrete_volume(members, None)
            return (round(volume, 2), ctx.ref_ifc) if volume is not None else (None, None)

    if category == "concrete":
        grade = mapping.parse_concrete_grade(row.name)
        volume = _concrete_volume(elements, grade)
        if volume:
            return round(volume, 2), ctx.ref_ifc
        return None, None

    if category == "rebar":
        # Арматура — исключительно из ВОР застройщика (по разделу ВОР).
        if not ctx.vor_name:
            return None, None
        diameter, rebar_class = mapping.parse_rebar_spec(row.name)
        t = documents.rebar_thickness(ctx.docs.rebar, row.element_kind, row.part, thickness)
        volume = ctx.docs.rebar.diameter_class(diameter, rebar_class,
                                               kind=row.element_kind, part=row.part,
                                               thickness=t, component=component)
        if volume <= 0 and diameter is None and rebar_class:
            volume = ctx.docs.rebar.diameter_class(None, rebar_class,
                                                   kind=row.element_kind, part=row.part,
                                                   thickness=t, component=component)
        if volume <= 0:
            return None, None
        return round(volume, 2), ctx.ref_vor

    # Прочие материалы — из документов, кроме ВОР застройщика (из него берётся
    # только арматура) — см. §4.3.
    doc = ctx.docs.lookup(row.name, row.unit, exclude_sources=(_VOR_SOURCE,))
    if doc and doc.volume:
        return doc.volume, ctx.ref_doc(doc.source, doc.file_name)
    return None, None


# =====================================================================
#  Динамические строки-ресурсы (арматура и бетон по фактическим данным)
# =====================================================================

def _concrete_specs(elements: List[Element]) -> List[Tuple[str, float]]:
    """Марки бетона элементов модели: ``[(«В30, W8, F150», объём), …]``.

    Порядок — по убыванию объёма (первой идёт преобладающая марка).
    """
    totals: Dict[str, float] = {}
    for el in elements:
        spec = el.concrete_spec or '(марка в IFC не указана)'
        totals[spec] = totals.get(spec, 0.0) + _model_quantity(el, el.quantity('volume'))
    return sorted(((spec, volume) for spec, volume in totals.items() if volume > 0),
                  key=lambda item: -item[1])


def _resource_sample(rows: List[template.TemplateRow], category: str) -> Optional[int]:
    """Строка-образец ресурса (для стилей динамических строк)."""
    for row in rows:
        if row.kind == "resource" and mapping.classify_resource(row.name) == category:
            return row.index
    for row in rows:
        if row.kind == "resource":
            return row.index
    return None


def _output_row(row: template.TemplateRow,
                fills: Dict[int, Tuple[Optional[float], Optional[str]]],
                label: Optional[str] = None) -> template.OutputRow:
    """Строка итогового ВОР по рассчитанным значениям."""
    volume, justification = fills.get(row.index, (None, None))
    return template.OutputRow(src_index=row.index, label=label,
                              volume=volume, justification=justification,
                              name_override=row.name if mapping.stair_component(row.name) == 'combined' else None)


def _fill_rows(rows: List[template.TemplateRow], elements: List[Element],
               ctx: _Context, kind: Optional[str] = None,
               part: Optional[str] = None, share: float = 1.0,
               thickness: Optional[int] = None, _banded=False) -> List[template.OutputRow]:
    """Заполняет строки (работы + ресурсы) одного блока/подраздела.

    Строки-работы арматуры («…стержней» / «…каркасов») и бетонирования получают
    **динамические** строки-ресурсы по фактическим данным: арматура — диаметры и
    классы из ВОР застройщика («Арматура класса …» → стержни, «Арматурные
    выпуски …» → отдельная работа выпусков), бетон — марки из модели IFC. Статические строки
    шаблона под этими работами заменяются; остальные строки заполняются как
    прежде (модель IFC → документы).

    Для лестничных подразделов (``stair``/``precast``) работы и ресурсы маршей
    и площадок заполняются **раздельно**: подтип строки (``flight``/``landing``)
    определяется по наименованию и наследуется последующими строками блока
    (работы арматуры после «опалубки маршей» относятся к маршам).

    ``share`` — доля группы в общем объёме конструкций (при разбивке по
    геометрии); ``thickness`` — толщина конструкции блока (мм).
    """
    from .stair_grouping import needs_bands, source_band, element_band, band_heading, band_rebar, adapt_template
    source_rows = documents.customer_rows(ctx.docs)
    scoped_source = [row for row in source_rows if row.kind == kind and (not part or not row.part or row.part == part)]
    if not _banded and rows and needs_bands(source_rows, kind, part):
        from dataclasses import replace
        bands = {source_band(row.section) for row in scoped_source} | {element_band(el) for el in elements}
        output = []
        for band in ('first', 'upper', 'general'):
            if band not in bands:
                continue
            members = [el for el in elements if element_band(el) == band]
            band_source = [row for row in scoped_source if source_band(row.section) == band]
            band_docs = replace(ctx.docs, rebar=band_rebar(ctx.docs.rebar, band),
                                vor_rows=[row for row in ctx.docs.vor_rows if source_band(row.get('section')) == band],
                                prepared_customer_rows=band_source)
            output.append(band_heading(rows, band))
            output.extend(_fill_rows(adapt_template(rows, band_source), members,
                                     _Context(ctx.index, band_docs, ctx.ifc_name), kind, part, share, thickness, _banded=True))
        return output
    agg = aggregate(elements)
    buckets = documents.rebar_buckets(ctx.docs.rebar, kind, part, thickness, share)
    rows = template.with_release_work(rows, buckets)
    if kind in ('insulation', 'seam') and ctx.docs.vor_rows:
        from . import customer_vor
        source = [r for r in documents.customer_rows(ctx.docs)
                  if customer_vor._in_scope(r, kind, part)]
        return customer_vor._fill_rows(rows, source, [],
                                      customer_vor._Context(ctx.docs.rebar, ctx.ref_vor),
                                      kind, part, 1.0, thickness)

    # Подтип лестницы для каждой строки блока (с наследованием в пределах блока).
    row_component: Dict[int, str] = {}
    if kind in ("stair", "precast"):
        current = ""
        for row in rows:
            comp = mapping.stair_component(row.name)
            if comp:
                current = comp
            row_component[row.index] = current

    def component_of(row: template.TemplateRow) -> str:
        return row_component.get(row.index, "")

    # Подмножества элементов по подтипу лестницы (кэш на блок).
    subsets: Dict[str, Tuple[List[Element], Dict[str, object]]] = {"": (elements, agg)}

    def subset(component: str) -> Tuple[List[Element], Dict[str, object]]:
        if component == 'combined':
            return elements, agg
        if component not in subsets:
            if any(el.component for el in elements):
                sub = [el for el in elements if el.component == component]
            else:
                sub = []  # An unidentified assembly cannot be both a flight and a landing.
            subsets[component] = (sub, aggregate(sub))
        return subsets[component]

    rebar_works = [r for r in rows if r.kind == "work" and mapping.classify_work(r.name) == "rebar"]
    has_bars_work = any(mapping.rebar_bucket(r.name) == "bars" for r in rebar_works)
    has_frames_work = any(mapping.rebar_bucket(r.name) == "frames" for r in rebar_works)

    fills: Dict[int, Tuple[Optional[float], Optional[str]]] = {}
    for row in rows:
        component = component_of(row)
        sub_elements, sub_agg = subset(component)
        if row.kind == "empty":
            fills[row.index] = (None, None)
        elif row.kind == "resource":
            fills[row.index] = _fill_resource(row, sub_elements, sub_agg, ctx, share,
                                              thickness, component)
        else:
            fills[row.index] = _fill_work(row, sub_elements, sub_agg, ctx, share,
                                          thickness, component)

    # Бетон — только из модели: объём марки заполняется в первую строку этой
    # марки, повторные строки той же марки остаются с прочерком.
    # Неизвестная марка не подставляется под произвольный бетон шаблона.
    concrete_rows = [r for r in rows
                     if r.kind == "resource" and mapping.classify_resource(r.name) == "concrete"]
    used_grades = set()
    any_filled = False
    for r in concrete_rows:
        grade = _norm_grade(mapping.parse_concrete_grade(r.name))
        if grade and grade in used_grades:
            fills[r.index] = (None, None)
            continue
        sub_elements, sub_agg = subset(component_of(r))
        volume = _concrete_volume(sub_elements, grade)
        if volume:
            fills[r.index] = (round(volume, 2), ctx.ref_ifc)
            used_grades.add(grade)
            any_filled = True
        else:
            fills[r.index] = (None, None)

    # Арматура: целочисленная строка = полный тоннаж раздела из ВОР
    # застройщика (в доле группы при разбивке по геометрии); если разрез
    # раздела пуст — сумма найденных ресурсов арматуры соответствующей работы.
    rebar_work = [r for r in rows
                  if r.kind == "work" and mapping.classify_work(r.name) == "rebar"]
    rebar_resources = [r for r in rows
                       if r.kind == "resource" and mapping.classify_resource(r.name) == "rebar"]
    for work in rebar_work:
        if fills[work.index][0]:
            continue
        prefix = str(work.number).strip() + "."
        related = [r for r in rebar_resources if str(r.number).strip().startswith(prefix)]
        resource_sum = sum(fills[r.index][0] or 0.0 for r in related)
        if resource_sum > 0:
            fills[work.index] = (round(resource_sum, 2), ctx.ref_vor)

    # Строки-ресурсы арматуры, не относящиеся к работам армирования («Устройство
    # закладных деталей» и т. п.), не заполняются из общего тоннажа раздела —
    # иначе объём арматуры удваивался бы с работами «…стержней»/«…каркасов».
    work_by_number = {str(r.number).strip(): r for r in rows if r.kind == "work"}
    for r in rebar_resources:
        parent = work_by_number.get(str(r.number).strip().split(".", 1)[0])
        if parent is None or mapping.classify_work(parent.name) != "rebar":
            fills[r.index] = (None, None)

    # --- Эмиссия строк: подмена статических строк-ресурсов динамическими ---
    output: List[template.OutputRow] = []
    i = 0
    total_rows = len(rows)
    while i < total_rows:
        row = rows[i]
        children: List[template.TemplateRow] = []
        j = i + 1
        while j < total_rows and rows[j].kind in ("resource", "variant", "empty"):
            children.append(rows[j])
            j += 1

        items: Dict = {}
        category = ""
        dynamic = False
        if row.kind == "work":
            qk = mapping.classify_work(row.name)
            component = component_of(row)
            row_buckets = documents.rebar_buckets(ctx.docs.rebar, kind, part, thickness,
                                                  share, component) if component else buckets
            if qk == "rebar" and any(row_buckets.values()):
                items = documents.rebar_work_items(row_buckets, row.name,
                                                   has_bars_work, has_frames_work)
                category = "rebar"
                dynamic = True
            elif qk == "concrete":
                concrete_specs = _concrete_specs(subset(component)[0])
                if concrete_specs:
                    items = dict(concrete_specs)
                    category = "concrete"
                    dynamic = True

        if not dynamic:
            output.append(_output_row(row, fills))
            i += 1
            continue

        ref = ctx.ref_vor if category == "rebar" else ctx.ref_ifc
        output.append(template.OutputRow(src_index=row.index,
                                         label=row.number if mapping.rebar_bucket(row.name) == 'releases' else None,
                                         name_override=row.name if mapping.stair_component(row.name) == 'combined' or mapping.rebar_bucket(row.name) == 'releases' else None,
                                         volume=round(sum(items.values()), 2) if items else None,
                                         justification=ref if items else None))
        sample = _resource_sample(children, category) or row.index
        ordered = sorted(items.items(), key=documents.rebar_sort_key) if category == "rebar" \
            else list(items.items())
        number = 0
        for key, volume in ordered:
            number += 1
            name = documents.rebar_label(*key) if category == "rebar" \
                else mapping.concrete_label(key)
            output.append(template.OutputRow(
                src_index=sample, label=f"{row.number}.{number}", name_override=name,
                volume=round(volume, 2), justification=ref))
        for child in children:
            if child.kind == "resource" and mapping.classify_resource(child.name) == category:
                continue  # заменена динамической строкой
            label = None
            if child.kind == "resource":
                number += 1
                label = f"{row.number}.{number}"
            output.append(_output_row(child, fills, label))
        i = j

    # Keep a measured assembly total once when the model does not identify
    # its flights and landings. Never copy that volume into both operations.
    if kind == 'stair':
        unidentified = [el for el in elements if not el.component]
        concrete = next((row for row in rows if row.kind == 'work'
                         and mapping.classify_work(row.name) == 'concrete'), None)
        if unidentified and concrete and any(el.quantity('volume') for el in unidentified):
            value, reference = _fill_work(concrete, unidentified, aggregate(unidentified), ctx, share, thickness, 'combined')
            output.append(template.OutputRow(
                src_index=concrete.index, label=concrete.number,
                name_override='Бетонирование монолитных железобетонных конструкций лестничных маршей и площадок',
                volume=value, justification=(reference or ctx.ref_ifc) + '. В IFC нет разделения объёма на марши и площадки; показан общий объём без распределения.'))
    from .stair_grouping import mixed_rows
    output.extend(mixed_rows(rows, documents.customer_rows(ctx.docs), kind, part, ctx.ref_ifc, generated=True))
    from .estimate_parameters import fill
    fill(output, rows, elements)
    from .thickness import fill_work_names
    fill_work_names(output, rows, thickness)
    return output


# =====================================================================
#  Группировка по геометрии
# =====================================================================

def _geometry_groups(elements: List[Element], param: str,
                     index: ElementIndex) -> List[Tuple[str, List[Element]]]:
    """Группирует элементы по геометрическому параметру (толщина/сечение/площадь)."""
    groups: Dict[str, List[Element]] = {}
    for el in elements:
        label = index.geometry_label(el, param) or ""
        groups.setdefault(label, []).append(el)

    def sort_key(item):
        label = item[0]
        if not label:
            return (1, 0.0, "")
        m = re.match(r"\d+(?:[.,]\d+)?", label)
        if m:
            return (0, float(m.group(0).replace(",", ".")), label)
        return (0, 0.0, label)

    return sorted(groups.items(), key=sort_key)


# =====================================================================
#  Основная сборка
# =====================================================================

def build_vor(elements_json_path: str, docs: Documents, output_path: str,
              template_path: str = None, sheet_name: str = template.DEFAULT_SHEET,
              ifc_name: str = "", progress: Progress = None,
              prepared_index=None, prepared_template=None) -> Dict[str, object]:
    """Собирает заполненный ВОР по типовому шаблону.

    Возвращает словарь со статистикой сборки (число строк, заполненных объёмов
    и т. п.) — для отображения в интерфейсе.
    """
    template_path = template_path or template.DEFAULT_TEMPLATE_PATH
    _say(progress, "Разбор типового шаблона ВОР")
    rows = prepared_template if prepared_template is not None else template.parse_template(template_path, sheet_name)
    _say(progress, "Индексация элементов модели IFC")
    index = prepared_index
    if index is None:
        index = ElementIndex.from_raw_dump(elements_json_path)
        index.apply_vor_foundation_changes(docs.vor_rows)
    from .stair_grouping import adapt_template, route_landings
    source_rows = documents.customer_rows(docs)
    route_landings(index, source_rows)
    rows = adapt_template(rows, source_rows)
    from dataclasses import replace
    calculation_docs = replace(docs, rebar=documents.RebarData())
    ctx = _Context(index, calculation_docs, ifc_name)

    by_index = {r.index: r for r in rows}
    pos_by_index = {r.index: k for k, r in enumerate(rows)}

    # Заголовки и диапазоны блоков (блок = строки после геометрического заголовка
    # до следующего заголовка любого уровня).
    header_indices = [r.index for r in rows if r.kind in _HEADER_KINDS]
    block_headers = [r for r in rows if r.kind == "block"]
    block_rows: Dict[int, List[template.TemplateRow]] = {}
    block_end: Dict[int, int] = {}
    for header in block_headers:
        later = [i for i in header_indices if i > header.index]
        end = (later[0] - 1) if later else rows[-1].index
        block_end[header.index] = end
        block_rows[header.index] = [by_index[i] for i in range(header.index + 1, end + 1)
                                    if i in by_index and by_index[i].kind in ("work", "resource", "variant", "empty")]

    output: List[template.OutputRow] = []
    filled = 0
    i = 0
    total_rows = len(rows)

    while i < total_rows:
        row = rows[i]

        if row.parking:
            # Часть «Автостоянка»: строки раздела всегда переносятся в ВОР,
            # но не заполняются — если в проекте автостоянки нет, раздел
            # остаётся пустым (объёмы по нему не рассчитываются).
            output.append(template.OutputRow(src_index=row.index))
            i += 1
            continue

        if row.kind == "block":
            part = row.part if row.part in ("underground", "above") else None
            elements = index.select(row.element_kind, part)
            children = block_rows.get(row.index, [])
            # Группировка по геометрии — только для толщины и сечения
            # (заголовки «…толщиной … мм» / «…сечением ахb мм»); «площадью … м2»
            # заполняется одним блоком по всей группе подраздела.
            if row.geometry in ("thickness", "section") and elements:
                groups = _geometry_groups(elements, row.geometry, index)
                total_volume = sum(el.quantity("volume") for el in elements)
                for label, group_elements in groups:
                    header_label = _group_header(group_elements, row.geometry, label,
                                                 _block_text(row))
                    group_volume = sum(el.quantity("volume") for el in group_elements)
                    share = (group_volume / total_volume) if total_volume > 0 else 1.0
                    thickness = _geometry_int(label) if row.geometry == "thickness" else None
                    output.append(template.OutputRow(src_index=row.index, label=header_label))
                    for out_row in _fill_rows(children, group_elements, ctx,
                                              row.element_kind, part, share, thickness):
                        if out_row.volume is not None:
                            filled += 1
                        output.append(out_row)
            else:
                label = None
                if row.geometry == "area" and elements:
                    total_area = sum(el.quantity("area") for el in elements)
                    if total_area:
                        label = f"{round(total_area, 2)}"
                header_label = _format_header(_block_text(row), row.geometry, label) \
                    if row.geometry else _block_text(row)
                output.append(template.OutputRow(src_index=row.index, label=header_label))
                for out_row in _fill_rows(children, elements, ctx, row.element_kind, part):
                    if out_row.volume is not None:
                        filled += 1
                    output.append(out_row)
            if row.element_kind == 'foundation_slab':
                crane_elements = index.select('crane_foundation', part)
                crane_groups = _geometry_groups(crane_elements, 'thickness', index)
                total = sum(el.quantity('volume') for el in crane_elements)
                for label, members in crane_groups:
                    heading = _group_header(members, 'thickness', label, 'Фундамент под башенный кран толщиной … мм')
                    if 'кран' not in heading.lower():
                        heading = 'Фундамент башенного крана: ' + heading
                    output.append(template.OutputRow(src_index=row.index,
                        label=heading))
                    share = sum(el.quantity('volume') for el in members) / total if total else 1.0
                    added = _fill_rows(template.with_kind(children, 'crane_foundation'), members, ctx,
                                       'crane_foundation', part, share, _geometry_int(label))
                    filled += sum(r.volume is not None for r in added)
                    output.extend(added)
            i = pos_by_index[block_end[row.index]] + 1
            continue

        if row.kind in ("work", "resource", "variant"):
            # Группа подряд идущих строк (работы + ресурсы) одного подраздела
            # заполняется вместе: целочисленные строки получают сумму своих
            # ресурсов, объёмы не дублируются между строками подраздела.
            group = []
            while i < total_rows and rows[i].kind in ("work", "resource", "variant"):
                group.append(rows[i])
                i += 1
            part = group[0].part if group[0].part in ("underground", "above") else None
            elements = index.select(group[0].element_kind, part)
            for out_row in _fill_rows(group, elements, ctx, group[0].element_kind, part):
                if out_row.volume is not None:
                    filled += 1
                output.append(out_row)
            continue

        # Части здания, разделы, подразделы, пустые строки — копируем как есть.
        output.append(template.OutputRow(src_index=row.index))
        i += 1

    from .source_rebar import copy_rebar
    copy_rebar(output, rows, docs.rebar, ctx.ref_vor)
    filled = sum(item.volume is not None for item in output)
    _say(progress, "Запись заполненного ВОР")
    from .stair_report import records as stair_records
    apply_mix_norms(output, rows)
    template.render_template(output, path=template_path, sheet_name=sheet_name,
                             output_path=output_path, stair_rows=stair_records(index))

    stats = {
        "rows": len(output),
        "filled": filled,
        "elements": len(index.elements),
        "rebar_total": round(docs.rebar.total, 2),
        "rebar_rows": len(docs.rebar.rows),
        "documents": docs.file_names,
        "output": os.path.basename(output_path),
    }
    logger.info("ВОР сформирован: строк %d, заполнено объёмов %d", len(output), filled)
    return stats


def apply_mix_norms(output, rows):
    """Apply mixture allowance only to generated resource rows, once."""
    from ..works_cost import typical_mix_factor
    by_index = {row.index: row for row in rows}
    factors = {}
    parent = ''
    for item in output:
        source = by_index[item.src_index]
        name = item.name_override or source.name
        if source.kind in ('part', 'section', 'subsection', 'block'):
            parent = ''
            continue
        dynamic_resource = bool(item.label and '.' in item.label and item.name_override)
        if source.kind == 'work' and not dynamic_resource:
            parent = name
            continue
        if item.volume is None or source.kind != 'resource' and not dynamic_resource:
            continue
        if source.element_kind and source.element_kind.startswith('precast'):
            continue
        if mapping.classify_resource(name) != 'concrete':
            continue
        if 'koefs.xlsx:' in (item.justification or ''):
            continue
        preparation = bool(re.search(r'подготов|подстилающ', parent, re.I))
        if preparation not in factors:
            factors[preparation] = typical_mix_factor(preparation)
        factor, rates = factors[preparation]
        if factor is None:
            item.justification = (item.justification or '') + '; koefs.xlsx: однозначная норма расхода не найдена; требуется проверка'
            continue
        base = item.volume
        item.volume = round(base * factor, 2)
        item.justification = (item.justification or '') + f'; koefs.xlsx: {rates}; объём бетонирования × {factor:g} = {float(item.volume):g} м³'
