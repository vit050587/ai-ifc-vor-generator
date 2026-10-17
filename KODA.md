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
> 5. **Отдельный функционал «Формирование ВОР»** (вкладка «Формирование ВОР»,
>    пакет [`src/services/vor_generation/`](./src/services/vor_generation)) **не
>    относится к режимам ЦС/ИИ** — подбор работ там не выполняется, заполняются
>    только объём и обоснование по типовому шаблону. У него **собственная
>    документация — [`readme_vor.md`](./readme_vor.md)** (источник истины); после
>    каждого изменения этого функционала обновляется `readme_vor.md`. Правила —
>    **§8.6**.
> 6. **Рабочая директория — это КОПИЯ сервиса (Flask), а не основной сервис.**
>    Разработка ведётся на этой копии; после завершения изменения переносятся
>    **вручную** в основной сервис, который работает в контейнере **Docker**
>    (см. §4.1). Учитывать это при любых правках: здесь нет автосинхронизации,
>    деплоя и CI/CD — перенос выполняет разработчик вручную.

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
    разбор ПОС/ПЗ/ВОР/ПД — LLM (`pd_parser`, `doc_pdf_parser`, `pd_full`);
    альтернативный подбор работ — по перечню сметчиков (`perechen_pipeline`).
- **Асинхронная обработка**: после загрузки файла сервис сразу возвращает `sessionId`, тяжёлая обработка выполняется в фоновых потоках (`threading.Thread`), пользователь опрашивает статус через API.
- **Два источника данных**: IFC (через `ifcopenshell`) или PDF-чертёж (через пайплайн машинного зрения `ai-blueprint-to-ifc`).
- **Хранилище без СУБД**: состояние сессий — JSON-файл `outputs/sessions.json` (потокобезопасный, атомарная запись); файлы результатов — файловая система `outputs/<session_id>/`.
- **Сравнение ведомостей объёмов работ** (отдельный режим, вкладка «Сравнение ведомостей объёмов работ»): несколько моделей/чертежей (ЦС/ИИ) + ВОР заказчика → сопоставление работ и объёмов сформированного ВОР и ВОР заказчика (`comparison_manager`, `works_comparison/`) → отчёт JSON/Excel; см. §5.5.
- **Формирование ВОР по типовому шаблону** (отдельный функционал, вкладка «Формирование ВОР», пакет `src/services/vor_generation/`): IFC-модель + документы (ВОР застройщика, ПОС, ПЗ, ПД) → заполненный по типовому шаблону `data/Типовой ВОР по КР.xlsx` файл `Сформированный_ВОР.xlsx` (заполняются только объём и обоснование; подбор работ **не** выполняется); не относится к режимам ЦС/ИИ; см. §5.6 и `readme_vor.md`.
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
│   ├── routes.py              # Все HTTP-эндпоинты (REST API, ~3200 строк)
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
│       ├── perechen_kb.py     # АР: база знаний перечня сметчиков (data/perechen_kr.xlsx) — подбор работ/расценок + LLM
│       ├── perechen_pipeline.py     # АР: финальный подбор по перечню (WORKS_SELECTOR=perechen) → Финальный_перечень_работ.json/.xlsx + analytic.json
│       ├── local_works.py / local_resources.py # АР: локальный справочник расценок/ресурсов (кеши из price_cost.xlsx)
│       ├── tsn_coeffs.py      # АР: поправочные коэффициенты сборников ТСН (data/tsn/*.json)
│       ├── pd_full.py         # АР: полный разбор ПОС/ПЗ (≈40 параметров) → pd_full.json
│       ├── ifc_features.py    # АР: признаки геометрии IFC (криволинейность/радиус, наклон, отметки, высота этажа)
│       ├── doc_facts.py       # АР: факты из ВОР/ПД (класс бетона B/W/F, класс арматуры, объёмы, сверка) 
│       ├── vor_comparison.py        # АР: сравнение работ/объёмов ВОР заказчика и сформированного перечня → Сравнение_ВОР_и_перечня.xlsx/.json
│       ├── ifc_json_builder.py      # Сборка финального JSON (final_result_AR.json / final_result_KR.json) + enrich_group_levels
│       ├── position_links.py  # Ссылки на позиции цифрового сборника (position_links.json, КР)
│       ├── pd_parser.py       # Разбор PDF ПОС/ПЗ через LLM → ПОС/ПЗ_глобальные_константы.json (+ LLMClient)
│       ├── doc_pdf_parser.py  # Разбор PDF ВОР/ПД через LLM (+ extract_vor_rows, vor_to_xlsx/pd_to_xlsx)
│       ├── works_cost.py      # Расчёт/форматирование стоимости работ финального перечня (КР и АР)
│       ├── group_excel.py     # Группировка элементов (КР: Часть здания→МССК→Материал→Тип→Геометрия; АР: Часть здания→МССК→Материал→Наименование)
│       ├── api_works_lookup.py# Подбор работ через API ТСН (КР)
│       ├── materials_lookup.py# Карта МССК-кодов материалов (АР)
│       ├── mssk_lookup.py     # Карта МССК-кодов элементов
│       ├── pdf_processor.py   # Обёртка пайплайна обработки PDF
│       ├── kb_inspect.py / kb_model.py / kb_etalon.py # Вспомогательные инструменты проверки базы знаний и эталона
│       ├── comparison_manager.py   # Сравнение ВОР: оркестратор comparison-сессий (session_type='comparison')
│       ├── comparison_documents.py # Сравнение ВОР: resolve_constants, material_choices (уточнение МССК-кодов)
│       ├── comparison_vor.py       # Сравнение ВОР: компактный отчёт сравнения (prepare_report, reportVersion=3)
│       ├── vor_parser.py           # Сравнение ВОР: локальный парсер ВОР заказчика (PDF/XLSX) по заголовкам колонок
│       ├── serializer.py      # Экспорт IFC → GLB (3D-модель по запросу)
│       ├── works_comparison/  # Пакет сравнения позиций смет заказчика с позициями IFC
│           │                  #   (нормализация единиц, группировка, сопоставление по шифру/семантике/
│           │                  #   конструкциям, LLM-валидация через Ollama, промпты prompts/);
│           │                  #   ПОДКЛЮЧЁН к сервису через comparison_manager (режим «Сравнение ВОР»)
│           ├── processor.py        # Оркестратор сравнения: позиции смет ↔ позиции IFC
│           ├── positions_extractor.py / ifc_positions_extractor.py # Извлечение позиций
│           ├── positions_processor.py / ifc_positions_processor.py # Нормализация единиц и группировка
│           ├── compare_positions_groups.py # Сравнение групп по нормализованным ключам
│           ├── semantic_matching.py / construction_matching.py # Сопоставление без шифров (LLM/правила)
│           ├── work_group_validator.py    # LLM-валидация групп (validate_material/validate_work)
│           ├── validation_result_former.py# Формирование результата → debug/ifc_comparison
│           ├── units.py / normalization_functions.py # Единицы измерения и нормализация
│           ├── ollama_service.py / config.py / utils.py и др.
│       └── vor_generation/    # Пакет «Формирование ВОР» по типовому шаблону (вкладка «Формирование ВОР»)
│           │                  #   IFC-модель + документы (ВОР застройщика/ПОС/ПЗ/ПД) → Сформированный_ВОР.xlsx;
│           │                  #   подбор работ НЕ выполняется, не относится к режимам ЦС/ИИ — см. §5.6 и readme_vor.md
│           ├── mapping.py     # Сопоставление текстов ВОР с типами конструкций/частями/геометрией, виды работ и ресурсов
│           ├── template.py    # Разбор и запись листа «ВОР КР_ЖС» типового шаблона (сохранение структуры и стилей)
│           ├── elements.py    # Индексация и агрегация элементов IFC из сырого дампа (QTO, марки, геометрия)
│           ├── documents.py   # Разбор документов: арматура из ВОР застройщика, поиск ресурсов, ПОС/ПЗ/ПД
│           ├── builder.py     # Сборка ВОР: заполнение строк работ/ресурсов, запись файла (build_vor)
│           ├── customer_vor.py # Приведение ВОР заказчика (PDF) к структуре типового ВОР → ВОР_заказчика.xlsx
│           ├── manager.py     # VorGenerationManager: жизненный цикл задачи (фон, статусы, артефакты)
│           └── __init__.py    # Экспорт VorGenerationManager, build_vor
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
├── prompts/                   # Промпты LLM: validate_material.txt, validate_work.txt —
│                              #   пакет works_comparison/ (через src/core/prompt_manager.py);
│                              #   element_analyze.txt — legacy (не используется); промпты
│                              #   основного пайплайна (pd_parser, works_final_selector) встроены в модули
│
├── data/                      # Справочники (только чтение!)
│   ├── algorithm.md                          # Спецификация 8-шагового алгоритма подбора работ (АР)
│   ├── perechen_kr.xlsx, perechen_kr_1.xlsx  # Перечни работ (КР; perechen_kr — база знаний подбора АР)
│   ├── koefs.xlsx                            # Нормы расхода (корректировка объёма, КР и АР)
│   ├── price_cost.xlsx                       # Стоимость расценок (Шифр → прямые затраты, КР; локальный справочник АР)
│   ├── ifc_to_collections.json               # Правила IfcClass (+PredefinedType) → сборники (АР)
│   ├── msck_elements_compact.json            # Дерево МССК-элементов (АР, шаг 3 подбора)
│   ├── tree_work_compact.json                # Дерево 41 сборника ГЭСН/ТСН-2001 с таблицами (АР)
│   ├── works_classification.json             # Классификация сборников + схема 10 констант (АР)
│   ├── params_registry.json                  # Реестр параметров ПОС/ПЗ для LLM-извлечения (АР; вкл. доп. константы: floor_height, soil_group, movement_distance, crane_capacity, bucket_capacity, equipment_power)
│   ├── selection_parameters.json             # Перечень параметров подбора работ из 3 главы perechen_kr_1.xlsx — шаблон для заполнения по элементам (АР)
│   ├── selection_parameters_mapping.json     # Расширяемая карта «параметр шаблона → ключи сырого дампа / ключевые слова / константы» (АР)
│   ├── tsn/                                  # Разобранные сборники ТСН (tsn_3_6_full.json и др.) — поправки (АР)
│   ├── comparison_vor_template.xlsx          # Шаблон стандартной иерархии ВОР (сравнение ВОР)
│   ├── Типовой ВОР по КР.xlsx                # Типовой шаблон ВОР, лист «ВОР КР_ЖС» (формирование ВОР, §5.6)
│   ├── catalog_tsn.json, etalon.xlsx         # Каталог ТСН, эталон проверки качества подбора
│   ├── kb_cache.json, tsn_decisions_cache.json, pd_full_cache_v2.json, doc_facts_cache.json # Кеши LLM-решений (АР)
│   ├── local_works.json, local_resources.json, local_norms.json # Кеши локального справочника расценок/ресурсов (АР)
│   ├── elements_mssk.xlsx / elements_mssk_nested.json  # МССК-справочник элементов
│   └── materials_mssk.xlsx / materials_mssk_nested.json # МССК-справочник материалов
│
├── uploads/                   # Загруженные пользователем файлы (по сессиям)
├── outputs/                   # Результаты обработки
│   ├── sessions.json          # «База» всех сессий (состояние, runs, файлы, сравнения)
│   ├── _doc_cache/            # Кеш разбора ПОС/ПЗ/ВОР/ПД по SHA-256 (служебный)
│   ├── <session_id>/          # Сессия обработки
│   │   ├── original/          # Исходные файлы (IFC/PDF, Excel, GLB, разметки)
│   │   ├── run_<NNN>/         # Результаты запуска NNN
│   │   └── ...справочные и промежуточные JSON/XLSX...
│   └── <comparison_id>/       # Сессия сравнения ВОР (session_type='comparison')
│       ├── models.json        # ВОР по каждой модели (final_result_* + sourceFile)
│       ├── estimates.json     # Разобранные ВОР заказчика
│       └── result.json / result.xlsx  # Отчёт сравнения
│   └── <job_id>/              # Задача формирования ВОР (session_type='vor_generation', §5.6)
│       ├── Сформированный_ВОР.xlsx    # Итоговый заполненный ВОР
│       ├── vor_generation.json        # Статистика сборки
│       └── IFC_исходные_параметры.json/.xlsx  # Сырой дамп модели
│
├── KODA.md                    # Настоящий файл (инструкция для агентов)
├── readme_ar.md               # Источник истины по пайплайну АР (правила §8.1–8.2)
├── readme_kr.md               # Источник истины по пайплайну КР (правила §8.3–8.4)
├── readme_vor.md              # Источник истины по функционалу «Формирование ВОР» (правила §8.6)
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

### 4.2. Локальная разработка вне Docker (рабочая копия сервиса)

> **Это рабочая КОПИЯ сервиса.** Разработка ведётся здесь (локальный Flask),
> а готовые изменения переносятся **вручную** в основной сервис, работающий
> в контейнере Docker (§4.1). Автосинхронизации, деплоя и CI/CD между копией
> и основным сервисом нет — перенос выполняет разработчик вручную.

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
  3. `works_table_selector.build_works_tables_json` — 8 шагов алгоритма (нормализация элемента, сборники-кандидаты по `ifc_to_collections.json`, уточнение по МССК, переключатель технологии, выбор таблиц по ключевым словам, правила по сборникам COMPLEX/SEPARATE, 10 констант проекта, объёмы QTO; для ж/б монолита в COMPLEX-режиме дополнительно SEPARATE-пакет отдела 1.2) → `Подобранные_таблицы_работ.json`;
  4. `works_fetcher.fetch_and_save_works` — работы из цифрового сборника (larix): актуальный период ТСН (`baseTypeCode=TSN`, «индекс» в `title`, максимальный `dateStart`) → `period.json` (корень сессии); работы по шифрам подобранных таблиц (`catalog/work-process/list`) → `Подобранные_работы.json` (в `run_<NNN>/`; ошибка API не прерывает запуск);
  5. `works_final_selector.select_final_works` (v13.2) — финальный подбор через LLM: листовые группы (разделённые по частям здания `split_leaf_groups_by_part`, строго по числовому индикатору «Этажа»); работы каждой таблицы элемента-представителя предфильтруются (анти-слова, «семейные» фильтры, часть здания, высота, геометрия) и ранжируются; LLM выбирает **ровно одну работу в каждой таблице**, fallback — ближайшая по толщине; объёмы — по правилам КР из агрегатов группы (объём/площади/длина швов/опалубка/арматура, корректировка `koefs.xlsx`); разбивка стоимости (ЗП/ЭМ/МР) — детальные параметры позиций larix → `Финальный_перечень_работ.json` + `.xlsx` (структура КР);
  6. `ifc_json_builder.build_final_json` → `final_result_AR.json`.
- **Итоговая выдача запуска** (файлы для скачивания) — в **обоих режимах только финальная таблица работ**: КР — `ОБЩИЙ_Финальный_перечень_работ.xlsx`, АР — `Финальный_перечень_работ.xlsx`; остальные артефакты (`selected_elements_grouped.json`, `final_result_*.json`, справочные JSON, промежуточные файлы) остаются на диске (`run_<NNN>/`, корень сессии).
- Опционально (АР): разбор PDF ПОС/ПЗ (`pd_parser.py` через LLM по `data/params_registry.json`) → `ПОС_глобальные_константы.json` / `ПЗ_глобальные_константы.json`; разбор PDF ВОР/ПД (`doc_pdf_parser.py`) → `ВОР_ведомость_объемов_работ.json/.xlsx`, `ПД_характеристики_элементов.json/.xlsx`; полный разбор ПОС/ПЗ (`pd_full.py`) → `pd_full.json`; факты ВОР/ПД (`doc_facts.py`). Константы (схема `works_classification.json` + доп. `floor_height`, `soil_group`, `movement_distance`, `crane_capacity`, `bucket_capacity`, `equipment_power`) подставляются в подбор работ и в шаблоны параметров. Опционально — сравнение ВОР заказчика и сформированного перечня (`vor_comparison.py`) → `Сравнение_ВОР_и_перечня.xlsx/.json`.
- Альтернативный подбор работ АР — по перечню сметчиков (`WORKS_SELECTOR=perechen`): `perechen_pipeline.py` + `perechen_kb.py` (база знаний `data/perechen_kr.xlsx`, локальный справочник расценок `price_cost.xlsx` через `local_works.py`/`local_resources.py`, поправки ТСН `tsn_coeffs.py`) → `Финальный_перечень_работ.json/.xlsx` + `analytic.json`.
- На этапе 0 (после `zero_step`) заполняется универсальный шаблон параметров подбора: `selection_template_builder.py` по каждому элементу сырого дампа (`data/selection_parameters.json` + карта `data/selection_parameters_mapping.json`) → `Параметры_подбора_элементов.json` в корне сессии.
- Все артефакты (XLSX, JSON, дампы) сохраняются в `run_<NNN>/` каждого запуска.

### 5.5. Сравнение ведомостей объёмов работ (comparison-сессии)

Отдельный режим сервиса (вкладка «Сравнение ведомостей объёмов работ», эндпоинты
`/api/comparisons*`) — общий для ЦС и ИИ. Оркестратор —
`comparison_manager.ComparisonManager`; состояние сравнения — запись с
`session_type='comparison'` в `outputs/sessions.json`, файлы — в
`outputs/<comparison_id>/`; дочерние сессии моделей ссылаются на сравнение полем
`parent_session_id`. Стадии (поле `stages`): `documents` → `models` →
`estimates` → `comparison`. Статусы: `models_processing` /
`parameters_required` (только ИИ) / `models_ready` / `comparing` /
`input_required` / `completed` / `error`.

1. **`documents`** (только ИИ): разбор загруженных ПОС/ПЗ/ВОР/ПД тем же кодом, что
   в обычной сессии (`pd_parser`, `doc_pdf_parser`, `pd_full`, `doc_facts`).
2. **`models`**: по каждой модели создаётся обычная сессия (`process_ifc` /
   `process_pdf`) и запускается полный конвейер (`select_rows(all_rows=True)`).
   В ИИ сначала формируется превью параметров
   (`comparison_documents.resolve_constants` + `material_choices`) → статус
   `parameters_required` и `POST /api/comparisons/<id>/parameters`
   (`overrides`, `use_documents`, `material_codes`, `use_tsn`). Результат —
   `models.json` (объединённые `final_result_*` + `sourceFile`),
   `model_analytics.json`.
3. **`estimates`**: разбор ВОР заказчика. PDF с текстовым слоем и XLSX —
   локальным парсером `vor_parser.parse_vor` (структура по заголовкам колонок,
   приоритет колонок «стало / после корректировки / по проекту»; при
   неоднозначности — `QuantityColumnRequired`, статус `input_required` и выбор
   столбца в `POST /api/comparisons/<id>/retry`); прочие форматы
   (`.xml/.zip/.gsfx/.gge`) — через внешний сервис разбора смет. Результат —
   `estimates.json`.
4. **`comparison`**: пакет `works_comparison/` (извлечение позиций, нормализация
   единиц, группировка по «нормализованный шифр + единица», сопоставление групп;
   без шифров — `semantic_matching.py` / `construction_matching.py`; LLM-валидация
   `work_group_validator.py` + `ollama_service.py`) → `result.json`; затем
   `comparison_vor.prepare_report` (`reportVersion=3`) по шаблону
   `data/comparison_vor_template.xlsx` → `result.json` / `result.xlsx`,
   диагностика — `semantic_match_diagnostics.jsonl`.

### 5.6. Формирование ВОР по типовому шаблону (отдельный функционал)

Не относится к режимам ЦС/ИИ (подбор работ **не** выполняется). Пакет
`src/services/vor_generation/`, оркестратор — `VorGenerationManager`; задача —
запись `session_type='vor_generation'` в `outputs/sessions.json`, файлы — в
`outputs/<job_id>/`. Вход: **IFC-модель** (обязательно) + документы
**ВОР застройщика / ПОС / ПЗ / ПД** (опционально, PDF/XLSX). Выход —
`Сформированный_ВОР.xlsx` (копия `data/Типовой ВОР по КР.xlsx`, лист
«ВОР КР_ЖС»; заполняются только «Объем» (D) и «Обоснование объема работ» (F)).
Дополнительно при загруженном PDF ВОР застройщика формируется
`ВОР_заказчика.xlsx` — ВОР заказчика, приведённый к структуре типового ВОР
(объёмы агрегируются по блокам/секциям/этажам заказчика в подразделы шаблона).
Статусы: `processing` → `completed` | `error`.

1. **Сырой дамп IFC** (`progress 5`) — `ifc_raw_dump.dump_ifc_elements_raw` →
   `IFC_исходные_параметры.json/.xlsx` (единственный источник геометрии и QTO).
   Дамп обогащается bbox-периметром/толщиной плит
   (`VorGenerationManager._enrich_slab_formwork`) для расчёта опалубки
   фундаментных плит по торцам.
2. **Разбор документов** (`progress 35`) — `documents.load_documents`: ВОР
   застройщика — детерминированно (`doc_pdf_parser.extract_vor_rows`, без LLM;
   извлекается арматура `extract_rebar`), ПОС/ПЗ — `pd_parser.parse_pos_constants`,
   ПД — `doc_pdf_parser.parse_pd`. Ошибка разбора документа не прерывает задачу.
3. **Приведение ВОР заказчика к типовому виду** (`progress 55`) — только при
   загруженном PDF ВОР застройщика: `customer_vor.build_customer_vor` →
   `ВОР_заказчика.xlsx` (структура типового ВОР, объёмы заказчика, суммированные
   по блокам/секциям/этажам). Ошибка шага не прерывает задачу.
4. **Сборка ВОР** (`progress 70`) — `builder.build_vor`: `template.parse_template`
   (разбор шаблона), `ElementIndex.from_raw_dump` (индекс элементов), заполнение
   строк (приоритет: модель IFC; арматура — только из ВОР застройщика, ВОР
   застройщика как источник прочих объёмов не используется; бетон — по марке из
   модели; опалубка фундаментных плит — по торцам (периметр × толщина);
   группировка по геометрии),
   `template.render_template` (запись в копию шаблона с сохранением структуры и
   стилей).
5. **Сохранение результата** (`progress 100`) — статистика в `vor_generation.json`
   (включая блок `customer`), статус `completed`.

Обработка — в фоновом потоке (`daemon`), статус опрашивается через API
(`GET /api/vor-generation/<job_id>`). При перезапуске сервиса незавершённые
задачи помечаются `error`. Справочник `data/Типовой ВОР по КР.xlsx` — только
чтение. Подробное описание — [`readme_vor.md`](./readme_vor.md).

---

## 6. Хранилища и справочники

Сервис **не использует классическую СУБД**.

| Хранилище | Описание |
|---|---|
| `outputs/sessions.json` | Метаданные сессий, запусков, файлов, прогресс, а также сравнения (записи `session_type='comparison'`) и задачи формирования ВОР (`session_type='vor_generation'`). Потокобезопасно (`RLock`), атомарная запись через tmp-файл + `os.replace`, резервное копирование при повреждении |
| `outputs/<session_id>/` | Файлы результатов сессии (`original/`, `run_<NNN>/`, справочники) |
| `outputs/<comparison_id>/` | Файлы сравнения ВОР: `models.json`, `estimates.json`, `model_analytics.json`, `result.json`, `result.xlsx`, `semantic_match_diagnostics.jsonl` |
| `outputs/<job_id>/` | Файлы задачи формирования ВОР: `Сформированный_ВОР.xlsx`, `vor_generation.json`, `IFC_исходные_параметры.json/.xlsx`, сохранённые входные файлы |
| `outputs/_doc_cache/` | Кеш разбора ПОС/ПЗ/ВОР/ПД по SHA-256 (служебный) |
| `uploads/<session_id>/` | Загруженные пользователем файлы |
| `data/` | Статические справочники (только чтение): `algorithm.md`, `ifc_to_collections.json`, `msck_elements_compact.json`, `tree_work_compact.json`, `works_classification.json`, `params_registry.json`, `selection_parameters*.json`, `tsn/*.json`, `comparison_vor_template.xlsx`, `Типовой ВОР по КР.xlsx` (шаблон формирования ВОР, §5.6), МССК-справочники `elements_mssk*`/`materials_mssk*`, перечни `perechen_kr*.xlsx`, `koefs.xlsx`, `price_cost.xlsx`, кеши LLM-решений и локальных расценок (`kb_cache.json`, `tsn_decisions_cache.json`, `pd_full_cache_v2.json`, `doc_facts_cache.json`, `local_*.json`) |
| **Ollama** | Локальная LLM (Qwen3-VL-8B — чертежи, YandexGPT-5-Lite-8B — разбор ПОС/ПЗ/ВОР/ПД (`pd_parser`, `doc_pdf_parser`, `pd_full`), подбор по перечню (`perechen_kb`), `OLLAMA_MODEL` — семантическое сопоставление и валидация в `works_comparison`) |
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
- Документы и константы (только ИИ): `POST /api/session/<id>/upload_pos`, `POST /api/session/<id>/upload_pz`, `POST /api/session/<id>/upload_vor`, `POST /api/session/<id>/upload_pd`, `GET /api/session/<id>/works_constants` (схема констант + автоподстановка), `GET /api/session/<id>/pd_full`, `GET /api/session/<id>/doc_facts`, `GET /api/session/<id>/analytic`, `GET /api/session/<id>/compare_vor` (сравнение ВОР и перечня), `GET /api/session/<id>/tsn_mode` / `/pd_mode` (переключатели), `GET /api/tsn/docs` (список сборников ТСН)
- Ссылки на позиции ЦС (только КР): `GET /api/session/<id>/position_links`
- Файлы: `GET /api/session/<id>/preview`, `preview_result/<filename>`, `blueprint_image`, `materials_md`, `download/<filename>`, `download_all`
- Финальный JSON запуска: `POST /api/session/<id>/run/<N>/build_final_json` (пересборка), `GET .../final_json/status`, `GET .../final_json/result`
- 3D: `POST /api/session/<id>/3d_model`, `GET /api/session/<id>/3d_model/status`
- Сравнение ВОР (общий для ЦС/ИИ): `GET/POST /api/comparisons`, `GET /api/comparisons/<id>`, `POST /api/comparisons/<id>/estimates`, `POST /api/comparisons/<id>/parameters` (ИИ), `POST /api/comparisons/<id>/saved-vor` (ИИ), `POST /api/comparisons/<id>/retry`, `GET /api/comparisons/<id>/files/<filename>` (`?download=1`)
- Формирование ВОР (отдельный функционал, §5.6): `GET/POST /api/vor-generation` (создание — multipart/form-data: `ifc` обязательно; `pos`, `vor`, `pz`, `pd` опционально), `GET /api/vor-generation/<job_id>` (статус), `GET /api/vor-generation/<job_id>/files/<filename>` (`?download=1`; допустимые имена — `Сформированный_ВОР.xlsx`, `ВОР_заказчика.xlsx`, `vor_generation.json`, `IFC_исходные_параметры.json`)

Форматы: ответы — JSON в **camelCase** (Pydantic `CamelModel`); ошибки — `{"detail": "..."}` (400/401/404/409/413/422/500); загрузка — multipart/form-data; выбор строк/запуски — JSON-тело.

---

## 8. Правила разработки

Активный режим разработки задаётся меткой в начале файла: **АР** — действуют
§8.1–8.2, **КР** — §8.3–8.4; §8.5 — общие правила для обоих режимов, §8.6 —
правила отдельного функционала «Формирование ВОР» (вне режимов ЦС/ИИ). При
переключении метки меняется только активный вариант правил.

### 8.1. Область разработки — только режим АР (действует при метке `АР`)

1. Дальнейшая разработка ведётся **исключительно в режиме АР** (архитектурные
   решения). Изменения **не должны затрагивать режим КР** (конструктивные
   решения); код КР меняется только по явному запросу пользователя.
2. **Затрагиваемые АР-модули** (`src/services/`):
   - этап 0: `zero_step.py` (АР-ветка, `ELEMENT_TYPES_AR`), `ifc_raw_dump.py`,
     `ifc_reference_builder.py`, `position_links.py`, `pdf_processor.py`;
   - этап 0: `selection_template_builder.py` (шаблон параметров подбора →
     `Параметры_подбора_элементов.json`), `ifc_features.py` (признаки геометрии);
   - этап 1: `works_table_selector.py` (ключевой модуль подбора — 8 шагов
     `data/algorithm.md`), `works_fetcher.py` (работы из цифрового сборника
     larix: период ТСН → `period.json`, работы таблиц →
     `Подобранные_работы.json`, детальные параметры позиций
     `catalog/work-process/detail`), `works_final_selector.py` (финальный
     LLM-подбор работ по группам, v13.2), `group_excel.py`
     (`process_ifc_excel_ar` / `group_elements_ar`), `materials_lookup.py`,
     `mssk_lookup.py`, `ifc_json_builder.py`;
   - альтернативный подбор (перечень сметчиков, `WORKS_SELECTOR=perechen`):
     `perechen_pipeline.py`, `perechen_kb.py`, `local_works.py`,
     `local_resources.py`, `tsn_coeffs.py`;
   - документы: `pd_parser.py` (ПОС/ПЗ), `doc_pdf_parser.py` (ВОР/ПД),
     `pd_full.py` (полный разбор ПОС/ПЗ), `doc_facts.py` (факты ВОР/ПД),
     `vor_comparison.py` (сравнение ВОР и перечня);
   - сравнение ВОР (АР-ветка): `comparison_manager.py`, `comparison_documents.py`,
     `comparison_vor.py`, `vor_parser.py`, `works_comparison/*`;
   - оркестрация: `session_manager.py` (АР-ветки `_run_processing_pipeline_in_run`),
     `routes.py` (АР-эндпоинты: `upload_pos`/`upload_pz`/`upload_vor`/`upload_pd`,
     `works_constants`, `compare_vor`, `/api/comparisons*` и т. д.),
     `schemas.py`, `templates/index.html`.
3. **«Неприкосновенное» для КР** (не менять без явного запроса):
   `api_works_lookup.py` (подбор через API ТСН), `works_cost.py` (расчёт/
   форматирование стоимости, выделен из удалённого legacy `fourth_etap`),
   КР-ветки `zero_step.py` (`ELEMENT_TYPES_KR`) и `group_excel.py`
   (`process_ifc_excel`), КР-логика `ifc_json_builder`
   (`selected_elements_grouped.json`, `final_result_KR.json`),
   `serializer.py`, справочники `perechen_kr*.xlsx`. Вне области АР также
   находится отдельный функционал «Формирование ВОР» (`src/services/vor_generation/`,
   §8.6) — не менять без явного запроса. При рефакторинге
   общего кода (`zero_step.py`, `group_excel.py`, `session_manager.py`,
   `routes.py`) поведение режима КР должно оставаться **побайтово
   неизменным** на уровне результатов.
4. **Справочники АР в `data/`** (только чтение): `algorithm.md` (спецификация
    8-шагового алгоритма — «источник истины» для `works_table_selector`),
    `ifc_to_collections.json`, `msck_elements_compact.json`,
    `tree_work_compact.json`, `works_classification.json` (включая схему
    глобальных констант), `params_registry.json`,
    `selection_parameters.json`, `selection_parameters_mapping.json`,
    `elements_mssk_nested.json`, `materials_mssk_nested.json`,
    `tsn/*.json` (поправки сборников ТСН), `comparison_vor_template.xlsx`
    (шаблон иерархии ВОР для сравнения). Файл
    `koefs.xlsx` используется АР (корректировка объёмов финального перечня в
    `works_final_selector` — те же правила, что в КР); `price_cost.xlsx`
    используется как локальный справочник расценок при подборе по перечню
    (`perechen_pipeline`), а также в КР.
5. **Артефакты АР**: новые/изменённые файлы результатов — только в
    `outputs/<session_id>/` и `outputs/<session_id>/run_<NNN>/`
    (`materials.json`, `filtered_elements.xlsx`, `Дерево_проекта*.xlsx`,
    `filtered_elements_grouped_AR.json`, `ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx`,
    `building_parts.json`, `Подобранные_таблицы_работ.json`,
    `Подобранные_работы.json` (larix, в `run_<NNN>/`),
    `Финальный_перечень_работ.json/.xlsx` (итоговая выдача — только xlsx),
    `Сравнение_ВОР_и_перечня.xlsx/.json` (сравнение работ/объёмов ВОР
    заказчика и сформированного перечня; `vor_comparison.py`),
    `period.json` (корень сессии), `final_result_AR.json`,
    `ПОС_глобальные_константы.json`, `ПЗ_глобальные_константы.json`,
    `ВОР_ведомость_объемов_работ.json/.xlsx`,
    `ПД_характеристики_элементов.json/.xlsx`, `pd_full.json`, `analytic.json`,
    `Параметры_подбора_элементов.json`). Имена файлов —
    на русском, как в существующем конвейере. Отдельный режим «Сравнение ВОР»
    ведёт собственный каталог `outputs/<comparison_id>/` (`models.json`,
    `estimates.json`, `model_analytics.json`, `result.json`, `result.xlsx`,
    `semantic_match_diagnostics.jsonl`) — см. §5.5.

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
     `filter_height`, `/api/comparisons*`), `schemas.py`;
   - сравнение ВОР (КР-ветка): `comparison_manager.py` (модели обрабатываются
     полным конвейером ЦС), `comparison_vor.py`, `vor_parser.py`,
     `works_comparison/*` (общие для ЦС и ИИ, §5.5).
3. **«Неприкосновенное» для АР** (не менять без явного запроса):
   `works_table_selector.py` (детерминированный подбор таблиц — 8 шагов
   `data/algorithm.md`), `works_fetcher.py` (цифровой сборник larix: период ТСН,
   работы таблиц, детальные параметры позиций), `works_final_selector.py`
   (финальный LLM-подбор работ по группам), `selection_template_builder.py`
   (шаблон параметров подбора), `ifc_features.py` (признаки геометрии),
   `perechen_pipeline.py` / `perechen_kb.py` / `local_works.py` /
   `local_resources.py` / `tsn_coeffs.py` (альтернативный подбор по перечню),
   `pd_parser.py` (разбор ПОС/ПЗ через LLM), `doc_pdf_parser.py` (ВОР/ПД),
   `pd_full.py` (полный разбор ПОС/ПЗ), `doc_facts.py` (факты ВОР/ПД),
   `vor_comparison.py` (сравнение ВОР и перечня), `comparison_manager.py` /
   `comparison_documents.py` / `comparison_vor.py` / `vor_parser.py` /
   `works_comparison/*` (режим «Сравнение ВОР»),
   АР-ветки `zero_step.py` (`ELEMENT_TYPES_AR`, `_classify_storey_type_ar`) и
   `group_excel.py` (`process_ifc_excel_ar` / `group_elements_ar`), АР-логика
   `ifc_json_builder` (`filtered_elements_grouped_AR.json`,
   `final_result_AR.json`), справочники `data/algorithm.md`,
   `ifc_to_collections.json`, `msck_elements_compact.json`,
   `tree_work_compact.json`, `works_classification.json`, `params_registry.json`,
   `selection_parameters.json`, `selection_parameters_mapping.json`. Вне области
   КР также находится отдельный функционал «Формирование ВОР»
   (`src/services/vor_generation/`, §8.6) — не менять без явного запроса. При
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
   `comparison_vor_template.xlsx` используется общим режимом «Сравнение ВОР»
   (§5.5) в обеих ветках.
5. **Артефакты КР**: новые/изменённые файлы результатов — только в
   `outputs/<session_id>/` и `outputs/<session_id>/run_<NNN>/`
   (`ОБЩИЙ_Финальный_перечень_работ.xlsx`, `selected_elements_grouped.json`,
   `api_works_response.json`, `api_works_cost_response.json`,
    `final_result_KR.json`, а в корне сессии — `ifc_elements_output.json`,
    `ifc_raw_elements_grouped.json`, `ifc_raw_elements.xlsx`,
    `position_links.json`). Имена файлов — на русском, как в существующем
    конвейере. Отдельный режим «Сравнение ВОР» ведёт собственный каталог
    `outputs/<comparison_id>/` (`models.json`, `estimates.json`,
    `model_analytics.json`, `result.json`, `result.xlsx`,
    `semantic_match_diagnostics.jsonl`) — см. §5.5.

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
6. **Фоновые задачи** — `threading.Thread(daemon=True)` через `SessionManager` (в функционале «Формирование ВОР» — через `VorGenerationManager`); прогресс обновляется через `_update_progress` (0–100).
7. **Справочники в `data/`** — только чтение; новые/изменённые файлы результатов — в `outputs/<session_id>/run_<NNN>/`.
8. **Промпты** — текстовые `.txt`: `prompts/` (`validate_material.txt`,
   `validate_work.txt` — пакет `src/services/works_comparison/` через
   `src/core/prompt_manager.py`; `element_analyze.txt` — legacy) и
   `ai-blueprint-to-ifc/prompts/` (для чертежей); редактировать только с
   разрешения. Промпты основного пайплайна (`pd_parser`, `works_final_selector`)
   встроены в модули.
9. **`src/services/works_comparison/`** — пакет сравнения позиций смет
   заказчика с позициями IFC (нормализация единиц, группировка, сопоставление
   по шифру/семантике/конструкциям, LLM-валидация через Ollama). **Подключён к
   сервису** через режим «Сравнение ВОР» (`comparison_manager`, §5.5); результат
   в этом режиме — `outputs/<comparison_id>/result.json` (автономный запуск
   `processor.Processor` по-прежнему пишет в
   `debug/ifc_comparison/comparison_results/result.json`). Менять только по
   явному запросу пользователя.
10. **Секреты** — `.env`, токены Keycloak, `WORKS_API_TOKEN` не выводятся в логи и в ответы.
11. **Тестирование** — тестовая инфраструктура не сформирована; при добавлении новых фич сопровождать код тестами.
12. **Git** — только просмотр статуса/логов; коммит/push — по явному разрешению.
13. **Не запускать** `docker`, `make`, `ollama` без явного запроса пользователя.
14. **Рабочая директория — КОПИЯ сервиса, а не основной сервис.** Здесь работает
    локальная копия на Flask (порт `6005`, §4.2). Основной сервис развёрнут в
    контейнере **Docker** (§4.1). После разработки на копии изменения переносятся
    в основной сервис **вручную** — автосинхронизации, деплоя и CI/CD нет.
    Учитывать при любых правках: правки в копии не попадают в Docker-сервис сами.

### 8.6. Отдельный функционал «Формирование ВОР» (вне режимов ЦС/ИИ)

1. Пакет `src/services/vor_generation/` — **самостоятельный функционал** сервиса
   (вкладка «Формирование ВОР»), **не относящийся ни к ЦС, ни к ИИ**: подбор
   работ не выполняется, заполняются только объём и обоснование по типовому
   шаблону. Изменения в нём **не должны затрагивать режимы ЦС/ИИ**, и наоборот —
   пайплайны режимов не меняются без явного запроса.
2. **Затрагиваемые модули**:
   - пакет `src/services/vor_generation/`: `mapping.py` (сопоставление текстов
     ВОР), `template.py` (разбор/запись листа «ВОР КР_ЖС»), `elements.py`
     (индекс и агрегация элементов IFC), `documents.py` (разбор документов и
     арматуры), `builder.py` (`build_vor`), `customer_vor.py`
     (`build_customer_vor` — приведение ВОР заказчика к типовому виду),
     `manager.py` (`VorGenerationManager`), `__init__.py` (экспорт);
   - оркестрация: `routes.py` (эндпоинты `/api/vor-generation*`,
     `_get_vor_generation_manager`), `templates/index.html` (вкладка
     «Формирование ВОР»: `vorTab`, `setupVorGenerationUploads`,
     `createVorGeneration`, `openVorGeneration`, `pollVorGeneration`,
     `loadVorGenerationJobs`, `restoreVorGeneration`), `schemas.py` (при наличии).
3. **Используемые общие модули** (менять их поведение нельзя — общий код):
   `ifc_raw_dump.py` (сырой дамп), `doc_pdf_parser.py` (`extract_vor_rows`,
   `parse_pd`), `pd_parser.py` (`parse_pos_constants`), `zero_step.py`
   (`classify_storey_type`), `session_manager.py` (реестр сессий
   `outputs/sessions.json`).
4. **Справочники `data/`** (только чтение): `Типовой ВОР по КР.xlsx` (лист
   «ВОР КР_ЖС»). Справочники МССК (`elements_mssk*`, `materials_mssk*`,
   `msck_elements_compact.json`) в текущей реализации не используются.
5. **Артефакты**: только в `outputs/<job_id>/` — `Сформированный_ВОР.xlsx`
   (итог), `ВОР_заказчика.xlsx` (ВОР заказчика в структуре типового ВОР; при
   загруженном PDF ВОР), `vor_generation.json` (статистика),
   `IFC_исходные_параметры.json/.xlsx`,
   сохранённые входные файлы. Задача хранится как запись
   `session_type='vor_generation'` в `outputs/sessions.json`. Подробности —
   §5.6 и [`readme_vor.md`](./readme_vor.md).
6. **Документация**: после **каждого** изменения функционала обязательно
   обновляется [`readme_vor.md`](./readme_vor.md) — он **источник истины**.
   Перед правкой нужно свериться с ним; при расхождении кода и документа
   приводится в соответствие **документ** (при намеренном изменении поведения)
   либо **код** (при случайной регрессии).

---

## 9. Частые задачи

- **Проверить статус/ошибку сессии**: `outputs/sessions.json` — поля `status`, `error`, `progress_message`; логи пишутся в консоль и в файлы.
- **Отладка отдельного модуля**: `./.venv/bin/python -m src.services.<модуль> ...` или временный скрипт внутри проекта.
- **Почему элемент не попал в перечень**: режим КР — смотреть `ifc_raw_elements_grouped.json` и `api_works_response.json` (сырые ответы API ТСН); режим АР — поле `note` элемента в `Подобранные_таблицы_работ.json` (причина, если сборники не определены), а также промежуточные файлы `run_<NNN>/`.
- **Отладка сравнения ВОР**: запись сравнения в `outputs/sessions.json` (`session_type='comparison'`, поля `status`, `stages`, `error`); файлы — `outputs/<comparison_id>/` (`models.json` — ВОР по моделям, `estimates.json` — разобранный ВОР заказчика, `result.json`/`result.xlsx` — отчёт, `semantic_match_diagnostics.jsonl` — диагностика семантического сопоставления).
- **Отладка формирования ВОР**: запись задачи в `outputs/sessions.json` (`session_type='vor_generation'`, поля `status`, `progress`, `message`, `error`); файлы — `outputs/<job_id>/` (`IFC_исходные_параметры.json` — сырой дамп модели, `vor_generation.json` — статистика сборки, `Сформированный_ВОР.xlsx` — итог). Подробности правил заполнения — [`readme_vor.md`](./readme_vor.md).
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
