from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import json
from pdf_prcoessor import PdfProcessor
import os
import shutil
from typing import List, Any
from PIL import Image

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
    confidence: float | None = None
):
    grouped_walls: dict[str, list[dict]] = {}
    material_colors: dict[str, str] = {}

    for wall in walls:
        material = _get_wall_material(wall)
        if material not in material_colors:
            material_colors[material] = MATERIAL_COLORS[
                len(material_colors) % len(MATERIAL_COLORS)
            ]

        color = material_colors[material]
        grouped_walls.setdefault(color, []).append(wall)

    for legend_row in legend_row_items:
        material = legend_row["full_description"]
        if material not in material_colors or not all("bbox" in symbol for symbol in legend_row["legend_symbols"]):
            continue
        color = material_colors[material]
        for symbol in legend_row["legend_symbols"]:
            grouped_walls.setdefault(color, []).append({"bbox_pdf": symbol["bbox"]})

    output_path = settings.DEBUG_DIR / folder_name / settings.DEBUG_IMAGES_DIR / file_name
    output_path.parent.mkdir(parents=True, exist_ok=True)

    _, painted_walls_image = pdf_processor.render_obb_rectangles(
        grouped_walls,
        width=2,
        fill_opacity=fill_opacity,
        zoom=settings.BLUEPRINT.zoom
    )
    img_with_labels = pdf_processor.draw_obb_labels(
        painted_walls_image,
        walls,
        label_key="id",
        zoom=settings.BLUEPRINT.zoom
    )

    if confidence:
        draw_text_in_top_left_corner(img_with_labels, f"Conf: {round(confidence*100, 1)}%")

    img_with_labels.save(output_path)

    materials_colors_md = _format_material_colors_markdown(material_colors)
    color_map_path = output_path.with_suffix(".materials.md")
    with color_map_path.open("w", encoding="utf-8") as color_map_file:
        color_map_file.write(materials_colors_md)

    return img_with_labels, materials_colors_md
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
        "| Цвет | Материал | Hex |",
        "| --- | --- | --- |",
    ]
    for material, color in material_colors.items():
        swatch = (
            f'<span style="display:inline-block;width:18px;height:18px;'
            f'background:{color};border:1px solid #999;"></span>'
        )
        material_text = _escape_markdown_table_cell(material)
        lines.append(f"| {swatch} | {material_text} | `{color}` |")

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
        map_for_save[folder_name] = legend_row["full_description"]
    
    with (settings.DEBUG_LEGEND_LAYOUTS_FILTERED_DIR / "map.json").open("w", encoding="utf-8") as f:
        json.dump(map_for_save, f, indent=2, ensure_ascii=False)

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

