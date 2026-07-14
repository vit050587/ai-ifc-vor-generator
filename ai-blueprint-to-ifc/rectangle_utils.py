import math
from typing import Any
from tqdm import tqdm
from joblib import Memory

memory = Memory("cache", verbose=0)

def rectangles_to_yolo_obb(
    rectangles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Приводит обычные YOLO bbox и готовые OBB к единому формату.

    Метаданные детекции сохраняются, а поле bbox всегда содержит
    четыре точки: x1, y1 ... x4, y4.
    """
    obb_rectangles: list[dict[str, Any]] = []

    for rectangle in rectangles:
        bbox = rectangle.get("bbox", rectangle)

        try:
            if all(
                coordinate in bbox
                for coordinate in (
                    "x1", "y1", "x2", "y2",
                    "x3", "y3", "x4", "y4",
                )
            ):
                obb = {
                    f"{axis}{index}": float(bbox[f"{axis}{index}"])
                    for index in range(1, 5)
                    for axis in ("x", "y")
                }
            else:
                left = min(float(bbox["x1"]), float(bbox["x2"]))
                top = min(float(bbox["y1"]), float(bbox["y2"]))
                right = max(float(bbox["x1"]), float(bbox["x2"]))
                bottom = max(float(bbox["y1"]), float(bbox["y2"]))
                obb = {
                    "x1": left,
                    "y1": top,
                    "x2": right,
                    "y2": top,
                    "x3": right,
                    "y3": bottom,
                    "x4": left,
                    "y4": bottom,
                }
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Прямоугольник должен содержать числовые x1, y1, x2, y2"
            ) from exc

        if "bbox" in rectangle:
            converted_rectangle = dict(rectangle)
            converted_rectangle["bbox"] = obb
            obb_rectangles.append(converted_rectangle)
        else:
            obb_rectangles.append(obb)

    return obb_rectangles


def _get_obb_points(bbox: dict[str, Any]) -> list[tuple[float, float]]:
    """Возвращает четыре точки OBB в порядке, заданном моделью."""
    try:
        return [
            (float(bbox[f"x{i}"]), float(bbox[f"y{i}"]))
            for i in range(1, 5)
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "OBB должен содержать числовые координаты x1, y1 ... x4, y4"
        ) from exc

def get_obb_dimensions(
    bbox: dict[str, Any],
) -> tuple[float, float, float]:
    """Возвращает длину, толщину и угол длинной стороны OBB."""
    points = _get_obb_points(bbox)
    edges = []

    for index in range(4):
        start = points[index]
        end = points[(index + 1) % 4]
        edge_length = math.hypot(end[0] - start[0], end[1] - start[1])
        edges.append((edge_length, start, end))

    long_edge = max(edges, key=lambda edge: edge[0])
    short_edge = min(edges, key=lambda edge: edge[0])
    angle = math.degrees(
        math.atan2(
            long_edge[2][1] - long_edge[1][1],
            long_edge[2][0] - long_edge[1][0],
        )
    ) % 180

    return long_edge[0], short_edge[0], angle

@memory.cache
def remove_small_area_walls(
    rectangles: list[dict[str, Any]],
    min_area: float,
) -> list[dict[str, Any]]:
    """
    Удаляет стены, площадь OBB которых меньше ``min_area``.
    """
    min_area = float(min_area)
    if min_area < 0:
        raise ValueError("min_area должен быть неотрицательным")

    obb_rectangles = rectangles_to_yolo_obb(rectangles)
    filtered = []

    for rectangle in obb_rectangles:
        bbox = rectangle.get("bbox", rectangle)
        area_bbox = {
            f"{axis}{point_index}": int(float(bbox[f"{axis}{point_index}"]))
            for point_index in range(1, 5)
            for axis in ("x", "y")
        }
        if _obb_area(area_bbox) < min_area:
            continue

        if "bbox" in rectangle:
            filtered.append(_copy_rectangle(rectangle))
        else:
            filtered.append(dict(rectangle))

    return filtered

@memory.cache
def merge_overlapping_obb(
    rectangles: list[dict[str, Any]],
    overlap_threshold: float = 0.5,
    angle_threshold_degrees: float = 10.0,
    edge_similarity_threshold: float = 0.5,
) -> list[dict[str, Any]]:
    """
    Объединяет похожие перекрывающиеся OBB-прямоугольники.

    overlap_threshold:
        Минимальная доля площади хотя бы одного прямоугольника, перекрытая
        другим. Например, 0.5 означает 50%. Поэтому пара объединится, если
        intersection / area(first) ИЛИ intersection / area(second) >= 0.5.
    angle_threshold_degrees:
        Максимальная разница направления длинных граней.
    edge_similarity_threshold:
        Минимальное отношение меньшей одноименной грани к большей.
        При 0.5 длина и толщина прямоугольников могут различаться не более
        чем в два раза.

    Вход и выход совпадают с форматом ``get_walls_cords``. Объединение
    повторяется, пока находятся подходящие пары.
    """
    overlap_threshold = _as_ratio(overlap_threshold, "overlap_threshold")
    edge_similarity_threshold = _as_ratio(
        edge_similarity_threshold,
        "edge_similarity_threshold",
    )
    if not 0 <= angle_threshold_degrees <= 90:
        raise ValueError(
            "angle_threshold_degrees должен быть в диапазоне от 0 до 90"
        )

    merged = rectangles_to_yolo_obb(rectangles)
    merged = [_copy_rectangle(rectangle) for rectangle in merged]

    pass_number = 1
    while True:
        candidates = []

        # Во время анализа прохода список и геометрия не изменяются.
        for first_index in tqdm(
            range(len(merged)),
            desc=f"Анализ объединений, проход {pass_number}",
            unit="rect",
        ):
            for second_index in range(first_index + 1, len(merged)):
                score = _get_merge_candidate_score(
                    merged[first_index]["bbox"],
                    merged[second_index]["bbox"],
                    overlap_threshold,
                    angle_threshold_degrees,
                    edge_similarity_threshold,
                )
                if score is not None:
                    candidates.append({
                        "first_index": first_index,
                        "second_index": second_index,
                        "score": score,
                    })

        if not candidates:
            break

        # Сначала выбираем наиболее релевантные непересекающиеся пары.
        candidates.sort(
            key=lambda candidate: (
                -candidate["score"],
                candidate["first_index"],
                candidate["second_index"],
            )
        )
        used_indexes = set()
        selected_pairs = []

        for candidate in candidates:
            first_index = candidate["first_index"]
            second_index = candidate["second_index"]
            if (
                first_index in used_indexes
                or second_index in used_indexes
            ):
                continue

            selected_pairs.append((first_index, second_index))
            used_indexes.update((first_index, second_index))

        if not selected_pairs:
            break

        # Формируем новый список только после завершения полного анализа.
        pair_by_first = {
            first_index: second_index
            for first_index, second_index in selected_pairs
        }
        second_indexes = {
            second_index
            for _, second_index in selected_pairs
        }
        next_merged = []

        for index, rectangle in enumerate(merged):
            if index in second_indexes:
                continue
            if index in pair_by_first:
                next_merged.append(
                    _merge_rectangle_pair(
                        rectangle,
                        merged[pair_by_first[index]],
                    )
                )
            else:
                next_merged.append(rectangle)

        merged = next_merged
        pass_number += 1

    return merged

@memory.cache
def trim_overlapping_obb(
    rectangles: list[dict[str, Any]],
    overlap_threshold: float = 0.0,
) -> list[dict[str, Any]]:
    """
    Устраняет пересечения OBB, обрезая один прямоугольник в каждой паре.

    Для обоих прямоугольников рассчитывается лучший прямоугольный остаток,
    не пересекающий второй OBB. Обрезается тот объект, который потеряет
    меньшую долю своей площади. Обработка выполняется пакетными проходами:
    геометрия не меняется до завершения анализа всех пар.

    overlap_threshold задаёт минимальную долю перекрытия хотя бы одного
    прямоугольника. Можно передать 0.05 или 5 для пяти процентов.
    Полностью поглощённый прямоугольник может быть удалён из результата.
    """
    overlap_threshold = _as_ratio(
        overlap_threshold,
        "overlap_threshold",
    )
    trimmed = rectangles_to_yolo_obb(rectangles)
    trimmed = [_copy_rectangle(rectangle) for rectangle in trimmed]
    pass_number = 1

    while True:
        candidates = []

        for first_index in tqdm(
            range(len(trimmed)),
            desc=f"Анализ обрезки, проход {pass_number}",
            unit="rect",
        ):
            for second_index in range(first_index + 1, len(trimmed)):
                candidate = _get_trim_pair_candidate(
                    trimmed[first_index],
                    trimmed[second_index],
                    first_index,
                    second_index,
                    overlap_threshold,
                )
                if candidate is not None:
                    candidates.append(candidate)

        if not candidates:
            break

        # Сначала применяются варианты с минимальной относительной потерей.
        candidates.sort(
            key=lambda candidate: (
                candidate["loss_ratio"],
                candidate["target_index"],
                candidate["blocker_index"],
            )
        )
        used_indexes = set()
        selected = []

        for candidate in candidates:
            target_index = candidate["target_index"]
            blocker_index = candidate["blocker_index"]
            if (
                target_index in used_indexes
                or blocker_index in used_indexes
            ):
                continue
            selected.append(candidate)
            used_indexes.update((target_index, blocker_index))

        if not selected:
            break

        changes_by_index = {
            candidate["target_index"]: candidate
            for candidate in selected
        }
        next_trimmed = []

        for index, rectangle in enumerate(trimmed):
            candidate = changes_by_index.get(index)
            if candidate is None:
                next_trimmed.append(rectangle)
                continue

            new_bbox = candidate["bbox"]
            if new_bbox is None:
                continue

            changed_rectangle = dict(rectangle)
            changed_rectangle["bbox"] = new_bbox
            changed_rectangle["trimmed_count"] = (
                int(rectangle.get("trimmed_count", 0)) + 1
            )
            changed_rectangle["remaining_area_ratio"] = (
                float(rectangle.get("remaining_area_ratio", 1.0))
                * (1 - candidate["loss_ratio"])
            )
            next_trimmed.append(changed_rectangle)

        trimmed = next_trimmed
        pass_number += 1

    return trimmed


def _get_trim_pair_candidate(
    first: dict[str, Any],
    second: dict[str, Any],
    first_index: int,
    second_index: int,
    overlap_threshold: float,
) -> dict[str, Any] | None:
    first_polygon = _ordered_polygon(_get_obb_points(first["bbox"]))
    second_polygon = _ordered_polygon(_get_obb_points(second["bbox"]))
    first_area = _polygon_area(first_polygon)
    second_area = _polygon_area(second_polygon)

    if first_area <= 0 or second_area <= 0:
        return None

    intersection = _clip_convex_polygon(first_polygon, second_polygon)
    intersection_area = _polygon_area(intersection)
    if intersection_area <= 1e-9:
        return None

    overlap_ratio = max(
        intersection_area / first_area,
        intersection_area / second_area,
    )
    if overlap_ratio < overlap_threshold:
        return None

    first_trim = _get_best_trim_bbox(first["bbox"], second["bbox"])
    second_trim = _get_best_trim_bbox(second["bbox"], first["bbox"])
    first_loss = (
        1.0
        if first_trim is None
        else 1 - _obb_area(first_trim) / first_area
    )
    second_loss = (
        1.0
        if second_trim is None
        else 1 - _obb_area(second_trim) / second_area
    )

    if abs(first_loss - second_loss) <= 1e-12:
        # При равной потере обрезаем менее уверенную детекцию.
        first_confidence = float(first.get("confidence", 0))
        second_confidence = float(second.get("confidence", 0))
        trim_first = first_confidence <= second_confidence
    else:
        trim_first = first_loss < second_loss

    if trim_first:
        return {
            "target_index": first_index,
            "blocker_index": second_index,
            "bbox": first_trim,
            "loss_ratio": first_loss,
        }
    return {
        "target_index": second_index,
        "blocker_index": first_index,
        "bbox": second_trim,
        "loss_ratio": second_loss,
    }


def _get_best_trim_bbox(
    target_bbox: dict[str, Any],
    blocker_bbox: dict[str, Any],
) -> dict[str, float] | None:
    target_points = _get_obb_points(target_bbox)
    blocker_points = _get_obb_points(blocker_bbox)
    axis_x, axis_y = _get_obb_axes(target_bbox)

    target_x = [_dot(point, axis_x) for point in target_points]
    target_y = [_dot(point, axis_y) for point in target_points]
    blocker_x = [_dot(point, axis_x) for point in blocker_points]
    blocker_y = [_dot(point, axis_y) for point in blocker_points]

    min_x, max_x = min(target_x), max(target_x)
    min_y, max_y = min(target_y), max(target_y)
    blocker_min_x, blocker_max_x = min(blocker_x), max(blocker_x)
    blocker_min_y, blocker_max_y = min(blocker_y), max(blocker_y)

    bounds = [
        (min_x, min(max_x, blocker_min_x), min_y, max_y),
        (max(min_x, blocker_max_x), max_x, min_y, max_y),
        (min_x, max_x, min_y, min(max_y, blocker_min_y)),
        (min_x, max_x, max(min_y, blocker_max_y), max_y),
    ]
    valid_bounds = [
        candidate
        for candidate in bounds
        if candidate[1] - candidate[0] > 1e-9
        and candidate[3] - candidate[2] > 1e-9
    ]
    if not valid_bounds:
        return None

    best_bounds = max(
        valid_bounds,
        key=lambda candidate: (
            (candidate[1] - candidate[0])
            * (candidate[3] - candidate[2])
        ),
    )
    candidate_bbox = _bbox_from_local_bounds(
        *best_bounds,
        axis_x,
        axis_y,
    )

    # Защита от численных и геометрических погрешностей.
    candidate_intersection = _clip_convex_polygon(
        _ordered_polygon(_get_obb_points(candidate_bbox)),
        _ordered_polygon(blocker_points),
    )
    if _polygon_area(candidate_intersection) > 1e-7:
        return None
    return candidate_bbox


def _get_obb_axes(
    bbox: dict[str, Any],
) -> tuple[tuple[float, float], tuple[float, float]]:
    points = _get_obb_points(bbox)
    edges = []

    for index in range(4):
        start = points[index]
        end = points[(index + 1) % 4]
        vector = (end[0] - start[0], end[1] - start[1])
        length = math.hypot(*vector)
        if length > 1e-12:
            edges.append((length, vector))

    if len(edges) < 2:
        raise ValueError("Невозможно определить оси вырожденного OBB")

    long_vector = max(edges, key=lambda edge: edge[0])[1]
    long_length = math.hypot(*long_vector)
    axis_x = (
        long_vector[0] / long_length,
        long_vector[1] / long_length,
    )
    axis_y = (-axis_x[1], axis_x[0])
    return axis_x, axis_y


def _bbox_from_local_bounds(
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
    axis_x: tuple[float, float],
    axis_y: tuple[float, float],
) -> dict[str, float]:
    points = [
        _from_axes(min_x, min_y, axis_x, axis_y),
        _from_axes(max_x, min_y, axis_x, axis_y),
        _from_axes(max_x, max_y, axis_x, axis_y),
        _from_axes(min_x, max_y, axis_x, axis_y),
    ]
    return {
        f"{axis}{index}": point[coordinate]
        for index, point in enumerate(points, start=1)
        for axis, coordinate in (("x", 0), ("y", 1))
    }


def _obb_area(bbox: dict[str, Any]) -> float:
    return _polygon_area(_ordered_polygon(_get_obb_points(bbox)))


def _get_merge_candidate_score(
    first_bbox: dict[str, Any],
    second_bbox: dict[str, Any],
    overlap_threshold: float,
    angle_threshold_degrees: float,
    edge_similarity_threshold: float,
) -> float | None:
    first_length, first_width, first_angle = get_obb_dimensions(first_bbox)
    second_length, second_width, second_angle = get_obb_dimensions(second_bbox)

    if min(first_length, first_width, second_length, second_width) <= 0:
        return None

    angle_difference = abs(first_angle - second_angle) % 180
    angle_difference = min(angle_difference, 180 - angle_difference)
    if angle_difference > angle_threshold_degrees:
        return None

    length_similarity = min(first_length, second_length) / max(
        first_length,
        second_length,
    )
    width_similarity = min(first_width, second_width) / max(
        first_width,
        second_width,
    )
    if (
        length_similarity < edge_similarity_threshold
        or width_similarity < edge_similarity_threshold
    ):
        return None

    first_polygon = _ordered_polygon(_get_obb_points(first_bbox))
    second_polygon = _ordered_polygon(_get_obb_points(second_bbox))
    intersection = _clip_convex_polygon(first_polygon, second_polygon)
    intersection_area = _polygon_area(intersection)

    if intersection_area <= 0:
        return None

    first_overlap = intersection_area / _polygon_area(first_polygon)
    second_overlap = intersection_area / _polygon_area(second_polygon)
    overlap_score = max(first_overlap, second_overlap)
    if overlap_score < overlap_threshold:
        return None

    edge_similarity = min(length_similarity, width_similarity)
    angle_similarity = (
        1.0
        if angle_threshold_degrees == 0
        else 1 - angle_difference / angle_threshold_degrees
    )

    return (
        overlap_score * 0.6
        + edge_similarity * 0.25
        + angle_similarity * 0.15
    )


def _merge_rectangle_pair(
    first: dict[str, Any],
    second: dict[str, Any],
) -> dict[str, Any]:
    first_bbox = first["bbox"]
    second_bbox = second["bbox"]
    _, _, first_angle = get_obb_dimensions(first_bbox)
    _, _, second_angle = get_obb_dimensions(second_bbox)
    first_area = _polygon_area(_get_obb_points(first_bbox))
    second_area = _polygon_area(_get_obb_points(second_bbox))

    angle = _mean_axis_angle(
        first_angle,
        second_angle,
        first_area,
        second_area,
    )
    angle_radians = math.radians(angle)
    axis_x = (math.cos(angle_radians), math.sin(angle_radians))
    axis_y = (-axis_x[1], axis_x[0])
    points = _get_obb_points(first_bbox) + _get_obb_points(second_bbox)

    projected_x = [_dot(point, axis_x) for point in points]
    projected_y = [_dot(point, axis_y) for point in points]
    min_x, max_x = min(projected_x), max(projected_x)
    min_y, max_y = min(projected_y), max(projected_y)

    corners = [
        _from_axes(min_x, min_y, axis_x, axis_y),
        _from_axes(max_x, min_y, axis_x, axis_y),
        _from_axes(max_x, max_y, axis_x, axis_y),
        _from_axes(min_x, max_y, axis_x, axis_y),
    ]
    merged_bbox = {
        f"{axis}{index}": point[coordinate]
        for index, point in enumerate(corners, start=1)
        for axis, coordinate in (("x", 0), ("y", 1))
    }

    first_confidence = float(first.get("confidence", 0))
    second_confidence = float(second.get("confidence", 0))
    result = dict(
        first if first_confidence >= second_confidence else second
    )
    result["bbox"] = merged_bbox
    result["confidence"] = max(first_confidence, second_confidence)
    result["merged_count"] = (
        int(first.get("merged_count", 1))
        + int(second.get("merged_count", 1))
    )
    return result


def _clip_convex_polygon(
    subject: list[tuple[float, float]],
    clip: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    output = subject

    for index, clip_start in enumerate(clip):
        clip_end = clip[(index + 1) % len(clip)]
        input_points = output
        output = []
        if not input_points:
            break

        previous = input_points[-1]
        for current in input_points:
            current_inside = _is_inside(current, clip_start, clip_end)
            previous_inside = _is_inside(previous, clip_start, clip_end)

            if current_inside:
                if not previous_inside:
                    output.append(
                        _line_intersection(
                            previous,
                            current,
                            clip_start,
                            clip_end,
                        )
                    )
                output.append(current)
            elif previous_inside:
                output.append(
                    _line_intersection(
                        previous,
                        current,
                        clip_start,
                        clip_end,
                    )
                )
            previous = current

    return output


def _ordered_polygon(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    center_x = sum(point[0] for point in points) / len(points)
    center_y = sum(point[1] for point in points) / len(points)
    return sorted(
        points,
        key=lambda point: math.atan2(
            point[1] - center_y,
            point[0] - center_x,
        ),
    )


def _polygon_area(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    return abs(
        sum(
            points[index][0] * points[(index + 1) % len(points)][1]
            - points[(index + 1) % len(points)][0] * points[index][1]
            for index in range(len(points))
        )
    ) / 2


def _is_inside(
    point: tuple[float, float],
    edge_start: tuple[float, float],
    edge_end: tuple[float, float],
) -> bool:
    return (
        (edge_end[0] - edge_start[0]) * (point[1] - edge_start[1])
        - (edge_end[1] - edge_start[1]) * (point[0] - edge_start[0])
    ) >= -1e-9


def _line_intersection(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> tuple[float, float]:
    first_direction = (
        first_end[0] - first_start[0],
        first_end[1] - first_start[1],
    )
    second_direction = (
        second_end[0] - second_start[0],
        second_end[1] - second_start[1],
    )
    denominator = (
        first_direction[0] * second_direction[1]
        - first_direction[1] * second_direction[0]
    )
    if abs(denominator) < 1e-12:
        return first_end

    offset = (
        second_start[0] - first_start[0],
        second_start[1] - first_start[1],
    )
    factor = (
        offset[0] * second_direction[1]
        - offset[1] * second_direction[0]
    ) / denominator
    return (
        first_start[0] + factor * first_direction[0],
        first_start[1] + factor * first_direction[1],
    )


def _mean_axis_angle(
    first_angle: float,
    second_angle: float,
    first_weight: float,
    second_weight: float,
) -> float:
    x = (
        math.cos(math.radians(first_angle * 2)) * first_weight
        + math.cos(math.radians(second_angle * 2)) * second_weight
    )
    y = (
        math.sin(math.radians(first_angle * 2)) * first_weight
        + math.sin(math.radians(second_angle * 2)) * second_weight
    )
    return math.degrees(math.atan2(y, x)) / 2 % 180


def _dot(
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    return first[0] * second[0] + first[1] * second[1]


def _from_axes(
    x: float,
    y: float,
    axis_x: tuple[float, float],
    axis_y: tuple[float, float],
) -> tuple[float, float]:
    return (
        x * axis_x[0] + y * axis_y[0],
        x * axis_x[1] + y * axis_y[1],
    )


def _as_ratio(value: float, name: str) -> float:
    value = float(value)
    if 1 < value <= 100:
        value /= 100
    if not 0 <= value <= 1:
        raise ValueError(f"{name} должен быть от 0 до 1 или от 0 до 100")
    return value


def _copy_rectangle(rectangle: dict[str, Any]) -> dict[str, Any]:
    copied = dict(rectangle)
    copied["bbox"] = dict(rectangle["bbox"])
    return copied

def get_two_points_bbox(four_points_bbox: dict):
    if not four_points_bbox:
        return {}
    
    x = [four_points_bbox["x1"], four_points_bbox["x2"], four_points_bbox["x3"], four_points_bbox["x4"]]
    y = [four_points_bbox["y1"], four_points_bbox["y2"], four_points_bbox["y3"], four_points_bbox["y4"]]
    return {"x0": min(x), "y0": min(y), "x1": max(x), "y1": max(y)}
