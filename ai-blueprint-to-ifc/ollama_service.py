from prompt_manager import PromptManager
import ollama
import json
from utils import _get_json_from_response
from joblib import Memory
from config import settings

memory = Memory("cache", verbose=0)

class OllamaService:
    def __init__(self, prompts_path):
        self.prompt_manager = PromptManager(prompts_path)
        self.prompt_manager.load_all()

    def extract_from_drawing(self, img_base64,  model_name, prompt_name, data = {}) -> dict:
        print(f"Размер изображения: {len(img_base64) / 1024 / 1024:.2f} MB")

        prompt = self.prompt_manager.get_prompt(prompt_name)
        prompt = prompt.format(**data)

        return self._extract_from_drawing_with_cache(prompt, img_base64, model_name)

    @staticmethod
    @memory.cache
    def _extract_from_drawing_with_cache(prompt, img_base64,  model_name) -> dict:
        client = ollama.Client(host=settings.OLLAMA_BASE_URL)
        response = client.chat(
            model=model_name,
            messages=[{'role': 'user', 'content': prompt, 'images': [img_base64]}],
            options={'temperature': 0.0, 'num_predict': 16000}
        )

        result_text = response['message']['content']

        return _get_json_from_response(result_text)

    def extract_from_images(self, images_base64, model_name, prompt_name, data = {}) -> dict:
        total_size_mb = sum(len(img_base64) for img_base64 in images_base64) / 1024 / 1024
        print(f"Images size: {total_size_mb:.2f} MB")

        prompt = self.prompt_manager.get_prompt(prompt_name)
        prompt = prompt.format(**data)

        print("Sending request...")
        client = ollama.Client(host=settings.OLLAMA_BASE_URL)
        response = client.chat(
            model=model_name,
            messages=[{'role': 'user', 'content': prompt, 'images': images_base64}],
            options={'temperature': 0.0, 'num_predict': 16000}
        )

        result_text = response['message']['content']

        return _get_json_from_response(result_text)
