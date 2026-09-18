from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import json
from pdf_prcoessor import PdfProcessor
import os
import re
import shutil
from typing import List, Any
from PIL import Image
import torch
import numpy as np

from logger import setup_logger
from config import settings

logger = setup_logger(__name__)

MATERIAL_COLORS = [
    "#e6194b",
    "#3cb44b",
    "#4363d8",
    "#f58231",
    "#911eb4",
    "#46f0f0",
    "#f032e6",
    "#bcf60c",
    "#fabebe",
    "#008080",
    "#e6beff",
    "#9a6324",
    "#800000",
    "#808000",
    "#000075",
]

def delete_debug_folder():
    path = settings.DEBUG_DIR
    if not path.exists():
        return

    for child_path in path.iterdir():
        if child_path.is_dir():
            shutil.rmtree(child_path)
        else:
            child_path.unlink()



def save_walls_highlighted(folder_name:str, walls, pdf_processor: PdfProcessor):
    save_path = settings.DEBUG_DIR / folder_name / settings.DEBUG_WALLS_HIGHLIGHTED_DIR
    Path(save_path).mkdir(parents=True, exist_ok=True)
    for i, wall in enumerate(walls):
        bbox = wall["bbox"]
        rect = {
            "x0": min(bbox["x1"], bbox["x2"], bbox["x3"], bbox["x4"]),
            "y0": min(bbox["y1"], bbox["y2"], bbox["y3"], bbox["y4"]),
            "x1": max(bbox["x1"], bbox["x2"], bbox["x3"], bbox["x4"]),
            "y1": max(bbox["y1"], bbox["y2"], bbox["y3"], bbox["y4"]),
        }
        crop_x0 = max(0, int(rect["x0"] - 20))
        crop_y0 = max(0, int(rect["y0"] - 20))
        _, img = pdf_processor.crop_image(
            rect["x0"] - 20,
            rect["y0"] - 20,
            rect["x1"] + 20,
            rect["y1"] + 20,
        )

        image_name = f"page_{pdf_processor.pdf_path.stem}_{i}.png"
        highlighted_img = img.copy()
        draw = ImageDraw.Draw(highlighted_img)
        points = [
            (
                float(bbox[f"x{point_index}"]) - crop_x0,
                float(bbox[f"y{point_index}"]) - crop_y0,
            )
            for point_index in range(1, 5)
        ]
        draw.line(points + [points[0]], fill="red", width=1)
        highlighted_img.save(save_path / f"{image_name}")


def save_walls_result(folder_name:str, result):
    save_path = settings.DEBUG_DIR / folder_name
    os.makedirs(save_path, exist_ok=True)
    with open(save_path / "walls_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)

def save_result(result):
    save_path = settings.DEBUG_DIR
    os.makedirs(save_path, exist_ok=True)
    with open(save_path / "result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)

def save_blueprint_walls_by_material(
    folder_name:str,
    walls: list[dict],
    pdf_processor: PdfProcessor,
    file_name: str | Path,
    legend_row_items: list[dict[str, Any]],
    fill_opacity: float,
    confidence: float | None = None,
    save_md: bool = True,
    zoom: float = settings.WALL_DETECTION.zoom,
    draw_obbs: bool = True,
    draw_polygons: bool = True,
):
    fill_opacity = float(fill_opacity)
    if 1 < fill_opacity <= 100:
        fill_opacity /= 100
    if not 0 <= fill_opacity <= 1:
        raise ValueError("fill_opacity must be from 0 to 1 or from 0 to 100")

    grouped_walls: dict[str, list[dict]] = {}
    material_colors: dict[str, str] = {}
    obb_walls: list[dict] = []
    grouped_polygons: dict[str, list[dict]] = {}

    for wall in walls:
        is_polygon = "polygon" in wall
        if (is_polygon and not draw_polygons) or (not is_polygon and not draw_obbs):
            continue

        material = _get_wall_material(wall)
        if material not in material_colors:
            material_colors[material] = MATERIAL_COLORS[
                len(material_colors) % len(MATERIAL_COLORS)
            ]

        color = material_colors[material]
        if is_polygon:
            grouped_polygons.setdefault(color, []).append(wall)
        else:
            drawable_wall = dict(wall)
            if "obb_pdf" in wall:
                drawable_wall["bbox_pdf"] = wall["obb_pdf"]
            grouped_walls.setdefault(color, []).append(drawable_wall)
            obb_walls.append(drawable_wall)

    for legend_row in legend_row_items if draw_obbs else []:
        material = legend_row["full_description"]
        if material not in material_colors or not all("bbox" in symbol for symbol in legend_row["legend_symbols"]):
            continue
        color = material_colors[material]
        for symbol in legend_row["legend_symbols"]:
            grouped_walls.setdefault(color, []).append({"bbox_pdf": symbol["bbox"]})

    output_path = settings.DEBUG_DIR / folder_name / settings.DEBUG_IMAGES_DIR / file_name
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if draw_obbs:
        _, painted_walls_image = pdf_processor.render_obb_rectangles(
            grouped_walls,
            width=2,
            fill_opacity=fill_opacity,
            zoom=zoom,
        )
        img_with_labels = pdf_processor.draw_obb_labels(
            painted_walls_image,
            obb_walls,
            label_key="id",
            zoom=zoom,
        )
    else:
        _, img_with_labels = pdf_processor.pdf_to_base64(zoom=zoom)
        img_with_labels = img_with_labels.copy()

    if grouped_polygons:
        img_with_labels = pdf_processor.render_pdf_polygons(
            grouped_polygons,
            image=img_with_labels,
            fill_opacity=fill_opacity,
            zoom=zoom,
            width=2,
            label_key="id",
        )

    if confidence:
        draw_text_in_top_left_corner(img_with_labels, f"Conf: {round(confidence*100, 1)}%")

    img_with_labels.save(output_path)

    materials_colors_md = _format_material_colors_markdown(material_colors)
    color_map_path = output_path.with_suffix(".materials.md")
    if save_md:
        with color_map_path.open("w", encoding="utf-8") as color_map_file:
            color_map_file.write(materials_colors_md)

    return img_with_labels, materials_colors_md


def save_blueprint_masks_by_material(
    folder_name: str,
    matrices: list[dict[str, Any]],
    pdf_processor: PdfProcessor,
    file_name: str | Path,
    legends: list[dict],
    fill_opacity: float = 0.5,
) -> None:
    """Save global binary channel masks overlaid on the rendered PDF page."""
    fill_opacity = float(fill_opacity)
    if 1 < fill_opacity <= 100:
        fill_opacity /= 100
    if not 0 <= fill_opacity <= 1:
        raise ValueError("fill_opacity must be from 0 to 1 or from 0 to 100")

    if not matrices:
        return

    for matrix_entry in matrices:
        matrix = matrix_entry["matrix"]
        channel_id_2_entry_id = matrix_entry["channel_id_2_entry_id"]
        if not isinstance(matrix, torch.Tensor):
            raise TypeError("Each debug matrix must be a torch.Tensor")
        if matrix.ndim != 3:
            raise ValueError(
                "Each debug matrix must have shape [N, H, W], "
                f"got {tuple(matrix.shape)}"
            )
        if matrix.device.type != "cpu":
            raise ValueError("Debug matrices must be moved to CPU before saving")
        if not isinstance(channel_id_2_entry_id, dict):
            raise TypeError("channel_id_2_entry_id must be a dict")
        if any(channel_id not in channel_id_2_entry_id for channel_id in range(matrix.shape[0])):
            raise ValueError("channel_id_2_entry_id must contain every matrix channel")

    _, page_image = pdf_processor.pdf_to_base64(dpi=settings.DEBUG_DPI)
    painted_image = page_image.convert("RGB")
    opacity = round(255 * fill_opacity)
    material_masks: dict[str, Image.Image] = {}
    material_entry_ids: dict[str, set[int]] = {}
    material_colors: dict[str, str] = {}

    for matrix_entry in matrices:
        matrix = matrix_entry["matrix"]
        channel_id_2_entry_id = matrix_entry["channel_id_2_entry_id"]

        for channel_id in range(matrix.shape[0]):
            entry_id = channel_id_2_entry_id[channel_id]
            if not isinstance(entry_id, int) or not 0 <= entry_id < len(legends):
                raise ValueError(
                    f"Legend entry id {entry_id!r} for channel {channel_id} is out of range"
                )

            material = str(
                legends[entry_id].get("full_description", f"entry_{entry_id}")
            )
            material_entry_ids.setdefault(material, set()).add(entry_id)
            material_mask = material_masks.get(material)
            if material_mask is None:
                material_mask = Image.new("L", painted_image.size, 0)
                material_masks[material] = material_mask

            height = min(int(matrix.shape[1]), painted_image.height)
            width = min(int(matrix.shape[2]), painted_image.width)
            if height == 0 or width == 0:
                continue

            binary = (
                matrix[channel_id, :height, :width]
                .to(dtype=torch.uint8)
                .mul(255)
                .numpy()
            )
            binary_mask = Image.fromarray(binary)
            # Paste only selected pixels, preserving masks from other drawings.
            material_mask.paste(opacity, (0, 0, width, height), binary_mask)

    for material_index, (material, material_mask) in enumerate(material_masks.items()):
        mask_draw = ImageDraw.Draw(material_mask)
        for entry_id in material_entry_ids[material]:
            for symbol in legends[entry_id].get("legend_symbols", []):
                bbox_pdf = symbol.get("bbox")
                if bbox_pdf is None:
                    continue

                bbox = pdf_processor.pdf_obb_to_image_obb(bbox_pdf)
                try:
                    points = [
                        (
                            float(bbox[f"x{point_index}"]),
                            float(bbox[f"y{point_index}"]),
                        )
                        for point_index in range(1, 5)
                    ]
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "Each legend symbol bbox must contain numeric "
                        "x1, y1 ... x4, y4"
                    ) from exc

                mask_draw.polygon(points, fill=opacity)
                mask_draw.line(
                    points + [points[0]],
                    fill=255,
                    width=2,
                    joint="curve",
                )

        color = MATERIAL_COLORS[material_index % len(MATERIAL_COLORS)]
        material_colors[material] = color
        painted_image.paste(color, (0, 0), material_mask)

    output_path = settings.DEBUG_DIR / folder_name / settings.DEBUG_IMAGES_DIR / file_name
    output_path.parent.mkdir(parents=True, exist_ok=True)
    painted_image.save(output_path)

    color_map_path = output_path.with_suffix(".materials.md")
    color_map_path.write_text(
        _format_material_colors_markdown(material_colors),
        encoding="utf-8",
    )


def save_probability_heatmaps_by_material(
    folder_name: str,
    drawing_index: int,
    matrix: torch.Tensor,
    channel_id_2_entry_id: dict[int, int],
    pdf_processor: PdfProcessor,
    legends: list[dict],
    source_dpi: int,
    target_dpi: int,
    threshold: float,
    max_opacity: float = 0.7,
) -> None:
    """Save one fixed-scale probability heatmap overlay per matrix channel."""
    if matrix.ndim != 3:
        raise ValueError(
            "Probability matrix must have shape [N, H, W], "
            f"got {tuple(matrix.shape)}"
        )
    if target_dpi <= 0:
        raise ValueError("target_dpi must be greater than zero")
    if source_dpi <= 0:
        raise ValueError("source_dpi must be greater than zero")
    if not 0 <= threshold <= 1:
        raise ValueError("threshold must be from 0 to 1")
    if not 0 <= max_opacity <= 1:
        raise ValueError("max_opacity must be from 0 to 1")
    if any(channel_id not in channel_id_2_entry_id for channel_id in range(matrix.shape[0])):
        raise ValueError("channel_id_2_entry_id must contain every matrix channel")

    _, page_image = pdf_processor.pdf_to_base64(dpi=target_dpi)
    page_image = page_image.convert("RGB")
    output_dir = (
        settings.DEBUG_DIR
        / folder_name
        / "probability_heatmaps"
        / f"drawing_{drawing_index}"
    )
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    used_names: set[str] = set()
    scale = target_dpi / source_dpi
    target_height = max(1, round(int(matrix.shape[1]) * scale))
    target_width = max(1, round(int(matrix.shape[2]) * scale))

    for channel_id in range(matrix.shape[0]):
        entry_id = channel_id_2_entry_id[channel_id]
        if not isinstance(entry_id, int) or not 0 <= entry_id < len(legends):
            raise ValueError(
                f"Legend entry id {entry_id!r} for channel {channel_id} is out of range"
            )
        material = str(
            legends[entry_id].get("full_description", f"entry_{entry_id}")
        )
        file_stem = _unique_debug_file_stem(
            _sanitize_debug_file_name(material),
            used_names,
        )

        # Move only one channel to RAM at a time to avoid retaining another
        # complete copy of the probability matrix.
        probability = (
            matrix[channel_id]
            .detach()
            .to(device="cpu", dtype=torch.float32)
            .clamp_(0, 1)
            .numpy()
        )
        probability_image = Image.fromarray(probability, mode="F").resize(
            (target_width, target_height),
            resample=Image.Resampling.BILINEAR,
        )
        probability = np.asarray(probability_image, dtype=np.float32)

        height = min(page_image.height, probability.shape[0])
        width = min(page_image.width, probability.shape[1])
        probability = probability[:height, :width]
        visible = probability >= threshold
        display_probability = _normalize_probability_for_heatmap(
            probability,
            threshold,
        )
        heatmap = _probability_to_heatmap(display_probability)

        # Keep values at the threshold visible while leaving everything below
        # it fully transparent. Higher confidence gradually increases opacity.
        min_opacity = min(0.25, max_opacity)
        alpha = np.where(
            visible,
            min_opacity + display_probability * (max_opacity - min_opacity),
            0,
        )
        alpha = np.rint(alpha * 255).astype(np.uint8)

        overlay = Image.fromarray(
            np.dstack((heatmap, alpha)),
            mode="RGBA",
        )
        result = page_image.copy()
        result.paste(overlay, (0, 0), overlay)
        result.save(output_dir / f"{file_stem}.png")


def _normalize_probability_for_heatmap(
    probability: np.ndarray,
    threshold: float,
) -> np.ndarray:
    """Expand [threshold, 1] to [0, 1] and emphasize high-score differences."""
    if threshold >= 1:
        return (probability >= 1).astype(np.float32)

    normalized = np.clip(
        (probability - threshold) / (1 - threshold),
        0,
        1,
    )
    return np.square(normalized, dtype=np.float32)


def _probability_to_heatmap(probability: np.ndarray) -> np.ndarray:
    """Map probabilities in [0, 1] to a fixed blue-to-red color scale."""
    values = np.rint(probability * 255).astype(np.uint8)
    anchor_positions = np.asarray([0, 64, 128, 191, 255])
    anchor_colors = np.asarray(
        [
            (0, 0, 255),
            (0, 255, 255),
            (0, 255, 0),
            (255, 255, 0),
            (255, 0, 0),
        ],
        dtype=np.float32,
    )
    lut = np.empty((256, 3), dtype=np.uint8)
    lut_values = np.arange(256)
    for color_channel in range(3):
        lut[:, color_channel] = np.rint(
            np.interp(
                lut_values,
                anchor_positions,
                anchor_colors[:, color_channel],
            )
        ).astype(np.uint8)
    return lut[values]


def _sanitize_debug_file_name(value: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    sanitized = re.sub(r"\s+", " ", sanitized)
    return sanitized[:180] or "material"


def _unique_debug_file_stem(file_stem: str, used_names: set[str]) -> str:
    candidate = file_stem
    suffix = 2
    while candidate.casefold() in used_names:
        candidate = f"{file_stem}_{suffix}"
        suffix += 1
    used_names.add(candidate.casefold())
    return candidate


def _get_wall_material(wall: dict) -> str:
    best_hatching = wall.get("hatching", {}).get("best")
    if best_hatching:
        material = best_hatching.get("text_designation")
        if material:
            return str(material)

    material = wall.get("material")
    if material:
        return str(material)

    return "unknown"


def _format_material_colors_markdown(material_colors: dict[str, str]) -> str:
    lines = [
        "| Цвет | Материал |",
        "| --- | --- |",
    ]
    for material, color in material_colors.items():
        swatch = (
            f'<span style="display:inline-block;width:18px;height:18px;'
            f'background:{color};border:1px solid #999;"></span>'
        )
        material_text = _escape_markdown_table_cell(material)
        lines.append(f"| {swatch} | {material_text} |")

    return "\n".join(lines) + "\n"


def _escape_markdown_table_cell(value: str) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def save_layouts(legends: List[Image.Image], titles: List[Image.Image], drawings: List[Image.Image]):
    _save_list_of_images(legends, settings.DEBUG_LAYOUTS_DIR / "legends")
    _save_list_of_images(titles, settings.DEBUG_LAYOUTS_DIR / "titles")
    _save_list_of_images(drawings, settings.DEBUG_LAYOUTS_DIR / "drawings")

def _save_list_of_images(images: List[Image.Image], path: Path):
    path.mkdir(parents=True, exist_ok=True)
    for i, img in enumerate(images):
        img.save(path / f"{i}.png")

def save_initial_blueprint(pdf_processor: PdfProcessor):
    _, img = pdf_processor.pdf_to_base64(2)
    img.save(settings.DEBUG_DIR / "initial_blueprint.png")

def save_legend_rows(legend_rows):
    map_for_save = {}

    for l_i, legend_row in enumerate(legend_rows):
        folder_name = f"row_{l_i}"
        path = settings.DEBUG_LEGEND_LAYOUTS_FILTERED_DIR / Path(folder_name)
        path.mkdir(parents=True, exist_ok=True)
        for s_i, symbol in enumerate(legend_row["legend_symbols"]):
            symbol["image"].save(path / f"symbol_{s_i}.png")
        for d_i, description in enumerate(legend_row.get("legend_descriptions", [])):
            description["image"].save(path / f"description_{d_i}.png")
        map_for_save[folder_name] = {"description": legend_row["full_description"], "element_type": legend_row.get("element_type", None)}
    
    with (settings.DEBUG_LEGEND_LAYOUTS_FILTERED_DIR / "map.json").open("w", encoding="utf-8") as f:
        json.dump(map_for_save, f, indent=2, ensure_ascii=False)

def clear_legend_rows_folder():
    path = settings.DEBUG_LEGEND_LAYOUTS_FILTERED_DIR
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)

def save_run_settings():
    with open(settings.DEBUG_DIR / "run_config.json", "w", encoding="utf-8") as f:
        json.dump(
            settings.model_dump(mode="json"),
            f,
            indent=4,
            ensure_ascii=False,
        )


def draw_text_in_top_left_corner(
    image: Image.Image,
    text: str,
    font_size_ratio: float = 0.03,
    margin_ratio: float = 0.01,
    text_color: str = "white",
    stroke_width_ratio: float = 0.002,
    stroke_color: str = "black",
) -> Image.Image:
    """Рисует текст в левом верхнем углу изображения.

    Размер шрифта, отступ и толщина обводки задаются в долях от меньшей
    стороны изображения. Функция изменяет и возвращает исходное изображение.
    """
    reference_size = min(image.size)
    font_size = max(1, round(reference_size * font_size_ratio))
    margin = max(0, round(reference_size * margin_ratio))
    stroke_width = max(0, round(reference_size * stroke_width_ratio))
    font = ImageFont.load_default(size=font_size)

    draw = ImageDraw.Draw(image)
    draw.text(
        (margin, margin),
        text,
        font=font,
        fill=text_color,
        stroke_width=stroke_width,
        stroke_fill=stroke_color,
    )
    return image

def save_torch_matrix(matrix: torch.Tensor, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(matrix, path)
