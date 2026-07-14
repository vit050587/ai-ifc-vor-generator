import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw


DEFAULT_OUTPUT_DIR = Path("debug") / "dino_train"


def save_dino_train_sample(
    plan_image: str | Path | Image.Image,
    plan_obb: list[float],
    legend_image: str | Path | Image.Image | list,
    best_result: dict | None = None,
    legend_obb: list[float] | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> dict:
    output_dir = Path(output_dir)
    plan_dir = output_dir / "plan_images"
    plan_highlighted_dir = output_dir / "plan_highlighted"
    legend_dir = output_dir / "legend_images"
    legend_highlighted_dir = output_dir / "legend_highlighted"

    for directory in (
        plan_dir,
        plan_highlighted_dir,
        legend_dir,
        legend_highlighted_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    plan = _load_image(plan_image)
    plan_name = f"{_image_hash(plan)}.png"

    _save_image_once(plan, plan_dir / plan_name)
    _save_image_once(
        _draw_obb(plan, plan_obb),
        plan_highlighted_dir / plan_name,
    )

    for legend in _iter_legend_images(legend_image):
        legend_name = f"{_image_hash(legend)}.png"
        _save_image_once(legend, legend_dir / legend_name)

        if legend_obb:
            _save_image_once(
                _draw_obb(legend, legend_obb),
                legend_highlighted_dir / legend_name,
            )

    best_legend_name = None
    if best_result and best_result.get("legend_image") is not None:
        best_legend = _load_image(best_result["legend_image"])
        best_legend_name = f"{_image_hash(best_legend)}.png"
        _save_image_once(best_legend, legend_dir / best_legend_name)

    row = {
        "plan_image": str(Path("plan_images") / plan_name),
        "plan_highlighted": str(Path("plan_highlighted") / plan_name),
        "plan_obb": [float(value) for value in plan_obb],
        "legend_obb": (
            [float(value) for value in legend_obb]
            if legend_obb
            else None
        ),
        "wall_type": best_legend_name,
    }

    with (output_dir / "train.jsonl").open("a", encoding="utf-8") as train_file:
        train_file.write(json.dumps(row, ensure_ascii=False) + "\n")

    return row


def _load_image(image_source: str | Path | Image.Image) -> Image.Image:
    if isinstance(image_source, Image.Image):
        return image_source.convert("RGB")
    return Image.open(image_source).convert("RGB")


def _iter_legend_images(legend_source: str | Path | Image.Image | list):
    if isinstance(legend_source, list):
        for item in legend_source:
            if isinstance(item, dict):
                if "legend_symbols" in item:
                    for symbol in item["legend_symbols"]:
                        image = symbol.get("image")
                        if image is not None:
                            yield _load_image(image)
                    continue

                image = item.get("image")
                if image is not None:
                    yield _load_image(image)
            else:
                yield _load_image(item)
        return

    yield _load_image(legend_source)


def _image_hash(image: Image.Image) -> str:
    image = image.convert("RGB")
    digest = hashlib.sha256()
    digest.update(str(image.size).encode("utf-8"))
    digest.update(image.tobytes())
    return digest.hexdigest()[:24]


def _save_image_once(image: Image.Image, path: Path) -> None:
    if not path.exists():
        image.save(path)


def _draw_obb(image: Image.Image, obb: list[float]) -> Image.Image:
    highlighted = image.copy()
    draw = ImageDraw.Draw(highlighted)
    points = [
        (float(obb[index]), float(obb[index + 1]))
        for index in range(0, len(obb), 2)
    ]
    draw.line(points + [points[0]], fill="red", width=2)
    return highlighted
