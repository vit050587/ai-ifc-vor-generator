"""Полное извлечение из ПОС/ПЗ всего, что влияет на подбор работ и объёмы.
CLI: python -u -m src.services.pd_full [ПОС.pdf] [ПЗ.pdf]
"""
import os, re, sys, json, hashlib, time
import fitz, requests

LLM_URL = os.getenv("LLM_BASE_URL", "http://ollama:11434").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.8:27b")
CACHE = os.getenv("PD_FULL_CACHE_PATH", "/app/data/pd_full_cache_v2.json")

# group: (документы, [(id, название, ключевые слова regex)])
PARAMS = {
 "Бетонные работы": ("ПОС", [
  ("concrete_supply_under", "Подача бетона в ПОДЗЕМНОЙ части (бетононасос / кран-бадья)", r"бетононасос|бадь|подач\w* бетон"),
  ("concrete_supply_above", "Подача бетона в НАДЗЕМНОЙ части (бетононасос / кран-бадья)", r"бетононасос|бадь|подач\w* бетон|надземн"),
  ("formwork", "Тип опалубки", r"опалуб"),
  ("winter_method", "Зимнее бетонирование: способ (электропрогрев, греющий провод, термос, тепляк, добавки)", r"электропрогрев|греющ\w* провод|термос|тепляк|противоморозн|зимн\w* бетон|прогрев"),
  ("curing", "Уход за бетоном", r"уход\w* за бетон|укрыти|увлажн"),
  ("rebar_join", "Соединение арматуры (вязка / сварка / муфты)", r"муфт|вязк\w* арматур|сварк\w* арматур|стык\w* арматур"),
  ("concrete_source", "Бетон: товарный с завода / приготовление на площадке, дальность доставки", r"товарн\w* бетон|бетонн\w* завод|автобетоносмесит|миксер"),
 ]),
 "Механизмы": ("ПОС", [
  ("crane", "Кран: тип, марка, грузоподъёмность, вылет", r"кран|КБ-|Liebherr|Potain"),
  ("hoist", "Подъёмники (грузопассажирские, мачтовые)", r"подъёмник|подъемник"),
  ("pump", "Бетононасос: марка, производительность", r"бетононасос|автобетононасос"),
 ]),
 "Земляные работы": ("ПОС", [
  ("pit", "Котлован: способ разработки, глубина, откосы", r"котлован|разработк\w* грунт|экскаватор"),
  ("pit_support", "Крепление котлована (шпунт, стена в грунте, распорки, анкеры)", r"шпунт|стен\w* в грунте|распор|анкер|крепл\w* котлован"),
  ("dewatering", "Водопонижение / водоотлив", r"водопониж|водоотлив|иглофильтр|дренаж"),
  ("backfill", "Обратная засыпка: грунт, уплотнение", r"обратн\w* засыпк|уплотнен"),
  ("soil_haul", "Вывоз грунта: объём, расстояние, отвал", r"вывоз\w* грунт|отвал|полигон|транспортир\w* грунт"),
 ]),
 "Прочие работы по ПОС": ("ПОС", [
  ("scaffold", "Леса и подмости (фасадные леса, вышки)", r"леса|подмост|вышк"),
  ("safety_net", "Защитные сетки, ограждения, козырьки", r"сетк|огражд|козыр"),
  ("temp_roads", "Временные дороги и площадки", r"временн\w* дорог|плит\w* ПАГ|временн\w* проезд"),
  ("season", "Сроки и сезонность работ (начало, продолжительность, зимний период)", r"продолжительност|срок\w* строит|зимн\w* период|календарн"),
 ]),
 "Конструкции": ("ПЗ,ПОС", [
  ("concrete_class", "Классы бетона B, W, F по конструкциям", r"\b[BВ]\s?[1-6]\d\b|\bW\s?\d|\bF\s?\d{2,3}|класс\w* бетон"),
  ("rebar", "Арматура: класс, диаметры, расход", r"арматур|A500|А500|A240|А240"),
  ("cover", "Защитный слой бетона", r"защитн\w* сло"),
  ("thickness", "Толщины плит, стен, сечения колонн", r"толщин|сечени"),
  ("foundation", "Фундамент: тип, толщина плиты, отметки", r"фундамент|фундаментн\w* плит|ростверк"),
  ("piles", "Сваи: тип, диаметр, длина", r"сва[ийя]"),
  ("prep", "Бетонная подготовка: класс, толщина", r"подготовк"),
  ("frame", "Конструктивная схема (каркас, стены, шаг колонн)", r"конструктивн\w* схем|каркас|шаг колонн"),
 ]),
 "Слои и отделка": ("ПЗ", [
  ("waterproof", "Гидроизоляция: тип, материал, число слоёв", r"гидроизол|мембран|Техноэласт|битумн"),
  ("insulation", "Утеплитель: материал, толщина", r"утепл|теплоизол|минераловат|пенополистир|XPS|ЭППС"),
  ("roof", "Кровля: тип, состав", r"кровл"),
  ("floors", "Полы: конструкция, стяжка", r"пол[ыа]\b|стяжк"),
  ("facade", "Фасад: тип (НВФ, штукатурный, облицовка)", r"фасад|облицов|НВФ|вентилир"),
  ("masonry", "Кладка: материал, толщина", r"кладк|кирпич|газобетон|блок"),
  ("lintels", "Перемычки", r"перемычк"),
  ("fireproof", "Огнезащита конструкций", r"огнезащит"),
  ("joints", "Деформационные швы", r"деформационн|шов|швы"),
 ]),
 "Общее": ("ПЗ,ПОС", [
  ("height", "Высота здания, м", r"высот\w* здани|высот\w* объект"),
  ("storeys", "Этажность, подземные этажи", r"этажн|подземн\w* эт"),
  ("floor_height", "Высота этажей", r"высот\w* этаж"),
  ("soils", "Грунты основания", r"грунт\w* основани|геолог|суглин|песок|глин"),
  ("groundwater", "Уровень грунтовых вод", r"грунтов\w* вод|УГВ"),
 ]),
}


USE = {
 "concrete_supply_under": "note:насос ставится только где он есть в ТСН (фундаменты — 3.6-73)", "concrete_supply_above": "note:насос по ТСН есть только для зданий выше 105 м (3.6-110/3.6-128), иначе кран-бадья", "curing": "calc", "season": "info", "height": "calc",
 "winter_method": "note:в ТСН нет отдельной расценки на прогрев — учитывается зимним удорожанием",
 "pit": "other", "pit_support": "other", "dewatering": "other", "backfill": "other", "soil_haul": "other",
 "scaffold": "other", "safety_net": "other", "temp_roads": "other",
}


def pages(path):
    d = fitz.open(path)
    return [d[i].get_text() for i in range(len(d))]


def snippets(docs, kw, limit=9000):
    """docs: {'ПОС': [texts], 'ПЗ': [...]} -> текст фрагментов с пометкой [ПОС стр.N]"""
    rx = re.compile(kw, re.I)
    hits = []
    for name, pgs in docs.items():
        for i, t in enumerate(pgs):
            n = len(rx.findall(t))
            if n:
                hits.append((n, name, i + 1, t))
    hits.sort(key=lambda h: -h[0])
    out, size = [], 0
    for n, name, pg, t in hits[:8]:
        parts = []
        for m in list(rx.finditer(t))[:4]:
            a, b = max(0, m.start() - 350), min(len(t), m.end() + 350)
            parts.append(t[a:b].replace("\n", " "))
        chunk = f"[{name} стр.{pg}] " + " … ".join(parts)
        if size + len(chunk) > limit:
            break
        out.append(chunk); size += len(chunk)
    return "\n\n".join(out)


def ask(prompt):
    r = requests.post(f"{LLM_URL}/api/chat", json={
        "model": LLM_MODEL, "stream": False, "think": False, "format": "json",
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 2048},
    }, timeout=600)
    r.raise_for_status()
    txt = r.json()["message"]["content"]
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else {}


def extract(pos_path=None, pz_path=None, log=print):
    docs = {}
    if pos_path and os.path.isfile(pos_path): docs["ПОС"] = pages(pos_path)
    if pz_path and os.path.isfile(pz_path): docs["ПЗ"] = pages(pz_path)
    key = hashlib.md5("|".join(f"{k}:{sum(map(len, v))}" for k, v in docs.items()).encode()).hexdigest()
    try: cache = json.load(open(CACHE, encoding="utf-8"))
    except Exception: cache = {}
    if key in cache:
        return cache[key]
    result = []
    for group, (where, items) in PARAMS.items():
        use = {k: v for k, v in docs.items() if k in where.split(",")}
        texts, todo = [], []
        for pid, title, kw in items:
            sn = snippets(use, kw, limit=3000)
            if sn:
                texts.append(f"### {pid}: {title}\n{sn}"); todo.append((pid, title))
            else:
                result.append({"group": group, "id": pid, "title": title, "value": "", "src": "", "page": None, "quote": "", "use": USE.get(pid, "info")})
        if not todo:
            continue
        t0 = time.time()
        prompt = (
            "Ты инженер-сметчик. Ниже фрагменты проектной документации (ПОС — проект организации строительства, "
            "ПЗ — пояснительная записка), сгруппированные по параметрам. Для КАЖДОГО параметра выпиши кратко "
            "(до 150 символов) что указано в документе, влияющее на состав строительных работ и их объёмы. "
            "Только факты из текста, ничего не придумывай. Если в фрагментах нет ответа — value пустая строка.\n"
            "ВАЖНО: речь только о строительных конструкциях здания (монолит, фундамент, стены, перекрытия, кровля, фасад) "
            "и организации их возведения. Если фрагмент про инженерные сети, воздуховоды, кабели, каналы теплосети, "
            "благоустройство или снос — это НЕ ответ, value пустая строка.\n"
            "Ответ строго JSON: {\"<id>\": {\"value\": \"...\", \"doc\": \"ПОС|ПЗ\", \"page\": N, \"quote\": \"дословная цитата до 150 символов\"}, ...}\n\n"
            + "\n\n".join(texts))
        try:
            ans = ask(prompt)
        except Exception as e:
            log(f"  ! {group}: ошибка LLM {e}"); ans = {}
        for pid, title in todo:
            a = ans.get(pid) or {}
            result.append({"group": group, "id": pid, "title": title,
                           "value": str(a.get("value") or "").strip(), "src": a.get("doc") or "",
                           "page": a.get("page"), "quote": str(a.get("quote") or "")[:200], "use": USE.get(pid, "info")})
        log(f"  {group}: {sum(1 for p,_ in todo if (ans.get(p) or {}).get('value'))}/{len(items)} найдено, {time.time()-t0:.0f} с")
    cache[key] = result
    try: json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception: pass
    return result


if __name__ == "__main__":
    pos = sys.argv[1] if len(sys.argv) > 1 else "/app/uploads/docs/ПОС.pdf"
    pz = sys.argv[2] if len(sys.argv) > 2 else "/app/uploads/docs/ПЗ.pdf"
    res = extract(pos, pz)
    json.dump(res, open("/app/outputs/pd_full.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    found = [r for r in res if r["value"]]
    print(f"\nНАЙДЕНО {len(found)} из {len(res)}  (полностью: outputs/pd_full.json)")
    g = None
    for r in res:
        if r["group"] != g:
            g = r["group"]; print(f"\n== {g}")
        v = r["value"][:70] if r["value"] else "— нет в документах"
        s = f" ({r['src']} с.{r['page']})" if r["value"] else ""
        print(f"  {r['title'][:38]:<38} {v}{s}")
