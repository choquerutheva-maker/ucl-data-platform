"""
Coeficiente de club UEFA (ranking de 5 temporadas) desde la API pública de UEFA
(comp.uefa.com, sin clave), para usarlo como punto de partida del modelo.

id_temporada = S usa seasonYear = S de la API: el ranking que se cierra al
terminar la temporada S-1/S y sirve para el sorteo de S/S+1. Así el modelo de
la temporada S nunca ve puntos ganados en S.

Coeficiente = totalValue de la API. Hasta 2017 era puntos del club + 20 % del
coeficiente de su asociación; desde 2018 es el mayor de ambos. Un club fuera
del ranking (sin puntos en 5 años) recibe el piso de su asociación
(nationalAssociationPoints, igual para todos los clubes del país).

Pasos:
  1. Guarda cada ranking crudo en bronze.ingesta.
  2. Resuelve los clubes con seeds/equipos_uefa.csv (por id de UEFA).
  3. Upsert en gold.d_equipo_alias (fuente 'uefa') y gold.f_coeficiente_uefa.
  4. Actualiza gold.d_equipo.codigo y nombre_corto (teamCode y displayNameShort
     del ranking más reciente en que aparece el club), para el dashboard.

Uso:
    py src/ingesta/uefa_coeficientes.py                         # 2015 .. 2026
    py src/ingesta/uefa_coeficientes.py --temporadas 2026

Sin DATABASE_URL deja data/silver/uefa_coeficientes.csv.

Autor : Ruth Choque (con PROMETHEUS) · 2026-10-26
"""

import argparse
import csv
import json
import os
import time
from pathlib import Path

from espn_cuotas import PAUSA_S, http_json

RAIZ = Path(__file__).resolve().parents[2]
URL = ("https://comp.uefa.com/v2/coefficients?coefficientRange=OVERALL&coefficientType=MEN_CLUB"
       "&language=EN&page=1&pagesize=500&seasonYear={anio}")
CAMPOS = ["id_temporada", "nombre_canonico", "pais", "uefa_id", "coeficiente", "puntos_club",
          "piso_asociacion", "es_piso_asociacion", "posicion"]


def leer_seed_equipos():
    with (RAIZ / "seeds" / "equipos_uefa.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def coeficientes(id_temporada, ranking, equipos):
    """Una fila por club del seed: su coeficiente o el piso de su asociación."""
    if len(ranking["data"]["members"]) >= 500:          # la API no acepta pagesize > 500
        raise ValueError(f"{id_temporada}: el ranking llenó la página; falta paginar")
    por_id, piso = {}, {}
    for m in ranking["data"]["members"]:
        o = m["overallRanking"]
        por_id[m["member"]["id"]] = o
        piso.setdefault(m["member"]["countryCode"], o["nationalAssociationPoints"])
    minimo = min(o["totalValue"] for o in por_id.values())

    filas = []
    for e in equipos:
        o = por_id.get(e["uefa_id"])
        piso_pais = piso.get(e["pais"])
        filas.append({
            "id_temporada": id_temporada, "nombre_canonico": e["nombre_canonico"], "pais": e["pais"],
            "uefa_id": e["uefa_id"],
            "coeficiente": o["totalValue"] if o else (piso_pais if piso_pais is not None else minimo),
            "puntos_club": o["totalPoints"] if o else None,
            "piso_asociacion": o["nationalAssociationPoints"] if o else piso_pais,
            "es_piso_asociacion": o is None,
            "posicion": o["position"] if o else None,
        })
    return filas


def identidades(crudos):
    """{uefa_id: (codigo, nombre_corto)} del ranking más reciente en que aparece cada club."""
    ident = {}
    for anio in sorted(crudos):
        for m in crudos[anio][1]["data"]["members"]:
            ident[m["member"]["id"]] = (m["member"].get("teamCode"), m["member"].get("displayNameShort"))
    return ident


def guardar_local(filas):
    destino = RAIZ / "data" / "silver"
    destino.mkdir(parents=True, exist_ok=True)
    with (destino / "uefa_coeficientes.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        w.writeheader()
        w.writerows(filas)


def cargar_postgres(url, crudos, filas, equipos):
    ident = identidades(crudos)
    import psycopg
    with psycopg.connect(url) as cx:
        for anio, (status, payload) in crudos.items():
            texto = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            cx.execute(
                """INSERT INTO bronze.ingesta (id_fuente, endpoint, parametros, http_status, payload_json, payload_hash, procesado)
                   VALUES ('uefa', 'coefficients', %s, %s, %s, md5(%s), TRUE)
                   ON CONFLICT (id_fuente, endpoint, payload_hash) DO NOTHING""",
                (json.dumps({"coefficientType": "MEN_CLUB", "seasonYear": anio}), status, texto, texto))

        id_equipo = dict(cx.execute("SELECT nombre_canonico, id_equipo FROM gold.d_equipo").fetchall())
        for e in equipos:
            if e["nombre_canonico"] in id_equipo:
                cx.execute(
                    """INSERT INTO gold.d_equipo_alias (id_fuente, alias, id_externo, id_equipo, validado)
                       VALUES ('uefa', %s, %s, %s, TRUE)
                       ON CONFLICT (id_fuente, alias) DO UPDATE SET
                           id_externo = EXCLUDED.id_externo, id_equipo = EXCLUDED.id_equipo, validado = TRUE""",
                    (e["uefa_nombre"], e["uefa_id"], id_equipo[e["nombre_canonico"]]))
                if e["uefa_id"] in ident:
                    cx.execute("UPDATE gold.d_equipo SET codigo = %s, nombre_corto = %s WHERE id_equipo = %s",
                               (*ident[e["uefa_id"]], id_equipo[e["nombre_canonico"]]))

        n = 0
        with cx.cursor() as cur:
            for f in filas:
                if f["nombre_canonico"] not in id_equipo:
                    continue
                cur.execute(
                    """INSERT INTO gold.f_coeficiente_uefa (id_equipo, id_temporada, coeficiente, puntos_club,
                                                           piso_asociacion, es_piso_asociacion, posicion)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (id_equipo, id_temporada) DO UPDATE SET
                           coeficiente = EXCLUDED.coeficiente, puntos_club = EXCLUDED.puntos_club,
                           piso_asociacion = EXCLUDED.piso_asociacion,
                           es_piso_asociacion = EXCLUDED.es_piso_asociacion,
                           posicion = EXCLUDED.posicion, actualizado_en = now()""",
                    (id_equipo[f["nombre_canonico"]], f["id_temporada"], f["coeficiente"], f["puntos_club"],
                     f["piso_asociacion"], f["es_piso_asociacion"], f["posicion"]))
                n += cur.rowcount
        return n


# -----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Coeficiente de club UEFA")
    ap.add_argument("--temporadas", default=",".join(str(a) for a in range(2015, 2027)),
                    help="Años de inicio separados por coma (2026 = ranking para el sorteo 2026-27)")
    args = ap.parse_args()

    equipos = leer_seed_equipos()
    crudos, filas = {}, []
    for id_temporada in (int(t) for t in args.temporadas.split(",")):
        status, ranking = http_json(URL.format(anio=id_temporada))
        crudos[id_temporada] = (status, ranking)
        filas_t = coeficientes(id_temporada, ranking, equipos)
        con_piso = sum(f["es_piso_asociacion"] for f in filas_t)
        print(f"{id_temporada}: {len(ranking['data']['members'])} clubes en el ranking · "
              f"{len(filas_t)} del seed ({con_piso} con piso de asociación)")
        filas += filas_t
        time.sleep(PAUSA_S)

    # Ranking en curso: SOLO para código y nombre corto de los debutantes (p. ej.
    # Como 1907 no aparece hasta 2027). Sus puntos no se usan como coeficiente.
    siguiente = max(crudos) + 1
    try:
        crudos[siguiente] = http_json(URL.format(anio=siguiente))
    except Exception as e:
        print(f"{siguiente}: ranking en curso no disponible ({e}); se omite")

    url_bd = os.getenv("DATABASE_URL")
    if url_bd:
        n = cargar_postgres(url_bd, crudos, filas, equipos)
        print(f"Cargado en PostgreSQL: {n} coeficientes insertados/actualizados")
    else:
        guardar_local(filas)
        print(f"Sin DATABASE_URL: CSV en {RAIZ / 'data' / 'silver'}")


if __name__ == "__main__":
    main()
