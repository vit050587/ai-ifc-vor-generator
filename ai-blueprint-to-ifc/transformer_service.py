from typing import Any, Dict, List, Optional

from prompt_manager import PromptManager
from utils import _get_json_from_response

from config import settings


class TransformerService:
    def __init__(
        self,
        prompts_path: str,
        device_map: str = "auto",
        torch_dtype: Any = "auto",
        generation_options: Optional[Dict[str, Any]] = None,
    ):
        self.prompt_manager = PromptManager(prompts_path)
        self.prompt_manager.load_all()

        self.model_name = settings.TG_MODEL_DIR
        self.device_map = device_map
        self.torch_dtype = torch_dtype
        self.generation_options = generation_options or {
            "max_new_tokens": 600,
            "temperature": 0.0,
            "do_sample": False,
        }

        self.processor = None
        self.model = None

    def _load_model(self, model_name: Optional[str] = None):
        model_name = model_name or self.model_name
        if not model_name:
            raise ValueError("model_name is required")

        if self.model is not None and self.processor is not None and model_name == self.model_name:
            return

        from transformers import AutoProcessor

        try:
            from transformers import AutoModelForImageTextToText

            model_cls = AutoModelForImageTextToText
        except ImportError:
            from transformers import AutoModelForVision2Seq

            model_cls = AutoModelForVision2Seq

        self.model_name = model_name
        self.processor = AutoProcessor.from_pretrained(model_name)
        self.model = model_cls.from_pretrained(
            model_name,
            device_map=self.device_map,
            torch_dtype=self.torch_dtype,
        )

    def _move_inputs_to_model_device(self, inputs):
        if hasattr(inputs, "to"):
            return inputs.to(self.model.device)

        return {
            key: value.to(self.model.device) if hasattr(value, "to") else value
            for key, value in inputs.items()
        }

    def _build_messages(self, images: List[Any], prompt: str) -> List[Dict[str, Any]]:
        content = [{"type": "image", "image": image} for image in images]
        content.append({"type": "text", "text": prompt})

        return [
            {
                "role": "user",
                "content": content,
            }
        ]

    def _prepare_inputs(self, messages: List[Dict[str, Any]], images: List[Any]):
        try:
            return self.processor.apply_chat_template(
                messages,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
            )
        except TypeError:
            text = self.processor.apply_chat_template(
                messages,
                add_generation_prompt=True,
                tokenize=False,
            )
            return self.processor(
                text=[text],
                images=images,
                return_tensors="pt",
            )

    def _decode_response(self, inputs, generated_ids) -> str:
        input_ids = inputs.get("input_ids") if isinstance(inputs, dict) else inputs.input_ids
        generated_ids = generated_ids[:, input_ids.shape[-1] :]

        return self.processor.batch_decode(
            generated_ids,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )[0]

    def extract_from_images(
        self,
        images: List[Any],
        model_name: Optional[str],
        prompt_name: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> dict:
        self._load_model(model_name)

        prompt = self.prompt_manager.get_prompt(prompt_name)
        prompt = prompt.format(**(data or {}))

        print(f"Images count: {len(images)}")
        print("Sending request...")

        messages = self._build_messages(images, prompt)
        inputs = self._prepare_inputs(messages, images)
        inputs = self._move_inputs_to_model_device(inputs)

        generated_ids = self.model.generate(
            **inputs,
            **self.generation_options,
        )

        result_text = self._decode_response(inputs, generated_ids)

        return _get_json_from_response(result_text)

    def extract_from_drawing(
        self,
        image: Any,
        model_name: Optional[str],
        prompt_name: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> dict:
        return self.extract_from_images(
            [image],
            model_name,
            prompt_name,
            data=data,
        )
