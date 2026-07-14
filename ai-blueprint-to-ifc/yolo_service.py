from __future__ import annotations

import numpy as np
import supervision as sv
import base64
import io
from pathlib import Path
from typing import Any, Mapping, Sequence

from rectangle_utils import rectangles_to_yolo_obb


class YoloService:
    """Обертка над Ultralytics YOLO для получения bbox из изображения."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        device: str | int | None = None,
    ) -> None:
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise ImportError(
                "Для YoloService установите ultralytics: pip install ultralytics"
            ) from exc

        self.model_path = (
            Path(model_path)
            if model_path is not None
            else Path(__file__).resolve().with_name("best.pt")
        )
        self.device = device
        self.model = YOLO(str(self.model_path))

    def detect(
        self,
        image: Any,
        *,
        confidence: float = 0.25,
        iou: float = 0.7,
        class_iou_thresholds: Mapping[int, float] | None = None,
        imgsz: int | tuple[int, int] = 736,
        classes: Sequence[int] | None = None,
        max_det: int = 300,
        agnostic_nms: bool = False,
        normalized: bool = False,
        save_debug_dir:Path | None = None,
        **predict_options: Any,
    ) -> list[dict[str, Any]]:
        """
        Параметры:
            image: изображение для детекции: путь, PIL.Image, numpy.ndarray,
                bytes или base64/data URL.
            confidence: минимальная уверенность детекции от 0 до 1.
            iou: IoU-порог для NMS от 0 до 1; чем ниже, тем сильнее
                удаляются пересекающиеся bbox.
            class_iou_thresholds: дополнительные IoU-пороги NMS по id класса.
                Классы, отсутствующие в словаре, повторно не фильтруются.
            imgsz: размер входа модели: одно число или пара (width, height).
            classes: список id классов, которые нужно искать. Если None,
                ищутся все классы.
            max_det: максимальное количество объектов на изображение.
            agnostic_nms: если True, NMS объединяет пересечения без учета
                класса.
            normalized: если True, добавляет bbox_normalized с координатами
                от 0 до 1.
            predict_options: дополнительные параметры для
                ultralytics.YOLO.predict.

        Выполняет детекцию и возвращает список найденных объектов.

        image:
            Путь, PIL.Image, numpy.ndarray, bytes или base64/data URL.
        normalized:
            Если True, bbox дополнительно содержит координаты от 0 до 1.
        predict_options:
            Остальные параметры ``ultralytics.YOLO.predict``.

        Возвращает список объектов:
            [
                {
                    "class_id": 0,
                    "class_name": "wall",
                    "confidence": 0.91,
                    "bbox": {
                        "x1": 10.0,
                        "y1": 20.0,
                        "x2": 110.0,
                        "y2": 220.0,
                        "width": 100.0,
                        "height": 200.0,
                    },
                    # Только если normalized=True.
                    "bbox_normalized": {
                        "x1": 0.01,
                        "y1": 0.02,
                        "x2": 0.11,
                        "y2": 0.22,
                        "width": 0.10,
                        "height": 0.20,
                    },
                }
            ]
        """
        if not 0 <= confidence <= 1:
            raise ValueError("confidence должен быть в диапазоне от 0 до 1")
        if not 0 <= iou <= 1:
            raise ValueError("iou должен быть в диапазоне от 0 до 1")
        if max_det < 1:
            raise ValueError("max_det должен быть больше 0")

        source = self._prepare_image(image)
        options: dict[str, Any] = {
            "source": source,
            "conf": confidence,
            "iou": iou,
            "imgsz": imgsz,
            "classes": list(classes) if classes is not None else None,
            "max_det": max_det,
            "agnostic_nms": agnostic_nms,
            "verbose": False,
        }
        if self.device is not None:
            options["device"] = self.device
        options.update(predict_options)

        results = self._apply_class_nms(
            list(self.model.predict(**options)),
            class_iou_thresholds=class_iou_thresholds,
        )

        detections: list[dict[str, Any]] = []

        for r_i, result in enumerate(results):
            if save_debug_dir:
                result.save(filename=str(save_debug_dir / f"{r_i}.png"))

            names = result.names
            obb = getattr(result, "obb", None)
            # Если модель передает obb
            if obb is not None:
                obb_points = obb.xyxyxyxy.detach().cpu().tolist()
                confidences = obb.conf.detach().cpu().tolist()
                class_ids = obb.cls.detach().cpu().tolist()
                normalized_obb_points = (
                    obb.xyxyxyxyn.detach().cpu().tolist()
                    if normalized and hasattr(obb, "xyxyxyxyn")
                    else [None] * len(obb_points)
                )

                for points, score, class_id, norm_points in zip(
                    obb_points, confidences, class_ids, normalized_obb_points
                ):
                    class_id = int(class_id)
                    flat_points = [
                        float(value)
                        for point in points
                        for value in (
                            point if isinstance(point, (list, tuple)) else [point]
                        )
                    ]
                    detection: dict[str, Any] = {
                        "class_id": class_id,
                        "class_name": self._class_name(names, class_id),
                        "confidence": float(score),
                        "bbox": {
                            f"{axis}{point_index}": flat_points[
                                (point_index - 1) * 2 + axis_index
                            ]
                            for point_index in range(1, 5)
                            for axis_index, axis in enumerate(("x", "y"))
                        },
                    }

                    if norm_points is not None:
                        flat_norm_points = [
                            float(value)
                            for point in norm_points
                            for value in (
                                point if isinstance(point, (list, tuple)) else [point]
                            )
                        ]
                        detection["bbox_normalized"] = {
                            f"{axis}{point_index}": flat_norm_points[
                                (point_index - 1) * 2 + axis_index
                            ]
                            for point_index in range(1, 5)
                            for axis_index, axis in enumerate(("x", "y"))
                        }

                    detections.append(detection)
                continue

            boxes = result.boxes
            if boxes is None:
                continue

            xyxy = boxes.xyxy.detach().cpu().tolist()
            confidences = boxes.conf.detach().cpu().tolist()
            class_ids = boxes.cls.detach().cpu().tolist()
            xyxyn = (
                boxes.xyxyn.detach().cpu().tolist()
                if normalized
                else [None] * len(xyxy)
            )

            for coords, score, class_id, norm_coords in zip(
                xyxy, confidences, class_ids, xyxyn
            ):
                class_id = int(class_id)
                x1, y1, x2, y2 = (float(value) for value in coords)
                detection: dict[str, Any] = {
                    "class_id": class_id,
                    "class_name": self._class_name(names, class_id),
                    "confidence": float(score),
                    "bbox": {
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                        "width": x2 - x1,
                        "height": y2 - y1,
                    },
                }
                if norm_coords is not None:
                    nx1, ny1, nx2, ny2 = (
                        float(value) for value in norm_coords
                    )
                    detection["bbox_normalized"] = {
                        "x1": nx1,
                        "y1": ny1,
                        "x2": nx2,
                        "y2": ny2,
                        "width": nx2 - nx1,
                        "height": ny2 - ny1,
                    }
                detections.append(detection)

        detections = rectangles_to_yolo_obb(detections)
        return detections

    def detect_many(
        self,
        images: Sequence[Any],
        **detect_options: Any,
    ) -> list[list[dict[str, Any]]]:
        """Выполняет детекцию для нескольких изображений."""
        return [self.detect(image, **detect_options) for image in images]

    @staticmethod
    def _apply_class_nms(
        results: Sequence[Any],
        *,
        class_iou_thresholds: Mapping[int, float] | None,
    ) -> list[Any]:
        if not class_iou_thresholds:
            return list(results)

        filtered_results = []
        for result in results:
            detections = sv.Detections.from_ultralytics(result)
            if len(detections) == 0:
                filtered_results.append(result)
                continue

            detections.data["source_index"] = np.arange(len(detections), dtype=int)
            filtered_classes = []

            for class_id in sorted({int(value) for value in detections.class_id}):
                class_detections = detections[detections.class_id == class_id]
                if class_id in class_iou_thresholds:
                    class_detections = class_detections.with_nms(
                        threshold=class_iou_thresholds[class_id],
                        class_agnostic=True,
                    )
                filtered_classes.append(class_detections)

            filtered_detections = sv.Detections.merge(filtered_classes)
            source_indices = sorted(
                int(index)
                for index in filtered_detections.data["source_index"]
            )
            filtered_result = result.new()
            if result.obb is not None:
                filtered_result.obb = result.obb[source_indices]
            elif result.boxes is not None:
                filtered_result.boxes = result.boxes[source_indices]
                if result.masks is not None:
                    filtered_result.masks = result.masks[source_indices]
                if result.keypoints is not None:
                    filtered_result.keypoints = result.keypoints[source_indices]

            filtered_results.append(filtered_result)

        return filtered_results

    @staticmethod
    def _prepare_image(image: Any) -> Any:
        from PIL import Image

        if isinstance(image, Path):
            return str(image)
        if isinstance(image, bytes):
            return Image.open(io.BytesIO(image)).convert("RGB")
        if isinstance(image, str):
            if not image.startswith("data:"):
                try:
                    path = Path(image)
                    if path.exists():
                        return str(path)
                except OSError:
                    pass

            encoded = image.split(",", 1)[1] if image.startswith("data:") else image
            try:
                raw = base64.b64decode(encoded, validate=True)
                return Image.open(io.BytesIO(raw)).convert("RGB")
            except Exception as exc:
                raise ValueError(
                    "Строка image должна быть существующим путем или корректным base64"
                ) from exc
        return image

    @staticmethod
    def _class_name(names: Any, class_id: int) -> str:
        if isinstance(names, dict):
            return str(names.get(class_id, class_id))
        if isinstance(names, (list, tuple)) and class_id < len(names):
            return str(names[class_id])
        return str(class_id)
