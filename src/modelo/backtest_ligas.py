"""
Backtest walk-forward de Dixon-Coles v3 (UCL + 5 grandes ligas) contra la v2.

  - v2 (DIXON_COLES_COEF): solo partidos UCL, hiperparámetros de producción.
  - v3 (DIXON_COLES_LIGAS): la misma fuerza por club en la UCL y en su liga;
    cada liga con su nivel de goles y su ventaja de local. Los clubes que nunca
    jugaron la UCL parten del piso de coeficiente de su asociación.

Dos evaluaciones, ambas sin leakage (cada pronóstico usa solo partidos
anteriores a su fecha):
  1. UCL: mismo conjunto de partidos para v2 y v3 (temporadas de prueba), con
     IC 95 % por bootstrap pareado y comparación contra el cierre de ESPN BET.
  2. Ligas: partidos de liga de las temporadas de prueba contra la cuota de
     cierre sin margen (Pinnacle y promedio del mercado). La v3 se reajusta como
     máximo una vez por semana para las ligas (pronóstico con hasta 6 días de
     antigüedad: sin leakage, solo menos fresco).

Los hiperparámetros de la v3 (vida media, regularización) se eligen con la
temporada de VALIDACIÓN de la UCL; las de PRUEBA no se tocan hasta el final.

Uso:
    py src/modelo/backtest_ligas.py                    # validación 2022, prueba 2023-2025
    py src/modelo/backtest_ligas.py --rapido           # grilla chica
    py src/modelo/backtest_ligas.py --vida 365 --reg 2 # sin grilla

Lee data/silver/{openfootball,espn,football_data}_partidos.csv,
uefa_coeficientes.csv, espn_cuotas.csv y football_data_cuotas.csv; deja
data/gold/backtest_ligas_pronosticos.csv y data/gold/backtest_ligas_metricas.json.

Autor : Ruth Choque (con PROMETHEUS) · 2026-10-31
"""

import argparse
import csv
import json
import time
from datetime import date
from pathlib import Path

import numpy as np

from backtest import leer_partidos, metricas, resultado
from benchmark_mercado import sin_margen_proporcional, sin_margen_shin
from coeficientes import anotar_conjunto, leer_coeficientes, leer_pisos
from dixon_coles import DixonColes

RAIZ = Path(__file__).resolve().parents[2]
PAIS_LIGA = {"ENG1": "ENG", "ESP1": "ESP", "GER1": "GER", "ITA1": "ITA", "FRA1": "FRA"}
VIDA_V2, REG_V2 = 365, 2.0                  # producción (src/modelo/publicar_pronosticos.py)
PRIMERA_TEMPORADA_LIGA = 2015               # primera con coeficientes UEFA cargados
CASA_ESPN_BET = "58"
DIAS_REAJUSTE_LIGA = 7


# -----------------------------------------------------------------------------
# Datos
# -----------------------------------------------------------------------------
def leer_ligas():
    partidos, pais_de = [], {}
    with (RAIZ / "data" / "silver" / "football_data_partidos.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pais_de.setdefault(r["local"], PAIS_LIGA[r["id_liga"]])
            pais_de.setdefault(r["visita"], PAIS_LIGA[r["id_liga"]])
            if r["estado"] != "JUGADO" or int(r["temporada"]) < PRIMERA_TEMPORADA_LIGA:
                continue
            partidos.append({
                "comp": r["id_liga"], "id_temporada": int(r["temporada"]), "id_ronda": None,
                "fecha": date.fromisoformat(r["fecha"]), "local": r["local"], "visita": r["visita"],
                "es_neutral": False, "jugado": True,
                "goles_local_90": int(r["goles_local_90"]), "goles_visita_90": int(r["goles_visita_90"]),
            })
    return partidos, pais_de


def leer_todo():
    ucl = [p for p in leer_partidos() if p["jugado"]]
    for p in ucl:
        p["comp"] = "UCL"
    ligas, pais_de = leer_ligas()
    todos = sorted(ucl + ligas, key=lambda p: (p["fecha"], p["comp"], p["local"]))
    anotar_conjunto(todos, leer_coeficientes(), leer_pisos(), pais_de)
    return todos


def leer_csv(nombre):
    ruta = RAIZ / "data" / "silver" / nombre
    if not ruta.exists():
        return []
    with ruta.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def cierre_espn():
    evento = {r["espn_event_id"]: (r["fecha"], r["local"], r["visita"]) for r in leer_csv("espn_partidos.csv")}
    return {evento[r["espn_event_id"]]: [float(r["cuota_local"]), float(r["cuota_empate"]), float(r["cuota_visita"])]
            for r in leer_csv("espn_cuotas.csv")
            if r["tipo_linea"] == "CIERRE" and r["casa_id"] == CASA_ESPN_BET and r["espn_event_id"] in evento}


def cierre_ligas(casa):
    return {(r["id_liga"], int(r["temporada"]), r["local"], r["visita"]):
            [float(r["cuota_local"]), float(r["cuota_empate"]), float(r["cuota_visita"])]
            for r in leer_csv("football_data_cuotas.csv") if r["casa"] == casa and r["momento"] == "CIERRE"}


# -----------------------------------------------------------------------------
# Walk-forward
# -----------------------------------------------------------------------------
def fila(p, modelo, pred):
    return {"comp": p["comp"], "id_temporada": p["id_temporada"], "fecha": p["fecha"].isoformat(),
            "local": p["local"], "visita": p["visita"], "goles_local_90": p["goles_local_90"],
            "goles_visita_90": p["goles_visita_90"], "resultado": resultado(p), "modelo": modelo,
            **{k: pred[k] for k in ("prob_local", "prob_empate", "prob_visita")}}


def walk_forward_ucl(todos, temporadas, vida, reg, con_v2=True):
    ucl = [p for p in todos if p["comp"] == "UCL"]
    objetivo = [p for p in ucl if p["id_temporada"] in temporadas]
    v2 = DixonColes(vida_media_dias=VIDA_V2, reg=REG_V2)
    v3 = DixonColes(vida_media_dias=vida, reg=reg, ventana_dias=4 * vida)
    filas = []
    for fecha in sorted({p["fecha"] for p in objetivo}):
        del_dia = [p for p in objetivo if p["fecha"] == fecha]
        v3.ajustar([p for p in todos if p["fecha"] < fecha], fecha)
        if con_v2:
            v2.ajustar([p for p in ucl if p["fecha"] < fecha], fecha)
        for p in del_dia:
            args = (p["local"], p["visita"], p["es_neutral"], p["x_local"], p["x_visita"])
            filas.append(fila(p, "DIXON_COLES_LIGAS", v3.predecir(*args)))
            if con_v2:
                filas.append(fila(p, "DIXON_COLES_COEF", v2.predecir(*args)))
    return filas, v3


def walk_forward_ligas(todos, temporadas, vida, reg):
    objetivo = [p for p in todos if p["comp"] != "UCL" and p["id_temporada"] in temporadas]
    v3 = DixonColes(vida_media_dias=vida, reg=reg, ventana_dias=4 * vida)
    filas, ultimo = [], None
    for fecha in sorted({p["fecha"] for p in objetivo}):
        if ultimo is None or (fecha - ultimo).days >= DIAS_REAJUSTE_LIGA:
            v3.ajustar([p for p in todos if p["fecha"] < fecha], fecha)
            ultimo = fecha
        for p in objetivo:
            if p["fecha"] == fecha:
                filas.append(fila(p, "DIXON_COLES_LIGAS", v3.predecir(
                    p["local"], p["visita"], False, p["x_local"], p["x_visita"], comp=p["comp"])))
    return filas


# -----------------------------------------------------------------------------
# Comparaciones
# -----------------------------------------------------------------------------
def ll_por_partido(filas, modelo):
    d = {}
    for f in filas:
        if f["modelo"] == modelo:
            p = {"L": f["prob_local"], "E": f["prob_empate"], "V": f["prob_visita"]}[f["resultado"]]
            d[(f["comp"], f["fecha"], f["local"], f["visita"])] = -np.log(max(p, 1e-12))
    return d


def dif_bootstrap(filas, modelo, base, semilla=42):
    a, b = ll_por_partido(filas, modelo), ll_por_partido(filas, base)
    claves = sorted(set(a) & set(b))
    d = np.array([a[k] - b[k] for k in claves])
    rng = np.random.default_rng(semilla)
    bs = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(5000)]
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return {"n": len(d), "media": round(float(d.mean()), 4), "ic95": [round(float(lo), 4), round(float(hi), 4)]}


def agregar_mercado(filas, cuotas, clave_de, sufijo):
    """Agrega filas MERCADO_PROP_<sufijo> / MERCADO_SHIN_<sufijo> para los partidos con cuota."""
    nuevas, vistos = [], set()
    for f in filas:
        k = clave_de(f)
        if k in cuotas and (f["comp"], f["fecha"], f["local"], f["visita"]) not in vistos:
            vistos.add((f["comp"], f["fecha"], f["local"], f["visita"]))
            for nombre, fn in (("MERCADO_PROP", sin_margen_proporcional), ("MERCADO_SHIN", sin_margen_shin)):
                p = fn(cuotas[k])
                nuevas.append({**f, "modelo": f"{nombre}_{sufijo}",
                               "prob_local": p[0], "prob_empate": p[1], "prob_visita": p[2]})
    return nuevas


def tabla(filas, modelos, titulo, claves=None):
    print(f"\n{titulo}")
    print(f"{'modelo':<26} {'n':>5} {'log_loss':>9} {'brier':>7} {'rps':>7} {'acierto':>8}")
    salida = {}
    for m in modelos:
        fs = [f for f in filas if f["modelo"] == m
              and (claves is None or (f["comp"], f["fecha"], f["local"], f["visita"]) in claves)]
        r = metricas(fs)
        if r:
            salida[m] = r
            print(f"{m:<26} {r['n']:>5} {r['log_loss']:>9} {r['brier']:>7} {r['rps']:>7} {r['acierto']:>8}")
    return salida


# -----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Backtest Dixon-Coles v3 (UCL + ligas)")
    ap.add_argument("--validacion", default="2022")
    ap.add_argument("--prueba", default="2023,2024,2025")
    ap.add_argument("--rapido", action="store_true")
    ap.add_argument("--vida", type=float, help="fija la vida media (sin grilla)")
    ap.add_argument("--reg", type=float, help="fija la regularización (sin grilla)")
    args = ap.parse_args()
    validacion = {int(t) for t in args.validacion.split(",")}
    prueba = {int(t) for t in args.prueba.split(",")}

    todos = leer_todo()
    n_ucl = sum(p["comp"] == "UCL" for p in todos)
    print(f"Partidos: {n_ucl} UCL + {len(todos) - n_ucl} de liga (desde {PRIMERA_TEMPORADA_LIGA})")

    # 1) Grilla con la temporada de validación (log loss UCL)
    if args.vida and args.reg:
        vida, reg, grilla = args.vida, args.reg, []
    else:
        vidas = [365, 730] if args.rapido else [180, 365, 730]
        regs = [2.0, 8.0] if args.rapido else [1.0, 2.0, 4.0, 8.0, 16.0]
        grilla = []
        for vm in vidas:
            for rg in regs:
                t0 = time.time()
                filas, _ = walk_forward_ucl(todos, validacion, vm, rg, con_v2=False)
                ll = metricas(filas)["log_loss"]
                grilla.append((ll, vm, rg))
                print(f"   validación vida {vm:>4} reg {rg:>5}: log loss {ll} ({time.time() - t0:.0f} s)")
        _, vida, reg = min(grilla)
    print(f"Elegidos: vida media {vida} d, reg {reg}")

    # 2) Prueba UCL: v2 vs v3 en los mismos partidos, y contra ESPN BET
    filas_ucl, v3 = walk_forward_ucl(todos, prueba, vida, reg)
    salida = {"validacion": sorted(validacion), "prueba": sorted(prueba),
              "hiperparametros": {"vida_media_dias": vida, "reg": reg, "ventana_dias": 4 * vida,
                                  "primera_temporada_liga": PRIMERA_TEMPORADA_LIGA},
              "grilla": [{"log_loss": g[0], "vida": g[1], "reg": g[2]} for g in sorted(grilla)],
              "niveles_final": {c: {"goles_base": round(a, 3), "ventaja_local": round(b, 3)}
                                for c, (a, b) in v3.niveles().items()},
              "betas_final": [round(b, 4) for b in v3.betas], "rho_final": round(v3.rho, 4)}
    modelos = ["DIXON_COLES_COEF", "DIXON_COLES_LIGAS"]
    salida["ucl"] = {"total": tabla(filas_ucl, modelos, f"UCL prueba {sorted(prueba)}")}
    for t in sorted(prueba):
        salida["ucl"][str(t)] = tabla([f for f in filas_ucl if f["id_temporada"] == t], modelos, f"UCL {t}")
    salida["ucl"]["v3_menos_v2"] = dif_bootstrap(filas_ucl, "DIXON_COLES_LIGAS", "DIXON_COLES_COEF")
    print(f"v3 - v2 (log loss, negativo = v3 mejor): {salida['ucl']['v3_menos_v2']}")

    espn = cierre_espn()
    mercado = agregar_mercado(filas_ucl, espn, lambda f: (f["fecha"], f["local"], f["visita"]), "ESPN_BET")
    if mercado:
        claves = {(f["comp"], f["fecha"], f["local"], f["visita"]) for f in mercado}
        filas_ucl += mercado
        salida["ucl"]["vs_espn_bet"] = tabla(filas_ucl, modelos + ["MERCADO_SHIN_ESPN_BET"],
                                             "UCL con cierre ESPN BET", claves)
        for m in modelos:
            salida["ucl"]["vs_espn_bet"][f"{m}_menos_shin"] = dif_bootstrap(filas_ucl, m, "MERCADO_SHIN_ESPN_BET")
            print(f"{m} - mercado Shin: {salida['ucl']['vs_espn_bet'][f'{m}_menos_shin']}")

    # 3) Prueba ligas: v3 contra el cierre de Pinnacle y del promedio del mercado
    t0 = time.time()
    filas_liga = walk_forward_ligas(todos, prueba, vida, reg)
    print(f"\nLigas: {len(filas_liga)} pronósticos ({time.time() - t0:.0f} s)")
    clave_liga = lambda f: (f["comp"], f["id_temporada"], f["local"], f["visita"])  # noqa: E731
    for casa in ("PINNACLE", "PROMEDIO"):
        filas_liga += agregar_mercado(filas_liga, cierre_ligas(casa), clave_liga, casa)
    salida["ligas"] = {}
    for casa in ("PINNACLE", "PROMEDIO"):
        claves = {(f["comp"], f["fecha"], f["local"], f["visita"]) for f in filas_liga
                  if f["modelo"] == f"MERCADO_SHIN_{casa}"}
        salida["ligas"][casa] = tabla(filas_liga, ["DIXON_COLES_LIGAS", f"MERCADO_PROP_{casa}",
                                                   f"MERCADO_SHIN_{casa}"], f"Ligas con cierre {casa}", claves)
        salida["ligas"][casa]["v3_menos_shin"] = dif_bootstrap(filas_liga, "DIXON_COLES_LIGAS", f"MERCADO_SHIN_{casa}")
        print(f"v3 - Shin {casa}: {salida['ligas'][casa]['v3_menos_shin']}")
    salida["ligas"]["por_liga"] = {}
    for c in sorted(PAIS_LIGA):
        claves = {(f["comp"], f["fecha"], f["local"], f["visita"]) for f in filas_liga
                  if f["modelo"] == "MERCADO_SHIN_PROMEDIO" and f["comp"] == c}
        salida["ligas"]["por_liga"][c] = tabla(filas_liga, ["DIXON_COLES_LIGAS", "MERCADO_SHIN_PROMEDIO"],
                                               f"{c} (cierre promedio)", claves)

    destino = RAIZ / "data" / "gold"
    destino.mkdir(parents=True, exist_ok=True)
    todas = filas_ucl + filas_liga
    with (destino / "backtest_ligas_pronosticos.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(todas[0]))
        w.writeheader()
        w.writerows(todas)
    (destino / "backtest_ligas_metricas.json").write_text(
        json.dumps(salida, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nCSV y métricas en {destino}")


if __name__ == "__main__":
    main()
