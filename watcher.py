"""Vigía Líbano: vigila fuentes sobre Marjayoun y UNIFIL y avisa por Discord.

Solo usa la biblioteca estándar. Variables de entorno:
  DISCORD_WEBHOOK_URL  webhook de Discord (sin ella, modo prueba: imprime y no envía)
  DISCORD_WEBHOOK_CONTEXT  opcional: otro canal para las noticias de contexto
  RADIUS_KM            radio de interés alrededor de la base (por defecto 15)
  MAX_AGE_HOURS        antigüedad máxima de una noticia (por defecto 12)
  MAX_PER_RUN          avisos máximos por ejecución (por defecto 12)
"""
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).parent
STATE_FILE = HERE / "state.json"
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
WEBHOOK_CONTEXT = os.environ.get("DISCORD_WEBHOOK_CONTEXT", "").strip()
RADIUS_KM = float(os.environ.get("RADIUS_KM", "15"))
MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS", "12"))
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "12"))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
NOW = dt.datetime.now(dt.timezone.utc)

CFG = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))
GEO = json.loads((HERE / "places.json").read_text(encoding="utf-8"))
BASE = GEO["base"]
LEVELS = CFG["_niveles"]

# --- Términos -----------------------------------------------------------------

# Mención inequívoca de la misión de la ONU en Líbano o de la base española.
UN_STRICT = re.compile(
    r"unifil|finul|اليونيفيل|يونيفيل|اليونفيل|قوات الطوارئ الدولية|miguel de cervantes"
    r"|الكتيبة الإسبانية|الكتيبة الاسبانية|الوحدة الإسبانية|الوحدة الاسبانية",
    re.I,
)
# Mención genérica de cascos azules: solo cuenta si además se habla de Líbano.
UN_LOOSE = re.compile(
    r"peacekeep|blue helmets?|cascos? azul(?:es)?|casques? bleus?|حفظ السلام|القبعات الزرق"
    r"|spanish (?:troops|soldiers?|battalion|contingent)|(?:soldados?|militares|contingente) español",
    re.I,
)
LEB_CUE = re.compile(
    r"lebanon|lebanese|l[íi]bano|liban\b|libanais|hezbollah|hezbol[áa]|لبنان|حزب الله|مرجعيون|النبطية"
    r"|الجنوب|جنوبي|marjayoun|marjeyoun|nabatieh|litani|الليطاني",
    re.I,
)
OTHER_CUE = re.compile(
    r"غزة|القطاع|نازح|النازحين|الضفة|مستوطن|الأونروا|gaza|west bank|cisjordania|syria|سوريا|حمص|algeria|الجزائر",
    re.I,
)
DISTRICT_CUE = re.compile(
    r"قضاء مرجعيون|marj[ae]youn district|district of marj[ae]youn|distrito de marjay[úu]n|caza de marj|caza of marj",
    re.I,
)
# Palabras que indican un hecho sobre el terreno (frente a noticias políticas o civiles).
INCIDENT = re.compile(
    r"air ?strike|strike[sd]?\b|\braid|shell(?:ed|ing|s)?\b|bomb|blast|explosi|detonat|drone|gunfire|\bfire[ds]?\b|\bfiring"
    r"|attack|kill|wound|injur|\bdead\b|clash|incursion|\btanks?\b|artillery|missile|rocket|evacuat|demoli|destroy"
    r"|\bpatrol|ambush|sniper|troops (?:enter|advance)"
    r"|ataque|bombarde|\bdron|disparo|muert|herid|proyectil|misil|cohete|incursi|tanque|artiller|evacua|patrulla"
    r"|frappe|d[ée]tonation|\btirs?\b|tu[ée]s?\b|bless[ée]|attaque|\bobus\b|\bchars?\b|patrouille"
    r"|غارة|غارتان|غارتين|غارات|قصف|تفجير|انفجار|مسي[ّ]?رة|درون|استهدف|استهداف|شهيد|شهداء|جريح|جرحى|إصابة|اصابة"
    r"|قتيل|مقتل|رشقات|رصاص|قذيفة|قذائف|مدفعي|توغل|دبابة|دبابات|نسف|إخلاء|إنذار|انذار|تمشيط|صاروخ|اعتداء"
    r"|إطلاق نار|اطلاق نار|تحليق|محلقة|قنبلة|دورية",
    re.I,
)
AR_CHARS = re.compile(r"[֐-ۿ]")


def _alias_re(alias):
    if AR_CHARS.search(alias):
        return re.compile(re.escape(alias))
    pat = re.escape(alias).replace(r"\-", r"[\s\-]?").replace(r"\ ", r"[\s\-]?")
    return re.compile(r"(?<![\w])" + pat + r"(?![\w])", re.I)


for _p in GEO["places"]:
    _p["_re"] = [_alias_re(a) for a in _p.get("aliases", [])]
    _p["_re_amb"] = [_alias_re(a) for a in _p.get("ambiguous_aliases", [])]

# --- Geografía ----------------------------------------------------------------


def distance_km(lat, lon):
    r = 6371.0
    p1, p2 = math.radians(BASE["lat"]), math.radians(lat)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon - BASE["lon"]) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing(lat, lon):
    """Rumbo desde la base hacia el lugar, en 8 puntos cardinales."""
    p1, p2 = math.radians(BASE["lat"]), math.radians(lat)
    dl = math.radians(lon - BASE["lon"])
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return ["N", "NE", "E", "SE", "S", "SO", "O", "NO"][int((deg + 22.5) // 45) % 8]


for _p in GEO["places"]:
    _p["_km"] = distance_km(_p["lat"], _p["lon"])
    _p["_dir"] = bearing(_p["lat"], _p["lon"])


def find_places(text, lebanese_source):
    has_leb = bool(LEB_CUE.search(text))
    has_other = bool(OTHER_CUE.search(text))
    found = []
    for p in GEO["places"]:
        hit = any(r.search(text) for r in p["_re"])
        if not hit and p["_re_amb"] and any(r.search(text) for r in p["_re_amb"]):
            # Nombres que también significan otra cosa ("الخيام" = las tiendas de campaña).
            hit = (has_leb or lebanese_source) and not has_other
        if hit:
            found.append(p)
    # "Distrito de Marjayoun" no es la ciudad: si hay un pueblo concreto, manda el pueblo.
    if len(found) > 1 and DISTRICT_CUE.search(text):
        found = [p for p in found if not p.get("district_name")]
    return sorted(found, key=lambda p: p["_km"])


# --- Descarga y lectura de fuentes -------------------------------------------


def fetch(url, data=None, headers=None, timeout=20, method=None):
    h = {"User-Agent": UA, "Accept-Language": "en,es;q=0.8,ar;q=0.7"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def strip_tags(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(s)).strip()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(raw):
    items = []
    root = ET.fromstring(raw)
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "body": "", "date": None, "publisher": ""}
        for ch in el:
            t = _local(ch.tag)
            txt = (ch.text or "").strip()
            if t == "title":
                d["title"] = strip_tags(txt)
            elif t == "link":
                d["link"] = d["link"] or ch.attrib.get("href") or txt
            elif t in ("description", "summary", "content", "encoded") and not d["body"]:
                d["body"] = strip_tags(txt)[:1500]
            elif t in ("pubdate", "published", "updated", "date") and not d["date"]:
                d["date"] = parse_date(txt)
            elif t == "source":
                d["publisher"] = txt
        if d["title"]:
            items.append(d)
    return items


TG_BLOCK = re.compile(r'data-post="([^"]+)"(.*?)(?=data-post="|\Z)', re.S)
TG_TEXT = re.compile(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
TG_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"')


def parse_telegram(raw):
    items = []
    page = raw.decode("utf-8", "replace")
    for post, block in TG_BLOCK.findall(page):
        m = TG_TEXT.search(block)
        if not m:
            continue
        text = strip_tags(m.group(1))
        if not text:
            continue
        times = TG_TIME.findall(block)
        first = text.split("\n")[0].strip()
        items.append({
            "title": first[:240] if len(first) > 25 else text.replace("\n", " ")[:240],
            "link": "https://t.me/" + post,
            "body": text[:1500],
            "date": parse_date(times[-1]) if times else None,
            "publisher": "",
        })
    return items


def classify_publisher(name):
    low = name.lower()
    for rule in CFG["publishers"]:
        if any(m in low for m in rule["match"]):
            return rule["level"], rule["origin"]
    return "?", "Vía Google News"


def collect():
    out, errors = [], []
    for src in CFG["sources"]:
        try:
            raw = fetch(src["url"])
            rows = parse_telegram(raw) if src["kind"] == "telegram" else parse_feed(raw)
        except Exception as e:  # una fuente caída no detiene al resto
            errors.append(f"{src['id']}: {type(e).__name__} {e}")
            continue
        for r in rows:
            r["src"] = src
            r["level"], r["origin"], r["source_name"] = src["level"], src["origin"], src["name"]
            if src["kind"] == "gnews":
                pub = r["publisher"]
                if not pub and " - " in r["title"]:
                    pub = r["title"].rsplit(" - ", 1)[1]
                if pub and r["title"].endswith(" - " + pub):
                    r["title"] = r["title"][: -len(pub) - 3]
                r["level"], r["origin"] = classify_publisher(pub)
                r["source_name"] = (pub or "medio desconocido") + " (vía Google News)"
                r["body"] = ""  # el resumen de Google News solo repite el titular
            out.append(r)
        print(f"  {src['id']:14s} {len(rows):3d} entradas")
    return out, errors


# --- Clasificación ------------------------------------------------------------

# Titulares de política, diplomacia o declaraciones: van al resumen, no a los avisos.
POLITICAL = re.compile(
    r"condemn|denounc|calls? (?:for|on)|\burges?\b|\bmeets?\b|met with|discuss|\btalks\b|mandate|renew|extension"
    r"|minister|president|premier|ambassador|envoy|security council|summit|proposal|\bplans?\b|initiative"
    r"|\bvisits?\b|\bwarns?\b|post-unifil|after unifil|withdrawal|diplomat|parliament|cabinet|election|press review"
    r"|condena|ministr|embajador|mandato|consejo de seguridad|reuni[óo]n|se re[úu]ne|\bvisita|propuesta|advierte"
    r"|retirada|diplom[áa]t|condamne|pr[ée]sident|mandat\b|conseil de s[ée]curit[ée]|rencontre|\bvisite\b"
    r"|proposition|post-finul|apr[èe]s la finul|revue de presse"
    r"|يبحث|بحث مع|بحثا|التقى|يلتقي|استقبل|يدين|أدان|ادان|استنكر|يدعو|الرئيس|وزير|مجلس الأمن|تمديد|ولاية"
    r"|موفد|زيارة|مبادرة|ما بعد|اتصالات|مشاورات|يحذر|خطة|مقترح|انسحاب|السفير|دبلوماس|النائب|كتلة|مجلس النواب",
    re.I,
)

# Tipo de suceso, por orden de prioridad. Se usa para saber si dos noticias cuentan lo mismo.
EVENT_TYPES = [
    ("explosion", "💥 Explosión o demolición", re.compile(
        r"blast|explosi|detonat|demoli|dynamit|blown up|blew up|تفجير|نسف|انفجار|d[ée]tonation|voladura", re.I)),
    ("aereo", "✈️ Ataque aéreo o de dron", re.compile(
        r"air ?strike|warplane|fighter jet|\bjets?\b|drone|\bstrikes?\b|\braids?\b|غارة|غارتان|غارتين|غارات|الطيران الحربي"
        r"|مسي[ّ]?رة|درون|محلقة|frappe|ataque a[ée]reo|\bdron", re.I)),
    ("artilleria", "🎯 Fuego de artillería", re.compile(
        r"shell(?:ed|ing|s)?\b|artillery|tank fire|مدفعي|قذيفة|قذائف|\bobus\b|artiller|proyectil", re.I)),
    ("disparos", "🔫 Disparos", re.compile(
        r"gunfire|machine.?gun|opened fire|\bfired?\b|رشقات|رصاص|تمشيط|إطلاق نار|اطلاق نار|\btirs?\b|disparo|r[áa]faga", re.I)),
    ("incursion", "🪖 Incursión terrestre", re.compile(
        r"incursion|توغل|tanks? (?:enter|advance)|troops (?:enter|advance)|incursi[óo]n|دبابة|دبابات", re.I)),
]
TYPE_LABEL = {k: lab for k, lab, _ in EVENT_TYPES}
TYPE_LABEL["otro"] = "⚠️ Incidente"


def event_type(item):
    for part in (item["title"], item["title"] + "\n" + item["body"]):
        for key, _, rx in EVENT_TYPES:
            if rx.search(part):
                return key
    return "otro"


def assess(item):
    """Devuelve (categoría, lugares, menciona_onu).

    "evento":   hecho sobre el terreno (ataque, explosión, víctimas...) cerca de la base o que afecta a UNIFIL.
    "politica": política, diplomacia, comunicados y resto de noticias de la zona (resumen silencioso).
    None:       no interesa.
    """
    title = item["title"]
    text = title + "\n" + item["body"]
    leb = item["src"]["lebanese"]
    un = bool(UN_STRICT.search(text)) or (bool(UN_LOOSE.search(text)) and (leb or bool(LEB_CUE.search(text))))
    places = find_places(text, leb)
    near = [p for p in places if p["_km"] <= RADIUS_KM]
    if not (un or near):
        return None, places, un
    # El titular manda: una declaración que cita un ataque no es un suceso nuevo.
    if POLITICAL.search(title):
        return "politica", places, un
    if INCIDENT.search(title) or (near and INCIDENT.search(text)):
        return "evento", places, un
    return "politica", places, un


def norm_title(t):
    t = re.sub(r"[\W_]+", " ", t.lower(), flags=re.U)
    return " ".join(t.split())[:160]


def keys_for(item):
    ks = [hashlib.sha1(norm_title(item["title"]).encode()).hexdigest()[:16]]
    if item["link"] and item["src"]["kind"] != "gnews":
        ks.append(hashlib.sha1(item["link"].encode()).hexdigest()[:16])
    return ks


STOP = set("para como este esta estos estas desde hasta sobre entre tras ante pero porque cuando donde "
           "tiene tienen haber sido será sera según segun también tambien más mas ciudad pueblo "
           "líbano libano israelí israeli israelíes israel".split())


def tokens(title):
    return {w for w in norm_title(title).split() if len(w) >= 4 and w not in STOP}


def similar(a, b):
    ta, tb = tokens(a), tokens(b)
    if len(ta) < 3 or len(tb) < 3:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# --- Traducción ---------------------------------------------------------------


DEEPL_KEY = os.environ.get("DEEPL_API_KEY", "").strip()
TR_FAILS = {}    # motor -> fallos seguidos en esta ejecución
TR_USED = {}     # motor -> traducciones hechas


def _fix_names(out):
    # Los traductores cambian los nombres propios; se devuelven a la forma habitual.
    out = re.sub(r"FPNUL", "FINUL", out)
    out = re.sub(r"(?:Al-)?Jiam", "Khiam", out)
    return out


def _tr_deepl(text):
    body = json.dumps({"text": [text], "target_lang": "ES"}).encode("utf-8")
    host = "api-free.deepl.com" if DEEPL_KEY.endswith(":fx") else "api.deepl.com"
    raw = fetch(f"https://{host}/v2/translate", data=body, timeout=15,
                headers={"Authorization": "DeepL-Auth-Key " + DEEPL_KEY, "Content-Type": "application/json"})
    return json.loads(raw)["translations"][0]["text"]


def _tr_google(text):
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=es&dt=t&q="
           + urllib.parse.quote(text))
    data = json.loads(fetch(url, timeout=15).decode("utf-8"))
    if data[2] == "es":
        return text
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def _tr_google_web(text):
    page = fetch("https://translate.google.com/m?sl=auto&tl=es&q=" + urllib.parse.quote(text), timeout=15).decode("utf-8")
    return html.unescape(re.search(r'class="result-container">(.*?)</div>', page, re.S).group(1))


def _tr_mymemory(text):
    url = "https://api.mymemory.translated.net/get?langpair=Autodetect%7Ces&q=" + urllib.parse.quote(text[:480])
    data = json.loads(fetch(url, timeout=15).decode("utf-8"))
    out = data["responseData"]["translatedText"]
    if data.get("responseStatus") not in (200, "200") or "MYMEMORY WARNING" in out.upper():
        raise RuntimeError("límite de MyMemory")
    return out


ENGINES = ([("deepl", _tr_deepl)] if DEEPL_KEY else []) + [
    ("google", _tr_google), ("google_web", _tr_google_web), ("mymemory", _tr_mymemory)]


def translate(text):
    """Traduce al español probando varios servicios. Si todos fallan, devuelve el original."""
    text = text.strip()
    if not text:
        return text
    foreign = bool(AR_CHARS.search(text))
    for name, fn in ENGINES:
        if TR_FAILS.get(name, 0) >= 2:
            continue  # este servicio no responde desde aquí; no se insiste
        try:
            out = (fn(text[:1800]) or "").strip()
            if not out or (foreign and AR_CHARS.search(out)):
                raise RuntimeError("sin traducir")
            TR_FAILS[name] = 0
            TR_USED[name] = TR_USED.get(name, 0) + 1
            return _fix_names(out)
        except Exception:
            TR_FAILS[name] = TR_FAILS.get(name, 0) + 1
    if foreign:
        TR_USED["ninguno"] = TR_USED.get("ninguno", 0) + 1
    return _fix_names(text)


# --- Sucesos ------------------------------------------------------------------

PLACE_BY_NAME = {p["name"]: p for p in GEO["places"]}
LEVEL_RANK = {"A": 0, "B": 1, "C": 2, "D": 3, "?": 4}
SAME_EVENT_HOURS = 6        # misma localidad y mismo tipo dentro de este margen = mismo suceso
SAME_EVENT_HOURS_LATE = 18  # los agregadores publican con horas de retraso
SIMILAR_HOURS = 24
SIMILAR_MIN = 0.5


def km_txt(p):
    return f"{p['_km']:.1f}".replace(".", ",") + f" km al {p['_dir']}"


def source_entry(item, title_es, ts):
    return {"name": item["source_name"], "level": item["level"], "origin": item["origin"],
            "title": title_es, "link": item["link"], "ts": ts}


def find_event(events, sig, title_es, ts, late):
    window = (SAME_EVENT_HOURS_LATE if late else SAME_EVENT_HOURS) * 3600
    for ev in reversed(events):
        if ev["sig"] == sig and abs(ts - ev["first_ts"]) <= window:
            return ev
        if abs(ts - ev["first_ts"]) <= SIMILAR_HOURS * 3600 and any(
                similar(title_es, s["title"]) >= SIMILAR_MIN for s in ev["sources"]):
            return ev
    return None


def render_event(ev):
    places = sorted((PLACE_BY_NAME[n] for n in ev["places"] if n in PLACE_BY_NAME), key=lambda p: p["_km"])
    nearest = places[0] if places else None
    km = nearest["_km"] if nearest else None
    if km is not None and km <= 5:
        color, mark = 0xD32F2F, "🔴"
    elif km is not None and km <= 10:
        color, mark = 0xF57C00, "🟠"
    elif km is not None and km <= RADIUS_KM:
        color, mark = 0xFBC02D, "🟡"
    else:
        color, mark = 0x1976D2, "🔵"
    if ev["un"]:
        mark += "🇺🇳"

    label = TYPE_LABEL.get(ev["type"], TYPE_LABEL["otro"])
    if places:
        dist_lines = [f"**{p['name']}**: {km_txt(p)} de la {BASE['name']}" for p in places[:4]]
        head = f"{label} · {nearest['name']}, {km_txt(nearest)} de la base"
    else:
        dist_lines = ["Sin lugar identificado en el texto; distancia a la base desconocida."]
        head = f"{label} · UNIFIL, lugar sin identificar"
    dist_note = "\n".join(dist_lines) + "\n-# Distancia en línea recta al centro del pueblo; margen de 1-2 km."

    srcs = ev["sources"]
    best = min((s["level"] for s in srcs), key=lambda l: LEVEL_RANK.get(l, 9))
    n_medios = len({s["name"] for s in srcs})
    conf = f"fiabilidad {best}" + (f" · {n_medios} fuentes" if n_medios > 1 else "")

    lines, extra = [], 0
    for s in srcs:
        line = f"• <t:{int(s['ts'])}:t> **{s['name']}** ({s['level']}) — [{s['title'][:140]}](<{s['link']}>)"
        if sum(len(x) + 1 for x in lines) + len(line) > 3700:
            extra += 1
            continue
        lines.append(line)
    if extra:
        lines.append(f"… y {extra} más")

    first = srcs[0]
    main = {
        "title": first["title"][:256],
        "url": first["link"] or None,
        "description": ev.get("body", "")[:1500],
        "color": color,
        "fields": [
            {"name": "📍 Distancia a la base", "value": dist_note[:1024], "inline": False},
            {"name": "Fiabilidad", "value": f"**{best}** · {LEVELS.get(best, '')}"
                                            + (f"\nLo recogen {n_medios} fuentes" if n_medios > 1 else f"\n{first['origin']}"),
             "inline": False},
        ],
        "timestamp": dt.datetime.fromtimestamp(ev["first_ts"], dt.timezone.utc).isoformat(),
    }
    sources_embed = {"title": f"Fuentes ({len(srcs)})", "description": "\n".join(lines), "color": color}
    return {
        "content": f"{mark} **{head}** · {conf}\n{first['title']}"[:1900],
        "embeds": [main, sources_embed],
        "allowed_mentions": {"parse": []},
    }


# --- Discord ------------------------------------------------------------------


def discord(payload, url, message_id=None):
    """Publica un mensaje o edita uno existente. Devuelve el id del mensaje, o None si falla."""
    if not url:
        print("\n--- " + ("EDICIÓN" if message_id else "AVISO") + " (modo prueba, no enviado) ---")
        print(payload["content"])
        for e in payload["embeds"]:
            for f in e.get("fields", []):
                print(f"[{f['name']}] {f['value']}")
            if e.get("title", "").startswith("Fuentes") or not e.get("title"):
                print(re.sub(r"\]\(<[^>]*>\)", "]", e.get("description", ""))[:1500])
        return message_id or "prueba"
    body = json.dumps(payload).encode("utf-8")
    target = f"{url}/messages/{message_id}" if message_id else url + "?wait=true"
    for _ in range(3):
        try:
            raw = fetch(target, data=body, method="PATCH" if message_id else "POST",
                        headers={"Content-Type": "application/json", "User-Agent": "vigia-libano/1.0"})
            time.sleep(1.2)
            return message_id or str(json.loads(raw).get("id", ""))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(wait + 0.5)
                continue
            print(f"  Discord respondió {e.code}: {e.read()[:200]!r}")
            return None
        except Exception as e:
            print(f"  Error enviando a Discord: {e}")
            time.sleep(2)
    return None


def build_digest(pending):
    lines = []
    for p in pending:
        if sum(len(x) + 1 for x in lines) + len(p["line"]) > 3900:
            break
        lines.append(p["line"])
    return {
        "content": f"🏛️ **Política, comunicados y otras noticias** · {len(lines)} sin suceso sobre el terreno",
        "embeds": [{"description": "\n".join(lines), "color": 0x607D8B}],
        "flags": 4096,  # mensaje silencioso: no hace sonar el móvil
        "allowed_mentions": {"parse": []},
    }, len(lines)


# --- Principal ----------------------------------------------------------------

DIGEST_MIN_ITEMS = 6      # el resumen se envía al juntar estas noticias...
DIGEST_MAX_WAIT_MIN = 60  # ...o cuando la más antigua lleva este tiempo esperando


def main():
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    seen = state.setdefault("seen", {})
    events = state.setdefault("events", [])
    pending = state.setdefault("pending", [])
    ctx_titles = state.setdefault("ctx_titles", [])
    before = json.dumps(state, sort_keys=True)
    now_ts = int(NOW.timestamp())

    print(f"Vigía Líbano · {NOW:%Y-%m-%d %H:%M} UTC · radio {RADIUS_KM:g} km · "
          f"{'envío a Discord' if WEBHOOK else 'MODO PRUEBA'}")
    items, errors = collect()
    for e in errors:
        print("  FALLO", e)

    cutoff = NOW - dt.timedelta(hours=MAX_AGE_HOURS)
    ev_items, ctx_items, batch_keys = [], [], set()
    for it in items:
        if it["date"] and it["date"] < cutoff:
            continue
        ks = keys_for(it)
        if any(k in seen or k in batch_keys for k in ks):
            continue
        kind, places, un = assess(it)
        if not kind:
            continue
        batch_keys.update(ks)
        (ev_items if kind == "evento" else ctx_items).append((it, places, un, ks))
    ev_items.sort(key=lambda x: x[0]["date"] or NOW)
    ctx_items.sort(key=lambda x: x[0]["date"] or NOW)
    print(f"{len(items)} entradas leídas: {len(ev_items)} de sucesos, {len(ctx_items)} de política y otras")

    def mark(ks):
        for k in ks:
            seen[k] = now_ts

    # 1. Sucesos: cada noticia se suma a un suceso ya avisado o abre uno nuevo.
    touched = {}  # id(ev) -> (ev, claves de las noticias añadidas)
    # Avisos que salieron sin traducir: se reintentan y se corrige el mensaje ya enviado.
    for ev in events:
        fixed = False
        for s in ev["sources"]:
            if AR_CHARS.search(s["title"]):
                new = translate(s["title"])
                fixed = fixed or new != s["title"]
                s["title"] = new
        if fixed:
            touched[id(ev)] = (ev, [])
    for p in pending:
        m = re.match(r"• \[(.*?)\]\(<", p["line"], re.S)
        if m and AR_CHARS.search(m.group(1)):
            p["line"] = p["line"].replace(m.group(1), translate(m.group(1))[:150], 1)
    new_events = []
    for it, places, un, ks in ev_items:
        ts = int((it["date"] or NOW).timestamp())
        title_es = translate(it["title"])
        near = [p for p in places if p["_km"] <= RADIUS_KM] or places
        sig = (near[0]["name"] if near else "UNIFIL") + "|" + event_type(it)
        late = it["src"]["kind"] == "gnews"
        ev = find_event(events + new_events, sig, title_es, ts, late)
        if ev is None:
            body = it["body"] if norm_title(it["body"]) != norm_title(it["title"]) else ""
            ev = {"sig": sig, "type": sig.split("|")[1], "places": [p["name"] for p in places[:4]], "un": un,
                  "first_ts": ts, "msg_id": None, "body": translate(body[:900]) if body else "", "sources": []}
            new_events.append(ev)
        ev["un"] = ev["un"] or un
        ev["sources"].append(source_entry(it, title_es, ts))
        touched.setdefault(id(ev), (ev, []))[1].extend(ks)

    posted = edited = 0
    for ev, ks in touched.values():
        is_new = ev["msg_id"] is None
        if is_new and posted >= MAX_PER_RUN:
            continue  # se quedan sin marcar y entran en la siguiente pasada
        mid = discord(render_event(ev), WEBHOOK, ev["msg_id"])
        if not mid:
            continue
        if is_new:
            ev["msg_id"] = mid
            events.append(ev)
            posted += 1
        else:
            edited += 1
        mark(ks)

    # 2. Política y otras: se descartan las repetidas y el resto espera al resumen.
    dups = 0
    for it, places, un, ks in ctx_items:
        title_es = translate(it["title"])
        mark(ks)
        if any(similar(title_es, t["title"]) >= 0.4 for t in ctx_titles):
            dups += 1
            continue
        ctx_titles.append({"title": title_es, "ts": now_ts})
        where = f" · {places[0]['name']}, {km_txt(places[0])}" if places else ""
        pending.append({"ts": now_ts, "line": f"• [{title_es[:150]}](<{it['link']}>)\n"
                                              f"-# {it['source_name']} · fiabilidad {it['level']}{where}"})

    digest = 0
    due = pending and (len(pending) >= DIGEST_MIN_ITEMS
                       or now_ts - pending[0]["ts"] >= DIGEST_MAX_WAIT_MIN * 60 or not WEBHOOK)
    while due:
        payload, n = build_digest(pending)
        if not discord(payload, WEBHOOK_CONTEXT or WEBHOOK):
            break
        del pending[:n]
        digest += 1
        due = len(pending) >= DIGEST_MIN_ITEMS and digest < 3

    print(f"Sucesos nuevos: {posted} · avisos ampliados con otra fuente: {edited} · "
          f"resúmenes: {digest} · política repetida descartada: {dups} · en espera: {len(pending)}")

    # 3. Guardar memoria.
    # Diagnóstico: qué traductor funciona y qué fuentes fallan (solo cambia si cambia la situación).
    state["diag"] = {"traductores": sorted(TR_USED) or state.get("diag", {}).get("traductores", []),
                     "fuentes_caidas": sorted(e.split(":")[0] for e in errors)}
    print("Traducciones:", TR_USED or "ninguna")
    state["seen"] = {k: v for k, v in seen.items() if v >= now_ts - 7 * 86400}
    state["events"] = [e for e in events if e["first_ts"] >= now_ts - 36 * 3600]
    state["ctx_titles"] = [t for t in ctx_titles if t["ts"] >= now_ts - 24 * 3600]
    if WEBHOOK and json.dumps(state, sort_keys=True) != before:
        STATE_FILE.write_text(json.dumps(state, indent=0, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    # Si fallan todas las fuentes, que la ejecución salga en rojo en GitHub.
    if errors and len(errors) == len(CFG["sources"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
