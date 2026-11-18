# Формат .drawio (выжимка)

## Минимальный файл

```xml
<mxfile host="Electron" modified="2026-01-01T00:00:00.000Z" version="26.0.0">
  <diagram id="page-1" name="Page-1">
    <mxGraphModel dx="0" dy="0" grid="1" gridSize="10" guides="1"
                  tooltips="1" connect="1" arrows="1" fold="1"
                  page="1" pageScale="1" pageWidth="850" pageHeight="1100"
                  math="0" shadow="0">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
        <!-- вершины и рёбра, parent="1" -->
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
```

## Сжатые листы

draw.io по умолчанию сохраняет лист как base64 + raw deflate + URL-encode
в тексте `<diagram>`. `drawio_io.load()` распаковывает такой payload и
встраивает `<mxGraphModel>` обратно в дерево, чтобы правки можно было
сохранить. `drawio_io.save()` всегда пишет **несжатый** XML — его читает и
draw.io, и человек.

## Атрибуты-комментарии узла (sync*)

Комментарии синхронизации хранятся в атрибутах `mxCell`, а не в `value`:
вид диаграммы не меняется, а привязка к README остаётся машиночитаемой.

```xml
<mxCell id="n7" value="2. Извлечение элементов" style="rounded=1;…"
        vertex="1" parent="1"
        syncKey="cs-stage0-extract-ifc"
        syncComment="Этап 0 режима ЦС: zero_step разбирает IFC …"
        syncAliases="Извлечение элементов | Этап 0 | zero_step"
        syncSource="§10 Режим ЦС / 5.1. Этап 0"
        syncAt="2026-11-18T13:20:00+00:00">
  <mxGeometry x="120" y="80" width="200" height="60" as="geometry" />
</mxCell>
```

| Атрибут | Назначение |
|---|---|
| `syncKey` | стабильный идентификатор сущности из `docs/readme-blocks.json` |
| `syncComment` | комментарий LLM — **единственный** источник сопоставления |
| `syncAliases` | алиасы через ` \| ` (варианты подписи, имена файлов/модулей) |
| `syncSource` | раздел и строка README (доказательство) |
| `syncAt` | время простановки (UTC, ISO-8601) |

Пишутся командой `sync_check.py annotate`; читаются командами
`sync_check.py` (все) и `drawio_io.py annotations`.

Проверки, которые выполняет `drawio_io.validate()`: первые две ячейки `0` и
`1`, уникальность `id`, существование `parent`/`source`/`target`, полная
`<mxGeometry as="geometry">` у вершин, отсутствие `vertex=1` и `edge=1`
одновременно.
