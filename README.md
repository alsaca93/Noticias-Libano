# Vigía Líbano

Vigila fuentes de noticias sobre el sur del Líbano y avisa por Discord de lo que ocurre
cerca de la base Miguel de Cervantes (Marjayoun) o afecta a las tropas de la ONU (UNIFIL/FINUL).

## Qué hace

Cada 5 minutos, GitHub Actions ejecuta `watcher.py`, que:

1. Lee las fuentes de `sources.json` (RSS, canales públicos de Telegram y Google News).
2. Se queda con las noticias que nombran un pueblo a menos de 15 km de la base o a UNIFIL.
3. Las separa en dos tipos:
   - **Suceso** (ataque, explosión, disparos, víctimas...): un aviso con sonido por suceso.
     Si otro medio cuenta después lo mismo (misma localidad y mismo tipo de suceso en 6 horas,
     o titular muy parecido), no llega otro aviso: se añade la fuente al aviso original.
   - **Política, comunicados y otras noticias**: un resumen agrupado y silencioso,
     que se envía al juntar 6 noticias o como mucho cada hora.
4. Traduce el titular al español y añade fuentes, nivel de fiabilidad y distancia a la base.
5. Apunta en `state.json` lo ya avisado y los sucesos abiertos.

Colores del aviso: 🔴 hasta 5 km · 🟠 hasta 10 km · 🟡 hasta 15 km · 🔵 UNIFIL sin lugar cercano · 🇺🇳 menciona a UNIFIL.

Niveles de fiabilidad: **A** oficial o primaria (parte interesada) · **B** medio profesional ·
**C** medio partidista · **D** sin contrastar · **?** medio sin clasificar.

## Puesta en marcha

1. En Discord: Ajustes del canal → Integraciones → Webhooks → Nuevo webhook → Copiar URL.
2. En GitHub: crear un repositorio **público** (en los privados, una tarea cada 5 minutos agota los minutos gratuitos).
3. En el repositorio: Settings → Secrets and variables → Actions → New repository secret,
   con nombre `DISCORD_WEBHOOK_URL` y la URL del webhook como valor.
   Opcional: `DISCORD_WEBHOOK_CONTEXT` con el webhook de otro canal para las noticias de contexto.
4. Subir este directorio al repositorio.

## Ajustes

- `places.json`: posición de la base y pueblos con sus nombres en varios idiomas.
- `sources.json`: fuentes, procedencia y nivel de fiabilidad; reglas para clasificar los medios que llegan por Google News.
- Variables de entorno: `RADIUS_KM` (15), `MAX_AGE_HOURS` (12), `MAX_PER_RUN` (12).

## Probar en local

Sin la variable `DISCORD_WEBHOOK_URL`, el script imprime los avisos y no envía ni guarda nada:

```bash
python watcher.py
```
