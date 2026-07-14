from pathlib import Path
from langchain_core.prompts import PromptTemplate
from src.core.logger import setup_logger

logger = setup_logger("prompt_manager")

class PromptManager:
    def __init__(self, prompts_dir: str = "prompts"):
        self.prompts_dir = prompts_dir
        self.prompts = {}

    def load_all(self):
        """Загружает промпты из папки"""
        prompts_path = Path(self.prompts_dir)

        prompts_path.mkdir(exist_ok=True)

        # Загружаем все .txt файлы
        for file_path in prompts_path.glob("*.txt"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    prompt_name = file_path.stem
                    self.prompts[prompt_name] = f.read()
                    logger.info(f"Загружен промт: {prompt_name}")
            except Exception as e:
                logger.error(f"Ошибка загрузки промта {file_path}: {e}")

    def get_prompt(self, name: str):
        if name not in self.prompts:
            logger.error(f"Промт '{name}' не найден")
            raise ValueError(f"Промт '{name}' не найден")
        return self.prompts[name]

    def get_template(self, name: str):
        return PromptTemplate.from_template(self.get_prompt(name))
