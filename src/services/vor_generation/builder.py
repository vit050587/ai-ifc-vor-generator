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
        return name
    return _format_header(fallback_text, param, label)


# =====================================================================
#  Расчёт объёмов
# =====================================================================

def _foundation_formwork_area(elements: List[Element]) -> float:
    """Площадь опалубки фундаментной плиты — только торцы (периметр × толщина).

    Фундаментная плита бетонируется на бетонную подготовку, поэтому опалубка
    устраивается только по боковой поверхности (торцам): периметр контура ×
    толщину плиты. Площади нижней и верхней поверхностей в опалубку не входят.
    Периметр берётся из QTO, а при его отсутствии — из bbox-оценки сырого дампа
    (``QTO_bbox::Периметр_мм``); толщина — из QTO или наименования группы.
    """
    total = 0.0
    for el in elements:
        perimeter = el.quantity("perimeter")       # мм
        thickness = el.geometry.get("thickness")   # мм
        if perimeter and thickness:
            total += (perimeter / 1000.0) * (thickness / 1000.0)
    return total


def _formwork_area(kind: Optional[str], agg: Dict[str, object],
                   elements: List[Element]) -> float:
    """Площадь опалубки по агрегатам группы элементов."""
    if kind == "foundation_slab":
        # Опалубка фундаментной плиты — только торцы (периметр × толщина).
        return _foundation_formwork_area(elements)
    side = float(agg.get("side_area") or 0.0)
    area = float(agg.get("area") or 0.0)
    surface = float(agg.get("surface_area") or 0.0)
    if kind == "wall":
        return side or surface
    if kind in ("slab", "stair"):
        return area or surface
    if kind in ("beam", "column"):
        return surface or side
    return side or surface or area


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
        return sum(el.quantity("volume") for el in matched)
    return sum(el.quantity("volume") for el in elements)


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
    unit = (row.unit or "").lower()
    result: Tuple[Optional[float], Optional[str]] = (None, None)

    if qk == "rebar":
        # Арматура — исключительно из ВОР застройщика (по разделу ВОР).
        if ctx.vor_name:
            t = documents.rebar_thickness(ctx.docs.rebar, row.element_kind, row.part, thickness)
            total = ctx.docs.rebar.section_total(row.element_kind, row.part, t, component) * share
            if total > 0:
                return round(total, 2), ctx.ref_vor
        return None, None

    if elements:
        if qk == "formwork":
            area = _formwork_area(row.element_kind, agg, elements)
            if area > 0:
                result = (round(area, 2), ctx.ref_ifc)
        elif qk == "concrete":
            volume = _concrete_volume(elements, None)
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
            volume = float(agg.get("volume") or agg.get("gross_volume") or 0.0)
            if volume:
                result = (round(volume, 2), ctx.ref_ifc)
        elif qk == "hydro_area":
            area = float(agg.get("area") or 0.0)
            if area:
                result = (round(area, 2), ctx.ref_ifc)
        elif qk == "count" and "шт" in unit and row.element_kind:
            # Штучный объём — количество элементов конкретного типа конструкции;
            # без распознанного типа конструкции брать все элементы части здания
            # нельзя (иначе штучная работа получила бы число всех элементов).
            count = float(agg.get("count") or 0)
            if count:
                result = (count, ctx.ref_ifc)

    if result[0] is not None:
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
                                               thickness=t, component=component) * share
        if volume <= 0 and diameter is None and rebar_class:
            volume = ctx.docs.rebar.diameter_class(None, rebar_class,
                                                   kind=row.element_kind, part=row.part,
                                                   thickness=t, component=component) * share
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
        if not el.concrete_spec:
            continue
        totals[el.concrete_spec] = totals.get(el.concrete_spec, 0.0) + el.quantity("volume")
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
                              volume=volume, justification=justification)


def _fill_rows(rows: List[template.TemplateRow], elements: List[Element],
               ctx: _Context, kind: Optional[str] = None,
               part: Optional[str] = None, share: float = 1.0,
               thickness: Optional[int] = None) -> List[template.OutputRow]:
    """Заполняет строки (работы + ресурсы) одного блока/подраздела.

    Строки-работы арматуры («…стержней» / «…каркасов») и бетонирования получают
    **динамические** строки-ресурсы по фактическим данным: арматура — диаметры и
    классы из ВОР застройщика («Арматура класса …» → стержни, «Арматурные
    выпуски …» → каркасы), бетон — марки из модели IFC. Статические строки
    шаблона под этими работами заменяются; остальные строки заполняются как
    прежде (модель IFC → документы).

    Для лестничных подразделов (``stair``/``precast``) работы и ресурсы маршей
    и площадок заполняются **раздельно**: подтип строки (``flight``/``landing``)
    определяется по наименованию и наследуется последующими строками блока
    (работы арматуры после «опалубки маршей» относятся к маршам).

    ``share`` — доля группы в общем объёме конструкций (при разбивке по
    геометрии); ``thickness`` — толщина конструкции блока (мм).
    """
    agg = aggregate(elements)
    buckets = documents.rebar_buckets(ctx.docs.rebar, kind, part, thickness, share)

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
        if component not in subsets:
            if any(el.component for el in elements):
                sub = [el for el in elements if el.component == component]
            else:
                sub = elements
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
    # марки, повторные строки той же марки остаются с прочерком. Если ни одна
    # марка не совпала — весь объём бетона группы в первую строку бетона.
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
    for r in concrete_rows:
        if fills[r.index][0]:
            continue
        sub_elements, sub_agg = subset(component_of(r))
        total_concrete = float(sub_agg.get("volume") or 0.0)
        # Марки бетона в модели не указаны — показываем объём в первой строке,
        # чтобы объём бетона не потерялся (иначе — прочерк).
        if total_concrete > 0 and not any(_norm_grade(el.concrete_grade) for el in sub_elements):
            fills[r.index] = (round(total_concrete, 2), ctx.ref_ifc)
            break

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
            if qk == "rebar" and (row_buckets.get("bars") or row_buckets.get("frames")):
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
              ifc_name: str = "", progress: Progress = None) -> Dict[str, object]:
    """Собирает заполненный ВОР по типовому шаблону.

    Возвращает словарь со статистикой сборки (число строк, заполненных объёмов
    и т. п.) — для отображения в интерфейсе.
    """
    template_path = template_path or template.DEFAULT_TEMPLATE_PATH
    _say(progress, "Разбор типового шаблона ВОР")
    rows = template.parse_template(template_path, sheet_name)
    _say(progress, "Индексация элементов модели IFC")
    index = ElementIndex.from_raw_dump(elements_json_path)
    ctx = _Context(index, docs, ifc_name)

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

    _say(progress, "Запись заполненного ВОР")
    template.render_template(output, path=template_path, sheet_name=sheet_name,
                             output_path=output_path)

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
