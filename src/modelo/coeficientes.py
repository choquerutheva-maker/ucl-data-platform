"""
Coeficiente de club UEFA como variable del modelo.

x = log(coeficiente) centrado en la media de los clubes que juegan esa
temporada (los de gold.f_partido / data/silver/*_partidos.csv). El coeficiente
de la temporada S es el del sorteo de S (ranking cerrado al terminar S-1/S),
así que no hay leakage. Un club sin coeficiente queda en x = 0 (promedio).

Autor : Ruth Choque (con PROMETHEUS) · 2026-10-26
"""

import csv
import math
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]


def leer_coeficientes(cx=None):
    """{(id_temporada, nombre_canonico): coeficiente} desde PostgreSQL (cx) o el CSV local."""
    if cx is not None:
        filas = cx.execute("""SELECT c.id_temporada, e.nombre_canonico, c.coeficiente
                              FROM gold.f_coeficiente_uefa c
                              JOIN gold.d_equipo e ON e.id_equipo = c.id_equipo""").fetchall()
        return {(int(t), n): float(c) for t, n, c in filas}
    ruta = RAIZ / "data" / "silver" / "uefa_coeficientes.csv"
    if not ruta.exists():
        return {}
    with ruta.open(encoding="utf-8") as f:
        return {(int(r["id_temporada"]), r["nombre_canonico"]): float(r["coeficiente"])
                for r in csv.DictReader(f)}


def anotar(partidos, coefs):
    """Agrega x_local / x_visita a cada partido. Devuelve {(temporada, club): x}."""
    clubes = defaultdict(set)
    for p in partidos:
        clubes[p["id_temporada"]] |= {p["local"], p["visita"]}
    x = {}
    for t, cs in clubes.items():
        logs = {c: math.log(coefs[(t, c)]) for c in cs if coefs.get((t, c), 0) > 0}
        if not logs:
            continue
        media = sum(logs.values()) / len(logs)
        x.update({(t, c): v - media for c, v in logs.items()})
    for p in partidos:
        p["x_local"] = x.get((p["id_temporada"], p["local"]), 0.0)
        p["x_visita"] = x.get((p["id_temporada"], p["visita"]), 0.0)
    return x
