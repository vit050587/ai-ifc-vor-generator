# KODA.md — проект ai-ifc-vor-generator

Инструкционный контекст для ИИ-агентов и разработчиков, работающих с репозиторием.

> **ВАЖНО (действующие правила разработки):**
>
> **АКТИВНЫЙ РЕЖИМ РАЗРАБОТКИ: `АР`** — метка переключается на `КР` в зависимости
> от того, какой режим нужно разрабатывать. Правила активного режима: АР — §8.1–8.2,
> КР — §8.3–8.4; §8.5 — общие для обоих режимов.
>
> **Терминология режимов (переименование):** режим **КР переименован в «ЦС»
> («Цифровой сборник»)**, режим **АР — в «ИИ» («Искусственный интеллект»)**.
> Оба режима обрабатывают одни и те же файлы (IFC/PDF), различается только
> пайплайн подбора работ. Метки `АР`/`КР` в этом файле и имена
> `readme_ar.md`/`readme_kr.md` сохранены как внутренние обозначения тех же
> режимов: **АР ≡ ИИ**, **КР ≡ ЦС** (подробнее — §1.1).
>
> 1. Дальнейшая разработка ведётся **только в активном режиме** (АР/ИИ —
>    архитектурные решения, подбор через LLM; КР/ЦС — конструктивные решения,
>    подбор работ через API ТСН). Изменения
>    **не должны затрагивать неактивный режим** — его код меняется только по явному
>    запросу пользователя.
> 2. После **каждого** изменения, затрагивающего активный режим, обязательно
>    **актуализируется его документация**: АР — [`readme_ar.md`](./readme_ar.md),
>    КР — [`readme_kr.md`](./readme_kr.md) — в неё вносятся описания внесённых изменений.
>    Документация всегда должна соответствовать текущему состоянию кода.
> 3. **Источники истины по режимам: АР — `readme_ar.md`, КР — `readme_kr.md`.** Перед
>    любой правкой пайплайна активного режима нужно свериться с его документом: он
>    описывает актуальный порядок модулей, создаваемые файлы и используемые
>    справочники `data/`. При расхождении кода и документации расхождение устраняется:
>    приводится в соответствие **документ** (при намеренном изменении поведения)
>    либо **код** (при случайной регрессии).
> 4. Подробные границы области активного режима, перечень затрагиваемых
>    модулей/справочников/артефактов и список «неприкосновенного» кода другого
>    режима — см. **§8 «Правила разработки»**.

---

## 1. Обзор проекта

**ai-ifc-vor-generator** — веб-сервис автоматической генерации **видов работ (ВОР)**
для смет из BIM-моделей. На вход принимает **IFC-файлы** (`.ifc`) и **PDF-чертежи**
(`.pdf`), извлекает строительные элементы, группирует их и формирует финальный
**перечень работ** (Excel) с объёмами и стоимостью.

Ключевые особенности:

- **Два режима обработки** (режимы переименованы, обрабатывают одни и те же
  файлы IFC/PDF — различается только пайплайн подбора работ):
  - **ЦС** («Цифровой сборник», публичный код `CS`; ранее — КР/KR,
    «конструктивные решения»; ж/б конструкции: стены, колонны, перекрытия,
    балки и т. д.). Подбор работ выполняется через **внешний API цифрового
    справочника ТСН** (не через LLM).
  - **ИИ** («Искусственный интеллект», публичный код `AI`; ранее — АР/AR,
    «архитектурные решения»; окна, двери, покрытия, кровля, отделка и т. д.).
    Подбор таблиц работ — **детерминированный алгоритм** (`works_table_selector`,
    `data/algorithm.md`); сами работы — цифровой сборник (larix); финальный
    выбор работ по группам — **локальная LLM** (`works_final_selector`);
    разбор ПОС/ПЗ — LLM (`pd_parser`).
- **Асинхронная обработка**: после загрузки файла сервис сразу возвращает `sessionId`, тяжёлая обработка выполняется в фоновых потоках (`threading.Thread`), пользователь опрашивает статус через API.
- **Два источника данных**: IFC (через `ifcopenshell`) или PDF-чертёж (через пайплайн машинного зрения `ai-blueprint-to-ifc`).
- **Хранилище без СУБД**: состояние сессий — JSON-файл `outputs/sessions.json` (потокобезопасный, атомарная запись); файлы результатов — файловая система `outputs/<session_id>/`.
- **Авторизация** через flask-login (встроенные пользователи) + защита API.
- Развёртывание — **Docker Compose** (`web` + `ollama`), GPU NVIDIA.

### 1.1. Коды режимов: ЦС/ИИ (CS/AI) и легаси КР/АР (KR/AR)

Режимы переименованы (публичные названия и коды):

| Публичный код | Название | Легаси-код | Бывшее название |
|---|---|---|---|
| `CS` | **ЦС** — Цифровой сборник | `KR` (также русские «КР») | КР — Конструктивные решения |
| `AI` | **ИИ** — Искусственный интеллект | `AR` (также русские «АР») | АР — Архитектурные решения |

Правила использования кодов:

- **Публичные коды `CS`/`AI`** — в API (поле `processingType` запросов),
  ответах сервера и веб-интерфейсе (radio-кнопки «ЦС — Цифровой сборник» /
  «ИИ — Искусственный интеллект»). Значение по умолчанию — `CS`.
- **Легаси-коды `KR`/`AR`** (в т.ч. русские «КР»/«АР») по-прежнему
  **принимаются на входе** API и нормализуются к `CS`/`AI` — обратная
  совместимость со старыми клиентами и сохранёнными сессиями
  (`outputs/sessions.json`).
- **Внутренние коды пайплайна** — по-прежнему `KR`/`AR`: модули пайплайна
  (`zero_step`, `group_excel`, `works_*`, `ifc_json_builder`,
  `ifc_reference_builder` и др.) работают с ними без изменений. Конвертация
  на границе — в `routes.py` через `src/core/modes.py`.
- **Центральный модуль кодов режимов** — [`src/core/modes.py`](./src/core/modes.py):
  `normalize_processing_type()` (вход → `CS`/`AI`), `to_pipeline_type()`
  (`CS`/`AI` → `KR`/`AR` для пайплайна), `to_public_type()` (`KR`/`AR` →
  `CS`/`AI` для ответов), `public_session()`/`public_runs()` (маппинг
  `processing_type` сессий/запусков в ответах), `MODE_NAMES` (человекочитаемые
  названия).
- Имена артефактов (`final_result_KR.json`, `final_result_AR.json`,
  `readme_ar.md`, `readme_kr.md`, `perechen_kr*.xlsx` и др.) **не меняются**.

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
│   ├── routes.py              # Все HTTP-эндпоинты (REST API, ~2400 строк)
│   ├── schemas.py             # Pydantic-схемы запросов/ответов (CamelModel)
│   ├── templates/
│   │   └── index.html         # Веб-интерфейс (SPA на JS)
│   ├── core/
│   │   ├── config.py          # load_config() — настройки из env (Dataclass Config)
│   │   ├── logger.py          # setup_logger() — консоль + файловый лог
│   │   ├── modes.py           # Коды режимов CS/AI (ЦС/ИИ): нормализация, конвертация
│   │   │                      #   в легаси-коды пайплайна KR/AR, маппинг ответов
│   │   ├── prompt_manager.py  # Загрузка .txt промптов из prompts/ (works_comparison)
│   │   └── keycloak.py        # Провайдер Bearer-токена Keycloak (client_credentials)
│   └── services/              # Бизнес-логика и пайплайн
│       ├── session_manager.py # SessionManager: сессии, runs, фоновые потоки, sessions.json,
│       │                      #   справочные сессии (reference_only), пересборка final JSON,
│       │                      #   разбор ПОС/ПЗ, position_links, 3D-GLB
│       ├── zero_step.py       # Извлечение элементов из IFC (КР/АР), нормализация, XLSX
│       ├── ifc_raw_dump.py    # «Сырой» дамп свойств/QTO/материалов + расчёт по bbox
│       ├── selection_template_builder.py # АР: заполнение шаблона параметров подбора (data/selection_parameters.json + карта соответствия) по каждому элементу → Параметры_подбора_элементов.json
│       ├── ifc_reference_builder.py # JSON-справочники (все элементы / группы); split_leaf_groups_by_part
│       ├── works_table_selector.py  # АР: детерминированный подбор таблиц работ (8 шагов data/algorithm.md)
│       ├── works_fetcher.py        # АР: работы из цифрового сборника (larix): период ТСН → period.json, работы таблиц → Подобранные_работы.json, детальные параметры позиций
│       ├── works_final_selector.py # АР: финальный подбор работ через LLM (v13.2: по каждой таблице — ровно одна работа; предфильтрация/ранжирование, fallback — ближайшая) → Финальный_перечень_работ.json/.xlsx
│       ├── ifc_json_builder.py      # Сборка финального JSON (final_result_AR.json / final_result_KR.json)
│       ├── position_links.py  # Ссылки на позиции цифрового сборника (position_links.json, КР)
│       ├── pd_parser.py       # Разбор PDF ПОС/ПЗ через LLM → ПОС/ПЗ_глобальные_константы.json (+ LLMClient для works_final_selector)
│       ├── works_cost.py      # Расчёт/форматирование стоимости работ финального перечня (КР и АР)
│       ├── group_excel.py     # Группировка элементов (КР: Часть здания→МССК→Материал→Тип→Геометрия; АР: Часть здания→МССК→Материал→Наименование)
│       ├── api_works_lookup.py# Подбор работ через API ТСН (КР)
│       ├── materials_lookup.py# Карта МССК-кодов материалов (АР)
│       ├── mssk_lookup.py     # Карта МССК-кодов элементов
│       ├── pdf_processor.py   # Обёртка пайплайна обработки PDF
│       ├── serializer.py      # Экспорт IFC → GLB (3D-модель по запросу)
│       └── works_comparison/  # Автономный пакет сравнения позиций смет проекта (заказчика) с позициями IFC
│           │                  #   (сравнение объёмов + LLM-валидация через Ollama, промпты prompts/);
│           │                  #   в основной конвейер НЕ подключён (запускается вручную, processor.Processor)
│           ├── processor.py        # Оркестратор сравнения: позиции смет ↔ позиции IFC
│           ├── positions_extractor.py / ifc_positions_extractor.py # Извлечение позиций
│           ├── compare_positions_groups.py # Сравнение групп по нормализованным ключам
│           ├── work_group_validator.py    # LLM-валидация групп (validate_material/validate_work)
│           ├── validation_result_former.py# Формирование результата → debug/ifc_comparison
│           ├── ollama_service.py / config.py / utils.py и др.
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
├── prompts/                   # Промпты LLM: element_analyze.txt, validate_material.txt,
│                              #   validate_work.txt — используются пакетом works_comparison/
│                              #   (через src/core/prompt_manager.py); промпты основного
│                              #   пайплайна (pd_parser, works_final_selector) встроены в модули
│
├── data/                      # Справочники (только чтение!)
│   ├── algorithm.md                          # Спецификация 8-шагового алгоритма подбора работ (АР)
│   ├── perechen_kr.xlsx, perechen_kr_1.xlsx  # Перечни работ (КР)
│   ├── koefs.xlsx                            # Нормы расхода (корректировка объёма, КР и АР)
│   ├── price_cost.xlsx                       # Стоимость расценок (Шифр → прямые затраты, КР)
│   ├── ifc_to_collections.json               # Правила IfcClass (+PredefinedType) → сборники (АР)
│   ├── msck_elements_compact.json            # Дерево МССК-элементов (АР, шаг 3 подбора)
│   ├── tree_work_compact.json                # Дерево 41 сборника ГЭСН/ТСН-2001 с таблицами (АР)
│   ├── works_classification.json             # Классификация сборников + схема 7 констант (АР)
│   ├── params_registry.json                  # Реестр параметров ПОС для LLM-извлечения (АР; вкл. доп. константы: floor_height, soil_group, movement_distance, crane_capacity, bucket_capacity, equipment_power)
│   ├── selection_parameters.json             # Перечень параметров подбора работ из 3 главы perechen_kr_1.xlsx — шаблон для заполнения по элементам (АР)
│   ├── selection_parameters_mapping.json     # Расширяемая карта «параметр шаблона → ключи сырого дампа / ключевые слова / константы» (АР)
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
├── readme_ar.md               # Источник истины по пайплайну АР (правила §8.1–8.2)
├── readme_kr.md               # Источник истины по пайплайну КР (правила §8.3–8.4)
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

1. **Загрузка файла** → `POST /api/upload_ifc` (multipart/form-data, `processingType=CS|AI`; легаси `KR|AR` принимаются как синонимы). Возвращает `sessionId` + `status`.
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

- **КР**: применение материалов → группировка `group_excel.process_ifc_excel_mssk` (**Часть здания → МССК → Материал → IFC-тип → Геометрия → Материал/Бетон**) → разделение смешанных групп по частям здания (`split_leaf_groups_by_part`) → `ifc_reference_builder.build_reference_output` → `selected_elements_grouped.json` → POST-запросы в API ТСН (`digital-collection/building-elements/positions`; группы сборного ж/б пропускаются) → фильтрация по высоте здания, отбор позиций → формирование `ОБЩИЙ_Финальный_перечень_работ.xlsx` (объём корректируется по `koefs.xlsx`, стоимость — API `works/resources`, fallback `price_cost.xlsx`).
- **АР**: детерминированный алгоритм по `data/algorithm.md` + финальный LLM-подбор:
  1. применение материалов пользователя → `materials.json`, фильтрация выбранных строк → `filtered_elements.xlsx`;
  2. группировка `group_excel.process_ifc_excel_ar` (**Часть здания → МССК → Материал → Наименование**; часть — строго по числовому индикатору «Этажа») → `Дерево_проекта_выбранные_элементы.xlsx`, `filtered_elements_grouped_AR.json`, `Дерево_проекта.xlsx` (всё здание), `ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx`, `building_parts.json`;
  3. `works_table_selector.build_works_tables_json` — 8 шагов алгоритма (нормализация элемента, сборники-кандидаты по `ifc_to_collections.json`, уточнение по МССК, переключатель технологии, выбор таблиц по ключевым словам, правила по сборникам COMPLEX/SEPARATE, 7 констант проекта, объёмы QTO; для ж/б монолита в COMPLEX-режиме дополнительно SEPARATE-пакет отдела 1.2) → `Подобранные_таблицы_работ.json`;
  4. `works_fetcher.fetch_and_save_works` — работы из цифрового сборника (larix): актуальный период ТСН (`baseTypeCode=TSN`, «индекс» в `title`, максимальный `dateStart`) → `period.json` (корень сессии); работы по шифрам подобранных таблиц (`catalog/work-process/list`) → `Подобранные_работы.json` (в `run_<NNN>/`; ошибка API не прерывает запуск);
  5. `works_final_selector.select_final_works` (v13.2) — финальный подбор через LLM: листовые группы (разделённые по частям здания `split_leaf_groups_by_part`, строго по числовому индикатору «Этажа»); работы каждой таблицы элемента-представителя предфильтруются (анти-слова, «семейные» фильтры, часть здания, высота, геометрия) и ранжируются; LLM выбирает **ровно одну работу в каждой таблице**, fallback — ближайшая по толщине; объёмы — по правилам КР из агрегатов группы (объём/площади/длина швов/опалубка/арматура, корректировка `koefs.xlsx`); разбивка стоимости (ЗП/ЭМ/МР) — детальные параметры позиций larix → `Финальный_перечень_работ.json` + `.xlsx` (структура КР);
  6. `ifc_json_builder.build_final_json` → `final_result_AR.json`.
- **Итоговая выдача запуска** (файлы для скачивания) — в **обоих режимах только финальная таблица работ**: КР — `ОБЩИЙ_Финальный_перечень_работ.xlsx`, АР — `Финальный_перечень_работ.xlsx`; остальные артефакты (`selected_elements_grouped.json`, `final_result_*.json`, справочные JSON, промежуточные файлы) остаются на диске (`run_<NNN>/`, корень сессии).
- Опционально: разбор PDF ПОС (`pd_parser.py` через LLM по `data/params_registry.json`) → `ПОС_глобальные_константы.json` — константы (5 из схемы `works_classification.json` + доп. `floor_height`, `soil_group`, `movement_distance`, `crane_capacity`, `bucket_capacity`, `equipment_power`) подставляются в подбор работ и в шаблоны параметров.
- На этапе 0 (после `zero_step`) заполняется универсальный шаблон параметров подбора: `selection_template_builder.py` по каждому элементу сырого дампа (`data/selection_parameters.json` + карта `data/selection_parameters_mapping.json`) → `Параметры_подбора_элементов.json` в корне сессии.
- Все артефакты (XLSX, JSON, дампы) сохраняются в `run_<NNN>/` каждого запуска.

---

## 6. Хранилища и справочники

Сервис **не использует классическую СУБД**.

| Хранилище | Описание |
|---|---|
| `outputs/sessions.json` | Метаданные сессий, запусков, файлов, прогресс. Потокобезопасно (`RLock`), атомарная запись через tmp-файл + `os.replace`, резервное копирование при повреждении |
| `outputs/<session_id>/` | Файлы результатов сессии (`original/`, `run_<NNN>/`, справочники) |
| `uploads/<session_id>/` | Загруженные пользователем файлы |
| `data/` | Статические справочники (только чтение): `algorithm.md`, `ifc_to_collections.json`, `msck_elements_compact.json`, `tree_work_compact.json`, `works_classification.json`, `params_registry.json`, МССК-справочники `elements_mssk*`/`materials_mssk*`, перечни `perechen_kr*.xlsx`, `koefs.xlsx`, `price_cost.xlsx` |
| **Ollama** | Локальная LLM (Qwen3-VL-8B — чертежи, YandexGPT-5-Lite-8B — разбор ПОС/ПЗ (`pd_parser`) и финальный подбор работ АР (`works_final_selector`)) |
| **API ТСН** (`normativ.mgexp.org/...`) | Подбор работ в режиме КР |
| **Keycloak** (`normativ-idm.mgexp.org/...`) | Выдача/обновление Bearer-токена (client_credentials) |

Основные параметры конфигурации: `src/core/config.py` (Dataclass `Config`, `load_config()`), `ai-blueprint-to-ifc/config.py` (Pydantic `Settings`).

---

## 7. API (кратко)

Префикс: `/ifc-vor`. Все вызовы, кроме авторизации и документации, требуют авторизации (cookie сессии; для `/api/*` без неё — `401 {"detail": "..."}`).

- Здоровье: `GET /api/health`
- Авторизация: `GET/POST /login`, `GET /logout`
- Загрузка: `POST /api/upload_ifc` (file + processingType `CS|AI`, для ИИ — `posFile`/`pzFile`), `POST /api/reference` (справочная сессия `reference_only`)
- Справочные сессии: `GET /api/session/<id>/reference` (результат построения JSON-справочников; 202 — ещё строится)
- Сессии: `GET /api/sessions`, `GET /api/session/<id>`, `GET /api/session/<id>/status`, `POST /api/session/<id>/restore` (восстановление интерфейса), `DELETE /api/session/<id>`
- Обработка: `POST /api/session/<id>/select_rows`, `POST /api/session/<id>/new_run`, `GET /api/session/<id>/runs`, `POST /api/session/<id>/switch_run/<run_id>`, `POST /api/session/<id>/filter_height`
- ПОС/ПЗ (только АР): `POST /api/session/<id>/upload_pos`, `POST /api/session/<id>/upload_pz`, `GET /api/session/<id>/works_constants` (схема констант + автоподстановка)
- Ссылки на позиции ЦС (только КР): `GET /api/session/<id>/position_links`
- Файлы: `GET /api/session/<id>/preview`, `preview_result/<filename>`, `blueprint_image`, `materials_md`, `download/<filename>`, `download_all`
- Финальный JSON запуска: `POST /api/session/<id>/run/<N>/build_final_json` (пересборка), `GET .../final_json/status`, `GET .../final_json/result`
- 3D: `POST /api/session/<id>/3d_model`, `GET /api/session/<id>/3d_model/status`

Форматы: ответы — JSON в **camelCase** (Pydantic `CamelModel`); ошибки — `{"detail": "..."}` (400/401/404/409/413/422/500); загрузка — multipart/form-data; выбор строк/запуски — JSON-тело.

---

## 8. Правила разработки

Активный режим разработки задаётся меткой в начале файла: **АР** — действуют
§8.1–8.2, **КР** — §8.3–8.4; §8.5 — общие правила для обоих режимов. При
переключении метки меняется только активный вариант правил.

### 8.1. Область разработки — только режим АР (действует при метке `АР`)

1. Дальнейшая разработка ведётся **исключительно в режиме АР** (архитектурные
   решения). Изменения **не должны затрагивать режим КР** (конструктивные
   решения); код КР меняется только по явному запросу пользователя.
2. **Затрагиваемые АР-модули** (`src/services/`):
   - этап 0: `zero_step.py` (АР-ветка, `ELEMENT_TYPES_AR`), `ifc_raw_dump.py`,
     `ifc_reference_builder.py`, `position_links.py`, `pdf_processor.py`;
   - этап 1: `works_table_selector.py` (ключевой модуль подбора — 8 шагов
     `data/algorithm.md`), `works_fetcher.py` (работы из цифрового сборника
     larix: период ТСН → `period.json`, работы таблиц →
     `Подобранные_работы.json`, детальные параметры позиций
     `catalog/work-process/detail`), `works_final_selector.py` (финальный
     LLM-подбор работ по группам, v13.2), `group_excel.py`
     (`process_ifc_excel_ar` / `group_elements_ar`), `materials_lookup.py`,
     `mssk_lookup.py`, `ifc_json_builder.py`;
   - опционально: `pd_parser.py` (разбор PDF ПОС через LLM);
   - оркестрация: `session_manager.py` (АР-ветки `_run_processing_pipeline_in_run`),
     `routes.py` (АР-эндпоинты: `upload_pos`, `works_constants` и т. д.),
     `schemas.py`.
3. **«Неприкосновенное» для КР** (не менять без явного запроса):
   `api_works_lookup.py` (подбор через API ТСН), `works_cost.py` (расчёт/
   форматирование стоимости, выделен из удалённого legacy `fourth_etap`),
   КР-ветки `zero_step.py` (`ELEMENT_TYPES_KR`) и `group_excel.py`
   (`process_ifc_excel`), КР-логика `ifc_json_builder`
   (`selected_elements_grouped.json`, `final_result_KR.json`),
   `serializer.py`, справочники `perechen_kr*.xlsx`. При рефакторинге
   общего кода (`zero_step.py`, `group_excel.py`, `session_manager.py`,
   `routes.py`) поведение режима КР должно оставаться **побайтово
   неизменным** на уровне результатов.
4. **Справочники АР в `data/`** (только чтение): `algorithm.md` (спецификация
    8-шагового алгоритма — «источник истины» для `works_table_selector`),
    `ifc_to_collections.json`, `msck_elements_compact.json`,
    `tree_work_compact.json`, `works_classification.json` (включая схему
    глобальных констант), `params_registry.json`,
    `selection_parameters.json`, `selection_parameters_mapping.json`,
    `elements_mssk_nested.json`, `materials_mssk_nested.json`. Файл
    `koefs.xlsx` используется АР (корректировка объёмов финального перечня в
    `works_final_selector` — те же правила, что в КР); `price_cost.xlsx` —
    только КР.
5. **Артефакты АР**: новые/изменённые файлы результатов — только в
    `outputs/<session_id>/` и `outputs/<session_id>/run_<NNN>/`
    (`materials.json`, `filtered_elements.xlsx`, `Дерево_проекта*.xlsx`,
    `filtered_elements_grouped_AR.json`, `ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx`,
    `building_parts.json`, `Подобранные_таблицы_работ.json`,
    `Подобранные_работы.json` (larix, в `run_<NNN>/`),
    `Финальный_перечень_работ.json/.xlsx` (итоговая выдача — только xlsx),
    `period.json` (корень сессии), `final_result_AR.json`,
    `ПОС_глобальные_константы.json`, `ПЗ_глобальные_константы.json`,
    `Параметры_подбора_элементов.json`). Имена файлов —
    на русском, как в существующем конвейере.

### 8.2. Актуализация документации АР (действует при метке `АР`)

1. После **каждого** изменения, затрагивающего режим АР (код пайплайна,
   эндпоинты, схемы, файлы результатов, справочники, параметры запуска),
   обязательно обновляется файл [`readme_ar.md`](./readme_ar.md) — в него
   вносятся описания внесённых изменений (новые/изменённые шаги пайплайна,
   создаваемые файлы, используемые справочники).
2. Документация всегда должна соответствовать текущему состоянию кода.
3. **Источник истины по АР — `readme_ar.md`.** Перед любой правкой АР-пайплайна
   нужно свериться с ним: он описывает актуальный порядок модулей, создаваемые
   файлы и используемые справочники `data/`. При расхождении кода и
   `readme_ar.md` расхождение устраняется: приводится в соответствие
   **документ** (при намеренном изменении поведения) либо **код**
   (при случайной регрессии).

### 8.3. Область разработки — только режим КР (действует при метке `КР`)

1. Дальнейшая разработка ведётся **исключительно в режиме КР** (конструктивные
   решения: ж/б конструкции, подбор работ через **внешний API ТСН**). Изменения
   **не должны затрагивать режим АР** (архитектурные решения); код АР меняется
   только по явному запросу пользователя.
2. **Затрагиваемые КР-модули** (`src/services/`):
   - этап 0: `zero_step.py` (КР-ветка: `ELEMENT_TYPES_KR`, `classify_storey_type`),
     `ifc_raw_dump.py` (сырой дамп), `ifc_reference_builder.py` (JSON-справочники
     цифрового сборника: `ifc_elements_output.json`, `ifc_raw_elements_grouped.json`,
     `ifc_raw_elements.xlsx`), `position_links.py` (ссылки на позиции цифрового
     сборника → `position_links.json`), `pdf_processor.py`;
   - этап 1: `group_excel.py` (`process_ifc_excel` / `process_ifc_excel_mssk` /
     `group_elements_mssk` — иерархия «Часть здания → Код МССК → Материал →
     IFC-тип → Геометрия → Материал/Бетон»), `api_works_lookup.py` (подбор работ
     через API ТСН: `digital-collection/building-elements/positions` + стоимость
     `works/resources`), `works_cost.py` (корректировка объёмов по `koefs.xlsx`,
     fallback-стоимость по `price_cost.xlsx`, форматирование денег, строка
     «ИТОГО:»), `ifc_json_builder.py` (КР-ветка `KRBuilder`:
     `selected_elements_grouped.json`, `final_result_KR.json`);
   - оркестрация: `session_manager.py` (КР-ветки `_process_ifc_bg` /
     `_process_pdf_bg` / `_run_processing_pipeline_in_run` /
     `_start_position_links_bg`), `routes.py` (КР-эндпоинты: `position_links`,
     `filter_height`), `schemas.py`.
3. **«Неприкосновенное» для АР** (не менять без явного запроса):
   `works_table_selector.py` (детерминированный подбор таблиц — 8 шагов
   `data/algorithm.md`), `works_fetcher.py` (цифровой сборник larix: период ТСН,
   работы таблиц, детальные параметры позиций), `works_final_selector.py`
   (финальный LLM-подбор работ по группам), `selection_template_builder.py`
   (шаблон параметров подбора), `pd_parser.py` (разбор ПОС/ПЗ через LLM),
   АР-ветки `zero_step.py` (`ELEMENT_TYPES_AR`, `_classify_storey_type_ar`) и
   `group_excel.py` (`process_ifc_excel_ar` / `group_elements_ar`), АР-логика
   `ifc_json_builder` (`filtered_elements_grouped_AR.json`,
   `final_result_AR.json`), справочники `data/algorithm.md`,
   `ifc_to_collections.json`, `msck_elements_compact.json`,
   `tree_work_compact.json`, `works_classification.json`, `params_registry.json`,
   `selection_parameters.json`, `selection_parameters_mapping.json`. При
   рефакторинге общего кода (`zero_step.py`, `group_excel.py`,
   `session_manager.py`, `routes.py`, `ifc_raw_dump.py`, `mssk_lookup.py`,
   `materials_lookup.py`) поведение режима АР должно оставаться **побайтово
   неизменным** на уровне результатов.
4. **Справочники КР в `data/`** (только чтение): `perechen_kr.xlsx`,
   `perechen_kr_1.xlsx` (перечни работ), `koefs.xlsx` (нормы расхода —
   корректировка объёма расценок), `price_cost.xlsx` (стоимость расценок —
   fallback при недоступности API стоимости). Справочники
   `elements_mssk_nested.json` / `materials_mssk_nested.json` — общие для КР и
   АР (группировка по МССК). АР-специфичные справочники (`algorithm.md`,
   `ifc_to_collections.json`, `tree_work_compact.json`,
   `works_classification.json`, `params_registry.json`,
   `selection_parameters*.json`) в КР-конвейере не используются.
5. **Артефакты КР**: новые/изменённые файлы результатов — только в
   `outputs/<session_id>/` и `outputs/<session_id>/run_<NNN>/`
   (`ОБЩИЙ_Финальный_перечень_работ.xlsx`, `selected_elements_grouped.json`,
   `api_works_response.json`, `api_works_cost_response.json`,
   `final_result_KR.json`, а в корне сессии — `ifc_elements_output.json`,
   `ifc_raw_elements_grouped.json`, `ifc_raw_elements.xlsx`,
   `position_links.json`). Имена файлов — на русском, как в существующем
   конвейере.

### 8.4. Актуализация документации КР

1. После **каждого** изменения, затрагивающего режим КР (код пайплайна,
   эндпоинты, схемы, файлы результатов, справочники, параметры запуска),
   обязательно обновляется файл [`readme_kr.md`](./readme_kr.md) — в него
   вносятся описания внесённых изменений (новые/изменённые шаги пайплайна,
   создаваемые файлы, используемые справочники).
2. Документация всегда должна соответствовать текущему состоянию кода.
3. **Источник истины по КР — `readme_kr.md`.** Перед любой правкой КР-пайплайна
   нужно свериться с ним: он описывает актуальный порядок модулей, создаваемые
   файлы и используемые справочники `data/`. При расхождении кода и
   `readme_kr.md` расхождение устраняется: приводится в соответствие
   **документ** (при намеренном изменении поведения) либо **код**
   (при случайной регрессии).

### 8.5. Общие правила

1. **Виртуальное окружение**: все команды — через `.venv/bin/pip` и `.venv/bin/python`. Системный Python не используется.
2. **Стиль**: PEP8, докстринги для новых функций/классов, комментарии — на русском или английском (как принято в проекте). Имена выходных файлов — **на русском** (`ДЛЯ_СМЕТЧИКА_...`, `Дерево_проекта...`, `ОБЩИЙ_Финальный_перечень_работ.xlsx`).
3. **Обратная совместимость**: при изменении публичных функций сохраняется прежний API. Пример: `element_types = ELEMENT_TYPES_KR` — алиас для внешнего кода.
4. **Режим/тип обработки** — строка `KR`/`AR`, по умолчанию `KR`. При неизвестном значении — тихий fallback на `KR`.
5. **Логирование** — `from src.core.logger import setup_logger`. В `ai-blueprint-to-ifc` — собственный `logger.py` (аналогичная функция).
6. **Фоновые задачи** — `threading.Thread(daemon=True)` через `SessionManager`; прогресс обновляется через `_update_progress` (0–100).
7. **Справочники в `data/`** — только чтение; новые/изменённые файлы результатов — в `outputs/<session_id>/run_<NNN>/`.
8. **Промпты** — текстовые `.txt`: `prompts/` (используются автономным пакетом
   `src/services/works_comparison/` через `src/core/prompt_manager.py`) и
   `ai-blueprint-to-ifc/prompts/` (для чертежей); редактировать только с
   разрешения. Промпты основного пайплайна (`pd_parser`, `works_final_selector`)
   встроены в модули.
9. **`src/services/works_comparison/`** — автономный пакет сравнения позиций
   смет заказчика с позициями IFC (LLM-валидация через Ollama, результат —
   `debug/ifc_comparison/comparison_results/result.json`). В конвейеры КР/АР
   **не подключён**; менять только по явному запросу пользователя.
10. **Секреты** — `.env`, токены Keycloak, `WORKS_API_TOKEN` не выводятся в логи и в ответы.
11. **Тестирование** — тестовая инфраструктура не сформирована; при добавлении новых фич сопровождать код тестами.
12. **Git** — только просмотр статуса/логов; коммит/push — по явному разрешению.
13. **Не запускать** `docker`, `make`, `ollama` без явного запроса пользователя.

---

## 9. Частые задачи

- **Проверить статус/ошибку сессии**: `outputs/sessions.json` — поля `status`, `error`, `progress_message`; логи пишутся в консоль и в файлы.
- **Отладка отдельного модуля**: `./.venv/bin/python -m src.services.<модуль> ...` или временный скрипт внутри проекта.
- **Почему элемент не попал в перечень**: режим КР — смотреть `ifc_raw_elements_grouped.json` и `api_works_response.json` (сырые ответы API ТСН); режим АР — поле `note` элемента в `Подобранные_таблицы_работ.json` (причина, если сборники не определены), а также промежуточные файлы `run_<NNN>/`.
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
