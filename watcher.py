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
    r"|\bpatrol|ambush|sniper|troops (?:enter|advance)|violat|violaci|خرق|خروقات"
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


def fetch(url, data=None, headers=None, timeout=20):
    h = {"User-Agent": UA, "Accept-Language": "en,es;q=0.8,ar;q=0.7"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
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


# --- Filtro -------------------------------------------------------------------


def assess(item):
    """Devuelve (categoría, lugares, menciona_onu).

    "incidente": hecho sobre el terreno cerca de la base o que afecta a UNIFIL (aviso inmediato).
    "contexto":  resto de noticias sobre UNIFIL o la zona (resumen silencioso).
    None:        no interesa.
    """
    text = item["title"] + "\n" + item["body"]
    leb = item["src"]["lebanese"]
    un = bool(UN_STRICT.search(text)) or (bool(UN_LOOSE.search(text)) and (leb or bool(LEB_CUE.search(text))))
    places = find_places(text, leb)
    near = [p for p in places if p["_km"] <= RADIUS_KM]
    if not (un or near):
        return None, places, un
    # El titular manda: un análisis político que cita un ataque de pasada no es un incidente.
    incident = bool(INCIDENT.search(item["title"])) or (bool(near) and bool(INCIDENT.search(text)))
    return ("incidente" if incident else "contexto"), places, un


def norm_title(t):
    t = re.sub(r"[\W_]+", " ", t.lower(), flags=re.U)
    return " ".join(t.split())[:160]


def keys_for(item):
    ks = [hashlib.sha1(norm_title(item["title"]).encode()).hexdigest()[:16]]
    if item["link"] and item["src"]["kind"] != "gnews":
        ks.append(hashlib.sha1(item["link"].encode()).hexdigest()[:16])
    return ks


# --- Traducción ---------------------------------------------------------------


def translate(text):
    """Traduce al español con el endpoint público de Google Translate. Si falla, devuelve el original."""
    text = text.strip()
    if not text:
        return text
    try:
        url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=es&dt=t&q="
               + urllib.parse.quote(text[:1800]))
        data = json.loads(fetch(url, timeout=15).decode("utf-8"))
        if data[2] == "es":
            return text
        out = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip() or text
        # El traductor cambia los nombres propios; se devuelven a la forma habitual.
        out = re.sub(r"\bFPNUL\b", "FINUL", out)
        out = re.sub(r"\bJiam\b", "Khiam", out)
        return out
    except Exception:
        return text


# --- Discord ------------------------------------------------------------------


def km_txt(p):
    return f"{p['_km']:.1f}".replace(".", ",") + f" km al {p['_dir']}"


def build_message(item, places, un):
    title_es = translate(item["title"])
    body = item["body"] if norm_title(item["body"]) != norm_title(item["title"]) else ""
    body_es = translate(body[:900]) if body else ""

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
    if un:
        mark += "🇺🇳"

    if places:
        dist_lines = [f"**{p['name']}**: {km_txt(p)} de la {BASE['name']}" for p in places[:4]]
        head = f"{nearest['name']}, {km_txt(nearest)} de la base"
    else:
        dist_lines = ["Sin lugar identificado en el texto; distancia a la base desconocida."]
        head = "UNIFIL, lugar sin identificar"
    dist_note = "\n".join(dist_lines) + "\n-# Distancia en línea recta al centro del pueblo; margen de 1-2 km."

    level = item["level"]
    fields = [
        {"name": "📍 Distancia a la base", "value": dist_note[:1024], "inline": False},
        {"name": "Fuente", "value": f"{item['source_name']}\n{item['origin']}"[:1024], "inline": True},
        {"name": "Fiabilidad", "value": f"**{level}** · {LEVELS.get(level, '')}", "inline": True},
    ]
    if title_es != item["title"]:
        fields.append({"name": "Titular original", "value": item["title"][:1024], "inline": False})
    embed = {
        "title": title_es[:256],
        "url": item["link"] or None,
        "description": body_es[:1500],
        "color": color,
        "fields": fields,
    }
    if item["date"]:
        embed["timestamp"] = item["date"].isoformat()
    return {
        "content": f"{mark} **{head}** · fiabilidad {level}\n{title_es}"[:1900],
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }


def build_digest(rows):
    """Un único mensaje silencioso con las noticias de contexto."""
    lines = []
    for it, places, un in rows:
        where = f" · {places[0]['name']}, {km_txt(places[0])}" if places else ""
        line = f"• [{translate(it['title'])[:150]}](<{it['link']}>)\n-# {it['source_name']} · fiabilidad {it['level']}{where}"
        if sum(len(x) + 1 for x in lines) + len(line) > 3900:
            break
        lines.append(line)
    return {
        "content": f"🗞️ **Contexto** · {len(lines)} noticias sin incidente sobre el terreno",
        "embeds": [{"description": "\n".join(lines), "color": 0x607D8B}],
        "flags": 4096,  # mensaje silencioso: no hace sonar el móvil
        "allowed_mentions": {"parse": []},
    }, len(lines)


def send(payload, url=None):
    url = url or WEBHOOK
    if not url:
        e = payload["embeds"][0]
        print("\n--- AVISO (modo prueba, no enviado) ---")
        print(payload["content"])
        for f in e.get("fields", []):
            print(f"[{f['name']}] {f['value']}")
        if e.get("description"):
            print("[Texto]", e["description"][:1200])
        if e.get("url"):
            print("[Enlace]", e["url"][:90])
        return True
    body = json.dumps(payload).encode("utf-8")
    for _ in range(3):
        try:
            fetch(url + "?wait=true", data=body,
                  headers={"Content-Type": "application/json", "User-Agent": "vigia-libano/1.0"})
            time.sleep(1.2)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(wait + 0.5)
                continue
            print(f"  Discord respondió {e.code}: {e.read()[:200]!r}")
            return False
        except Exception as e:
            print(f"  Error enviando a Discord: {e}")
            time.sleep(2)
    return False


# --- Principal ----------------------------------------------------------------


def main():
    state = {"seen": {}}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    seen = state["seen"]

    print(f"Vigía Líbano · {NOW:%Y-%m-%d %H:%M} UTC · radio {RADIUS_KM:g} km · "
          f"{'envío a Discord' if WEBHOOK else 'MODO PRUEBA'}")
    items, errors = collect()
    for e in errors:
        print("  FALLO", e)

    cutoff = NOW - dt.timedelta(hours=MAX_AGE_HOURS)
    fresh, context, batch_keys = [], [], set()
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
        (fresh if kind == "incidente" else context).append((it, places, un, ks))

    fresh.sort(key=lambda x: x[0]["date"] or NOW)
    context.sort(key=lambda x: x[0]["date"] or NOW)
    print(f"{len(items)} entradas leídas: {len(fresh)} incidentes nuevos, {len(context)} de contexto"
          + (f" (se envían {MAX_PER_RUN} incidentes, el resto en la siguiente pasada)" if len(fresh) > MAX_PER_RUN else ""))

    def mark(ks):
        if WEBHOOK:
            for k in ks:
                seen[k] = int(NOW.timestamp())

    sent = 0
    for it, places, un, ks in fresh[:MAX_PER_RUN]:
        if send(build_message(it, places, un)):
            sent += 1
            mark(ks)

    if context:
        payload, n = build_digest([c[:3] for c in context])
        if send(payload, WEBHOOK_CONTEXT or WEBHOOK):
            sent += 1
            for c in context[:n]:
                mark(c[3])

    if WEBHOOK and sent:
        limit = NOW.timestamp() - 7 * 86400
        state["seen"] = {k: v for k, v in seen.items() if v >= limit}
        STATE_FILE.write_text(json.dumps(state, indent=0, sort_keys=True), encoding="utf-8")
    print(f"Enviados: {sent}")
    # Si fallan todas las fuentes, que la ejecución salga en rojo en GitHub.
    if errors and len(errors) == len(CFG["sources"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
