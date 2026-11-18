# Инвентарь проекта (что должно быть на диаграмме)

## 1. Режимы
| Ключ | Подпись | Примечание |
|---|---|---|
| mode.CS | ЦС / цифровой сборник | ранее KR; работы через API ТСН, LLM не используется |
| mode.AI | ИИ / искусственный интеллект | ранее AR; детерминированный подбор + локальная LLM |
| mode.ARCH | Архитектура | только архитектурные элементы; свои копии модулей |

## 2. Источники
- **IFC** — `ifcopenshell`, Property Sets, QTO, геометрия, bbox.
- **PDF** — YOLO (стены/layout/легенда) → DINO (штриховки) → VLM (материалы).

## 3. Этапы
| Этап | Модуль | Вход | Выход |
|---|---|---|---|
| Загрузка | `routes.upload_ifc` | IFC/PDF | sessionId |
| Извлечение IFC | `zero_step` + `ifc_raw_dump` | IFC | `ДЛЯ_СМЕТЧИКА_исправленный.xlsx` |
| Извлечение PDF | `pdf_processor` / `ai-blueprint-to-ifc` | PDF | Excel + PNG + materials.md |
| Нормализация | `ifc_raw_dump` | элементы | размеры, материалы, этажность |
| Группировка | `group_excel` | строки | дерево проекта, группы |
| Выбор строк | `select_rows` | rowIndices | запуск |
| Подбор работ ЦС | `api_works_lookup` | группы | `api_works_response.json` |
| Подбор работ ИИ | `works_table_selector` → `works_final_selector` | группы | `Подобранные_работы.json` |
| Подбор работ ARCH | `services/architecture/*` | архитектурные элементы | `final_result_ARCH.json` |
| Объёмы | `koefs.xlsx` | группы | скорректированные объёмы |
| Стоимость | `price_cost.xlsx` | работы | стоимость |
| Excel | `works_cost` / `group_excel` | перечень | `ОБЩИЙ_Финальный_перечень_работ.xlsx` |
| 3D | `serializer` | IFC | `3Dmodel.glb` (по запросу) |
| Проверка ЦИМ | `check_model/*` | IFC | `Проверка_ЦИМ_модели.xlsx` |
| Типовые ВОР | `vor_generation/session_typical.py` | IFC + документы | две ведомости + сверка |
| Сверка ВОР | `comparison_*` | ВОР заказчика + перечень | `Сравнение_ВОР_и_перечня.xlsx` |

## 4. Артефакты
`IFC_исходные_параметры.json/.xlsx`, `ifc_elements_output.json`,
`ifc_raw_elements_grouped.json`, `ДЛЯ_СМЕТЧИКА_исправленный.xlsx`,
`Дерево_проекта*.xlsx`, `ДЛЯ_СМЕТЧИКА_сгруппированный.xlsx`,
`ОБЩИЙ_Финальный_перечень_работ.xlsx`, `final_result_KR.json`,
`final_result_AR.json`, `final_result_ARCH.json`, `3Dmodel.glb`,
`blueprint_painted.png`, `materials_colors.md`, `ПОС_глобальные_константы.json`,
`ПЗ_глобальные_константы.json`, `pd_full.json`, `Сравнение_ВОР_и_перечня.xlsx`.

## 5. API (основное)
`/api/upload_ifc`, `/api/check_model`, `/api/sessions`,
`/api/session/<id>/status`, `/select_rows`, `/new_run`, `/runs`,
`/switch_run/<run_id>`, `/filter_height`, `/works_constants`,
`/upload_pos|pz|vor|pd`, `/pd_full`, `/doc_facts`, `/analytic`, `/compare_vor`,
`/tsn_mode`, `/pd_mode`, `/build_final_json`, `/final_json/status`,
`/download/<filename>`, `/download_all`, `/3d_model`, `/estimate`,
`/typical_vor`.

## 6. Модели
YOLO walls/layout/legend, DINO hatching, Qwen3-VL-8B (Ollama),
YandexGPT-5-Lite-8B (NORMS_LLM_MODEL), `qwen3.8:27b` (check_model),
ifcopenshell.

## 7. Внешние системы
Ollama (локальный inference), API цифрового справочника ТСН
(`normativ.mgexp.org`), Keycloak (`normativ-idm.mgexp.org`).

## 8. Данные
`perechen_kr.xlsx`, `koefs.xlsx`, `price_cost.xlsx`,
`ifc_to_collections.json`, `msck_elements_compact.json`,
`tree_work_compact.json`, `works_classification.json`,
`elements_mssk_nested.json`, `materials_mssk_nested.json`,
`selection_parameters.json`, `tsn/*.json`, `parametrs_name.json`.

---

Этот файл — не догма, а стартовая карта. Если README добавляет сущность —
добавь её сюда и в диаграмму; если убирает — помечай как `deprecated`, а не
удаляй сразу.