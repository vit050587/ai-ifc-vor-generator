import cv2
import numpy as np
import torch
from pathlib import Path
from PIL import Image
from torchvision import transforms
from torchvision.transforms import functional as TF
from torchvision.transforms import InterpolationMode

from config import settings

class DinoService:
    def __init__(
        self,
        model_path: str | Path = settings.DINO_HATCHING_MODEL,
        device: str | None = None,
        image_size: int = settings.DINO_IMAGE_SIZE,
    ):
        self.model_path = Path(model_path)
        self.device = device if device is not None else ("cuda" if torch.cuda.is_available() else "cpu")
        self.image_size = image_size
        self._model = None

        self.image_tf = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(
                mean=[0.485, 0.456, 0.406],
                std=[0.229, 0.224, 0.225],
            ),
        ])

        self.mask_tf = transforms.ToTensor()

    def _load_model(self):
        model = torch.jit.load(self.model_path, map_location=self.device)
        return model.to(self.device).eval()

    def _get_model(self):
        if self._model is None:
            self._model = self._load_model()

        return self._model

    def _create_full_mask(self, size: tuple[int, int]) -> Image.Image:
        return Image.new("L", size, 255)

    def _create_obb_mask(self, size: tuple[int, int], obb: list[float]) -> Image.Image:
        width, height = size
        points = np.array(obb, dtype=np.int32).reshape(4, 2)

        mask = np.zeros((height, width), dtype=np.uint8)
        cv2.fillPoly(mask, [points], 255)

        return Image.fromarray(mask)

    def _resize_and_pad_pair(
        self,
        image: Image.Image,
        mask: Image.Image,
    ) -> tuple[Image.Image, Image.Image]:
        width, height = image.size
        if mask.size != image.size:
            raise ValueError(
                f"Image and mask sizes differ: image={image.size}, mask={mask.size}"
            )

        scale = min(
            self.image_size / width,
            self.image_size / height,
        )
        resized_width = min(
            self.image_size,
            round(width * scale),
        )
        resized_height = min(
            self.image_size,
            round(height * scale),
        )
        resized_size = [resized_height, resized_width]

        image = TF.resize(
            image,
            resized_size,
            interpolation=InterpolationMode.BILINEAR,
            antialias=True,
        )
        mask = TF.resize(
            mask,
            resized_size,
            interpolation=InterpolationMode.NEAREST,
        )

        pad_x = self.image_size - resized_width
        pad_y = self.image_size - resized_height
        padding = [
            pad_x // 2,
            pad_y // 2,
            pad_x - pad_x // 2,
            pad_y - pad_y // 2,
        ]

        image = TF.pad(image, padding, fill=255)
        mask = TF.pad(mask, padding, fill=0)
        return image, mask

    def prepare_image_and_mask(
        self,
        image_source: str | Path | Image.Image,
        obb: list[float] | None = None,
    ):
        # Если не нашли в кеше считаем
        if isinstance(image_source, Image.Image):
            image = image_source.convert("RGB")
        else:
            image = Image.open(image_source).convert("RGB")

        if obb:
            mask = self._create_obb_mask(image.size, obb)
        else:
            mask = self._create_full_mask(image.size)

        image, mask = self._resize_and_pad_pair(image, mask)

        image_tensor = self.image_tf(image).unsqueeze(0).to(self.device)
        mask_tensor = self.mask_tf(mask).unsqueeze(0).to(self.device)

        result = (image_tensor, mask_tensor)

        return result

    @torch.no_grad()
    def predict_pair(
        self,
        plan_image: str | Path | Image.Image,
        plan_obb: list[float],
        image2: str | Path | Image.Image,
        image2_obb: list[float] | None = None,
    ) -> dict:
        plan_image_tensor, plan_mask_tensor = self.prepare_image_and_mask(
            image_source=plan_image,
            obb=plan_obb,
        )

        image2_tensor, image2_mask_tensor = self.prepare_image_and_mask(
            image_source=image2,
            obb=image2_obb,
        )
        return self.predict_pair_in_tensors(image2_tensor, image2_mask_tensor, plan_image_tensor, plan_mask_tensor)

        
    @torch.no_grad()
    def predict_pair_in_tensors(self, image2_tensor, image2_mask_tensor, plan_image_tensor, plan_mask_tensor):
        predictions = self.predict_pairs_in_tensors(
            image2_tensors=[image2_tensor],
            image2_mask_tensors=[image2_mask_tensor],
            plan_image_tensors=[plan_image_tensor],
            plan_mask_tensors=[plan_mask_tensor],
        )
        return predictions[0]

    @torch.no_grad()
    def predict_pairs_in_tensors(
        self,
        image2_tensors: list[torch.Tensor],
        image2_mask_tensors: list[torch.Tensor],
        plan_image_tensors: list[torch.Tensor],
        plan_mask_tensors: list[torch.Tensor],
        max_batch_size: int | None = None,
    ) -> list[dict]:
        tensor_count = len(image2_tensors)
        if not (
            tensor_count
            == len(image2_mask_tensors)
            == len(plan_image_tensors)
            == len(plan_mask_tensors)
        ):
            raise ValueError("All tensor lists must have the same length")
        if not image2_tensors:
            return []

        batch_size = max_batch_size or settings.DINO_MAX_BATCH_SIZE
        if batch_size < 1:
            raise ValueError("DINO batch size must be greater than 0")

        model = self._get_model()
        predictions: list[dict] = []

        for start in range(0, len(image2_tensors), batch_size):
            end = start + batch_size
            legend_images = torch.cat(image2_tensors[start:end], dim=0)
            legend_masks = torch.cat(image2_mask_tensors[start:end], dim=0)
            plan_images = torch.cat(plan_image_tensors[start:end], dim=0)
            plan_masks = torch.cat(plan_mask_tensors[start:end], dim=0)

            logits = model(
                legend_image=legend_images,
                legend_mask=legend_masks,
                plan_image=plan_images,
                plan_mask=plan_masks,
            )

            scores = torch.sigmoid(logits).detach().cpu().flatten().tolist()
            predictions.extend({"score": float(score)} for score in scores)

        return predictions
