"""
Publica pronósticos 1X2 en gold.f_pronostico (Dixon-Coles v1 y v2, Elo UCL v1).

Dixon-Coles v2 parte del coeficiente UEFA del club (gold.f_coeficiente_uefa,
src/ingesta/uefa_coeficientes.py); sin coeficientes cargados equivale a la v1.

  - Partidos JUGADOS desde --desde-temporada: pronóstico de BACKTEST
    (walk-forward: entrenado solo con partidos anteriores a su fecha).
  - Partidos PROGRAMADOS (solo con --en-vivo): pronóstico EN VIVO de los
    modelos de MODELOS_EN_VIVO, entrenado con todo lo jugado hasta hoy. Se
    recalcula en cada corrida mientras el partido no empiece (nunca se emite
    para un partido con inicio_utc ya pasado, aunque siga PROGRAMADO); una vez jugado, el pronóstico en vivo se conserva tal cual (el backtest
    nunca lo reemplaza).

d_modelo guarda los hiperparámetros y las métricas del período de prueba
(temporadas terminadas del backtest).

Uso:
    py src/modelo/publicar_pronosticos.py
    py src/modelo/publicar_pronosticos.py --desde-temporada 2023

Con DATABASE_URL lee gold.f_partido y escribe gold.d_modelo / gold.f_pronostico;
sin ella usa data/silver/*_partidos.csv y deja data/gold/f_pronostico.csv.

Autor : Ruth Choque (con PROMETHEUS) · 2026-10-22
"""

import argparse
import csv
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from backtest import EloUCL, leer_partidos, metricas, walk_forward
from backtest_ligas import PAIS_LIGA, PRIMERA_TEMPORADA_LIGA, leer_ligas, walk_forward_ucl
from coeficientes import anotar, anotar_conjunto, leer_coeficientes, leer_pisos
from dixon_coles import DixonColes
from elo_ligas import PARAMETROS as PARAMETROS_ELO_PROPIO
from elo_ligas import funcion_x_inicial, recorrer

RAIZ = Path(__file__).resolve().parents[2]

# Elegidos con la temporada de validación 2022-23 (src/modelo/backtest.py y backtest_ligas.py)
VIDA_MEDIA_DIAS, REG, K_ELO = 365, 1.0, 30
VIDA_MEDIA_DIAS_COEF, REG_COEF = 365, 2.0
VIDA_MEDIA_DIAS_LIGAS, REG_LIGAS = 365, 2.0

MODELOS = {
    "DIXON_COLES": ("Dixon-Coles", "v1", "DIXON_COLES",
                    "Poisson bivariado con corrección τ (ρ), ataque/defensa por club con L2 y "
                    "ponderación temporal; solo partidos UCL a los 90'."),
    "DIXON_COLES_COEF": ("Dixon-Coles", "v2", "DIXON_COLES",
                         "Dixon-Coles v1 + coeficiente de club UEFA: ataque/defensa = β·log(coeficiente) "
                         "+ desvío del club con L2; un debutante parte de lo que predice su coeficiente."),
    "ELO_UCL": ("Elo UCL", "v1", "ELO",
                "Elo solo con partidos UCL (margen de goles, regresión entre temporadas) + "
                "logit ordenado sobre la diferencia de Elo."),
    "DIXON_COLES_LIGAS": ("Dixon-Coles", "v3", "DIXON_COLES",
                          "Dixon-Coles v2 entrenado también con las 5 grandes ligas (desde 2015-16): la misma "
                          "fuerza por club en la UCL y en su liga, con nivel de goles y ventaja de local por "
                          "competición; un club sin coeficiente parte del piso de su asociación."),
    "ELO_LIGAS": ("Elo propio", "v1", "ELO",
                  "Elo propio (calculado por el proyecto, no es ClubElo): 5 grandes ligas desde 2012-13 + UCL; "
                  "K y ventaja de local por tipo de competición; un club nuevo arranca según su coeficiente "
                  "UEFA o el piso de su asociación; logit ordenado por temporada para UCL y ligas."),
}
# En vivo desde la J4 2026-27: Dixon-Coles v2 y v3 y Elo propio lado a lado con el
# mercado (gold.vw_pronostico_vs_mercado). Dixon-Coles v1 y Elo UCL v1: solo backtest.
MODELOS_EN_VIVO = {"DIXON_COLES_COEF", "DIXON_COLES_LIGAS", "ELO_LIGAS"}
COLUMNAS = ["prob_local", "prob_empate", "prob_visita", "goles_esp_local", "goles_esp_visita", "prob_over_2_5"]


# -----------------------------------------------------------------------------
def leer_postgres(cx):
    filas = cx.execute("""
        SELECT p.id_partido, p.id_temporada, p.id_ronda, p.fecha, l.nombre_canonico, v.nombre_canonico,
               p.es_neutral, p.estado, p.goles_local_90, p.goles_visita_90, p.inicio_utc
        FROM gold.f_partido p
        JOIN gold.d_equipo l ON l.id_equipo = p.id_equipo_local
        JOIN gold.d_equipo v ON v.id_equipo = p.id_equipo_visita""").fetchall()
    partidos = [{
        "id_partido": r[0], "id_temporada": r[1], "id_ronda": r[2],
        "fecha": r[3] if isinstance(r[3], date) else date.fromisoformat(str(r[3])),
        "local": r[4], "visita": r[5], "es_neutral": r[6], "jugado": r[7] == "JUGADO",
        "goles_local_90": r[8], "goles_visita_90": r[9], "inicio_utc": r[10],
    } for r in filas if r[7] in ("JUGADO", "PROGRAMADO")]
    partidos.sort(key=lambda p: (p["fecha"], p["local"]))
    anotar(partidos, leer_coeficientes(cx))
    return partidos


def leer_ligas_postgres(cx):
    """Partidos de liga jugados (v3) y el país de cada club de liga."""
    filas = cx.execute("""
        SELECT pl.id_liga, pl.temporada, pl.fecha, l.nombre_canonico, v.nombre_canonico,
               pl.goles_local_90, pl.goles_visita_90, d.pais_iso3
        FROM gold.f_partido_liga pl
        JOIN gold.d_liga d ON d.id_liga = pl.id_liga
        JOIN gold.d_equipo l ON l.id_equipo = pl.id_equipo_local
        JOIN gold.d_equipo v ON v.id_equipo = pl.id_equipo_visita
        WHERE pl.estado = 'JUGADO'""").fetchall()
    ligas, pais_de = [], {}
    for comp, t, fecha, local, visita, gl, gv, pais in filas:
        pais_de.setdefault(local, pais)
        pais_de.setdefault(visita, pais)
        # Todas las temporadas: el Elo propio usa desde 2012, la v3 filtra desde 2015 (conjunto_v3)
        ligas.append({"comp": comp, "id_temporada": int(t), "id_ronda": None,
                      "fecha": fecha if isinstance(fecha, date) else date.fromisoformat(str(fecha)),
                      "local": local, "visita": visita, "es_neutral": False, "jugado": True,
                      "goles_local_90": gl, "goles_visita_90": gv})
    return ligas, pais_de


def conjunto_v3(partidos, ligas, pais_de, coefs, pisos):
    """Partidos UCL (jugados y programados) + ligas con el x de la v3. Copias: no toca el x de la v2."""
    ucl = [{**p, "comp": "UCL"} for p in partidos]
    ligas = [dict(p) for p in ligas if p["id_temporada"] >= PRIMERA_TEMPORADA_LIGA]
    todos = sorted(ucl + ligas, key=lambda p: (p["fecha"], p["comp"], p["local"]))
    anotar_conjunto(todos, coefs, pisos, pais_de)
    return todos


def elo_propio(partidos, ligas, pais_de, coefs, pisos, temporadas):
    """Recorre UCL + ligas (desde 2012) en orden. Devuelve (filas de backtest UCL, Elo al día)."""
    todos = sorted([{**p, "comp": "UCL"} for p in partidos if p["jugado"]] + [dict(p) for p in ligas],
                   key=lambda p: (p["fecha"], p["comp"], p["local"]))
    x_inicial = funcion_x_inicial(todos, pais_de, coefs, pisos)
    filas, elo = recorrer(todos, {("UCL", t) for t in temporadas}, PARAMETROS_ELO_PROPIO, x_inicial)
    elo.datos_hasta = max(p["fecha"] for p in todos)
    return filas, elo


def pronosticos(partidos, desde_temporada, en_vivo, todos_v3=None, datos_elo=None):
    jugados = [p for p in partidos if p["jugado"]]
    temporadas = sorted({p["id_temporada"] for p in jugados if p["id_temporada"] >= desde_temporada})

    # 1) Backtest walk-forward
    filas_bt, _ = walk_forward(partidos, set(temporadas), VIDA_MEDIA_DIAS, REG, K_ELO,
                               VIDA_MEDIA_DIAS_COEF, REG_COEF)
    if todos_v3:
        jugados_v3 = [p for p in todos_v3 if p["jugado"]]
        filas_v3, _ = walk_forward_ucl(jugados_v3, set(temporadas), VIDA_MEDIA_DIAS_LIGAS, REG_LIGAS,
                                       con_v2=False)
        filas_bt += filas_v3
    elo2 = None
    if datos_elo:
        filas_elo, elo2 = elo_propio(partidos, *datos_elo, temporadas)
        filas_bt += filas_elo
    salida = []
    for f in filas_bt:
        if f["modelo"] not in MODELOS:
            continue
        hasta = max(p["fecha"] for p in jugados if p["fecha"] < date.fromisoformat(f["fecha"]))
        salida.append({**f, "es_backtest": True, "datos_hasta": hasta.isoformat()})

    # 2) En vivo: programados, entrenando con todo lo jugado
    programados = [p for p in partidos if not p["jugado"]]
    ahora = datetime.now(timezone.utc)
    por_empezar = [p for p in programados
                   if (p["inicio_utc"] > ahora if p.get("inicio_utc") else p["fecha"] >= ahora.date())]
    if por_empezar and en_vivo:
        hoy = max(p["fecha"] for p in jugados)
        dc2 = DixonColes(vida_media_dias=VIDA_MEDIA_DIAS_COEF, reg=REG_COEF).ajustar(jugados, hoy)
        elo = EloUCL(k=K_ELO)
        for p in jugados:
            elo.actualizar(p)
        elo.ajustar_logit()
        predictores = {
            "DIXON_COLES_COEF": lambda p: dc2.predecir(p["local"], p["visita"], p["es_neutral"],
                                                       p["x_local"], p["x_visita"]),
            "ELO_UCL": elo.predecir,
        }
        hasta = {m: hoy for m in MODELOS_EN_VIVO}
        if todos_v3:
            # La v3 entrena hasta el último partido jugado, sea de UCL o de liga (fin de semana incluido)
            jugados_v3 = [p for p in todos_v3 if p["jugado"]]
            hoy_v3 = max(p["fecha"] for p in jugados_v3)
            dc3 = DixonColes(vida_media_dias=VIDA_MEDIA_DIAS_LIGAS, reg=REG_LIGAS,
                             ventana_dias=4 * VIDA_MEDIA_DIAS_LIGAS).ajustar(jugados_v3, hoy_v3)
            x_v3 = {(p["fecha"], p["local"], p["visita"]): (p["x_local"], p["x_visita"])
                    for p in todos_v3 if p["comp"] == "UCL"}
            predictores["DIXON_COLES_LIGAS"] = lambda p: dc3.predecir(
                p["local"], p["visita"], p["es_neutral"], *x_v3[(p["fecha"], p["local"], p["visita"])])
            hasta["DIXON_COLES_LIGAS"] = hoy_v3
        if elo2:
            predictores["ELO_LIGAS"] = lambda p: elo2.predecir({**p, "comp": "UCL"})
            hasta["ELO_LIGAS"] = elo2.datos_hasta
        for p in por_empezar:
            base = {"id_temporada": p["id_temporada"], "fecha": p["fecha"].isoformat(),
                    "local": p["local"], "visita": p["visita"], "es_backtest": False}
            for m in sorted(MODELOS_EN_VIVO & set(predictores)):
                salida.append({**base, "modelo": m, "datos_hasta": hasta[m].isoformat(), **predictores[m](p)})

    # Métricas del período de prueba (temporadas del backtest ya terminadas)
    en_curso = {p["id_temporada"] for p in programados}
    prueba = [t for t in temporadas if t not in en_curso]
    met = {m: {"temporadas_prueba": prueba,
               "prueba": metricas([f for f in filas_bt if f["modelo"] == m and f["id_temporada"] in prueba])}
           for m in MODELOS if any(f["modelo"] == m for f in filas_bt)}
    return salida, met, elo2


# -----------------------------------------------------------------------------
def escribir_postgres(cx, salida, met, partidos):
    id_partido = {(p["fecha"].isoformat(), p["local"], p["visita"]): p["id_partido"] for p in partidos}
    id_modelo = {}
    for clave, (nombre, version, algoritmo, descripcion) in MODELOS.items():
        if clave not in met:
            continue
        id_modelo[clave] = cx.execute(
            """INSERT INTO gold.d_modelo (nombre, version, algoritmo, temporadas_train, entrenado_en,
                                          metricas_validacion, descripcion)
               VALUES (%s, %s, %s, %s, now(), %s, %s)
               ON CONFLICT (nombre, version) DO UPDATE SET
                   entrenado_en = now(), metricas_validacion = EXCLUDED.metricas_validacion,
                   temporadas_train = EXCLUDED.temporadas_train, descripcion = EXCLUDED.descripcion
               RETURNING id_modelo""",
            (nombre, version, algoritmo, "2015-16 en adelante (walk-forward)",
             json.dumps({**met[clave], "hiperparametros": {"vida_media_dias": VIDA_MEDIA_DIAS, "reg": REG,
                                                            "k_elo": K_ELO,
                                                            "vida_media_dias_coef": VIDA_MEDIA_DIAS_COEF,
                                                            "reg_coef": REG_COEF,
                                                            "vida_media_dias_ligas": VIDA_MEDIA_DIAS_LIGAS,
                                                            "reg_ligas": REG_LIGAS,
                                                            "elo_propio": PARAMETROS_ELO_PROPIO}}),
             descripcion)).fetchone()[0]

    # Modelos que dejaron de publicarse en vivo: se borran sus pronósticos en vivo de
    # partidos que todavía no empezaron (los ya jugados se conservan tal cual)
    en_vivo = [MODELOS[m][:2] for m in sorted(MODELOS_EN_VIVO)]
    cx.execute("""
        DELETE FROM gold.f_pronostico f
        USING gold.d_modelo m, gold.f_partido p
        WHERE m.id_modelo = f.id_modelo AND p.id_partido = f.id_partido
          AND NOT f.es_backtest AND p.estado = 'PROGRAMADO'
          AND (p.inicio_utc IS NULL OR p.inicio_utc > now())
          AND NOT ((m.nombre, m.version) IN (SELECT * FROM unnest(%s::text[], %s::text[])))""",
               ([n for n, _ in en_vivo], [v for _, v in en_vivo]))

    sql = f"""
        INSERT INTO gold.f_pronostico (id_partido, id_modelo, generado_en, {", ".join(COLUMNAS)}, es_backtest, datos_hasta)
        VALUES (%s, %s, now(), {", ".join(["%s"] * len(COLUMNAS))}, %s, %s)
        ON CONFLICT (id_partido, id_modelo) DO UPDATE SET
            generado_en = now(), {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNAS)},
            es_backtest = EXCLUDED.es_backtest, datos_hasta = EXCLUDED.datos_hasta
        WHERE gold.f_pronostico.es_backtest              -- un backtest se puede recalcular
           OR NOT EXCLUDED.es_backtest                    -- un en vivo se actualiza hasta el inicio
    """
    with cx.cursor() as cur:
        cur.executemany(sql, [
            (id_partido[(f["fecha"], f["local"], f["visita"])], id_modelo[f["modelo"]],
             *[round(f[c], 4) if f.get(c) is not None else None for c in COLUMNAS],
             f["es_backtest"], f["datos_hasta"])
            for f in salida])


def escribir_elo(cx, elo):
    """Reemplaza el historial del Elo propio en gold.f_elo_historico (fuente = 'propio')."""
    id_equipo = dict(cx.execute("SELECT nombre_canonico, id_equipo FROM gold.d_equipo").fetchall())
    tramos = [(id_equipo[c], d, h, r) for c, d, h, r in elo.tramos() if c in id_equipo]
    cx.execute("DELETE FROM gold.f_elo_historico WHERE fuente = 'propio'")
    with cx.cursor() as cur:
        cur.executemany("""INSERT INTO gold.f_elo_historico (fuente, id_equipo, fecha_desde, fecha_hasta, elo)
                           VALUES ('propio', %s, %s, %s, %s)""", tramos)
    return len(tramos)


def main():
    ap = argparse.ArgumentParser(description="Publica pronósticos en gold.f_pronostico")
    ap.add_argument("--desde-temporada", type=int, default=2023)
    ap.add_argument("--en-vivo", action="store_true", help="emitir también pronósticos de partidos programados")
    args = ap.parse_args()

    url_bd = os.getenv("DATABASE_URL")
    if url_bd:
        import psycopg
        cx = psycopg.connect(url_bd)
        partidos = leer_postgres(cx)
        ligas, pais_de = leer_ligas_postgres(cx)
        coefs, pisos = leer_coeficientes(cx), leer_pisos(cx)
    else:
        partidos = leer_partidos()
        hay_ligas = (RAIZ / "data" / "silver" / "football_data_partidos.csv").exists()
        ligas, pais_de = leer_ligas(desde=2012) if hay_ligas else ([], {})
        coefs, pisos = leer_coeficientes(), leer_pisos()

    # Sin partidos de liga cargados no se publican la v3 ni el Elo propio
    todos_v3 = conjunto_v3(partidos, ligas, pais_de, coefs, pisos) if ligas else None
    datos_elo = (ligas, pais_de, coefs, pisos) if ligas else None
    salida, met, elo = pronosticos(partidos, args.desde_temporada, args.en_vivo, todos_v3, datos_elo)
    n_bt = sum(f["es_backtest"] for f in salida)
    print(f"Pronósticos: {n_bt} de backtest, {len(salida) - n_bt} en vivo")
    for m, d in met.items():
        print(f"   {m:<12} prueba {d['temporadas_prueba']}: {d['prueba']}")

    if url_bd:
        try:
            escribir_postgres(cx, salida, met, partidos)
            n_elo = escribir_elo(cx, elo) if elo else 0
            cx.commit()
        finally:
            cx.close()
        print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC] cargado en gold.f_pronostico "
              f"y {n_elo} tramos del Elo propio en gold.f_elo_historico")
    else:
        destino = RAIZ / "data" / "gold"
        destino.mkdir(parents=True, exist_ok=True)
        campos = ["modelo", "es_backtest", "datos_hasta", "id_temporada", "fecha", "local", "visita", *COLUMNAS]
        with (destino / "f_pronostico.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
            w.writeheader()
            w.writerows(salida)
        if elo:
            with (destino / "f_elo_historico.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["nombre_canonico", "fecha_desde", "fecha_hasta", "elo"])
                w.writerows(elo.tramos())
        print(f"Sin DATABASE_URL: CSV en {destino}")


if __name__ == "__main__":
    main()
