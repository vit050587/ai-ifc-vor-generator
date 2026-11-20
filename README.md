# ai-ifc-vor-generator

Сервис автоматической генерации **видов работ (ВОР)** для смет из BIM-моделей.
Принимает на вход **IFC-файлы** и **PDF-чертежи**, извлекает строительные элементы,
группирует их и формирует финальный **перечень работ** (Excel) с объёмами и стоимостью.

Документ единый: он описывает **все режимы обработки и все вспомогательные
функционалы сервиса**. Других README в проекте нет — при изменении кода
обновляется этот файл.

| Код | Название | Легаси-код | Что обрабатывает | Подбор работ |
|---|---|---|---|---|
| `CS` | **ЦС** — Цифровой сборник | `KR`, `КР` | конструктив (стены, колонны, перекрытия, балки, фундаменты, сваи, лестницы) | внешний API цифрового справочника ТСН |
| `AI` | **ИИ** — Искусственный интеллект | `AR`, `АР` | конструктив + архитектура (окна, двери, кровля, покрытия, отделка, перила, мебель и др.) | детерминированный алгоритм `works_table_selector` + LLM (`works_final_selector` / `perechen_pipeline`) |
| `ARCH` | **Архитектура** | — | только архитектура (без конструктивных элементов) | те же алгоритмы и LLM, что в ИИ, но собственные копии модулей `src/services/architecture/` |

Вспомогательные функционалы (вне трёх режимов, подбор работ в них не выполняется
или выполняется отдельным путём):

- **Проверка ЦИМ модели** — отдельная вкладка: IFC → проверка требуемых параметров
  по `data/parametrs_name.json` (LLM-сопоставление) → `Проверка_ЦИМ_модели.xlsx`;
- **Типовые ВОР, сверка и сметная форма** — блок в результатах вкладки «Обработка»:
  две типовые ведомости (по модели и по ВОР заказчика) + сверка + сметный Excel;
- **Сравнение ведомостей объёмов работ** — API `/api/comparisons`: несколько моделей
  и ВОР заказчика → сопоставление работ и объёмов, отчёт JSON/Excel;
- **Формирование ВОР по типовому шаблону** — API `/api/vor-generation`
  (IFC + документы → `Сформированный_ВОР.xlsx`).

Сервис реализован на **Flask + gunicorn**, работает в **Docker Compose** вместе с **Ollama**
(локальная LLM) на базе графического ускорителя NVIDIA. Вся тяжёлая обработка
выполняется **в фоне** (в отдельных потоках), API — асинхронное: после загрузки файла
сервис сразу возвращает `sessionId`, а пользователь опрашивает статус до завершения.

---

## Содержание

1. [Обзор](#1-обзор)
2. [Режимы обработки](#2-режимы-обработки)
3. [Функциональность](#3-функциональность)
4. [Структура проекта](#4-структура-проекта)
5. [Технологический стек](#5-технологический-стек)
6. [Модели ИИ](#6-модели-ии)
7. [Хранилища и справочники](#7-хранилища-и-справочники)
8. [API](#8-api)
9. [Запуск и развертывание](#9-запуск-и-развертывание)
10. [Режим ЦС (CS)](#10-режим-цс-cs)
11. [Режим ИИ (AI)](#11-режим-ии-ai)
12. [Режим Архитектура (ARCH)](#12-режим-архитектура-arch)
13. [Проверка ЦИМ модели](#13-проверка-цим-модели)
14. [Типовые ВОР, сверка и сметная форма](#14-типовые-вор-сверка-и-сметная-форма)
15. [Сравнение ведомостей объёмов работ](#15-сравнение-ведомостей-объёмов-работ)
16. [Сопровождение документации](#16-сопровождение-документации)

---

## 1. Обзор

**ai-ifc-vor-generator** — веб-сервис автоматической генерации ВОР для смет из
BIM-моделей. Ключевые особенности:

- **Три режима обработки** (`CS` / `AI` / `ARCH`) — обрабатывают одни и те же файлы
  (IFC/PDF), различается набор извлекаемых элементов и пайплайн подбора работ.
- **Два источника данных**: IFC (через `ifcopenshell`) или PDF-чертёж (через пайплайн
  машинного зрения `ai-blueprint-to-ifc`).
- **Асинхронная обработка**: после загрузки файла сервис сразу возвращает `sessionId`,
  тяжёлая обработка выполняется в фоновых потоках (`threading.Thread`), статус
  опрашивается через API.
- **Хранилище без СУБД**: состояние сессий — JSON-файл `outputs/sessions.json`
  (потокобезопасный, атомарная запись); файлы результатов — `outputs/<session_id>/`.
- **Множественные запуски** (runs) в рамках одной сессии: исходный файл обрабатывается
  один раз, далее можно делать сколько угодно расчётов по разным выборкам строк.
- **Авторизация** через flask-login (встроенные пользователи) + защита API.

## 2. Режимы обработки

Коды режимов нормализуются в [`src/core/modes.py`](./src/core/modes.py):

| Функция | Назначение |
|---|---|
| `normalize_processing_type()` | входное значение (`CS`/`AI`/`ARCH`, легаси `KR`/`AR`, русские `КР`/`АР`, `ARCHITECTURE`/`АРХ`/`АРХИТЕКТУРА`) → публичный код `CS`/`AI`/`ARCH`; по умолчанию `CS` |
| `to_pipeline_type()` | публичный код → внутренний код пайплайна (`KR` / `AR` / `ARCH`) |
| `to_public_type()` | внутренний код → публичный (`CS` / `AI` / `ARCH`) для ответов API |
| `public_session()`, `public_runs()` | маппинг `processing_type` сессий и запусков в ответах |
| `is_architecture()`, `is_ai_like()` | проверки режима (`ARCH`; `AI` или `ARCH`) |
| `MODE_NAMES` | человекочитаемые названия режимов |

Правила:

- **Публичные коды** `CS`/`AI`/`ARCH` — в API (поле `processingType`), ответах сервера
  и веб-интерфейсе (переключатели «ЦС — Цифровой сборник», «ИИ — Искусственный
  интеллект», «Архитектура»). Значение по умолчанию — `CS`.
- **Легаси-коды** `KR`/`AR` (в т.ч. русские) принимаются на входе и нормализуются —
  обратная совместимость со старыми клиентами и сохранёнными сессиями.
- **Внутренние коды пайплайна** — `KR`/`AR` для ЦС/ИИ (модули пайплайна работают с
  ними без изменений) и `ARCH` для режима «Архитектура». Конвертация на границе — в
  `routes.py` через `src/core/modes.py`.
- Имена артефактов ЦС/ИИ (`final_result_KR.json`, `final_result_AR.json`,
  `perechen_kr*.xlsx` и др.) **не меняются**; для режима «Архитектура» используется
  `final_result_ARCH.json`.

## 3. Функциональность

- **Загрузка IFC (.ifc) или PDF-чертежа (.pdf)** с указанием режима обработки
  `CS` / `AI` / `ARCH` (легаси `KR` / `AR` также принимаются). Для ИИ и Архитектуры
  дополнительно можно приложить документы ПОС/ПЗ/ВОР/ПД.
- **Извлечение элементов** из IFC (`ifcopenshell`): классы определяются режимом
  (`ELEMENT_TYPES_KR` / `ELEMENT_TYPES_AR` / `ELEMENT_TYPES_ARCH`).
- **Извлечение данных из PDF-чертежей** (машинное зрение): детекция стен (YOLO OBB),
  layout и легенд, анализ штриховок и расшифровка материалов (DINO + VLM), масштабы.
- **Нормализация** характеристик: размеры, геометрия (в т. ч. расчёт количеств по bbox,
  когда в IFC нет QTO), материал, части здания, этажность.
- **Группировка** элементов в иерархию для сметчика — единая для всех режимов
  (`group_elements_mssk`: Часть здания → МССК → Материал → IFC-тип → Геометрия →
  Материал/Бетон); различаются только имена файлов (`_AR`, `_mssk`).
- **Подбор работ**: API ТСН (ЦС); детерминированный алгоритм + LLM (ИИ и Архитектура);
  альтернативно — по перечню сметчиков (`WORKS_SELECTOR=perechen`).
- **Расчёт объёмов** (по группам/материалам), корректировка по нормам расхода
  (`koefs.xlsx`) и **стоимости** (`price_cost.xlsx`, larix, API ТСН).
- **Документы** (ИИ/Архитектура): разбор ПОС/ПЗ (`pd_parser`, `pd_full`), ВОР/ПД
  (`doc_pdf_parser`, `doc_facts`), поправки сборников ТСН (`tsn_coeffs`).
- **Excel-перечни**: `ДЛЯ_СМЕТЧИКА*`, `ОБЩИЙ_Финальный_перечень_работ.xlsx`,
  `Финальный_перечень_работ.xlsx`, `Дерево_проекта*`.
- **Типовые ВОР и сверка** (`builder`/`customer_vor` + `comparison_typical`):
  две типовые ведомости и общий файл сверки.
- **Сметная форма** по сформированному перечню (ТСН-2001.12 с дополнением 83).
- **Проверка ЦИМ модели** — отдельная вкладка, подбор работ не выполняется.
- **3D-модель (GLB)** из IFC — по запросу; просмотр групп, объёмов и элементов модели.
- **Просмотр и скачивание** всех промежуточных и финальных файлов, выгрузка архивом.

## 4. Структура проекта

```
ai-ifc-vor-generator/
├── Dockerfile                 # Образ сервиса (CUDA 12.9, Python 3.11)
├── docker-compose.yml         # Связка ollama (GPU) + web-сервис
├── nginx.conf                 # Проксирование /ifc-vor/ → 127.0.0.1:6001
├── Makefile                   # Утилиты: make up / down / restart / logs / clean
├── start.sh                   # Локальный запуск Flask (рабочая копия, порт 6005)
├── requirements.txt           # Python-зависимости
├── .env                       # Переменные окружения (модели, API, Keycloak)
├── KODA.md                    # Правила разработки для ИИ-агентов
├── instruction.md             # Инструкция для пользователя сервиса
├── README.md                  # Этот документ — описание всех режимов
│
├── src/                       # Основной сервис (Flask)
│   ├── __init__.py            # create_app(), регистрация blueprint, Swagger
│   ├── wsgi.py                # Точка входа gunicorn
│   ├── routes.py              # Все HTTP-эндпоинты (REST API)
│   ├── schemas.py             # Pydantic-схемы запросов/ответов
│   ├── templates/
│   │   └── index.html         # Веб-интерфейс (SPA на JS)
│   ├── core/
│   │   ├── config.py          # Конфигурация из env (пути, модели, API)
│   │   ├── logger.py          # Настройка логирования
│   │   ├── modes.py           # Коды режимов CS/AI/ARCH и их конвертация
│   │   ├── prompt_manager.py  # Загрузка промптов из prompts/
│   │   ├── cache_manager.py   # Обёртка дискового кеша (сравнение ВОР)
│   │   └── keycloak.py        # Bearer-токен Keycloak (client_credentials)
│   ├── services/              # Бизнес-логика и пайплайны
│   │   ├── session_manager.py # Сессии, запуски, фоновые потоки, sessions.json
│   │   ├── zero_step.py       # Извлечение элементов IFC (KR/AR/ARCH), нормализация
│   │   ├── ifc_raw_dump.py    # «Сырой» дамп свойств/QTO/материалов + расчёт по bbox
│   │   ├── ifc_reference_builder.py   # JSON-справочники цифрового сборника (ЦС)
│   │   ├── position_links.py          # Ссылки на позиции цифрового сборника (ЦС)
│   │   ├── group_excel.py             # Группировка элементов (ЦС, ИИ, ARCH)
│   │   ├── api_works_lookup.py        # ЦС: подбор работ через API ТСН
│   │   ├── works_cost.py              # Стоимость/объёмы перечня (ЦС, ARCH)
│   │   ├── selection_template_builder.py # Шаблон параметров подбора по элементам
│   │   ├── works_table_selector.py    # ИИ: детерминированный подбор таблиц (8 шагов)
│   │   ├── works_fetcher.py           # ИИ: работы таблиц из сборника (larix)
│   │   ├── works_final_selector.py    # ИИ: финальный подбор работ через LLM
│   │   ├── perechen_kb.py / perechen_pipeline.py  # ИИ: подбор по перечню сметчиков
│   │   ├── local_works.py / local_resources.py    # Локальный справочник расценок
│   │   ├── tsn_coeffs.py              # Поправочные коэффициенты сборников ТСН
│   │   ├── pd_parser.py               # Разбор ПОС/ПЗ через LLM (10 констант)
│   │   ├── pd_full.py                 # Полный разбор ПОС/ПЗ (≈40 параметров)
│   │   ├── doc_pdf_parser.py          # Разбор ВОР/ПД через LLM
│   │   ├── doc_facts.py               # Факты ВОР/ПД (бетон, арматура, объёмы)
│   │   ├── vor_comparison.py          # Сравнение ВОР заказчика и перечня
│   │   ├── ifc_features.py            # Признаки геометрии IFC
│   │   ├── ifc_json_builder.py        # Сборка final_result_KR/AR/ARCH.json
│   │   ├── materials_lookup.py / mssk_lookup.py   # МССК-справочники
│   │   ├── pdf_processor.py           # Обёртка пайплайна обработки PDF
│   │   ├── serializer.py              # Экспорт IFC → GLB
│   │   ├── estimate_*.py              # Сметная форма по перечню: calculation,
│   │   │                              #   consolidation, formulas, revisions,
│   │   │                              #   parameter_comparison, comparison(+_report),
│   │   │                              #   tasks_client, export
│   │   ├── comparison_*.py            # Сравнение ВОР: manager, documents, layout,
│   │   │                              #   reconciliation, similarity, typical,
│   │   │                              #   report, exports, ai_reason, wording, vor
│   │   ├── vor_parser.py              # Разбор ВОР заказчика (pdfplumber + эвристики)
│   │   ├── quantity_*.py, *_formwork.py, ifc_*quantities.py
│   │   │                              # Количества: меры, происхождение, правила,
│   │   │                              #   опалубка стен/контактов/лестниц, QTO
│   │   ├── model_inspector.py, model_documents.py, work_list_view.py,
│   │   │   work_sections.py, cs_final_reader.py, cs_model_members.py
│   │   │                              # Просмотр модели/документов/перечня, ЦС
│   │   ├── construction_method.py, drawing_provenance.py, vor_verdicts.py
│   │   ├── customer_documents/        # Разбор смет/ВОР заказчика (readers, schema,
│   │   │                              #   validation, cache, ollama_client, service)
│   │   ├── architecture/      # Режим ARCH: собственные копии модулей ИИ
│   │   │                      #   zero_step, ifc_raw_dump, work_height, group_excel,
│   │   │                      #   works_table_selector, works_fetcher,
│   │   │                      #   works_final_selector, works_cost,
│   │   │                      #   ifc_reference_builder, ifc_json_builder,
│   │   │                      #   ifc_features, selection_template_builder
│   │   ├── vor_generation/    # Типовые ВОР: builder, customer_vor, session_typical,
│   │   │                      #   template, typical_fill, saved_comparisons, documents,
│   │   │                      #   elements, mapping, quantity/rates, reconcile, manager
│   │   ├── check_model/       # Проверка ЦИМ: requirements, mapping, pipeline,
│   │   │                      #   grouping, formwork, llm_client, geometry
│   │   └── works_comparison/  # Сравнение позиций смет (LLM-валидация)
│   │                          #   processor, positions_*, compare_positions_groups,
│   │                          #   semantic_matching, construction_matching,
│   │                          #   work_group_validator, ollama_service, units
│   │
│   │   # Не используются рабочим пайплайном (legacy, оставлены как история):
│   │   # first_etap.py, second_etap.py, third_etap.py, fourth_etap.py,
│   │   # base_knowledge.py, geometry_filter.py, validation_processor.py.
│   │   # second_etap.py к тому же падает при импорте (нет Config.AR_DOCUMENTS_PATH).
│   │
│   ├── tools/
│   │   └── tsn/               # Подготовка сборников ТСН (CLI): parse_coeffs,
│   │                          #   parse_norms, tech_part, grid, refs, resolve,
│   │                          #   assemble, build, validate, tsn_cfg
│
├── ai-blueprint-to-ifc/       # Пайплайн машинного зрения для PDF-чертежей
│   ├── config.py              # Настройки (модели, параметры, пороги)
│   ├── processor.py           # Оркестратор обработки чертежа
│   ├── pdf_prcoessor.py       # Конвертация PDF → изображения, тайлы
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
│   ├── draw_geometry.py       # Отрисовка размеченных чертежей
│   ├── models/                # Веса ML-моделей (только использование)
│   └── prompts/               # Промпты VLM для чертежей
│
├── prompts/                   # Промпты LLM (validate_material.txt, validate_work.txt)
├── data/                      # Справочники (только чтение) — см. §7
├── cache/                     # Дисковые кеши: check_model/mapping_cache.json,
│                              #   customer_document_imports/, joblib/
├── uploads/                   # Загруженные файлы (по сессиям)
├── outputs/                   # Результаты обработки (sessions.json, <session_id>/)
└── docs/                      # Манифест синхронизации диаграммы (diagram-sync*.json,
                               #   readme-blocks*.json)
```

## 5. Технологический стек

- **Backend**: Flask 3.1, gunicorn, flasgger (Swagger), Flask-Login
- **Валидация**: Pydantic 2 (`src/schemas.py`, ответы в camelCase)
- **IFC**: ifcopenshell 0.8.5 (парсинг, геометрия, экспорт GLB)
- **Данные/Excel**: pandas, openpyxl, PyMuPDF, pdf2image, pillow, opencv
- **LLM**: ollama (py-клиент), langchain/langgraph, промпты в `prompts/`
- **Машинное зрение**: ultralytics (YOLO OBB/layout/legend), supervision, DINO
  (TorchScript), transformers (VLM `GreenMap/qwen3-vl-4b-ru-blueprint-extractor`)
- **Прочее**: pymorphy3, fuzzywuzzy, requests, httpx, tenacity, shapely
- **Инфраструктура**: Docker / docker-compose, Nginx (проксирование `/ifc-vor/`),
  CUDA 12.9, Python 3.11

## 6. Модели ИИ

| Модель | Фреймворк | Роль |
|--------|-----------|------|
| `yolo_walls_obb.pt` | Ultralytics YOLO (OBB) | Детекция стен на чертеже PDF |
| `yolo_layout.pt` | Ultralytics YOLO | Детекция layout-областей (чертёж/легенда) |
| `yolo_legend_layout.pt` | Ultralytics YOLO | Детекция таблиц легенды |
| `dino_hatching.pt` | DINO (TorchScript) | Анализ штриховок, сравнение символов легенды |
| `hf.co/unsloth/Qwen3-VL-8B-Instruct-GGUF` (`OLLAMA_MODEL_NAME`) | Ollama / VLM | Обработка чертежей PDF: распознавание материалов и штриховок |
| `yandex/YandexGPT-5-Lite-8B-instruct-GGUF` (`NORMS_LLM_MODEL`, `LLM_MODEL`, `KB_LLM_MODEL`) | Ollama | Режимы ИИ и Архитектура: разбор ПОС/ПЗ/ВОР/ПД, подбор работ, поправки ТСН |
| `qwen3.8:27b` (`CHECK_MODEL_LLM_MODEL`, fallback `LLM_MODEL` → `NORMS_LLM_MODEL`) | Ollama | Проверка ЦИМ модели: сопоставление требуемых параметров с колонками модели |
| `OLLAMA_MODEL` (`works_comparison`) | Ollama | Сравнение ВОР: семантическое сопоставление и LLM-валидация |
| `GreenMap/qwen3-vl-4b-ru-blueprint-extractor` (`TG_MODEL_DIR`) | transformers (HF) | Извлечение данных с чертежа/легенды |
| **ifcopenshell** | C++/Python | Парсинг и геометрия IFC, экспорт в GLB (не ML) |

Роль моделей по задачам:

- **PDF-чертежи** — конвейер `ai-blueprint-to-ifc`: YOLO (стены/layout/легенда) →
  DINO + VLM (штриховки и материалы) → формирование Excel.
- **IFC-файлы** — `ifcopenshell`: атрибуты, Property Sets, QTO, геометрия → расчёт количеств.
- **Подбор работ** — ИИ и Архитектура: локальная LLM; ЦС: внешний API ТСН (LLM не используется).

## 7. Хранилища и справочники

Сервис **не использует классическую СУБД**:

| Хранилище | Описание |
|---|---|
| `outputs/sessions.json` | Метаданные сессий, запусков, файлов, прогресс; записи `session_type='comparison'` и `'vor_generation'`. Потокобезопасно (`RLock`), атомарная запись (tmp + `os.replace`), резервное копирование при повреждении |
| `outputs/<session_id>/` | Файлы сессии: `original/`, `run_<NNN>/`, справочные JSON/XLSX |
| `outputs/<comparison_id>/` | Сравнение ВОР: `models.json`, `estimates.json`, `model_analytics.json`, `result.json`, `result.xlsx`, `semantic_match_diagnostics.jsonl` |
| `outputs/<job_id>/` | Задача формирования ВОР: `Сформированный_ВОР.xlsx`, `vor_generation.json`, `IFC_исходные_параметры.json/.xlsx` |
| `outputs/_doc_cache/` | Кеш разбора ПОС/ПЗ/ВОР/ПД по SHA-256 (служебный) |
| `uploads/<session_id>/` | Загруженные пользователем файлы |
| `cache/check_model/mapping_cache.json` | Кеш карты сопоставления параметров проверки ЦИМ |
| `cache/customer_document_imports/` | Кеш импорта смет/ВОР заказчика (по SHA-256 файла) |
| `cache/joblib/` | Дисковый кеш тяжёлых вычислений (joblib) |

Справочники `data/` (только чтение):

| Файл | Назначение |
|------|-----------|
| `perechen_kr.xlsx`, `perechen_kr_1.xlsx` | Перечни работ (ЦС; `perechen_kr` — база знаний подбора ИИ) |
| `perechen_ar.xlsx` | Legacy-перечень прежнего 4-этапного пайплайна АР (не используется) |
| `koefs.xlsx` | Нормы расхода/коэффициенты корректировки объёмов |
| `price_cost.xlsx` | Стоимость расценок (ЦС — fallback; ИИ — локальный справочник) |
| `algorithm.md` | Спецификация 8-шагового алгоритма подбора таблиц (ИИ/Архитектура) |
| `ifc_to_collections.json`, `msck_elements_compact.json`, `tree_work_compact.json`, `works_classification.json`, `params_registry.json` | Справочники детерминированного подбора работ (ИИ/Архитектура) |
| `selection_parameters.json`, `selection_parameters_mapping.json` | Шаблон параметров подбора и карта соответствия |
| `tsn/*.json` | Разобранные сборники ТСН (поправочные коэффициенты, состав норм) |
| `elements_mssk.xlsx` / `elements_mssk_nested.json` | МССК-справочник элементов |
| `materials_mssk.xlsx` / `materials_mssk_nested.json` | МССК-справочник материалов |
| `comparison_vor_template.xlsx` | Шаблон иерархии ВОР для отчёта сравнения |
| `parametrs_name.json` | Требования к параметрам ЦИМ по IFC-классам |
| `Типовой ВОР по КР.xlsx` | Типовой шаблон ВОР (лист «ВОР КР_ЖС») |
| `catalog_tsn.json`, `etalon.xlsx` | Каталог ТСН, эталон качества подбора |
| `kb_cache.json`, `tsn_decisions_cache.json`, `pd_full_cache_v2.json`, `doc_facts_cache.json`, `rate_fix_cache.json`, `tsn_conflicts_cache.json`, `local_*.json` | Кеши LLM-решений, поправок ТСН и локальных расценок |
| `ifc_models/*.ifc` | Тестовые IFC-модели пяти проектов для проверки ЦИМ (§13) |

Внешние системы (по сети):

- **Ollama** — локальный инференс LLM/VLM;
- **API цифрового справочника ТСН** `normativ.mgexp.org/digital-collection/...` — подбор
  работ в ЦС, работы таблиц и детальные параметры позиций (larix) в ИИ;
- **Keycloak** `normativ-idm.mgexp.org/...` — выдача/обновление Bearer-токена.

## 8. API

Базовый префикс: `/ifc-vor`. Swagger UI: `/ifc-vor/docs`, спецификация: `/ifc-vor/apispec.json`.
Все вызовы, кроме `/login`, `/logout` и документации, требуют авторизации (cookie сессии;
для `/api/*` без неё — `401 {"detail": "..."}`). Ответы — JSON в **camelCase**
(Pydantic `CamelModel`), ошибки — `{"detail": "..."}` (400/401/404/409/413/422/500).
Загрузка — `multipart/form-data`, выбор строк и запуски — JSON-тело.

### Авторизация и служебное

| Метод | Путь | Описание |
|-------|------|----------|
| GET/POST | `/login` | Вход (HTML-форма) |
| GET | `/logout` | Выход |
| GET | `/` | Веб-интерфейс (SPA) |
| GET | `/api/health` | `{"status":"ok","timestamp":...}` |

### Загрузка и проверка ЦИМ

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/upload_ifc` | Загрузка IFC/PDF + `processingType` (`CS`/`AI`/`ARCH`; легаси `KR`/`AR`), опционально `posFile`, `pzFile`, `vorFile`, `pdFile`. Async: `sessionId`, `status` |
| POST | `/api/check_model` (алиас `/api/check-model`) | Проверка ЦИМ: загрузка только IFC (multipart, поле `file`), async (202, `Location`, `Retry-After`) |
| GET | `/api/check_model/<id>` (алиас `/api/check-model/<id>`) | Статус и результат проверки (`status`, `progress`, `progressMessage`, `result`) |
| GET | `/api/check_model/<id>/groups` (алиас) | Дерево проверенных элементов по структуре типового ВОР (`?refresh=1` — без кеша) |

### Сессии и файлы

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/sessions` | Список сессий (+`total`) |
| GET | `/api/session/<id>` | Полные данные сессии |
| DELETE | `/api/session/<id>` | Удаление сессии и файлов |
| GET | `/api/session/<id>/status` | Краткий статус для поллинга |
| POST | `/api/session/<id>/restore` | Восстановление интерфейса (`?saved=1` — из сохранённых результатов) |
| GET | `/api/session/<id>/preview` | Предпросмотр Excel + карты МССК/материалов |
| GET | `/api/session/<id>/preview_result/<filename>` | Предпросмотр произвольного xlsx/csv/json |
| GET | `/api/session/<id>/blueprint_image` | PNG размеченного чертежа (PDF) |
| GET | `/api/session/<id>/materials_md` | Markdown условных обозначений (PDF) |
| GET | `/api/session/<id>/download/<filename>` | Скачать файл результата |
| GET | `/api/session/<id>/download_all` | Скачать все файлы ZIP-архивом |

### Обработка и запуски

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/session/<id>/select_rows` | Выбор строк (`rowIndices`, `allRows`, `rowTypes`, `rowMaterials`, `buildingHeight`, `processingType`; ИИ/Архитектура — `floorHeight`, `globalConstants`) и запуск обработки |
| POST | `/api/session/<id>/new_run` | Новый запуск без повторного извлечения из файла |
| GET | `/api/session/<id>/runs` | Список запусков |
| POST | `/api/session/<id>/switch_run/<run_id>` | Переключение на другой запуск |
| POST | `/api/session/<id>/filter_height` | Обновить высоту здания |

Статусы: `ifc_processing` / `pdf_processing` / `selecting_rows` / `processing` /
`check_model_processing` / `completed` / `error` (краткий `status` маппится в
`processing`, `selecting_rows` и т. п.).

### Документы и константы (ИИ и Архитектура)

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/session/<id>/works_constants` | Схема констант подбора + значения из IFC/ПОС/ПЗ |
| POST | `/api/session/<id>/upload_pos` | PDF ПОС → `ПОС_глобальные_константы.json` |
| POST | `/api/session/<id>/upload_pz` | PDF ПЗ → `ПЗ_глобальные_константы.json` |
| POST | `/api/session/<id>/upload_vor` | PDF ВОР → `ВОР_ведомость_объемов_работ.json/.xlsx` |
| POST | `/api/session/<id>/upload_pd` | PDF ПД → `ПД_характеристики_элементов.json/.xlsx` |
| GET | `/api/session/<id>/docs_progress` | Прогресс разбора документов |
| GET | `/api/session/<id>/pd_full` | Полный разбор ПОС/ПЗ → `pd_full.json` |
| GET | `/api/session/<id>/doc_facts` | Тезисная сводка фактов ВОР/ПД |
| GET | `/api/session/<id>/analytic` | Аналитическая справка по расчёту (`analytic.json`) |
| GET | `/api/session/<id>/compare_vor` | Сравнение ВОР заказчика и перечня (`?rebuild=1`) |
| GET | `/api/session/<id>/tsn_mode` | Учёт поправок ТСН (`?set=on|off`) |
| GET | `/api/session/<id>/pd_mode` | Учёт ПОС/ПЗ (`?set=on|off`) |
| GET | `/api/tsn/docs` | Список загруженных сборников ТСН |

### Цифровой сборник (ЦС)

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/session/<id>/position_links` | Ссылки на позиции цифрового сборника (`position_links.json`) |

### Финальный JSON запуска

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/session/<id>/run/<N>/build_final_json` | Сборка `final_result_KR/AR/ARCH.json` |
| GET | `/api/session/<id>/run/<N>/final_json/status` | Статус сборки |
| GET | `/api/session/<id>/run/<N>/final_json/result` | Результат сборки |

### 3D-модель (только IFC)

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/session/<id>/3d_model` | Создать GLB по запросу |
| GET | `/api/session/<id>/3d_model/status` | Статус (`none/creating/ready/error`) |
| GET | `/api/session/<id>/3d_model/groups` | Группы элементов модели |
| GET | `/api/session/<id>/3d_model/document_row` | Привязка строки документа к элементу |
| GET | `/api/session/<id>/3d_model/quantities` | Объёмы элементов |
| GET | `/api/session/<id>/3d_model/elements/<guid>` | Данные элемента по GUID |

### Типовые ВОР, сверка и смета

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/session/<id>/typical_vor` | Две типовые ведомости и общий файл сверки (`?saved=1`, `?rebuild=1`) |
| GET | `/api/session/<id>/estimate` | Сметная форма по перечню (`?saved=1`, `?rebuild=1`) |
| POST | `/api/session/<id>/estimate/compare` | Сравнение со сметой заказчика (отдельно от сверки ВОР) |

### Сравнение ведомостей объёмов работ

| Метод | Путь | Описание |
|-------|------|----------|
| GET/POST | `/api/comparisons` | Список сравнений / создание нового (модели + ВОР заказчика) |
| GET | `/api/comparisons/<id>` | Статус, стадии и результат сравнения |
| POST | `/api/comparisons/<id>/estimates` | Загрузка ВОР заказчика |
| POST | `/api/comparisons/<id>/parameters` | Параметры расчёта (ИИ) |
| POST | `/api/comparisons/<id>/saved-vor` | Подстановка сохранённого ВОР (ИИ) |
| POST | `/api/comparisons/<id>/retry` | Повтор стадии / выбор столбца объёма |
| GET | `/api/comparisons/<id>/files/<filename>` | Файл сравнения (`?download=1`) |

### Формирование ВОР по типовому шаблону

| Метод | Путь | Описание |
|-------|------|----------|
| GET/POST | `/api/vor-generation` | Список задач / создание (multipart: `ifc` обязательно; `pos`, `vor`, `pz`, `pd` опционально) |
| GET | `/api/vor-generation/<job_id>` | Статус задачи |
| GET | `/api/vor-generation/<job_id>/files/<filename>` | Файл задачи (`?download=1`) |
| GET | `/api/vor-generation/<job_id>/preview/<filename>` | Предпросмотр файла задачи |

## 9. Запуск и развертывание

### Docker (основной способ)

Требования: Docker + Docker Compose, GPU NVIDIA (CUDA). Порт наружу — `6001`
(внутри `6000`), Ollama — `11450` (внутри `11434`).

```bash
make up        # собрать и запустить (docker-compose up -d --build + логи)
make down      # остановить
make restart   # перезапуск
make logs      # логи
make clean     # остановить и удалить volumes (удаляет ollama_data)
```

Проксирование Nginx — по пути `/ifc-vor/` на `http://127.0.0.1:6001` (`nginx.conf`).

### Локальная рабочая копия

Разработка ведётся на локальной копии сервиса, изменения переносятся в Docker-сервис
**вручную** (автосинхронизации и CI/CD нет).

- Виртуальное окружение `.venv/` — все команды только через него.
- Запуск: `./start.sh` (активирует `.venv`, загружает `.env`), порт по умолчанию `6005`;
  для копии задаётся отдельное имя cookie `ifc_vor_dev_session`.
- Установка зависимостей: `./.venv/bin/pip install -r requirements.txt`.
- Скрипты и модули: `./.venv/bin/python <script.py>`, `./.venv/bin/python -m <модуль>`.
- Ollama вручную не запускается — только через docker-compose.

### Переменные окружения

- `OLLAMA_BASE_URL`, `LLM_BASE_URL` — адреса Ollama (в Docker `http://ollama:11434`,
  в рабочей копии `http://localhost:11450`);
- `OLLAMA_MODEL_NAME` — VLM для чертежей (Qwen3-VL-8B);
- `NORMS_LLM_MODEL`, `LLM_MODEL`, `KB_LLM_MODEL` — LLM для разбора документов и подбора;
- `LLM_TIMEOUT`, `LLM_TEMPERATURE`, `LLM_NUM_CTX`, `LLM_MAX_CONCURRENCY`, `LLM_PARALLEL`,
  `LLM_API_KEY` — параметры обращений к LLM;
- `KB_TIMEOUT`, `KB_WORKERS`, `KB_NUM_PREDICT`, `KB_THINK`, `KB_THINK_TIMEOUT`,
  `KB_THINK_NUM_PREDICT`, `KB_CACHE_PATH` — параметры `perechen_kb`;
- `CHECK_MODEL_LLM_MODEL` (fallback `LLM_MODEL` → `NORMS_LLM_MODEL`),
  `CHECK_MODEL_LLM_TIMEOUT`, `CHECK_MODEL_LLM_NUM_CTX`, `CHECK_MODEL_USE_LLM`,
  `CHECK_MODEL_CACHE_DIR` — проверка ЦИМ модели;
- `WORKS_SELECTOR` — `perechen` (основной, задан в `.env`) или `llm` (прежний
  `works_final_selector`); при отсутствии переменной код использует `llm`;
- `WORKS_SOURCE` — `local` (локальный справочник) или `api` (larix);
- `DOCUMENTS_PATH`, `KOEFS_PATH`, `PRICE_COST_PATH`, `MSSK_EXCEL_PATH`,
  `MSSK_NESTED_PATH`, `PERECHEN_KR_PATH`, `PERECHEN_XLSX`, `TREE_WORK_PATH`,
  `PARAMS_REGISTRY_PATH`, `ETALON_PATH`, `TSN_DIR`, `LOCAL_WORKS_PATH`,
  `LOCAL_RESOURCES_PATH`, `LOCAL_NORMS_PATH` — справочники `data/`;
- `WORKS_API_URL`, `WORKS_API_TOKEN`, `WORKS_RESOURCES_API_URL`,
  `WORKS_PERIOD_FILTER_URL`, `WORKS_WORK_PROCESS_URL`, `WORKS_WORK_PROCESS_DETAIL_URL` —
  API цифрового справочника ТСН / larix;
- `KEYCLOAK_TOKEN_URL`, `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_CLIENT_SECRET` — токен Keycloak;
- `UPLOAD_FOLDER`, `OUTPUT_FOLDER`, `SESSIONS_FILE`, `CACHE_PATH` — каталоги данных;
- `DOC_CACHE_DIR`, `PD_FULL_CACHE_PATH`, `DOC_FACTS_CACHE_PATH`, `KB_CACHE_PATH` —
  кеши разбора документов;
- `MAX_UPLOAD_MB` — лимит загрузки (по умолчанию 1024 МБ);
- `SESSION_COOKIE_NAME`, `SECRET_KEY` — cookie и подпись сессии;
- `VOR_LLM`, `ESTIMATE_TASKS_BASE_URL`, `CUSTOMER_DOCUMENT_MAX_CALLS`,
  `RETRIEVAL_TOP_K`, `PERECHEN_LIMIT_GROUPS`, `TSN_N` — тонкая настройка подбора
  работ, смет и разбора документов заказчика.

---

## 10. Режим ЦС (CS)

Документ описывает полный путь данных в режиме **CS** (ранее — режим **KR**,
«Конструктивные решения»): какие файлы принимаются на вход, какие модули в каком
порядке работают, какие файлы создаются на каждом шаге и какие справочники из
папки `data/` используются.

> **Переименование режимов.** Режим **КР переименован в «ЦС» («Цифровой
> сборник», публичный код `CS`)**, режим АР — в «ИИ» («Искусственный
> интеллект», `AI`). Оба режима обрабатывают одни и те же файлы (IFC/PDF),
> различается только пайплайн подбора работ. Публичный код режима в API и
> веб-интерфейсе — **`CS`** (значение по умолчанию); легаси-код `KR` (и русские
> «КР») по-прежнему принимается на входе и нормализуется к `CS`
> (`src/core/modes.py`), а внутренние коды пайплайна (`zero_step`,
> `group_excel`, `api_works_lookup`, `ifc_json_builder` и др.) остались
> прежними (`KR`). Имена артефактов (`final_result_KR.json`,
> `ОБЩИЙ_Финальный_перечень_работ.xlsx`, `perechen_kr*.xlsx` и др.)
> не меняются.

Режим **ЦС (ранее КР)** предназначен для конструктивных элементов модели (железобетонные
конструкции: стены, фундаменты, перекрытия, колонны, балки, лестницы, сваи, пандусы,
а также изоляция — `IfcCovering`). **Подбор работ** выполняется через **внешний API
справочника ТСН** (`digital-collection/building-elements/positions`): группы выбранных
элементов отправляются POST-запросами, из ответов формируется финальный перечень работ;
стоимость — по эндпоинту `works/resources` (fallback — `data/price_cost.xlsx`).
LLM-подбор (как в режиме ИИ) и константы проекта в ЦС **не используются**.

---

### 1. Входные данные

#### 1.1. Основной файл модели

| Источник | Эндпоинт | Описание |
|---|---|---|
| IFC-файл (`.ifc`) | `POST /api/upload_ifc` (multipart, поле `processingType=CS`; легаси `KR` принимается) | BIM-модель; элементы извлекаются через `ifcopenshell` |
| PDF-чертёж (`.pdf`) | `POST /api/upload_ifc` (multipart, `processingType=CS`) | Обрабатывается пайплайном машинного зрения `ai-blueprint-to-ifc`; затем для ЦС строятся JSON-справочники и ссылки на позиции цифрового сборника. Легенду можно задать вручную (`autoLegend=false` + `legend`, §3.6) |

`processingType` — строка `CS`/`AI` (легаси `KR`/`AR` принимаются и нормализуются),
по умолчанию **`CS`**; при неизвестном значении — тихий fallback на `CS`.

**Модели чертежей при локальном запуске.** `./start.sh` после загрузки `.env`
настраивает общий каталог весов `/home/projects/external/models` (тот же каталог
на хосте, который использует основной Docker-сервис). Переопределить его для
локальной копии можно через `LOCAL_BLUEPRINT_MODELS_DIR` в окружении или `.env`.
Скрипт экспортирует `MODELS_DIR`, отдельные пути `YOLO_LAYOUT_MODEL`,
`YOLO_LEGEND_LAYOUT_MODEL`, `DINO_HATCHING_MODEL`, `HATCH_FINDER_MODEL` и
абсолютный `MODEL_NAME` обоих профилей `WALL_DETECTION` /
`UNHATCHED_WALL_DETECTION`. Используются файлы `yolo_walls_obb.pt`,
`yolo_layout.pt`, `yolo_legend_layout.pt`, `dino_hatching.pt`, `hatch_finder.pt`;
копировать веса в `ai-blueprint-to-ifc/models` не требуется. Настройки путей
локального запуска имеют приоритет над одноимёнными контейнерными путями из
`.env`. Дополнительно `src/services/pdf_processor.py` перед импортом процессора
устанавливает эти же пути для локальной копии: это работает и при запуске Flask
напрямую, без `start.sh`, и заменяет устаревшие `/app/...` из окружения.
Docker-ветка (корень сервиса `/app`) не меняет пути моделей. Docker Compose и
настройки Ollama/Transformers не меняются. После изменения настроек или кода
нужно перезапустить локальный Flask: модуль `config` кэшируется после первого
импорта; старую ошибочную сессию следует заменить новой загрузкой чертежа.

#### 1.2. Параметры запуска (`POST /api/session/<id>/select_rows` или `/new_run`)

Тело JSON:

| Параметр | Описание |
|---|---|
| `rowIndices` / `allRows` | Индексы выбранных строк превью (0-based) или «выбрать все» |
| `rowTypes` | `{индекс: 'Надземная'\|'Подземная'\|'Цоколь'}` — часть здания для строки |
| `rowMaterials` | `{индекс: 'материал'}` — материал строки; **в ЦС материал можно переопределить вручную** (селектор «Материал для выбранных...» в интерфейсе доступен; в ИИ — отключён) |
| `rowMaterialCodes` | `{индекс: 'СТ xx xx xx'}` — уточнение **неполных** МССК-кодов материала, выбранное в превью (напр. «СТ 00 15» — «Железобетон» → «СТ 00 15 01»/«СТ 00 15 02» — сборный/монолитный); учитывается при группировке по материалу (см. §4.2) |
| `buildingHeight` | **Высота здания, м** — используется для фильтрации групп работ по высоте (`workGroups` в ответе API: «до 57», «более 57 до 75 м» и т.д.); без неё в перечень попадают расценки всех диапазонов высоты |
| `processingType` | `"CS"` (легаси `"KR"` также принимается и нормализуется к `"CS"`) |

Высоту здания можно обновить и отдельным эндпоинтом
`POST /api/session/<id>/filter_height` (`buildingHeight` в теле) — значение
сохраняется в сессии/запусках и применяется при следующем подборе.

Загрузка ПОС/пояснительной записки (`upload_pos`/`upload_pz`) и константы проекта
(`globalConstants`) — функциональность **режима ИИ (АР)**; в ЦС-конвейере не участвуют.

---

### 2. Общая схема пайплайна

```text
IFC / PDF ──► [Этап 0. Загрузка и извлечение элементов]  (фоновый поток, статус ifc_processing)
                ├─ ifc_raw_dump.py        → IFC_исходные_параметры.xlsx/.json (сырой дамп)
                ├─ zero_step.py          → ДЛЯ_СМЕТЧИКА_исправленный.xlsx, height.txt
                │                          (высота здания), IFC_глобальные_константы.json
                ├─ selection_template_builder.py (аддитивный шаг, не влияет на подбор КР)
                │                          → Параметры_подбора_элементов.json
                ├─ ifc_reference_builder.build_reference_from_ifc
                │                          → ifc_elements_output.json («все элементы.json»)
                │                          → ifc_raw_elements_grouped.json («группы элементов.json»)
                │                          → ifc_raw_elements.xlsx
                └─ position_links.py (фоновый поток, только КР)
                                           → position_links.json (ссылки на карточки позиций)
                                           → Отсутствующие_в_ЦС_группы.json / .xlsx
                статус → ifc_processed → selecting_rows (пользователь смотрит превью)

select_rows / new_run ──► [Этап 1. Запуск run_<NNN>]  (фоновый поток)
                ├─ применение материалов          → materials.json
                ├─ фильтрация строк               → filtered_elements.xlsx
                ├─ group_excel.process_ifc_excel_mssk (group_elements_mssk)
                │      (Часть здания → МССК → Материал → IFC-тип → Геометрия → Материал/Бетон)
                │                                 → Дерево_проекта_выбранные_элементы.xlsx
                │                                 → Дерево_проекта.xlsx (всё здание)
                ├─ листовые группы                → ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx
                │                                 → building_parts.json
                ├─ ifc_reference_builder.build_reference_output
                │      (после split_leaf_groups_by_part — разделение
                │       смешанных групп по частям здания, и
                │       split_leaf_groups_by_name — по типу элемента)
                │                                 → selected_elements_grouped.json
                │                                   (формат запроса API ТСН)
                ├─ api_works_lookup.fetch_works_from_api (POST API ТСН)
                │                                 → api_works_response.json (сырые ответы)
                └─ api_works_lookup.build_final_works_from_api
                   ├─ корректировка объёмов       (koefs.xlsx)
                   ├─ стоимость works/resources   → api_works_cost_response.json
                   │   (fallback — price_cost.xlsx)
                   └─                             → ОБЩИЙ_Финальный_перечень_работ.xlsx
                + ifc_json_builder (KRBuilder)    → final_result_KR.json
              статус → completed (файлы доступны на скачивание)
```

---

### 3. Этап 0 — загрузка файла и извлечение элементов

Координатор — `src/services/session_manager.py` (`SessionManager`). После
`POST /api/upload_ifc` создаётся сессия (`outputs/sessions.json` + папка
`outputs/<session_id>/`), файл сохраняется в `uploads/<session_id>/`, и в
`threading.Thread(daemon=True)` запускается `_process_ifc_bg` (для IFC) или
`_process_pdf_bg` (для PDF).

> **Производительность этапа 0 (актуализация).** Модель `ifcopenshell`
> открывается **один раз** на весь этап 0 и передаётся в `dump_ifc_elements_raw`
> и `zero_step` (аргумент `model`); удалён отладочный `analyze_qto_properties`
> (только печатал QTO и повторно открывал модель). Габариты bbox для плит/
> маршей считаются ручным обходом вершин вместо ядра `create_shape` — см. §3.2.
> Ускорение этапа 0 ≈10× при идентичных результатах (проверено: `ДЛЯ_СМЕТЧИКА_*.xlsx`
> совпадают, `IFC_глобальные_константы.json` идентичен).

#### 3.1. `ifc_raw_dump.py` — сырой дамп параметров

Функция `dump_ifc_elements_raw(ifc_path, session_dir, model=None)` выгружает
**все** параметры, QTO и материалы каждого элемента IFC без нормализации
(создаётся в обоих режимах; необязательный `model` — уже открытая модель
`ifcopenshell`, чтобы не открывать файл повторно):

- `outputs/<session_id>/IFC_исходные_параметры.xlsx` — таблица всех свойств;
- `outputs/<session_id>/IFC_исходные_параметры.json` — JSON-копия (список записей
  с ключом `GlobalId`).

#### 3.2. `zero_step.py` — извлечение и нормализация элементов (КР-ветка)

`zero_step(ifc_path, output_folder=session_dir, write_full_data=False, processing_type="KR", model=None)`:

- Необязательный аргумент **`model`** — уже открытая модель `ifcopenshell`
  (в `_process_ifc_bg` открывается один раз на весь этап 0).

- Извлекаются элементы по списку **`ELEMENT_TYPES_KR`** (только конструктив):
  `IfcWall`/`IfcWallStandardCase` (Стены), `IfcFooting` (Фундамент), `IfcSlab`
  (Перекрытия), `IfcColumn` (Колонны), `IfcBeam` (Балки), `IfcStair`/
  `IfcStairFlight` (Лестницы), `IfcRamp` (Пандусы), `IfcPile` (Сваи),
  `IfcCovering` (Изоляция). Архитектурные классы (`_ARCH_TYPES`: окна, двери,
  витражи, кровля и т.д.) — только режим АР.
- Нормализуются характеристики (тип, материал, этаж/часть здания, код МССК из
  Pset `ExpCheck_*::MGE_ElementCode`), количества считываются из QTO либо по bbox
  (`ifc_raw_dump._compute_bbox_quantities`).
- **По-элементный bbox-фолбэк периметра/толщины для опалубки (оба режима — КР и АР)**:
  QTO может быть у файла в целом, но у отдельных элементов — неполным
  (`IfcSlab` без `Perimeter`/`Depth` — приямки, часть фундаментных плит;
  `IfcStairFlight` — `Qto_StairFlightBaseQuantities` не содержит `Perimeter`/
  `Depth` вовсе). Для таких элементов в `get_element_info` вызывается
  `ifc_raw_dump._compute_formwork_bbox_quantities` (габариты меша в мировых
  координатах): плита — периметр = 2×(два наибольших габарита bbox), толщина =
  наименьший габарит; лестничный марш — периметр = 2×длина по склону,
  толщина = ширина (периметр × толщина = площадь двух боковых граней марша).
  Значения пишутся в нормализованные колонки `Длина_Perimeter_мм` /
  `Длина_Depth_мм` (без этого в АР объёмы опалубочных работ не заполнялись;
  парсинг режимов идентичен).
  **Производительность:** для «простых» представлений (экструзия, тесселяция,
  граненая B-Rep без отверстий, `IfcMappedItem` над ними) габариты считаются
  **ручным обходом вершин** (`_fast_bbox_mm` без ядра `create_shape`) — те же
  вершины, что и у ядра, без триангуляции; для сложной геометрии
  (boolean/CSG/swept/advanced brep и т.п.) используется ядро. Результаты
  идентичны, время — на порядки меньше.
- **Высота здания — по максимальному уровню элементов**: «Высота_надземной_части_м»
  = максимум `Уровень_этажа_мм` среди **всех** элементов модели (раньше высота
  считалась только по этажам типа Цокольный/Надземный/Технический/Мансардный,
  из-за чего этаж «Крыша» (тип Кровля) исключался и высота занижалась); если
  уровней нет — высота по отметкам этажей.
- Колонка **`Тип_этажа`** в КР заполняется функцией `classify_storey_type`
  (по словам в имени этажа + отметке этажа) — в отличие от АР, где действует
  строгая классификация `_classify_storey_type_ar` по числовому индикатору.
- Этажи классифицируются → часть здания (Подземная / Цоколь / Надземная).

Создаваемые файлы (в корне сессии, затем переносятся в `original/`):

| Файл | Содержимое |
|---|---|
| `ДЛЯ_СМЕТЧИКА_исправленный.xlsx` | Лист `Данные` (по строке на элемент — превью для выбора), лист `Сводка_по_типам`, лист `Высота_здания` |
| `height.txt` | В КР — **высота здания** (в АР — высота основного этажа). Legacy-файл: читается только как fallback |
| `IFC_глобальные_константы.json` | Глобальные константы модели (высоты, отметки); в КР используется преимущественно для высоты здания |
| `IFC_ВСЕ_ДАННЫЕ*.xlsx` | Полная таблица всех элементов (кеш «Дерева_проекта») |

#### 3.3. `selection_template_builder.py` — шаблон параметров подбора

После `zero_step` строится универсальный шаблон параметров подбора работ по каждому
элементу (`data/selection_parameters.json` + карта
`data/selection_parameters_mapping.json`) →
`outputs/<session_id>/Параметры_подбора_элементов.json`. Шаг выполняется в обоих
режимах и является **аддитивным**: на подбор работ в КР и остальные результаты
не влияет (используется только конвейером АР).

#### 3.4. `ifc_reference_builder.py` — JSON-справочники цифрового сборника (только КР)

`build_reference_from_ifc(ifc_path, session_dir, processing_type="KR")` — три этапа:

1. **`extract_elements_from_ifc`** — извлечение всех элементов из IFC в
   промежуточный Excel `ifc_raw_elements.xlsx` (лист `Данные`);
2. **`group_elements_by_type`** — группировка через `group_excel.process_ifc_excel`
   (тот же путь, что «Авто-группировка» в веб-интерфейсе);
3. **`build_reference_output`** — преобразование листовых групп дерева в целевой
   формат запроса API ТСН.

Создаваемые файлы (в корне сессии):

| Файл | Отображаемое имя | Содержимое |
|---|---|---|
| `ifc_elements_output.json` | «все элементы.json» | Все элементы модели в формате справочника |
| `ifc_raw_elements_grouped.json` | «группы элементов.json» | Группы элементов в формате запроса API ТСН: `buildingElementName`, `totalMeasure` (`{type: volume\|area\|count, value, unit}`), `characteristics`, `additionalCharacteristics`, `elementCount`, `totalAreas`, служебные поля `_reinforcementVolumeRatio` (суммарный расход арматуры группы, кг),
`_formworkArea` (площадь опалубки вертикальных граней фундаментных плит, м²),
`_isPrecast` (признак сборного железобетона — см. п. 4.3),
`_buildingPart` (часть здания группы — Подземная/Цоколь/Надземная, по `path[0]`;
используется финальным перечнем для разбивки таблицы на части),
`_typeKey` (тип элемента без номера экземпляра — основа заголовка группы
итогового перечня в формате режима ИИ) и `_rawMaterial` (сырой материал
первого элемента группы — для того же заголовка) — в запрос API не включаются |
| `ifc_raw_elements.xlsx` | — | Промежуточная таблица всех элементов |

**Геометрические характеристики элементов** (`characteristics`, например
«Толщина: до 200») определяются `_get_geometry_range_for_element` по
`GEOMETRY_GROUP_RULES`. Порядок источников значения геометрии:
1. колонка поля правила (для стен — `Ширина, мм`);
2. «сырые» QTO-колонки (для стен — `Длина_Width_мм`, затем ширина сечения);
3. **толщина из имени элемента** (`_geometry_from_name` →
   `zero_step.parse_name`, только стены): у части стен модели нет
   QTO-количеств (пусты QTO Width и все площади), а толщина известна
   только из имени («Стена_180мм_ЖБ_B35_W4_F75» → 180 мм). Без этого
   фолбэка у таких элементов не было характеристики «Толщина» в
   `ifc_elements_output.json`, и их группы предпросмотра получали ссылку
   на карточку позиции с другой толщиной (первую попавшуюся — например,
   «Стена … толщиной до 100 мм» для стены 180 мм).

Для PDF-источника — `build_reference_from_pdf` (те же выходные файлы).

Оба JSON регистрируются в списке файлов сессии (кнопки «Элементы (JSON)» и
«Группы (JSON)» в веб-интерфейсе — в АР скрыты).

#### 3.5. `position_links.py` — ссылки на позиции цифрового сборника (только КР)

Сразу после обработки IFC/PDF (до нажатия «Запустить обработку») в фоновом потоке
(`SessionManager._start_position_links_bg` → `position_links.build_position_links`)
читается `ifc_elements_output.json` (поэлементный справочник в формате API) и
каждая группа **предпросмотра** отправляется в API справочника ТСН (тот же
эндпоинт и формат запроса, что при подборе работ). Группы строятся по строкам
предпросмотра — nameKey = «Имя элемента» без хвостового цифрового ID, вариант =
(nameKey, часть здания, геометрический диапазон); вариант задаётся payload'ом
первого элемента варианта. Это не листовые группы группировки: несколько nameKey
попадают в одну листовую группу (например, стены 220 и 250 мм внутри геометрической
группы «до 300»), и каждому нужен свой запрос — иначе у части групп предпросмотра
не было ссылок, хотя работы к ним подбирались.

Из ответов берутся id позиций → **`outputs/<session_id>/position_links.json`**:

```json
{
  "<Имя элемента без цифрового ID>": [
    {
      "part": "Надземная",
      "geo": "до 200",
      "positions": [
        {"id": 1391, "name": "Стена надземной части здания толщиной до 200 мм",
         "group": "до 57"}
      ]
    }
  ]
}
```

Позиции фильтруются по характеристикам группы: точное совпадение («Расположение»,
«Материал») и числовые диапазоны («Толщина: более 150 до 200») — по сырому значению
геометрии из `additionalCharacteristics`. Для каждого варианта группы сохраняется
ровно одна (первая отфильтрованная) позиция. Вариант, для которого API не вернул
позиций (или запрос не удался), сохраняется с **пустым списком `positions`**.
Фронтенд по выбранному варианту рисует кликабельную иконку 📎 со ссылкой
`https://digital-collection.mgexp.org/building-elements/position/<id>`; для
варианта с пустым `positions` — серую некликабельную иконку «группа отсутствует
в цифровом сборнике». Статус отдаёт эндпоинт
`GET /api/session/<id>/position_links` (в АР не запрашивается).

**Параметр `?group=` ссылки (группа работ по высоте здания)**: у части позиций ЦС
работы сгруппированы по диапазонам высоты («до 57», «более 57 до 75 м»,
«более 75 до 105 м» …) — карточка такой позиции открывается по ссылке с
параметром `?group=<имя группы>` (например, `.../position/464?group=до+57`).
Имя группы берётся из поля `group` элемента `workGroups` ответа API
(`_position_height_group`) — у позиции, уже отфильтрованной по высоте здания,
остаются только подходящие группы работ, поэтому имя первой группы с диапазоном
соответствует фактической высоте. Позициям, у которых ни у одной группы работ нет
диапазона высоты (работы от высоты не зависят, например «Фундаментная плита»),
параметр не нужен — ссылка остаётся без него (`group` = `""`). Если высота здания
не определена, параметр не добавляется (группу выбрать нельзя). Поле `group`
сохраняется у каждой позиции в `position_links.json`, фронтенд подставляет его
в ссылку (URL-кодирование, пробелы → `+`).

Ссылка собирается на фронтенде (`positionLinksHtml` в `src/templates/index.html`):
`.../position/<id>` и, если у позиции непустое `group`, `?group=<группа>`
(например, `.../position/465?group=более+75+до+105+м`). Ответ эндпоинта
`GET /api/session/<id>/position_links` проходит через Pydantic-схемы
`PositionLinkItem` / `PositionLinkVariant` (`src/schemas.py`): **любое поле,
добавляемое в `position_links.json`, должно быть объявлено в этих схемах** —
иначе оно молча вырезается из ответа, и иконка ведёт на карточку без группы
(тогда ЦС сам открывает первую группу высоты по умолчанию — «до 57», независимо
от реальной высоты здания).

Варианты, отсутствующие в цифровом сборнике, дополнительно сохраняются отдельными
файлами в корне сессии:
- **`outputs/<session_id>/Отсутствующие_в_ЦС_группы.json`** — полный список записей
  `{nameKey, part, geo, buildingElementName, characteristics, elementCount, reason}`
  (причина — «Позиции в цифровом сборнике не найдены» либо текст ошибки запроса);
- **`outputs/<session_id>/Отсутствующие_в_ЦС_группы.xlsx`** — табличный вид
  (№ п/п, Часть здания, Группа элементов, Тип позиции ЦС, Материал, Геометрия,
  Кол-во элементов, Причина).

Часть здания варианта определяется `_resolve_part`: **строго** по
«Расположению» → «Этажу» → «Типу этажа» (`_extract_part`, «Тип этажа» —
только последний fallback, когда «Расположения» и «Этажа» нет). Числовой
индикатор «Этажа» приоритетен: 1-й этаж с числовым индикатором «1» относится к
надземной части даже при «Типе этажа» = «Цокольный» — отметка 0,000 ошибочно
размечала 1-й этаж как цокольный (та же строгая логика, что в
`ifc_reference_builder._element_part` при разделении смешанных групп;
переквалификация по «Типу этажа» **не выполняется**). Если часть варианта
расходится с характеристикой «Расположение» элемента (например,
«Надземная часть здания» по индикатору «Этажа» при «Типе этажа» =
«Цокольный»), в payload запроса `Расположение` подменяется на часть варианта
(`_override_location`) — иначе API возвращает позиции чужой части здания, и
иконка в превью ведёт на карточку другой позиции (например, «Стена надземной
части…» вместо «Стена цокольной части…»).

Высота здания учитывается при отборе позиций: работы позиций ЦС сгруппированы
по диапазонам высоты («до 57», «более 57 до 75 м», «более 75 до 105 м» …), и
позиции, у которых для высоты здания нет ни одной подходящей группы работ, в
финальный перечень не попадают (api_works_lookup._filter_response_by_height) —
ссылка на них не формируется (вариант сохраняется с пустым `positions` и
пояснением в `reason`). Отбор позиций идёт тем же конвейером, что при подборе
работ (`_filter_response_by_height` → `_select_best_positions` → фильтрация по
характеристикам), поэтому иконка в предпросмотре ведёт на карточку именно той
позиции, работы которой попадают в перечень для заданной высоты.

Высота здания для ссылок определяется `detect_building_height` из файлов сессии
(`IFC_глобальные_константы.json` → `constants.building_height_m.value`,
fallback — `height.txt`). При изменении высоты пользователем
(`POST /api/session/<id>/filter_height`, в КР вызывается фронтендом при
изменении поля «Высота здания» в превью) `position_links.json` удаляется и
**пересобирается в фоне с новой высотой**; фронтенд по `ready=false`
дожидается пересборки и обновляет иконки. Повторные изменения высоты во время
пересборки не запускают параллельных потоков — новая пересборка стартует после
текущей (по факту — при следующем изменении).

По завершении статус сессии → `ifc_processed` / `selecting_rows`. В превью КР
колонки **«Часть здания»** и **«Материал»** редактируемы пользователем (в АР —
только отображение). Часть здания строки в превью определяется фронтендом
**строго по числовому индикатору «Этажа»** («-N/M» → «Цоколь», «-N» →
«Подземная», «N»/«Крыша» → «Надземная») — синхронно с бэкендом
(`_element_part`); переквалификация по колонке «Тип_этажа» не выполняется.

**Неполный МССК-код материала.** Если код материала неполный (в справочнике
`data/materials_mssk_nested.json` у него есть дочерние узлы, напр. `СТ 00 15` —
«Железобетон»), материал определён неоднозначно (сборный/монолитный). Такая
строка группы материала в превью **подсвечивается**, и в неё добавляется
**селект** «сборный/монолитный» (варианты — дочерние коды справочника,
`options`/`incomplete` в `materialsGroupMap`, вычисляются
`materials_lookup.material_code_options`). Выбор пользователя отправляется
полем `rowMaterialCodes` (см. §1.3) и учитывается при группировке (§4.2).
То же поведение — в превью ИИ (общий код `groupRowsCommon` в
`src/templates/index.html`).

**Каскадная подсветка.** Подсветка распространяется на все строки-предки
неполной группы материала: вместе с группой материала подсвечиваются строки
кода МССК и части здания («Подземная часть» и т.п.), в которые она входит.
При выборе материала подсветка группы снимается, а подсветка предка гаснет
**только когда уточнены все** неполные группы материала, входящие в него
(функции `refreshIncompleteHighlight`/`setParentHighlight` в
`groupRowsCommon`; цепочка предков — по карте `groupParent`). Возврат селекта
в значение «— выберите —» возвращает подсветку.

#### 3.6. Ручная легенда чертежа (PDF) — `pdf_processor.py` + `ai-blueprint-to-ifc/`

Общая для КР и АР функциональность (реализация — в общем PDF-пути). При загрузке
PDF-чертежа (`POST /api/upload_ifc`) можно отключить авто-определение легенды и
передать собственный список обозначений (multipart-поля `autoLegend` и `legend`):

- `autoLegend=true` (по умолчанию) — легенда определяется пайплайном
  машинного зрения как раньше;
- `autoLegend=false` — используется ручная легенда `legend` — JSON-строка вида
  `{"items": [{"label": "Кирпич", "image": "data:image/png;base64,..."}, ...]}`;
  `routes.py` валидирует структуру (непустые `label`/`image`, `image` — data URL).

Проброс: `SessionManager.process_pdf(..., manual_legend=...)` → `_process_pdf_bg`
→ `_process_pdf_with_progress` → `pdf_processor.process_pdf(..., manual_legend=...)`;
в корень сессии сохраняется `legend.json`, в состоянии сессии — `auto_legend` /
`manual_legend`. `pdf_processor` сохраняет `legend.json` и `legend_item_N.<ext>`
и строит `materials_colors.md` по ручной легенде; при её наличии
`ai-blueprint-to-ifc/processor.py` идёт веткой `WallDetectionPipeline.run(manual_legend=...)`
с `HatchingProcessor.set_legend(items)` вместо авто-разбора легенды.

---

### 4. Этап 1 — запуск обработки (`run_<NNN>`)

`POST /api/session/<id>/select_rows` (первый запуск) или `.../new_run` (повторный)
→ создаётся `outputs/<session_id>/run_<NNN>/` → фоновый поток
`_run_processing_pipeline_in_run`.

#### 4.1. Применение материалов и фильтрация строк

1. **Материалы** (`rowMaterials`, в КР могут быть заданы вручную) записываются в
   колонку «Материал» листа `Данные`; карта сохраняется в `run_<NNN>/materials.json`.
2. Из исходного Excel берутся только выбранные строки (`rowIndices`) →
   **`run_<NNN>/filtered_elements.xlsx`**.
3. Из `IFC_исходные_параметры.json` строится карта `GlobalId → строка материалов`
   и подмешивается колонкой `Свойство::IfcMaterialLayer::Name` в
   `filtered_elements.xlsx` (как и в АР).
4. **Уточнение неполных МССК-кодов материала** (`rowMaterialCodes`): карта
   пересчитывается из исходных индексов строк в позиции отфильтрованного
   DataFrame и передаётся в группировку (`material_code_overrides`). Для строк
   с уточнением группа материала (L3) определяется по указанному полному коду
   (напр. «СТ 00 15 01» — «Железобетон сборный»), а не по неполному значению
   из параметров элемента («СТ 00 15» — «Железобетон»).

#### 4.2. Группировка — `group_excel.py` (`process_ifc_excel_mssk`)

Обёртка с дисковым кешем вызывает `group_elements_mssk`. Иерархия группировки КР:

```text
Часть здания (L1) — Подземная (до отм. 0,000) / Цоколь (от отм. 0,000) /
                    Надземная (выше отм. 0,000)
  → Код МССК (L2) — название из data/elements_mssk_nested.json (mssk_lookup);
                    пустые/неизвестные коды → «Прочее»
    → Материал (L3) — группа по коду МССК материала из
                      data/materials_mssk_nested.json (materials_lookup):
                      «Железобетон сборный (СТ 00 15 01)» и т.п.; нет кода → «Прочее»
      → IFC-тип (L4, только если в группе несколько типов)
        → Геометрия (L5) — по GEOMETRY_GROUP_RULES: стены — Ширина, мм (толщина),
                           плиты/балки — Площадь, м2, колонны — Периметр, мм
                           (+ подгруппа «Сторона» по ширине), сваи — Длина, мм и т.д.
          → Материал (L6) — «Материал: …» (только при нескольких материалах)
            → Бетон (L7) — «Бетон: В…, W…, F…» (по ExpCheck_MaterialConcrete_*)
```

Параметр `material_code_overrides` функции `group_elements_mssk` — уточнение
**неполных** МССК-кодов материала из `rowMaterialCodes` (см. §4.1 п. 4): для
строки с уточнением группа материала (L3) определяется по указанному полному
коду, что разделяет «Железобетон (СТ 00 15)» на «Железобетон сборный
(СТ 00 15 01)» / «Железобетон монолитный (СТ 00 15 02)». При наличии уточнений
дисковый кеш группировки игнорируется
(`_get_cached_or_compute(force_recompute=True)`).

Группировка **включает часть здания** (L1); в режимах ИИ и Архитектура
используется та же иерархия и та же функция. Агрегация в каждом
узле (`_create_group`):

- **объём группы** — coalesce: по каждой строке берётся первое ненулевое значение
  среди **всех** колонок-источников объёма (`_find_volume_columns`: NetVolume
  Qto_Wall/Qto_Slab/Qto_Beam/… + агрегированная «Объём, м3»);
- **суммарные площади** (`total_areas`) — по колонкам площадей;
- **суммарный расход арматуры** (`total_reinforcement`, кг) —
  Σ (ReinforcementVolumeRatio, кг/м³ × объём элемента, м³);
- **площадь опалубки плит** (`formwork_area`, м², в геометрических листовых
  группах `_add_geometry_groups`/`create_geo_group`, по-элементно
  `_element_formwork_area_m2`):
  - фундаментная плита («фундамент» в имени) — боковые граны:
    **периметр × толщина**;
  - перекрытие («перекрыт» в имени) — боковые грани + нижняя поверхность:
    **периметр × толщина + площадь плиты**;
  - прочие плиты (лестницы, приямки) — периметр × толщина.
  Периметр/толщина — колонки `Периметр, мм` / `Длина_Depth_мм` (mm → м²
  через ÷1e6); при отсутствии QTO-толщины у плит IfcSlab толщина берётся
  из имени элемента («Перекрытие_200мм_…»). При отсутствии QTO-периметра/
  толщины у отдельного элемента срабатывает по-элементный bbox-фолбэк из
  `zero_step` (см. п. 3.2): значения уже заполнены в колонки
  `Длина_Perimeter_мм` / `Длина_Depth_мм` (для маршей периметр × толщина
  даёт площадь двух боковых граней).

Создаваемые файлы:

| Файл | Откуда |
|---|---|
| `run_<NNN>/Дерево_проекта_выбранные_элементы.xlsx` | дерево групп по выбранным строкам |
| `run_<NNN>/Дерево_проекта.xlsx` | дерево по **всему** зданию (кеш из `IFC_ВСЕ_ДАННЫЕ*.xlsx`) |
| `run_<NNN>/ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx` | листовые группы: агрегаты `*_grouped`, `Название_группы`, `Уровень_группы`, `Индексы_элементов` |
| `run_<NNN>/building_parts.json` | часть здания для каждой группы (модальная из элементов, по умолчанию «Надземная») |

#### 4.3. Группы для API — `ifc_reference_builder.build_reference_output`

Листовые группы дерева группировки преобразуются в формат запроса API ТСН и
сохраняются в **`run_<NNN>/selected_elements_grouped.json`** (те же поля, что у
`ifc_raw_elements_grouped.json`: `buildingElementName`, `totalMeasure`,
`characteristics`, `additionalCharacteristics`, `totalAreas`,
`_reinforcementVolumeRatio`, `_formworkArea`, `_buildingPart` — служебное
поле с частью здания группы (Подземная/Цоколь/Надземная), заполняется
`build_reference_output` по `path[0]` группы и в запрос API не включается;
используется при формировании финального перечня работ для разбивки таблицы
на части здания; плюс `_isPrecast` — служебное поле-признак сборного
железобетона, см. ниже). Рабочий артефакт запуска: в итоговую выдачу (список
файлов для скачивания) не входит, остаётся в `run_<NNN>/`.

**Признак сборного железобетона — `_isPrecast`.** В цифровом сборнике (ЦС)
содержатся только монолитные конструкции, поэтому для сборных элементов работы
из ЦС не подбираются. Признак определяется в `ifc_reference_builder`
(`_is_precast_element`) по «сырым» данным группы, не меняя нормализованное
имя материала (в характеристике «Материал» остаётся «Железобетон» —
существующие группы не ломаются):

1. материал элемента (`ExpCheck_*::MGE_Material`): «Железобетон сборный» и т.п.;
2. свойство `ConstructionMethod` (`Pset_ConcreteElementGeneral`) = `Precast`;
3. уровень материала в пути группировки («Железобетон сборный
   (СТ 00 15 01)» из `materials_mssk_nested.json`).

**Разделение групп по частям здания — `split_leaf_groups_by_part` (перед
преобразованием).** В одной группе могут оказаться элементы разных частей здания
(например, стены одного типоразмера на 1-м цокольном этаже и на надземных
этажах), а параметры запроса (`Расположение`, `Этаж` и т.д.) берутся по первому
элементу группы — вся группа уходила в API как одна часть здания, и расценки
остальных частей не попадали в перечень. Теперь перед формированием запроса
смешанная группа делится на подгруппы по частям здания
(надземная / подземная / цокольная):

- часть элемента — **строго по числовому индикатору «Этажа»**
  (`_element_part`): «-N/M» → цокольная, «-N» → подземная, «N»/«Крыша» →
  надземная. Переквалификация по колонке «Тип_этажа» **не выполняется**:
  отметка 1-го этажа (0,000) ошибочно размечала его как «Цокольный», и
  элементы 1-го этажа попадали в цокольную часть здания. Если числовой
  индикатор не распознан («Этаж» пуст, «-», «Крыша» и т.п.) — элемент
  остаётся в базовой части группы (path[0] дерева группировки);
- каждая подгруппа получает свои `elementCount`, `totalMeasure` (объём/площадь),
  `totalAreas`, `_reinforcementVolumeRatio`, `formwork_area` (суммы по
  элементам подгруппы, включая площадь опалубки) и `first_element` — первый
  элемент **своей** части (из него берутся `additionalCharacteristics`);
  сумма показателей подгрупп равна показателям исходной группы;
- `path[0]` подгруппы заменяется на метку её части здания — по ней
  `build_reference_output` формирует характеристику «Расположение»;
- несмешанные группы проходят без изменений.

**Разделение групп по типу элемента — `split_leaf_groups_by_name` (перед
преобразованием, после `split_leaf_groups_by_part`).** Группировка итогового
перечня работ КР приведена к виду режима ИИ (`perechen_pipeline`): элементы
одного типоразмера (совпадают имя элемента без № экземпляра и материал)
образуют **отдельную группу**, поэтому «Кол-во», объёмы и работы считаются по
конкретному типоразмеру, а не по геометрическому диапазону целиком (например,
`Базовая стена:Стена_200мм_ЖБ_B35_W6_F150` и
`Базовая стена:Стена_180мм_ЖБ_B35_W6_F150` вместо общей группы
«Толщина: до 200 мм | Бетон: B35»). Ключ типа — `_element_type_key`
(имя без № экземпляра, совпадает с `perechen_kb.type_key`).

- каждая подгруппа получает свои `elementCount`, `totalMeasure`, `totalAreas`,
  `_reinforcementVolumeRatio`, `formwork_area` и `first_element` — суммы по
  элементам подгруппы считаются общим хелпером `_recompute_group_aggregates`
  (используется и в `split_leaf_groups_by_part`); **сумма показателей подгрупп
  равна показателям исходной группы — объёмы не меняются**;
- группировка и заголовки групп итогового перечня КР совпадают с режимом ИИ
  (см. §4.5).

#### 4.4. Подбор работ через API ТСН — `api_works_lookup.fetch_works_from_api`

1. Каждая группа из `selected_elements_grouped.json` отправляется **отдельным
   POST-запросом** на эндпоинт `WORKS_API_URL`
   (`digital-collection/building-elements/positions`, `pageSize=1000`) — API
   принимает один объект, не массив. Служебные поля с префиксом `_` в запрос
   не включаются.
   **Группы сборного железобетона (`_isPrecast`) пропускаются** — запрос не
   отправляется (в ЦС только монолитные конструкции). Причина фиксируется
   в ответе группы (`_skipReason` → `api_works_response.json`) и в заголовке
   группы финального перечня работ.
2. **Авторизация** — Bearer-токен Keycloak (`KeycloakTokenProvider`,
   client_credentials; при 401 токен обновляется и запрос повторяется один раз);
   fallback — статичный `WORKS_API_TOKEN` из `.env`.
3. **Фильтрация по высоте здания** (`_filter_response_by_height`): API возвращает
   работы позиции, сгруппированные по высоте («до 57», «более 57 до 75 м», …) —
   фильтрацию по `buildingHeight` выполняет клиент. Группы без диапазона
   («Уход за бетоном») сохраняются всегда; без высоты — ответ не фильтруется.
4. **Отбор позиций** (`_select_best_positions`): остаются позиции, у которых нет
   характеристик, отсутствующих в запросе группы (имена характеристик позиции ⊆
   имён характеристик группы) — отсекает, например, «Фундаментную плиту под
   оборудование» у обычной фундаментной плиты. Если точных нет — все (fallback).

Сырые ответы сохраняются в **`run_<NNN>/api_works_response.json`**
(`{results: [{element, response}]}`) — основной файл отладки «почему элемент
не попал в перечень».

#### 4.5. Финальный перечень работ — `api_works_lookup.build_final_works_from_api`

Формирует **`run_<NNN>/ОБЩИЙ_Финальный_перечень_работ.xlsx`** (лист `Данные`).
Колонки: **«Шифр ТСН», «Наименование расценки/ресурса», «Ед. изм.», «Объём работ»,
«ЗП», «ЭМ», «МР», «Стоимость»**. Порядок расчёта:

1. **Работы**: из отобранных позиций берутся все работы (`_iter_works` —
   поддерживает форматы: работы в `workGroups`, вложенные `works`, плоский список);
   Шифр ТСН = `works/code`, наименование = `works/name`, ед. изм. =
   `_resolve_unit_label` (обозначение соответствует делителю: «м²», «100 м²»).
2. **Объём работ** (`_calculate_work_volume`) — по `totalMeasure` группы:
   м³ → объём, м² → площадь, шт → количество, делённые на делитель нормы
   (`_work_divisor`: `okeiValue` из API, иначе числовой префикс единицы
   «100 м2» → 100). Особые случаи:
   - расценка в **м²** при измерителе группы «объём»: монтаж/демонтаж опалубки
     — по площади опалубки плит `_formworkArea` (фундаментные плиты —
     периметр × толщина; перекрытия — периметр × толщина + площадь плиты).
     Для **плитных** групп (фундаментная плита / перекрытие) fallback на
     площадь плиты из `totalAreas` запрещён: если `_formworkArea` не
     рассчитан (нет периметра/толщины), объём остаётся пустым — площадь
     плиты не является площадью опалубки. Прочие расценки в м² (например,
     опалубка стен) — по суммарной площади группы из `totalAreas`
     (`_pick_area_value`, приоритет «Площадь, м2» → GrossSideArea →
     NetSideArea → GrossArea → NetArea);
   - **арматурные работы** («арматур», «каркас», «стержн», «сетк», «закладн»):
     объём = суммарный расход арматуры группы (`_reinforcementVolumeRatio`, кг)
     ÷ 1000 подставляется **ровно в одну** расценку — «Установка … отдельных
     стержней …»; остальные арматурные расценки (каркасы/сетки/закладные детали)
     идут без объёма (иначе объём задваивается).
3. **Корректировка объёмов** (`works_cost._get_corrected_volume`) — по нормам
   расхода из **`data/koefs.xlsx`**: для пар «Шифр ТСН + наименование ресурса»
   объём умножается на норму (норма > 100 — × норма/100, иначе × норма).
4. **Стоимость** (`fetch_work_costs`) — работы (дедупликация по `id`, батчи по
   100) отправляются POST на `WORKS_RESOURCES_API_URL` (`works/resources`,
   формат `{"works": [...], "wrate": false, "coefficient": true}`); из ответа
   берутся показатели стоимости одной измерительной единицы: `curSalary` (ЗП),
   `curOperationOfMachines` (ЭМ), `curCostOfMaterialResources` (МР).
   **ЗП / ЭМ / МР = показатель × Объём работ; «Стоимость» = ЗП + ЭМ + МР.**
   При недоступности API — fallback на `data/price_cost.xlsx`
   (`works_cost._add_cost_column`, только «Стоимость»). Сырые ответы —
   `run_<NNN>/api_works_cost_response.json`.
5. **Форматирование** — денежные колонки форматируются
   `works_cost.format_money` («392 458.21»).
6. **Разбивка на части здания** — таблица делится на секции
   **«ПОДЗЕМНАЯ ЧАСТЬ» / «ЦОКОЛЬНАЯ ЧАСТЬ» / «НАДЗЕМНАЯ ЧАСТЬ»** (порядок —
   Подземная → Цоколь → Надземная; порядок задаёт `BUILDING_PART_ORDER`
   в `api_works_lookup.py`, тексты — `_BUILDING_PART_TITLES`). Часть каждой
   группы определяется
   `_resolve_building_part`: приоритет — служебное поле `_buildingPart`
   (см. п. 4.3), затем характеристика «Расположение», fallback —
   «Надземная». Перед работами каждой части — строка-заголовок части
   (жирный шрифт, тёмно-серая заливка), после них — итоговая строка
   «ИТОГО по подземной/цокольной/надземной части:» (жирный шрифт,
   суммы ЗП/ЭМ/МР/Стоимость по работам части) и пустая строка.
7. **Заголовки групп — в формате режима ИИ**: перед работами каждой группы —
   строка-заголовок **«<Имя элемента без № экземпляра> (<Материал>)
   Кол-во: N»** (жирный шрифт, серая заливка) — как в
   `perechen_pipeline._write_xlsx` (ИИ). Имя элемента — служебное поле
   `_typeKey` (см. п. 4.3), материал — `_rawMaterial` (сырой, без
   нормализации); «Кол-во» выводится всегда. После каждой группы (включая
   последнюю в части) — пустая строка; в конце таблицы — общая
   строка **«ИТОГО:»** (сумма по всем работам всех частей; итоговые
   строки частей при этом не задваиваются — суммируются только строки
   работ); автофильтр по таблице. Для групп сборного железобетона
   (пропущенных при подборе) в заголовок добавляется причина пропуска —
   «(Сборный железобетон — в цифровом сборнике только монолитные
   конструкции, работы из ЦС не подбираются)».
   Ранее заголовок был «<Тип элемента> (<Материал>, <Имя из Revit>)
   Кол-во: N», «Кол-во» — только при N > 1.

#### 4.6. Сборка финального JSON — `ifc_json_builder.py` (KRBuilder)

`build_final_json(input_folder=run_dir, source_file=<имя IFC/PDF>, discipline="KR",
output_filename="final_result_KR.json")` — сопоставляет группы
(`selected_elements_grouped.json`), элементы и подобранные работы в единый
выходной JSON **`run_<NNN>/final_result_KR.json`**.

**Позиции работ** (`KRBuilder._build_positions_from_api`) строятся из ответов
API (`api_works_response.json`, сопоставление группы и результата — по
«Имени элемента» из `additionalCharacteristics`). Работы извлекаются из
актуального формата ответа API — `data[].workGroups[].works` (поддерживается
и устаревший `data[].works`). Отбор позиций повторяет логику финального
перечня работ: остаются позиции, у которых нет характеристик, отсутствующих
в запросе группы (имена характеристик позиции ⊆ имён характеристик группы);
если точных нет — все (fallback). Работы **«Уход за бетоном …» добавляются
всегда, если они есть в ответе API**, — даже если они пришли в позициях,
отсечённых отбором по лишним характеристикам (например, «Фундаментная плита
под оборудование …»): уход за бетоном обязателен для любой монолитной
конструкции и не зависит от лишних характеристик позиции, в которой пришёл.
Дедупликация работ — по шифру расценки (`works/code`). «Объём» позиции
рассчитывается по измерителю группы: м³ → `totalMeasure` группы (объём),
м² → суммарная площадь (`totalAreas`), т → расход арматуры
(`_reinforcementVolumeRatio`, кг → т ÷ 1000); без подходящего измерителя —
`quantityStatus = "missing_data"`.

#### 4.7. Финализация

Состав итоговой выдачи запуска КР (файлы для скачивания через
`GET .../download/<filename>` и `download_all`) — **только итоговая таблица
работ**:

- **`ОБЩИЙ_Финальный_перечень_работ.xlsx`** — итоговая таблица работ.

Остальные артефакты (`selected_elements_grouped.json`,
`final_result_KR.json`, справочные JSON корня сессии
`ifc_elements_output.json` / `ifc_raw_elements_grouped.json`, сырой дамп
`IFC_исходные_параметры.xlsx/.json`, промежуточные `filtered_elements.xlsx`,
`materials.json`, `building_parts.json`, `Дерево_проекта*.xlsx`,
`ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx`, `api_works_response.json`,
`api_works_cost_response.json`) продолжают создаваться в `run_<NNN>/` и
корне сессии (нужны для работы конвейера и отладки), но в итоговую выдачу
(список файлов запуска для скачивания через `GET .../download/<filename>` и
`download_all`) **не попадают**.

**Пересборка финального JSON по запросу** — эндпоинты
`POST /api/session/<id>/run/<N>/build_final_json` (тело `processingType`),
`GET .../run/<N>/final_json/status` и `GET .../run/<N>/final_json/result`
(`SessionManager.build_final_json_by_number`): повторный запуск
`ifc_json_builder.build_final_json` в фоновом потоке для завершённого запуска.
`final_result_KR.json` API ищет напрямую на диске: директория запуска →
список файлов запуска → корень сессии (`get_final_json_result_by_number`).

**Восстановление сессии** — `POST /api/session/<id>/restore`
(`RestoreResponse`): возвращает состояние для продолжения работы в
интерфейсе — статус/прогресс, файлы текущего запуска (или сессии), выбранные
строки, высоту здания, высоту этажа (`floor_height` текущего запуска),
список запусков и текущий запуск, тип обработки (`processingType`).

Статус запуска → `completed`.

---

### 5. Сводка по модулям (порядок работы)

| # | Модуль | Когда | Что делает |
|---|---|---|---|
| 1 | `session_manager.py` | всегда | Оркестратор: сессии, `sessions.json`, фоновые потоки, прогресс 0–100, runs, `_start_position_links_bg`, пересборка финального JSON по номеру запуска (`build_final_json_by_number`), восстановление сессии |
| 2 | `ifc_raw_dump.py` | этап 0 | Сырой дамп всех свойств/QTO IFC → `IFC_исходные_параметры.xlsx/.json` |
| 3 | `zero_step.py` | этап 0 | Извлечение элементов по `ELEMENT_TYPES_KR`, нормализация, QTO/bbox, `ДЛЯ_СМЕТЧИКА_исправленный.xlsx`, `height.txt` (высота здания), `IFC_глобальные_константы.json`, лист `Высота_здания` |
| 3а | `selection_template_builder.py` | этап 0 | Заполнение шаблона параметров подбора → `Параметры_подбора_элементов.json` (аддитивный шаг, на КР не влияет) |
| 4 | `ifc_reference_builder.py` | этап 0 | JSON-справочники цифрового сборника: `ifc_elements_output.json`, `ifc_raw_elements_grouped.json`, `ifc_raw_elements.xlsx`; на этапе 1 — `split_leaf_groups_by_part` / `split_leaf_groups_by_name` и `build_reference_output` → `selected_elements_grouped.json` |
| 5 | `position_links.py` | этап 0 (только КР) | Ссылки на карточки позиций цифрового сборника по группам предпросмотра (nameKey из `ifc_elements_output.json`) → `position_links.json` (фон. поток, иконки 📎 в превью; серая иконка для отсутствующих) + `Отсутствующие_в_ЦС_группы.json/.xlsx` |
| 6 | `pdf_processor.py` + `ai-blueprint-to-ifc/` | этап 0 (для PDF) | Пайплайн машинного зрения: стены (YOLO OBB), штриховки (DINO+VLM), легенды → те же Excel/JSON |
| 7 | `group_excel.py` | этап 1 | `process_ifc_excel_mssk` → `group_elements_mssk`: Часть здания → МССК → Материал → IFC-тип → Геометрия → Материал/Бетон; деревья и `ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx`; `material_code_overrides` — уточнение неполных МССК-кодов материала из `rowMaterialCodes` |
| 8 | `api_works_lookup.py` | этап 1 | Подбор работ через API ТСН (`fetch_works_from_api`: POST `digital-collection/building-elements/positions`, фильтрация по высоте, отбор позиций, пропуск сборного ж/б) + финальный перечень (`build_final_works_from_api`: объёмы, арматура, опалубка, корректировка `koefs.xlsx`, стоимость `works/resources`) → `ОБЩИЙ_Финальный_перечень_работ.xlsx` |
| 9 | `works_cost.py` | этап 1 | Корректировка объёмов (`koefs.xlsx`), fallback-стоимость (`price_cost.xlsx`), `format_money`, строка «ИТОГО:» |
| 10 | `ifc_json_builder.py` | этап 1 | Финальный `final_result_KR.json` (KRBuilder) |
| — | `works_table_selector.py`, `works_fetcher.py`, `works_final_selector.py`, `pd_parser.py` | **не используется в КР** | Детерминированный подбор таблиц, цифровой сборник larix, LLM-подбор работ, разбор ПОС/ПЗ — функциональность режима АР |
| — | `mssk_lookup.py`, `materials_lookup.py` | этап 0/1 | Карты МССК-кодов элементов/материалов (`elements_mssk_nested.json`, `materials_mssk_nested.json`) — общие для КР и АР; `material_code_options` — дочерние коды для **неполного** кода материала (напр. «СТ 00 15» → сборный/монолитный) |
| — | `serializer.py` | по запросу | 3D-модель GLB (кнопка «3D модель») |
| — | `comparison_manager.py` | режим «Сравнение ВОР» | Оркестратор comparison-сессий (`session_type='comparison'`): обработка нескольких моделей/чертежей (ЦС/ИИ), разбор документов (ИИ), разбор ВОР заказчика и сравнение → `models.json`/`estimates.json`/`result.json`/`result.xlsx` (§9) |
| — | `vor_parser.py` | режим «Сравнение ВОР» | Локальный парсер ВОР заказчика (PDF с текстом / XLSX) по заголовкам колонок; выбор столбца объёма при неоднозначности (§9) |
| — | `comparison_documents.py` | режим «Сравнение ВОР» (ИИ) | `resolve_constants` (IFC → ПОС → ПЗ → по умолчанию + ручные), `material_choices` — уточнение неполных МССК-кодов материалов (§9) |
| — | `works_comparison/` | режим «Сравнение ВОР» | Пакет сравнения позиций (нормализация единиц, группировка, сопоставление по шифру/семантике/конструкциям, LLM-валидация, формирование отчёта) — подключён через `comparison_manager` (§9) |
| — | `comparison_vor.py` | режим «Сравнение ВОР» | Компактный отчёт сравнения (`prepare_report`, `reportVersion=3`) по шаблону `data/comparison_vor_template.xlsx` (§9) |

---

### 6. Справочники `data/` и их роль в режиме КР

| Файл | Кто читает | Роль |
|---|---|---|
| `perechen_kr.xlsx`, `perechen_kr_1.xlsx` | справочно / АР (`selection_parameters.json`) | Перечни работ; прямого участия в КР-конвейере (подбор через API) не принимают |
| `koefs.xlsx` | `works_cost._get_corrected_volume` | Нормы расхода — корректировка «Объёма работ» расценок (пары «Шифр ТСН + ресурс», норма ×/÷100) |
| `price_cost.xlsx` | `works_cost._add_cost_column` | Стоимость расценок (листы `_Получние_параметры_позиции_sel`, `_Связаанные_ресурсы_select_wp_p`) — fallback при недоступности API `works/resources` |
| `elements_mssk_nested.json` | `mssk_lookup.build_mssk_lookup` | Иерархический справочник МССК-элементов; названия групп L2 в `group_elements_mssk` |
| `materials_mssk_nested.json` | `materials_lookup.build_materials_lookup` / `build_material_children_map` | Иерархический справочник МССК-материалов (`СТ …`); группа материала L3 («Железобетон сборный (СТ 00 15 01)», «Прочее»); дочерние коды (`material_code_options`) — варианты уточнения неполного кода |
| `algorithm.md`, `ifc_to_collections.json`, `msck_elements_compact.json`, `tree_work_compact.json`, `works_classification.json`, `params_registry.json`, `selection_parameters*.json` | АР (`works_table_selector`, `pd_parser`, `selection_template_builder`) | В КР-конвейере **не используются** (детерминированный подбор таблиц и константы проекта — режим АР) |
| `comparison_vor_template.xlsx` | `comparison_vor.prepare_report` | Шаблон стандартной иерархии ВОР (часть здания → раздел → подраздел → работа → шифр) — по нему отчёт сравнения (§9) относит работы к разделам |

Все файлы `data/` — **только чтение**; результаты пишутся в `outputs/<session_id>/`.

---

### 7. Полный список создаваемых файлов (КР)

**Корень сессии `outputs/<session_id>/`** (этап 0):

- `IFC_исходные_параметры.xlsx` / `IFC_исходные_параметры.json` — сырой дамп IFC;
- `ДЛЯ_СМЕТЧИКА_исправленный.xlsx` (листы `Данные`, `Сводка_по_типам`,
  `Высота_здания`) — в `original/`;
- `height.txt` (высота здания, legacy-fallback);
- `IFC_глобальные_константы.json` — глобальные константы IFC (высоты, отметки);
- `Параметры_подбора_элементов.json` — заполненный шаблон параметров подбора
  (аддитивный шаг, используется конвейером АР);
- `legend.json` + `legend_item_N.<ext>` — ручная легенда PDF-чертежа
  (если PDF загружен с `autoLegend=false`, §3.6);
- `ifc_elements_output.json` («все элементы.json») — все элементы в формате справочника;
- `ifc_raw_elements_grouped.json` («группы элементов.json») — группы в формате
  запроса API ТСН;
- `ifc_raw_elements.xlsx` — промежуточная таблица всех элементов;
- `position_links.json` — ссылки на карточки позиций цифрового сборника
  (только КР; вариант без позиций — группа отсутствует в ЦС);
- `Отсутствующие_в_ЦС_группы.json` / `Отсутствующие_в_ЦС_группы.xlsx` —
  группы предпросмотра, отсутствующие в цифровом сборнике (только КР).

**Запуск `outputs/<session_id>/run_<NNN>/`** (этап 1):

- `materials.json` — применённые материалы (промежуточный);
- `filtered_elements.xlsx` — выбранные строки (промежуточный, вход для группировки);
- `Дерево_проекта_выбранные_элементы.xlsx`, `Дерево_проекта.xlsx` (всё здание);
- `ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx`;
- `building_parts.json` (промежуточный);
- **`selected_elements_grouped.json`** — группы выбранных элементов в формате
  запроса API ТСН (рабочий артефакт; в итоговую выдачу запуска не входит,
  остаётся в `run_<NNN>/`);
- `api_works_response.json` — сырые ответы API подбора работ (отладка);
- `api_works_cost_response.json` — сырые ответы API стоимости (отладка);
- **`ОБЩИЙ_Финальный_перечень_работ.xlsx`** — итоговая таблица работ: шифры ТСН,
  объёмы (с корректировкой по `koefs.xlsx`), «ЗП»/«ЭМ»/«МР» (показатели
  `works/resources` × объём), «Стоимость» = ЗП+ЭМ+МР; таблица разбита на
  части здания «ПОДЗЕМНАЯ ЧАСТЬ» / «ЦОКОЛЬНАЯ ЧАСТЬ» / «НАДЗЕМНАЯ ЧАСТЬ»
  с итоговыми
  строками «ИТОГО по … части:» в конце каждой части и общим «ИТОГО:» в конце
  таблицы; заголовки групп — «<Имя элемента> (<Материал>)  Кол-во: N»
  (формат режима ИИ, см. §4.5);
- **`final_result_KR.json`** — финальный выходной JSON запуска.

---

### 8. Ключевые отличия от режима АР

| Аспект | КР | АР |
|---|---|---|
| IFC-классы | Только конструктив (`ELEMENT_TYPES_KR`) | Конструктив + архитектура (`ELEMENT_TYPES_AR`) |
| Группировка | **Часть здания → МССК → Материал → IFC-тип → Геометрия → Материал/Бетон** (`group_elements_mssk`) | **Та же иерархия, что и в КР**: `process_ifc_excel_ar` вызывает `group_elements_mssk`; отличается только суффикс имён файлов (`_AR`) |
| Группы итогового перечня | Перед запросом к API группы делятся по части здания (`split_leaf_groups_by_part`) и **по типу элемента** (`split_leaf_groups_by_name`) — заголовки и «Кол-во» как в АР | Группы дерева делятся по типу элемента в `perechen_pipeline` |
| Подбор работ | **Внешний API ТСН** (`api_works_lookup`: `digital-collection/building-elements/positions` + стоимость `works/resources`) | Подбор таблиц — локальный детерминированный алгоритм (`works_table_selector`); работы — `works_fetcher` (larix); финальный выбор — LLM (`works_final_selector`) |
| Константы проекта | Не требуются (только высота здания для фильтрации групп работ) | 10 констант из UI/ПОС/ПЗ/IFC |
| Материал строки | Можно переопределить вручную в превью | Автоопределение, ручной выбор отключён |
| Цифровой сборник | JSON-справочники (`ifc_elements_output.json`, `ifc_raw_elements_grouped.json`), `position_links.json`, иконки 📎 перехода к карточке позиции (серая иконка для отсутствующих) + `Отсутствующие_в_ЦС_группы.json/.xlsx` | Справочники и ссылки не строятся; larix используется только для работ таблиц и детальных параметров позиций |
| Корректировка объёмов | `koefs.xlsx` (нормы расхода) | Не выполняется |
| Стоимость | API `works/resources` (ЗП/ЭМ/МР), fallback `price_cost.xlsx` | Детальные параметры позиций larix (`catalog/work-process/detail`) |
| Итог | **`ОБЩИЙ_Финальный_перечень_работ.xlsx`** — единственный файл итоговой выдачи запуска; `selected_elements_grouped.json`, `final_result_KR.json` и справочные JSON остаются на диске | **`Финальный_перечень_работ.xlsx`** — единственный файл итоговой выдачи запуска (+ `final_result_AR.json` на диске) |

---

## 11. Режим ИИ (AI)

Документ описывает полный путь данных в режиме **AI** (ранее — режим **AR**,
«Архитектурные решения»): какие файлы принимаются на вход, какие модули в каком
порядке работают, какие файлы создаются на каждом шаге и какие справочники из
папки `data/` используются.

> **Переименование режимов.** Режим **АР переименован в «ИИ» («Искусственный
> интеллект», публичный код `AI`)**, режим КР — в «ЦС» («Цифровой сборник»,
> `CS`). Оба режима обрабатывают одни и те же файлы (IFC/PDF), различается
> только пайплайн подбора работ. Публичный код режима в API и веб-интерфейсе —
> **`AI`**; легаси-код `AR` (и русские «АР») по-прежнему принимается на входе
> и нормализуется к `AI` (`src/core/modes.py`), а внутренние коды пайплайна
> (`zero_step`, `group_excel`, `works_*`, `ifc_json_builder` и др.) остались
> прежними (`AR`). Имена артефактов (`final_result_AR.json`,
> `Финальный_перечень_работ.xlsx` и др.) не меняются.

Режим **ИИ (ранее АР)** предназначен для архитектурно-отделочных элементов модели (окна, двери,
витражи, покрытия, кровля, перила, мебель, прокси-элементы, а также базовый конструктив
— стены, перекрытия и т.д.). **Подбор таблиц работ** выполняется
**детерминированным алгоритмом** (без обращения к внешнему API ТСН, как в режиме
ЦС) по правилам из `data/algorithm.md`.

> **Два варианта финального подбора работ (переключатель `WORKS_SELECTOR`).**
> Шаги «этапа 1» до `works_table_selector` включительно (материалы, фильтрация,
> группировка, детерминированный подбор таблиц, `works_fetcher`) одинаковы для
> обоих вариантов. Различается **финальный подбор работ по группам**:
>
> - **`WORKS_SELECTOR=perechen` (текущий/основной)** — подбор по **перечню
>   сметчиков `data/perechen_kr.xlsx`** с локальной LLM и **локальным
>   справочником расценок `data/price_cost.xlsx`** (ветка Bors, §5.8–§5.12);
>   работы и стоимость берутся из локального справочника (`WORKS_SOURCE=local`).
>   Результат — `Финальный_перечень_работ.json/.xlsx` + `analytic.json`.
>   (Шаг `works_fetcher` с обращением к larix в конвейере остаётся, но его
>   результат `Подобранные_работы.json` этим вариантом не используется.)
> - **`WORKS_SELECTOR=llm`** — прежний `works_final_selector.py` (§5.5):
>   работы из цифрового сборника **larix** (`works_fetcher`, §5.4), финальный
>   выбор работ по группам — локальная LLM.
>
> Остальные модули прежней ветки (`works_fetcher.py`, `works_final_selector.py`,
> `ifc_json_builder.py`) сохранены и работают при `WORKS_SELECTOR=llm`.

---

### 1. Входные данные

#### 1.1. Основной файл модели

| Источник | Эндпоинт | Описание |
|---|---|---|
| IFC-файл (`.ifc`) | `POST /api/upload_ifc` (multipart, поле `processingType=AI`; легаси `AR` принимается) | BIM-модель; элементы извлекаются через `ifcopenshell` |
| PDF-чертёж (`.pdf`) | `POST /api/upload_ifc` (multipart, `processingType=AI`) | Обрабатывается пайплайном машинного зрения `ai-blueprint-to-ifc`; результат — те же Excel/JSON, что и для IFC. Легенду можно задать вручную (`autoLegend=false` + `legend`, §3.5) |

**Модели чертежей при локальном запуске.** `./start.sh` после загрузки `.env`
настраивает общий каталог весов `/home/projects/external/models` (тот же каталог
на хосте, который использует основной Docker-сервис). Переопределить его для
локальной копии можно через `LOCAL_BLUEPRINT_MODELS_DIR` в окружении или `.env`.
Скрипт экспортирует `MODELS_DIR`, отдельные пути `YOLO_LAYOUT_MODEL`,
`YOLO_LEGEND_LAYOUT_MODEL`, `DINO_HATCHING_MODEL`, `HATCH_FINDER_MODEL` и
абсолютный `MODEL_NAME` обоих профилей `WALL_DETECTION` /
`UNHATCHED_WALL_DETECTION`. Используются файлы `yolo_walls_obb.pt`,
`yolo_layout.pt`, `yolo_legend_layout.pt`, `dino_hatching.pt`, `hatch_finder.pt`;
копировать веса в `ai-blueprint-to-ifc/models` не требуется. Настройки путей
локального запуска имеют приоритет над одноимёнными контейнерными путями из
`.env`. Дополнительно `src/services/pdf_processor.py` перед импортом процессора
устанавливает эти же пути для локальной копии: это работает и при запуске Flask
напрямую, без `start.sh`, и заменяет устаревшие `/app/...` из окружения.
Docker-ветка (корень сервиса `/app`) не меняет пути моделей. Docker Compose и
настройки Ollama/Transformers не меняются. После изменения настроек или кода
нужно перезапустить локальный Flask: модуль `config` кэшируется после первого
импорта; старую ошибочную сессию следует заменить новой загрузкой чертежа.

#### 1.2. Опциональные файлы ПОС и пояснительной записки (ПЗ)

| Источник | Эндпоинт | Описание |
|---|---|---|
| PDF пояснительной записки ПОС | `POST /api/session/<id>/upload_pos` | LLM извлекает глобальные константы проекта (материалы конструкций, высоты и т.п.) → `ПОС_глобальные_константы.json` |
| PDF пояснительной записки (ПЗ) | `POST /api/session/<id>/upload_pz` | Разбирается **тем же пайплайном и той же LLM**, что и ПОС (`pd_parser.parse_pos_constants`): из документа извлекаются глобальные константы подбора работ → `ПЗ_глобальные_константы.json` |
| PDF ведомости объёмов работ (ВОР) | `POST /api/session/<id>/upload_vor` | `doc_pdf_parser.parse_vor` (pdfplumber + LLM): извлекаются группы элементов, объёмы работ и единицы измерения → `ВОР_ведомость_объемов_работ.json` и `.xlsx` (используется подбором работ — `doc_facts`, §5.8.1, и сравнением с перечнем — `vor_comparison`, §5.13) |
| PDF проектной документации (ПД) | `POST /api/session/<id>/upload_pd` | `doc_pdf_parser.parse_pd` (pdfplumber + LLM): разбирается **только текстовая часть** (графические/альбомные страницы пропускаются), извлекаются характеристики элементов (класс бетона, прочность, пластичность, морозостойкость, водонепроницаемость) → `ПД_характеристики_элементов.json` и `.xlsx` (используется подбором работ — `doc_facts`, §5.8.1) |

Помимо 10 глобальных констант (`pd_parser`, §4), ПОС/ПЗ разбираются **расширенно**:
`GET /api/session/<id>/pd_full` (ленивый фоновый запуск, `pd_full.py`, §5.10) извлекает
≈40 параметров со страницами и цитатами → `<сессия>/pd_full.json`; результат
используется подбором по перечню (§5.8), поправками сборников ТСН (§5.9) и
аналитической справкой (§5.12).

Все четыре документа можно передать и вместе с основным файлом в `POST /api/upload_ifc`
(multipart-поля `posFile`, `pzFile`, `vorFile`, `pdFile` — только режим ИИ).
Пояснительная записка — источник констант с **низшим приоритетом**: используются
константы, не найденные в IFC и ПОС (порядок: UI → IFC → ПОС → ПЗ).

#### 1.3. Параметры запуска (`POST /api/session/<id>/select_rows` или `/new_run`)

Тело JSON:

| Параметр | Описание |
|---|---|
| `rowIndices` / `allRows` | Индексы выбранных строк превью (0-based) или «выбрать все» |
| `rowTypes` | `{индекс: 'Надземная'\|'Подземная'\|'Цоколь'}` — часть здания для строки |
| `rowMaterials` | `{индекс: 'материал'}` — материал строки; в режиме ИИ фронтенд отправляет автоматически определённые значения (МССК-группа материала или исходное значение колонки «Материал»), ручной выбор в интерфейсе отключён |
| `rowMaterialCodes` | `{индекс: 'СТ xx xx xx'}` — уточнение **неполных** МССК-кодов материала, выбранное в превью (напр. «СТ 00 15» — «Железобетон» → «СТ 00 15 01»/«СТ 00 15 02» — сборный/монолитный); учитывается при группировке по материалу (см. §5.2) |
| `buildingHeight` | Высота здания, м (информационно + детект констант) |
| `floorHeight` | Высота типового этажа, м (специфично для режима ИИ) |
| `processingType` | `"AI"` (легаси `"AR"` также принимается и нормализуется к `"AI"`) |
| `globalConstants` | `{имя константы: значение}` — константы подбора работ (см. §5.3) |

Схема констант и автоопределённые значения отдаются эндпоинтом
`GET /api/session/<id>/works_constants` (схема — из `data/works_classification.json`,
высоты — из `IFC_глобальные_константы.json` (fallback — лист `Высота_здания`
Excel сессии), константы ПОС — из `ПОС_глобальные_константы.json`, константы
пояснительной записки — из `ПЗ_глобальные_константы.json`).
`IFC_глобальные_константы.json`, `ПОС_глобальные_константы.json` и
`ПЗ_глобальные_константы.json` имеют **идентичную структуру** (цель одна —
определить константы, влияющие на расценки); приоритет источников:
**IFC → ПОС → ПЗ** (если константа найдена в нескольких, берётся значение
источника с более высоким приоритетом). Статусы разбора и найденные константы
документов отдаются полями `posStatus`/`posDetected` и `pzStatus`/`pzDetected`
того же эндпоинта.

В `detected` эндпоинт отдаёт **все** константы, определённые по IFC:
`building_height_m`, `floor_height_m`, `total_height_m`, `top_elevation_m`,
`min_ground_elevation_m` (последняя может быть равна 0 — отметка земли), а также
строчную константу `building_storeys_type` (этажность здания). Для старых сессий
(без константы в `IFC_глобальные_константы.json`) этажность определяется fallback-ом
по колонке «Этаж» Excel полной модели (`zero_step.detect_storeys_type_from_session`).

Веб-интерфейс (режим АР, шаг «Выбор строк и параметров обработки») показывает
блок **«Параметры влияющие на расценки»** — **только глобальные константы** из
схемы `works_classification.json` (выпадающие списки с автоподстановкой:
IFC → ПОС → ПЗ → default):

- 10 констант: `formwork_type`, `building_height_m`, `concrete_curing_season`,
  `concrete_placement_scheme`, `floor_height` (высота этажа, м),
  `building_storeys_type` (этажность здания: одноэтажное/многоэтажное),
  `crane_type` (тип крана), `crane_capacity` (грузоподъёмность крана/транспорта, т),
  `bucket_capacity` (вместимость ковша, м3), `equipment_power` (мощность
  оборудования, кВт); параметры с `"stage": "next_stage"` не влияют на выбор
  таблиц (следующий этап — выбор строк/норм внутри таблиц ГЭСН);
- числовые константы (`floor_height`, `crane_capacity`, `bucket_capacity`,
  `equipment_power`) автоподбираются в вариант из списка `values` схемы:
  найденное по IFC/ПОС/ПЗ число переводится в диапазон («до 4 м», «до 10 т»,
  «до 1,8 м3», «59 кВт» и т.п.);
- **IFC-специфичные константы** (`floor_height_m`, `total_height_m`,
  `min_ground_elevation_m`, `top_elevation_m`) из блока **удалены** (ранее
  показывались полями «только для чтения»); высота этажа из IFC учитывается
  константой `floor_height`, высота здания — константой `building_height_m`;
- блок загрузки ПОС и пояснительной записки (ПЗ) — на шаге 0 (блоки
  «Файл ПОС (PDF)» и «Файл пояснительной записки (PDF)», отображаются только
  в режиме АР); константы из обоих документов подставляются в поля
  автоматически (ПЗ — если константа не найдена в ПОС);
- **дозагрузка констант ПОС/ПЗ**: панель констант рисуется сразу после обработки
  IFC, а разбор документов (`pos_processing` / `pz_processing`) может
  завершиться позже. Пока идёт любой из разборов, веб-интерфейс опрашивает
  `works_constants` каждые 3 с (`trackPosConstants`); по завершении разборов
  (`pos_completed` / `pos_error`, `pz_completed` / `pz_error`) панель
  перерисовывается и константы из ПОС/ПЗ (опалубка, схема бетонирования,
  краны, ковш, мощность и т.д.) автоматически подставляются в поля.
  Значения, **выбранные пользователем вручную**, при перерисовке сохраняются
  (приоритет ручного выбора над автоподстановкой). Ручным выбором считается
  только значение, **отличное от прошлой автоподстановки** (карта
  `_lastAutoConstants` в `index.html`): автоподставленное до завершения
  разбора ПОС/ПЗ значение по умолчанию (например, «деревянная щитовая» для
  `formwork_type`) не блокирует подстановку значения из файла ПОС/ПЗ при
  перерисовке панели.

---

### 2. Общая схема пайплайна

```
IFC / PDF ──► [Этап 0. Загрузка и извлечение элементов]  (фоновый поток, статус ifc_processing)
                ├─ ifc_raw_dump.py        → IFC_исходные_параметры.xlsx/.json (сырой дамп)
                ├─ zero_step.py          → ДЛЯ_СМЕТЧИКА_исправленный.xlsx, height.txt,
                │                          IFC_глобальные_константы.json
                └─ selection_template_builder.py
                                          → Параметры_подбора_элементов.json
                                            (заполненный шаблон параметров подбора по каждому элементу)
               статус → ifc_processed → selecting_rows (пользователь смотрит превью)

PDF ПОС ────► [Опционально] pd_parser.py → ПОС_глобальные_константы.json
PDF ПЗ ─────► [Опционально] pd_parser.py → ПЗ_глобальные_константы.json
               (тот же пайплайн, что и для ПОС; в т.ч. константы следующего
                этапа: floor_height, crane_capacity, bucket_capacity,
                equipment_power; вне схемы: soil_group, movement_distance)

PDF ВОР ────► [Опционально] doc_pdf_parser.parse_vor (pdfplumber + LLM)
                                          → ВОР_ведомость_объемов_работ.json/.xlsx
PDF ПД ─────► [Опционально] doc_pdf_parser.parse_pd (pdfplumber + LLM, только текст)
                                          → ПД_характеристики_элементов.json/.xlsx
                (оба документа используются подбором по перечню: класс бетона
                 B/W/F, класс арматуры, объёмы работ «по проекту», сверка объёмов —
                 doc_facts.py, §5.8.1; подтверждение условных работ «не моделируется» —
                 §5.8.2)

select_rows / new_run ──► [Этап 1. Запуск run_<NNN>]  (фоновый поток)
                ├─ применение материалов          → materials.json
                ├─ фильтрация строк               → filtered_elements.xlsx
                ├─ group_excel.process_ifc_excel_ar
                │      (та же иерархия, что и в ЦС: Часть здания → МССК → Материал
                │       → IFC-тип → Геометрия → Материал/Бетон)
                │                                 → Дерево_проекта_выбранные_элементы.xlsx
                │                                 → filtered_elements_grouped_AR.json
                │                                 → Дерево_проекта.xlsx (всё здание)
                ├─ листовые группы                → ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx
                │                                 → building_parts.json
                ├─ works_table_selector (8 шагов алгоритма data/algorithm.md)
                │                                 → Подобранные_таблицы_работ.json
                ├─ works_fetcher (цифровой сборник larix: период ТСН + работы таблиц)
                │                                 → period.json (корень сессии)
                │                                 → Подобранные_работы.json (run_<NNN>/)
                ├─ финальный подбор работ по группам (ветка WORKS_SELECTOR):
                │   ├─ perechen (основной): perechen_pipeline.build_final_from_perechen
                │   │      (перечень сметчиков data/perechen_kr.xlsx + локальный
                │   │       справочник расценок data/price_cost.xlsx, §5.8–§5.12)
                │   │                                 → Финальный_перечень_работ.json
                │   │                                 → Финальный_перечень_работ.xlsx
                │   │                                 → analytic.json
                │   └─ llm: works_final_selector (LLM: по каждой таблице кандидатов —
                │          ровно одна работа; предфильтрация/ранжирование работ,
                │          fallback — ближайшая работа, §5.5)
                │                                 → Финальный_перечень_работ.json
                │                                 → Финальный_перечень_работ.xlsx
                └─ ifc_json_builder.build_final_json → final_result_AR.json
              статус → completed (файлы доступны на скачивание)
```

---

### 3. Этап 0 — загрузка файла и извлечение элементов

Координатор — `src/services/session_manager.py` (`SessionManager`).
После `POST /api/upload_ifc` создаётся сессия (`outputs/sessions.json` + папка
`outputs/<session_id>/`), файл сохраняется в `uploads/<session_id>/`, и в
`threading.Thread(daemon=True)` запускается `_process_ifc_bg` (для IFC) или
`_process_pdf_bg` (для PDF). Порядок работы для IFC:

> **Производительность этапа 0 (актуализация).** Ядро этапа 0 для IFC
> (`_process_ifc_bg`) ускорено: модель `ifcopenshell` открывается **один раз**
> и передаётся в `dump_ifc_elements_raw` и `zero_step`; удалён отладочный
> `analyze_qto_properties`; габариты bbox для плит/маршей считаются ручным
> обходом вершин вместо ядра `create_shape` (см. §3.2). На сессии с 11 339
> элементами (70 МБ IFC) время этапа 0 сократилось с ~7,4 мин до ~45 с
> (≈10×) при идентичных результатах.

#### 3.1. `ifc_raw_dump.py` — сырой дамп параметров

Функция `dump_ifc_elements_raw(ifc_path, session_dir, model=None,
exclude_classes=("IfcOpeningElement", "IfcBuildingElementProxy"))` выгружает
**все** параметры, QTO и материалы каждого элемента IFC без нормализации
(необязательный аргумент `model` — уже открытая модель `ifcopenshell`: если
задана, повторное открытие файла не выполняется; `exclude_classes` —
исключаемые IFC-классы, по умолчанию вырезы и прокси-элементы, режим
«Проверка ЦИМ модели» передаёт пустой кортеж, чтобы включить проёмы):

- `outputs/<session_id>/IFC_исходные_параметры.xlsx` — таблица всех свойств;
- `outputs/<session_id>/IFC_исходные_параметры.json` — JSON-копия (список записей
  с ключом `GlobalId`).

Дамп критичен для АР: из него позже берутся `PredefinedType`, `ConstructionMethod`
(In-situ/Precast), `Pset_WallCommon::IsExternal`, коды МССК, материалы слоёв.

**Свойства типа элемента (`IfcTypeObject`) — fallback.** В части моделей
параметры (`ExpCheck_*::MGE_ElementCode`, `MGE_MaterialCode` и др.) размещены
не на экземпляре элемента (`IfcRelDefinesByProperties`), а на его **типе**
(`IfcWallType`, `IfcSlabType`, `IfcBeamType`, …) — через
`IfcRelDefinesByType` → `RelatingType` → `HasPropertySets`. Дамп читает такие
свойства вторым проходом (ключи `Свойство::{Pset}::{Prop}`), приоритет — у
свойств экземпляра (`setdefault`). Без этого в моделях с кодами на типе
не определялись код МССК элемента и код материала → элементы отбрасывались
фильтром в `zero_step`, а группировка по МССК/материалу не выполнялась.

**Стены-пилоны считаются колоннами для опалубки.** `_surface_formwork_kind`
трактует `IfcWall`/`IfcWallStandardCase`, в имени или типе которых есть «пилон»
или «колонн», как `column` (наравне с `IfcColumn`): площадь опалубки
считается по боковым граням без площади низа. Соответственно
`enrich_formwork_quantities` не удаляет у таких стен QTO-величины
`QTO_geom::Площадь_опалубки_м2` / `Площадь_низа_м2` / `Площадь_торцов_м2`
(у обычных стен они по-прежнему отбрасываются).

#### 3.2. `zero_step.py` — извлечение и нормализация элементов

`zero_step(ifc_path, output_folder=session_dir, write_full_data=False, processing_type="AR", model=None)`:

- Необязательный аргумент **`model`** — уже открытая модель `ifcopenshell`.
  В `_process_ifc_bg` модель открывается **один раз** на весь этап 0 и
  передаётся и в `dump_ifc_elements_raw`, и в `zero_step` (раньше файл
  открывался трижды: дамп + отладочный анализ QTO + `zero_step`) — на больших
  IFC это экономит несколько секунд.
- **Отладочный `analyze_qto_properties` удалён** (функция только печатала
  QTO-наборы и повторно открывала модель; на результаты не влияла).

- Извлекаются элементы по списку **`ELEMENT_TYPES_AR`** = конструктивные классы
  (`ELEMENT_TYPES_KR`: `IfcWall`, `IfcSlab`, `IfcColumn`, `IfcBeam`, `IfcStair`,
  `IfcFooting`, `IfcPile`, `IfcCovering`, …) + архитектурные `_ARCH_TYPES`:
  `IfcBuildingElementProxy`, `IfcWindow`, `IfcDoor`,
  `IfcCurtainWall`, `IfcRoof`, `IfcRailing`, `IfcFurnishingElement`, `IfcPlate`,
  `IfcShadingDevice`, `IfcWasteTerminal`, `IfcAirTerminal`, `IfcMember`.
  Дедупликация по IFC-классу. **Парсинг АР идентичен парсингу КР**: `IfcCovering`
  помечается как **«Изоляция»** (как в `ELEMENT_TYPES_KR`), а не «Покрытие» —
  колонка «Тип (RU)» влияет на заголовки групп финального перечня работ.
- Нормализуются характеристики (тип, материал, этаж/часть здания, код МССК из
  Pset `ExpCheck_*::MGE_ElementCode`), количества считаются из QTO либо по bbox
  (`ifc_raw_dump._compute_bbox_quantities`). **Материал элемента** — из Pset
  `ExpCheck_<Класс>::MGE_Material`; для ИИ дополнительно выполняется
  поэлементное уточнение сборного/монолитного железобетона (см. ниже).
- **Уточнение железобетона только в ИИ.** `materials_lookup.extract_material_value_ai`
  проверяет общий код `СТ 00 15` по всем непустым параметрам материала и
  технологии данного элемента, независимо от имени Pset. Поддерживаются
  русские/английские имена (`Материал`, `Material`, `ConstructionMethod`,
  `ConstructionType`, технология, способ/метод изготовления или возведения,
  тип конструкции), сырые ключи `Свойство::…` и нормализованные `Свойство_…`.
  Признаки «монолитный» / `In-situ` / `Cast in place` дают `СТ 00 15 02`,
  «сборный» / `Precast` / `Prefabricated` — `СТ 00 15 01`. Полный код из другого
  параметра материала тоже используется для уточнения. При отсутствии признаков
  в свойствах проверяются имя/типовое имя элемента; без свидетельства, что материал
  является железобетоном, технология сама по себе не назначает материал.
  Полный исходный код и многослойные материалы не изменяются; конфликтующие
  признаки или отсутствие данных сохраняют исходное значение и ручное уточнение.
  Для вновь загружаемых IFC уточняется колонка «Материал» этапа 0. Карта
  `SessionManager._load_material_layer_map` применяет ту же логику к сырому дампу
  **существующих сессий**, поэтому превью получает полный код без повторной
  загрузки модели. В новом запуске уточнение записывается в `filtered_elements.xlsx`
  (материал и код в строке слоёв) и используется группировкой и подбором работ.
  Ручной `rowMaterialCodes` применяется после автоопределения и имеет приоритет.
  Сырой дамп и справочники `data/` не изменяются; ЦС и ARCH используют прежний
  `extract_material_value` без нового уточнения.
- **Свойства типа элемента как fallback.** `get_all_properties` читает Pset
  не только с экземпляра (`IsDefinedBy`), но и с **типа** элемента
  (`IsTypedBy` → `RelatingType` → `HasPropertySets`), не перезаписывая
  значения экземпляра. Это необходимо для моделей, где код МССК
  (`ExpCheck_*::MGE_ElementCode`) и код материала (`MGE_MaterialCode`) заданы
  на `IfcWallType`/`IfcSlabType`/… — без обхода типа `element_code_cols`
  оказывается пустым, и **все** элементы отбрасываются фильтром по коду
  (`valid_code_mask`: код должен начинаться с «ЭЛ»), из-за чего
  `ДЛЯ_СМЕТЧИКА_исправленный.xlsx`/`сокращенный.xlsx` и группировка остаются
  пустыми.
- Этажи классифицируются → часть здания (Подземная / Цоколь / Надземная)
  и высотные отметки. Колонка **`Тип_этажа`** заполняется функцией
  `classify_storey_type` (слова «цоколь»/«подвал»/«крыша» + отметка этажа) —
  **одинаково для режимов АР и КР** (парсинг режимов идентичен). Ранее в АР
  использовалась `_classify_storey_type_ar` (строго по числовому индикатору в
  значении «Этаж»); функция сохранена для обратной совместимости, но в
  пайплайне не используется.
- **Высота здания — по максимальному уровню элементов**: «Высота_надземной_части_м»
  = максимум `Уровень_этажа_мм` среди **всех** элементов модели (ранее — по
  отметкам этажей типов Цокольный/Надземный/Технический/Мансардный, из-за чего
  этаж «Крыша» (тип Кровля) исключался и высота занижалась); если уровней нет —
  высота по отметкам этажей.
- **Bbox-фолбэк периметра/толщины плит** (`_compute_formwork_bbox_quantities`):
  если у `IfcSlab`/`IfcStairFlight` нет QTO `Perimeter`/`Depth`, они вычисляются
  из геометрии (bbox) в колонки `Длина_Perimeter_мм`/`Длина_Depth_мм` — по ним
  считается площадь опалубки плит. Применяется в **обоих режимах** (ранее —
  только КР; без этого в АР объёмы опалубочных работ не заполнялись).
  **Производительность:** для «простых» представлений (экструзия, тесселяция,
  граненая B-Rep без отверстий, `IfcMappedItem` над ними) габариты считаются
  **ручным обходом вершин** (`_fast_bbox_mm` через `_collect_geom_from_item` +
  `_local_placement_to_matrix`, без ядра `ifcopenshell.geom.create_shape`) — те
  же вершины, что и у ядра, но без триангуляции (на порядки быстрее). Для
  сложной геометрии (boolean/CSG/swept/advanced brep и т.п.) используется ядро
  (`create_shape`) — поведение не меняется. Проверено на сессии с 11 339
  элементами: габариты совпадают до 0,0 мм, выходные
  `ДЛЯ_СМЕТЧИКА_*.xlsx` идентичны, `zero_step` ускоряется с ~400 до ~21 с.
- **Распознавание количеств с альтернативными именами параметров** (актуализация
  после проверки сессии, где объёмы/площади не считались):
  - в списки источников «ПЛОЩАДЬ» (`geometry_mapping`) после Gross-источников
    автоматически добавляются Net-источники (`NetArea`, `NetSideArea`,
    `NetSurfaceArea` и т.п.) — у части элементов (гидроизоляции, утеплитель,
    приямки) площадь задана **только** в Net-количествах; Gross сохраняет
    приоритет;
  - в `geometry_mapping` добавлен тип **«Изоляция»** (IfcCovering в режиме КР,
    те же источники, что и «Покрытие») — ранее агрегированные
    «Площадь, м2»/«Объём, м3» для изоляции не заполнялись вовсе;
  - в `ДЛЯ_СМЕТЧИКА_исправленный.xlsx` теперь включаются и **Net-колонки
    площадей** (ранее только Gross) — иначе такие элементы оставались без
    площади в группировке;
  - `parse_name` извлекает толщину из имён вида «Стена_180мм_ЖБ_B35_W4_F75»
    (ранее `\b` после «мм» не срабатывал перед подчёркиванием — толщина
    стен без Width-QTO не заполнялась, и площадь стен не выводилась из
    объёма/толщины).

Создаваемые файлы (в корне сессии, затем переносятся в `original/`):

| Файл | Содержимое |
|---|---|
| `ДЛЯ_СМЕТЧИКА_исправленный.xlsx` | Лист `Данные` (по строке на элемент — это превью для выбора), лист `Сводка_по_типам`, лист `Высота_здания` (высота надземной части, высота основного этажа, отметки) |
| `height.txt` | В АР — **высота основного этажа** (в КР — высота здания). Legacy-файл: читается только как fallback, если нет `IFC_глобальные_константы.json` |
| `IFC_глобальные_константы.json` | Глобальные константы, определённые по модели IFC: `building_height_m` (высота надземной части), `floor_height` (высота основного этажа), `total_height_m`, `min_ground_elevation_m`, `top_elevation_m`, `building_storeys_type` (этажность здания: «одноэтажное»/«многоэтажное» — по наибольшему числовому индикатору в колонке «Этаж» всех элементов модели, `zero_step.detect_storeys_type`; индикатор ≥ 2, т.е. есть 2-й этаж → «многоэтажное»). **Структура идентична `ПОС_глобальные_константы.json`**: `document`, `constants` (полный перечень констант — схемные 10 + доп. ПОС + IFC-специфичные; каждая `{name, title, value, found, raw_values, quote, page, source}`), `warnings`, `file_name`, `generated_at`. Константы, не определяемые по IFC (опалубка, схема бетонирования и т.д.), записываются с `value: null, found: false` — их значения берутся из ПОС или по умолчанию. Приоритет источников: **IFC → ПОС** |
| `IFC_ВСЕ_ДАННЫЕ*.xlsx` | Полная таблица всех элементов (используется для кеша «Дерево_проекта») |

#### 3.3. `selection_template_builder.py` — заполнение шаблона параметров подбора

После `zero_step` (для IFC) / после объединения листов (для PDF) строится
**универсальный шаблон параметров подбора работ** по каждому элементу модели:

- Шаблон — перечень параметров из **`data/selection_parameters.json`** (3 глава
  перечня ВОР из `perechen_kr_1.xlsx`: геометрия — высота/длина/ширина/толщина/
  диаметр/площадь/объём/масса и т.д., положение в здании, технология, оборудование).
- Соответствие «параметр шаблона → ключи сырого дампа IFC» задаётся расширяемым
  справочником **`data/selection_parameters_mapping.json`**: `raw_keys` (явные
  ключи `Свойство::…`/`QTO::…`/`QTO_bbox::…`, по порядку), `keywords`
  (fallback-поиск по подстроке в ключе, `exclude_keywords` отсекают ложные
  совпадения), `computed` (вычисление), `constant_names` (константы проекта).
  Справочник дополняется вручную при появлении новых имён свойств в моделях.
- Приоритет заполнения: `raw_keys` → `keywords` → `computed` → `constant`
  (константы: UI/globalConstants → IFC `IFC_глобальные_константы.json`
  (fallback для `floor_height` в старых сессиях — `height.txt`) → ПОС → ПЗ).
- Значения из ключей `QTO_bbox::*_мм` переводятся в метры; значения QTO берутся
  как есть (единицы проекта IFC).
- Вычисляемые параметры: `building_part` (по этажу: Подземная/Цокольная/Надземная),
  `wall_location` (`Pset_WallCommon::IsExternal` → наружная/внутренняя),
  `layers_count` (число слоёв материала), `min_section_side` (мин. габарит, мм).

**Выход**: `outputs/<session_id>/Параметры_подбора_элементов.json` — метаданные,
константы проекта и массив `elements` (по одному заполненному шаблону на элемент:
`global_id`, `name`, `ifc_class`, `predefined_type`, `storey`, `parameters` —
для каждого параметра `{value, unit, origin: element|computed|constant|not_found,
source_key}`). Файл регистрируется в списке файлов сессии (доступен для скачивания).
Шаг аддитивный: на подбор работ и остальные результаты не влияет.

Раздел `constants` содержит **только константы схемы** подбора работ — все
константы `works_classification.json` (`global_constants`; те же 10 констант,
что отображаются в веб-интерфейсе в блоке «Параметры влияющие на расценки») в
порядке схемы, включая не найденные нигде (значение `null`). Значения
записываются в виде **выбранных вариантов** из списков `values` схемы — как
автоподстановка в веб-интерфейсе: числовые значения переводятся в диапазоны
(`building_height_m` 8.4 → «до 30», `floor_height` 4.2 → «до 6 м»,
`crane_capacity` 25 → «свыше 20 т», `bucket_capacity` 0.5 → «до 0,5 м3»,
`equipment_power` 474 → «свыше 132 кВт»; аналоги функций
`heightToRange`/`floorHeightToValue`/… из `src/templates/index.html`),
строковые значения из ПОС/ПЗ/UI передаются как есть. Приоритет значений:
UI/globalConstants → IFC → ПОС → ПЗ. Параметры элементов заполняются исходными
значениями констант (числа с единицами измерения). Файл строится на этапе 0
(после `zero_step` / обработки PDF) и **точечно обновляется после завершения
разбора ПОС/ПЗ** (`_process_doc_constants_bg` →
`_update_selection_parameters_constants`): пересчитываются раздел `constants`
и параметры элементов с `origin == constant`, **без перечитывания сырого дампа
и полной пересборки** файла (экономия чтения 63 МБ + записи 70 МБ на каждое
обновление). Параметры из IFC (`element`/`computed`) не зависят от ПОС/ПЗ и
остаются без изменений. Если файла ещё нет — выполняется полная сборка
(`build_selection_parameters`).

#### 3.4. Справочники цифрового сборника — в АР не строятся

Модули `ifc_reference_builder.py` (JSON-справочники `ifc_elements_output.json`,
`ifc_raw_elements_grouped.json`) и `position_links.py` (ссылки на позиции
цифрового сборника, `position_links.json`) — это функциональность **режима КР**
(подбор работ через API ТСН и переход к карточке позиции в цифровом сборнике).

В режиме АР **подбор таблиц работ** выполняется локальным детерминированным
алгоритмом (`works_table_selector`), поэтому на этапе 0 справочники цифрового
сборника не строятся (на этапе 1 сам **цифровой сборник larix** используется —
`works_fetcher.py` запрашивает актуальный период ТСН и работы подобранных
таблиц, см. §5.4):

- в `_process_ifc_bg` / `_process_pdf_bg` справочники и `position_links.json`
  **не строятся** (только для `processing_type == "KR"`);
- в превью сессии (`GET /api/session/<id>/preview`) для АР не отдаются
  `hasIfcElementsJson` / `hasIfcGroupedJson` — кнопки «Элементы (JSON)» и
  «Группы (JSON)» в веб-интерфейсе скрыты;
- фронтенд не запрашивает `GET /api/session/<id>/position_links` для АР —
  иконки перехода к карточке позиции в цифровом сборнике не отображаются.

Отдельный режим построения справочников (`POST /api/reference`, `reference_only`)
удалён вместе с вкладкой «JSON-справочники». Вместо него в интерфейсе появилась
вкладка «Проверка ЦИМ модели» — самостоятельный функционал вне режимов ЦС/ИИ
(IFC → `IFC_исходные_параметры.json/.xlsx` → заполнение требуемых параметров по
`data/parametrs_name.json` через LLM), см. раздел «Проверка ЦИМ модели».

По завершении статус сессии → `ifc_processed` / `selecting_rows`, пользователь
работает с превью `ДЛЯ_СМЕТЧИКА_исправленный.xlsx`.

В превью АР колонки **«Часть здания»** и **«Материал»** отображаются как обычные
значения (как «Тип элемента») и **не редактируются пользователем**:

- часть здания определяется автоматически по сохранённому значению / параметру
  «Этаж» (подземная / цокольная / надземная часть);
- материал — по МССК-группе материала (`materialsGroupMap` в ответе `preview`)
  либо по исходному значению колонки «Материал»;
- селектор массового назначения материала («Материал для выбранных...») в АР скрыт.

**Неполный МССК-код материала.** Если код материала неполный (в справочнике
`data/materials_mssk_nested.json` у него есть дочерние узлы, напр. `СТ 00 15` —
«Железобетон»), материал определён неоднозначно (сборный/монолитный). Такая
строка группы материала в превью **подсвечивается**, и в неё добавляется
**селект** «сборный/монолитный» (варианты — дочерние коды справочника,
`options`/`incomplete` в `materialsGroupMap`, вычисляются
`materials_lookup.material_code_options`). Выбор пользователя отправляется
полем `rowMaterialCodes` (см. §1.3) и учитывается при группировке (§5.2).
То же поведение — в превью ЦС (общий код `groupRowsCommon` в
`src/templates/index.html`).

**Каскадная подсветка.** Подсветка распространяется на все строки-предки
неполной группы материала: вместе с группой материала подсвечиваются строки
кода МССК и части здания («Подземная часть» и т.п.), в которые она входит.
При выборе материала подсветка группы снимается, а подсветка предка гаснет
**только когда уточнены все** неполные группы материала, входящие в него
(функции `refreshIncompleteHighlight`/`setParentHighlight` в
`groupRowsCommon`; цепочка предков — по карте `groupParent`). Возврат селекта
в значение «— выберите —» возвращает подсветку.

В режиме КР поведение прежнее: материал строки можно переопределить вручную.

#### 3.5. Ручная легенда чертежа (PDF) — `pdf_processor.py` + `ai-blueprint-to-ifc/`

При загрузке PDF-чертежа (`POST /api/upload_ifc`) можно отключить
авто-определение легенды и передать собственный список обозначений
(multipart-поля `autoLegend` и `legend`):

- `autoLegend=true` (по умолчанию) — легенда определяется пайплайном
  машинного зрения как раньше;
- `autoLegend=false` — используется ручная легенда `legend` — JSON-строка вида
  `{"items": [{"label": "Кирпич", "image": "data:image/png;base64,..."}, ...]}`;
  каждый элемент — подпись (`label`) и изображение обозначения (`image`,
  data URL с base64). `routes.py` валидирует структуру (непустые `label`/`image`,
  `image` — data URL) и отклоняет пустой список.

Проброс ручной легенды по конвейеру:

- `SessionManager.process_pdf(..., manual_legend=...)` → `_process_pdf_bg` →
  `_process_pdf_with_progress` → `pdf_processor.process_pdf(..., manual_legend=...)`;
  в корень сессии сохраняется `legend.json`, в состоянии сессии фиксируются
  `auto_legend` / `manual_legend`;
- `pdf_processor._save_manual_legend_files` сохраняет `legend.json` и файлы
  элементов `legend_item_N.<ext>`, а `_build_materials_md_from_legend` строит
  `materials_colors.md` по ручной легенде (приоритет над авто-определением);
- `ai-blueprint-to-ifc/processor.py`: при заданной ручной легенде
  `Processor.process()` идёт веткой `_process_with_hatchfinder` →
  `WallDetectionPipeline.run(manual_legend=...)`, иначе — прежней
  `_process_with_yolo`;
- `WallDetectionPipeline` при ручной легенде вызывает
  `HatchingProcessor.set_legend(items)` (декодирование картинок —
  `ai-blueprint-to-ifc/utils.base64_to_image`) и пропускает авто-разбор легенды;
  `HatchingProcessor.set_legend` формирует `self.legends` из переданных
  обозначений.

---

### 4. Опционально — разбор ПОС и пояснительной записки ПЗ (`pd_parser.py`)

`POST /api/session/<id>/upload_pos` → фоновый поток `_process_pos_bg` →
`pd_parser.parse_pos_constants(pos_path, use_llm=True, ...)`:

`POST /api/session/<id>/upload_pz` → фоновый поток `_process_pz_bg` →
`pd_parser.parse_pos_constants(pz_path, use_llm=True, document_label="ПЗ", ...)`
— **тот же пайплайн и та же LLM**, что и для ПОС. Оба метода — обёртки над
общими `SessionManager._upload_doc_constants` / `_process_doc_constants_bg`
(конфигурация источников — `_DOC_CONSTANTS_CONFIG`: имена файлов констант,
префиксы полей состояния `pos_*`/`pz_*`). Оба файла можно передать и вместе с
основным (`upload_ifc`, поля `posFile`/`pzFile`, только АР).

- PDF конвертируется в текст (PyMuPDF), страницы нарезаются на выдержки;
- по реестру параметров **`data/params_registry.json`** (тип `pos_global_constants`)
  формируются LLM-задачи «найти материал/параметр группы конструкций»;
  запросы уходят в LLM (Ollama, `LLM_BASE_URL`/`LLM_MODEL`), промпты встроены в
  `pd_parser.py`; схема констант сверяется с **`data/works_classification.json`**;
- результат: `outputs/<session_id>/ПОС_глобальные_константы.json` или
  `outputs/<session_id>/ПЗ_глобальные_константы.json` — словарь
  `constants: {имя: {value, quote, page, ...}}`;
- статусы разбора в сессии: `pos_status`/`pz_status`
  (`pos_processing`/`pos_completed`/`pos_error`, `pz_processing`/`pz_completed`/`pz_error`).

Схема `works_classification.json` (`global_constants`) содержит **10 констант**:
опалубка, высота здания, сезон ухода за бетоном, схема бетонирования,
`floor_height` (высота этажа, м), `building_storeys_type` (этажность здания:
одноэтажное/многоэтажное — из ПОС не извлекается, определяется по модели IFC),
`crane_type` (тип крана), `crane_capacity` (грузоподъёмность крана/транспорта, т),
`bucket_capacity` (вместимость ковша, м3), `equipment_power` (мощность
оборудования, кВт). Константы со
`"stage": "next_stage"` (`crane_type`, `crane_capacity`, `bucket_capacity`,
`equipment_power`) не влияют на выбор таблиц — это параметры следующего
этапа подбора (выбор строк/норм внутри таблиц ГЭСН). Помимо констант схемы
`pd_parser` извлекает **дополнительные константы** (вне схемы UI):
`soil_group` (группа грунтов 1–11), `movement_distance` (расстояние
перемещения/перевозки). Regex-поля для них
добавлены в `params_registry.json` (тип `pos_global_constants`), запросы и
нормализация значений — `POS_CONSTANT_QUERIES`/`EXTRA_POS_CONSTANTS`/
`CONSTANT_MAPPERS` в `pd_parser.py` (числовые значения `floor_height`,
`crane_capacity`, `bucket_capacity`, `equipment_power` из ПОС в веб-интерфейсе
переводятся в варианты списков `values` схемы).

Затем эти значения подставляются как `global_constants` при подборе работ
(пользователь видит их в `works_constants` и может переопределить), а также
используются `selection_template_builder` при заполнении шаблона параметров
подбора (см. §3.3). По завершении разбора ПОС (`pos_completed`) или ПЗ
(`pz_completed`) файл `Параметры_подбора_элементов.json` **точечно
обновляется** — константы, найденные из документов, попадают в его раздел
`constants` (и в параметры-константы элементов; приоритет: UI → IFC → ПОС → ПЗ),
без полной пересборки файла.

**Оптимизация разбора ПОС/ПЗ (актуализация).** Все обращения к Ollama идут
через `pd_parser.LLMClient` и ограничены глобальным семафором
`LLM_MAX_CONCURRENCY` (по умолчанию `4`) — он согласован с параллелизмом
Ollama (`OLLAMA_NUM_PARALLEL`) и ограничивает размер очереди (см. §4.1 и §6).
Кроме того, **LLM-вызов для константы пропускается**, если regex-шаблон
(`harvest_field` по `data/params_registry.json`) уже дал распознанное значение,
а маппер константы выбирает **первое подходящее** значение
(`FIRST_MATCH_MAPPERS`: высота здания, высота этажа, группа грунтов, расстояние
перемещения, грузоподъёмность/вместимость/мощность) — добавление результатов
модели после шаблона такой итог не меняет (проверено: значения констант
идентичны). Для «объединяющих» мапперов (тип опалубки, сезон ухода за бетоном,
схема бетонирования, тип крана) LLM вызывается всегда — модель может уточнить
результат. Результат разбора **кэшируется** по SHA-256 содержимого файла
(`DOC_CACHE_DIR`, §6) — повторная загрузка того же ПОС/ПЗ не тратит время LLM.

#### 4.1. Разбор ВОР и ПД (`doc_pdf_parser.py`)

Помимо ПОС/ПЗ (глобальные константы), в режиме ИИ принимаются ещё два
PDF-документа — **ВОР** (ведомость объёмов работ) и **ПД** (проектная
документация). Оба разбираются модулем
[`src/services/doc_pdf_parser.py`](./src/services/doc_pdf_parser.py), который
использует **тот же подход, что и `pd_parser`**: извлечение текста/таблиц через
`pdfplumber` (тот же инструмент, что в `src/tools/tsn`) + структурирование
локальной LLM (Ollama) через `pd_parser.LLMClient`. Разбор идёт в фоновом потоке
(`SessionManager._upload_parsed_doc` → `_process_parsed_doc_bg`), статусы —
`vor_status`/`pd_status` (`vor_processing`/`vor_completed`/`vor_error`,
`pd_processing`/`pd_completed`/`pd_error`), число извлечённых записей —
`vor_count`/`pd_count` (отдаются эндпоинтом `works_constants`).

**ВОР** (`POST /api/session/<id>/upload_vor`, поле `vorFile` в `upload_ifc`):

- из каждой страницы PDF извлекаются текст и таблицы (`pdfplumber`), таблицы
  разворачиваются построчно в «колонки через `|`» (маркер `[Таблица страницы]`);
- страницы порциями (по бюджету контекста `CONTEXT_CHAR_BUDGET` = 14 000 знаков)
  уходят в LLM, которая формирует перечень работ: `name` (наименование
  работы/группы элементов), `unit` (единица измерения), `volume` (числовой
  объём), `page`, `quote`;
- результат: `ВОР_ведомость_объемов_работ.json` (структура
  `{document, items, warnings}`) и `ВОР_ведомость_объемов_работ.xlsx`
  (колонки «Наименование работы / группы элементов», «Ед. изм.», «Объём»,
  «Стр.», «Цитата») в корне сессии.

**Детерминированный разбор структуры ВОР** (`extract_vor_rows`). Помимо
LLM-разбора, модуль умеет читать **структуру** ведомости без LLM: разделы
(«N. Название»), блоки/захватки («Деформационный блок №N. Секции X и Y»,
«Секция №N») и строки позиций с колонками «было / стало / разница». Результат —
список записей `{page, section, block, pos, name, unit, volume_was, volume_now}`
(объём «стало» — с учётом корректировки ПД). Функция используется сравнением с
перечнем (`vor_comparison`, §5.13), чтобы агрегировать объёмы одной конструкции
(напр. фундаментной плиты толщиной 1000 мм), разбитой в ВОР по блокам и секциям,
в одну группу элементов. Итоговый JSON/XLSX LLM-разбора при этом не меняется.

**ПД** (`POST /api/session/<id>/upload_pd`, поле `pdFile` в `upload_ifc`):

- разбирается **только текстовая часть**: страницы без осмысленного текстового
  слоя (графические/альбомные) пропускаются (число пропущенных указывается в
  `warnings`);
- LLM извлекает характеристики элементов: `element` (наименование
  конструкции/марки), `concrete_class` (класс бетона B), `strength` (прочность),
  `plasticity` (пластичность/подвижность), `frost_resistance` (морозостойкость F),
  `water_resistance` (водонепроницаемость W), а также `page`, `quote`;
- результат: `ПД_характеристики_элементов.json` (структура
  `{document, items, warnings}`) и `ПД_характеристики_элементов.xlsx` (колонки
  «Элемент / конструкция», «Класс бетона (B)», «Прочность»,
  «Пластичность / подвижность», «Морозостойкость (F)»,
  «Водонепроницаемость (W)», «Стр.», «Цитата») в корне сессии.

Оба результата регистрируются в списке файлов сессии (доступны для скачивания
через `download/<filename>` и `download_all`).

**Оптимизация разбора ВОР/ПД (актуализация).** Порции обрабатываются
**параллельно** (`_parse_batches`, до `LLM_MAX_CONCURRENCY` одновременно) —
результат не зависит от порядка ответов (порядок порций сохраняется). При
последовательной Ollama (`OLLAMA_NUM_PARALLEL=1`) время не меняется (запросы
ждут очереди внутри Ollama), при поднятом параллелизме — ускоряется за счёт
одновременной генерации ответов. **Объём порции (`CONTEXT_CHAR_BUDGET` = 14 000
знаков) менять нельзя**: замеры показали, что при больших порциях модель (8B)
начинает пропускать строки таблиц (ВОР: 32 порции → 217 записей; 11 порций по
~30 000 знаков → 152 записи; ПД: 83 → 40 записей), а удаление табличного блока
из порции обрушает качество (ВОР: 217 → 82). Результаты разбора **кэшируются**
по SHA-256 содержимого файла (`DOC_CACHE_DIR`, §6): повторная загрузка того же
ВОР/ПД берёт результат из кэша мгновенно (замер: ВОР 246 с → 0,00 с, ПД/ПОС —
аналогично).

**Использование в подборе работ** (с `doc_facts.py`, §5.8.1). Разбор ВОР/ПД
применяется подбором работ по перечню (`WORKS_SELECTOR=perechen`) там, где
данных модели не хватает — модуль
[`src/services/doc_facts.py`](./src/services/doc_facts.py) сопоставляет записи
документов с элементами/группами модели (по виду конструкции, части здания и
пересечению слов наименования; спорные случаи — локальная LLM, кеш
`data/doc_facts_cache.json`):

- **класс бетона B/W/F** по конструкции — в проекте класс указан лишь у
  единичных элементов модели, поэтому бетонная смесь без него не подбиралась.
  Источники: ВОР (класс прямо в наименовании, напр. «Фундаментная плита,
  толщиной 1000мм, бетон В30 W8 F150») и ПД (`concrete_class`/`frost_resistance`/
  `water_resistance`); отсутствующие W/F добираются из другой записи того же
  класса прочности;
- **класс арматуры и масса по диаметрам** («Арматура класса А500С» из ВОР /
  «Арматура A500c» из ПД) — для блока `REBAR_STEEL`: арматурная сталь
  добавляется отдельными строками по классу и диаметру из ВОР (Ø10 → `1.3-4-21`,
  Ø12–14 → `1.3-4-22`, Ø16–18 → `1.3-4-23`, Ø20–22 → `1.3-4-24`, Ø25–28 →
  `1.3-4-25`, Ø32 → `1.3-4-26`, Ø36–40 → `1.3-4-27`; см. §5.8.1, п. 4);
- **объёмы работ, которых нет в модели** (опалубка торца плиты, гидроизоляция
  шва, засыпка, галтель и т.п.) — из ВОР для строк «объём по проекту»;
- **сверка объёмов модель ↔ ВОР** по видам конструкций — в аналитическую справку
  (`analytic.json`, группа «Сверка с ВОР и ПД»).

Если документы не загружены, `doc_facts.load()` возвращает `None` и подбор
работает как раньше — шаг полностью аддитивен.

**Сравнение ВОР с итоговым перечнем** (независимо от `doc_facts`): после
завершения запуска АР модуль `vor_comparison.py` (§5.13) сопоставляет работы и
объёмы ВОР и сформированного `Финальный_перечень_работ.json` **по каждой группе
элементов и по каждой работе** (группа — канонический вид конструкции: стены,
плиты перекрытия, лестничные марши/площадки, балки, фундаментная плита и т.д.;
разбивка ВОР по секциям/этажам приводится к части здания; PDF ВОР разбирается
детерминированно — разделы/блоки/объёмы «стало») и пишет
`Сравнение_ВОР_и_перечня.xlsx/.json` в папку запуска (с подсветкой расхождений,
работ, отсутствующих в одном из документов, и арматуры «взято из ВОР»).

---

### 5. Этап 1 — запуск обработки (`run_<NNN>`)

`POST /api/session/<id>/select_rows` (первый запуск) или `.../new_run` (повторный)
→ `SessionManager.select_rows` / `new_run` → создаётся `outputs/<session_id>/run_<NNN>/`
(копируются исходные файлы из `original/`) → фоновый поток
`_run_processing_pipeline_in_run`.

#### 5.1. Применение материалов и фильтрация строк

1. **Материалы** (`rowMaterials`) записываются в колонку «Материал»
   листа `Данные` исходного Excel; карта сохраняется в `run_<NNN>/materials.json`.
   В АР фронтенд отправляет автоматически определённые значения (МССК-группа
   материала из `materialsGroupMap` или исходное значение колонки «Материал») —
   ручной выбор материала в превью отключён.
2. Из исходного Excel берутся только выбранные строки (`rowIndices`) →
   **`run_<NNN>/filtered_elements.xlsx`** (лист `Данные`).
3. Из `IFC_исходные_параметры.json` строится карта `GlobalId → строка материалов`
   (`_load_material_layer_map`, через `materials_lookup.extract_material_value`) и
   подмешивается колонкой `Свойство::IfcMaterialLayer::Name` в `filtered_elements.xlsx`.
   Если слоёв нет — fallback на пары `MGE_MaterialCode`/`MGE_Material` из Pset
   `ExpCheck_*` (двери, окна, витражи). Коды материалов (`MGE_MaterialCode`)
   могут быть заданы **на типе элемента** — они попадают в дамп благодаря
   обходу `IsTypedBy` (см. §3.1), поэтому материал определяется и в таких
   моделях.
4. **Уточнение неполных МССК-кодов материала** (`rowMaterialCodes`): карта
   пересчитывается из исходных индексов строк в позиции отфильтрованного
   DataFrame и передаётся в группировку (`material_code_overrides`).
   Для строк с уточнением группа материала (L3) определяется по указанному
   полному коду (напр. «СТ 00 15 01» — «Железобетон сборный»), а не по
   неполному значению из параметров элемента («СТ 00 15» — «Железобетон»).
   **Уточнение применяется и к самому `filtered_elements.xlsx`**
   (`SessionManager._apply_material_code_overrides`): в строках с уточнением
   колонка «Материал» заменяется на имя группы материала из справочника
   (напр. «Железобетон сборный»), а код в `Свойство::IfcMaterialLayer::Name` —
   на полный («(СТ 00 15)» → «(СТ 00 15 01)»). Это необходимо потому, что из
   `filtered_elements.xlsx` строятся и подбор таблиц (`works_table_selector`,
   §5.3), и подбор работ по перечню (`perechen_pipeline`, §5.8). Без этого
   признак «сборный/монолитный» терялся, и сборный ж/б подбирался как монолитный.

#### 5.2. Группировка — `group_excel.py` (`process_ifc_excel_ar`)

Обёртка с дисковым кешем вызывает **`group_elements_mssk`** — ту же функцию и ту же
иерархию, что и в режиме ЦС (§10, п. 4.2): дерево финального перечня АР должно совпадать
с КР. Отличаются только имена файлов (суффикс `_AR`), контракт с
`works_final_selector` не меняется. Функция `group_elements_ar` (упрощённая
группировка «МССК → Материал → Наименование») в модуле сохранена, но обёрткой
не используется.

Иерархия:

```
Часть здания (L1) — Подземная (до отм. 0,000) / Цоколь (от отм. 0,000) /
                     Надземная (выше отм. 0,000) — по колонке «Этаж»
  → Код МССК (L2) — название из data/elements_mssk_nested.json (через mssk_lookup);
                  пустые/неизвестные коды → «Прочее»
    → Материал (L3) — группа по МССК-коду материала из data/materials_mssk_nested.json
                     (через materials_lookup): 1 материал → имя группы,
                     >1 → «Многослойные», нет кода → «Прочее»; если источников
                     материала нет (нет IfcMaterialLayer/MGE-колонок) — уровень
                     пропускается
      → IFC-тип (L4, только если в группе несколько типов)
        → Геометрия (L5) — по GEOMETRY_GROUP_RULES (толщина стен, площадь плит,
                          периметр колонн, длина свай и т.д.)
          → Материал (L6) — «Материал: …» (только при нескольких материалах)
            → Бетон (L7) — «Бетон: В…, W…, F…»
```

Параметр `material_code_overrides` функции `group_elements_mssk` — уточнение
**неполных** МССК-кодов материала из `rowMaterialCodes` (см. §5.1 п. 4): для
строки с уточнением группа материала (L3) определяется по указанному полному
коду, что разделяет «Железобетон (СТ 00 15)» на «Железобетон сборный
(СТ 00 15 01)» / «Железобетон монолитный (СТ 00 15 02)». При наличии уточнений
дисковый кеш группировки игнорируется
(`_get_cached_or_compute(force_recompute=True)`).

Часть здания строки определяется функцией `get_building_part`:
**приоритет — числовой индикатор** значения «Этаж» (`_get_part_from_storey_name`:
«-N/M» → Цоколь, «-N» → Подземная, «N» → Надземная). Это сохраняет прежнее
поведение по проектам, где индикатор есть (1-й этаж на отм. 0,000 остаётся
надземным). **Если числового индикатора нет** (произвольные имена этажей:
«Подвал», «Тех. прост», «Крыша») — часть определяется по нормализованной
колонке **«Тип_этажа»** (`zero_step.classify_storey_type`: слова
«подвал/подзем/цоколь» + знак отметки этажа). Ранее такие имена безоговорочно
относились к надземной части.

Та же логика продублирована во **фронтенде** (`src/templates/index.html`,
`renderPreview`): «Часть здания» строки и группировка превью (`groupRowsCommon`)
используют числовой индикатор, а при его отсутствии — «Тип_этажа». Это важно,
т.к. веб-интерфейс строит дерево превью по `rowParts` и отправляет
`rowTypes` в `select_rows`/`new_run`.
Для каждой группы суммируются объём/площади (`_create_group`), порядок групп —
по порядку узлов в справочниках (сначала известные МССК/материалы, затем «Прочее»).

Разбивка смешанных листовых групп по частям здания (с переквалификацией
агрегатов) дополнительно выполняется на шаге финального подбора работ
(`works_final_selector`, §5.5 п. 0) функцией КР `split_leaf_groups_by_part` —
после добавления L1 группы уже почти всегда односоставные, но функция
гарантирует разбивку для групп, собранных из элементов с разными индикаторами
«Этажа» внутри одной части.

Актуализация агрегации в `_create_group`/`_add_geometry_groups` (общий код КР/АР):

- **объём группы** — coalesce: по каждой строке берётся первое ненулевое значение
  среди **всех** колонок-источников объёма (`_find_volume_columns`: NetVolume
  Qto_Wall/Qto_Slab/Qto_Beam/Qto_StairFlight/… + агрегированная «Объём, м3»);
  ранее выбиралась одна колонка (первая по порядку заголовков — стеновая),
  из-за чего объёмы плит/балок/лестниц терялись;
- **суммарный расход арматуры** (`total_reinforcement`, кг) — теперь
  Σ (ReinforcementVolumeRatio, кг/м³ × объём элемента, м³); ранее суммировались
  сами плотности армирования, что давало некорректные тонны арматуры.

Создаваемые файлы:

| Файл | Откуда |
|---|---|
| `run_<NNN>/Дерево_проекта_выбранные_элементы.xlsx` | дерево групп по выбранным строкам |
| `run_<NNN>/filtered_elements_grouped_AR.json` | JSON-дерево групп (вход для следующих шагов) |
| `run_<NNN>/Дерево_проекта.xlsx` | дерево по **всему** зданию: `process_ifc_excel_ar` повторно запускается на `IFC_ВСЕ_ДАННЫЕ*.xlsx` из `original/` (кеш) |
| `run_<NNN>/ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx` | листовые группы (по строке на группу): агрегаты `*_grouped` (объём, площади, количество), `Название_группы`, `Уровень_группы`, `Индексы_элементов` |
| `run_<NNN>/building_parts.json` | часть здания для каждой группы (модальная часть из элементов группы, по умолчанию «Надземная») |

#### 5.3. Подбор таблиц работ — `works_table_selector.py`

Ключевой шаг АР. `build_works_tables_json(excel_path=filtered_elements.xlsx,
output_path=..., global_constants=..., raw_dump_json_path=IFC_исходные_параметры.json)`
реализует 8 шагов алгоритма `data/algorithm.md`. Перед вызовом `session_manager`
дополняет `global_constants` константой `building_storeys_type` (этажность
здания) из файлов сессии, если она не задана в UI
(`zero_step.detect_storeys_type_from_session`: `IFC_глобальные_константы.json` →
fallback по колонке «Этаж» Excel полной модели; приоритет: UI → IFC).
Этажность определяется по **всей модели**, а не по выбранным строкам — в
выбранное подмножество этажи могут не попасть. Шаги алгоритма:

1. **Нормализация элемента** — строка Excel склеивается с записью сырого дампа
   (по `GlobalId`), отбрасываются пустые свойства; извлекаются `IfcClass`
   (с приведением подтипов: `IfcWallStandardCase → IfcWall` и т.д.),
   `PredefinedType`, код МССК, материал, `ConstructionMethod`, этаж, `IsExternal`,
   QTO (`NetVolume`, `GrossArea`).
2. **Сборники-кандидаты** — по `IfcClass + PredefinedType` в
   `data/ifc_to_collections.json` (сначала точные совпадения по PredefinedType,
   затем по классу).
3. **Уточнение по МССК** — код ищется в `data/msck_elements_compact.json`
   (точное совпадение, затем по укорачиваемому префиксу `ЭЛ 30 10 15 01 → ЭЛ 30 10 15`).
4. **Переключатель технологии** — материал + `ConstructionMethod`.
   **Приоритет у явного материала элемента**: сталь/металл → Сб. 9
   (только по полю «Материал», не по имени — чтобы не ловить «стальную
   закладную» в ж/б элементе); дерево/пластмасс/ПВХ/ГКЛ → Сб. 10;
   кирпич/кладка/блоки → Сб. 8. Технология `ConstructionMethod`
   (`Precast` → Сб. 7, `In-situ` → Сб. 6) и слова «сборн»/«монолит»
   проверяются после материала: Precast/In-situ применимы только к
   бетонным и ж/б конструкциям, но могут ошибочно стоять в IFC у
   элементов из других материалов (например, стальная труба-колонна
   ГОСТ 30245-2003 с `ConstructionMethod=Precast` → Сб. 9, а не Сб. 7).
5. **Выбор таблиц по ключевым словам** — поиск по названиям таблиц в
   `data/tree_work_compact.json` (индекс строится по кодам вида `3.6-15`);
   сначала ключевые слова по типу элемента (`IfcWall → «стен», «перегородок»`, …),
   таблицы «усиления» существующих конструкций исключаются (элементы IFC —
   новое строительство); при пустом результате — фолбэк по словам
   материала/имени (длина ≥ 4 символов), без смешивания наборов (слово
   материала вроде «сталь» матчит нерелевантные таблицы рядом с точным
   совпадением по типу).
6. **Правила по сборникам** (`work_type` из `data/works_classification.json`):
- **Сб. 6 (монолит)**: отдел 1.1 (деревянная щитовая опалубка) → `COMPLEX`
       (комплексные таблицы 3.6-1…3.6-15 по типу элементу и части здания:
       3.6-1 фундаменты, 3.6-8 стены подземной/цокольной, 3.6-10/3.6-11 стены
       (бетон/ж/б), 3.6-9 колонны, 3.6-12/3.6-13 балки, 3.6-15 + 3.6-5 плиты/лестницы);
       **для ж/б монолитных элементов в COMPLEX-режиме ДОПОЛНИТЕЛЬНО
       добавляется SEPARATE-пакет таблиц** (role `package`) — развёрнутый
       расчёт опалубки/арматуры/бетонирования отдела 1.2 по части здания и
       высоте (см. ниже) + таблица ухода за бетоном;
        **фундаментная плита** (`IfcSlab` с `PredefinedType=BASESLAB` либо
       «фундаментная плита» в имени) — таблицы фундаментов, а не перекрытий:
       3.6-1 (раздел 1.1.1) + 3.6-6 (раздел 1.1.2); отделы 1.2–1.4
       (индустриальная/крупнощитовая/переставная опалубка) → `SEPARATE`:
       пакет из 4 таблиц (монтаж/демонтаж опалубки + арматура + бетонирование),
       раздел зависит от части здания и высоты (3.6-74…3.6-97); **фундаменты
       (`IfcFooting`) и фундаментные плиты — всегда раздел 1.2.2 «Фундаменты
       под здания и сооружения» (3.6-70…3.6-73) независимо от константы
       `formwork_type`**: крупнощитовая опалубка отдела 1.3 (3.6-107/108)
       применяется только к стенам, колоннам и днищам, а на проекте может
       использоваться несколько типов опалубки одновременно
       (крупнощитовая — для стен/колонн, мелкощитовая — для фундаментов);
       + уход за бетоном (3.6-98 лето / 3.6-61 зима).
    - **Сб. 7 (сборные)** → `SEPARATE`: монтажная таблица + стыки/замоноличивание
      (3.7-1…3.7-88, выбор по типу элемента, части здания, `IsExternal`,
      этажности здания); фундаменты и фундаментные плиты → 3.7-1 (+3.7-2).
      Этажность здания определяется константой `building_storeys_type`
      («одноэтажное»/«многоэтажное», см. шаг 7): одноэтажное → 3.7-5
      (колонны), 3.7-9 (балки), 3.7-13 (плиты покрытий), 3.7-16 (панели
      наружных стен); многоэтажное → 3.7-22 (колонны), 3.7-10 (балки/ригели),
      3.7-15 (плиты перекрытий), 3.7-17 (панели наружных стен). Если константа
      не задана — прежняя эвристика по высоте здания (многоэтажное при высоте
      > 30 м).
   - **Сб. 8 (кирпич/блоки)** → `COMPLEX` по ключевым словам.
   - **Сб. 11 (полы)** → `SEPARATE`: слой пирога определяется по коду МССК
     (30 32 30 стяжка, 30 32 15 гидроизоляция, 30 32 20 теплоизоляция) или материалу.
   - Остальные сборники → поиск по ключевым словам (до 3 таблиц).
7. **Константы проекта** (`_resolve_constants`): приоритет «свойство элемента →
   константа → значение по умолчанию»: `building_height_m` (число → диапазон
   до 30/30-40/…/150-250), `floor_height` (высота этажа, м),
   `building_storeys_type` (этажность здания: «одноэтажное»/«многоэтажное»;
   определяется по модели IFC в `zero_step` по наибольшему числовому индикатору
   в колонке «Этаж» всех элементов (≥ 2 → многоэтажное), подставляется в UI и
   в `global_constants` при запуске; если не задана — при подборе Сб. 7
   действует прежняя эвристика по высоте здания), `formwork_type`,
   `concrete_curing_season`, `concrete_placement_scheme`, `crane_type`,
   `crane_capacity`, `bucket_capacity`, `equipment_power` (последние четыре
   на выбор таблицы не влияют — параметры следующего этапа). Параметры
   `building_part` (часть здания, из
   этажа элемента) и `wall_location` (из `Pset_WallCommon::IsExternal`)
   **константами проекта больше не являются** — берутся только из самого
   элемента IFC-модели (fallback: «надземная» / «наружная»); в схеме
   `global_constants` (UI, ПОС) они отсутствуют.
8. **Объёмы** — QTO элемента (`volume_m3`, `area_m2`) прикладываются к каждой
   подобранной таблице.

Если сборники-кандидаты не определены — глобальный поиск по ключевым словам по всем
сборникам дерева работ; иначе элемент получает `note` с причиной.

**Выход**: `run_<NNN>/Подобранные_таблицы_работ.json`:

```json
{
  "generated_at": "...", "algorithm": "data/algorithm.md", "processing_type": "AR",
  "global_constants": {...},
  "total_elements": N, "total_works": M,
  "elements": [
    {
      "element": {"global_id", "name", "ifc_class", "predefined_type",
                   "mssk_code", "material", "construction_method", "storey", ...},
      "mssk_context": {"code", "name"},
      "candidate_collections": [...],
      "selected_collection": "Сборник  6. ...",
      "work_type": "COMPLEX | SEPARATE",
      "works": [{"code": "3.6-11", "name", "collection", "department", "section",
                  "role": "main|additional|package", "work_type", "quantity": {...}}],
      "applied_constants": {...},
      "note": ""
    }
  ]
}
```

#### 5.4. Работы из цифрового сборника — `works_fetcher.py`

После подбора таблиц (`Подобранные_таблицы_работ.json`) новый модуль
`works_fetcher.py` запрашивает в **цифровом сборнике (larix)** сами работы
подобранных таблиц:

1. **Актуальный период ТСН** — `GET WORKS_PERIOD_FILTER_URL`
   (`catalog/period/filter`, страница 0/50, сортировка `DATE_START DESC`).
   Из ответа фильтруются записи: `baseTypeCode == "TSN"` и наличие слова
   **«индекс»** в `title` (отличает сам период — «221 индекс/дополнение 82 …» —
   от технических правок и тестовых дополнений). Выбирается запись с
   максимальным `dateStart` (при равенстве — поздний `createdOn`).
   Период сохраняется в корне сессии: **`outputs/<session_id>/period.json`**
   (`period`, `id`, `date_start`, `base_type_code`, `fetched_at`).
2. **Работы таблиц** — по каждому уникальному шифру (`code`, например
   `3.6-71`) из `Подобранные_таблицы_работ.json` выполняется
   `GET WORKS_WORK_PROCESS_URL` (`catalog/work-process/list`): `per=<title
   периода>`, `pressmark=<код таблицы>`, `search_mode=CONTAINS`,
   `deleted=0`, сортировка `PRESSMARK_SORT ASC`; выборка постраничная
   (`page_size=10`, до `paging.totalPages`).
3. Все полученные работы сохраняются в папке запуска:
   **`run_<NNN>/Подобранные_работы.json`** (период — общий для запусков —
   остаётся в корне сессии):

```json
{
  "generated_at": "...", "processing_type": "AR",
  "period": "221 индекс/дополнение 82 ...", "period_id": 176009637,
  "source": "Подобранные_таблицы_работ.json",
  "total_tables": N, "total_works": M,
  "tables": [
    {"code": "3.6-71", "name": "Демонтаж опалубки ...",
     "works_count": 5,
     "works": [{"pressmark": "3.6-71-1", "title": "...", "unitOfMeasure": "100 м2",
                "status": "Утвержден", "id": 27115691, ...}]}
  ]
}
```

Авторизация — Bearer-токен Keycloak (тот же `KeycloakTokenProvider`, что и в
режиме КР; при 401 токен обновляется и запрос повторяется один раз), fallback —
статичный `WORKS_API_TOKEN`. Эндпоинты задаются переменными окружения
`WORKS_PERIOD_FILTER_URL`, `WORKS_WORK_PROCESS_URL` и
`WORKS_WORK_PROCESS_DETAIL_URL` (`.env`).
Шаг выполняется в `_run_processing_pipeline_in_run` (АР) после
`build_works_tables_json`; ошибка внешнего API логируется, но не прерывает
финальную сборку запуска. Отладка: `python -m src.services.works_fetcher
<Подобранные_таблицы_работ.json> <папка_сессии> [папка_для_Подобранные_работы.json]`.

**Детальные параметры позиций (разбивка стоимости)** — `fetch_work_details`:
`GET WORKS_WORK_PROCESS_DETAIL_URL` (`catalog/work-process/detail`) по `id`
каждой работы и `period=<period_id>` возвращает **полную разбивку стоимости**
(`curSalary` — ЗП, `curOperationOfMachines` — ЭМ,
`curCostOfMaterialResources` — МР, `curDirectCosts`, `curTotalCost` и т. д.),
которой **нет** в ответе списка работ (`catalog/work-process/list` содержит
только МР и итоги). Используется `works_final_selector` (§5.5) после LLM-выбора
работ — по id только ВЫБРАННЫХ работ (не всех кандидатов); ошибка отдельной
позиции (404 и т. п.) пропускается с предупреждением и не прерывает обработку.

#### 5.5. Финальный подбор работ через LLM — `works_final_selector.py`

После получения работ из цифрового сборника модуль `works_final_selector.py`
(версия 13.2) отбирает подходящие работы с помощью **LLM** (та же модель, что и
для разбора ПОС — `pd_parser.LLMClient` / Ollama, `OLLAMA_BASE_URL` +
`NORMS_LLM_MODEL`). Единица обработки — **листовая группа элементов** дерева
группировки АР (`run_<NNN>/filtered_elements_grouped_AR.json`, §5.2: Часть
здания → МССК → Материал → IFC-тип → Геометрия → Материал/Бетон; листовые
узлы содержат `indices` —
0-based индексы строк `filtered_elements.xlsx`, они же позиции записей
`elements` в `Подобранные_таблицы_работ.json`). **Группировка и объёмы — по
правилам КР** (`api_works_lookup` / `ifc_reference_builder`), чтобы финальный
перечень АР совпадал с перечнем КР по составу групп и объёмам работ:

0. **Разделение групп по частям здания** — перед подбором листовые группы
   проходят `ifc_reference_builder.split_leaf_groups_by_part`. Часть элемента
   определяется **СТРОГО по числовому индикатору «Этажа»** (FIX-46: «-N/M» →
   цоколь, «-N» → подземная, «N»/«Крыша» → надземная; переквалификация 1-го
   этажа по «Типу этажа» «Цокольный» **убрана** — отметка 0,000 ошибочно
   размечала 1-й этаж как цокольный). Агрегаты подгрупп (объём, площади,
   расход арматуры, площадь опалубки) пересчитываются по элементам своей
   части; сумма подгрупп равна агрегатам исходной группы. Сборный
   железобетон подбирается на общих основаниях — попытка подбора делается
   для **всех** групп элементов (FIX-47); если кандидатов в таблицах нет,
   группа остаётся без строк работ.
1. **Единица подбора — таблицы работ элемента-представителя**: LLM-запрос
   строится по записи **первого элемента группы** (минимальный индекс) — по
   его таблицам из `Подобранные_таблицы_работ.json` (`element.works[].code`,
   роли main/package/additional) из `Подобранные_работы.json` берутся работы
   **каждой таблицы** (работы с чужими шифрами — не начинающимися с
   `<код таблицы>-` — отфильтровываются). Контекст запроса: наименование,
   IFC-класс, МССК, материал, часть здания, высота здания, толщина элемента
   и наименьшая сторона сечения (мм), число элементов группы. Промпт встроен
   в модуль (`SYSTEM_PROMPT`).
2. **Предфильтрация и ранжирование работ таблицы** (до LLM,
   `_filter_works_in_table` / `_dedupe_variants` / `_rank_works`):
   - универсальный анти-фильтр (`_UNIVERSAL_HARD_ANTI`: торкрет, мосты,
     тоннели, резервуары, «на каждые», «добавляется к» и т.п.);
   - «семейные» фильтры (`FAMILY_FILTERS` по классификации `_classify_family`:
     wall / foundation_slab / floor / column / beam / stair_march / pile /
     insulation / masonry / preparation …): required/forbidden ключевые
     слова — только для главных таблиц (main/package);
   - совместимость по части здания (`_part_compatible`: подземная/цокольная
     ↔ надземная), по высоте здания (`_is_height_compatible` — диапазоны
     «до 57», «более 57 до 75 м» …) и по геометрии (`_geometry_compatible` —
     толщина «до X мм»);
   - если после всех фильтров пусто — каскадный fallback на ближайший
     непустой этап (геометрия → высота → часть → семейство), таблица
     помечается `fallback`;
   - дедупликация вариантов расценок (`_dedupe_variants` — «до X мм» /
     «свыше X мм») и ранжирование по баллам (`_score_work`: семейство +
     технологичность + часть + семантические бонус/штраф).
   Для семейств wall/foundation_slab/floor убираются строки «Устройство …»
   (не основные работы этих семейств).
3. **Объём работ — из агрегатов группы (правила КР)**: измерители группы
   берутся из агрегатов дерева группировки (`_group_quantity_from_aggregates`;
   для разделённых подгрупп — пересчитаны): `total_volume` → `volume_m3`,
   площадь группы из `total_areas` (приоритет ключей как в КР: «Площадь, м2»
   → GrossSideArea → NetSideArea → GrossArea → NetArea; `_pick_group_area_m2`),
   `count` → количество, а также **длина швов** (`joint_length_m` — сумма
   периметров элементов группы, для расценок в единицах длины) и длина.
   Приоритет измерителя — как в КР: объём → площадь → длина → длина швов →
   количество. **Опалубка**: для расценок монтажа/демонтажа опалубки (м²)
   объём = площадь опалубки группы (`formwork_area` из дерева группировки;
   при отсутствии группы — по-элементный расчёт `_sum_formwork_area`/
   `_formwork_area_m2` из параметров подбора и QTO: фундаментная плита —
   периметр × толщина, стены — 2×длина×высота, перекрытия — 2×(длина+ширина)×
   толщина, колонны — 2×(длина+ширина)×высота); для **плитных групп**
   (фундаментные плиты/перекрытия) fallback на площадь плиты запрещён —
   если площадь опалубки не рассчитана, объём остаётся пустым (как в КР).
   **Арматура**: расход арматуры группы (`total_reinforcement` из дерева
   группировки; при отсутствии группы — Σ ReinforcementVolumeRatio × объём
   по `filtered_elements.xlsx`, `_load_rebar_mass_by_gid`) подставляется
   **только** в расценку «Установка … отдельных стержней …» (кг ÷ 1000);
   прочие арматурные расценки (каркасы/сетки/закладные детали) идут без
   объёма — иначе объём задваивается. **Корректировка объёмов по
   `koefs.xlsx`** (`works_cost._get_corrected_volume`, как в КР): объёмы
   строк работ умножаются на нормы расхода ресурсов, корректировка
   применяется к плоской таблице работ в порядке групп; стоимость считается
   по скорректированным объёмам.
4. **LLM выбирает РОВНО ОДНУ работу в каждой таблице-кандидате**: в промпт
   передаются секции «таблица (код, название, роль) → строки работ
   (шифр | наименование | ед. изм.)»; LLM возвращает JSON
   `{"selected": [{"table": "3.6-70", "pressmark": "3.6-70-3", "reason": "..."}]}`
   — по одной строке на таблицу (правила промпта: ж/б элемент →
   «железобетонных», фундаментная плита → «фундаментных плит»/«плоских»,
   толщина «до X мм» с X ≥ толщины элемента и т.д.). Выбор сопоставляется с
   кандидатами по коду таблицы + полному шифру (`_match_selection`).
5. **Fallback по таблице**: если LLM не выбрала работу для таблицы (ошибка
   LLM, пустой ответ, неизвестный шифр) — берётся **ближайшая работа**
   детерминированно (`_pick_closest`: без семантических штрафов, затем
   минимальная толщина «до X мм» с X ≥ толщины элемента, иначе максимальная;
   при отсутствии толщин — первая по ранжированию), с пометкой в `reason`.
   Ошибка LLM не обнуляет результат запуска — по каждой таблице группы
   всё равно выходит ровно одна строка работы.
6. **Разбивка стоимости выбранных работ (ЗП/ЭМ/МР)**: список работ
   (`catalog/work-process/list`) не содержит `curSalary`/`curOperationOfMachines`
   (только МР и итоги) — после LLM-выбора для каждой выбранной работы (по
   `work_id` — id работы в larix, сохраняется в строке работы) запрашиваются
   **детальные параметры позиции** `catalog/work-process/detail`
   (`works_fetcher.fetch_work_details`, §5.4) за период ТСН запуска
   (`period_id` из `Подобранные_работы.json`). Полученные показатели
   (`salary`/`curSalary`, `operationOfMachines`/`curOperationOfMachines`,
   `costOfMaterialResources`/`curCostOfMaterialResources`,
   `directCosts`/`curDirectCosts`, `totalCost`/`curTotalCost`) подставляются в
   строки выбранных работ (`_enrich_selected_works_with_costs`). При
   недоступности эндпоинта/периода или ошибке отдельной позиции стоимость
   остаётся по данным списка работ — шаг не обнуляет результат запуска.

Если `filtered_elements_grouped_AR.json` отсутствует/повреждён или индексы
групп не покрывают все записи — неохваченные элементы обрабатываются
поодиночно (прежнее поведение, объёмы — из записи самого элемента).

**Выход** (в `run_<NNN>/`):

- **`Финальный_перечень_работ.json`** — структурированный перечень: по каждой
  группе — элемент-представитель (`element`), МССК-контекст, таблицы
  (`tables` — таблицы элемента из `Подобранные_таблицы_работ.json`),
  выбранные работы (`selected_works`), `note` (причина пустого подбора),
  классификация (`family`), часть здания (`part` — Подземная/Цоколь/Надземная),
  `work_type`; метаданные группы: `group` (название, число элементов),
  `group_path` (путь в дереве группировки), `group_quantity` (суммарные
  измерители группы: объём/площадь/длина/длина швов/количество),
  `group_formwork_area_m2` (площадь опалубки), `group_joint_length_m`
  (длина швов), `group_header` / `group_base_name` (заголовок и базовое
  название группы в формате КР), `group_rebar_mass_kg` (расход арматуры
  группы, кг). Строка работы: `pressmark`, `title`, `unit_of_measure`,
  `work_id` (id работы в larix), `table_code`/`table_name`,
  компоненты стоимости из детальных параметров позиции
  (`catalog/work-process/detail`, §5.4) — `salary`/`cur_salary` (ЗП),
  `operation_of_machines`/`cur_operation_of_machines` (ЭМ),
  `cost_of_material_resources`/`cur_cost_of_material_resources` (МР),
  `direct_costs`/`cur_direct_costs`, `quantity` (групповые измерители:
  `volume_m3`, `area_m2`, `length_m`, `joint_length_m`, `count`;
  для арматурных работ — `mass_t`: расход арматуры группы, т),
  `reason` (обоснование выбора / пометка fallback), `llm_selected`;
- **`Финальный_перечень_работ.xlsx`** — итоговая таблица в структуре режима КР
  (`ОБЩИЙ_Финальный_перечень_работ.xlsx`): колонки **«Шифр ТСН», «Наименование
  расценки/ресурса», «Ед. изм.», «Объём работ», «ЗП», «ЭМ», «МР», «Стоимость»**
  (колонка «Стоимость за Ед. Изм.» удалена — стоимость считается по
  компонентам ЗП/ЭМ/МР), лист `Данные`. Группы разбиты по **частям здания**
  (как в КР): секции «ПОДЗЕМНАЯ ЧАСТЬ» / «ЦОКОЛЬ» / «НАДЗЕМНАЯ ЧАСТЬ»
  (порядок: Подземная → Цоколь → Надземная), в конце каждой секции —
  «ИТОГО по подземной/цокольной/надземной части:», последняя строка — «ИТОГО:»
  (суммы колонок «ЗП», «ЭМ», «МР», «Стоимость»). Перед работами каждой
  группы — строка-заголовок группы элементов (жирный шрифт, серая заливка)
  **в формате КР**: «Тип элемента (Материал, Имя элемента) Кол-во: N»
  (`_group_header` по первому элементу группы: тип — из колонки «Тип (RU)»,
  материал — нормализованный «Материал», имя — без цифрового ID в конце;
  «Кол-во: N» — при числе элементов > 1), между группами — пустая строка.
   Группы без работ (кандидаты не найдены, LLM не выбрал работ) выводятся только
  заголовком — строка «Работы не подобраны» не добавляется (как в КР;
  строка остаётся только если в перечне нет ни одной работы).
   Заполнение колонок: Шифр ТСН = `pressmark` работы, наименование = `title`,
   ед. изм. = `unitOfMeasure` (с множителем нормы: «100 м2» → «100 м²»);
   объём — **по правилам КР** (см. п. 3): измеритель группы (объём → площадь →
   длина → длина швов → количество) с делением на множитель нормы из единицы
   измерения («100 м2» → площадь / 100 и т.д.), опалубка — по площади опалубки
   группы (плитные группы — только по площади опалубки), расценки в метрах
   (швы, стыки) — по длине швов группы, арматура — только «отдельные
   стержни» (расход группы, кг ÷ 1000), с последующей корректировкой по
   `koefs.xlsx`; **ЗП** = `curSalary` × Объём работ
  (fallback — `salary`), **ЭМ** = `curOperationOfMachines` × Объём работ
  (fallback — `operationOfMachines`), **МР** = `curCostOfMaterialResources` ×
  Объём работ (fallback — `costOfMaterialResources`) — компоненты считаются
  по скорректированным объёмам; **Стоимость** = ЗП + ЭМ + МР (сумма
  вычисленных компонентов); компоненты ЗП/ЭМ/МР берутся из
  детальных параметров позиции (§5.5 п. 6); деньги форматируются
  `works_cost.format_money` («392 458.21»). Строится функцией
  `works_final_selector.build_final_works_xlsx`. Файл доступен для просмотра в
  веб-интерфейсе (`👁️ Просмотр`,
  `GET .../preview_result/Финальный_перечень_работ.xlsx`) и скачивания.

Шаг выполняется в `_run_processing_pipeline_in_run` (АР) после `works_fetcher`
(прогресс 75); ошибка шага логируется, но не прерывает финальную сборку.
Отладка: `python -m src.services.works_final_selector <Подобранные_таблицы_работ.json>
<Подобранные_работы.json> <папка_запуска>`.

#### 5.6. Сборка финального JSON — `ifc_json_builder.py`

`build_final_json(input_folder=run_dir, source_file=<имя IFC/PDF>,
discipline="AR", output_filename="final_result_AR.json")` — сопоставляет группы
(`filtered_elements_grouped_AR.json`), элементы и подобранные таблицы работ в
единый выходной JSON **`run_<NNN>/final_result_AR.json`** (для КР используется
`selected_elements_grouped.json` и `final_result_KR.json`).

#### 5.7. Финализация

В список итоговых файлов запуска АР входит **только** итоговая таблица работ:

- **`Финальный_перечень_работ.xlsx`** — итоговая таблица в структуре режима КР
  (`works_final_selector.build_final_works_xlsx`, §5.5): шифры ТСН, объёмы,
  компоненты стоимости **«ЗП», «ЭМ», «МР»** (показатели ответа larix × объём
  работ) и «Стоимость» (= ЗП + ЭМ + МР), строки-заголовки групп элементов,
  итоговая строка «ИТОГО:» (суммы ЗП, ЭМ, МР, Стоимость). Единственный файл,
  выводимый на веб-интерфейсе в
  режиме АР (с кнопками «👁️ Просмотр» и «Скачать»).

Остальные артефакты запуска (`Подобранные_таблицы_работ.json`,
`Подобранные_работы.json`, `Финальный_перечень_работ.json`,
`final_result_AR.json`, `ДЛЯ_СМЕТЧИКА_*.xlsx`, `Дерево_проекта*.xlsx`,
`filtered_elements.xlsx`, `materials.json`, `building_parts.json`,
`filtered_elements_grouped_AR.json`, `period.json` и сырой дамп) продолжают
создаваться в `run_<NNN>/` и корне сессии (нужны для работы конвейера и
отладки), но в итоговую выдачу (список файлов запуска для скачивания через
`GET .../download/<filename>` и `download_all`) **не попадают**.
`final_result_AR.json` по-прежнему читается API напрямую из директории запуска
(`get_final_json` ищет файл на диске, а не в списке файлов запуска).

Статус запуска → `completed`.

**Пересборка финального JSON по запросу** — эндпоинты
`POST /api/session/<id>/run/<N>/build_final_json` (тело `processingType`),
`GET .../run/<N>/final_json/status` и `GET .../run/<N>/final_json/result`
(`SessionManager.build_final_json_by_number`): повторный запуск
`ifc_json_builder.build_final_json` в фоновом потоке для завершённого запуска
(например, после изменения данных запуска). Финальный JSON также ищется API
напрямую на диске: директория запуска → список файлов запуска → корень сессии.
Состав итоговых файлов запуска при этом не меняется (только
`Финальный_перечень_работ.xlsx`).

#### 5.8. Подбор по перечню сметчиков — `perechen_pipeline.py` + `perechen_kb.py`

Активный вариант финального подбора АР (`WORKS_SELECTOR=perechen`, `.env`).
Выполняется в `_run_processing_pipeline_in_run` (АР) вместо `works_final_selector`
(§5.5) — прогресс 75. Работы и стоимость берутся из **локального справочника**
`data/price_cost.xlsx` (кеши `data/local_works.json`, `data/local_resources.json`,
`data/local_norms.json`; `WORKS_SOURCE=local`). Шаг `works_fetcher` (larix,
§5.4) в конвейере сохраняется, но его результат этим вариантом не используется.

**Вход**: `run_<NNN>/Подобранные_таблицы_работ.json` (элементы + их таблицы),
`run_<NNN>/filtered_elements_grouped_AR.json` (дерево групп АР),
`run_<NNN>/filtered_elements.xlsx` (поэлементные объёмы/площади/арматура),
`IFC_глобальные_константы.json` (высота здания). **Выход**:
`run_<NNN>/Финальный_перечень_работ.json` + `.xlsx` (структура КР: «Шифр ТСН,
Наименование расценки/ресурса, Ед. изм., Объём работ, ЗП, ЭМ, МР, Стоимость»,
секции по частям здания, «ИТОГО:») и `run_<NNN>/analytic.json` (§5.12).

Основной шаг — `perechen_pipeline.build_final_from_perechen(tables_json_path, run_dir)`:

1. Читает элементы (`Подобранные_таблицы_работ.json`) и листовые группы дерева АР;
   высоту здания — из `IFC_глобальные_константы.json`
   (`perechen_kb.building_height_for_run`).
2. Обходит **листовые группы**, делит их по **типу элемента** (имя без ID +
   материал, `perechen_kb.type_key`); объёмы суммирует поэлементно из
   `filtered_elements.xlsx` (для однородной группы — итоги группы надёжнее).
3. По каждому типу вызывает `perechen_kb.pick_for_element` (подбор работ) и
   `finalize_rows` (формирование строк перечня).
4. Сортирует по частям здания (Подземная → Цоколь → Надземная), пишет JSON и XLSX.

**База знаний `perechen_kb.py`** (`data/perechen_kr.xlsx`, лист «ВОР КР+расценки»):

- `load_perechen()` — разбор перечня в подразделы (Часть здания → Раздел →
  Подраздел (конструкция) → Группа → Работа → расценки/ресурсы);
- фильтры **кодом** (без LLM): `filter_by_height` (диапазон высоты здания из
  названия таблицы), `filter_by_thickness` (варианты расценок по толщине),
  `is_precast` (сборный ж/б — см. ниже);
- **сборный железобетон** (`is_precast`): работы подбираются по монтажу сборных
  конструкций, если они есть в подразделе перечня (напр. «Установка сборных
  железобетонных лестничных маршей» + ресурс «Марши лестничные сборные»).
  В `select_works` и `review_selection` для сборного элемента список работ
  ограничивается сборными (`_is_precast_work`), поэтому монолитные
  опалубка/арматура/бетонирование не предлагаются и не возвращаются контролёром.
  Если в подразделе сборных работ нет — работ не подбирается, и `pick_for_element`
  уходит в `search_full` (полный справочник ТСН). Ранее сборный ж/б безусловно
  отправлялся в `search_full` с пометкой «в перечне КР только монолит», из-за
  чего работы по сборным лестничным маршам из перечня не использовались;
- шаги **LLM** (Ollama, `KB_LLM_MODEL`, `KB_THINK`): `select_subsection` (выбор
  конструкции/подраздела — IFC-класс ненадёжен, смотрят имя/материал/МССК),
  `select_works` (какие работы нужны), `review_selection` (контролёр),
  `select_rates` (выбор конкретных расценок внутри работ), `search_full`
  (запасной путь: ключевые слова → поиск по полному справочнику ТСН
  `tree_work_compact.json` → выбор до 4 расценок);
- правила кодом: `_apply_rebar_rule` (все диаметры арматуры), `_attach_parent_works`
  (родительские работы материала), добавление сопутствующих работ бетонной
  конструкции («не моделируется»), отсечение бетонных работ для не-бетона;
- **карта «класс + диаметр → шифр работы»** (`rebar_diameter_map()` /
  `rebar_pressmark(cls, diameter)`, см. §5.8.1 п. 4): по строкам перечня
  собираются работы арматурной стали с диапазоном из «Параметризации»
  («Ф = 10», «12<=Ф<=14», «16<=Ф<=18», «20<=Ф<=22», «25<=Ф<=28», «Ф = 32»,
  «36<=Ф<=40»); для каждой пары «класс + диаметр» выбирается самый частый шифр
  (голосование). Используется блоком `REBAR_STEEL` для раскладки массы ВОР по
  работам: Ø10 → `1.3-4-21`, Ø12–14 → `1.3-4-22`, Ø16–18 → `1.3-4-23`,
  Ø20–22 → `1.3-4-24`, Ø25–28 → `1.3-4-25`, Ø32 → `1.3-4-26`, Ø36–40 →
  `1.3-4-27` (А500); А240: Ø6–7 → `1.3-4-1`, Ø8 → `1.3-4-2`, Ø12 → `1.3-4-4`,
  Ø16–18 → `1.3-4-5`;
- кеш решений — `data/kb_cache.json` (подпись = имя без ID + материал + часть +
  высота + версия; `KB_CACHE_PATH`).

**Расчёт строк** (`finalize_rows`, `_volume`, `_formwork_area`, `_costs`):

- объём: м³ — по объёму элемента; м² — площадь (или площадь опалубки, или
  ×число слоёв); т — только «установка отдельных стержней» (кг арматуры ÷ 1000,
  при отсутствии данных — норма армирования); шт — количество элементов;
  «конструкц»/«элемент» (напр. «100 сборных конструкций» — монтаж сборного ж/б) —
  количество элементов группы, приведённое к единице расценки;
- **расход арматуры из модели** (`_element_quantities`): колонка
  `ReinforcementVolumeRatio` (кг/м³) домножается на объём элемента, но
  **значения-заглушки игнорируются** — в моделях часто проставлено `999` как
  признак «данных нет», а не норма армирования (считать по ней 999 кг/м³ нельзя:
  получались тысячи тонн арматуры). Реальными считаются значения 0 < v < 500 кг/м³;
  при отсутствии данных объём берётся по норме (`REBAR_NORM`), а при загруженном
  ВОР — по расходу арматуры из ВОР (блок `DOC_FACTS`, §5.8.1). Для арматурной
  стали (`REBAR_STEEL`, блок 11) при загруженном ВОР строки формируются
  **по классам и диаметрам** из ВОР (объём — масса ВОР по диаметру, §5.8.1 п. 4),
  а в `group_quantity` группы пишется разбивка `rebar_by_diameter`
  (`[{cls, diameter, kg}]`) рядом с суммарным `rebar_kg`;
- **опалубка (м²) считается по правилам ЦС (КР)** — `_formwork_area`:
  - плиты с данными периметра/толщины (фундаментные плиты, перекрытия,
    приямки, лестничные площадки) — периметр × толщина, для перекрытий
    дополнительно площадь плиты (`q["fw_hint"]`; поэлементно считается той же
    функцией `group_excel._element_formwork_area_m2`, что и в КР);
  - стены, балки и прочие элементы (периметра/толщины нет) — «Площадь, м2»
    группы (`q["area"]`), как в КР;
  - плитные группы (плита/перекрытие) без рассчитанной площади опалубки —
    объём не считается (площадь плиты не является площадью опалубки).

  Ранее опалубка считалась приближённо по геометрии (стены — 2 × объём /
  толщина, плиты — объём / толщина, фундаментные плиты — 4 · √(объём /
  толщина) · толщина), из-за чего объёмы работ расходились с ЦС: у стен —
  ровно в 2 раза, у плит — на десятки процентов. Приведено к правилам КР;
- **ЗП/ЭМ/МР** = `curSalary` / `curOperationOfMachines` /
  `curCostOfMaterialResources` из `local_works` × объём; для ресурсов — цена из
  `local_resources`;
- **бетонная смесь** — только подходящего класса прочности
  (`local_resources.concrete_mixes`, `_concrete_resource`);
- **уход за бетоном** добавляется правилом для монолита (`3.6-98-1`), кроме
  бетонной подготовки.

Поверх базовой логики модуль накладывает цепочку доработок (monkey-patch-обёрток,
порядок — снизу вверх по файлу), каждая дописывает `analytic.json`:

| № | Блок | Что делает |
|---|---|---|
| 0 | База | `finalize_rows`, `_volume`/`_costs`, `_concrete_resource` |
| 1 | Доработки | точный класс бетона по W/F; огрунтовка под наплавляемую гидроизоляцию (`3.8-48-1`); дедупликация работ лестниц; оценка цены ресурса; материал утеплителя по справочнику; количество ресурса без объёма — по норме расхода (`local_norms`) |
| 2 | `PROJECT_CONSTANTS` | константы проекта (UI → IFC → ПОС → ПЗ); сезон ухода за бетоном (холодный период → `3.6-61-1`) |
| 3 | `PD_FULL` | бетон по ПОС: «кран-бадья» → «бетононасос»; зимние методы; подстановка значений `pd_full.json` в перечень и справку |
| 4 | `ANALYTIC` / `ANALYTIC_V2` | аналитическая справка (`analytic.json`) |
| 5 | `TSN_COEFFS` | поправки из сборников ТСН: применимые → в перечень, неясные → в справку |
| 6 | `TSN_MODE` | учитывать/не учитывать сборники ТСН (`tsn_mode.json`) |
| 7 | `AN_GROUPS` | группировка справки по источникам (ПОС / ПЗ / ПОС и ПЗ / сборники ТСН / модель) |
| 8 | `IFC_FEATURES` | признаки геометрии (`ifc_features.json`) → в проверку поправок и справку |
| 9 | `CONFLICTS` (+`FIX1`/`FIX2`/`RULES`) | противоречия ПОС / ПЗ / сборники / модель / расценки перечня (LLM + жёсткие правила) |
| 10 | `BOOK_MATERIALS` | материалы, не входящие в расценки по сборнику ТСН (`tsn_coeffs.book_rate`) → пометка в строке или «добавьте в перечень» |
| 11 | `REBAR_STEEL` | арматурная сталь отдельными строками **по классам и диаметрам из ВОР** (`1.3-4-1 … 1.3-4-27`, цена из `local_resources`; см. §5.8.1, п. 4). Если ВОР по виду конструкции нет — одна строка `1.3-4-22` (Ø12–14 мм, класс по модели, иначе А500С) с пометкой «уточнить по спецификации КР» |
| 12 | `PD_MODE` | при `pd_mode=off` значения ПОС/ПЗ в расчёт не подставляются |
| 13 | `RATE_FIX` | проверка «расценка из чужого сборника подходит к работе» с заменой через LLM (`pd_full.ask`) |
| 14 | `DOC_FACTS` | факты из ВОР/ПД (§5.8.1): класс бетона B/W/F по конструкциям (когда в модели его нет), класс арматуры, **масса арматуры по конструкции, классу и диаметру** из ВОР (установка + сталь по диаметрам), объёмы работ «по проекту» из ВОР (раскладка по группам), сверка объёмов модель ↔ ВОР и тезисы «Из ВОР»/«Из ПД» в справку |
| 15 | `CONDITIONAL_WORKS` | работы «не моделируется» (гидрошпонка, штуцер инъекционный, набухающий профиль, уплотнители швов, закладные детали) остаются только при подтверждении документом сессии (IFC/ПОС/ВОР/ПД); без подтверждения строка и её ресурсы удаляются (§5.8.2) |
| 16 | `MIX_NORMS` | объём бетонной смеси приводится к норме расхода `koefs.xlsx` (`works_cost._get_corrected_volume`) по строкам каждого элемента: база — объём элемента (`_material_base`), после корректировки пересчитываются МР/стоимость ресурса; строки «объём по проекту» и объёмы, уже содержащие норму расхода, не меняются |

Обёртка `MIX_NORMS` (`finalize_rows` в конце `perechen_pipeline.py`) —
самая внешняя в цепочке: она выполняется последней, уже поверх всех блоков выше,
и применяет к строкам каждого элемента ту же корректировку по `koefs.xlsx`, что
и в КР. Модуль импортирует `pandas` локально внутри функций (общий `pd` на
уровне модуля не объявлен), поэтому обёртка импортирует его сама.

Отладка: `python -m src.services.perechen_kb list` (структура перечня),
`python -m src.services.perechen_kb [N]` (тест подбора на последнем запуске),
`python -m src.services.kb_inspect` / `kb_model` / `kb_etalon` (вспомогательные
инструменты проверки базы знаний и эталона `data/etalon.xlsx`).

#### 5.8.1. Факты из ВОР и ПД — `doc_facts.py`

Модуль [`src/services/doc_facts.py`](./src/services/doc_facts.py) читает
результаты разбора дополнительных документов (ВОР и ПД, §4.1) из корня сессии и
даёт подбору по перечню то, чего нет в модели. Встроен в `perechen_pipeline`
блоком `DOC_FACTS` (обёртки над `_concrete_resource`, `finalize_rows`,
`_rebar_class`, `_an_build`); если документов нет, `load()` возвращает `None` и
подбор работает как раньше.

Сопоставление записи документа с элементом/группой — семантическое:
`primary_kind` (вид конструкции по ключевым словам: фундаментная плита, свая,
фундамент, лестница, стена, колонна, балка, перекрытие, кровля, утеплитель,
гидроизоляция, кладка), часть здания (`part_of`), пересечение «стеммов»
наименования; записи МАФ/благоустройства/инженерных сетей и плиты башенного
крана (БК) отсекаются (`_NOISE_RX`, `_BK_RX`). При равных оценках кандидатов с
разными классами бетона выбор делает локальная LLM (как в `pd_full`/`tsn_coeffs`),
решения кешируются в `data/doc_facts_cache.json` (`DOC_FACTS_CACHE_PATH`).

Что берётся из документов:

1. **Класс бетона `(B, W, F)`** — `Facts.concrete_bwf(el, part)`:
   приоритет — ВОР (класс прямо в наименовании конструкции), затем ПД
   (`concrete_class`/`frost_resistance`/`water_resistance`); недостающие W/F
   добираются из другой записи того же класса прочности. Используется в
   `_concrete_resource`, когда в модели класса нет (`_bwf` по имени/материалу
   пуст): марка смеси выбирается по классу прочности и W/F (та же логика, что в
   блоке (6) §5.8); дополнительно `finalize_rows` отсекает строки бетонной смеси
   чужого класса (`_doc_filter_concrete`), если класса в модели нет.
2. **Класс арматуры** — `Facts.rebar_class()` («Арматура класса А500С» из ВОР,
   «Арматура A500c» из ПД); подставляется в `_rebar_class` (блок `REBAR_STEEL`),
   если модель класс не дала.
3. **Объёмы работ, которых нет в модели** — `Facts.volume_for(work, unit, el)`:
   для строк «объём по проекту» (`finalize_rows`, работы с пометкой
   «не моделируется»: гидрошпонка, уплотнители/герметизация швов, опалубка торца
   плиты, засыпка и т.п.) объём берётся из ВОР с приведением к единице расценки
   (`_doc_fill_volumes`), стоимость пересчитывается; ресурсы без объёма —
   по норме расхода своей расценки.

   Сопоставление **мягкое**, потому что строка перечня сметчиков и запись ВОР
   почти никогда не совпадают дословно (расценка «Заполнение вертикальных швов
   стеновых панелей упругими прокладками» ↔ ВОР «Гидрошпонка АКВАСТОП…»).
   Учитываются:
   * единица измерения (обязательное совпадение; распознаются единицы с
     множителем — «100 м», «100 м2», «100 м3», «1 т», «10 шт»);
   * наименование **работы** (`work` строки, а не наименование расценки);
   * совпадение вида конструкции (`primary_kind`) и части здания;
   * общие слова наименования — достаточно **одного специфичного** (редкого в
     ВОР) слова, напр. «гидрош» для гидрошпонки; частые слова («бетон», «работ»)
     сами по себе совпадением не считаются;
   * цитата записи ВОР (в ВОР конструкция часто указана только в ней:
     «Уточнение объемов арматуры ФП и …»).

   Объём в ВОР относится к конструкции **целиком** (61,6 пог.м гидрошпонки на всю
   фундаментную плиту), а в перечне конструкция представлена несколькими группами
   (по классу бетона), поэтому найденный объём раскладывается по группам
   пропорционально объёму бетона конструкции в модели (`_doc_share`) — иначе он
   задваивался бы в каждой группе.
4. **Масса арматуры по конструкции, классу и диаметру** (FIX-48) —
   `Facts.rebar_by_diameter(kind)`
   + `_doc_prepare_rebar` / `_doc_fill_rebar`: масса арматуры берётся из ВОР
   **детерминированно** — `vor_rebar_rows` разбирает подпозиции-диаметры ВОР
   («D10», «D20», …) и берёт класс из родительской позиции («Арматура класса
   А500С» / «… А240»), вид конструкции — из раздела ведомости («Монолитные ж/б
   конструкции. Фундаменты / Стены … / Плиты перекрытия …»), часть здания — из
   раздела. Масса **суммируется по всем блокам/захваткам и секциям/этажам**
   раздела (ВОР разбита по секциям — это учитывается), записи БК/МАФ
   отсекаются (`_NOISE_RX`, `_BK_RX`), объём берётся из колонки «стало»
   (`volume_now`, корректировка ПД), сборный ж/б исключается.

   Полученный расход `{(класс, диаметр): т}` делится на суммарный объём бетона
   конструкций этого вида в модели (`_DOC_REBAR_NORM`, `_DOC_REBAR_BY_D`) —
   так объёмы групп складываются ровно в расход по ВОР. `_doc_fill_rebar`
   подставляет в `q["rebar_kg"]` суммарную массу (объём установки арматуры
   `3.6-72-3` / `3.6-76-3` / `3.6-92-5` и т. п.), а в `q["rebar_by_diameter"]` —
   массу по каждому классу и диаметру. Блок `REBAR_STEEL` (§5.8, блок 11) по
   этой разбивке добавляет **отдельные работы арматурной стали по диаметрам**:
   класс и диаметр сопоставляются с работами перечня
   (`perechen_kb.rebar_diameter_map()`: Ø10 → 1.3-4-21, Ø12–14 → 1.3-4-22,
   Ø16–18 → 1.3-4-23, Ø20–22 → 1.3-4-24, Ø25–28 → 1.3-4-25, Ø32 → 1.3-4-26,
   Ø36–40 → 1.3-4-27; А240: Ø6–7 → 1.3-4-1, Ø8 → 1.3-4-2, Ø12 → 1.3-4-4,
   Ø16–18 → 1.3-4-5), работы одного диаметра объединяются в одну строку, объём —
   масса ВОР, цена — из `local_resources`.

   Если расхода по виду конструкции в ВОР нет (или ВОР не загружен), остаётся
   прежнее поведение: норма модели/типовая (`REBAR_NORM`) и одна строка стали
   `1.3-4-22` с пометкой «цена принята для Ø12–14 мм, уточнить по спецификации
   КР». `primary_kind` распознаёт «лестницы» и «площадки» как вид `stair` —
   поэтому арматура лестниц и лестничных площадок берётся из разделов ВОР
   «Лестницы …» и «Лестничные марши и промежуточные площадки».
5. **Сверка объёмов модель ↔ ВОР** — `Facts.beton_volume(el)`: по каждой группе
   объём бетона модели сравнивается с суммой объёмов ВОР по тому же виду
   конструкции; расхождение ≥ 25 % выводится в `analytic.json` (группа
   «Сверка с ВОР и ПД», поле `doc_volumes`) как «требует проверки» — в ВОР
   учтены все захватки/корпуса, а в расчёте — выбранные элементы.

В `analytic.json` помимо группы «Сверка с ВОР и ПД» (что именно взято из
документов: класс бетона — N типов, объёмы — M позиций, масса арматуры — K типов,
класс арматуры) появились **тезисные группы «Из ВОР» и «Из ПД»**: расход
арматуры по конструкциям (т и кг/м³, **с разбивкой по классам и диаметрам**),
количество объёмов, взятых из ВОР, число записей ВОР и классы бетона
конструкций по ВОР/ПД. Отдельный тезис группы «Из ВОР» сообщает раскладку
массы арматуры ВОР по работам перечня («1.3-4-21 — 508,5 т; 1.3-4-22 —
2 008,6 т; …», §5.8.1 п. 4).

**Сводка для веб-интерфейса** — `Facts.summary_items()` и `facts_items(session_dir)`
(эндпоинт `GET /api/session/<id>/doc_facts`): тезисная сводка ВОР/ПД — записи ВОР
с объёмом агрегируются по наименованию (одна строка на материал с суммарным
объёмом, напр. «Гидрошпонка АКВАСТОП… — 61,6 пог.м (ВОР, стр. 15)»), записи ПД —
по классам бетона конструкций («B30 W8 F150»). Отображается в интерфейсе в блоке
«Всё найденное в ВОР и ПД» (§7).

Отладка: `python -m src.services.doc_facts <папка_сессии>` — список классов
бетона/арматуры по документам и примеры сопоставления по конструкциям.

#### 5.8.2. Условные работы «не моделируется» — блок `CONDITIONAL_WORKS`

Перечень сметчиков добавляет к бетонной конструкции работы, которых нет в модели
(`perechen_kb`, правило «не моделируется»: гидрошпонка, штуцер инъекционный,
набухающий каучуковый профиль, уплотнители/герметизация швов, закладные детали).
Раньше они попадали в перечень **всегда** — строкой-заглушкой «объём по проекту»,
даже если в проекте их нет. Теперь такая работа остаётся, **только если её наличие
подтверждено документом сессии** (IFC-модель, ПОС, ВОР, ПД):

1. найден объём работы в ВОР (`Facts.volume_for`, §5.8.1) — работа подтверждена,
   объём подставлен;
2. иначе ищется упоминание работы в документах по характерным словам
   (`doc_facts.confirm_keys`) — тогда строка остаётся с пометкой источника
   («— по документам (ВОР, стр. 15)»);
3. если работа не найдена ни в одном документе — строка **и связанные с ней
   ресурсы удаляются** (напр. «Штуцер из ПВХ контрольно-инъекционный» вместе с
   работой «Штуцер инъекционный»).

Характерные слова заданы категориями (`_COND_KEYS`): `шпонк`/`аквастоп` —
гидрошпонка, `штуцер`/`инъекц` — инъекционный штуцер, `набуха`/`каучук` —
набухающий профиль, `уплотнит`/`вилатерм`/`гермет` — уплотнители швов, `закладн` —
закладные детали. Обязательные работы бетонной конструкции (опалубка, арматура,
бетонирование, уход за бетоном, бетонная смесь) не фильтруются (`_ALWAYS_RX`).

Корпус документов для поиска подтверждений (`doc_facts._corpus`, кешируется по
каталогу): ВОР, ПД, ПОС/ПЗ (`*_глобальные_константы.json`), полный разбор ПОС/ПЗ
(`pd_full.json`) и наименования элементов модели (`filtered_elements.xlsx`,
`Подобранные_таблицы_работ.json`).

Результат попадает в `analytic.json`:
- группа «Из проектной документации» (`ok`) — работы, подтверждённые проектом и
  оставленные в перечне;
- группа «Из проектной документации» (`info`) — работы, не найденные ни в
  IFC/ПОС, ни в ВОР/ПД и потому исключённые из перечня (с числом позиций).

Проверка на сессии `8895e79e` (ФП): удалены «Установка закладных деталей»,
«Штуцер инъекционный» (и ресурс 1.1-1-3749), «Набухающий каучуковый профиль» —
в документах их нет; гидрошпонка оставлена (ВОР, 61,6 пог.м → 0,597 в единице
расценки «100 м»).

#### 5.9. Поправочные коэффициенты сборников ТСН — `tsn_coeffs.py`
- Сборники лежат в `data/tsn/*.json` — результат разбора PDF (`src/tools/tsn/`,
  скрипт `tsn_add.sh`, §10); `TSN_DIR` — каталог сборников.
- `docs()` — список загруженных сборников для UI (`GET /api/tsn/docs`).
- `_load()` — индексация: коэффициенты (§3), техническая часть, состав норм,
  ссылки на таблицы.
- `decide(pressmark, rate_title, el, q, extra)` — по каждой подобранной расценке
  находит применимые пункты коэффициентов, собирает «факты» об элементе
  (материал, толщина, высота, признаки геометрии из `ifc_features`) и просит LLM
  (`pd_full.ask`) решить: **да / нет / неясно**; решения кешируются
  (`data/tsn_decisions_cache.json`).
- `book_rate(pressmark)` — состав нормы сборника: какие материалы не входят в
  расценку и нужны отдельными строками (блок `BOOK_MATERIALS`, §5.8).
- `tech_for` / `tables_of` — техническая часть и таблицы сборников.

#### 5.10. Полный разбор ПОС/ПЗ — `pd_full.py`

`GET /api/session/<id>/pd_full` (ленивый фоновый запуск; UI опрашивает каждые 3 с,
статусы `running/ready/none`) → `pd_full.extract(pos, pz)` →
`outputs/<session_id>/pd_full.json` (≈40 параметров с полями
`group/id/title/value/src/page/quote/use`; `use` = `calc` / `other` / `note:…` /
`info`). PDF ПОС/ПЗ ищутся в папке сессии автоматически (`_pd_find_docs` по
именам «ПОС»/«ПЗ»/«пояснит»). LLM — `LLM_BASE_URL` + `LLM_MODEL` (Ollama), кеш —
`data/pd_full_cache_v2.json` (`PD_FULL_CACHE_PATH`). Результат используется
подбором по перечню (блок `PD_FULL`, §5.8), поправками ТСН (§5.9) и справкой
(§5.12). Отладка: `python -m src.services.pd_full [ПОС.pdf] [ПЗ.pdf]`.

#### 5.11. Признаки геометрии модели — `ifc_features.py`

Считает по типу элемента (`ObjectType`) из IFC: криволинейность и радиус контура,
наклон (max угол), `z_min/z_max` (отметки от ±0.000), высоту этажа, переменное
сечение. Запуск: `python -m src.services.ifc_features [папка_сессии]` →
`<сессия>/ifc_features.json`. `perechen_pipeline` читает файл, если он есть
(в автоматическом пайплайне не создаётся — только CLI); без него проверка
поправок ТСН работает без геометрических признаков.

#### 5.12. Аналитическая справка и переключатели UI

- **`analytic.json`** (в `run_<NNN>/`) — справка по последнему расчёту: что учтено
  (`groups.ok`), что требует проверки (`groups.doubt`), что не вошло в перечень
  (`info`), противоречия между документами; метаданные `elements`/`rows`/
  `with_pd`/`with_tsn`; группа «Сверка с ВОР и ПД» (`doc_facts`, §5.8.1) — что
  взято из ВОР/ПД и расхождения объёмов модель ↔ ВОР (поле `doc_volumes`);
  тезисные группы «Из ВОР» и «Из ПД» — расход арматуры по конструкциям,
  объёмы работ из ВОР, классы бетона конструкций; группа «Из проектной
  документации» (`CONDITIONAL_WORKS`, §5.8.2) — работы «не моделируется»,
  оставленные по подтверждению проектом (`ok`) и исключённые как не найденные
  ни в IFC/ПОС, ни в ВОР/ПД (`info`).
  Отдаётся эндпоинтом `GET /api/session/<id>/analytic`
  (последний по времени `analytic.json` из сессии).
- **Сводка фактов ВОР/ПД**: `GET /api/session/<id>/doc_facts` (`doc_facts.facts_items`,
  §5.8.1) — тезисный список: наименование, суммарный объём/класс бетона, источник,
  страница и цитата. Строится по уже разобранным `ВОР_ведомость_объемов_работ.json`
  и `ПД_характеристики_элементов.json` (без обращения к LLM).
- **Переключатели** (сохраняются в корне сессии): `tsn_mode.json` —
  `GET /api/session/<id>/tsn_mode?set=on|off` («поправки из сборников ТСН:
  учитывать / не учитывать»); `pd_mode.json` —
  `GET /api/session/<id>/pd_mode?set=on|off` («с учётом ПОС и ПЗ / без учёта»).
- **Список сборников ТСН**: `GET /api/tsn/docs`.
- **Панели веб-интерфейса** (только режим ИИ, подключаются скриптами в
  `index.html`): «📄 Параметры для расчёта» + «📑 Всё найденное в ПОС и ПЗ»
  (PD_V5), «📑 Всё найденное в ВОР и ПД» (DOCFACTS_V1; тезисная сводка
  `doc_facts.facts_items`, §5.8.1), «📚 Сборники ТСН» (TSN_UI_V1),
  «📋 Справка по расчёту» (AN_V3), синхронизация режима ПОС/ПЗ на сервере
  (PDMODE_V1). Кнопка «✅ Рассчитать все элементы», кнопка повторного запуска
  (`new_run`) после завершения обработки.

#### 5.13. Сравнение работ и объёмов ВОР заказчика и сформированного перечня — `vor_comparison.py`

`src/services/vor_comparison.py` сопоставляет **ведомость объёмов работ
заказчика** (ВОР) и **сформированный перечень работ** (`Финальный_перечень_работ.json`
запуска, §5.5/§5.8): какие работы и в каком объёме заявлены в ВОР и попали в
перечень, где объёмы расходятся, а какие работы присутствуют только в одном из
документов. Сравнение ведётся **по каждой группе элементов и по каждой работе**.

Источник ВОР — **загруженный PDF ВОР** (`vor_*.pdf` в корне сессии), который
разбирается **детерминированно** (`doc_pdf_parser.extract_vor_rows`, §4.1):
читается структура ведомости — разделы («6. Монолитные ж/б конструкции. Стены
подземной части»), блоки/захватки («Деформационный блок №1. Секции 1 и 2»,
«Секция №N») и строки позиций с колонками «было / стало / разница» (для
сравнения берётся **«стало»** — объём с учётом корректировки ПД). Если PDF нет —
используется результат LLM-разбора `ВОР_ведомость_объемов_работ.json` (без
разделов/блоков, источник помечается как `json`).

**Группа элементов** — канонический **вид конструкции** (`element_group`),
совпадающий с МССК-группами перечня (`path[1]`) и разделами ВОР: Стены, Плиты
перекрытия / покрытия, Лестничные марши, Лестничные площадки, Лестницы, Балки,
Колонны, Фундаментная плита, Фундаменты, Приямок, Парапеты,
Термовкладыши / утепление, Сваи, Металлоконструкции, а также отдельные группы
«Прочее (БК)» (фундаменты башенного крана) и «Прочее (МАФ)» (детские/спортивные
площадки). Для перечня вид берётся из МССК-группы (`path[1]`), а при её
отсутствии — из наименования элемента; для ВОР — из наименования строки, а если
вид в наименовании не указан (арматура, опалубка, холодный шов и т.п.) —
**наследуется от ближайшей выше строки-конструкции** (верхнеуровневой позиции
вида «1»/«5») в пределах блока/захватки (категории `APPLIED_CATEGORIES`); у
самостоятельных работ (гидроизоляция, засыпка, грунт, утепление) вид берётся из
заголовка раздела.

**Разбивка ВОР по секциям и этажам** приводится к уровню **части здания**
(`_vor_part`: Подземная / Цоколь / Надземная) — так же, как сгруппирован
сформированный перечень. Сравнение строится в двух разрезах: по ключу «часть
здания · вид конструкции → работа → единица» (лист «Сравнение по группам») и по
ключу «вид конструкции → работа → единица» без части здания (лист «По видам
конструкций»).

Что делает модуль:

- **приводит единицы измерения к базовым** (`м2`/`м3`/`т`/`м`/`шт`): в ВОР
  объёмы в натуральных единицах, а в перечне — в единицах расценки с множителем
  («100 м2», «100 м3», «1 Т», «10 фасонных частей»); объём перечня умножается на
  множитель единицы расценки (напр. «100 м2» → × 100). Разбор — `base_unit`;
- **формирует группы элементов** по каноническому виду конструкции
  (`element_group`, см. выше) — так лестничные марши, площадки, балки и стены,
  названные в ВОР и в перечне по-разному, сопоставляются в одной группе;
- **агрегирует объёмы только по листовым позициям** (`_leaf_rows`): родительская
  позиция ВОР («2 Арматура класса А500С») равна сумме подпозиций («2.1 D10» …
  «2.4 D28»), поэтому суммирование всех строк удвоило бы объём;
- **классифицирует работы по категориям** по ключевым словам
  (`WORK_CATEGORIES`: опалубка, армирование, бетонирование, уход за бетоном,
  гидрошпонка, гидроизоляция, закладные, засыпка, грунт, утепление, лестницы и
  т.д.);
- **арматура берётся из ВОР и не сравнивается**: арматура не моделируется в IFC,
  поэтому для категории `rebar` Δ не считается, строка получает статус
  «взято из ВОР» (объём перечня приводится справочно — теперь это сумма строк
  стали по классам и диаметрам, §5.8.1 п. 4), а при отсутствии арматуры
  в ВОР — «нет в ВОР (оценка модели)»;
- **работы и ресурсы сравниваются раздельно**: работы (расценки) — на основных
  листах, ресурсы/материалы перечня (`is_resource`) — на отдельном листе
  «Ресурсы (материалы)», чтобы объём бетона/арматуры не учитывался дважды;
- **сопоставляет** объёмы по ключу «группа элементов → категория работы → базовая
  единица» (лист «Сравнение по группам»), дополнительно по ключу «вид конструкции
  → категория → единица» (лист «По видам конструкций») и по ресурсам (лист
  «Ресурсы (материалы)»), считает расхождение;
- **пишет** `Сравнение_ВОР_и_перечня.xlsx` (листы «Сравнение по группам»,
  «По видам конструкций», «Ресурсы (материалы)» — метка, работа, ед. изм.,
  объёмы ВОР и перечня, Δ, Δ %, статус, источник объёма, позиции ВОР и группы
  перечня; «Детализация ВОР» — строки ВОР с разделом, блоком, частью здания и
  группой; «Детализация перечня» — работы и ресурсы перечня) и
  `Сравнение_ВОР_и_перечня.json` в папку запуска `run_<NNN>/`. Подсветка строк:
  **красный** — работа только в ВОР или только в перечне; **жёлтый** — расхождение
  объёмов аномально большое (`|Δ %| ≥ DIFF_PCT` = 20 % при `|Δ| ≥ DIFF_ABS` = 1);
  **голубой** — арматура («взято из ВОР»).


Запуск:

- автоматически в конце запуска АР (`session_manager._run_processing_pipeline_in_run`),
  если ВОР загружен и разобран (шаг вспомогательный: ошибка не ломает запуск);
  файл `Сравнение_ВОР_и_перечня.xlsx` входит в итоговые файлы запуска и доступен
  для скачивания;
- по запросу: `GET /api/session/<id>/compare_vor` (строит/читает сравнение
  последнего запуска, `?rebuild=1` — пересобрать; возвращает строки, сводку и
  `download_url`), `GET /api/session/<id>/compare_vor` без ВОР/перечня отдаёт
  `status=none`;
- CLI: `python -m src.services.vor_comparison <каталог_сессии> [каталог_запуска]`.

---

### 6. Сводка по модулям (порядок работы)

| # | Модуль | Когда | Что делает |
|---|---|---|---|
| 1 | `session_manager.py` | всегда | Оркестратор: сессии, `sessions.json`, фоновые потоки, прогресс 0–100, runs |
| 2 | `ifc_raw_dump.py` | этап 0 | Сырой дамп всех свойств/QTO IFC → `IFC_исходные_параметры.xlsx/.json` |
| 3 | `zero_step.py` | этап 0 | Извлечение элементов по `ELEMENT_TYPES_AR`, нормализация, QTO/bbox, `ДЛЯ_СМЕТЧИКА_исправленный.xlsx`, `height.txt`, `IFC_глобальные_константы.json` (высоты + `building_storeys_type` — этажность по колонке «Этаж»; `detect_storeys_type_from_session` — чтение этажности из файлов сессии для UI/подбора), лист `Высота_здания` |
| 3а | `selection_template_builder.py` | этап 0 | Заполнение универсального шаблона параметров подбора (`data/selection_parameters.json` + карта `data/selection_parameters_mapping.json`) по каждому элементу сырого дампа → `Параметры_подбора_элементов.json`. После разбора ПОС/ПЗ — **точечное обновление** констант (`update_selection_parameters_constants`: раздел `constants` + параметры-константы элементов, без перечитывания сырого дампа) |
| — | `ifc_reference_builder.py`, `position_links.py` | **не используется в АР** | JSON-справочники и ссылки на позиции цифрового сборника — функциональность режима КР; в АР-конвейере не вызываются (см. §3.3) |
| 6 | `pdf_processor.py` + `ai-blueprint-to-ifc/` | этап 0 (для PDF) | Пайплайн машинного зрения: стены (YOLO OBB), штриховки (DINO+VLM), легенды → те же Excel/JSON |
| 7 | `pd_parser.py` | опционально | Разбор PDF ПОС и пояснительной записки ПЗ через LLM (один и тот же пайплайн) → `ПОС_глобальные_константы.json` / `ПЗ_глобальные_константы.json`. Семафор `LLM_MAX_CONCURRENCY` на обращения к Ollama; пропуск LLM для констант, где шаблон уже дал однозначное значение (`FIRST_MATCH_MAPPERS`); кэш разбора по SHA-256 содержимого (`doc_cache_*`, `DOC_CACHE_DIR`) |
| 7а | `doc_pdf_parser.py` | опционально | Разбор PDF ВОР и ПД через LLM → `ВОР_ведомость_объемов_работ.json/.xlsx`, `ПД_характеристики_элементов.json/.xlsx`. Порции обрабатываются параллельно (`_parse_batches`, до `LLM_MAX_CONCURRENCY`), результат кэшируется по SHA-256 содержимого. Дополнительно — **детерминированный разбор структуры ВОР** (`extract_vor_rows`): разделы, блоки/захватки, позиции, объёмы «стало» (используется сравнением с перечнем, §5.13) |
| 8 | `materials_lookup.py` | этап 1 | Карта МССК-кодов материалов (`materials_mssk_nested.json`), разбор строк «Имя (СТ xx xx xx)», `extract_material_value` (IfcMaterialLayer / MGE-пары); `material_code_options` — дочерние коды для **неполного** кода материала (напр. «СТ 00 15» → сборный/монолитный) |
| 9 | `mssk_lookup.py` | этап 1 | Карта МССК-кодов элементов (`elements_mssk_nested.json`), названия групп L1 |
| 10 | `group_excel.py` | этап 1 | `process_ifc_excel_ar` → `group_elements_mssk`: Часть здания → МССК → Материал → IFC-тип → Геометрия → Материал/Бетон (часть здания — строго по числовому индикатору «Этажа»); деревья и `ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx`; `material_code_overrides` — уточнение неполных МССК-кодов материала из `rowMaterialCodes` |
| 11 | `works_table_selector.py` | этап 1 | Детерминированный подбор таблиц ГЭСН/ТСН-2001 (8 шагов `data/algorithm.md`) → `Подобранные_таблицы_работ.json` |
| 11а | `works_fetcher.py` | этап 1 | Цифровой сборник (larix): актуальный период ТСН → `period.json` (корень сессии); работы по шифрам подобранных таблиц → `Подобранные_работы.json` (в `run_<NNN>/`) |
| 11б | `works_final_selector.py` | этап 1 | Финальный подбор работ через LLM (`pd_parser.LLMClient`, Ollama), v13.2: единица обработки — листовая группа из `filtered_elements_grouped_AR.json`, **разделённая по частям здания** (`split_leaf_groups_by_part` из КР: часть — **строго по числовому индикатору «Этажа»**, FIX-46; сборный ж/б — на общих основаниях, FIX-47); по **каждой таблице** элемента-представителя работы из `Подобранные_работы.json` предфильтруются (анти-слова, «семейные» фильтры `_classify_family`/`FAMILY_FILTERS`, часть здания, высота, геометрия — с каскадным fallback), дедуплицируются и ранжируются; LLM выбирает **ровно одну работу в каждой таблице** (`{"selected": [{table, pressmark, reason}]}`), при пропуске таблицы — детерминированный fallback `_pick_closest` (ближайшая по толщине); заголовки групп и секции частей здания — в формате КР; **объёмы работ — по правилам КР** из агрегатов группы (объём/площади/длина швов/площадь опалубки/расход арматуры; опалубка плитных групп — только по площади опалубки, арматура — только «отдельные стержни») с корректировкой по `koefs.xlsx`; разбивка стоимости выбранных работ (ЗП/ЭМ/МР) — из детальных параметров позиций `catalog/work-process/detail` (`works_fetcher.fetch_work_details`, §5.4) → `Финальный_перечень_работ.json` + `.xlsx` (в `run_<NNN>/`; xlsx — в структуре режима КР, `build_final_works_xlsx`) |
| 11в | `perechen_kb.py` + `perechen_pipeline.py` | этап 1 (`WORKS_SELECTOR=perechen`) | Финальный подбор по **перечню сметчиков** `data/perechen_kr.xlsx` (LLM выбирает подраздел/работы/расценки, контролёр) + локальный справочник расценок → `Финальный_перечень_работ.json/.xlsx` + `analytic.json` (§5.8) |
| 11г | `local_works.py` / `local_resources.py` | этап 1 (`WORKS_SELECTOR=perechen`) | Локальный справочник расценок/ресурсов из `data/price_cost.xlsx` (кеши `data/local_works.json`, `data/local_resources.json`, `data/local_norms.json`) — замена цифрового сборника larix |
| 11д | `tsn_coeffs.py` | этап 1 (`WORKS_SELECTOR=perechen`) | Поправочные коэффициенты из сборников `data/tsn/*.json`: `decide` (LLM: да/нет/неясно), `book_rate` (не учтённые материалы), `docs` (§5.9) |
| 11е | `pd_full.py` | опционально | Полный разбор ПОС/ПЗ (≈40 параметров) → `pd_full.json`; эндпоинт `GET /api/session/<id>/pd_full` (§5.10) |
| 11ж | `ifc_features.py` | опционально (CLI) | Признаки геометрии IFC (криволинейность/радиус, наклон, отметки, высота этажа, переменное сечение) → `<сессия>/ifc_features.json` (§5.11) |
| 11з | `doc_facts.py` | этап 1 (`WORKS_SELECTOR=perechen`) | Факты из ВОР/ПД (§5.8.1): класс бетона B/W/F по конструкциям (когда в модели его нет), класс арматуры, **масса арматуры по конструкции, классу и диаметру** из ВОР (`vor_rebar_rows`/`Facts.rebar_by_diameter`, установка + сталь по диаметрам), объёмы работ «по проекту» из ВОР (раскладка по группам), сверка объёмов модель ↔ ВОР и тезисы «Из ВОР»/«Из ПД» → `analytic.json`; сводка для UI — `GET /api/session/<id>/doc_facts`; кеш LLM-решений `data/doc_facts_cache.json`; подтверждение условных работ «не моделируется» (`_corpus`/`confirm_keys`, §5.8.2) |
| 11и | `perechen_pipeline.py` (`CONDITIONAL_WORKS`) | этап 1 (`WORKS_SELECTOR=perechen`) | Работы «не моделируется» (гидрошпонка, штуцер инъекционный, набухающий профиль, уплотнители швов, закладные) остаются только при подтверждении документом сессии (IFC/ПОС/ВОР/ПД), иначе строка и её ресурсы удаляются (§5.8.2) |
| 11к | `vor_comparison.py` | этап 1 (если ВОР загружен) | Сравнение работ и объёмов ВОР заказчика и сформированного перечня (`Финальный_перечень_работ.json`): детерминированный разбор PDF ВОР (`doc_pdf_parser.extract_vor_rows`: разделы, блоки/захватки, объёмы «стало»; fallback — `ВОР_ведомость_объемов_работ.json`), приведение единиц к базовым, **сравнение по каждой группе элементов и каждой работе** (группа — канонический вид конструкции `element_group`; ВОР разбит по секциям/этажам — приводится к части здания; отдельные листы по группам, видам конструкций и ресурсам; арматура не сравнивается — «взято из ВОР»), агрегация объёмов по листовым позициям, подсветка расхождений → `Сравнение_ВОР_и_перечня.xlsx/.json` (в `run_<NNN>/`); эндпоинт `GET /api/session/<id>/compare_vor` (§5.13) |
| 12 | `ifc_json_builder.py` | этап 1 | Финальный `final_result_AR.json`; `enrich_group_levels` — восстановление этажей групп по GUID из `filtered_elements.xlsx` (используется сравнением ВОР, §15) |
| 13 | `comparison_manager.py` | режим «Сравнение ВОР» | Оркестратор comparison-сессий (`session_type='comparison'`): обработка нескольких моделей/чертежей (ЦС/ИИ), разбор документов, параметры, разбор ВОР заказчика и сравнение → `models.json`/`estimates.json`/`result.json`/`result.xlsx` (§15) |
| 14 | `comparison_documents.py` | режим «Сравнение ВОР» | Общие константы сравнения: `DOC_FILENAMES`, `resolve_constants` (приоритет IFC → ПОС → ПЗ → по умолчанию + ручные переопределения), `material_choices` (уточнение неполных МССК-кодов материалов) (§15) |
| 15 | `vor_parser.py` | режим «Сравнение ВОР» | Локальный парсер ВОР заказчика (PDF с текстовым слоем / XLSX) по заголовкам колонок: позиции, единицы, объём («стало»/«после корректировки»/«по проекту»), разделы, выбор столбца объёма при неоднозначности (`QuantityColumnRequired`) (§15) |
| 16 | `works_comparison/` | режим «Сравнение ВОР» | Пакет сравнения позиций (извлечение, нормализация единиц, группировка, сопоставление по шифру/семантике/конструкциям, LLM-валидация, формирование отчёта) — подключён к конвейеру через `comparison_manager` (§15) |
| 17 | `comparison_vor.py` | режим «Сравнение ВОР» | Компактный отчёт сравнения (`prepare_report`, `reportVersion=3`): `vorRows` (иерархия часть → раздел → подраздел → группа/работа), `vorSummary`, `matchingStatistics`, `comparisonStatistics` (§15) |
| — | `api_works_lookup.py`, `works_cost.py`, `serializer.py` | вне АР-подбора | КР (API ТСН), расчёт/форматирование стоимости перечня (КР), 3D-GLB по запросу |

Примечание: прежний 4-этапный LLM-пайплайн АР (`first_etap.py`, `second_etap.py`,
`third_etap.py`, `fourth_etap.py`, `base_knowledge.py`, `geometry_filter.py`,
`validation_processor.py`, промпт `prompts/element_analyze.txt`, справочник
`data/perechen_ar.xlsx` и переменная `AR_DOCUMENTS_PATH`) **выведен из пайплайна**
(файлы-модули сохранены как legacy и нигде не импортируются) — подбор работ в АР
полностью заменён детерминированным `works_table_selector`.

Примечание: пакет [`src/services/works_comparison/`](./src/services/works_comparison/)
(LLM-валидация и сравнение позиций, промпты `prompts/validate_material.txt`,
`prompts/validate_work.txt`) **подключён к сервису** через режим «Сравнение
ведомостей объёмов работ» (`comparison_manager`, §15); ранее запускался только
вручную.

---

### 7. Справочники `data/` и их роль в режиме ИИ (АР)

| Файл | Кто читает | Роль |
|---|---|---|
| `algorithm.md` | `works_table_selector.py` (документально) | Текстовое описание 8-шагового алгоритма подбора работ — «спецификация» модуля |
| `ifc_to_collections.json` | `works_table_selector._ifc_collections` | ~20 правил `IfcClass (+PredefinedType) → сборники-кандидаты` (шаг 2) |
| `msck_elements_compact.json` | `works_table_selector._mssk_tree` | Дерево из 951 узлов МССК элементов (`ЭЛ …`); даёт контекст элемента и уточняет подбор (шаг 3); в Сб. 11 различает слои пирога пола |
| `tree_work_compact.json` | `works_table_selector._tree` / `_table_index` | Дерево 41 сборника ГЭСН/ТСН-2001 с названиями таблиц; строится индекс «код таблицы → название/сборник/отдел/раздел» для поиска по ключевым словам (шаг 5) |
| `works_classification.json` | `works_table_selector._classification`, `pd_parser` | Классификация сборников: `work_type` (COMPLEX/SEPARATE/«зависит от опалубки»), отделы/пакеты, **схема 10 глобальных констант** (`global_constants`: опалубка, высота здания, сезон ухода за бетоном, схема бетонирования, высота этажа, этажность здания, тип крана, грузоподъёмность крана, вместимость ковша, мощность оборудования) — отдаётся фронтенду через `GET /works_constants` (шаги 6–7) |
| `params_registry.json` | `pd_parser` | Реестр параметров ПОС (тип `pos_global_constants`) — какие константы и по каким правилам извлекать из пояснительной записки LLM (включая группу грунтов и расстояние перемещения грунта — вне схемы `works_classification.json`) |
| `selection_parameters.json` | `selection_template_builder.py` | Перечень параметров подбора работ из 3 главы `perechen_kr_1.xlsx` (геометрия, положение в здании, технология, оборудование) — «шаблон» для заполнения по каждому элементу |
| `selection_parameters_mapping.json` | `selection_template_builder.py` | Расширяемая карта соответствия «параметр шаблона → ключи сырого дампа IFC (`raw_keys`), ключевые слова (`keywords`), вычисления (`computed`), константы (`constant_names`)»; дополняется вручную при появлении новых имён свойств в моделях. Единица параметра берётся из поля `unit` карты (у `depth` исправлена на «мм» — QTO Depth хранится в мм; при подаче в LLM нормализуется к метрам, §5.5) |
| `elements_mssk_nested.json` | `mssk_lookup.build_mssk_lookup` | Иерархический справочник МССК-элементов; плоская карта «код → {имя, порядок}» для группировки L2 в `group_elements_mssk` |
| `materials_mssk_nested.json` | `materials_lookup.build_materials_lookup` / `build_material_children_map` | Иерархический справочник МССК-материалов (`СТ …`); определяет группу материала L2 («Железобетон монолитный», «Многослойные», «Прочее»); дочерние коды (`material_code_options`) — варианты уточнения **неполного** кода (напр. «СТ 00 15» → сборный/монолитный) |
| `koefs.xlsx`, `price_cost.xlsx` | `api_works_lookup` / `works_cost` (КР), `works_final_selector` (АР), `local_works`/`local_resources` (`WORKS_SELECTOR=perechen`) | Нормы расхода и стоимость расценок; `koefs.xlsx` — корректировка объёмов работ в финальном перечне АР (`works_cost._get_corrected_volume`, те же правила, что и в КР, §5.5 п. 3); `price_cost.xlsx` — КР (API ТСН/стоимость) и **локальный справочник расценок/ресурсов** подбора по перечню (§5.8: кеши `local_works.json`, `local_resources.json`, `local_norms.json`) |
| `perechen_kr.xlsx` | `perechen_kb.load_perechen` | **Перечень сметчиков** — база знаний подбора работ подбором по перечню (§5.8) |
| `perechen_kr_1.xlsx` | `selection_template_builder` | Расширенный перечень (3 глава — источник `selection_parameters.json`) |
| `tsn/*.json` (`tsn_3_6_full.json`, `tsn_3_7_full.json`, `tsn_3_15_full.json`, …) | `tsn_coeffs` | Разобранные сборники ТСН (коэффициенты §3, техническая часть, состав норм, ссылки) для поправочных коэффициентов (§5.9); пополняются через `tsn_add.sh` (§10) |
| `kb_cache.json`, `tsn_decisions_cache.json`, `pd_full_cache_v2.json`, `doc_facts_cache.json` | `perechen_kb` / `tsn_coeffs` / `pd_full` / `doc_facts` | Кеши LLM-решений подбора по перечню, поправок ТСН, полного разбора ПОС/ПЗ и сопоставления фактов ВОР/ПД с конструкциями (§5.8.1) |
| `etalon.xlsx` | `kb_etalon` | Эталон для проверки качества подбора (вспомогательный инструмент) |
| `comparison_vor_template.xlsx` | `comparison_vor.prepare_report` | Шаблон стандартной иерархии ВОР (часть здания → раздел → подраздел → работа → шифр) — по нему отчёт сравнения (§15) относит работы к разделам и раскрывает компактный вид `vorRows` |
| `perechen_kr*.xlsx`, `ifc_to_collections.json` (КР-часть), `elements_mssk*.xlsx` | КР / вспомогательно | Используются в режиме КР; `elements_mssk.xlsx` — исходник nested-JSON |

Все файлы `data/` — **только чтение**; результаты пишутся в `outputs/<session_id>/`.

Служебный каталог **`outputs/_doc_cache/`** (`DOC_CACHE_DIR`) — кэш результатов
разбора ПОС/ПЗ/ВОР/ПД по SHA-256 содержимого файла (ключ включает модель и
`DOC_CACHE_VERSION`); лежит вне папок сессий и переиспользуется между сессиями
(повторная загрузка того же документа не тратит время LLM).

---

### 8. Полный список создаваемых файлов (ИИ/АР)

**Корень сессии `outputs/<session_id>/`** (этап 0):

- `IFC_исходные_параметры.xlsx` / `IFC_исходные_параметры.json` — сырой дамп IFC;
- `ДЛЯ_СМЕТЧИКА_исправленный.xlsx` (листы `Данные`, `Сводка_по_типам`, `Высота_здания`) — в `original/`;
- `height.txt` (legacy — читается только при отсутствии `IFC_глобальные_константы.json`);
- `IFC_глобальные_константы.json` — глобальные константы IFC (высоты здания/этажа, отметки);
- `Параметры_подбора_элементов.json` — заполненный шаблон параметров подбора по каждому элементу (сырой дамп + константы проекта); читается `works_final_selector` (этап 1) — параметры элементов группы используются при расчёте объёмов работ (длина швов/периметр, площадь опалубки, толщина), а не в LLM-промпте;
- `ПОС_глобальные_константы.json` (если загружался ПОС);
- `ПЗ_глобальные_константы.json` (если загружалась пояснительная записка ПЗ);
- `legend.json` + `legend_item_N.<ext>` — ручная легенда PDF-чертежа (если PDF загружен с `autoLegend=false`, §3.5); `materials_colors.md` в этом случае строится по ней;
- `period.json` — актуальный период ТСН из цифрового сборника (этап 1, `works_fetcher`; общий для всех запусков сессии);
- `pd_full.json` — полный разбор ПОС/ПЗ (≈40 параметров, §5.10; создаётся лениво при запросе `GET /api/session/<id>/pd_full`);
- `pd_mode.json`, `tsn_mode.json` — переключатели «с учётом ПОС/ПЗ / без» и «поправки ТСН: учитывать / нет» (эндпоинты `/pd_mode`, `/tsn_mode`, §5.12);
- `ifc_features.json` — признаки геометрии по типам элементов (создаётся CLI `python -m src.services.ifc_features`, §5.11).

Примечание: `ifc_raw_elements.xlsx`, `ifc_elements_output.json`,
`ifc_raw_elements_grouped.json` и `position_links.json` в режиме АР **не
создаются** (это файлы режима КР — цифровой сборник, см. §3.3).

**Запуск `outputs/<session_id>/run_<NNN>/`** (этап 1):

- `materials.json` — применённые пользователем материалы (промежуточный);
- `filtered_elements.xlsx` — выбранные строки (промежуточный, вход для подбора);
- `Дерево_проекта_выбранные_элементы.xlsx`, `filtered_elements_grouped_AR.json`;
- `Дерево_проекта.xlsx` (всё здание);
- `ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx`;
- `building_parts.json` (промежуточный);
- **`Подобранные_таблицы_работ.json`** — основной результат подбора (АР);
- **`Подобранные_работы.json`** — работы цифрового сборника (larix) по шифрам подобранных таблиц (этап 1, `works_fetcher`);
- **`Финальный_перечень_работ.json`** / **`Финальный_перечень_работ.xlsx`** — финальный перечень работ по каждой группе элементов (при `WORKS_SELECTOR=perechen` — `perechen_pipeline`, §5.8; при `WORKS_SELECTOR=llm` — `works_final_selector`, §5.5: по каждой таблице элемента-представителя — ровно одна работа, LLM-выбор с детерминированным fallback; в JSON — метаданные группы (`group`, `group_path`, `group_quantity`, `group_formwork_area_m2`, `group_joint_length_m`, `group_header`, `group_rebar_mass_kg`), `work_id` каждой выбранной работы, компоненты стоимости из детальных параметров позиции larix (`catalog/work-process/detail`) — `cur_salary`/`cur_operation_of_machines`/`cur_cost_of_material_resources`, `quantity` — групповые измерители (объём/площадь/длина/длина швов/количество, для арматурных работ — `mass_t`; при загруженном ВОР арматурная сталь идёт отдельными строками по классам и диаметрам с объёмом из ВОР, а в `group_quantity` пишется разбивка `rebar_by_diameter`, §5.8.1 п. 4); xlsx — итоговая таблица в структуре режима КР: шифры, объёмы, «ЗП»/«ЭМ»/«МР», «Стоимость» = ЗП+ЭМ+МР, ИТОГО по ЗП/ЭМ/МР/Стоимость);
- `final_result_AR.json` — финальный выходной JSON запуска (читается API напрямую с диска);
- `analytic.json` — аналитическая справка по расчёту (§5.12; создаётся подбором по перечню `perechen_pipeline`, отдаётся эндпоинтом `GET /api/session/<id>/analytic`).
- **`Сравнение_ВОР_и_перечня.xlsx`** / **`Сравнение_ВОР_и_перечня.json`** — сравнение работ и объёмов ВОР заказчика и сформированного перечня работ **по каждой группе элементов и каждой работе** (листы «Сравнение по группам», «По видам конструкций», «Ресурсы (материалы)», «Детализация ВОР», «Детализация перечня»; арматура не сравнивается — «взято из ВОР»; §5.13, `vor_comparison`; создаётся, если ВОР загружен — PDF ВОР разбирается детерминированно `doc_pdf_parser.extract_vor_rows`, иначе по `ВОР_ведомость_объемов_работ.json`).

Состав итоговой выдачи запуска (файлы для скачивания, §5.7): только
**`Финальный_перечень_работ.xlsx`** — итоговая таблица работ (также выводится
на веб-интерфейсе с кнопкой «👁️ Просмотр»), а также (если загружен ВОР)
**`Сравнение_ВОР_и_перечня.xlsx`** — сравнение с ВОР заказчика (§5.13);
остальные файлы списка выше — рабочие артефакты конвейера, они остаются в
`run_<NNN>/` на диске, но в список файлов запуска не включаются.

**Отдельный режим «Сравнение ведомостей объёмов работ»** (несколько моделей +
ВОР заказчика) использует собственный каталог `outputs/<comparison_id>/` и файлы
`models.json` / `estimates.json` / `result.json` / `result.xlsx` — см. §15.

---

### 9. Ключевые отличия от режима КР

| Аспект | ЦС (КР) | ИИ (АР) |
|---|---|---|
| IFC-классы | Только конструктив (`ELEMENT_TYPES_KR`) | Конструктив + архитектура (`ELEMENT_TYPES_AR`) |
| Группировка | Часть здания → МССК → Материал → IFC-тип → Геометрия (→ Материал/Бетон) | **Та же иерархия, что и в ЦС** (`process_ifc_excel_ar` вызывает `group_elements_mssk`) |
| Подбор работ | Внешний API ТСН (`api_works_lookup`) | Подбор таблиц — локальный детерминированный алгоритм (`works_table_selector`, `data/algorithm.md`); финальный выбор работ по группам — по переключателю `WORKS_SELECTOR`: **`perechen`** (основной) — по перечню сметчиков `data/perechen_kr.xlsx` + локальный справочник расценок `data/price_cost.xlsx` (`perechen_kb`/`perechen_pipeline`, §5.8); **`llm`** — работы таблиц из цифрового сборника larix (`works_fetcher`, §5.4) + LLM-выбор (`works_final_selector`, §5.5) |
| Документы и сборники | Не используются | ПОС/ПЗ: 10 глобальных констант (`pd_parser`, §4) и полный разбор ≈40 параметров (`pd_full`, §5.10, эндпоинт `/pd_full`); ВОР и ПД: класс бетона B/W/F по конструкциям, класс арматуры, объёмы работ «по проекту» и сверка объёмов (`doc_facts`, §5.8.1); сборники ТСН: поправочные коэффициенты и не учтённые материалы (`tsn_coeffs`, §5.9, эндпоинты `/tsn/docs`, `/tsn_mode`); признаки геометрии IFC (`ifc_features`, §5.11); аналитическая справка (`analytic.json`, эндпоинт `/analytic`, §5.12) |
| Константы проекта | Не требуются | 10 констант (опалубка, высота здания, сезон ухода за бетоном, схема бетонирования, высота этажа, этажность здания, тип крана, грузоподъёмность крана, вместимость ковша, мощность оборудования) — из UI и/или ПОС/ПЗ/IFC; часть здания и `wall_location` (наружная/внутренняя) — параметры элемента IFC, не константы |
| Цифровой сборник | JSON-справочники (`ifc_elements_output.json`, `ifc_raw_elements_grouped.json`), `position_links.json`, иконки перехода к карточке позиции в превью | Справочники и ссылки не строятся (§3.4); на этапе 1 larix используется для получения работ подобранных таблиц: `works_fetcher` → `period.json` (корень сессии) + `Подобранные_работы.json` (`run_<NNN>/`, §5.4), а также для разбивки стоимости выбранных работ — `catalog/work-process/detail` (ЗП/ЭМ/МР, §5.5 п. 6) |
| Итог | `ОБЩИЙ_Финальный_перечень_работ.xlsx` (в итоговой выдаче запуска — только эта таблица; `selected_elements_grouped.json`, `final_result_KR.json` и др. остаются на диске запуска) | Итоговая выдача: **`Финальный_перечень_работ.xlsx`** — итоговая таблица в той же структуре, что и КР (шифры ТСН, объёмы, компоненты «ЗП»/«ЭМ»/«МР» (показатели larix × объём), «Стоимость» = ЗП+ЭМ+МР, заголовки групп, «ИТОГО:» по ЗП/ЭМ/МР/Стоимость; колонки «Стоимость за Ед. Изм.» нет, §5.5/§5.7); на диске также создаются `Подобранные_таблицы_работ.json`, `Подобранные_работы.json`, `Финальный_перечень_работ.json`, `final_result_AR.json` и `period.json` |

---

### 10. Конфигурация и добавление сборников ТСН

**Переменные окружения `.env`** (влияют на подбор ИИ):

| Переменная | Назначение |
|---|---|
| `WORKS_SELECTOR` | `perechen` (основной) — подбор по перечню сметчиков (§5.8); `llm` — прежний `works_final_selector` (§5.5) |
| `WORKS_SOURCE` | `local` — локальный справочник расценок (`data/price_cost.xlsx`); `api` — larix |
| `LLM_MODEL` | LLM для `pd_full`/`tsn_coeffs`/`perechen_pipeline` (rate_fix) |
| `KB_LLM_MODEL`, `KB_THINK` | LLM и режим «размышления» для `perechen_kb` |
| `NORMS_LLM_MODEL` | LLM для `pd_parser` (10 констант) |
| `LLM_BASE_URL`, `OLLAMA_BASE_URL` | адрес Ollama (`http://ollama:11434` в Docker; `http://localhost:11450` в рабочей копии) |
| `PERECHEN_KR_PATH`, `TREE_WORK_PATH`, `MSSK_NESTED_PATH`, `KB_CACHE_PATH` | справочники и кеш `perechen_kb` |
| `LOCAL_WORKS_PATH`, `LOCAL_RESOURCES_PATH`, `LOCAL_NORMS_PATH` | кеши локального справочника расценок/ресурсов |
| `TSN_DIR`, `PD_FULL_CACHE_PATH` | каталог сборников ТСН (`data/tsn`) и кеш полного разбора ПОС/ПЗ |
| `DOC_FACTS_CACHE_PATH` | кеш сопоставления фактов ВОР/ПД с конструкциями (`data/doc_facts_cache.json`, §5.8.1) |
| `LLM_MAX_CONCURRENCY` | лимит одновременных обращений к Ollama (семафор в `pd_parser`, по умолчанию `4`); согласуется с `OLLAMA_NUM_PARALLEL` — при `1` запросы просто ждут очереди, при большем значении параллельные порции ВОР/ПД и константы ПОС/ПЗ обрабатываются одновременно (генерация ответов — узкое место, ~70–85 % времени вызова) |
| `DOC_CACHE_DIR` | каталог кэша результатов разбора ПОС/ПЗ/ВОР/ПД по SHA-256 содержимого (по умолчанию `outputs/_doc_cache`; служебный, вне папок сессий) |

**Ollama** (Docker, `docker-compose.yml`): `OLLAMA_NUM_PARALLEL` (число
одновременно обслуживаемых запросов; при `1` — строго последовательно, замер
дал ускорение 1.03× на 4 параллельных запросах), `OLLAMA_CONTEXT_LENGTH`,
`OLLAMA_MAX_LOADED_MODELS`. Для ускорения разбора документов `OLLAMA_NUM_PARALLEL`
можно поднять (например, до `2`–`4`) согласованно с `LLM_MAX_CONCURRENCY`
(риск — рост потребления VRAM под KV-кэш).

Пути заданы **относительными** (`data/...`) — они работают и в рабочей копии
(относительно корня проекта), и в Docker (WORKDIR `/app` → `/app/data/...`).

**Добавление сборника ТСН**: PDF (с текстовым слоем) → `uploads/tsn/`, затем
`bash tsn_add.sh "uploads/tsn/<файл>.pdf"` (парсер `src/tools/tsn/`) →
`data/tsn/tsn_3_<N>_full.json`; после этого достаточно перезапросить
`GET /api/tsn/docs` (список сборников в UI обновится).

---

## 12. Режим Архитектура (ARCH)

Источник истины по режиму «Архитектура» — этот раздел README. Он описывает
актуальный состав модулей, порядок обработки, создаваемые файлы и
используемые справочники.

> Режим «Архитектура» — третий режим сервиса наряду с «ЦС» (`CS`) и «ИИ» (`AI`).
> Он обрабатывает те же входные файлы (IFC/PDF и документы ПОС/ПЗ/ВОР/ПД),
> что и режим «ИИ», но извлекает и подбирает работы **только по архитектурным
> решениям**, исключая конструктивные элементы.

---

### 1. Назначение и код режима

| Параметр | Значение |
|---|---|
| Публичное название | **Архитектура** |
| Публичный код (API/UI) | `ARCH` |
| Внутренний код пайплайна | `ARCH` |
| Значение по умолчанию | нет (по умолчанию остаётся `CS`) |

Допустимые входные значения `processingType`:

- `ARCH`, `ARCHITECTURE`, `АРХ`, `АРХИТЕКТУРА` — режим «Архитектура»;
- `CS`, `KR`, `КР` — режим «ЦС»;
- `AI`, `AR`, `АР` — режим «ИИ».

Нормализация кодов выполняется в
[`src/core/modes.py`](./src/core/modes.py):
`normalize_processing_type()`, `to_pipeline_type()`, `to_public_type()`,
`is_architecture()`, `is_ai_like()`.

---

### 2. Чем режим отличается от «ИИ» и «ЦС»

| Признак | ЦС (`CS`) | ИИ (`AI`) | Архитектура (`ARCH`) |
|---|---|---|---|
| Входные файлы | IFC/PDF | IFC/PDF + ПОС/ПЗ/ВОР/ПД | IFC/PDF + ПОС/ПЗ/ВОР/ПД |
| Набор IFC-классов | конструктив (`ELEMENT_TYPES_KR`) | конструктив + архитектура (`ELEMENT_TYPES_AR`) | **только архитектура** (`ELEMENT_TYPES_ARCH`) |
| Группировка | `process_ifc_excel_mssk` | `process_ifc_excel_ar` | `architecture.group_excel.process_ifc_excel_ar` |
| Подбор работ | API цифрового справочника ТСН | детерминированный алгоритм + LLM | собственные копии `works_table_selector`, `works_fetcher`, `works_final_selector` |
| Модули пайплайна | `src/services/*` | `src/services/*` | `src/services/architecture/*` |
| Итоговая выдача | `ОБЩИЙ_Финальный_перечень_работ.xlsx` | `Финальный_перечень_работ.xlsx` | `Финальный_перечень_работ.xlsx` |
| Финальный JSON | `final_result_KR.json` | `final_result_AR.json` | `final_result_ARCH.json` |

Режим «Архитектура» **не изменяет** поведение режимов ЦС и ИИ: их модули
продолжают работать с прежними кодами `KR`/`AR` и прежними наборами
IFC-классов.

---

### 3. Архитектурный набор IFC-классов

Определён в [`src/services/architecture/zero_step.py`](./src/services/architecture/zero_step.py)
как `ELEMENT_TYPES_ARCH`:

| IFC-класс | «Тип (RU)» |
|---|---|
| `IfcBuildingElementProxy` | Прочие_элементы |
| `IfcWindow` | Окна |
| `IfcDoor` | Двери |
| `IfcCurtainWall` | Стены (витражи/навесные фасады) |
| `IfcRoof` | Кровля |
| `IfcRailing` | Перила |
| `IfcFurnishingElement` | Мебель |
| `IfcPlate` | Плиты |
| `IfcShadingDevice` | Солнцезащитные устройства |
| `IfcWasteTerminal` | Санитарно-технические приборы |
| `IfcAirTerminal` | Воздухораспределители |
| `IfcMember` | Стойка_ограждения |
| `IfcCovering` | Изоляция (отделка/изоляция) |

**Исключены конструктивные решения:** `IfcWall`, `IfcWallStandardCase`,
`IfcFooting`, `IfcSlab`, `IfcColumn`, `IfcBeam`, `IfcStair`,
`IfcStairFlight`, `IfcRamp`, `IfcPile`.

> Для PDF-чертежей состав элементов определяется пайплайном машинного зрения
> (`ai-blueprint-to-ifc`): он распознаёт стены/конструкции чертежа. Режим
> «Архитектура» в PDF-ветке использует тот же результат распознавания, что и
> «ИИ»; архитектурная фильтрация по IFC-классам применяется к IFC-источнику.

---

### 4. Состав пакета `src/services/architecture/`

Все модули — копии соответствующих модулей `src/services/` с изменёнными
относительными импортами и архитектурным набором IFC-классов.

| Модуль | Назначение |
|---|---|
| `__init__.py` | Экспорт функций пайплайна, документация пакета |
| `zero_step.py` | Извлечение элементов IFC по `ELEMENT_TYPES_ARCH`, нормализация, Excel-таблицы, `IFC_глобальные_константы.json` |
| `ifc_raw_dump.py` | «Сырой» дамп свойств/QTO/материалов + расчёт количеств по bbox |
| `work_height.py` | Признаки высот/отметок элементов |
| `group_excel.py` | Группировка по правилам КР/АР: `process_ifc_excel_ar` вызывает `group_elements_mssk` (иерархия как в ЦС, суффикс файлов `_AR`) |
| `works_table_selector.py` | Детерминированный подбор таблиц работ (8 шагов `data/algorithm.md`) |
| `works_fetcher.py` | Работы таблиц из цифрового сборника (larix): `period.json`, `Подобранные_работы.json`, детальные параметры позиций |
| `works_final_selector.py` | Финальный подбор работ через LLM, расчёт объёмов и стоимости |
| `works_cost.py` | Корректировка объёмов по `koefs.xlsx`, форматирование стоимости |
| `ifc_reference_builder.py` | Вспомогательные функции группировки/агрегатов для финального подбора |
| `ifc_json_builder.py` | Сборка `final_result_ARCH.json` (`ARBuilder`, дисциплина `ARCH` → `АР`) |
| `ifc_features.py` | Признаки геометрии IFC (криволинейность/радиус, наклон, отметки) |
| `selection_template_builder.py` | Шаблон параметров подбора → `Параметры_подбора_элементов.json` |

Общие модули, которые **не дублируются** и используются как есть:

- `pd_parser.py`, `doc_pdf_parser.py`, `pd_full.py`, `doc_facts.py` — разбор
  документов ПОС/ПЗ/ВОР/ПД (формат документов не зависит от режима);
- `estimate_export.py` — сметная форма по сформированному перечню;
- `materials_lookup.py`, `mssk_lookup.py` — общие МССК-справочники;
- `serializer.py` — экспорт IFC → GLB;
- `session_manager.py`, `routes.py` — оркестрация и API (ветки `ARCH`).

---

### 5. Порядок обработки

#### 5.1. Этап 0 — извлечение элементов

`SessionManager._process_ifc_bg` для `processing_type="ARCH"`:

1. Выгружает сырой дамп `IFC_исходные_параметры.json/.xlsx` общим
   `ifc_raw_dump.dump_ifc_elements_raw` (все элементы модели — источник
   геометрии и QTO).
2. Вызывает `architecture.zero_step.zero_step(...)` — извлекает **только**
   `ELEMENT_TYPES_ARCH`, формирует `ДЛЯ_СМЕТЧИКА_исправленный.xlsx`,
   `IFC_глобальные_константы.json`.
3. Заполняет `Параметры_подбора_элементов.json` собственной копией
   `architecture.selection_template_builder`.

Для PDF используется общий конвейер `ai-blueprint-to-ifc` (детекция
стен/layout/легенды, анализ штриховок), после чего результат обрабатывается
теми же модулями группировки/подбора, что и IFC.

#### 5.2. Этап 1 — группировка и подбор работ

1. Применяются материалы пользователя, выполняется фильтрация выбранных
   строк → `filtered_elements.xlsx`.
2. Группировка `architecture.group_excel.process_ifc_excel_ar`
   (та же иерархия, что и в ЦС/ИИ: Часть здания → МССК → Материал → IFC-тип →
   Геометрия → Материал/Бетон) →
   `Дерево_проекта_выбранные_элементы.xlsx`,
   `filtered_elements_grouped_AR.json`, `ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx`,
   `building_parts.json`.
3. `architecture.works_table_selector.build_works_tables_json` — 8 шагов
   алгоритма `data/algorithm.md` → `Подобранные_таблицы_работ.json`.
4. `architecture.works_fetcher.fetch_and_save_works` — актуальный период ТСН
   → `period.json` (корень сессии), работы таблиц →
   `Подобранные_работы.json` (папка запуска).
5. `architecture.works_final_selector.select_final_works` — финальный подбор
   работ через LLM (по одной работе на таблицу), расчёт объёмов и стоимости
   → `Финальный_перечень_работ.json/.xlsx`.
6. `architecture.ifc_json_builder.build_final_json` →
   `final_result_ARCH.json`.

#### 5.3. Документы (опционально)

Для режима `ARCH` доступны те же документы, что и для `AI`:

- ПОС → `ПОС_глобальные_константы.json`;
- ПЗ → `ПЗ_глобальные_константы.json`;
- ВОР → `ВОР_ведомость_объемов_работ.json/.xlsx`;
- ПД → `ПД_характеристики_элементов.json/.xlsx`;
- полный разбор ПОС/ПЗ → `pd_full.json`;
- факты ВОР/ПД → `doc_facts` (эндпоинт `/doc_facts`);
- сравнение ВОР заказчика и перечня → `Сравнение_ВОР_и_перечня.xlsx/.json`.

---

### 6. Артефакты

Все результаты — только в `outputs/<session_id>/` и
`outputs/<session_id>/run_<NNN>/`:

- `ДЛЯ_СМЕТЧИКА_исправленный.xlsx`, `ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx`;
- `Дерево_проекта_выбранные_элементы.xlsx`, `Дерево_проекта.xlsx`;
- `filtered_elements.xlsx`, `filtered_elements_grouped_AR.json`,
  `building_parts.json`, `materials.json`;
- `Подобранные_таблицы_работ.json`, `Подобранные_работы.json`,
  `Финальный_перечень_работ.json/.xlsx`;
- `final_result_ARCH.json`;
- `Параметры_подбора_элементов.json`;
- `period.json` (корень сессии);
- `Смета_по_сформированному_перечню.xlsx`,
  `Сравнение_ВОР_и_перечня.xlsx/.json` (при наличии данных).

Итоговая выдача запуска (как и в режиме ИИ) — только финальная таблица работ:
`Финальный_перечень_работ.xlsx` (+ сметная форма/сравнение при наличии).
Остальные файлы остаются на диске для отладки.

---

### 7. Используемые справочники `data/`

Только чтение:

- `algorithm.md` — спецификация 8-шагового алгоритма подбора таблиц;
- `ifc_to_collections.json` — правила `IfcClass (+PredefinedType)` → сборники;
- `msck_elements_compact.json` — дерево МССК-элементов;
- `tree_work_compact.json` — дерево сборников ГЭСН/ТСН-2001;
- `works_classification.json` — классификация сборников и схема констант;
- `params_registry.json` — реестр параметров ПОС/ПЗ;
- `selection_parameters.json`, `selection_parameters_mapping.json` — шаблон и
  карта параметров подбора;
- `tsn/*.json` — поправки сборников ТСН;
- `koefs.xlsx` — нормы расхода (корректировка объёмов);
- `price_cost.xlsx` — локальный справочник расценок;
- МССК-справочники `elements_mssk*`, `materials_mssk*`;
- `Типовой ВОР по КР.xlsx` — не используется режимом (относится к отдельному
  функционалу «Формирование ВОР»).

---

### 8. API и веб-интерфейс

- В интерфейсе (вкладка «Обработка») добавлен третий переключатель
  **«Архитектура»** рядом с «ЦС» и «ИИ».
- `POST /api/upload_ifc` принимает `processingType=ARCH`; для него доступны
  те же дополнительные поля `posFile`, `pzFile`, `vorFile`, `pdFile`.
- `POST /api/session/<id>/select_rows` и `/new_run` принимают
  `processingType=ARCH`, `floorHeight`, `globalConstants`.
- `GET /api/session/<id>/preview`, `/status`, `/sessions` возвращают
  публичный код `ARCH`.
- `GET /api/session/<id>/works_constants`, `/pd_full`, `/doc_facts`,
  `/analytic`, `/compare_vor` работают для `ARCH` так же, как для `AI`.
- Вкладка «JSON-справочники» удалена; вместо неё в интерфейсе вкладка
  «Проверка ЦИМ модели» — самостоятельный функционал вне режимов ЦС/ИИ
  (`src/services/check_model/`, см. раздел «Проверка ЦИМ модели»).
- `GET /api/session/<id>/run/<N>/final_json/result` для запуска `ARCH`
  возвращает `processingType=ARCH` (поле `discipline` внутри JSON остаётся
  «АР» — структура как у ИИ; публичный код берётся из запуска, а не из
  `discipline`).
- **Авторизация и cookie.** Cookie сессии Flask-Login называется `session`
  (настраивается переменной `SESSION_COOKIE_NAME`). Cookie браузера общие для
  всех портов одного хоста: если рядом работает другой экземпляр сервиса
  (Docker на `6001`, копия на `6005`) с иным `SECRET_KEY`, они перезаписывают
  cookie друг друга, и API отвечает `401 «Требуется авторизация»` (в UI —
  «❌ Требуется авторизация» в блоке «Параметры влияющие на расценки», опрос
  `docs_progress`). `start.sh` задаёт для рабочей копии отдельное имя cookie
  `ifc_vor_dev_session`.

---

### 9. Ограничения

- Режим предназначен для **архитектурных** элементов; конструктивные
  элементы (стены, колонны, перекрытия, балки, фундаменты, сваи, лестницы)
  в выборку не попадают.
- Для PDF-чертежей архитектурная фильтрация ограничена возможностями
  пайплайна распознавания (детекция стен/конструкций); режим использует тот
  же результат, что и «ИИ».
- Подбор работ выполняется теми же алгоритмами и LLM, что и в режиме «ИИ»;
  отличия — только в наборе элементов и используемых копиях модулей.
- Перенос изменений в основной Docker-сервис выполняется вручную
  (см. `KODA.md`, §4.1, §8.5).

---

## 13. Проверка ЦИМ модели

Источник истины по отдельному функционалу **«Проверка ЦИМ модели»** — этот раздел README. Функционал
не относится к режимам ЦС/ИИ/Архитектура и к «Формированию ВОР»: подбор работ
не выполняется, используется только IFC-модель и справочник требований
`data/parametrs_name.json`.

### 1. Назначение

Проверка ЦИМ модели:

1. принимает IFC-файл (только `.ifc`);
2. выполняет тот же шаг выгрузки сырых параметров, что и режим ЦС
   (`ifc_raw_dump.dump_ifc_elements_raw`) — в каталоге сессии появляются
   `IFC_исходные_параметры.json` и `IFC_исходные_параметры.xlsx`;
3. по IFC-классу элемента определяет требования из
   `data/parametrs_name.json` (наборы `RusSet_Common`, `RusSet_Quantities`,
   `RusSet_Location`, `RusSet_Label`, `RusSet_Construction`);
4. с помощью LLM (`CHECK_MODEL_LLM_MODEL` → `LLM_MODEL` → `NORMS_LLM_MODEL`;
   в `.env` рабочей копии задано `qwen3.8:27b`) сопоставляет требуемые параметры
   (`RUS_*`) с колонками модели;
5. дополняет каждый элемент требуемыми параметрами и сохраняет результат.

Функционал рассчитан на модели **разных проектировщиков**: имена свойств,
наборов и QTO в каждом файле свои. Поэтому поддерживаются оба распространённых
варианта именования (`MGE_*` в наборах `ExpCheck_*` и `RUS_*` в наборах
`RusSet_*`), произвольные имена QTO, «этажные» префиксы корпуса/секции,
проёмы без пространственной привязки и классы-заглушки
(`IfcBuildingElementProxy`). Модели проверялись на пяти наборах:
`Алтуфьевское_шоссе`, `Варшавское_шоссе`, `Главмосстрой`, `Давыдковская`,
`Мира_2` (`data/ifc_models/`).

### 2. Модули

Все модули функционала — в `src/services/check_model/`:

| Модуль | Назначение |
|---|---|
| `requirements.py` | Загрузка `data/parametrs_name.json`, группировка по IFC-классам, выбор требований для элемента (код МССК `MGE_ElementCode`/`RUS_MSSK_Element_Code`, `PredefinedType`, наименование), разбор этажа на объект/подобъект/уровень (`storey_parts`) |
| `llm_client.py` | Клиент Ollama `/api/chat` (JSON-ответы), модель: `CHECK_MODEL_LLM_MODEL` → `LLM_MODEL` → `NORMS_LLM_MODEL` (в `.env` — `qwen3.8:27b`), контекст `CHECK_MODEL_LLM_NUM_CTX` (по умолчанию 16k — больший контекст заставляет Ollama перезагружать модель и резко замедляет запрос) |
| `mapping.py` | Построение карты «требуемый параметр → колонка модели + преобразование»: LLM + детерминированный fallback, проверка ответа LLM (отклонение производных величин опалубки/проекций, кодов и типов вместо наименований), кэш `cache/check_model/mapping_cache.json` |
| `geometry.py` | По-элементный добор геометрических параметров (`RUS_Length`, `RUS_Width`, `RUS_Height`, `RUS_Thickness`, `RUS_Perimeter`, `RUS_Area`, `RUS_NetFootprintArea`, `RUS_NetSideArea`, `RUS_NetSurfaceArea`, `RUS_Volume`, `RUS_GrossVolume`): альтернативные колонки QTO/`QTO_bbox::*` и расчёт по формулам класса IFC; отдельные правила для стен, плит, проёмов, колонн/свай и балок/связей (`IfcBeam`, `IfcMember` — размеры сечения из габаритов bbox) |
| `grouping.py` | Группировка проверенных элементов для веб-превью по иерархии типового ВОР (часть здания → раздел → подраздел → наименование элемента): вид конструкции — по коду МССК из `data/elements_mssk_nested.json`, часть здания — по типу этажа/отметке, технология (сборные/монолитные) — по `ConstructionMethod`; кеш `Проверка_ЦИМ_модели_группы.json` |
| `formwork.py` | Площадь опалубки элемента (м²) по виду конструкции типового ВОР: стены/парапеты — все четыре боковые поверхности (две лицевые нетто + два торца стены + вертикальные щёки проёмов), колонны/пилоны — четыре стороны, фундаменты/приямки — периметр × толщина, перекрытия/площадки — горизонтальная площадь с одной стороны, лестничные марши — проекция на пол (shapely), балки — низ и две длинные боковые грани. Считается только для монолитного ж/б; у сборного ж/б и неконструктивных элементов площади опалубки нет. Источник величин — требуемые параметры `RUS_*`, геометрия дампа — только как уточнение (ориентация сечения балки, проекция марша, торцы стены и щёки проёмов) |
| `pipeline.py` | Оркестратор проверки: чтение сырого дампа, распределение элементов по требованиям (включая наследников `*StandardCase`/`*ElementedCase` и прокси-элементы), заполнение параметров, запись JSON/XLSX и отчёта |
| `__init__.py` | Экспорт `run_check_model`, `load_requirements`, имён файлов |

Оркестрация фонового запуска — `SessionManager.process_check_model` /
`SessionManager._process_check_model_bg` (`src/services/session_manager.py`).

### 3. API и интерфейс

Вкладка **«Проверка ЦИМ модели»** в веб-интерфейсе (рядом с «Обработка» и
«История»). Вкладка «JSON-справочники» удалена.

- `POST /ifc-vor/api/check_model` (синоним `/api/check-model`) — multipart
  `file` (только `.ifc`), ответ `202` с `sessionId`.
- `GET /ifc-vor/api/check_model/<session_id>` — статус/результат:
  `202` пока выполняется, `200` с `report` и списком файлов после завершения.
- `GET /ifc-vor/api/check_model/<session_id>/groups` (синоним
  `/api/check-model/<session_id>/groups`) — сгруппированное по типовому ВОР
  превью проверенных элементов: дерево «часть здания → раздел → подраздел →
  наименование элемента», счётчики и суммы объёма и **площади опалубки** по
  группам (`totalFormworkArea` — итог по всей модели).
  Параметр `?refresh=1` пересчитывает группы, игнорируя кеш сессии. Пустые
  группы не возвращаются; элементы, для которых подраздел не определён
  (проёмы `IfcOpeningElement` и т. п.), собраны в блок «Прочие элементы
  (вне разделов типового ВОР)».
- Статус, скачивание и удаление — общими эндпоинтами
  `/api/session/<id>/status`, `/api/session/<id>/download/<filename>`,
  `/api/session/<id>/download_all`, `DELETE /api/session/<id>`.

Карточка результата во вкладке показывает:

- «Элементов в модели» — всего строк в сыром дампе (`elements_total`);
- «Проверено элементов» — в формате `X из Y` (`elements_checked` из
  `elements_total`);
- «Классов без требований» — количество классов и суммарное число их
  элементов (`skipped_classes`);
- таблицу **«Заполнение по классам и требованиям»**:
  - колонка «Элементов» — `X из Y`, где `X` — проверено по этому набору
    требований (`elements`), `Y` — всего элементов этого IFC-класса в файле
    (`elements_total`, считается по открытой IFC-модели через `by_type()`,
    поэтому учитываются и наследники, например `IfcWallStandardCase` для
    `IfcWall`); при отсутствии данных — только `X`;
  - колонка «Заполнено» — по каждому требуемому параметру `имя: заполнено/всего`;
    параметры, не заполненные хотя бы у части элементов, выделены красным, при
    наведении курсора показывается подсказка с количеством пустых значений, а
    на заголовке ячейки — перечень незаполненных параметров;
  - под таблицей — примечание о классах, отсутствующих в справочнике
    требований (с числом элементов), объясняющее разницу между «всего» и
    «проверено».

Кнопка **«🌳 Превью по типовому ВОР»** в карточке результата разворачивает
встроенный блок «Превью по типовому ВОР» под карточкой результата проверки
(аналогично блоку выбора строк и параметров обработки в режиме ИИ; модального
окна нет) с деревом проверенных элементов, сгруппированных по иерархии типового
ВОР (`data/Типовой ВОР по КР.xlsx`): часть здания → раздел → подраздел (для
автостоянки — с блоком «Подземная/Надземная часть») → наименование элемента.
Строки элементов подгружаются при раскрытии группы наименования. В блоке
показываются количество элементов, сумма объёмов и сумма **площадей опалубки**
по каждой группе,
доступны кнопки «Развернуть всё» / «Свернуть всё» / «🔄 Обновить» (пересчёт
групп с `?refresh=1`) / «✕ Скрыть». Повторное раскрытие блока использует уже
загруженные данные без повторного запроса. Пустые группы не отображаются.

Оформление групп превью повторяет блок «Выбор строк и параметров обработки»
режима ИИ (`templates/index.html`): те же фоны и цвета текста по уровням
(часть здания — `#e2e8f0`/`#2d3748`, раздел — `#edf2f7`/`#2d3748`,
блок/подраздел — `#f0fff4`/`#276749` и `#f7fafc`/`#4a5568`, наименование —
белый/`#2b6cb0`), шаг отступа вложенности 30 px, стрелки `.group-arrow`
(▶/▼ с поворотом при раскрытии), бейдж количества `×N` и сводка
`Σ Объём: … м³ · Σ Площадь опалубки: … м²`. Раскрытие/сворачивание — кликом по строке
группы, корневая часть здания оформлена как sticky-заголовок. Отличаются только
набор столбцов (IFC-класс, этаж, код МССК, объём, площадь опалубки) и тексты групп
(разделы/подразделы типового ВОР).

Правила отнесения элемента к подразделу (`grouping.py`):

- **часть здания** — «Автостоянка» по тексту этажа/наименования (внутри неё —
  блок «Подземная/Надземная часть»), иначе тип этажа
  (`zero_step.classify_storey_type`: подземный/цокольный/надземный), затем знак
  отметки; фундаменты, сваи и изоляция фундаментов всегда относятся к подземной
  части, плита 1-го этажа при наличии подземной части — к перекрытиям подземной
  части;
- **вид конструкции** — в первую очередь по коду МССК
  (`RUS_MSSK_Element_Code`) через `data/elements_mssk_nested.json` (например,
  `ЭЛ 30 24 20` — лестничная площадка, `ЭЛ 30 10 80` — плита перекрытия,
  `ЭЛ 10 10 30 04` — фундаментная плита); если код не найден — по IFC-классу,
  `Элемент_ЦИМ` и тексту наименования. **Приямок** (текст «приям» в
  наименовании/типе/материале элемента) всегда относится к подразделу
  «Фундаментная плита» независимо от IFC-класса и кода МССК: в моделях он
  выгружается как `IfcSlab`/«Перекрытие» с кодом МССК фундаментного блока, и
  без этого правила попадал в подраздел плит перекрытия;
- **технология** — по `Pset_ConcreteElementGeneral.ConstructionMethod`
  (`src/services/construction_method.py`): сборные стены, лестницы и парапеты
  попадают в раздел «Сборные ж/б конструкции», монолитные — в монолитные
  разделы;
- **последний уровень** — наименование элемента (`Name` без завершающих
  числовых хвостов `:<число>`; у маршей и лестничных площадок используется
  `ObjectType`, иначе каждая ступень стала бы отдельной группой);
- элементы без подраздела (проёмы `IfcOpeningElement` и др.) собираются в блок
  «Прочие элементы (вне разделов типового ВОР)» → «Не распределено по разделам
  типового ВОР» → по `Элемент_ЦИМ` → по наименованию.

#### Площадь опалубки в превью (`formwork.py`)

Площадь в превью — это **площадь опалубки** (м²), а не площадь поверхности
элемента. Она нужна только элементам из **монолитного** железобетона: у сборного
ж/б (материал `Железобетон сборный`, `ConstructionMethod` = `Precast`, «сборн» в
наименовании) и у неконструктивных элементов (проёмы, изоляция, деформационные
швы, засыпка, сваи) площади опалубки нет — в превью и в суммах групп такие
элементы дают `0`, в строке элемента отображается «—» с подсказкой.

Расчёт опирается на **требуемые параметры `RUS_*`** (они универсальны: на шаге
проверки ЦИМ заполняются из исходных параметров модели, которые в разных
проектах называются по-разному). Геометрические колонки сырого дампа
используются только как уточнение там, где `RUS_*` принципиально недостаточно:
ориентация сечения балки (`QTO_geom::Площадь_низа_м2`), проекция марша на пол
(`QTO_geom::Горизонтальная_проекция_м2`), торцы стены
(`QTO_geom::Площадь_торцов_стены_м2` — учитывает стены из нескольких тел) и
щёки проёмов (`QTO_geom::Площадь_торцов_проёмов_м2`).

| Вид конструкции (подраздел ВОР) | Правило | Формула |
|---|---|---|
| Стена, парапет | площадь по всем четырём боковым поверхностям: две лицевые грани нетто + два торца стены + вертикальные щёки проёмов | `2 × RUS_Volume / RUS_Thickness + QTO_geom::Площадь_торцов_стены_м2 + QTO_geom::Площадь_торцов_проёмов_м2` (лицевые эквивалентны `2 × RUS_NetSideArea`, но не зависят от выгрузки `NetSideArea` в QTO). Торцы стены — `2 × толщина × высота` на каждое тело меша (стена может быть собрана из нескольких тел, у каждого свои два торца); fallback — `2 × RUS_Thickness × RUS_Height`. Щёки проёмов — сумма `2 × глубина × высота` по всем `IfcRelVoidsElement`-вырезам стены. Fallback лицевых — `2 × RUS_NetSideArea`, затем `2 × RUS_Length × RUS_Height` |
| Колонна, пилон | площадь с четырёх сторон | `2 × (RUS_Width + RUS_Height) × RUS_Length`; fallback `RUS_NetSideArea` |
| Фундамент, приямок | только по внешнему периметру и толщине плиты | `RUS_Perimeter × RUS_Thickness` (периметр — м, толщина — мм); fallback `2 × (RUS_Length + RUS_Width) × RUS_Thickness` |
| Перекрытие | горизонтальная площадь с одной стороны | `RUS_Area`; fallback `QTO_geom::Горизонтальная_проекция_м2`, затем `RUS_Length × RUS_Width` |
| Лестничная площадка | горизонтальная площадь с одной стороны | `RUS_Area` (то же fallback) |
| Лестничный марш | площадь проекции на пол | `QTO_geom::Горизонтальная_проекция_м2` (shapely-объединение проекций треугольников меша, `stair_formwork.horizontal_projection`); fallback `RUS_Length × RUS_Width` |
| Балка | низ и две длинные боковые грани, верхняя не опалубливается | `RUS_Length × b + 2 × RUS_Length × h`; ориентация сечения уточняется по низу меша `QTO_geom::Площадь_низа_м2` и `RUS_Volume`, без геометрии `b`/`h` — меньший/больший из `RUS_Width`/`RUS_Height` |
| Агрегат `IfcStair` | не считается | марши и площадки учитываются собственными элементами (иначе площадь удвоится) |

В каждой листовой строке превью сохраняется правило расчёта (`areaBasis` —
подсказка при наведении на площадь) и площадь самого элемента (`elementArea`,
`RUS_Area`) для справки. Кеш `Проверка_ЦИМ_модели_группы.json` содержит версию
структуры (`version`); при изменении правил группировки/расчёта кеш
пересчитывается автоматически.

Сессия хранится в `outputs/sessions.json` с `session_type='check_model'`,
статус обработки — `check_model_processing`.

### 4. Артефакты сессии (`outputs/<session_id>/`)

| Файл | Содержание |
|---|---|
| `IFC_исходные_параметры.xlsx` | Сырой дамп всех элементов IFC (как в режиме ЦС) |
| `IFC_исходные_параметры.json` | JSON-копия сырого дампа |
| `Проверка_ЦИМ_модели.xlsx` | Элементы проверяемых IFC-классов + требуемые параметры `RUS_*` |
| `Проверка_ЦИМ_модели.json` | То же в JSON |
| `Проверка_ЦИМ_модели_отчет.json` | Сводка: элементы, классы, заполненные/пустые значения, ошибки LLM |
| `Сопоставление_параметров.json` | Карта «требуемый параметр → колонка модели/преобразование», использованная LLM |
| `Проверка_ЦИМ_модели_группы.json` | Кеш сгруппированного превью по типовому ВОР (строится при первом запросе `.../groups`, пересчитывается при обновлении `Проверка_ЦИМ_модели.json`, смене версии структуры `version` или `?refresh=1`) |

Поля отчёта, важные для интерфейса:

- `elements_total` / `elements_checked` / `elements_skipped` — всего элементов в
  сыром дампе, проверено и пропущено;
- `skipped_classes` — `{IFC-класс: количество}` для классов, отсутствующих в
  `data/parametrs_name.json` (их элементы в результат не попадают);
- `classes[<IFC-класс>].elements_total` — всего элементов класса в файле IFC
  (считается в `SessionManager._process_check_model_bg` по открытой модели
  через `by_type()` до выгрузки дампа; учитывает наследников);
- `classes[<IFC-класс>].requirements[<код>]` — `elements` (проверено по этому
  набору требований), `elements_total` (всего элементов класса в файле),
  `filled` / `missing` по каждому параметру.

Если карта `class_totals` не передана (например, при вызове
`run_check_model` только по каталогу сессии), количество элементов класса
берётся из сырого дампа.

Отчёты, сформированные до появления полей `elements_total` в классах и
наборах требований, отображаются в прежнем виде (только `X` в колонке
«Элементов»); для получения «X из Y» нужно запустить проверку модели заново.

Для проверки ЦИМ сырой дамп формируется с `exclude_classes=()` (в него попадают
и проёмы `IfcOpeningElement`, требования В.13), а также с
`compute_dimensions=True` и `dimension_classes` = классы требований + их
наследники (`IfcWallStandardCase`, `IfcWallElementedCase`,
`IfcSlabStandardCase`, `IfcSlabElementedCase`, `IfcOpeningStandardCase`) +
`IfcBuildingElementProxy`. Это нужно потому, что QTO многих моделей содержит
объём и площадь, но не содержит размеров сечения (балки, пластины, колонны):
недостающие габариты добираются из геометрии элемента
(`ifc_geometry_quantities.fill_missing` → `QTO_bbox::*`). Дополнительно
передаётся `opening_ends=True` — у стен сохраняются расчётные колонки для
опалубки:

- `QTO_geom::Площадь_торцов_стены_м2` — торцы стены: `2 × толщина × высота` на
  каждое тело меша (число тел — по числу элементов `Body`; стена может быть
  собрана из нескольких тел, у каждого свои два торца);
- `QTO_geom::Площадь_торцов_проёмов_м2` — вертикальные щёки проёмов: сумма
  `2 × глубина × высота` по всем `IfcRelVoidsElement`-вырезам стены
  (`ifc_raw_dump.opening_ends_area_m2`). Высота — вертикальный габарит проёма,
  глубина — QTO `Depth`, но не больше толщины стены (в моделях встречаются
  проёмы с перепутанными осями QTO: `Height` = толщина стены, `Depth` = её
  высота).

В режимах ЦС/ИИ поведение `dump_ifc_elements_raw` не изменилось (по умолчанию
вырезы и прокси-элементы исключаются, `compute_dimensions=False`,
`opening_ends=False`, расчётные колонки торцов не создаются).

**Плиты: геометрия не рассчитывается, если QTO уже содержит объём и площадь.**
Для `IfcSlab` (и наследников `IfcSlabStandardCase`/`IfcSlabElementedCase`)
габариты в требованиях ЦИМ не используются: толщина берётся из
`Qto_SlabBaseQuantities::Depth`, периметр — из `...::Perimeter`, площадь и
объём — из `Net/GrossArea`/`Net/GrossVolume`, а длина/ширина восстанавливаются
из площади и периметра (`check_model/geometry.py`, см. §5). Поэтому при
наличии в QTO объёма и площади `ifc_geometry_quantities.fill_missing` не
вызывает `create_shape` ради колонок `QTO_bbox::*`: для плиты это вычитание
проёмов и триангуляция всей геометрии — от 1 до 19 с на элемент. Если объёма
или площади в QTO нет, геометрия рассчитывается как раньше (и габариты
публикуются вместе с ней). На модели `Главмосстрой_КР.ifc` это сократило шаг
выгрузки дампа с ~23 до ~5,5 мин (503 плиты, из них 476 — без расчёта
геометрии); значения требуемых параметров в `Проверка_ЦИМ_модели.*` не
изменились ни по одному классу и набору требований, а у элементов остальных
классов состав и значения колонок дампа совпадают побайтово.

Этаж проёма в дампе определяется через связь `IfcRelVoidsElement`
(`VoidsElements` → `RelatingBuildingElement` → `find_storey`): у
`IfcOpeningElement` нет собственного `ContainedInStructure`, поэтому раньше
`RUS_Level` у всех проёмов был пуст.

### 5. Правила заполнения

- Если для IFC-класса в справочнике несколько записей (например, `IfcSlab` —
  В.2 «Перекрытие» и В.3 «Фундаментная плита»), требования выбираются по коду
  МССК (`MGE_ElementCode` или `RUS_MSSK_Element_Code`), `PredefinedType` и
  наименованию элемента. Дополнительно учитываются наименования из свойств
  (`MGE_Name`, `RUS_MSSK_Element_Name`, `RUS_Name`) и `PredefinedType`
  (`BASESLAB`/`FOUNDATION` — фундаментная плита, `FLOOR`/`ROOF`/`LANDING` —
  перекрытие), поэтому в модели без кодов МССК плита основания не путается с
  перекрытием. Классы-наследники (`IfcWallStandardCase`,
  `IfcSlabElementedCase`, `IfcOpeningStandardCase`, `IfcColumnStandardCase` и
  т. п.) приводятся к классу требований.
- **Классы-заглушки.** `IfcBuildingElementProxy` не имеет собственных
  требований, но проектировщики кладут в него типовые элементы (например,
  «Лестничный марш сборный» вместо `IfcStairFlight`). Такой элемент получает
  требования, если его код МССК точно совпадает с кодом требования либо в
  тексте элемента есть полное наименование элемента из справочника
  (`_infer_proxy_requirement`); при неоднозначности элемент остаётся в
  `skipped_classes`. Догадки по отдельным словам не принимаются.
- **Невидимые символы в именах.** Некоторые CAD-системы добавляют в имена
  этажей LTR/RTL-марки и BOM (`\u202a`, `\ufeff` и др.) — они удаляются при
  разборе этажа, иначе префикс корпуса/секции не распознавался.
- **Объект, подобъект и уровень.** `RUS_Level` — уровень (этаж),
  `RUS_Object` — номер объекта (корпуса), `RUS_Object_Sub` — номер подобъекта
  (секции). Если отдельных свойств нет, значения разбираются из имени этажа:
  префикс `К…`/`K…` → `RUS_Object`, префикс `С…`/`C…` → `RUS_Object_Sub`,
  остальное — уровень. Например, `К01_1_этаж` → объект `К01`; `С02_1_этаж` →
  подобъект `С02`; `Этаж 01 (+0,000)` → только уровень. Значения из свойств
  (`MGE_Object`, `RUS_Object`, `MGE_ObjectSub`, `RUS_Object_Sub`) приоритетнее
  разбора этажа.
- **Детерминированная карта** (fallback без LLM) понимает оба варианта имён
  свойств: `MGE_ElementCode` / `RUS_MSSK_Element_Code`, `MGE_Name` / `RUS_Name`
  / `RUS_MSSK_Element_Name`, `MGE_MaterialCode` / `RUS_MSSK_Material_Code`,
  `MGE_ConcreteGrade` / `RUS_StrengthClass`,
  `MGE_ReinforceStrengthClass` / `RUS_ReinforcementStrengthClass` /
  `RUS_SteelGrade`, `MGE_WaterResist` / `RUS_WaterImpermeability`,
  `MGE_FreezeDurability` / `RUS_FreezeDurability`, `MGE_Object` / `RUS_Object`
  и т. д. Поэтому модели с наборами `RusSet_*` (свойства уже названы как
  требуемые параметры) заполняются и без LLM.
- **Почему сопоставление делает LLM.** В сервис подаются модели разных
  проектировщиков: одни и те же по смыслу данные лежат в свойствах с
  произвольными именами (наборы `Pset_*`, пользовательские `ExpCheck_*`, QTO,
  русские названия колонок), поэтому жёсткое сопоставление по имени колонки
  работает только на «эталонной» модели. LLM получает:
  - требуемые параметры (`RUS_*` — характеристика, тип, единица, пример);
  - все непустые колонки модели класса с примерами значений;
  - контекст элемента (`IfcClass`, `Name`, `ObjectType`, `Description`,
    `PredefinedType`, `Tag`, `Материал`, `Этаж`),
  и сопоставляет параметры по смыслу, типу значения и единицам, а не по
  совпадению названия. В промпте явно указано, что имена колонок произвольны
  и в каждом файле свои, а также запрещено подставлять «похожее по имени, но
  другое по смыслу» значение (площадь вместо объёма, длина элемента вместо
  размера сечения).
- **Марки бетона W/F.** Для параметров `RUS_WaterResist` (водонепроницаемость,
  пример `W6`) и `RUS_FreezeDurability` (морозостойкость, пример `F150`)
  детерминированная карта выбирает колонки со свойством
  `MGE_WaterResist` / `MGE_FreezeDurability` (а также колонки с «WaterResist»,
  «Водонепрониц», «FreezeDurability», «Морозостой» в имени), а значение всегда
  приводится к каноническому виду `W<n>` / `F<n>` преобразованиями
  `extract_water_resist` / `extract_freeze_durability`. Это позволяет заполнять
  марки и в том случае, когда они записаны в одной строке с другими данными
  (например, «B30 W6 F150»). Если выбранная LLM колонка не содержит марку,
  значение ищется в свойствах `::MGE_WaterResist` / `::MGE_FreezeDurability`,
  затем в «Материале» и наименовании элемента. Если в модели марок нет,
  параметр остаётся пустым и попадает в `missing` отчёта.
- Ответ LLM проверяется: `source` должен быть точным именем колонки из
  переданного списка; неизвестные колонки и недопустимые преобразования
  игнорируются (остаётся значение из детерминированной карты). Дополнительно
  отклоняются заведомо неверные подстановки: производные величины (площадь
  опалубки, торцов/низа, горизонтальная проекция) — в `RUS_Area` и площади;
  колонки площадей — в объёмы; периметры — в линейные размеры; имена типов и
  коды — в `*_Name`. Если свойство модели уже названо как требуемый параметр
  (например, `Свойство::RusSet_Common::RUS_MSSK_Element_Code`), ответ LLM его
  не заменяет.
- Карта кэшируется по набору колонок модели, IFC-классу, коду требований и
  версии промпта (`PROMPT_VERSION` в `mapping.py`), поэтому повторная проверка
  той же модели не вызывает LLM заново, а для модели с другими именами колонок
  карта строится заново.
- Если LLM недоступна, используется детерминированное сопоставление по именам
  колонок (`MGE_*`, `QTO::*`, `QTO_bbox::*`, `Этаж` и т.д.). Ошибка LLM не
  прерывает проверку; в отчёте (`llm_enabled`, `llm_errors`, `llm_used` по
  каждому набору) и в карточке результата в интерфейсе видно, чем выполнено
  сопоставление.
- Поддерживаемые преобразования: `none`, `mm_to_m`, `mm2_to_m2`, `mm3_to_m3`,
  `storey_object`, `storey_sub`, `storey_level`, `extract_concrete_grade`,
  `extract_steel_grade`, `extract_water_resist`, `extract_freeze_durability`,
  `first_number`.
- Единицы приводятся к единицам требования (например, периметр из мм в м).
  Если единицы уже пересчитаны преобразованием (`transform`), повторное
  приведение по единице требования не выполняется.
- **Геометрические параметры: добор по элементу (`geometry.py`).** Карта
  сопоставления строится один раз на набор требований, поэтому для всех
  элементов класса выбирается одна и та же колонка. На практике QTO выгружены
  не у всех элементов (например, Revit не пишет `Qto_WallBaseQuantities` для
  части стен), и у конкретного элемента выбранная колонка пуста — хотя габариты
  есть в `QTO_bbox::*`. Поэтому после применения карты пустые геометрические
  параметры (`RUS_Length`, `RUS_Width`, `RUS_Height`, `RUS_Thickness`,
  `RUS_Perimeter`, `RUS_Diameter`, `RUS_Area`, `RUS_NetFootprintArea`,
  `RUS_NetSideArea`, `RUS_NetSurfaceArea`, `RUS_Volume`, `RUS_GrossVolume`)
  добираются по цепочке:
  1. альтернативные колонки дампа: QTO своего класса (`Qto_WallBaseQuantities`,
     `Qto_SlabBaseQuantities`, `Qto_OpeningElementBaseQuantities`),
     `QTO_bbox::Длина_мм` / `Ширина_мм` / `Высота_мм` / `Толщина_мм` /
     `Периметр_мм`, толщина слоя `IfcMaterialLayer::LayerThickness`;
  2. расчёт по формулам класса IFC (если размеры/площади известны):
     - **стена** — толщина = ширина bbox; площадь в плане = длина × толщина;
       площадь боковой поверхности = `NetVolume / толщина` (нетто; при
       отсутствии объёма — длина × высота);
       объём = длина × толщина × высота;
     - **перекрытие/плита** — толщина = `Qto_SlabBaseQuantities::Depth`, затем
       минимальный габарит bbox; площадь = QTO-площадь или длина × ширина;
       объём = площадь × толщина; периметр = QTO-периметр, затем
       2 × (длина + ширина); длина и ширина — из QTO-габаритов
       (`Qto_SlabBaseQuantities::Length/Width`), затем `QTO_bbox::*`, а если
       габаритов нет — из площади и периметра QTO (`_slab_plan_dims_mm`: след
       приводится к прямоугольнику `a + b = P / 2`, `a × b = A`; при
       отрицательном дискриминанте — непрямоугольный след — габариты не
       восстанавливаются). Расчёт по площади и периметру нужен потому, что для
       плит с объёмом и площадью в QTO геометрия не считается (см. §4);
     - **проём** — ширина и высота = два наибольших габарита bbox (третий —
       глубина/толщина стены), ширина — больший из них; **площадь проёма — это
       площадь отверстия** (`Qto_OpeningElementBaseQuantities::Area`,
       `QTO::BaseQuantities::Area` или ширина × высота), а не площадь
       поверхности (`QTO_bbox::Площадь_поверхности_м2` учитывает боковые грани
       и завышает значение в разы). Если выгружены не все три габарита,
       ориентация неоднозначна и размеры не восстанавливаются (площадь берётся
       из QTO);
     - **лестница** — объём = длина × ширина × высота;
     - **колонна/свая** — длина (высота элемента) = вертикальный габарит
       `QTO_bbox::Высота_мм` или QTO Length; ширина/высота сечения =
       `QTO_bbox::Ширина_мм` / `QTO_bbox::Длина_мм` (горизонтальные габариты);
     - **балка/связь** (`IfcBeam`, `IfcMember`) — длина = QTO Length (для
       наклонных элементов точнее габарита), затем `QTO_bbox::Длина_мм`;
       ширина и высота сечения = меньший и средний габариты bbox
       (`QTO_bbox::Высота_мм` / `QTO_bbox::Ширина_мм`);
     - **пластина/ребро** (`IfcPlate`) — толщина = `Qto_PlateBaseQuantities::Depth`
       или `QTO_bbox::Высота_мм`, длина = `QTO_bbox::Длина_мм`.
  3. объём нетто берётся из геометрии (`QTO_geom::Объём_м3` /
     `QTO_bbox::Объём_м3`), затем считается по габаритам, и только в последнюю
     очередь используется габаритный объём bbox (`QTO_bbox::Габаритный_объём_м3`,
     завышен для некруглых элементов). Объём брутто — наоборот, габаритный
     объём bbox.
- **Проверка единиц объёма.** В выгрузках Revit объём плит иногда записан в
  литрах (площадь м² × толщина мм), из-за чего QTO-значение расходится с
  геометрией примерно в 1000 раз. Если QTO-объём (`RUS_Volume`,
  `RUS_GrossVolume`) отличается от геометрической оценки более чем в 100 раз,
  подставляется геометрическая оценка; корректные значения не меняются.
- Единицы дампа: длины `QTO::*` и `QTO_bbox::*_мм` — миллиметры, площади QTO и
  `QTO_bbox::Площадь_поверхности_м2` — м², объёмы `QTO_geom::Объём_м3` /
  `QTO_bbox::*_м3` — м³. Добранное значение приводится к единице требования
  (`attribute.unit`).
- Элементы IFC-классов, отсутствующих в `parametrs_name.json`, в результат не
  попадают; их количество и классы перечислены в отчёте (`skipped_classes`).
  Исключение — `IfcBuildingElementProxy`: если он однозначно сопоставлен
  требованиям (код МССК или полное наименование), он проверяется. На практике
  в `skipped_classes` остаются отделочные элементы (`IfcCovering`) и
  комплектующие (`IfcDiscreteAccessory`) — требований ЦИМ по конструктивным
  решениям для них в справочнике нет.
- **Что остаётся пустым и почему.** Параметры, которых нет в модели, не
  синтезируются: масса (`RUS_Weight`), процент армирования
  (`RUS_ReinforcementVolumeRatio`), стандарт (`RUS_Standard`), марки
  бетона/стали для элементов без этих свойств, номер объекта/подобъекта в
  моделях без корпусов и секций (например, этажи вида `2_этаж_основной`).
  Такие значения попадают в `missing` отчёта — это и есть результат проверки
  модели на соответствие требованиям.
- **Модели с «пустыми» сборными элементами.** У `IfcStair` часто нет
  собственной геометрии и QTO (лестница — сборка из маршей и площадок),
  поэтому её объём/габариты остаются пустыми; марши
  (`IfcStairFlight`) при этом заполняются.

### 6. Настройки

| Переменная | Назначение |
|---|---|
| `CHECK_MODEL_LLM_MODEL` | Модель Ollama для проверки (fallback: `LLM_MODEL` → `NORMS_LLM_MODEL`; в рабочей копии `qwen3.8:27b`) |
| `LLM_BASE_URL` / `OLLAMA_BASE_URL` | Адрес Ollama |
| `CHECK_MODEL_LLM_TIMEOUT` | Таймаут запроса к LLM, сек (по умолчанию 900) |
| `CHECK_MODEL_LLM_NUM_CTX` | Контекст LLM, токенов (по умолчанию 16384; большее значение заставляет Ollama перезагружать модель и замедляет сопоставление) |
| `CHECK_MODEL_USE_LLM` | `0` — отключить LLM и использовать только детерминированные правила |
| `CHECK_MODEL_CACHE_DIR` | Каталог кэша карт сопоставления (по умолчанию `cache/check_model`) |

### 7. Проверка на моделях разных проектировщиков

Функционал проверен на пяти моделях из `data/ifc_models/` (IFC4, единицы —
миллиметры), отличающихся наборами свойств, структурой этажей и составом
классов:

| Модель | Наборы свойств | Проверено / всего | Заполнено / требуется |
|---|---|---|---|
| `Алтуфьевское_шоссе_КР.ifc` | `RusSet_*` (свойства уже `RUS_*`) | 10861 / 10861 | 166132 / 192246 |
| `Варшавское_шоссе_КР.ifc` | `ExpCheck_*` (`MGE_*`) + QTO | 1725 / 1895 | 21101 / 27478 |
| `Главмосстрой_КР.ifc` | `ExpCheck_*` (`MGE_*`), невидимые марки в этажах | 18531 / 23771 | 183754 / 239756 |
| `Давыдковская_КР.ifc` | `ExpCheck_*` (`MGE_*`) + QTO | 3877 / 6046 | 47950 / 59705 |
| `Мира_2_КР.ifc` | `MGE_*`, `Pset_*Common`, `IfcMaterialLayer` | 3324 / 3336 | 29482 / 43420 |

Цифры — при отключённой LLM (`CHECK_MODEL_USE_LLM=0`), то есть на
детерминированных правилах; LLM дополнительно заполняет единичные параметры
(например, марку стали из альтернативного свойства), не ухудшая остальные.
Разница «проверено / всего» — классы, для которых в `parametrs_name.json` нет
требований (`IfcCovering`, `IfcWindow`, `IfcDoor`, `IfcRailing`,
`IfcCurtainWall`, `IfcDiscreteAccessory`, `IfcStackTerminal` и
несопоставленные `IfcBuildingElementProxy`).

---

## 14. Типовые ВОР, сверка и сметная форма

Рабочий процесс находится во вкладке **«Обработка»**. Отдельные вкладки
формирования ВОР и сравнения удалены. Остались также «Проверка ЦИМ модели»
(самостоятельный функционал, см. раздел «Проверка ЦИМ модели») и «История».

После загрузки IFC и ВОР заказчика в результатах доступны две кнопки:

- **«Сравнить с ВОР исполнителя»** — сверка сформированного перечня работ с
  исходными строками ВОР заказчика, с выводами и пояснениями.
- **«Разложить в типовую ВОР и сравнить»** — формирование обеих типовых
  ведомостей по правилам формирования и сравнение их готовых строк.

### Формирование типовых ведомостей

Источники — `IFC_исходные_параметры.json`, загруженный ВОР и документы ПОС,
ПЗ, ПД. Типовой шаблон — `data/Типовой ВОР по КР.xlsx`.

`builder.build_vor` формирует ведомость из исходных элементов IFC и
документов. `customer_vor.build_customer_vor` приводит ВОР заказчика к
типовой форме. Геометрия и бетон берутся из IFC; арматура, каркасы, выпуски,
гидроизоляция, теплоизоляция, деформационные швы и другие документные работы
сохраняют правила формирования и ссылки на источник. Заимствованный из документа
объём не считается независимой проверкой по модели.

Этот путь не требует подбора расценок и не восстанавливает объёмы из
финального перечня выбранных работ.

Стены-пилоны (`IfcWall`/`IfcWallStandardCase` с «пилон» или «колонн» в имени
или типе) трактуются как колонны: площадь опалубки считается по боковым граням
без площади низа, и QTO-величины `QTO_geom::Площадь_опалубки_м2` /
`Площадь_низа_м2` / `Площадь_торцов_м2` у них не отбрасываются (у обычных стен
эти величины по-прежнему исключаются из расчёта опалубки).

### Результаты

Два отдельных файла доступны для просмотра и скачивания:

- `Сформированный_ВОР_типовой.xlsx`;
- `ВОР_заказчика_типовой.xlsx`.

Общий `Типовая_ВОР_сверка.xlsx` содержит шесть листов:

1. «По типовому ВОР» — различающиеся строки.
2. «Типовой ВОР — все строки» — полное сравнение.
3. «Сформированная ВОР».
4. «ВОР заказчика».
5. «Сверка» — вывод и причина по каждой сравниваемой работе.
6. «Итог» — сводка проверки.

Сравнение: часть здания → раздел → подраздел → группа → работа.
Материалы сопоставляются внутри работы; пустое значение отличается от нуля.
«Расхождение» следует после объёма заказчика и равно объёму заказчика минус
сформированный объём; процент считается относительно модуля сформированного
объёма. При неполных данных разность не выдаётся за точный результат.
Сохраняются расчёты, источники, пояснения по правилам и «Обоснование ИИ».

### Сессии и скорость

При восстановлении сессии и переключении запуска восстанавливаются кнопки,
готовые отчёты, ссылки и пояснения. Режим `saved=1` читает сохранённые
результаты без нового расчёта или вызова ИИ. Старые сессии отдельных вкладок
доступны в истории для скачивания результатов.

PDF кешируется по содержимому; индекс IFC и документные строки готовятся
один раз для пары ведомостей. Готовые результаты переиспользуются при
неизменных входах, коде, шаблоне и настройках. Неудачный ответ ИИ допускает
повторную попытку.

Основной код: `src/services/vor_generation/session_typical.py`,
`builder.py`, `customer_vor.py`, `reconcile.py`, а также
`src/services/comparison_typical.py`, `comparison_layout.py` и
`comparison_ai_reason.py`.

### Отдельная сметная форма по перечню

В «Обработке» доступна кнопка «Смета по сформированному перечню».
Файл `Смета_по_сформированному_перечню.xlsx` добавляется также к результатам
новых запусков. Исходный перечень остаётся отдельным файлом.

Сметный Excel — отдельный расчёт по ТСН-2001.12 с дополнением 83.
Сформированный перечень, его подбор работ и исходные файлы не изменяются.

На одном листе представлены:
- F: базисные цены единицы из API (salary, operationOfMachines,
  costOfMaterialResources, salaryDrivers);
- G: поправочные коэффициенты, в том числе отдельный коэффициент ЗПМ;
- H: базисные зимние коэффициенты baseWinterRisePrice/baseWinterRisePriceMat;
- I: базисные суммы;
- J: индексы salaryInflRate/machinesInflRate/materialInflRate и нормы НР/СП;
- K: текущие суммы;
- L: текущие зимние коэффициенты winterRisePrice/winterRisePriceMat.

Базисная сумма = объём × базисная цена × поправка × базисный зимний коэффициент.
Текущая сумма = объём × базисная цена × поправка × текущий зимний коэффициент × индекс.
Для ЗПМ используется индекс зарплаты рабочих. Её сумма уже входит в ЭМ
и отдельно к прямым затратам не прибавляется.
НР = (ЗП + ЗПМ) × rateOverhead / 100; СП = (ЗП + ЗПМ) × rateProfit / 100
в каждом уровне цен отдельно.
Норма трудозатрат fixedTime представлена строкой ЗТР. Она умножается на объём,
поправку к труду и зимний коэффициент; индекс зарплаты к часам не применяется.
ЗТР не включается в денежные итоги.

Цены единицы и коэффициенты сохраняют исходную точность. Округляются конечные
денежные суммы; денежные ячейки всегда показывают два знака. В титуле стоимость
указана в тысячах рублей, в строках и итогах — в рублях.
Синие поля редактируемые, связанные суммы и оба уровня итогов пересчитываются.
Текущие цены curSalary/curOperationOfMachines/curCostOfMaterialResources/
curSalaryDrivers сохранены для контрольной сверки, но не участвуют
в нормативной формуле и не умножаются на индекс повторно.

При отсутствии базисной цены, индекса или объёма недостающие данные остаются
пустыми; известная сумма из перечня включается как справочная с признаком
неполноты. После изменения исходных параметров, при которых пересчёт пока
невозможен, такая сумма не переносится на новый объём автоматически.
Неизвестные нормативы НР/СП не подменяются нулём.
Комментарий к поправке содержит сборник, пункт, условие и основание применения.
Неподтверждённые условия не увеличивают стоимость автоматически.
Материалы, не включённые в расценки, и достоверность подбора работ требуют
отдельной проверки; сметный экспорт сам не дополняет состав перечня.

Одинаковые конструкции объединяются по зданию и диапазону высотных
коэффициентов перед суммированием объёмов. Разные этажи внутри одного
диапазона не создают отдельные позиции. Подбор работ по типу выполняется один
раз; объёмы, опалубка, арматура и швы суммируются по элементам группы.
Высоты над землёй и глубины сохраняются отдельными группами.
В заголовках перечня и сметы указаны диапазон, этажи и отметки работ.

Для поправок на высоту от поверхности земли применяется числовая проверка
всего интервала от нижней до верхней отметки конструкции, без решения ИИ.
Отметки извлекаются из геометрии и ObjectPlacement IFC с переводом единиц
в метры относительно нуля здания. Земля определяется разностью
IfcBuilding.ElevationOfTerrain и ElevationOfRefHeight; также поддерживается
пара MGE_ElevationOfTerrain/MGE_ElevationOfRefHeight в свойствах здания.
Для нестандартных свойств с ненулевой разностью нужны подтверждённые единицы;
равные отметки дают нулевую разность независимо от единиц.

Если нет отметки земли, геометрия не поддерживается, конструкция пересекает
границу диапазона или высота уже указана в расценке, автоматическая поправка
не применяется: в комментарии указывается причина проверки. Объём пересекающего
границу элемента не делится пропорционально его габаритной высоте — для этого
нужны реальные объёмы участков. Обоснования подтверждённых поправок содержат
отметки работ, отметку и источник земли, сборник и пункт.
При новом запуске расчёта старой сессии недостающие отметки дочитываются из
сохранённого IFC по GlobalId. Готовые старые сметы без поэтажной информации
требуют нового запуска формирования перечня; прежние суммы не распределяются
по этажам приблизительно.

---

## 15. Сравнение ведомостей объёмов работ

Отдельный режим сервиса (общий для ЦС и ИИ), доступный только через API
`/api/comparisons*` — отдельной вкладки в интерфейсе нет. Оркестратор —
`comparison_manager.ComparisonManager`; состояние сравнения хранится записью с
`session_type='comparison'` в `outputs/sessions.json`, файлы — в
`outputs/<comparison_id>/`; дочерние сессии моделей ссылаются на сравнение полем
`parent_session_id`.

Стадии (поле `stages`): `documents` → `models` → `estimates` → `comparison`.
Статусы: `models_processing` / `parameters_required` (только ИИ) / `models_ready` /
`comparing` / `input_required` / `completed` / `error`.

1. **`documents`** (только ИИ): разбор загруженных ПОС/ПЗ/ВОР/ПД тем же кодом, что в
   обычной сессии (`pd_parser`, `doc_pdf_parser`, `pd_full`, `doc_facts`).
2. **`models`**: по каждой модели создаётся обычная сессия (`process_ifc` /
   `process_pdf`) и запускается полный конвейер (`select_rows(all_rows=True)`).
   В ИИ сначала формируется превью параметров
   (`comparison_documents.resolve_constants` + `material_choices`) → статус
   `parameters_required` и `POST /api/comparisons/<id>/parameters`
   (`overrides`, `use_documents`, `material_codes`, `use_tsn`). Результат —
   `models.json` (объединённые `final_result_*` + `sourceFile`) и `model_analytics.json`.
3. **`estimates`**: разбор ВОР заказчика. PDF с текстовым слоем и XLSX — локальным
   парсером `vor_parser.parse_vor` (структура по заголовкам колонок, приоритет колонок
   «стало / после корректировки / по проекту»; при неоднозначности —
   `QuantityColumnRequired`, статус `input_required` и выбор столбца в
   `POST /api/comparisons/<id>/retry`); прочие форматы (`.xml/.zip/.gsfx/.gge`) —
   через внешний сервис разбора смет. Результат — `estimates.json`.
4. **`comparison`**: пакет `works_comparison/` (извлечение позиций, нормализация
   единиц, группировка по «нормализованный шифр + единица», сопоставление групп;
   без шифров — `semantic_matching.py` / `construction_matching.py`; LLM-валидация
   `work_group_validator.py` + `ollama_service.py`) → `result.json`; затем
   `comparison_vor.prepare_report` (`reportVersion=3`) по шаблону
   `data/comparison_vor_template.xlsx` → `result.json` / `result.xlsx`,
   диагностика — `semantic_match_diagnostics.jsonl`.

Артефакты режима: `outputs/<comparison_id>/` — `models.json`, `estimates.json`,
`model_analytics.json`, `result.json`, `result.xlsx`,
`semantic_match_diagnostics.jsonl`. Автономный запуск `processor.Processor`
по-прежнему пишет отчёт в `debug/ifc_comparison/comparison_results/result.json`.

---

## 16. Сопровождение документации

- **`README.md` — единственный источник истины** по всем режимам (`CS`, `AI`, `ARCH`)
  и вспомогательным функционалам (проверка ЦИМ модели, типовые ВОР и сверка, сметная
  форма, сравнение ВОР, формирование ВОР).
- После **каждого** изменения, затрагивающего режим или функционал (код пайплайна,
  эндпоинты, схемы, файлы результатов, справочники, параметры запуска), обновляется
  соответствующий раздел этого файла: описание шагов пайплайна, создаваемые файлы,
  используемые справочники `data/`.
- При расхождении кода и README расхождение устраняется: приводится в соответствие
  **документ** (при намеренном изменении поведения) либо **код** (при случайной
  регрессии).
- Правила разработки для ИИ-агентов и границы режимов — [`KODA.md`](./KODA.md);
  инструкция для пользователя сервиса — [`instruction.md`](./instruction.md).
- Справочники `data/` — только чтение. Новые и изменённые файлы результатов — только
  в `outputs/<session_id>/` и `outputs/<session_id>/run_<NNN>/` (имена файлов — на
  русском, как в существующем конвейере).
