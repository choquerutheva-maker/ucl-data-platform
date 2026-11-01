"""
Resultados, estadísticas y cuotas de las 5 grandes ligas desde football-data.co.uk.

Fuente : https://www.football-data.co.uk/mmz4281/{AAbb}/{división}.csv  (temporadas)
         https://www.football-data.co.uk/fixtures.csv                (próximos partidos)
Cubre  : Premier League, LaLiga, Bundesliga, Serie A y Ligue 1, desde 2012/13.
         La web se actualiza ~2 veces por semana; el archivo de fixtures trae los
         partidos de los próximos días con cuotas previas.

Pasos:
  1. Descarga cada CSV (bronze.ingesta, texto crudo; reintenta si llega cortado).
  2. Parsea partidos: fecha, hora (Reino Unido -> UTC), goles 90' y 1T, xG,
     tiros, rojas; y cuotas 1X2 / O-U 2,5 por casa, previas y de cierre.
  3. Resuelve equipos con seeds/equipos_football_data.csv (falla si aparece un
     nombre nuevo, para revisarlo a mano).
  4. Valida: sin pares local-visita repetidos, FTR coherente con los goles y,
     en temporadas terminadas, n equipos x (n - 1) partidos.
  5. Carga gold.d_equipo, gold.d_equipo_alias, gold.f_partido_liga y
     silver.fd_cuotas_liga (upsert idempotente). Un partido JUGADO nunca vuelve
     a PROGRAMADO. Sin DATABASE_URL deja CSV en data/silver/.

Uso:
    py src/ingesta/football_data_ligas.py                       # temporada actual + fixtures
    py src/ingesta/football_data_ligas.py --temporadas 2012-2025 --sin-fixtures
    py src/ingesta/football_data_ligas.py --ligas ENG1,ESP1 --temporadas 2024,2025

Autor : Ruth Choque (con PROMETHEUS) · 2026-10-31
"""

import argparse
import csv
import io
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BASE = "https://football-data.co.uk"       # www. redirige (302) a este host
RAIZ = Path(__file__).resolve().parents[2]
ZONA_HORARIA = ZoneInfo("Europe/London")   # football-data publica la hora del Reino Unido
FUENTE = "football_data_uk"

LIGAS = {"ENG1": "E0", "ESP1": "SP1", "GER1": "D1", "ITA1": "I1", "FRA1": "F1"}
PRIMERA_TEMPORADA = 2012
# Temporadas que no se completaron: (liga, temporada) -> partidos jugados
INCOMPLETAS = {("FRA1", 2019): 279}        # Ligue 1 2019/20 suspendida por COVID

# Cuotas: (casa, momento) -> columnas (local, empate, visita, over 2,5, under 2,5).
# Se prueba cada alternativa en orden (los nombres cambiaron en 2019/20).
CUOTAS = {
    ("BET365", "PREVIA"):     [("B365H", "B365D", "B365A", "B365>2.5", "B365<2.5"),
                               ("B365H", "B365D", "B365A", None, None)],
    ("PINNACLE", "PREVIA"):   [("PSH", "PSD", "PSA", "P>2.5", "P<2.5"),
                               ("PSH", "PSD", "PSA", None, None)],
    ("PROMEDIO", "PREVIA"):   [("AvgH", "AvgD", "AvgA", "Avg>2.5", "Avg<2.5"),
                               ("BbAvH", "BbAvD", "BbAvA", "BbAv>2.5", "BbAv<2.5")],
    ("MAXIMO", "PREVIA"):     [("MaxH", "MaxD", "MaxA", "Max>2.5", "Max<2.5"),
                               ("BbMxH", "BbMxD", "BbMxA", "BbMx>2.5", "BbMx<2.5")],
    ("BETFAIR_EX", "PREVIA"): [("BFEH", "BFED", "BFEA", "BFE>2.5", "BFE<2.5")],
    ("BET365", "CIERRE"):     [("B365CH", "B365CD", "B365CA", "B365C>2.5", "B365C<2.5")],
    ("PINNACLE", "CIERRE"):   [("PSCH", "PSCD", "PSCA", "PC>2.5", "PC<2.5")],
    ("PROMEDIO", "CIERRE"):   [("AvgCH", "AvgCD", "AvgCA", "AvgC>2.5", "AvgC<2.5")],
    ("MAXIMO", "CIERRE"):     [("MaxCH", "MaxCD", "MaxCA", "MaxC>2.5", "MaxC<2.5")],
    ("BETFAIR_EX", "CIERRE"): [("BFECH", "BFECD", "BFECA", "BFEC>2.5", "BFEC<2.5")],
}


def temporada_actual(hoy=None):
    hoy = hoy or date.today()
    return hoy.year if hoy.month >= 7 else hoy.year - 1


# -----------------------------------------------------------------------------
# Descarga y seeds
# -----------------------------------------------------------------------------
def descargar(ruta, intentos=3):
    url = f"{BASE}/{ruta}"
    for i in range(1, intentos + 1):
        try:
            req = Request(url, headers={"User-Agent": "ucl-data-platform/0.1"})
            with urlopen(req, timeout=60) as r:
                return r.status, r.read().decode("utf-8-sig", errors="replace")
        except OSError as e:
            if i == intentos:
                raise
            print(f"  ! {url}: {e}; reintento {i}", file=sys.stderr)
            time.sleep(5 * i)


def ruta_temporada(id_liga, temporada):
    return f"mmz4281/{temporada % 100:02d}{(temporada + 1) % 100:02d}/{LIGAS[id_liga]}.csv"


def leer_seed_equipos():
    with (RAIZ / "seeds" / "equipos_football_data.csv").open(encoding="utf-8") as f:
        return {r["fd_nombre"]: (r["nombre_canonico"], r["pais"]) for r in csv.DictReader(f)}


# -----------------------------------------------------------------------------
# Parseo
# -----------------------------------------------------------------------------
def entero(v):
    v = (v or "").strip()
    return int(float(v)) if v else None


def decimal(v):
    v = (v or "").strip()
    try:
        return float(v) if v else None
    except ValueError:
        return None


def parsear_fecha(texto):
    d, m, a = texto.strip().split("/")
    a = int(a)
    return date(a + 2000 if a < 100 else a, int(m), int(d))


def cuotas_de(fila):
    salida = []
    for (casa, momento), alternativas in CUOTAS.items():
        for cl, ce, cv, co, cu in alternativas:
            l, e, v = decimal(fila.get(cl)), decimal(fila.get(ce)), decimal(fila.get(cv))
            if l and e and v and min(l, e, v) > 1:
                over = decimal(fila.get(co)) if co else None
                under = decimal(fila.get(cu)) if cu else None
                if not (over and under and min(over, under) > 1):
                    over = under = None
                salida.append({"casa": casa, "momento": momento, "cuota_local": l, "cuota_empate": e,
                               "cuota_visita": v, "cuota_over_2_5": over, "cuota_under_2_5": under})
                break
    return salida


def parsear(texto, id_liga, temporada, equipos, desconocidos, es_fixture=False):
    partidos = []
    for fila in csv.DictReader(io.StringIO(texto)):
        if not fila.get("HomeTeam"):
            continue                                   # filas vacías al final del archivo
        if es_fixture and fila.get("Div") != LIGAS[id_liga]:
            continue
        fecha = parsear_fecha(fila["Date"])
        inicio = None
        if (hora := (fila.get("Time") or "").strip()):
            hh, mm = hora.split(":")
            inicio = datetime(fecha.year, fecha.month, fecha.day, int(hh), int(mm), tzinfo=ZONA_HORARIA)
            inicio = inicio.astimezone(timezone.utc).isoformat()
        for lado in ("HomeTeam", "AwayTeam"):
            if fila[lado] not in equipos:
                desconocidos.add((id_liga, fila[lado]))

        gl, gv = entero(fila.get("FTHG")), entero(fila.get("FTAG"))
        jugado = not es_fixture and gl is not None and gv is not None
        if jugado and fila.get("FTR"):
            esperado = "H" if gl > gv else ("D" if gl == gv else "A")
            if fila["FTR"] != esperado:
                raise ValueError(f"{id_liga} {temporada}: FTR {fila['FTR']} no cuadra con {gl}-{gv} "
                                 f"en {fila['HomeTeam']}-{fila['AwayTeam']}")
        partidos.append({
            "id_liga": id_liga,
            "temporada": temporada if temporada is not None else (fecha.year if fecha.month >= 7 else fecha.year - 1),
            "fecha": fecha.isoformat(), "inicio_utc": inicio,
            "fd_local": fila["HomeTeam"], "fd_visita": fila["AwayTeam"],
            "estado": "JUGADO" if jugado else "PROGRAMADO",
            "goles_local_90": gl if jugado else None, "goles_visita_90": gv if jugado else None,
            "goles_local_ht": entero(fila.get("HTHG")) if jugado else None,
            "goles_visita_ht": entero(fila.get("HTAG")) if jugado else None,
            "xg_local": decimal(fila.get("HxG")) if jugado else None,
            "xg_visita": decimal(fila.get("AxG")) if jugado else None,
            "tiros_local": entero(fila.get("HS")) if jugado else None,
            "tiros_visita": entero(fila.get("AS")) if jugado else None,
            "tiros_arco_local": entero(fila.get("HST")) if jugado else None,
            "tiros_arco_visita": entero(fila.get("AST")) if jugado else None,
            "rojas_local": entero(fila.get("HR")) if jugado else None,
            "rojas_visita": entero(fila.get("AR")) if jugado else None,
            "cuotas": cuotas_de(fila),
        })
    return partidos


# -----------------------------------------------------------------------------
# Validaciones
# -----------------------------------------------------------------------------
def validar_temporada(id_liga, temporada, partidos, terminada):
    if not partidos and not terminada:
        return 0                                       # temporada que aún no empieza
    pares = Counter((p["fd_local"], p["fd_visita"]) for p in partidos)
    repetidos = [k for k, n in pares.items() if n > 1]
    if repetidos:
        raise ValueError(f"{id_liga} {temporada}: pares local-visita repetidos: {repetidos[:5]}")
    equipos = {p["fd_local"] for p in partidos} | {p["fd_visita"] for p in partidos}
    n = len(equipos)
    if n not in (18, 20):
        raise ValueError(f"{id_liga} {temporada}: {n} equipos (se esperaban 18 o 20)")
    if terminada:
        esperado = INCOMPLETAS.get((id_liga, temporada), n * (n - 1))
        if len(partidos) != esperado:
            raise ValueError(f"{id_liga} {temporada}: {len(partidos)} partidos, se esperaban {esperado} "
                             "(¿descarga incompleta?)")
    fuera = [p for p in partidos if not (date(temporada, 7, 1).isoformat() <= p["fecha"]
                                         <= date(temporada + 1, 8, 31).isoformat())]
    if fuera:
        raise ValueError(f"{id_liga} {temporada}: {len(fuera)} partidos fuera de la temporada")
    return n


def cargar_temporada(id_liga, temporada, equipos, desconocidos, terminada, intentos=3):
    """Descarga y valida; si la temporada terminada llega incompleta, vuelve a bajarla."""
    for i in range(1, intentos + 1):
        status, texto = descargar(ruta_temporada(id_liga, temporada))
        partidos = parsear(texto, id_liga, temporada, equipos, desconocidos)
        try:
            n = validar_temporada(id_liga, temporada, partidos, terminada)
            return status, texto, partidos, n
        except ValueError as e:
            if i == intentos or "descarga incompleta" not in str(e):
                raise
            print(f"  ! {e}; reintento {i}", file=sys.stderr)
            time.sleep(5 * i)


# -----------------------------------------------------------------------------
# Carga
# -----------------------------------------------------------------------------
COLUMNAS = ["id_liga", "temporada", "fecha", "inicio_utc", "estado", "goles_local_90", "goles_visita_90",
            "goles_local_ht", "goles_visita_ht", "xg_local", "xg_visita", "tiros_local", "tiros_visita",
            "tiros_arco_local", "tiros_arco_visita", "rojas_local", "rojas_visita"]


def guardar_local(partidos):
    destino = RAIZ / "data" / "silver"
    destino.mkdir(parents=True, exist_ok=True)
    with (destino / "football_data_partidos.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS + ["local", "visita"], extrasaction="ignore")
        w.writeheader()
        w.writerows(partidos)
    with (destino / "football_data_cuotas.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id_liga", "temporada", "local", "visita", "casa", "momento",
                    "cuota_local", "cuota_empate", "cuota_visita", "cuota_over_2_5", "cuota_under_2_5"])
        for p in partidos:
            for c in p["cuotas"]:
                w.writerow([p["id_liga"], p["temporada"], p["local"], p["visita"], c["casa"], c["momento"],
                            c["cuota_local"], c["cuota_empate"], c["cuota_visita"],
                            c["cuota_over_2_5"], c["cuota_under_2_5"]])


def cargar_postgres(url, crudos, partidos, equipos):
    import psycopg
    with psycopg.connect(url) as cx:
        cur = cx.cursor()
        cur.executemany(
            """INSERT INTO bronze.ingesta (id_fuente, endpoint, parametros, http_status, payload_texto, payload_hash, procesado)
               VALUES (%s, %s, %s, %s, %s, md5(%s), TRUE)
               ON CONFLICT (id_fuente, endpoint, payload_hash) DO NOTHING""",
            [(FUENTE, ruta, None, status, texto, texto) for ruta, (status, texto) in crudos.items()])

        # Equipos: los clubes UCL ya existen (mismo nombre canónico); solo se agregan los nuevos
        usados = {p[k] for p in partidos for k in ("fd_local", "fd_visita")}
        cur.executemany(
            """INSERT INTO gold.d_equipo (nombre_canonico, pais_iso3) VALUES (%s, %s)
               ON CONFLICT (nombre_canonico) DO NOTHING""",
            sorted({equipos[n] for n in usados}))
        canonicos = sorted({equipos[n][0] for n in usados})
        id_equipo = dict(cur.execute(
            "SELECT nombre_canonico, id_equipo FROM gold.d_equipo WHERE nombre_canonico = ANY(%s)",
            (canonicos,)).fetchall())
        cur.executemany(
            """INSERT INTO gold.d_equipo_alias (id_fuente, alias, id_equipo, validado)
               VALUES (%s, %s, %s, TRUE)
               ON CONFLICT (id_fuente, alias) DO UPDATE SET id_equipo = EXCLUDED.id_equipo, validado = TRUE""",
            [(FUENTE, n, id_equipo[equipos[n][0]]) for n in sorted(usados)])

        # Partidos: un JUGADO nunca vuelve a PROGRAMADO (fixtures viejos no pisan resultados)
        cur.executemany(
            f"""INSERT INTO gold.f_partido_liga ({", ".join(COLUMNAS)}, id_equipo_local, id_equipo_visita)
                VALUES ({", ".join(["%s"] * (len(COLUMNAS) + 2))})
                ON CONFLICT (id_liga, temporada, id_equipo_local, id_equipo_visita) DO UPDATE SET
                    {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNAS[2:])}, actualizado_en = now()
                WHERE gold.f_partido_liga.estado <> 'JUGADO' OR EXCLUDED.estado = 'JUGADO'""",
            [tuple(p[c] for c in COLUMNAS) + (id_equipo[p["local"]], id_equipo[p["visita"]]) for p in partidos])

        ids = {(liga, t, l, v): i for i, liga, t, l, v in cur.execute(
            """SELECT id_partido_liga, id_liga, temporada, id_equipo_local, id_equipo_visita
               FROM gold.f_partido_liga
               WHERE (id_liga, temporada) IN (SELECT * FROM unnest(%s::text[], %s::smallint[]))""",
            ([p["id_liga"] for p in partidos], [p["temporada"] for p in partidos])).fetchall()}

        filas = []
        for p in partidos:
            id_p = ids[(p["id_liga"], p["temporada"], id_equipo[p["local"]], id_equipo[p["visita"]])]
            for c in p["cuotas"]:
                filas.append((id_p, c["casa"], c["momento"], c["cuota_local"], c["cuota_empate"],
                              c["cuota_visita"], c["cuota_over_2_5"], c["cuota_under_2_5"]))
        cur.executemany(
            """INSERT INTO silver.fd_cuotas_liga (id_partido_liga, casa, momento, cuota_local, cuota_empate,
                                                  cuota_visita, cuota_over_2_5, cuota_under_2_5)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id_partido_liga, casa, momento) DO UPDATE SET
                   cuota_local = EXCLUDED.cuota_local, cuota_empate = EXCLUDED.cuota_empate,
                   cuota_visita = EXCLUDED.cuota_visita, cuota_over_2_5 = EXCLUDED.cuota_over_2_5,
                   cuota_under_2_5 = EXCLUDED.cuota_under_2_5, actualizado_en = now()""",
            filas)
        return len(id_equipo), len(filas)


# -----------------------------------------------------------------------------
def expandir_temporadas(texto):
    salida = []
    for parte in texto.split(","):
        if "-" in parte:
            a, b = parte.split("-")
            salida += range(int(a), int(b) + 1)
        else:
            salida.append(int(parte))
    return salida


def main():
    actual = temporada_actual()
    ap = argparse.ArgumentParser(description="Ligas nacionales desde football-data.co.uk")
    ap.add_argument("--ligas", default=",".join(LIGAS), help="ENG1,ESP1,GER1,ITA1,FRA1")
    ap.add_argument("--temporadas", default=str(actual),
                    help=f"años de inicio: '2024', '2012-2025' o '2023,2024' (por defecto {actual})")
    ap.add_argument("--sin-fixtures", action="store_true", help="no cargar los próximos partidos")
    args = ap.parse_args()

    ligas = args.ligas.split(",")
    if set(ligas) - set(LIGAS):
        ap.error(f"ligas desconocidas: {set(ligas) - set(LIGAS)}")
    temporadas = expandir_temporadas(args.temporadas)
    if min(temporadas) < PRIMERA_TEMPORADA or max(temporadas) > actual:
        ap.error(f"temporadas fuera de rango {PRIMERA_TEMPORADA}-{actual}")

    equipos = leer_seed_equipos()
    desconocidos, crudos, partidos = set(), {}, []

    for id_liga in ligas:
        for temporada in temporadas:
            status, texto, ps, n = cargar_temporada(id_liga, temporada, equipos, desconocidos,
                                                    terminada=temporada < actual)
            crudos[ruta_temporada(id_liga, temporada)] = (status, texto)
            jugados = sum(p["estado"] == "JUGADO" for p in ps)
            con_cierre = sum(any(c["momento"] == "CIERRE" for c in p["cuotas"]) for p in ps)
            print(f"{id_liga} {temporada}/{(temporada + 1) % 100:02d}: {jugados} jugados · {n} equipos · "
                  f"{con_cierre} con cuota de cierre")
            partidos += ps

    if not args.sin_fixtures:
        status, texto = descargar("fixtures.csv")
        crudos["fixtures.csv"] = (status, texto)
        jugados = {(p["id_liga"], p["temporada"], p["fd_local"], p["fd_visita"]) for p in partidos}
        # Un fixture que ya figura como jugado en esta corrida se descarta
        nuevos = [p for id_liga in ligas
                  for p in parsear(texto, id_liga, None, equipos, desconocidos, es_fixture=True)
                  if (p["id_liga"], p["temporada"], p["fd_local"], p["fd_visita"]) not in jugados]
        print(f"fixtures: {len(nuevos)} próximos partidos")
        partidos += nuevos

    if desconocidos:
        raise SystemExit(f"Equipos sin mapear en seeds/equipos_football_data.csv: {sorted(desconocidos)}")
    for p in partidos:
        p["local"] = equipos[p["fd_local"]][0]
        p["visita"] = equipos[p["fd_visita"]][0]

    url_bd = os.getenv("DATABASE_URL")
    if url_bd:
        n_equipos, n_cuotas = cargar_postgres(url_bd, crudos, partidos, equipos)
        print(f"Cargado en PostgreSQL: {len(partidos)} partidos, {n_equipos} equipos, {n_cuotas} cuotas")
    else:
        guardar_local(partidos)
        print(f"Sin DATABASE_URL: CSV en {RAIZ / 'data' / 'silver'}")


if __name__ == "__main__":
    main()
