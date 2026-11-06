#!/bin/bash
# Добавить сборник(и) ТСН:  bash tsn_add.sh ["uploads/tsn/<файл сборника>.pdf"]
# Без аргументов обрабатываются все PDF из папки uploads/tsn/
#
# ВАЖНО про пути в контейнере web: WORKDIR=/app, код — /app/src, справочники — /app/data.
# /app/uploads смонтирован из КАТАЛОГА ОСНОВНОГО СЕРВИСА, а не из этой рабочей копии,
# поэтому PDF сборников скрипт копирует в контейнер сам (в /tmp/tsn_in).
#
# Парсер сборников (src/tools/tsn) не входит в образ основного сервиса, поэтому скрипт
# каждый раз копирует его из этой рабочей копии в контейнер (docker cp). Копия живёт до
# пересоздания контейнера (up --build / rm); чтобы закрепить — добавьте в docker-compose.yml
# сервиса web том "./src/tools:/app/src/tools:ro" и pdfplumber в requirements.txt.

cd "$(dirname "$(readlink -f "$0")")" || exit 1

if [ -n "$1" ]; then
    files=("$1")
else
    shopt -s nullglob
    files=(uploads/tsn/*.pdf)
    shopt -u nullglob
fi

[ ${#files[@]} -gt 0 ] || { echo "нет PDF в папке uploads/tsn/ (положите файлы сборников туда)"; exit 1; }

for f in "${files[@]}"; do
    [ -f "$f" ] || { echo "нет файла: $f  (положите PDF в папку uploads/tsn/)"; exit 1; }
done
[ -d src/tools/tsn ] || { echo "нет каталога src/tools/tsn (парсер сборников ТСН)"; exit 1; }

CID=$(sudo docker-compose ps -q web) || exit 1
[ -n "$CID" ] || { echo "ОШИБКА: контейнер web не запущен (sudo docker-compose up -d)"; exit 1; }

# 1. Парсер сборников в контейнер (его нет в образе сервиса)
sudo docker exec "$CID" mkdir -p /app/src/tools
sudo docker cp src/tools/. "$CID":/app/src/tools/ \
    || { echo "ОШИБКА: не удалось скопировать src/tools в контейнер"; exit 1; }

# 2. pdfplumber в контейнере (нужен парсеру; в образе может отсутствовать)
sudo docker exec "$CID" sh -c "python -c 'import pdfplumber' 2>/dev/null || pip install -q pdfplumber" \
    || { echo "ОШИБКА: не удалось установить pdfplumber в контейнере web"; exit 1; }

# 3. Разбор каждого сборника (PDF копируется в контейнер: /app/uploads смонтирован
#    из основного сервиса и этих файлов не содержит)
IN=/tmp/tsn_in
sudo docker exec "$CID" mkdir -p "$IN"
for f in "${files[@]}"; do
    echo "=== Обработка: $f ==="
    sudo docker cp "$f" "$CID":"$IN"/ \
        || { echo "ОШИБКА: не удалось скопировать $f в контейнер"; exit 1; }
    sudo docker exec "$CID" sh -c "mkdir -p /app/data/tsn && cd /app/src/tools/tsn && rm -rf out && python build.py '$IN/$(basename "$f")' && cp out/tsn_3_*_full.json /app/data/tsn/ && ls /app/data/tsn" \
        || { echo "ОШИБКА: не удалось обработать $f"; exit 1; }
done
sudo docker exec "$CID" rm -rf "$IN"

sudo docker exec "$CID" sh -c "rm -f /app/data/tsn_decisions_cache.json"

# 4. Копируем разобранные сборники на хост (data/ не смонтирован в контейнер,
#    поэтому результат иначе останется только в слое контейнера)
mkdir -p data/tsn
sudo docker cp "$CID":/app/data/tsn/. data/tsn/ \
    || { echo "ОШИБКА: не удалось скопировать сборники в data/tsn/ на хосте"; exit 1; }
echo "Сборники на хосте (data/tsn/):"
ls -1 data/tsn/

# 5. Перезапуск, чтобы сервис перечитал сборники и сбросил кеш решений
sudo docker-compose restart web >/dev/null && echo "ГОТОВО: сборник(и) добавлен(ы), программа перезапущена"
