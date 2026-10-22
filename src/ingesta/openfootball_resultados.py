"""
Carga histórica de resultados de la UEFA Champions League desde openfootball
(dominio público, formato Football.TXT).

Fuente : https://github.com/openfootball/champions-league  ({temporada}/cl.txt)
Cubre  : 2015-16 .. 2025-26 (fase de grupos / liga + eliminatorias; sin previas)

Pasos:
  1. Descarga cl.txt de cada temporada (bronze.ingesta, texto crudo).
  2. Parsea partidos: ronda, jornada, grupo, marcador 90' / prórroga / penales.
  3. Resuelve equipos con seeds/equipos_openfootball.csv (falla si aparece un
     nombre nuevo, para revisarlo a mano).
  4. Arma las llaves de eliminatoria (f_serie) y quién avanza.
  5. Valida contra el encabezado del archivo y la continuidad entre rondas.
  6. Carga gold.d_equipo, gold.d_equipo_alias, gold.f_serie y gold.f_partido
     (upsert idempotente). Sin DATABASE_URL deja CSV en data/silver/.

Uso:
    py src/ingesta/openfootball_resultados.py
    py src/ingesta/openfootball_resultados.py --temporadas 2023-24,2024-25

Autor : Ruth Choque (con PROMETHEUS) · 2026-10-21
"""

import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

URL = "https://raw.githubusercontent.com/openfootball/champions-league/master/{temporada}/cl.txt"
TEMPORADAS = [f"{y}-{str(y + 1)[2:]}" for y in range(2015, 2026)]
ZONA_HORARIA = ZoneInfo("Europe/Madrid")   # openfootball publica la hora en CET/CEST
RAIZ = Path(__file__).resolve().parents[2]

RONDAS = {                                  # sección -> id_ronda (gold.d_ronda)
    "Round of 16": 30, "Quarterfinals": 40, "Semifinals": 50, "Final": 60,
}
ID_GRUPOS, ID_LIGA, ID_PLAYOFF, ID_FINAL = 10, 11, 20, 60

RE_SECCION = re.compile(r"^▪\s+(.+?)\s*$")
RE_FECHA = re.compile(r"^\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([A-Z][a-z]{2}) (\d{1,2})(?: (\d{4}))?\s*$")
RE_PARTIDO = re.compile(
    r"^\s+(?:(\d{1,2})[:.](\d{2})\s+)?"
    r"(?P<local>.+?\([A-Z]{3}\))\s+v\s+(?P<visita>.+?\([A-Z]{3}\))\s+"
    r"(?:(?P<pl>\d+)-(?P<pv>\d+) pen\.\s+)?"
    r"(?P<gl>\d+)-(?P<gv>\d+)(?P<aet> a\.e\.t\.)?"
    r"(?:\s*\((?P<p1l>\d+)-(?P<p1v>\d+)(?:,\s*(?P<p2l>\d+)-(?P<p2v>\d+))?\))?\s*$"
)
# Prórrogas que el cl.txt publica sin 'a.e.t.' ni marcador al 90' (detectadas
# al validar contra ESPN): (fecha, alias local) -> goles al 90' (local, visita)
CORRECCIONES_PRORROGA = {
    ("2025-02-19", "PSV (NED)"): (2, 1),                        # PSV 3-1 Juventus (Flamingo 98')
    ("2025-05-06", "FC Internazionale Milano (ITA)"): (3, 3),   # Inter 4-3 Barcelona (Frattesi 99')
}
MESES = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


# -----------------------------------------------------------------------------
# Descarga y seeds
# -----------------------------------------------------------------------------
def descargar(temporada):
    req = Request(URL.format(temporada=temporada), headers={"User-Agent": "ucl-data-platform/0.1"})
    with urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8")


def leer_seed_equipos():
    with (RAIZ / "seeds" / "equipos_openfootball.csv").open(encoding="utf-8") as f:
        return {f"{r['alias']} ({r['pais_alias']})": (r["nombre_canonico"], r["pais"])
                for r in csv.DictReader(f)}


def leer_seed_grupos_2023():
    with (RAIZ / "seeds" / "grupos_2023_24.csv").open(encoding="utf-8") as f:
        return {r["equipo_referencia"]: r["grupo"] for r in csv.DictReader(f)}


# -----------------------------------------------------------------------------
# Parseo de Football.TXT
# -----------------------------------------------------------------------------
def parsear(temporada, texto, equipos):
    id_temporada = int(temporada[:4])
    partidos, desconocidos = [], set()
    seccion = None

    fecha = hora = None

    for linea in texto.splitlines():
        if m := RE_SECCION.match(linea):
            seccion = m.group(1)
            continue
        if m := RE_FECHA.match(linea):
            # El año se deduce de la temporada: sep-dic = año de inicio, ene-ago = año
            # siguiente (2019-20 terminó en agosto de 2020). No sirve "arrastrar" el año
            # porque los archivos por grupo reinician en septiembre en cada grupo.
            mes = MESES[m.group(1)]
            anio = id_temporada if mes >= 9 else id_temporada + 1
            if m.group(3) and int(m.group(3)) != anio:
                raise ValueError(f"{temporada}: año inesperado en {linea.strip()!r}")
            fecha, hora = date(anio, mes, int(m.group(2))), None
            continue
        if " v " not in linea:
            continue
        m = RE_PARTIDO.match(linea)
        if not m:
            raise ValueError(f"{temporada}: línea de partido no reconocida: {linea!r}")
        if m.group(1):
            hora = (int(m.group(1)), int(m.group(2)))

        for lado in ("local", "visita"):
            if m.group(lado) not in equipos:
                desconocidos.add(m.group(lado))

        gl, gv = int(m["gl"]), int(m["gv"])
        correccion = CORRECCIONES_PRORROGA.get((fecha.isoformat(), m["local"]))
        aet = bool(m["aet"]) or correccion is not None
        if correccion:
            l90, v90 = correccion
            lpro, vpro = gl, gv
        elif m["aet"]:
            l90, v90 = (int(m["p1l"]), int(m["p1v"])) if m["p1l"] else (gl, gv)
            lpro, vpro = gl, gv
        else:
            l90, v90, lpro, vpro = gl, gv, None, None

        id_ronda, jornada, grupo, partido_serie = clasificar_seccion(seccion)
        inicio = None
        if hora:
            inicio = datetime(fecha.year, fecha.month, fecha.day, *hora, tzinfo=ZONA_HORARIA)
            inicio = inicio.astimezone(timezone.utc).isoformat()

        partidos.append({
            "id_temporada": id_temporada, "seccion": seccion, "id_ronda": id_ronda,
            "jornada": jornada, "grupo": grupo, "partido_serie": partido_serie,
            "fecha": fecha.isoformat(), "inicio_utc": inicio,
            "alias_local": m["local"], "alias_visita": m["visita"],
            "goles_local_90": l90, "goles_visita_90": v90,
            "goles_local_prorroga": lpro, "goles_visita_prorroga": vpro,
            "penales_local": int(m["pl"]) if m["pl"] else None,
            "penales_visita": int(m["pv"]) if m["pv"] else None,
            "metodo_definicion": "PENALES" if m["pl"] else ("PRORROGA" if aet else "90"),
        })

    if desconocidos:
        raise ValueError(f"{temporada}: equipos sin mapear en seeds/equipos_openfootball.csv: "
                         f"{sorted(desconocidos)}")
    for p in partidos:
        p["local"] = equipos[p["alias_local"]][0]
        p["visita"] = equipos[p["alias_visita"]][0]
    return partidos


def clasificar_seccion(seccion):
    """'Group A' / 'Gruppe G' / 'Group, Matchday 3' / 'League, Matchday 5' /
    'Playoffs, Matchday 1' / 'Finals, Round of 16' / 'Final'
    -> (id_ronda, jornada, grupo, partido_serie)"""
    if m := re.match(r"^(?:Group|Gruppe) ([A-H])$", seccion):
        return ID_GRUPOS, None, m.group(1), None
    if m := re.match(r"^(Group|League|Playoffs), Matchday (\d+)$", seccion):
        tipo, n = m.group(1), int(m.group(2))
        if tipo == "Playoffs":
            return ID_PLAYOFF, None, None, n
        return (ID_GRUPOS if tipo == "Group" else ID_LIGA), n, None, None
    nombre = seccion.removeprefix("Finals, ")
    if nombre in RONDAS:
        return RONDAS[nombre], None, None, None
    raise ValueError(f"Sección desconocida: {seccion!r}")


# -----------------------------------------------------------------------------
# Derivaciones: jornada / grupo, llaves de eliminatoria
# -----------------------------------------------------------------------------
def completar_fase_grupos(partidos, temporada, grupos_2023):
    fase = [p for p in partidos if p["id_ronda"] == ID_GRUPOS]
    if not fase:
        return

    # 2023-24 viene por jornada: el grupo se deduce de quién juega contra quién
    if any(p["grupo"] is None for p in fase):
        vecinos = defaultdict(set)
        for p in fase:
            vecinos[p["local"]].add(p["visita"])
            vecinos[p["visita"]].add(p["local"])
        letra_de = {}
        for ref, letra in grupos_2023.items():
            pendientes, visto = [ref], {ref}
            while pendientes:
                e = pendientes.pop()
                letra_de[e] = letra
                for v in vecinos[e] - visto:
                    visto.add(v)
                    pendientes.append(v)
        for p in fase:
            p["grupo"] = letra_de.get(p["local"])
        if any(p["grupo"] is None for p in fase):
            raise ValueError(f"{temporada}: equipos sin grupo asignado (revisar seeds/grupos_2023_24.csv)")

    # Jornada: n-ésimo partido de cada equipo en la fase (si no viene en la sección)
    if any(p["jornada"] is None for p in fase):
        jugados = Counter()
        for p in sorted(fase, key=lambda p: (p["fecha"], p["inicio_utc"] or "")):
            jl, jv = jugados[p["local"]] + 1, jugados[p["visita"]] + 1
            if jl != jv:
                print(f"  ! {temporada}: jornada inconsistente {p['local']}-{p['visita']} ({jl} vs {jv})",
                      file=sys.stderr)
            p["jornada"] = max(jl, jv)
            jugados[p["local"]] += 1
            jugados[p["visita"]] += 1


def armar_series(partidos, regla_gol_visitante):
    """Agrupa las eliminatorias por par de equipos. Devuelve la lista de series
    (ida y vuelta) y marca id_equipo_avanza / es_neutral en los partidos."""
    series = []
    por_llave = defaultdict(list)
    for p in partidos:
        p["es_neutral"] = p["id_ronda"] == ID_FINAL
        p["equipo_avanza"] = None
        p["clave_serie"] = None
        if p["id_ronda"] >= ID_PLAYOFF:
            por_llave[(p["id_ronda"], frozenset((p["local"], p["visita"])))].append(p)

    for (id_ronda, _), ps in por_llave.items():
        ps.sort(key=lambda p: (p["fecha"], p["inicio_utc"] or ""))
        if len(ps) == 1:                       # partido único: final o Lisboa 2019-20
            p = ps[0]
            p["es_neutral"] = True
            p["partido_serie"] = None
            p["equipo_avanza"] = ganador_partido(p)
            continue
        if len(ps) != 2:
            raise ValueError(f"Llave con {len(ps)} partidos: {[x['local'] + '-' + x['visita'] for x in ps]}")

        ida, vuelta = ps
        ida["partido_serie"], vuelta["partido_serie"] = 1, 2
        a, b = ida["local"], ida["visita"]
        vl = goles_finales(vuelta)              # (local, visita) de la vuelta incl. prórroga
        global_a = ida["goles_local_90"] + vl[1]
        global_b = ida["goles_visita_90"] + vl[0]
        visitante_a, visitante_b = vl[1], ida["goles_visita_90"]

        if global_a != global_b:
            avanza = a if global_a > global_b else b
            metodo = "PRORROGA" if vuelta["metodo_definicion"] == "PRORROGA" else "GLOBAL"
        elif regla_gol_visitante and visitante_a != visitante_b:
            avanza = a if visitante_a > visitante_b else b
            metodo = "GOL_VISITANTE"
        elif vuelta["metodo_definicion"] == "PENALES":
            avanza = b if vuelta["penales_local"] > vuelta["penales_visita"] else a
            metodo = "PENALES"
        else:
            raise ValueError(f"No se pudo definir la llave {a} - {b}")

        vuelta["equipo_avanza"] = avanza
        clave = (id_ronda, a, b)
        ida["clave_serie"] = vuelta["clave_serie"] = clave
        series.append({
            "id_temporada": ida["id_temporada"], "id_ronda": id_ronda,
            "equipo_a": a, "equipo_b": b, "global_a": global_a, "global_b": global_b,
            "goles_visitante_a": visitante_a, "goles_visitante_b": visitante_b,
            "metodo_definicion": metodo, "equipo_avanza": avanza, "clave": clave,
        })
    return series


def goles_finales(p):
    if p["goles_local_prorroga"] is not None:
        return p["goles_local_prorroga"], p["goles_visita_prorroga"]
    return p["goles_local_90"], p["goles_visita_90"]


def ganador_partido(p):
    if p["penales_local"] is not None:
        return p["local"] if p["penales_local"] > p["penales_visita"] else p["visita"]
    gl, gv = goles_finales(p)
    return p["local"] if gl > gv else p["visita"]


# -----------------------------------------------------------------------------
# Validaciones
# -----------------------------------------------------------------------------
def validar(temporada, texto, partidos):
    esperado = re.search(r"^# Matches\s+(\d+)", texto, re.M)
    if esperado and int(esperado.group(1)) != len(partidos):
        raise ValueError(f"{temporada}: {len(partidos)} partidos parseados vs {esperado.group(1)} en el encabezado")

    # Continuidad: los que avanzan en una ronda son exactamente los de la siguiente
    rondas = sorted({p["id_ronda"] for p in partidos if p["id_ronda"] >= ID_PLAYOFF})
    for r1, r2 in zip(rondas, rondas[1:]):
        avanzan = {p["equipo_avanza"] for p in partidos if p["id_ronda"] == r1 and p["equipo_avanza"]}
        siguientes = {e for p in partidos if p["id_ronda"] == r2 for e in (p["local"], p["visita"])}
        if r1 == ID_PLAYOFF:
            if not avanzan <= siguientes:
                raise ValueError(f"{temporada}: ganadores del playoff ausentes en octavos: {avanzan - siguientes}")
        elif avanzan != siguientes:
            raise ValueError(f"{temporada}: ronda {r1}->{r2} no cuadra: {avanzan ^ siguientes}")

    final = [p for p in partidos if p["id_ronda"] == ID_FINAL]
    return final[0]["equipo_avanza"] if final else None


# -----------------------------------------------------------------------------
# Carga
# -----------------------------------------------------------------------------
def guardar_local(todos, series):
    destino = RAIZ / "data" / "silver"
    destino.mkdir(parents=True, exist_ok=True)
    columnas = [k for k in todos[0] if k not in ("clave_serie",)]
    with (destino / "openfootball_partidos.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columnas, extrasaction="ignore")
        w.writeheader()
        w.writerows(todos)
    with (destino / "openfootball_series.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[k for k in series[0] if k != "clave"], extrasaction="ignore")
        w.writeheader()
        w.writerows(series)


def cargar_postgres(url, crudos, todos, series, equipos):
    import psycopg
    with psycopg.connect(url) as cx:
        for temporada, (status, texto) in crudos.items():
            cx.execute(
                """INSERT INTO bronze.ingesta (id_fuente, endpoint, parametros, http_status, payload_texto, payload_hash, procesado)
                   VALUES ('openfootball', %s, %s, %s, %s, md5(%s), TRUE)
                   ON CONFLICT (id_fuente, endpoint, payload_hash) DO NOTHING""",
                (f"champions-league/{temporada}/cl.txt", '{"temporada": "%s"}' % temporada, status, texto, texto))

        # Equipos y alias
        id_equipo = {}
        for canonico, pais in sorted(set(equipos.values())):
            id_equipo[canonico] = cx.execute(
                """INSERT INTO gold.d_equipo (nombre_canonico, pais_iso3) VALUES (%s, %s)
                   ON CONFLICT (nombre_canonico) DO UPDATE SET pais_iso3 = EXCLUDED.pais_iso3
                   RETURNING id_equipo""", (canonico, pais)).fetchone()[0]
        for alias, (canonico, _) in equipos.items():
            cx.execute(
                """INSERT INTO gold.d_equipo_alias (id_fuente, alias, id_equipo, validado)
                   VALUES ('openfootball', %s, %s, TRUE)
                   ON CONFLICT (id_fuente, alias) DO UPDATE SET id_equipo = EXCLUDED.id_equipo, validado = TRUE""",
                (alias, id_equipo[canonico]))

        # Series
        id_serie = {}
        for s in series:
            id_serie[(s["id_temporada"], s["clave"])] = cx.execute(
                """INSERT INTO gold.f_serie (id_temporada, id_ronda, id_equipo_a, id_equipo_b, global_a, global_b,
                                             goles_visitante_a, goles_visitante_b, metodo_definicion, id_equipo_avanza)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id_temporada, id_ronda, id_equipo_a, id_equipo_b) DO UPDATE SET
                       global_a = EXCLUDED.global_a, global_b = EXCLUDED.global_b,
                       goles_visitante_a = EXCLUDED.goles_visitante_a, goles_visitante_b = EXCLUDED.goles_visitante_b,
                       metodo_definicion = EXCLUDED.metodo_definicion, id_equipo_avanza = EXCLUDED.id_equipo_avanza
                   RETURNING id_serie""",
                (s["id_temporada"], s["id_ronda"], id_equipo[s["equipo_a"]], id_equipo[s["equipo_b"]],
                 s["global_a"], s["global_b"], s["goles_visitante_a"], s["goles_visitante_b"],
                 s["metodo_definicion"], id_equipo[s["equipo_avanza"]])).fetchone()[0]

        # Partidos
        for p in todos:
            serie = id_serie.get((p["id_temporada"], p["clave_serie"])) if p["clave_serie"] else None
            cx.execute(
                """INSERT INTO gold.f_partido (
                       id_temporada, id_ronda, jornada, grupo, id_serie, partido_serie, fecha, inicio_utc,
                       id_equipo_local, id_equipo_visita, es_neutral, estado,
                       goles_local_90, goles_visita_90, goles_local_prorroga, goles_visita_prorroga,
                       penales_local, penales_visita, metodo_definicion, id_equipo_avanza, id_fuente_principal)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'JUGADO',
                           %s, %s, %s, %s, %s, %s, %s, %s, 'openfootball')
                   ON CONFLICT (id_temporada, id_ronda, id_equipo_local, id_equipo_visita) DO UPDATE SET
                       jornada = EXCLUDED.jornada, grupo = EXCLUDED.grupo, id_serie = EXCLUDED.id_serie,
                       partido_serie = EXCLUDED.partido_serie, fecha = EXCLUDED.fecha, inicio_utc = EXCLUDED.inicio_utc,
                       es_neutral = EXCLUDED.es_neutral, estado = EXCLUDED.estado,
                       goles_local_90 = EXCLUDED.goles_local_90, goles_visita_90 = EXCLUDED.goles_visita_90,
                       goles_local_prorroga = EXCLUDED.goles_local_prorroga, goles_visita_prorroga = EXCLUDED.goles_visita_prorroga,
                       penales_local = EXCLUDED.penales_local, penales_visita = EXCLUDED.penales_visita,
                       metodo_definicion = EXCLUDED.metodo_definicion, id_equipo_avanza = EXCLUDED.id_equipo_avanza,
                       actualizado_en = now()""",
                (p["id_temporada"], p["id_ronda"], p["jornada"], p["grupo"], serie, p["partido_serie"],
                 p["fecha"], p["inicio_utc"], id_equipo[p["local"]], id_equipo[p["visita"]], p["es_neutral"],
                 p["goles_local_90"], p["goles_visita_90"], p["goles_local_prorroga"], p["goles_visita_prorroga"],
                 p["penales_local"], p["penales_visita"], p["metodo_definicion"],
                 id_equipo[p["equipo_avanza"]] if p["equipo_avanza"] else None))

        # Rango de fechas de cada temporada cargada
        cx.execute("""UPDATE gold.d_temporada t SET fecha_inicio = x.ini, fecha_fin = x.fin
                      FROM (SELECT id_temporada, MIN(fecha) ini, MAX(fecha) fin FROM gold.f_partido
                            WHERE id_fuente_principal = 'openfootball' GROUP BY id_temporada) x
                      WHERE x.id_temporada = t.id_temporada""")
        return id_equipo


# -----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Carga histórica openfootball - Champions League")
    ap.add_argument("--temporadas", default=",".join(TEMPORADAS))
    args = ap.parse_args()

    equipos = leer_seed_equipos()
    grupos_2023 = leer_seed_grupos_2023()
    crudos, todos, series = {}, [], []

    for temporada in args.temporadas.split(","):
        status, texto = descargar(temporada)
        crudos[temporada] = (status, texto)
        partidos = parsear(temporada, texto, equipos)
        completar_fase_grupos(partidos, temporada, grupos_2023)
        series_t = armar_series(partidos, regla_gol_visitante=int(temporada[:4]) <= 2020)
        campeon = validar(temporada, texto, partidos)
        n_ronda = Counter(p["id_ronda"] for p in partidos)
        print(f"{temporada}: {len(partidos)} partidos {dict(sorted(n_ronda.items()))} · "
              f"{len(series_t)} llaves · campeón {campeon}")
        todos += partidos
        series += series_t

    url_bd = os.getenv("DATABASE_URL")
    if url_bd:
        id_equipo = cargar_postgres(url_bd, crudos, todos, series, equipos)
        print(f"Cargado en PostgreSQL: {len(todos)} partidos, {len(series)} llaves, {len(id_equipo)} equipos")
    else:
        guardar_local(todos, series)
        print(f"Sin DATABASE_URL: CSV en {RAIZ / 'data' / 'silver'}")


if __name__ == "__main__":
    main()
