"""
Elo propio con las 5 grandes ligas y la Champions (reemplazo de ClubElo).

ClubElo (api.clubelo.com) da 502 desde el 22-10-2026 y su API de fixtures está
desactivada, así que calculamos un Elo propio con los mismos datos del proyecto:

  - Partidos: ligas desde 2012-13 (football-data) + UCL desde 2015-16, en orden
    cronológico. Cada partido se pronostica ANTES de actualizar (sin leakage).
  - Esperado: E = 1 / (1 + 10^(-(R_local - R_visita + H_c) / 400)); H_c es la
    ventaja de local de la competición (0 en cancha neutral).
  - Actualización: ΔR = K_c · (ln(|dif. de goles| + 1) + 1) · (S - E), con K
    distinto para liga y UCL (la UCL es la que conecta las ligas entre sí).
  - Club nuevo (debutante, ascendido o de una liga no cargada): arranca en
    1500 + c · x, con x = log(coeficiente UEFA) centrado (su coeficiente o el
    piso de su asociación), igual que la v2/v3 de Dixon-Coles. Antes de 2015 se
    usa el coeficiente de 2015: solo afecta el calentamiento 2012-2014, nunca
    a las temporadas que se evalúan (2022+).
  - Probabilidades 1X2: logit ordenado sobre (ΔR + H) / 400, ajustado una vez
    por temporada con los partidos anteriores, por separado para UCL y ligas
    (la UCL tiene otra tasa de empates).

Backtest: los hiperparámetros se eligen con la UCL 2022-23 y se prueban en
2023-24 a 2025-26 contra Elo UCL v1 y Dixon-Coles v2/v3 (mismos partidos) y,
en las ligas, contra Dixon-Coles v3 y el cierre del mercado.

Uso:
    py src/modelo/elo_ligas.py                 # grilla + prueba
    py src/modelo/elo_ligas.py --rapido

Necesita data/gold/backtest_pronosticos.csv (backtest.py) y
data/gold/backtest_ligas_pronosticos.csv (backtest_ligas.py) para comparar.

Autor : Ruth Choque (con PROMETHEUS) · 2026-11-01
"""

import argparse
import csv
import itertools
import json
import math
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

from backtest import metricas, resultado
from backtest_ligas import (PAIS_LIGA, agregar_mercado, cierre_ligas, dif_bootstrap, fila, leer_todo,
                            tabla)
from coeficientes import leer_coeficientes, leer_pisos

RAIZ = Path(__file__).resolve().parents[2]
RESULTADOS = ("L", "E", "V")
PRIMERA_TEMPORADA_COEF = 2015


# -----------------------------------------------------------------------------
# Logit ordenado: (ΔR + H) / 400 -> P(L), P(E), P(V)
# -----------------------------------------------------------------------------
def ajustar_logit(historial):
    if len(historial) < 50:
        return (2.5, -0.55, 0.55)
    d = np.array([h[0] for h in historial])
    y = np.array([RESULTADOS.index(h[1]) for h in historial])

    def nll(t):
        s, c1, c2 = t[0], t[1], t[1] + math.exp(t[2])
        pv = 1 / (1 + np.exp(-(c1 - s * d)))
        pve = 1 / (1 + np.exp(-(c2 - s * d)))
        p = np.select([y == 2, y == 1], [pv, pve - pv], 1 - pve)
        return -np.log(np.maximum(p, 1e-12)).sum()

    r = minimize(nll, [2.5, -0.5, 0.0], method="Nelder-Mead")
    return (r.x[0], r.x[1], r.x[1] + math.exp(r.x[2]))


def prob_logit(logit, d):
    s, c1, c2 = logit
    pv = 1 / (1 + math.exp(-(c1 - s * d)))
    pve = 1 / (1 + math.exp(-(c2 - s * d)))
    return {"prob_local": 1 - pve, "prob_empate": pve - pv, "prob_visita": pv}


# -----------------------------------------------------------------------------
# Elo
# -----------------------------------------------------------------------------
class EloLigas:
    def __init__(self, k_liga=20.0, k_ucl=30.0, h_liga=65.0, h_ucl=65.0, c_coef=200.0, x_inicial=None):
        self.k = {"liga": k_liga, "UCL": k_ucl}
        self.h = {"liga": h_liga, "UCL": h_ucl}
        self.c_coef = c_coef
        self.x_inicial = x_inicial or (lambda club, temporada: 0.0)
        self.elo = {}
        self.historico = []                     # (fecha, club, rating después del partido)
        self.historial = {"liga": [], "UCL": []}
        self.logit = {"liga": (2.5, -0.55, 0.55), "UCL": (2.5, -0.55, 0.55)}
        self.temporada_logit = None

    @staticmethod
    def tipo(p):
        return "UCL" if p["comp"] == "UCL" else "liga"

    def rating(self, club, temporada):
        if club not in self.elo:
            self.elo[club] = 1500.0 + self.c_coef * self.x_inicial(club, temporada)
        return self.elo[club]

    def dif(self, p):
        h = 0.0 if p["es_neutral"] else self.h[self.tipo(p)]
        return (self.rating(p["local"], p["id_temporada"]) - self.rating(p["visita"], p["id_temporada"]) + h) / 400

    def predecir(self, p):
        if p["id_temporada"] != self.temporada_logit:          # un ajuste por temporada
            self.logit = {t: ajustar_logit(h) for t, h in self.historial.items()}
            self.temporada_logit = p["id_temporada"]
        return prob_logit(self.logit[self.tipo(p)], self.dif(p))

    def actualizar(self, p):
        d = self.dif(p)
        esperado = 1 / (1 + 10 ** (-d))
        r = resultado(p)
        real = {"L": 1.0, "E": 0.5, "V": 0.0}[r]
        margen = math.log(abs(p["goles_local_90"] - p["goles_visita_90"]) + 1) + 1
        delta = self.k[self.tipo(p)] * margen * (real - esperado)
        self.historial[self.tipo(p)].append((d, r))
        self.elo[p["local"]] += delta
        self.elo[p["visita"]] -= delta
        self.historico.append((p["fecha"], p["local"], self.elo[p["local"]]))
        self.historico.append((p["fecha"], p["visita"], self.elo[p["visita"]]))

    def tramos(self):
        """[(club, fecha_desde, fecha_hasta, elo)]: un tramo por día con partidos del club."""
        por_club = defaultdict(dict)
        for fecha, club, r in self.historico:
            por_club[club][fecha] = r                          # el último del día manda
        salida = []
        for club, fechas in por_club.items():
            orden = sorted(fechas)
            for i, f in enumerate(orden):
                hasta = orden[i + 1] - timedelta(days=1) if i + 1 < len(orden) else date(9999, 12, 31)
                salida.append((club, f, hasta, round(fechas[f], 2)))
        return salida


# Elegidos con la UCL 2022-23 (grilla de 144 combinaciones, main() de este archivo)
PARAMETROS = {"k_liga": 15.0, "k_ucl": 20.0, "h_liga": 70.0, "h_ucl": 55.0, "c_coef": 100.0}


def funcion_x_inicial(todos, pais_de, coefs=None, pisos=None):
    """x de arranque de cada club: su coeficiente o el piso de su asociación,
    centrado con la media de los clubes UCL de esa temporada (desde 2015)."""
    coefs = leer_coeficientes() if coefs is None else coefs
    pisos = leer_pisos() if pisos is None else pisos
    clubes = defaultdict(set)
    for p in todos:
        if p["comp"] == "UCL":
            clubes[p["id_temporada"]] |= {p["local"], p["visita"]}
    media = {}
    for t, cs in clubes.items():
        logs = [math.log(coefs[(t, c)]) for c in cs if coefs.get((t, c), 0) > 0]
        if logs:
            media[t] = sum(logs) / len(logs)

    def x(club, temporada):
        t = max(temporada, PRIMERA_TEMPORADA_COEF)
        while t not in media and t > PRIMERA_TEMPORADA_COEF:
            t -= 1
        if t not in media:
            return 0.0
        v = coefs.get((t, club)) or pisos.get((t, pais_de.get(club)))
        return math.log(v) - media[t] if v and v > 0 else 0.0

    return x


def recorrer(todos, objetivo, params, x_inicial):
    """Recorre todos los partidos en orden; pronostica los de 'objetivo' antes de actualizar."""
    elo = EloLigas(**params, x_inicial=x_inicial)
    filas = []
    for p in todos:
        pred = elo.predecir(p)                                  # también mantiene el logit al día
        if ("UCL" if p["comp"] == "UCL" else "LIGAS", p["id_temporada"]) in objetivo:
            filas.append(fila(p, "ELO_LIGAS", pred))
        elo.actualizar(p)
    return filas, elo


# -----------------------------------------------------------------------------
def leer_filas(nombre, modelos):
    ruta = RAIZ / "data" / "gold" / nombre
    if not ruta.exists():
        return []
    with ruta.open(encoding="utf-8") as f:
        salida = []
        for r in csv.DictReader(f):
            if r["modelo"] in modelos:
                for k in ("prob_local", "prob_empate", "prob_visita"):
                    r[k] = float(r[k])
                r["id_temporada"] = int(r["id_temporada"])
                r.setdefault("comp", "UCL")
                salida.append(r)
        return salida


def main():
    ap = argparse.ArgumentParser(description="Elo propio (ligas + UCL): grilla y backtest")
    ap.add_argument("--validacion", default="2022")
    ap.add_argument("--prueba", default="2023,2024,2025")
    ap.add_argument("--rapido", action="store_true")
    args = ap.parse_args()
    validacion = {int(t) for t in args.validacion.split(",")}
    prueba = {int(t) for t in args.prueba.split(",")}

    todos, pais_de = leer_todo(desde_liga=2012, con_pais=True)
    x_inicial = funcion_x_inicial(todos, pais_de)
    print(f"Partidos: {len(todos)} ({sum(p['comp'] == 'UCL' for p in todos)} UCL)")

    # 1) Grilla con la UCL de validación
    if args.rapido:
        grilla = {"k_liga": [20], "k_ucl": [20, 40], "h_liga": [65], "h_ucl": [65], "c_coef": [100, 250]}
    else:
        grilla = {"k_liga": [15, 20, 25], "k_ucl": [20, 30, 45], "h_liga": [55, 70],
                  "h_ucl": [55, 75], "c_coef": [0, 50, 100, 200]}
    obj_val = {("UCL", t) for t in validacion}
    resultados = []
    t0 = time.time()
    for valores in itertools.product(*grilla.values()):
        params = dict(zip(grilla, valores))
        filas, _ = recorrer(todos, obj_val, params, x_inicial)
        resultados.append((metricas(filas)["log_loss"], params))
    resultados.sort(key=lambda r: r[0])
    mejor = resultados[0][1]
    print(f"Grilla: {len(resultados)} combinaciones en {time.time() - t0:.0f} s")
    for ll, p in resultados[:5]:
        print(f"   {ll}  {p}")

    # 2) Prueba UCL y ligas en una sola pasada
    objetivo = {("UCL", t) for t in prueba} | {("LIGAS", t) for t in prueba}
    filas, elo = recorrer(todos, objetivo, mejor, x_inicial)
    f_ucl = [f for f in filas if f["comp"] == "UCL"]
    f_liga = [f for f in filas if f["comp"] != "UCL"]

    otros = leer_filas("backtest_pronosticos.csv", {"ELO_UCL"})
    for f in otros:
        f["comp"] = "UCL"
    otros += leer_filas("backtest_ligas_pronosticos.csv", {"DIXON_COLES_COEF", "DIXON_COLES_LIGAS"})
    otros = [f for f in otros if f["id_temporada"] in prueba]
    filas_ucl = f_ucl + [f for f in otros if f["comp"] == "UCL"]
    claves_ucl = {(f["comp"], f["fecha"], f["local"], f["visita"]) for f in f_ucl}

    salida = {"hiperparametros": mejor, "grilla_top": [{"log_loss": ll, **p} for ll, p in resultados[:10]],
              "logit_final": {k: [round(v, 4) for v in l] for k, l in elo.logit.items()}}
    modelos_ucl = ["ELO_UCL", "ELO_LIGAS", "DIXON_COLES_COEF", "DIXON_COLES_LIGAS"]
    salida["ucl"] = tabla(filas_ucl, modelos_ucl, f"UCL prueba {sorted(prueba)}", claves_ucl)
    for base in ("ELO_UCL", "DIXON_COLES_LIGAS"):
        salida["ucl"][f"elo_ligas_menos_{base.lower()}"] = d = dif_bootstrap(filas_ucl, "ELO_LIGAS", base)
        print(f"ELO_LIGAS - {base}: {d}")

    filas_liga = f_liga + [f for f in otros if f["comp"] != "UCL"]
    clave_liga = lambda f: (f["comp"], f["id_temporada"], f["local"], f["visita"])  # noqa: E731
    filas_liga += agregar_mercado(f_liga, cierre_ligas("PROMEDIO"), clave_liga, "PROMEDIO")
    claves = {(f["comp"], f["fecha"], f["local"], f["visita"]) for f in filas_liga
              if f["modelo"] == "MERCADO_SHIN_PROMEDIO"}
    salida["ligas"] = tabla(filas_liga, ["ELO_LIGAS", "DIXON_COLES_LIGAS", "MERCADO_SHIN_PROMEDIO"],
                            "Ligas con cierre promedio", claves)
    for base in ("DIXON_COLES_LIGAS", "MERCADO_SHIN_PROMEDIO"):
        salida["ligas"][f"elo_ligas_menos_{base.lower()}"] = d = dif_bootstrap(filas_liga, "ELO_LIGAS", base)
        print(f"ELO_LIGAS - {base} (ligas): {d}")

    # Ranking actual (top 15) como control de sentido común
    print("\nTop 15 al final de los datos:")
    for club, r in sorted(elo.elo.items(), key=lambda x: -x[1])[:15]:
        print(f"   {r:7.1f}  {club}")
    salida["top15"] = [[c, round(r, 1)] for c, r in sorted(elo.elo.items(), key=lambda x: -x[1])[:15]]

    destino = RAIZ / "data" / "gold"
    with (destino / "elo_ligas_pronosticos.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0]))
        w.writeheader()
        w.writerows(filas)
    (destino / "elo_ligas_metricas.json").write_text(json.dumps(salida, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nCSV y métricas en {destino}")


if __name__ == "__main__":
    main()
