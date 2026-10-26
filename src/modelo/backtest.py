"""
Backtest walk-forward de los modelos 1X2 (sin leakage).

Para cada fecha con partidos de las temporadas de prueba, cada modelo se
entrena solo con partidos jugados ANTES de esa fecha y pronostica los de ese día.

Modelos:
  - FRECUENCIA : proporción histórica de L / E / V (baseline ingenuo).
  - ELO_UCL    : Elo calculado solo con partidos de Champions; probabilidades
                 con un logit ordenado sobre la diferencia de Elo.
  - DIXON_COLES: src/modelo/dixon_coles.py (v1, solo partidos UCL).
  - DIXON_COLES_COEF: v2, ataque/defensa parten del coeficiente UEFA del club
                 (src/modelo/coeficientes.py); corrige a los debutantes.

Hiperparámetros de cada Dixon-Coles (vida media, regularización) y de Elo (K) se
eligen con la temporada de VALIDACIÓN; las de PRUEBA no se tocan hasta el final.

Métricas: log loss, Brier multiclase (0 = perfecto, 2 = peor), RPS y acierto.

Uso:
    py src/modelo/backtest.py                       # validación 2022, prueba 2023-2025
    py src/modelo/backtest.py --rapido              # grilla chica

Lee data/silver/*_partidos.csv (openfootball tiene prioridad sobre ESPN) y deja
data/gold/backtest_pronosticos.csv y data/gold/backtest_metricas.json.

Autor : Ruth Choque (con PROMETHEUS) · 2026-10-22
"""

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

from coeficientes import anotar, leer_coeficientes
from dixon_coles import DixonColes

RAIZ = Path(__file__).resolve().parents[2]
RESULTADOS = ("L", "E", "V")


# -----------------------------------------------------------------------------
# Datos
# -----------------------------------------------------------------------------
def leer_partidos():
    partidos, vistos = [], set()
    for nombre in ("openfootball_partidos.csv", "espn_partidos.csv"):
        ruta = RAIZ / "data" / "silver" / nombre
        if not ruta.exists():
            continue
        with ruta.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                clave = (r["id_temporada"], r["id_ronda"], r["local"], r["visita"])
                if clave in vistos:
                    continue
                vistos.add(clave)
                jugado = (r.get("estado") or "JUGADO") == "JUGADO"
                partidos.append({
                    "id_temporada": int(r["id_temporada"]), "id_ronda": int(r["id_ronda"]),
                    "fecha": date.fromisoformat(r["fecha"]), "local": r["local"], "visita": r["visita"],
                    "es_neutral": r["es_neutral"] == "True", "jugado": jugado,
                    "goles_local_90": int(r["goles_local_90"]) if jugado else None,
                    "goles_visita_90": int(r["goles_visita_90"]) if jugado else None,
                })
    partidos.sort(key=lambda p: (p["fecha"], p["local"]))
    anotar(partidos, leer_coeficientes())
    return partidos


def resultado(p):
    gl, gv = p["goles_local_90"], p["goles_visita_90"]
    return "L" if gl > gv else ("E" if gl == gv else "V")


# -----------------------------------------------------------------------------
# Métricas
# -----------------------------------------------------------------------------
def metricas(filas):
    if not filas:
        return {}
    ll = br = rps = ac = 0.0
    for f in filas:
        p = [f["prob_local"], f["prob_empate"], f["prob_visita"]]
        o = [1.0 if f["resultado"] == r else 0.0 for r in RESULTADOS]
        ll += -math.log(max(p[RESULTADOS.index(f["resultado"])], 1e-12))
        br += sum((pi - oi) ** 2 for pi, oi in zip(p, o))
        rps += ((p[0] - o[0]) ** 2 + (p[0] + p[1] - o[0] - o[1]) ** 2) / 2
        ac += RESULTADOS[int(np.argmax(p))] == f["resultado"]
    n = len(filas)
    return {"n": n, "log_loss": round(ll / n, 4), "brier": round(br / n, 4),
            "rps": round(rps / n, 4), "acierto": round(ac / n, 4)}


# -----------------------------------------------------------------------------
# Baselines
# -----------------------------------------------------------------------------
def prob_frecuencia(entreno):
    c = defaultdict(int)
    for p in entreno:
        c[resultado(p)] += 1
    n = sum(c.values())
    return {"prob_local": c["L"] / n, "prob_empate": c["E"] / n, "prob_visita": c["V"] / n}


class EloUCL:
    """Elo solo con partidos de Champions. Regresión a la media entre temporadas."""

    def __init__(self, k=25.0, ventaja=60.0, regresion=0.3):
        self.k, self.ventaja, self.regresion = k, ventaja, regresion
        self.elo = defaultdict(lambda: 1500.0)
        self.temporada = None
        self.historial = []                  # (dif_elo pre-partido, resultado) para el logit
        self.logit = (1.0, -0.4, 0.4)        # (pendiente, umbral V|E, umbral E|L)

    def dif(self, p):
        return self.elo[p["local"]] - self.elo[p["visita"]] + (0 if p["es_neutral"] else self.ventaja)

    def actualizar(self, p):
        if p["id_temporada"] != self.temporada:
            for e in self.elo:
                self.elo[e] = 1500 + (self.elo[e] - 1500) * (1 - self.regresion)
            self.temporada = p["id_temporada"]
        d = self.dif(p)
        esperado = 1 / (1 + 10 ** (-d / 400))
        real = {"L": 1.0, "E": 0.5, "V": 0.0}[resultado(p)]
        margen = math.log(abs(p["goles_local_90"] - p["goles_visita_90"]) + 1) + 1
        delta = self.k * margen * (real - esperado)
        self.historial.append((d, resultado(p)))
        self.elo[p["local"]] += delta
        self.elo[p["visita"]] -= delta

    def ajustar_logit(self):
        d = np.array([h[0] for h in self.historial]) / 400
        y = np.array([RESULTADOS.index(h[1]) for h in self.historial])   # 0=L, 1=E, 2=V

        def nll(t):
            s, c1, c2 = t[0], t[1], t[1] + math.exp(t[2])
            pv = 1 / (1 + np.exp(-(c1 - s * d)))
            pve = 1 / (1 + np.exp(-(c2 - s * d)))
            p = np.select([y == 2, y == 1], [pv, pve - pv], 1 - pve)
            return -np.log(np.maximum(p, 1e-12)).sum()

        r = minimize(nll, [2.0, -0.5, 0.0], method="Nelder-Mead")
        self.logit = (r.x[0], r.x[1], r.x[1] + math.exp(r.x[2]))

    def predecir(self, p):
        s, c1, c2 = self.logit
        d = self.dif(p) / 400
        pv = 1 / (1 + math.exp(-(c1 - s * d)))
        pve = 1 / (1 + math.exp(-(c2 - s * d)))
        return {"prob_local": 1 - pve, "prob_empate": pve - pv, "prob_visita": pv}


# -----------------------------------------------------------------------------
# Walk-forward
# -----------------------------------------------------------------------------
def walk_forward(partidos, temporadas, vida_media, reg, k_elo, vida_coef=None, reg_coef=None):
    """Sin vida_coef / reg_coef no corre DIXON_COLES_COEF."""
    jugados = [p for p in partidos if p["jugado"]]
    objetivo = [p for p in jugados if p["id_temporada"] in temporadas]
    fechas = sorted({p["fecha"] for p in objetivo})
    dc = DixonColes(vida_media_dias=vida_media, reg=reg, usa_coef=False)
    dc2 = DixonColes(vida_media_dias=vida_coef, reg=reg_coef) if vida_coef else None
    elo = EloUCL(k=k_elo)
    filas, i_elo, temporada_logit = [], 0, None

    for fecha in fechas:
        entreno = [p for p in jugados if p["fecha"] < fecha]
        while i_elo < len(jugados) and jugados[i_elo]["fecha"] < fecha:
            elo.actualizar(jugados[i_elo])
            i_elo += 1
        del_dia = [p for p in objetivo if p["fecha"] == fecha]
        if del_dia[0]["id_temporada"] != temporada_logit:          # logit de Elo: una vez por temporada
            elo.ajustar_logit()
            temporada_logit = del_dia[0]["id_temporada"]
        dc.ajustar(entreno, fecha)
        if dc2:
            dc2.ajustar(entreno, fecha)
        freq = prob_frecuencia(entreno)
        for p in del_dia:
            base = {"id_temporada": p["id_temporada"], "id_ronda": p["id_ronda"], "fecha": p["fecha"].isoformat(),
                    "local": p["local"], "visita": p["visita"], "es_neutral": p["es_neutral"],
                    "goles_local_90": p["goles_local_90"], "goles_visita_90": p["goles_visita_90"],
                    "resultado": resultado(p)}
            filas.append({**base, "modelo": "FRECUENCIA", **freq})
            filas.append({**base, "modelo": "ELO_UCL", **elo.predecir(p)})
            filas.append({**base, "modelo": "DIXON_COLES", **dc.predecir(p["local"], p["visita"], p["es_neutral"])})
            if dc2:
                filas.append({**base, "modelo": "DIXON_COLES_COEF",
                              **dc2.predecir(p["local"], p["visita"], p["es_neutral"], p["x_local"], p["x_visita"])})
    return filas, dc2 or dc


def resumen(filas, modelo, temporadas=None):
    return metricas([f for f in filas if f["modelo"] == modelo
                     and (temporadas is None or f["id_temporada"] in temporadas)])


# -----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Backtest walk-forward 1X2")
    ap.add_argument("--validacion", default="2022")
    ap.add_argument("--prueba", default="2023,2024,2025")
    ap.add_argument("--rapido", action="store_true", help="grilla chica de hiperparámetros")
    args = ap.parse_args()
    validacion = {int(t) for t in args.validacion.split(",")}
    prueba = [int(t) for t in args.prueba.split(",")]
    partidos = leer_partidos()

    # 1) Elegir hiperparámetros con la temporada de validación
    vidas = [365, 730] if args.rapido else [180, 365, 730, 1095]
    regs = [1.0, 4.0] if args.rapido else [0.5, 1.0, 2.0, 4.0, 8.0]
    ks = [15, 30] if args.rapido else [10, 20, 30, 45]
    grilla = []
    for vm in vidas:
        for rg in regs:
            filas, _ = walk_forward(partidos, validacion, vm, rg, ks[0])
            grilla.append((resumen(filas, "DIXON_COLES")["log_loss"], vm, rg))
    _, vida, reg = min(grilla)
    regs_coef = [1.0, 4.0, 16.0] if args.rapido else [0.5, 1.0, 2.0, 4.0, 8.0, 16.0]
    grilla_coef = []
    for vm in vidas:
        for rg in regs_coef:
            filas, _ = walk_forward(partidos, validacion, vida, reg, ks[0], vm, rg)
            grilla_coef.append((resumen(filas, "DIXON_COLES_COEF")["log_loss"], vm, rg))
    _, vida_coef, reg_coef = min(grilla_coef)
    grilla_k = []
    for k in ks:
        filas, _ = walk_forward(partidos, validacion, vida, reg, k)
        grilla_k.append((resumen(filas, "ELO_UCL")["log_loss"], k))
    _, k_elo = min(grilla_k)
    print(f"Validación {sorted(validacion)}: Dixon-Coles vida media {vida} d, reg {reg} · "
          f"DC coef vida media {vida_coef} d, reg {reg_coef} · Elo K {k_elo}")
    for nombre, g in (("DC", grilla), ("DC coef", grilla_coef)):
        for ll, vm, rg in sorted(g)[:5]:
            print(f"   {nombre:<7} vida {vm:>5} reg {rg:>4}: log loss {ll}")

    # 2) Prueba (una sola corrida con los hiperparámetros elegidos)
    filas, dc = walk_forward(partidos, set(prueba), vida, reg, k_elo, vida_coef, reg_coef)
    salida = {"validacion": sorted(validacion), "prueba": prueba,
              "hiperparametros": {"vida_media_dias": vida, "reg": reg, "k_elo": k_elo,
                                  "vida_media_dias_coef": vida_coef, "reg_coef": reg_coef},
              "rho": round(dc.rho, 4), "ventaja_local": round(dc.ventaja_local, 4),
              "betas_coef": [round(b, 4) for b in dc.betas], "modelos": {}}
    print(f"\nPrueba {prueba}  (DC coef: ρ = {dc.rho:.3f}, ventaja local ×{dc.ventaja_local:.3f} en goles, "
          f"βa = {dc.betas[0]:.3f}, βd = {dc.betas[1]:.3f})")
    print(f"{'modelo':<16} {'temporada':>9} {'n':>4} {'log_loss':>9} {'brier':>7} {'rps':>7} {'acierto':>8}")
    for m in ("FRECUENCIA", "ELO_UCL", "DIXON_COLES", "DIXON_COLES_COEF"):
        salida["modelos"][m] = {"total": resumen(filas, m)}
        for t in prueba + [None]:
            r = resumen(filas, m, None if t is None else {t})
            if t is not None:
                salida["modelos"][m][str(t)] = r
            print(f"{m:<16} {('todas' if t is None else t):>9} {r['n']:>4} {r['log_loss']:>9} "
                  f"{r['brier']:>7} {r['rps']:>7} {r['acierto']:>8}")

    destino = RAIZ / "data" / "gold"
    destino.mkdir(parents=True, exist_ok=True)
    with (destino / "backtest_pronosticos.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for fila in filas for k in fila)))
        w.writeheader()
        w.writerows(filas)
    (destino / "backtest_metricas.json").write_text(json.dumps(salida, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nCSV y métricas en {destino}")


if __name__ == "__main__":
    main()
