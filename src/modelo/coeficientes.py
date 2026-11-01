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


def leer_pisos(cx=None):
    """{(id_temporada, pais_iso3): piso de la asociación} (nationalAssociationPoints)."""
    if cx is not None:
        filas = cx.execute("""SELECT c.id_temporada, e.pais_iso3, MAX(c.piso_asociacion)
                              FROM gold.f_coeficiente_uefa c
                              JOIN gold.d_equipo e ON e.id_equipo = c.id_equipo
                              WHERE c.piso_asociacion IS NOT NULL
                              GROUP BY 1, 2""").fetchall()
        return {(int(t), p): float(v) for t, p, v in filas}
    ruta = RAIZ / "data" / "silver" / "uefa_coeficientes.csv"
    if not ruta.exists():
        return {}
    with ruta.open(encoding="utf-8") as f:
        return {(int(r["id_temporada"]), r["pais"]): float(r["piso_asociacion"])
                for r in csv.DictReader(f) if r["piso_asociacion"]}


def anotar_conjunto(partidos, coefs, pisos, pais_de):
    """Como anotar(), para partidos de UCL y de liga juntos (v3).

    El centrado por temporada es el mismo de la v2 (media de los clubes UCL de esa
    temporada), así que el x de un club UCL no cambia. Un club sin coeficiente
    propio recibe el piso de su asociación, como hace la UEFA con los clubes
    fuera del ranking. Temporadas sin clubes UCL (antes de 2015): x = 0."""
    media = {}
    clubes = defaultdict(set)
    for p in partidos:
        if p.get("comp", "UCL") == "UCL":
            clubes[p["id_temporada"]] |= {p["local"], p["visita"]}
    for t, cs in clubes.items():
        logs = [math.log(coefs[(t, c)]) for c in cs if coefs.get((t, c), 0) > 0]
        if logs:
            media[t] = sum(logs) / len(logs)

    def x(t, c):
        if t not in media:
            return 0.0
        v = coefs.get((t, c)) or pisos.get((t, pais_de.get(c)))
        return math.log(v) - media[t] if v and v > 0 else 0.0

    for p in partidos:
        p["x_local"] = x(p["id_temporada"], p["local"])
        p["x_visita"] = x(p["id_temporada"], p["visita"])


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
