"""
Pronósticos de Niké (agente de análisis de fútbol) desde entradas/nike/*.csv
hacia silver.nike_pronosticos.

Niké no escribe en la base: deja un CSV en entradas/nike/ y se hace commit y
push ANTES del partido. La hora del commit en GitHub es la prueba pública de
que el pronóstico es previo; este script además marca es_tardio cuando el
pronóstico o la carga son posteriores al inicio del partido (según ESPN).

Reglas:
  - Una fila por partido y pronóstico; una revisión es una fila nueva con otro
    pronosticado_en. Nunca se pisa una fila ya cargada.
  - Valida rangos, suma de probabilidades (± 0,005) y que el evento exista
    en ESPN para la Champions; falla con la lista de errores.
  - pronosticado_en no puede estar en el futuro.

Formato del CSV: ver entradas/nike/README.md.

Uso:
    py src/ingesta/nike_pronosticos.py                  # todos los CSV de entradas/nike/
    py src/ingesta/nike_pronosticos.py --archivo entradas/nike/2026-11-03_j4.csv

Sin DATABASE_URL solo valida e imprime el resumen.

Autor : Ruth Choque (con PROMETHEUS) · 2026-10-26
"""

import argparse
import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from espn_cuotas import PAUSA_S, URL_BASE, http_json

RAIZ = Path(__file__).resolve().parents[2]
CARPETA = RAIZ / "entradas" / "nike"
LIGA = "uefa.champions"

OBLIGATORIAS = ["espn_event_id", "pronosticado_en", "prob_local", "prob_empate", "prob_visita", "version_metodo"]
PROBABILIDADES = ["prob_local", "prob_empate", "prob_visita", "prob_over_2_5", "prob_btts"]
GOLES = ["goles_esp_local", "goles_esp_visita"]
NIVELES = {"importancia_local": ("ALTA", "MEDIA", "BAJA"), "importancia_visita": ("ALTA", "MEDIA", "BAJA"),
           "confianza": ("ALTA", "MEDIA", "BAJA"), "tipo_once": ("PROBABLE", "OFICIAL")}
TEXTOS = ["once_local", "once_visita", "comentario"]
COLUMNAS_BD = (["espn_event_id", "pronosticado_en", "version_metodo"] + PROBABILIDADES + GOLES
               + list(NIVELES) + TEXTOS + ["es_tardio", "inicio_utc", "archivo_origen"])


def leer_csv(ruta):
    with ruta.open(encoding="utf-8-sig") as f:
        return [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def validar(fila, n, archivo, ahora):
    """Devuelve (fila normalizada, errores)."""
    err, f = [], {"archivo_origen": archivo}
    donde = f"{archivo} fila {n}"
    for c in OBLIGATORIAS:
        if not fila.get(c):
            err.append(f"{donde}: falta {c}")
    if err:
        return None, err
    f["espn_event_id"] = fila["espn_event_id"]
    f["version_metodo"] = fila["version_metodo"]
    try:
        f["pronosticado_en"] = datetime.fromisoformat(fila["pronosticado_en"].replace("Z", "+00:00"))
        if f["pronosticado_en"].tzinfo is None:
            err.append(f"{donde}: pronosticado_en sin zona horaria (usar p. ej. 2026-11-02T20:00-05:00)")
        elif f["pronosticado_en"] > ahora:
            err.append(f"{donde}: pronosticado_en está en el futuro")
    except ValueError:
        err.append(f"{donde}: pronosticado_en no es ISO 8601")
    for c in PROBABILIDADES + GOLES:
        v = fila.get(c, "")
        if not v:
            f[c] = None
            continue
        try:
            f[c] = float(v.replace(",", "."))
        except ValueError:
            err.append(f"{donde}: {c} = '{v}' no es un número")
            continue
        if c in PROBABILIDADES and not 0 <= f[c] <= 1:
            err.append(f"{donde}: {c} = {f[c]} fuera de 0..1")
        if c in GOLES and f[c] < 0:
            err.append(f"{donde}: {c} negativo")
    if all(f.get(c) is not None for c in PROBABILIDADES[:3]):
        suma = f["prob_local"] + f["prob_empate"] + f["prob_visita"]
        if abs(suma - 1) >= 0.005:
            err.append(f"{donde}: L + E + V = {suma:.4f} (debe sumar 1)")
    for c, validos in NIVELES.items():
        v = fila.get(c, "").upper()
        if v and v not in validos:
            err.append(f"{donde}: {c} = '{v}' (válidos: {', '.join(validos)})")
        f[c] = v or None
    for c in TEXTOS:
        f[c] = fila.get(c) or None
    return f, err


def inicio_evento(espn_event_id, cache):
    """Hora de inicio en ESPN (None si el evento no es de la Champions)."""
    if espn_event_id not in cache:
        try:
            _, s = http_json(URL_BASE.format(liga=LIGA, recurso="summary") + f"?event={espn_event_id}")
            fecha = s["header"]["competitions"][0]["date"]
            cache[espn_event_id] = datetime.fromisoformat(fecha.replace("Z", "+00:00"))
        except Exception:
            cache[espn_event_id] = None
        time.sleep(PAUSA_S)
    return cache[espn_event_id]


def cargar_postgres(url, filas):
    import psycopg
    sql = f"""
        INSERT INTO silver.nike_pronosticos ({", ".join(COLUMNAS_BD)})
        VALUES ({", ".join(["%s"] * len(COLUMNAS_BD))})
        ON CONFLICT (espn_event_id, version_metodo, pronosticado_en) DO NOTHING"""
    n = 0
    with psycopg.connect(url) as cx, cx.cursor() as cur:
        for f in filas:
            cur.execute(sql, [f[c] for c in COLUMNAS_BD])
            n += cur.rowcount
    return n


# -----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Pronósticos de Niké -> silver.nike_pronosticos")
    ap.add_argument("--archivo", help="un CSV puntual (por defecto, todos los de entradas/nike/)")
    args = ap.parse_args()

    rutas = [RAIZ / args.archivo] if args.archivo else sorted(CARPETA.glob("*.csv"))
    ahora = datetime.now(timezone.utc)
    filas, errores, cache = [], [], {}
    for ruta in rutas:
        archivo = ruta.relative_to(RAIZ).as_posix()
        for n, fila in enumerate(leer_csv(ruta), start=2):
            f, err = validar(fila, n, archivo, ahora)
            errores += err
            if err:
                continue
            f["inicio_utc"] = inicio_evento(f["espn_event_id"], cache)
            if f["inicio_utc"] is None:
                errores.append(f"{archivo} fila {n}: evento ESPN {f['espn_event_id']} no encontrado en {LIGA}")
                continue
            # tardío: pronosticado o cargado con el partido ya empezado
            f["es_tardio"] = f["pronosticado_en"] >= f["inicio_utc"] or ahora >= f["inicio_utc"]
            filas.append(f)

    if errores:
        raise SystemExit("Errores de validación:\n  " + "\n  ".join(errores))
    tardios = sum(f["es_tardio"] for f in filas)
    print(f"{len(rutas)} archivo(s), {len(filas)} pronósticos válidos ({tardios} tardíos)")

    url_bd = os.getenv("DATABASE_URL")
    if url_bd:
        n = cargar_postgres(url_bd, filas)
        print(f"Cargado en PostgreSQL: {n} nuevos (los ya cargados no se tocan)")
    else:
        print("Sin DATABASE_URL: solo validación")


if __name__ == "__main__":
    main()
