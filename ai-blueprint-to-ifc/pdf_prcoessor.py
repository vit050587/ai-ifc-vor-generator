from pdf2image import convert_from_path
import base64
from PIL import ImageEnhance, ImageFilter
import io
import fitz
import numpy as np
from dataclasses import replace
from math import atan2, ceil, degrees, floor, hypot
from PIL import Image, ImageColor, ImageDraw
from shapely.geometry import Polygon
from shapely.ops import polylabel
from utils import image_to_base64
from pathlib import Path
from typing import Tuple

from config import settings
from mask_polygonizer import (
    PdfMaskPolygon,
    PdfPolygonizedMask,
    PolygonizedMask,
)
from obb_converter import RegionOBB
from polygon_converter import RegionPolygon

class PdfProcessor:

    def __init__(self, pdf_path: Path):
        Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS

        self.pdf_path = pdf_path
        self.img = None
        self.img_b64 = None
        self.zoom = None

    def pdf_to_base64(self, zoom: float | None = None, dpi: int | None = None) -> Tuple:
        if zoom is not None and dpi is not None:
            raise ValueError("Specify either zoom or dpi, not both")

        if dpi is not None:
            zoom = dpi / 72

        if zoom is None:
            zoom = 2.0
        
        if self.img_b64 and self.img and self.zoom == zoom:
            return self.img_b64, self.img

        doc = fitz.open(self.pdf_path)
        page = doc[0]

        pix = page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom),
            alpha=False,
        )

        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

        self.img = img
        self.img_b64 = image_to_base64(img)
        self.zoom = zoom

        return self.img_b64, img

    def pdf_rect_to_image_rect(self, rect: dict) -> dict:
        if self.zoom is None:
            self.pdf_to_base64()

        doc = fitz.open(self.pdf_path)
        page = doc[0]
        r = fitz.Rect(
            rect["x0"],
            rect["y0"],
            rect["x1"],
            rect["y1"],
        ) * page.rotation_matrix

        return {
            "x0": r.x0 * self.zoom,
            "y0": r.y0 * self.zoom,
            "x1": r.x1 * self.zoom,
            "y1": r.y1 * self.zoom,
        }

    def pdf_obb_to_image_obb(self, rectangle: dict, zoom=None) -> dict:
        if zoom:
            self.pdf_to_base64(zoom=zoom)
        if self.zoom is None:
            self.pdf_to_base64()

        doc = fitz.open(self.pdf_path)
        page = doc[0]
        bbox = rectangle

        converted_bbox = {}
        for point_index in range(1, 5):
            point = fitz.Point(
                float(bbox[f"x{point_index}"]),
                float(bbox[f"y{point_index}"]),
            ) * page.rotation_matrix
            converted_bbox[f"x{point_index}"] = point.x * self.zoom
            converted_bbox[f"y{point_index}"] = point.y * self.zoom

        if "bbox" not in rectangle:
            return converted_bbox

        converted_rectangle = dict(rectangle)
        converted_rectangle["bbox"] = converted_bbox
        return converted_rectangle

    def _image_point_to_pdf_point(
        self,
        x: float,
        y: float,
        zoom: float | None = None,
    ) -> tuple[float, float]:
        """Переводит точку из пикселей отрендеренного изображения в PDF points."""
        transform = self._get_image_to_pdf_transform(zoom)
        return self._transform_image_point(x, y, transform)

    def _get_image_to_pdf_transform(
        self,
        zoom: float | None = None,
    ) -> tuple[float, fitz.Matrix]:
        """Prepare a reusable image-to-PDF transform."""
        if zoom is not None and zoom != self.zoom:
            self.pdf_to_base64(zoom)
        elif self.zoom is None:
            self.pdf_to_base64()

        with fitz.open(self.pdf_path) as doc:
            derotation_matrix = fitz.Matrix(doc[0].derotation_matrix)

        return self.zoom, derotation_matrix

    @staticmethod
    def _transform_image_point(
        x: float,
        y: float,
        transform: tuple[float, fitz.Matrix],
    ) -> tuple[float, float]:
        zoom, derotation_matrix = transform
        point = fitz.Point(float(x) / zoom, float(y) / zoom)
        point = point * derotation_matrix
        return point.x, point.y

    def image_obb_to_pdf_obb(
        self,
        rectangle: dict,
        zoom: float | None = None,
    ) -> dict:
        """Переводит один OBB из пикселей изображения в координаты PDF."""
        transform = self._get_image_to_pdf_transform(zoom)
        return self._image_obb_to_pdf_obb(rectangle, transform)

    def _image_obb_to_pdf_obb(
        self,
        rectangle: dict,
        transform: tuple[float, fitz.Matrix],
    ) -> dict:
        bbox = rectangle.get("bbox", rectangle)

        try:
            converted_bbox = {}
            for point_index in range(1, 5):
                x, y = self._transform_image_point(
                    bbox[f"x{point_index}"],
                    bbox[f"y{point_index}"],
                    transform,
                )
                converted_bbox[f"x{point_index}"] = x
                converted_bbox[f"y{point_index}"] = y
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Each OBB must contain numeric x1, y1 ... x4, y4"
            ) from exc

        if "bbox" not in rectangle:
            return converted_bbox

        converted_rectangle = dict(rectangle)
        converted_rectangle["bbox"] = converted_bbox
        return converted_rectangle

    def image_obbs_to_pdf_obbs(
        self,
        rectangles: list[dict],
        zoom: float | None = None,
    ) -> list[dict]:
        """Переводит список OBB из пикселей изображения в координаты PDF."""
        if not rectangles:
            return []

        transform = self._get_image_to_pdf_transform(zoom)
        return [
            self._image_obb_to_pdf_obb(rectangle, transform)
            for rectangle in rectangles
        ]

    def image_geometry_to_pdf_geometry(
        self,
        geometry_parts: list[RegionOBB | RegionPolygon],
        dpi: int,
    ) -> list[RegionOBB | RegionPolygon]:
        """Return the same geometry types with their coordinates in PDF points."""
        if not geometry_parts:
            return []

        coefficient = dpi / 72
        transform = self._get_image_to_pdf_transform(coefficient)
        converted_polygons: dict[int, RegionPolygon] = {}

        def convert_points(points) -> np.ndarray:
            return np.asarray(
                [self._transform_image_point(x, y, transform) for x, y in points],
                dtype=np.float64,
            ).reshape(-1, 2)

        def convert_polygon(part: RegionPolygon, measure: bool = False) -> RegionPolygon:
            cached = converted_polygons.get(id(part))
            if cached is not None:
                if measure and cached.temp_width is None:
                    cached.temp_width = part.width / coefficient
                    cached.temp_area = part.area / coefficient ** 2
                return cached
            converted = replace(
                part,
                polygon=convert_points(part.polygon),
                holes=[convert_points(hole) for hole in part.holes],
                source_polygon=(
                    convert_polygon(part.source_polygon)
                    if part.source_polygon is not None else None
                ),
                temp_width=part.width / coefficient if measure else None,
                temp_area=part.area / coefficient ** 2 if measure else None,
            )
            converted_polygons[id(part)] = converted
            return converted

        converted = []
        for part in geometry_parts:
            if isinstance(part, RegionOBB):
                corners = convert_points(part.polygon)
                width_axis = corners[1] - corners[0]
                height_axis = corners[2] - corners[1]
                converted.append(replace(
                    part,
                    source_polygon=convert_polygon(part.source_polygon),
                    center=tuple(np.mean(corners, axis=0)),
                    width=hypot(*width_axis),
                    height=hypot(*height_axis),
                    angle=degrees(atan2(width_axis[1], width_axis[0])) % 180,
                ))
            elif isinstance(part, RegionPolygon):
                converted.append(convert_polygon(part, measure=True))
            else:
                raise TypeError(f"Unsupported geometry part: {type(part).__name__}")

        return converted

    def cropped_image_obbs_to_pdf_obbs(
        self,
        crop_bbox: dict,
        rectangles: list[dict],
        zoom: float | None = None,
        dpi: int | None = None
    ) -> list[dict]:
        if dpi is not None:
            self.pdf_to_base64(dpi=dpi)
            zoom = dpi / 72
        elif zoom is not None and zoom != self.zoom:
            self.pdf_to_base64(zoom)
        elif self.zoom is None:
            self.pdf_to_base64()

        image_rect = self.pdf_rect_to_image_rect(crop_bbox)
        shifted_rectangles = []

        for rectangle in rectangles:
            bbox = rectangle.get("bbox", rectangle)
            shifted_bbox = {
                f"{axis}{point_index}": (
                    float(bbox[f"{axis}{point_index}"])
                    + (image_rect["x0"] if axis == "x" else image_rect["y0"])
                )
                for point_index in range(1, 5)
                for axis in ("x", "y")
            }

            if "bbox" in rectangle:
                shifted_rectangle = dict(rectangle)
                shifted_rectangle["bbox"] = shifted_bbox
            else:
                shifted_rectangle = shifted_bbox

            shifted_rectangles.append(shifted_rectangle)

        return self.image_obbs_to_pdf_obbs(shifted_rectangles, zoom)

    def crop_image(self, x0: int, y0: int, x1: int, y1: int, zoom = None):
        if zoom and zoom != self.zoom:
            self.pdf_to_base64(zoom=zoom)
            
        if self.img is None:
            self.pdf_to_base64()

        width, height = self.img.size
        box = (
            max(0, int(x0)),
            max(0, int(y0)),
            min(width, int(x1)),
            min(height, int(y1)),
        )

        cropped_img = self.img.crop(box)
        cropped_img_b64 = image_to_base64(cropped_img)

        return cropped_img_b64, cropped_img

    def crop_pdf_rect(self, rect: dict, crop_size: int = 0, zoom: float = 4.0, dpi: int | None = None):

        if dpi:
            self.pdf_to_base64(dpi=dpi)
        else:
            self.pdf_to_base64(zoom=zoom)

        image_rect = self.pdf_rect_to_image_rect(rect)

        return self.crop_image(
            image_rect["x0"] - crop_size,
            image_rect["y0"] - crop_size,
            image_rect["x1"] + crop_size,
            image_rect["y1"] + crop_size,
        )

    def render_rectangles_and_crop(
        self,
        rectangles: list[dict],
        crop_rect: dict | None = None,
        outline: str = "red",
        width: int = 2,
    ):
        if self.img is None:
            self.pdf_to_base64()

        img_with_rectangles = self.img.copy()
        draw = ImageDraw.Draw(img_with_rectangles)

        for rect in rectangles:
            draw.rectangle(
                [
                    rect["x0"],
                    rect["y0"],
                    rect["x1"],
                    rect["y1"],
                ],
                outline=outline,
                width=width,
            )

        if crop_rect is None:
            return image_to_base64(img_with_rectangles), img_with_rectangles

        original_img = self.img
        self.img = img_with_rectangles
        try:
            return self.crop_image(
                crop_rect["x0"],
                crop_rect["y0"],
                crop_rect["x1"],
                crop_rect["y1"],
            )
        finally:
            self.img = original_img

    def render_obb_rectangles(
        self,
        rectangles: dict[str, list[dict]],
        width: int = 2,
        save_path: Path | None = None,
        fill_opacity: float = 0.0,
        zoom=None
    ):
        """
        Рисует OBB-прямоугольники в глобальных pdf координатах страницы.

        Поддерживаемые форматы:
            {"x1": ..., "y1": ..., ..., "x4": ..., "y4": ...}
            {"bbox_pdf": {"x1": ..., "y1": ..., ..., "x4": ..., "y4": ...}}
        """
        if zoom:
            self.pdf_to_base64(zoom=zoom)
        else:
            self.pdf_to_base64()

        image_with_rectangles = self.img.copy()
        fill_opacity = float(fill_opacity)
        if 1 < fill_opacity <= 100:
            fill_opacity /= 100
        if not 0 <= fill_opacity <= 1:
            raise ValueError("fill_opacity must be from 0 to 1 or from 0 to 100")

        if fill_opacity > 0:
            image_with_rectangles = image_with_rectangles.convert("RGBA")
            fill_layer = Image.new("RGBA", image_with_rectangles.size, (0, 0, 0, 0))
            fill_draw = ImageDraw.Draw(fill_layer)
        points_by_color = []

        for color, color_rectangles in rectangles.items():
            for rectangle in color_rectangles:
                bbox = rectangle.get("bbox_pdf", rectangle)
                bbox = self.pdf_obb_to_image_obb(bbox, zoom)
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
                        "Каждый OBB должен содержать числовые "
                        "x1, y1 ... x4, y4"
                    ) from exc

                if fill_opacity > 0:
                    fill_draw.polygon(
                        points,
                        fill=(*Image.new("RGB", (1, 1), color).getpixel((0, 0)), int(255 * fill_opacity)),
                    )

                points_by_color.append((color, points))

        if fill_opacity > 0:
            image_with_rectangles = Image.alpha_composite(image_with_rectangles, fill_layer).convert("RGB")

        draw = ImageDraw.Draw(image_with_rectangles)
        for color, points in points_by_color:
            draw.line(
                points + [points[0]],
                fill=color,
                width=width,
                joint="curve",
            )

        if save_path:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            image_with_rectangles.save(save_path)

        return (
            image_to_base64(image_with_rectangles),
            image_with_rectangles,
        )

    def render_pdf_polygons(
        self,
        polygons_by_color: dict[str, list[dict]],
        image: Image.Image | None = None,
        *,
        fill_opacity: float = 0.0,
        zoom: float | None = None,
        width: int = 2,
        label_key: str | None = None,
    ) -> Image.Image:
        """Draw PDF-point polygon exteriors, holes, and optional labels on an image."""
        fill_opacity = float(fill_opacity)
        if 1 < fill_opacity <= 100:
            fill_opacity /= 100
        if not 0 <= fill_opacity <= 1:
            raise ValueError("fill_opacity must be from 0 to 1 or from 0 to 100")

        if image is None:
            _, image = self.pdf_to_base64(zoom=zoom)
        render_zoom = zoom if zoom is not None else self.zoom
        if render_zoom is None or render_zoom <= 0:
            raise ValueError("zoom must be positive when an image is supplied")
        result = image.copy()

        with fitz.open(self.pdf_path) as document:
            rotation_matrix = fitz.Matrix(document[0].rotation_matrix)

        def to_image_points(points):
            result_points = []
            for x, y in points:
                point = fitz.Point(float(x), float(y)) * rotation_matrix
                result_points.append((point.x * render_zoom, point.y * render_zoom))
            return result_points

        opacity = round(255 * fill_opacity)
        outlines = []
        for color, polygons in polygons_by_color.items():
            for polygon in polygons:
                if "polygon_pdf" not in polygon:
                    raise ValueError("Polygon must contain polygon_pdf")
                exterior = to_image_points(polygon["polygon_pdf"])
                holes = [to_image_points(hole) for hole in polygon.get("holes_pdf", [])]
                if opacity and len(exterior) >= 3:
                    left = max(0, floor(min(x for x, _ in exterior)))
                    top = max(0, floor(min(y for _, y in exterior)))
                    right = min(result.width, ceil(max(x for x, _ in exterior)) + 1)
                    bottom = min(result.height, ceil(max(y for _, y in exterior)) + 1)
                    if right > left and bottom > top:
                        fill_layer = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
                        fill_draw = ImageDraw.Draw(fill_layer)
                        fill_draw.polygon(
                            [(x - left, y - top) for x, y in exterior],
                            fill=(*ImageColor.getrgb(color), opacity),
                        )
                        for hole in holes:
                            if len(hole) >= 3:
                                fill_draw.polygon(
                                    [(x - left, y - top) for x, y in hole],
                                    fill=(0, 0, 0, 0),
                                )
                        base = result.crop((left, top, right, bottom)).convert("RGBA")
                        blended = Image.alpha_composite(base, fill_layer)
                        result.paste(blended.convert("RGB"), (left, top))
                label = polygon.get(label_key) if label_key else None
                outlines.append((color, exterior, holes, label))

        draw = ImageDraw.Draw(result)
        for color, exterior, holes, label in outlines:
            if len(exterior) >= 2:
                draw.line(exterior + [exterior[0]], fill=color, width=width)
            for hole in holes:
                if len(hole) >= 2:
                    draw.line(hole + [hole[0]], fill=color, width=width)
            if label and exterior:
                center = (
                    sum(x for x, _ in exterior) / len(exterior),
                    sum(y for _, y in exterior) / len(exterior),
                )
                if len(exterior) >= 3:
                    shape = Polygon(exterior, [hole for hole in holes if len(hole) >= 3])
                    if not shape.is_valid:
                        shape = shape.buffer(0)
                    if not shape.is_empty:
                        if shape.geom_type == "MultiPolygon":
                            shape = max(shape.geoms, key=lambda part: part.area)
                        center = polylabel(shape, tolerance=1.0).coords[0]
                text_bbox = draw.textbbox(center, str(label), anchor="mm")
                draw.rectangle(text_bbox, fill="white")
                draw.text(center, str(label), fill="black", anchor="mm")

        return result

    def draw_obb_labels(
        self,
        image: Image.Image,
        rectangles: list[dict],
        label_key: str,
        save_path: Path | None = None,
        zoom=None
    ) -> Image.Image:
        image_with_labels = image.copy()
        draw = ImageDraw.Draw(image_with_labels)

        for rectangle in rectangles:
            label = rectangle.get(label_key)
            if label is None:
                continue
            
            if "bbox_pdf" in rectangle:
                bbox = self.pdf_obb_to_image_obb(
                    rectangle["bbox_pdf"],
                    zoom=zoom,
                )
            else:
                bbox = rectangle.get("bbox", rectangle)
            try:
                center_x = sum(float(bbox[f"x{i}"]) for i in range(1, 5)) / 4
                center_y = sum(float(bbox[f"y{i}"]) for i in range(1, 5)) / 4
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "Каждый OBB должен содержать числовые x1, y1 ... x4, y4"
                ) from exc

            text = str(label)
            text_bbox = draw.textbbox((center_x, center_y), text, anchor="mm")
            draw.rectangle(text_bbox, fill="white")
            draw.text((center_x, center_y), text, fill="black", anchor="mm")

        if save_path:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            image_with_labels.save(save_path)

        return image_with_labels

    def crop_image_around_rects(self,
        rectangles: list[dict],
        crop_size: int,
        outline: str = "red",
        width: int = 2,
        render_zoom: float = 4.0,
    ):
        self.pdf_to_base64(zoom=render_zoom)

        image_rectangles = [
            self.pdf_rect_to_image_rect(rect)
            for rect in rectangles
        ]
        crop_rect = {"x0": min(rect["x0"] for rect in image_rectangles) - crop_size,
                     "x1": max(rect["x1"] for rect in image_rectangles) + crop_size,
                     "y0": min(rect["y0"] for rect in image_rectangles) - crop_size,
                     "y1": max(rect["y1"] for rect in image_rectangles) + crop_size
                     }
        return self.render_rectangles_and_crop(image_rectangles, crop_rect, outline, width)

    def crop_side(self, side, zoom):
        self.pdf_to_base64(zoom=zoom)

        if side == "left":
            x0 = 0
            x1 = self.img.width / 2
        elif side == "right":
            x0 = self.img.width / 2
            x1 = self.img.width
        else:
            raise ValueError(f"Unknown side: {side}")

        y0 = 0
        y1 = self.img.height

        return self.crop_image(x0, y0, x1, y1)

    def split_image_to_tiles(
        self,
        drawing_bbox: dict | None,
        tile_width: int,
        tile_height: int,
        overlap_percent: float = 0,
        zoom: float | None = None,
        exclude_bboxes: list | None = None,
        dpi: int | None = None
    ) -> list[dict]:
        if tile_width <= 0 or tile_height <= 0:
            raise ValueError("tile_width and tile_height must be greater than 0")

        if overlap_percent < 0 or overlap_percent >= 100:
            raise ValueError("overlap_percent must be in range [0, 100)")

        if zoom and zoom != self.zoom:
            self.pdf_to_base64(zoom=zoom)

        if dpi:
            self.pdf_to_base64(dpi=dpi)

        if self.img is None:
            self.pdf_to_base64()

        crop_offset_x = 0
        crop_offset_y = 0

        if exclude_bboxes:
            _, self.img = self.render_obb_rectangles({"white": exclude_bboxes}, width=0, save_path=None, fill_opacity=1, zoom=self.zoom)

        if drawing_bbox:
            image_rect = self.pdf_rect_to_image_rect(drawing_bbox)
            crop_offset_x = max(0, int(image_rect["x0"]))
            crop_offset_y = max(0, int(image_rect["y0"]))
            _, drawing_img = self.crop_pdf_rect(drawing_bbox, zoom=self.zoom)
        else:
            drawing_img = self.img

        self.img = None

        image_width, image_height = drawing_img.size
        x_step = max(1, int(tile_width * (1 - overlap_percent / 100)))
        y_step = max(1, int(tile_height * (1 - overlap_percent / 100)))

        x_positions = list(range(0, image_width, x_step))
        y_positions = list(range(0, image_height, y_step))

        if x_positions[-1] + tile_width < image_width:
            x_positions.append(image_width - tile_width)
        if y_positions[-1] + tile_height < image_height:
            y_positions.append(image_height - tile_height)

        tiles = []
        seen_boxes = set()

        for y0 in y_positions:
            for x0 in x_positions:
                x0 = max(0, min(x0, image_width - tile_width))
                y0 = max(0, min(y0, image_height - tile_height))
                x1 = min(image_width, x0 + tile_width)
                y1 = min(image_height, y0 + tile_height)
                box = (x0, y0, x1, y1)

                if box in seen_boxes:
                    continue

                seen_boxes.add(box)
                tile_img = drawing_img.crop(box)
                tiles.append({
                    "x0": crop_offset_x + x0,
                    "y0": crop_offset_y + y0,
                    "x1": crop_offset_x + x1,
                    "y1": crop_offset_y + y1,
                    "image_base64": image_to_base64(tile_img),
                    "image": tile_img,
                })

        return tiles
    
    

        
    
    
