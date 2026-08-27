# KODA.md — проект ai-ifc-vor-generator

Инструкционный контекст для ИИ-агентов и разработчиков, работающих с репозиторием.

---

## 1. Обзор проекта

**ai-ifc-vor-generator** — веб-сервис автоматической генерации **видов работ (ВОР)**
для смет из BIM-моделей. На вход принимает **IFC-файлы** (`.ifc`) и **PDF-чертежи**
(`.pdf`), извлекает строительные элементы, группирует их и формирует финальный
**перечень работ** (Excel) с объёмами и стоимостью.

Ключевые особенности:

- **Два режима обработки**:
  - **KR** — конструктивные решения (ж/б конструкции: стены, колонны, перекрытия, балки и т. д.). Подбор работ выполняется через **внешний API цифрового справочника ТСН** (не через LLM).
  - **AR** — архитектурные решения (окна, двери, покрытия, кровля, отделка и т. д.). Подбор работ выполняется **локальной LLM** (YandexGPT через Ollama) в четыре этапа.
- **Асинхронная обработка**: после загрузки файла сервис сразу возвращает `sessionId`, тяжёлая обработка выполняется в фоновых потоках (`threading.Thread`), пользователь опрашивает статус через API.
- **Два источника данных**: IFC (через `ifcopenshell`) или PDF-чертёж (через пайплайн машинного зрения `ai-blueprint-to-ifc`).
- **Хранилище без СУБД**: состояние сессий — JSON-файл `outputs/sessions.json` (потокобезопасный, атомарная запись); файлы результатов — файловая система `outputs/<session_id>/`.
- **Авторизация** через flask-login (встроенные пользователи) + защита API.
- Развёртывание — **Docker Compose** (`web` + `ollama`), GPU NVIDIA.

---

## 2. Технологический стек

- **Backend**: Flask 3.1, gunicorn 26, flasgger (Swagger), Flask-Login
- **Валидация**: Pydantic 2 (схемы в `src/schemas.py`, ответы в camelCase)
- **IFC**: ifcopenshell 0.8.5 (парсинг, геометрия, экспорт GLB)
- **Данные/Excel**: pandas 3, openpyxl, PyMuPDF, pdf2image, pillow, opencv
- **LLM**: ollama (py-клиент), langchain/langgraph, промпты в `prompts/`
- **Машинное зрение**: ultralytics (YOLO OBB/layout/legend), supervision,
  DINO (TorchScript), transformers (VLM `GreenMap/qwen3-vl-4b-ru-blueprint-extractor`)
- **Прочее**: pymorphy3, fuzzywuzzy, requests, httpx, tenacity, shapely
- **Инфраструктура**: Docker / docker-compose, Nginx (проксирование `/ifc-vor/`), CUDA 12.9, Python 3.11

Пакеты зафиксированы с точными версиями в `requirements.txt`.

---

## 3. Структура проекта

```
ai-ifc-vor-generator/
├── Dockerfile                 # Образ сервиса (CUDA 12.9, Python 3.11, torch cu126)
├── docker-compose.yml         # Сервисы: ollama (GPU, порт 11450→11434) + web (6001→6000)
├── nginx.conf                 # Проксирование /ifc-vor/ → http://127.0.0.1:6001
├── Makefile                   # make up / down / restart / logs / clean
├── start.sh                   # Локальный запуск Flask (загрузка .env, активация .venv)
├── requirements.txt           # Python-зависимости (пиннинг версий)
├── .env                       # Переменные окружения (НЕ коммитить/не выводить в логи)
│
├── src/                       # Основной сервис (Flask)
│   ├── __init__.py            # create_app(): конфиг, LoginManager, blueprint, Swagger
│   ├── wsgi.py                # Точка входа gunicorn (app = create_app())
│   ├── routes.py              # Все HTTP-эндпоинты (REST API, ~1700 строк)
│   ├── schemas.py             # Pydantic-схемы запросов/ответов (CamelModel)
│   ├── templates/
│   │   └── index.html         # Веб-интерфейс (SPA на JS)
│   ├── core/
│   │   ├── config.py          # load_config() — настройки из env (Dataclass Config)
│   │   ├── logger.py          # setup_logger() — консоль + файловый лог
│   │   ├── prompt_manager.py  # Загрузка .txt промптов из prompts/
│   │   └── keycloak.py        # Провайдер Bearer-токена Keycloak (client_credentials)
│   └── services/              # Бизнес-логика и пайплайн
│       ├── session_manager.py # SessionManager: сессии, runs, фоновые потоки, sessions.json
│       ├── zero_step.py       # Извлечение элементов из IFC (КР/АР), нормализация, XLSX
│       ├── ifc_raw_dump.py    # «Сырой» дамп свойств/QTO/материалов + расчёт по bbox
│       ├── ifc_reference_builder.py # JSON-справочники (все элементы / группы)
│       ├── first_etap.py      # Этап 1 (АР): анализ элемента LLM
│       ├── second_etap.py     # Этап 2 (АР): фильтрация по части здания
│       ├── third_etap.py      # Этап 3 (АР): фильтрация по высоте (+LLM)
│       ├── fourth_etap.py     # Этап 4 (АР): подбор работ + объём/стоимость
│       ├── base_knowledge.py  # База ключевых слов работ для подбора
│       ├── geometry_filter.py # Фильтрация работ по геометрии элемента
│       ├── group_excel.py     # Группировка элементов (КР и АР), правила группировки
│       ├── api_works_lookup.py# Подбор работ через API ТСН (КР)
│       ├── materials_lookup.py# Карта МССК-кодов материалов (АР)
│       ├── mssk_lookup.py     # Карта МССК-кодов элементов
│       ├── pdf_processor.py   # Обёртка пайплайна обработки PDF
│       └── serializer.py      # Экспорт IFC → GLB (3D-модель по запросу)
│
├── ai-blueprint-to-ifc/       # Пайплайн машинного зрения для PDF-чертежей
│   ├── config.py              # Settings (Pydantic BaseSettings), профили YOLO, пороги
│   ├── processor.py           # Оркестратор обработки чертежа (класс Processor)
│   ├── pdf_prcoessor.py       # Конвертация PDF → изображения, тайлы (опечатка в имени)
│   ├── walls_processor.py     # Детекция стен (YOLO OBB)
│   ├── hatching_processor.py  # Анализ штриховок и расшифровка материалов
│   ├── dino_service.py        # DINO-модель (сравнение символов легенды)
│   ├── yolo_service.py        # Обёртка над YOLO/ultralytics
│   ├── transformer_service.py # VLM (GreenMap qwen3-vl) для извлечения данных
│   ├── ollama_service.py      # Обёртка над Ollama (VLM Qwen3-VL)
│   ├── layout_processor.py    # Детекция layout (чертёж/легенда)
│   ├── legend_layout_processor.py # Разбор строк легенды
│   ├── drawing_statistics_analyzer.py # Статистика уверенности обработки
│   ├── result_former.py       # Формирование DataFrame результатов
│   ├── rectangle_utils.py     # Работа с OBB-прямоугольниками
│   ├── draw_geometry.py       # Отрисовка размеченных чертежей
│   ├── dino_train_creator.py  # Подготовка данных для обучения DINO
│   ├── models/                # Веса ML-моделей (yolo_walls_obb.pt, yolo_layout.pt,
│   │                          #   yolo_legend_layout.pt, dino_hatching.pt)
│   ├── prompts/               # Промпты VLM для чертежей (get_scale.txt, get_text_from_image.txt)
│   └── logger.py, utils.py, run.py, debug_manager.py  # Вспомогательные модули
│
├── prompts/                   # Промпты пайплайна (src)
│   └── element_analyze.txt    # Промпт этапа 1 (нормализация характеристик элемента)
│
├── data/                      # Справочники (только чтение!)
│   ├── perechen_kr.xlsx, perechen_kr_1.xlsx  # Перечни работ (КР)
│   ├── perechen_ar.xlsx                      # Перечень работ (АР)
│   ├── koefs.xlsx                            # Нормы расхода (корректировка объёма)
│   ├── price_cost.xlsx                       # Стоимость расценок (Шифр → прямые затраты)
│   ├── elements_mssk.xlsx / elements_mssk_nested.json  # МССК-справочник элементов
│   └── materials_mssk.xlsx / materials_mssk_nested.json # МССК-справочник материалов
│
├── uploads/                   # Загруженные пользователем файлы (по сессиям)
├── outputs/                   # Результаты обработки
│   ├── sessions.json          # «База» всех сессий (состояние, runs, файлы)
│   └── <session_id>/
│       ├── original/          # Исходные файлы (IFC/PDF, Excel, GLB, разметки)
│       ├── run_<NNN>/         # Результаты запуска NNN
│       └── ...справочные и промежуточные JSON/XLSX...
│
├── KODA.md                    # Настоящий файл (инструкция для агентов)
├── .venv/                     # Локальное виртуальное окружение Python
└── .gitignore, .gitattributes, ai-ifc-vor-generator.code-workspace
```

Примечания:

- `.env` содержит секреты (токены, пароли Keycloak) — **не** выводить в логи, не коммитить.
- `models/` в `ai-blueprint-to-ifc` — веса ML-моделей, изменение запрещено, только использование.

---

## 4. Сборка и запуск

### 4.1. Docker (основной способ)

Требования: Docker + Docker Compose, GPU NVIDIA (CUDA). Порт наружу — `6001`, Ollama — `11450`.

```bash
make up        # собрать и запустить (docker-compose up -d --build + follow логи)
make down      # остановить
make restart   # перезапустить (down + up)
make logs      # логи всех сервисов
make clean     # остановить и удалить volumes (внимание: удаляет ollama_data)
```

### 4.2. Локальная разработка вне Docker

- Виртуальное окружение `.venv/` (все команды — только через него).
- Запуск сервера: `./start.sh` (активирует `.venv`, загружает `.env`); порт по умолчанию `6005`. Альтернатива — `flask run` напрямую.
- Установка зависимостей: `./.venv/bin/pip install -r requirements.txt`.
- Запуск скриптов: `./.venv/bin/python <script.py>`.
- Запуск отдельного модуля: `./.venv/bin/python -m <модуль>`.
- Вне Docker Ollama должна быть доступна по `OLLAMA_BASE_URL`; сам Ollama не запускается вручную — только через docker-compose.

### 4.3. Тестирование

Готовая тестовая инфраструктура (директория `tests/`) **отсутствует**. При добавлении новой логики желательно сопровождать её тестами в стиле проекта.

### 4.4. Доступные точки

- Веб-интерфейс: `/ifc-vor/`
- Swagger UI: `/ifc-vor/docs`, спецификация: `/ifc-vor/apispec.json`
- Здоровье: `GET /ifc-vor/api/health`

---

## 5. Пайплайн обработки

### 5.1. Уровень сессии/запусков

1. **Загрузка файла** → `POST /api/upload_ifc` (multipart/form-data, `processingType=KR|AR`). Возвращает `sessionId` + `status`.
2. **Фоновая обработка**: `SessionManager._process_ifc_bg` (для IFC) или `SessionManager._process_pdf_bg` (для PDF).
3. Статус `selecting_rows` — пользователь смотрит превью `ДЛЯ_СМЕТЧИКА_исправленный.xlsx`, указывает части здания/материалы/высоту.
4. **Выбор строк** → `POST /api/session/<id>/select_rows` (или `new_run`) → создаётся `run_<NNN>/` и в фоновом потоке запускается конвейер `_run_processing_pipeline_in_run`.
5. Каждый запуск хранит собственные файлы в `run_<NNN>/`; пользователь может переключаться между запусками (`switch_run`).
6. Просмотр/скачивание файлов (`download`, `download_all`), для IFC — 3D-модель GLB по запросу.

### 5.2. Исходный источник: IFC

`zero_step(ifc_path, output_folder=..., processing_type=...)`:
- Извлечение элементов по IFC-классам: КР — `IfcWall`, `IfcFooting`, `IfcSlab`, `IfcColumn`, `IfcBeam`, `IfcStair`, `IfcPile`, `IfcCovering` (изоляция) и др.; АР — расширенный набор: окна, двери, кровля, покрытия, перила, мебель, прокси-элементы и др. (`_ARCH_TYPES`).
- Нормализация характеристик, расчёт количеств QTO/по bbox (`ifc_raw_dump._compute_bbox_quantities`).
- Формирование XLSX-таблиц `ДЛЯ_СМЕТЧИКА_исправленный.xlsx`; сырой дамп `IFC_исходные_параметры.xlsx/.json`; JSON-справочники `ifc_elements_output.json`, `ifc_raw_elements_grouped.json`.

### 5.3. Исходный источник: PDF (машинное зрение)

`src/services/pdf_processor.process_pdf` → конвейер `ai-blueprint-to-ifc/processor.py`:
- Конвертация PDF → изображения/тайлы, детекция стен (YOLO OBB), layout, легенды.
- Анализ штриховок и расшифровка материалов (DINO + VLM Qwen3-VL), масштабы/легенды.
- Результат — те же Excel/JSON, что и для IFC, плюс размеченный чертёж (`blueprint_painted.png`) и условные обозначения (`materials_colors.md`).
- Объединение номерных листов (`Данные_N` → `Данные`), режим `reference_only` (только JSON-справочники).

### 5.4. Подбор работ

- **КР**: фильтр выбранных строк по типам/материалам → группировка (`group_excel.process_ifc_excel`) → конвертация в `ifc_raw_elements_grouped.json` → POST-запросы в API ТСН (`digital-collection/building-elements/positions`) → формирование `ОБЩИЙ_Финальный_перечень_работ.xlsx` (объём корректируется по `koefs.xlsx`, стоимость — по `price_cost.xlsx`).
- **АР**: четырёхэтапный LLM-пайплайн:
  1. `first_etap` — нормализация характеристик элемента LLM (промпт `element_analyze.txt`);
  2. `second_etap` — фильтрация работ по части здания (надземная/подземная/цоколь);
  3. `third_etap` — фильтрация по высоте (паттерны + LLM-проверка, высота типового этажа);
  4. `fourth_etap` — подбор работ по материалу/ключевым словам + LLM-отбор, расчёт объёмов и стоимости.
- Все артефакты (XLSX, JSON, дампы) сохраняются в `run_<NNN>/` каждого запуска.

---

## 6. Хранилища и справочники

Сервис **не использует классическую СУБД**.

| Хранилище | Описание |
|---|---|
| `outputs/sessions.json` | Метаданные сессий, запусков, файлов, прогресс. Потокобезопасно (`RLock`), атомарная запись через tmp-файл + `os.replace`, резервное копирование при повреждении |
| `outputs/<session_id>/` | Файлы результатов сессии (`original/`, `run_<NNN>/`, справочники) |
| `uploads/<session_id>/` | Загруженные пользователем файлы |
| `data/` | Статические справочники: перечни (KR/AR), `koefs.xlsx` (нормы расхода), `price_cost.xlsx` (стоимость), `elements_mssk*`, `materials_mssk*` |
| **Ollama** | Локальная LLM (Qwen3-VL-8B — чертежи, YandexGPT-5-Lite-8B — этапы АР) |
| **API ТСН** (`normativ.mgexp.org/...`) | Подбор работ в режиме КР |
| **Keycloak** (`normativ-idm.mgexp.org/...`) | Выдача/обновление Bearer-токена (client_credentials) |

Основные параметры конфигурации: `src/core/config.py` (Dataclass `Config`, `load_config()`), `ai-blueprint-to-ifc/config.py` (Pydantic `Settings`).

---

## 7. API (кратко)

Префикс: `/ifc-vor`. Все вызовы, кроме авторизации и документации, требуют авторизации (cookie сессии; для `/api/*` без неё — `401 {"detail": "..."}`).

- Здоровье: `GET /api/health`
- Авторизация: `GET/POST /login`, `GET /logout`
- Загрузка: `POST /api/upload_ifc` (file + processingType), `POST /api/reference`
- Сессии: `GET /api/sessions`, `GET /api/session/<id>`, `GET /api/session/<id>/status`, `DELETE /api/session/<id>`
- Обработка: `POST /api/session/<id>/select_rows`, `POST /api/session/<id>/new_run`, `GET /api/session/<id>/runs`, `POST /api/session/<id>/switch_run/<run_id>`, `POST /api/session/<id>/filter_height`
- Файлы: `GET /api/session/<id>/preview`, `preview_result/<filename>`, `blueprint_image`, `materials_md`, `download/<filename>`, `download_all`
- 3D: `POST /api/session/<id>/3d_model`, `GET /api/session/<id>/3d_model/status`

Форматы: ответы — JSON в **camelCase** (Pydantic `CamelModel`); ошибки — `{"detail": "..."}` (400/401/404/409/413/422/500); загрузка — multipart/form-data; выбор строк/запуски — JSON-тело.

---

## 8. Правила разработки

1. **Виртуальное окружение**: все команды — через `.venv/bin/pip` и `.venv/bin/python`. Системный Python не используется.
2. **Стиль**: PEP8, докстринги для новых функций/классов, комментарии — на русском или английском (как принято в проекте). Имена выходных файлов — **на русском** (`ДЛЯ_СМЕТЧИКА_...`, `Дерево_проекта...`, `ОБЩИЙ_Финальный_перечень_работ.xlsx`).
3. **Обратная совместимость**: при изменении публичных функций сохраняется прежний API. Пример: `element_types = ELEMENT_TYPES_KR` — алиас для внешнего кода.
4. **Режим/тип обработки** — строка `KR`/`AR`, по умолчанию `KR`. При неизвестном значении — тихий fallback на `KR`.
5. **Логирование** — `from src.core.logger import setup_logger`. В `ai-blueprint-to-ifc` — собственный `logger.py` (аналогичная функция).
6. **Фоновые задачи** — `threading.Thread(daemon=True)` через `SessionManager`; прогресс обновляется через `_update_progress` (0–100).
7. **Справочники в `data/`** — только чтение; новые/изменённые файлы результатов — в `outputs/<session_id>/run_<NNN>/`.
8. **Промпты** — текстовые `.txt` в `prompts/` (для src) и `ai-blueprint-to-ifc/prompts/` (для чертежей); редактировать только с разрешения.
9. **Секреты** — `.env`, токены Keycloak, `WORKS_API_TOKEN` не выводятся в логи и в ответы.
10. **Тестирование** — тестовая инфраструктура не сформирована; при добавлении новых фич сопровождать код тестами.
11. **Git** — только просмотр статуса/логов; коммит/push — по явному разрешению.
12. **Не запускать** `docker`, `make`, `ollama` без явного запроса пользователя.

---

## 9. Частые задачи

- **Проверить статус/ошибку сессии**: `outputs/sessions.json` — поля `status`, `error`, `progress_message`; логи пишутся в консоль и в файлы.
- **Отладка отдельного модуля**: `./.venv/bin/python -m src.services.<модуль> ...` или временный скрипт внутри проекта.
- **Почему элемент не попал в перечень**: режим КР — смотреть `ifc_raw_elements_grouped.json` и `api_works_response.json` (сырые ответы API ТСН); режим АР — промежуточные файлы этапов в `run_<NNN>/`.
- **Изменение пайплайна PDF**: править код в `ai-blueprint-to-ifc/`, настройки порогов/моделей — в `ai-blueprint-to-ifc/config.py`.
- **Добавление IFC-класса**: дополнить списки в `zero_step.py` (`ELEMENT_TYPES_KR`, `_ARCH_TYPES`); учесть дедупликацию по классу между режимами (при совпадении приоритет у архитектурной метки).

---

## 10. Ограничения и безопасность

- Доступ к файлам — только внутри проекта (относительные пути от корня).
- Не изменять `data/`, `ai-blueprint-to-ifc/models/` и системную конфигурацию (Docker и др.) без явного запроса.
- Модели ML (YOLO, DINO, веса Qwen3-VL) — только использовать, не обучать/не перезаписывать (`dino_train_creator.py` — подготовка данных, запуск по явному запросу).
- `MAX_UPLOAD_MB` ограничивает размер загрузки (по умолчанию 1024 МБ).
- API требует авторизации; список пользователей зашит в `src/routes.py` (`USERS`).
- Не удалять `outputs/`, `.venv` и директории сессий без подтверждения.