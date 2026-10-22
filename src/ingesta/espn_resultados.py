"""
Resultados y calendario de la UEFA Champions League desde la API no oficial de
ESPN, para las temporadas que openfootball no cubre (2026-27 en adelante).

Por temporada Y consulta el scoreboard de los años calendario Y e Y+1
(?dates=AAAA trae el año completo) y se queda con la competición principal
(sin rondas previas).

Pasos:
  1. Guarda el scoreboard crudo en bronze.ingesta.
  2. Parsea cada partido: ronda, estado y marcador. ESPN da el marcador final
     (incluida la prórroga): el 90' se reconstruye con los goles de 'details'
     hasta el minuto 90 (+ descuento). Validado contra openfootball 2015-16 ..
     2025-26: 1.495 de 1.497 iguales; las 2 diferencias eran errores de
     openfootball (prórrogas sin 'a.e.t.').
  3. Resuelve equipos con seeds/equipos_espn.csv (por id de ESPN; falla si
     aparece uno nuevo, para curarlo a mano).
  4. Jornada de la fase de liga = n-ésimo partido de cada equipo.
  5. Arma las llaves terminadas (misma lógica que openfootball).
  6. Upsert en gold.d_equipo, gold.d_equipo_alias, gold.f_serie y
     gold.f_partido. Nunca pisa partidos cargados desde otra fuente.

Uso:
    py src/ingesta/espn_resultados.py                    # temporada actual (2026)
    py src/ingesta/espn_resultados.py --temporadas 2026

Sin DATABASE_URL deja CSV en data/silver/.

Autor : Ruth Choque (con PROMETHEUS) · 2026-10-22
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from espn_cuotas import URL_BASE, PAUSA_S, _sin_ruido, http_json
from openfootball_resultados import ID_FINAL, ID_LIGA, ID_PLAYOFF, armar_series

RAIZ = Path(__file__).resolve().parents[2]
LIGA = "uefa.champions"

RONDAS = {                                  # season.slug de ESPN -> id_ronda (gold.d_ronda)
    "league-phase": ID_LIGA, "knockout-round-playoffs": ID_PLAYOFF,
    "round-of-16": 30, "quarterfinals": 40, "semifinals": 50, "final": ID_FINAL,
}
ESTADOS = {                                 # status.type.name -> gold.f_partido.estado
    "STATUS_SCHEDULED": "PROGRAMADO",
    "STATUS_FULL_TIME": "JUGADO", "STATUS_FINAL_AET": "JUGADO",
    "STATUS_FINAL_PEN": "JUGADO", "STATUS_FINAL_AGT": "JUGADO",
    "STATUS_POSTPONED": "APLAZADO", "STATUS_SUSPENDED": "SUSPENDIDO",
    "STATUS_ABANDONED": "SUSPENDIDO",
}
RE_MINUTO = re.compile(r"^(\d+)'")


def leer_seed_equipos():
    with (RAIZ / "seeds" / "equipos_espn.csv").open(encoding="utf-8") as f:
        return {r["espn_id"]: (r["nombre_canonico"], r["pais"], r["espn_nombre"])
                for r in csv.DictReader(f)}


# -----------------------------------------------------------------------------
# Parseo
# -----------------------------------------------------------------------------
def goles_90(comp, lado_de):
    """Goles hasta el 90' (+ descuento) a partir de los eventos de gol."""
    g = {"home": 0, "away": 0}
    for d in comp.get("details") or []:
        if not d.get("scoringPlay") or d.get("shootout"):
            continue
        m = RE_MINUTO.match((d.get("clock") or {}).get("displayValue") or "")
        lado = lado_de.get((d.get("team") or {}).get("id"))
        if m and lado and int(m.group(1)) <= 90:
            g[lado] += 1
    return g["home"], g["away"]


def parsear(evento, id_temporada, equipos, desconocidos):
    comp = evento["competitions"][0]
    t = {c["homeAway"]: c for c in comp["competitors"]}
    for c in t.values():
        if c["team"]["id"] not in equipos:
            desconocidos[c["team"]["id"]] = c["team"]["displayName"]
    estado_espn = evento["status"]["type"]["name"]
    if estado_espn not in ESTADOS:
        raise ValueError(f"Estado ESPN desconocido {estado_espn!r} en el evento {evento['id']}")
    estado = ESTADOS[estado_espn]

    p = {
        "id_temporada": id_temporada, "espn_event_id": evento["id"],
        "id_ronda": RONDAS[evento["season"]["slug"]], "jornada": None, "grupo": None,
        "partido_serie": None, "fecha": evento["date"][:10], "inicio_utc": evento["date"],
        "id_local_espn": t["home"]["team"]["id"], "id_visita_espn": t["away"]["team"]["id"],
        "estado": estado, "asistencia": comp.get("attendance") or None,
        "goles_local_90": None, "goles_visita_90": None,
        "goles_local_prorroga": None, "goles_visita_prorroga": None,
        "penales_local": None, "penales_visita": None, "metodo_definicion": None,
    }
    if estado == "JUGADO":
        lado_de = {t["home"]["team"]["id"]: "home", t["away"]["team"]["id"]: "away"}
        l90, v90 = goles_90(comp, lado_de)
        lf, vf = int(t["home"]["score"]), int(t["away"]["score"])
        p["goles_local_90"], p["goles_visita_90"] = l90, v90
        if estado_espn in ("STATUS_FINAL_AET", "STATUS_FINAL_PEN"):
            p["goles_local_prorroga"], p["goles_visita_prorroga"] = lf, vf
        elif (l90, v90) != (lf, vf):
            raise ValueError(f"Evento {evento['id']}: goles de details {l90}-{v90} vs marcador {lf}-{vf}")
        if estado_espn == "STATUS_FINAL_PEN":
            p["penales_local"] = int(t["home"]["shootoutScore"])
            p["penales_visita"] = int(t["away"]["shootoutScore"])
            p["metodo_definicion"] = "PENALES"
        else:
            p["metodo_definicion"] = "PRORROGA" if estado_espn == "STATUS_FINAL_AET" else "90"
    return p


def completar_jornadas(partidos):
    """Fase de liga: jornada = n-ésimo partido de cada equipo (incluye programados)."""
    jugados = Counter()
    for p in sorted((p for p in partidos if p["id_ronda"] == ID_LIGA), key=lambda p: p["inicio_utc"]):
        jl, jv = jugados[p["local"]] + 1, jugados[p["visita"]] + 1
        if jl != jv:
            print(f"  ! jornada inconsistente {p['local']}-{p['visita']} ({jl} vs {jv})", file=sys.stderr)
        p["jornada"] = max(jl, jv)
        jugados[p["local"]] += 1
        jugados[p["visita"]] += 1


def armar_series_terminadas(partidos, id_temporada):
    """armar_series necesita las dos piernas jugadas: las llaves en curso quedan
    sin serie (se completan en la próxima corrida)."""
    por_llave = defaultdict(list)
    for p in partidos:
        if p["id_ronda"] >= ID_PLAYOFF:
            por_llave[(p["id_ronda"], frozenset((p["local"], p["visita"])))].append(p)
    en_curso = [p for ps in por_llave.values() if any(x["estado"] != "JUGADO" for x in ps) for p in ps]
    ids_en_curso = {id(p) for p in en_curso}
    series = armar_series([p for p in partidos if id(p) not in ids_en_curso],
                          regla_gol_visitante=id_temporada <= 2020)
    for ps in por_llave.values():
        if not any(id(p) in ids_en_curso for p in ps):
            continue
        ps.sort(key=lambda p: p["inicio_utc"])
        for n, p in enumerate(ps, 1):
            p["partido_serie"] = n if len(ps) == 2 else None
            p["es_neutral"] = p["id_ronda"] == ID_FINAL
            p["equipo_avanza"] = None
            p["clave_serie"] = None
    return series


def validar(id_temporada, partidos):
    liga = [p for p in partidos if p["id_ronda"] == ID_LIGA]
    if liga:
        por_equipo = Counter(e for p in liga for e in (p["local"], p["visita"]))
        if len(liga) != 144 or set(por_equipo.values()) != {8}:
            raise ValueError(f"{id_temporada}: fase de liga con {len(liga)} partidos y "
                             f"partidos por equipo {sorted(set(por_equipo.values()))} (esperado 144 y 8)")
        if any(not 1 <= p["jornada"] <= 8 for p in liga):
            raise ValueError(f"{id_temporada}: jornadas fuera de 1..8")


# -----------------------------------------------------------------------------
# Carga
# -----------------------------------------------------------------------------
def guardar_local(partidos, series):
    destino = RAIZ / "data" / "silver"
    destino.mkdir(parents=True, exist_ok=True)
    with (destino / "espn_partidos.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[k for k in partidos[0] if k != "clave_serie"], extrasaction="ignore")
        w.writeheader()
        w.writerows(partidos)
    if series:
        with (destino / "espn_series.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[k for k in series[0] if k != "clave"], extrasaction="ignore")
            w.writeheader()
            w.writerows(series)


def cargar_postgres(url, crudos, partidos, series, equipos):
    import psycopg
    with psycopg.connect(url) as cx:
        for anio, (status, payload) in crudos.items():
            texto = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            cx.execute(
                """INSERT INTO bronze.ingesta (id_fuente, endpoint, parametros, http_status, payload_json, payload_hash, procesado)
                   VALUES ('espn', 'scoreboard', %s, %s, %s, md5(%s), TRUE)
                   ON CONFLICT (id_fuente, endpoint, payload_hash) DO NOTHING""",
                (json.dumps({"liga": LIGA, "dates": anio}), status, texto, texto))

        # Equipos y alias (solo los que aparecen en lo cargado)
        usados = {p[k] for p in partidos for k in ("id_local_espn", "id_visita_espn")}
        id_equipo = {}
        for espn_id in sorted(usados):
            canonico, pais, nombre = equipos[espn_id]
            id_equipo[canonico] = cx.execute(
                """INSERT INTO gold.d_equipo (nombre_canonico, pais_iso3) VALUES (%s, %s)
                   ON CONFLICT (nombre_canonico) DO UPDATE SET pais_iso3 = EXCLUDED.pais_iso3
                   RETURNING id_equipo""", (canonico, pais)).fetchone()[0]
            cx.execute(
                """INSERT INTO gold.d_equipo_alias (id_fuente, alias, id_externo, id_equipo, validado)
                   VALUES ('espn', %s, %s, %s, TRUE)
                   ON CONFLICT (id_fuente, alias) DO UPDATE SET
                       id_externo = EXCLUDED.id_externo, id_equipo = EXCLUDED.id_equipo, validado = TRUE""",
                (nombre, espn_id, id_equipo[canonico]))

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

        n = 0
        for p in partidos:
            serie = id_serie.get((p["id_temporada"], p["clave_serie"])) if p["clave_serie"] else None
            cur = cx.execute(
                """INSERT INTO gold.f_partido (
                       id_temporada, id_ronda, jornada, grupo, id_serie, partido_serie, fecha, inicio_utc,
                       id_equipo_local, id_equipo_visita, es_neutral, asistencia, estado,
                       goles_local_90, goles_visita_90, goles_local_prorroga, goles_visita_prorroga,
                       penales_local, penales_visita, metodo_definicion, id_equipo_avanza, id_fuente_principal)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s, 'espn')
                   ON CONFLICT (id_temporada, id_ronda, id_equipo_local, id_equipo_visita) DO UPDATE SET
                       jornada = EXCLUDED.jornada, id_serie = EXCLUDED.id_serie,
                       partido_serie = EXCLUDED.partido_serie, fecha = EXCLUDED.fecha, inicio_utc = EXCLUDED.inicio_utc,
                       es_neutral = EXCLUDED.es_neutral, asistencia = EXCLUDED.asistencia, estado = EXCLUDED.estado,
                       goles_local_90 = EXCLUDED.goles_local_90, goles_visita_90 = EXCLUDED.goles_visita_90,
                       goles_local_prorroga = EXCLUDED.goles_local_prorroga, goles_visita_prorroga = EXCLUDED.goles_visita_prorroga,
                       penales_local = EXCLUDED.penales_local, penales_visita = EXCLUDED.penales_visita,
                       metodo_definicion = EXCLUDED.metodo_definicion, id_equipo_avanza = EXCLUDED.id_equipo_avanza,
                       actualizado_en = now()
                   WHERE gold.f_partido.id_fuente_principal = 'espn'""",
                (p["id_temporada"], p["id_ronda"], p["jornada"], p["grupo"], serie, p["partido_serie"],
                 p["fecha"], p["inicio_utc"], id_equipo[p["local"]], id_equipo[p["visita"]], p["es_neutral"],
                 p["asistencia"], p["estado"],
                 p["goles_local_90"], p["goles_visita_90"], p["goles_local_prorroga"], p["goles_visita_prorroga"],
                 p["penales_local"], p["penales_visita"], p["metodo_definicion"],
                 id_equipo[p["equipo_avanza"]] if p["equipo_avanza"] else None))
            n += cur.rowcount

        cx.execute("""UPDATE gold.d_temporada t SET fecha_inicio = x.ini, fecha_fin = x.fin
                      FROM (SELECT id_temporada, MIN(fecha) ini, MAX(fecha) fin FROM gold.f_partido
                            WHERE id_fuente_principal = 'espn' GROUP BY id_temporada) x
                      WHERE x.id_temporada = t.id_temporada""")
        return n


# -----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Resultados y calendario ESPN - Champions League")
    ap.add_argument("--temporadas", default="2026", help="Años de inicio separados por coma (2026 = 2026-27)")
    args = ap.parse_args()

    equipos = leer_seed_equipos()
    crudos, partidos, series = {}, [], []
    for id_temporada in (int(t) for t in args.temporadas.split(",")):
        eventos, desconocidos = {}, {}
        for anio in (id_temporada, id_temporada + 1):
            status, sb = http_json(URL_BASE.format(liga=LIGA, recurso="scoreboard") + f"?dates={anio}&limit=1000")
            crudos[str(anio)] = (status, _sin_ruido(sb))
            for e in sb.get("events") or []:
                if e["season"]["year"] == id_temporada and e["season"]["slug"] in RONDAS:
                    eventos[e["id"]] = e
            time.sleep(PAUSA_S)

        ps = [parsear(e, id_temporada, equipos, desconocidos) for e in eventos.values()]
        if desconocidos:
            raise ValueError(f"{id_temporada}: equipos ESPN sin mapear en seeds/equipos_espn.csv: {desconocidos}")
        for p in ps:
            p["local"], p["visita"] = equipos[p["id_local_espn"]][0], equipos[p["id_visita_espn"]][0]
        completar_jornadas(ps)
        series_t = armar_series_terminadas(ps, id_temporada)
        validar(id_temporada, ps)
        n_ronda = Counter(p["id_ronda"] for p in ps)
        n_estado = Counter(p["estado"] for p in ps)
        print(f"{id_temporada}: {len(ps)} partidos {dict(sorted(n_ronda.items()))} {dict(n_estado)} · "
              f"{len(series_t)} llaves terminadas")
        partidos += ps
        series += series_t

    url_bd = os.getenv("DATABASE_URL")
    if url_bd:
        n = cargar_postgres(url_bd, crudos, partidos, series, equipos)
        print(f"Cargado en PostgreSQL: {n} partidos insertados/actualizados, {len(series)} llaves")
    else:
        guardar_local(partidos, series)
        print(f"Sin DATABASE_URL: CSV en {RAIZ / 'data' / 'silver'}")


if __name__ == "__main__":
    main()
