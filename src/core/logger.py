import logging
import sys
from pathlib import Path


def setup_logger(name=None, log_file=None, level=logging.INFO, 
                 console=True, file_level=None):
    """
    Расширенная настройка логгера
    
    Args:
        name: имя логгера
        log_file: путь к файлу для логирования
        level: уровень логирования по умолчанию
        console: выводить ли в консоль
        file_level: отдельный уровень для файла (если None, равен level)
    """
    logger = logging.getLogger(name)
    
    if file_level is None:
        file_level = level
    
    # Предотвращаем дублирование
    if logger.handlers:
        return logger
    
    logger.setLevel(min(level, file_level))
    
    # Форматтер
    formatter = logging.Formatter(
        '%(asctime)s | %(name)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Консольный handler
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    # Файловый handler
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger