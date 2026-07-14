from typing import Dict, Any, List, Optional
import threading
import time
import json
import re
import demjson3
from json_repair import repair_json
import io
import base64
from PIL import Image

from logger import setup_logger

logger = setup_logger(__name__)

MAX_RETRIES=3
RETRY_DELAY=1.0
LLM_HARD_TIMEOUT = 300


def rectangles_intersect(rect_a: Dict[str, Any], rect_b: Dict[str, Any]) -> bool:
    """Returns True if two rectangles overlap by area."""
    a_x0, a_x1 = sorted((rect_a["x0"], rect_a["x1"]))
    a_y0, a_y1 = sorted((rect_a["y0"], rect_a["y1"]))
    b_x0, b_x1 = sorted((rect_b["x0"], rect_b["x1"]))
    b_y0, b_y1 = sorted((rect_b["y0"], rect_b["y1"]))

    return a_x0 < b_x1 and a_x1 > b_x0 and a_y0 < b_y1 and a_y1 > b_y0


def find_intersecting_rectangles(
    rectangles: List[Dict[str, Any]],
    target_rectangle: Dict[str, Any],
) -> List[Dict[str, Any]]:
    return [
        rectangle
        for rectangle in rectangles
        if rectangle is not target_rectangle
        and rectangles_intersect(rectangle, target_rectangle)
    ]

def _invoke_chain_with_timeout(chain, data: Dict[str, Any], timeout: float) -> str:
    """Вызывает chain.invoke в отдельном daemon-потоке с жёстким таймаутом"""
    result_holder: list = [None]
    error_holder: list = [None]

    def target():
        try:
            result_holder[0] = chain.invoke(data)
        except Exception as exc:
            error_holder[0] = exc

    tread = threading.Thread(target=target, daemon=True)
    tread.start()
    tread.join(timeout)

    if tread.is_alive():
        # Логируем утечку
        logger.error(
            f"Жесткий тайм-аут после {timeout}s - произошла утечка фонового потока"
        )
        raise TimeoutError(f"LLM hard-timeout after {timeout}s")

    if error_holder[0] is not None:
        raise error_holder[0]

    return result_holder[0]


def invoke_with_retry(chain, data: Dict[str, Any]) -> str:
    """Вызывает цепочку с retry логикой и подставляет пустые переменные при необходимости"""
    for attempt in range(MAX_RETRIES):
        try:
            # 🔹 Автоматически добавляем недостающие переменные
            expected_vars = getattr(chain, "input_variables", [])
            if "characteristics" in expected_vars and "characteristics" not in data:
                data["characteristics"] = "[]"

            return _invoke_chain_with_timeout(chain, data, timeout=LLM_HARD_TIMEOUT)

        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
            else:
                logger.error(
                    f"Failed after {MAX_RETRIES} attempts: {str(e)}")
                raise Exception(
                    f"Failed after {MAX_RETRIES} attempts: {str(e)}"
                )
            
def _get_json_from_response(response, stub = {}):
    """
    Получает json из ответа llm
    """
    def _parse_text(text):
        try:
            parsed_json, errors, stats = demjson3.decode(text, strict=False, return_errors=True)
            # Пытаемся конвертировать в json чтобы выявить ошибку. И отлавливаем в try
            json.dumps(parsed_json, ensure_ascii=False, indent=2)
            if errors:
                # Были ошибки
                logger.warning("Во время обработки json исправлены ошибки:\n" + "\n".join(str(e) for e in errors))
                logger.debug(f"Входной текст: \n{text}")

            return parsed_json, None

        except (json.JSONDecodeError, TypeError) as e:
            return None, e
    
    # Очистка json
    clean_text = response.strip().replace("```json", "").replace("```", "").strip()
    #Убираем висячие запятые
    clean_text = re.sub(r',\s*([\]}])', r'\1', clean_text)

    parsed_json, error = _parse_text(clean_text)
    if parsed_json is None:
        # Пытаемся исправить json
        clean_text = repair_json(clean_text)
        parsed_json, error = _parse_text(clean_text)
        if parsed_json is None:
            logger.error(f"Ошибка парсинга json: {error}")
            return stub
        else:
            return parsed_json
    else:
        return parsed_json

def image_to_base64(img: Image.Image) -> str:
    buffer = io.BytesIO()

    img.save(buffer, format="PNG")

    return base64.b64encode(
        buffer.getvalue()
    ).decode("utf-8")

def execute_llm_chain(chain, params = {}, stub = {}):
    response = invoke_with_retry(chain,params)
    return _get_json_from_response(response, stub=stub)
