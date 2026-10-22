"""
Captura de cuotas de la UEFA Champions League desde la API no oficial de ESPN.

Por cada día del rango consulta el scoreboard y, por cada partido, el summary.
Guarda el payload crudo (bronze) y una fila por casa de apuestas y tipo de
línea (silver):
    ACTUAL    -> cuota vigente al momento de la captura (partido por jugarse)
    APERTURA  -> primera línea publicada (bloque moneyline.open)
    CIERRE    -> última línea antes del inicio (partido ya iniciado/jugado)

Uso:
    py src/ingesta/espn_cuotas.py                                   # ayer .. +14 días
    py src/ingesta/espn_cuotas.py --desde 2024-09-17 --hasta 2025-05-31   # backfill

Destino: si existe DATABASE_URL -> PostgreSQL (bronze.ingesta + silver.espn_cuotas);
si no, archivos locales en data/ (ignorado por git).
Los datos de ESPN NO se republican crudos (ver gold.d_fuente).

Autor : Ruth Choque (con PROMETHEUS) · 2026-10-21
"""

import argparse
import csv
import gzip
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

URL_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/{liga}/{recurso}"
PAUSA_S = 0.4
RAIZ = Path(__file__).resolve().parents[2]

# Del summary (~280 KB) solo se guarda en bronze lo que sirve para cuotas (~10 KB)
CLAVES_SUMMARY = ("header", "gameInfo", "pickcenter", "odds")
CLAVES_RUIDO = {"links", "link", "logos", "logo", "playerOdds", "betSlipUrl", "footer", "uid", "$ref"}

COLUMNAS = [
    "espn_event_id", "liga", "inicio_utc", "estado_partido",
    "local_espn_id", "local_nombre", "visita_espn_id", "visita_nombre",
    "estadio", "ciudad", "casa_id", "casa_apuestas", "tipo_linea",
    "cuota_local", "cuota_empate", "cuota_visita",
    "linea_ou", "cuota_over", "cuota_under", "formato_origen", "capturado_en",
]


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------
def http_json(url, intentos=3):
    """GET con reintentos exponenciales; los 4xx no se reintentan."""
    for i in range(intentos):
        try:
            req = Request(url, headers={"User-Agent": "ucl-data-platform/0.1",
                                        "Accept-Encoding": "gzip"})
            with urlopen(req, timeout=30) as r:
                cuerpo = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    cuerpo = gzip.decompress(cuerpo)
                return r.status, json.loads(cuerpo)
        except HTTPError as e:
            if e.code < 500 or i == intentos - 1:
                raise
        except (URLError, TimeoutError):
            if i == intentos - 1:
                raise
        time.sleep(2 ** i)


# -----------------------------------------------------------------------------
# Conversión de formatos de cuota a decimal
# -----------------------------------------------------------------------------
def americana_a_decimal(v):
    if v in (None, ""):
        return None
    try:
        x = float(str(v).replace("+", ""))
    except ValueError:
        return None
    if x == 0:
        return None
    return round(1 + x / 100, 3) if x > 0 else round(1 + 100 / abs(x), 3)


def fraccional_a_decimal(v):
    if not v:
        return None
    s = str(v).strip().upper()
    if s in ("EVS", "EVENS"):
        return 2.0
    if "/" not in s:
        return None
    num, den = s.split("/", 1)
    try:
        return round(1 + float(num) / float(den), 3)
    except (ValueError, ZeroDivisionError):
        return None


def a_linea(v):
    """'o3.5' / 'u3.5' / 3.5 -> 3.5"""
    if v in (None, ""):
        return None
    try:
        return float(str(v).lstrip("ouOU+"))
    except ValueError:
        return None


def _sin_ruido(nodo):
    if isinstance(nodo, dict):
        return {k: _sin_ruido(v) for k, v in nodo.items() if k not in CLAVES_RUIDO}
    if isinstance(nodo, list):
        return [_sin_ruido(x) for x in nodo]
    return nodo


def recortar_summary(summary):
    return _sin_ruido({k: summary[k] for k in CLAVES_SUMMARY if k in summary})


def cuotas_validas(fila):
    return all(fila[k] is not None and fila[k] > 1
               for k in ("cuota_local", "cuota_empate", "cuota_visita"))


# -----------------------------------------------------------------------------
# Parseo de bloques de cuotas
# -----------------------------------------------------------------------------
def _filas_americanas(bloque, prepartido):
    """Bloques tipo DraftKings / ESPN BET: moneyLine en formato americano."""
    h = bloque.get("homeTeamOdds") or {}
    a = bloque.get("awayTeamOdds") or {}
    d = bloque.get("drawOdds") or {}
    ml = bloque.get("moneyline") or {}
    tot = bloque.get("total") or {}
    filas = []

    if ml:
        tipos = (("APERTURA", "open"), ("ACTUAL" if prepartido else "CIERRE", "close"))
        for tipo, k in tipos:
            over = (tot.get("over") or {}).get(k) or {}
            under = (tot.get("under") or {}).get(k) or {}
            filas.append({
                "tipo_linea": tipo,
                "cuota_local": americana_a_decimal(((ml.get("home") or {}).get(k) or {}).get("odds")),
                "cuota_empate": americana_a_decimal(((ml.get("draw") or {}).get(k) or {}).get("odds")),
                "cuota_visita": americana_a_decimal(((ml.get("away") or {}).get(k) or {}).get("odds")),
                "linea_ou": a_linea(over.get("line")),
                "cuota_over": americana_a_decimal(over.get("odds")),
                "cuota_under": americana_a_decimal(under.get("odds")),
            })
    else:
        filas.append({
            "tipo_linea": "ACTUAL" if prepartido else "CIERRE",
            "cuota_local": americana_a_decimal(h.get("moneyLine")),
            "cuota_empate": americana_a_decimal(d.get("moneyLine")),
            "cuota_visita": americana_a_decimal(a.get("moneyLine")),
            "linea_ou": a_linea(bloque.get("overUnder")),
            "cuota_over": americana_a_decimal(bloque.get("overOdds")),
            "cuota_under": americana_a_decimal(bloque.get("underOdds")),
        })

    for f in filas:
        f["formato_origen"] = "americana"
    return filas


def _filas_fraccionales(bloque):
    """Bloque tipo Bet 365 (summary): fracción + decimal. Solo válido pre-partido:
    en partidos jugados ESPN deja la cuota en vivo final (p. ej. 150/1)."""
    def cuota(nodo):
        nodo = nodo or {}
        nodo = nodo.get("odds", nodo)            # summary anida en 'odds'; scoreboard no
        return fraccional_a_decimal(nodo.get("summary")) or nodo.get("value")

    to = ((bloque.get("bettingOdds") or {}).get("teamOdds")) or {}
    return [{
        "tipo_linea": "ACTUAL",
        "cuota_local": cuota(bloque.get("homeTeamOdds")),
        "cuota_empate": cuota(bloque.get("drawOdds")),
        "cuota_visita": cuota(bloque.get("awayTeamOdds")),
        "linea_ou": a_linea((to.get("preMatchOverUnderHandicap") or {}).get("value")),
        "cuota_over": fraccional_a_decimal((to.get("preMatchGoalLineOver") or {}).get("value")),
        "cuota_under": fraccional_a_decimal((to.get("preMatchGoalLineUnder") or {}).get("value")),
        "formato_origen": "fraccional",
    }]


def filas_cuotas(evento, summary, liga, capturado_en):
    comp = evento["competitions"][0]
    equipos = {c["homeAway"]: c["team"] for c in comp["competitors"]}
    estado = evento["status"]["type"]["name"]
    prepartido = estado == "STATUS_SCHEDULED"
    venue = comp.get("venue") or {}
    base = {
        "espn_event_id": evento["id"],
        "liga": liga,
        "inicio_utc": evento["date"],
        "estado_partido": estado,
        "local_espn_id": equipos["home"]["id"],
        "local_nombre": equipos["home"]["displayName"],
        "visita_espn_id": equipos["away"]["id"],
        "visita_nombre": equipos["away"]["displayName"],
        "estadio": venue.get("fullName"),
        "ciudad": (venue.get("address") or {}).get("city"),
        "capturado_en": capturado_en,
    }

    if summary is not None:
        bloques = (summary.get("pickcenter") or []) + (summary.get("odds") or [])
    else:
        bloques = comp.get("odds") or []      # respaldo si el summary falló

    filas, vistos = [], set()
    for b in bloques:
        if not b:
            continue
        casa = b.get("provider") or {}
        if "live" in (casa.get("name") or "").lower():
            continue                           # cuotas en vivo (p. ej. 'ESPN BET - Live Odds')
        h = b.get("homeTeamOdds") or {}
        if "moneyLine" in h or "moneyline" in b:
            parciales = _filas_americanas(b, prepartido)
        elif prepartido and ("odds" in h or "value" in h):
            parciales = _filas_fraccionales(b)
        else:
            continue                           # p. ej. 'bloomberg' (solo % de victoria)
        for p in parciales:
            clave = (casa.get("id"), p["tipo_linea"])
            fila = {**base, **p, "casa_id": casa.get("id"), "casa_apuestas": casa.get("name")}
            if clave in vistos or not cuotas_validas(fila):
                continue
            vistos.add(clave)
            filas.append(fila)
    return filas


# -----------------------------------------------------------------------------
# Destinos
# -----------------------------------------------------------------------------
class DestinoLocal:
    """Sin base de datos: JSON crudo en data/raw y CSV acumulado en data/silver."""

    def __init__(self, capturado_en):
        marca = capturado_en.replace(":", "").replace("-", "")[:15]
        self.dir_raw = RAIZ / "data" / "raw" / "espn" / marca
        self.csv = RAIZ / "data" / "silver" / "espn_cuotas.csv"
        self.dir_raw.mkdir(parents=True, exist_ok=True)
        self.csv.parent.mkdir(parents=True, exist_ok=True)

    def guardar_crudo(self, endpoint, parametros, status, payload):
        nombre = endpoint + "_" + "_".join(str(v) for v in parametros.values()) + ".json"
        (self.dir_raw / nombre).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return None

    def guardar_cuotas(self, filas, id_ingesta):
        nuevo = not self.csv.exists()
        with self.csv.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNAS, extrasaction="ignore")
            if nuevo:
                w.writeheader()
            w.writerows(filas)
        return len(filas)

    def cerrar(self):
        pass


class DestinoPostgres:
    def __init__(self, url):
        import psycopg                         # solo se requiere en modo BD
        self.cx = psycopg.connect(url, autocommit=False)

    def guardar_crudo(self, endpoint, parametros, status, payload):
        texto = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        # ON CONFLICT DO UPDATE no-op: devuelve el id tanto si es nuevo como si ya existía
        fila = self.cx.execute(
            """INSERT INTO bronze.ingesta (id_fuente, endpoint, parametros, http_status, payload_json, payload_hash)
               VALUES ('espn', %s, %s, %s, %s, md5(%s))
               ON CONFLICT (id_fuente, endpoint, payload_hash)
               DO UPDATE SET procesado = bronze.ingesta.procesado
               RETURNING id_ingesta""",
            (endpoint, json.dumps(parametros), status, texto, texto),
        ).fetchone()
        return fila[0]

    def guardar_cuotas(self, filas, id_ingesta):
        n = 0
        for f in filas:
            cur = self.cx.execute(
                f"""INSERT INTO silver.espn_cuotas (id_ingesta, {", ".join(COLUMNAS)})
                    VALUES (%s, {", ".join(["%s"] * len(COLUMNAS))})
                    ON CONFLICT DO NOTHING""",
                (id_ingesta, *[f[c] for c in COLUMNAS]),
            )
            n += cur.rowcount
        return n

    def cerrar(self):
        self.cx.commit()
        self.cx.close()


# -----------------------------------------------------------------------------
# Proceso principal
# -----------------------------------------------------------------------------
def capturar(desde, hasta, liga, destino, capturado_en):
    dias = partidos = cuotas = 0
    dia = desde
    while dia <= hasta:
        clave_dia = dia.strftime("%Y%m%d")
        status, sb = http_json(URL_BASE.format(liga=liga, recurso="scoreboard") + f"?dates={clave_dia}")
        dias += 1
        eventos = sb.get("events") or []
        if eventos:
            destino.guardar_crudo("scoreboard", {"liga": liga, "dates": clave_dia}, status, _sin_ruido(sb))
        for ev in eventos:
            time.sleep(PAUSA_S)
            try:
                st, summary = http_json(URL_BASE.format(liga=liga, recurso="summary") + f"?event={ev['id']}")
                summary = recortar_summary(summary)
                id_ing = destino.guardar_crudo("summary", {"liga": liga, "event": ev["id"]}, st, summary)
            except (HTTPError, URLError) as e:
                print(f"  ! summary {ev['id']} falló ({e}); uso cuotas del scoreboard", file=sys.stderr)
                summary, id_ing = None, None
            filas = filas_cuotas(ev, summary, liga, capturado_en)
            cuotas += destino.guardar_cuotas(filas, id_ing)
            partidos += 1
        dia += timedelta(days=1)
        time.sleep(PAUSA_S)
    return dias, partidos, cuotas


def main():
    hoy = datetime.now(timezone.utc).date()
    ap = argparse.ArgumentParser(description="Captura de cuotas ESPN - Champions League")
    ap.add_argument("--desde", type=date.fromisoformat, default=hoy - timedelta(days=1))
    ap.add_argument("--hasta", type=date.fromisoformat, default=hoy + timedelta(days=14))
    ap.add_argument("--liga", default="uefa.champions")
    args = ap.parse_args()

    url_bd = os.getenv("DATABASE_URL")
    if os.getenv("GITHUB_ACTIONS") and not url_bd:
        sys.exit("DATABASE_URL no configurado: en GitHub Actions los archivos locales se pierden.")

    capturado_en = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    destino = DestinoPostgres(url_bd) if url_bd else DestinoLocal(capturado_en)
    try:
        dias, partidos, cuotas = capturar(args.desde, args.hasta, args.liga, destino, capturado_en)
    except Exception:
        if isinstance(destino, DestinoPostgres):
            destino.cx.rollback()
        raise
    destino.cerrar()

    modo = "PostgreSQL" if url_bd else f"local ({RAIZ / 'data'})"
    print(f"[{capturado_en}] {args.liga} {args.desde}..{args.hasta} · días {dias} · "
          f"partidos {partidos} · filas de cuotas {cuotas} · destino {modo}")


if __name__ == "__main__":
    main()
