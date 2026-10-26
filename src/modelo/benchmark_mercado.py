"""
Benchmark contra el mercado: probabilidad implícita SIN margen de la cuota de
CIERRE (por defecto DraftKings, casa_id 100) frente a los modelos del backtest.

Quitar el margen (overround):
  - PROPORCIONAL: p_i = (1/c_i) / Σ(1/c_j).
  - SHIN (1993): supone una fracción z de apostadores con información; resuelve
        p_i = (√(z² + 4(1-z)·q_i²/Q) - z) / (2(1-z)),  q_i = 1/c_i, Q = Σq,
    con z tal que Σp = 1 (iteración de punto fijo de Jullien-Salanié).
    Reparte menos margen a los favoritos (corrige el sesgo favorito-sorpresa).

Compara en el subconjunto de partidos con cuota (mismo n para todos) y da el
IC 95 % por bootstrap pareado de (modelo - mercado) en log loss.

Uso:
    py src/modelo/benchmark_mercado.py [--casa 100]

Lee data/gold/backtest_pronosticos.csv (src/modelo/backtest.py),
data/silver/espn_partidos.csv y data/silver/espn_cuotas.csv.

Autor : Ruth Choque (con PROMETHEUS) · 2026-10-22
"""

import argparse
import csv
import json
from pathlib import Path

import numpy as np

from backtest import metricas

RAIZ = Path(__file__).resolve().parents[2]


def sin_margen_proporcional(cuotas):
    q = 1 / np.asarray(cuotas, dtype=float)
    return q / q.sum()


def sin_margen_shin(cuotas, iteraciones=1000, tol=1e-12):
    q = 1 / np.asarray(cuotas, dtype=float)
    total, n, z = q.sum(), len(q), 0.0
    for _ in range(iteraciones):
        raiz = np.sqrt(z ** 2 + 4 * (1 - z) * q ** 2 / total)
        z_nuevo = (raiz.sum() - 2) / (n - 2)
        if abs(z_nuevo - z) < tol:
            z = z_nuevo
            break
        z = z_nuevo
    p = (np.sqrt(z ** 2 + 4 * (1 - z) * q ** 2 / total) - z) / (2 * (1 - z))
    return p / p.sum()


def leer_csv(nombre):
    with (RAIZ / "data" / nombre).open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser(description="Benchmark de modelos vs cuota de cierre sin margen")
    ap.add_argument("--casa", default="100", help="casa_id de ESPN (100 = DraftKings)")
    args = ap.parse_args()

    evento = {r["espn_event_id"]: (r["fecha"], r["local"], r["visita"]) for r in leer_csv("silver/espn_partidos.csv")}
    cierre = {}
    for r in leer_csv("silver/espn_cuotas.csv"):
        if r["tipo_linea"] == "CIERRE" and r["casa_id"] == args.casa and r["espn_event_id"] in evento:
            cierre[evento[r["espn_event_id"]]] = [float(r["cuota_local"]), float(r["cuota_empate"]), float(r["cuota_visita"])]

    filas = [f for f in leer_csv("gold/backtest_pronosticos.csv") if (f["fecha"], f["local"], f["visita"]) in cierre]
    for f in filas:
        f["id_temporada"] = int(f["id_temporada"])
        for k in ("prob_local", "prob_empate", "prob_visita"):
            f[k] = float(f[k])
    base = {(f["fecha"], f["local"], f["visita"]): f for f in filas if f["modelo"] == "DIXON_COLES"}
    margenes = []
    for clave, f in base.items():
        c = cierre[clave]
        margenes.append(sum(1 / x for x in c) - 1)
        for nombre, fn in (("MERCADO_PROP", sin_margen_proporcional), ("MERCADO_SHIN", sin_margen_shin)):
            p = fn(c)
            filas.append({**f, "modelo": nombre, "prob_local": p[0], "prob_empate": p[1], "prob_visita": p[2]})

    total = len({(f["fecha"], f["local"], f["visita"]) for f in filas if f["modelo"] == "DIXON_COLES"})
    print(f"Partidos del backtest con cuota de cierre (casa {args.casa}): {total} · margen medio {np.mean(margenes):.3%}")
    temporadas = sorted({f["id_temporada"] for f in filas})
    print(f"{'modelo':<13} {'temporada':>9} {'n':>4} {'log_loss':>9} {'brier':>7} {'rps':>7} {'acierto':>8}")
    salida = {}
    for m in ("FRECUENCIA", "ELO_UCL", "DIXON_COLES", "DIXON_COLES_COEF", "MERCADO_PROP", "MERCADO_SHIN"):
        salida[m] = {}
        for t in temporadas + [None]:
            r = metricas([f for f in filas if f["modelo"] == m and (t is None or f["id_temporada"] == t)])
            salida[m]["todas" if t is None else str(t)] = r
            print(f"{m:<13} {('todas' if t is None else t):>9} {r['n']:>4} {r['log_loss']:>9} {r['brier']:>7} {r['rps']:>7} {r['acierto']:>8}")

    # Bootstrap pareado: modelo - mercado (Shin) en log loss por partido
    def ll_por_partido(modelo):
        d = {}
        for f in filas:
            if f["modelo"] == modelo:
                p = {"L": f["prob_local"], "E": f["prob_empate"], "V": f["prob_visita"]}[f["resultado"]]
                d[(f["fecha"], f["local"], f["visita"])] = -np.log(max(p, 1e-12))
        return d

    mercado = ll_por_partido("MERCADO_SHIN")
    rng = np.random.default_rng(42)
    print("\nDiferencia de log loss vs MERCADO_SHIN (negativo = el modelo es mejor):")
    for m in ("ELO_UCL", "DIXON_COLES", "DIXON_COLES_COEF", "MERCADO_PROP"):
        mod = ll_por_partido(m)
        d = np.array([mod[k] - mercado[k] for k in mercado])
        bs = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(5000)]
        lo, hi = np.percentile(bs, [2.5, 97.5])
        salida[m]["dif_vs_shin"] = {"media": round(float(d.mean()), 4), "ic95": [round(float(lo), 4), round(float(hi), 4)]}
        print(f"   {m:<13} {d.mean():+.4f}  IC95% [{lo:+.4f}, {hi:+.4f}]")

    (RAIZ / "data" / "gold" / "benchmark_mercado.json").write_text(
        json.dumps(salida, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
