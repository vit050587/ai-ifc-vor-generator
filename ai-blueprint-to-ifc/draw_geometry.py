from pathlib import Path

import fitz
from PIL import Image, ImageDraw
from tqdm import tqdm


def render_rectangles_on_image(
    image: Image.Image | str | Path,
    rectangles: list[dict],
    out_path: str | Path | None = None,
    outline: str = "red",
    width: int = 2,
):
    if isinstance(image, Image.Image):
        img = image.copy()
    else:
        img = Image.open(image).convert("RGB")

    draw = ImageDraw.Draw(img)

    for rect in tqdm(
        rectangles,
        desc="Drawing rectangles",
        unit="rect",
    ):
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

    if out_path is not None:
        img.save(out_path)
        print(f"Done: {out_path}")

    return img


def render_rectangles_fast(
    pdf_path: str | Path,
    rectangles: list[dict],
    out_path: str | Path,
    page_index: int = 0,
    zoom: float = 2.0,
):
    doc = fitz.open(pdf_path)
    page = doc[page_index]

    rotation_matrix = page.rotation_matrix

    print("Рендер страницы...")
    pix = page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom),
        alpha=False,
    )

    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    draw = ImageDraw.Draw(img)

    for rect in tqdm(
        rectangles,
        desc="Отрисовка прямоугольников",
        unit="rect"
    ):
        r = fitz.Rect(
            rect["x0"],
            rect["y0"],
            rect["x1"],
            rect["y1"],
        ) * rotation_matrix

        draw.rectangle(
            [
                r.x0 * zoom,
                r.y0 * zoom,
                r.x1 * zoom,
                r.y1 * zoom,
            ],
            outline="red",
            width=2,
        )

    print("Сохранение...")
    img.save(out_path)

    print(f"Готово: {out_path}")
    return img
