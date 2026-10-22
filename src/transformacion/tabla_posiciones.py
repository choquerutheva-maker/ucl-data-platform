"""
Tabla de posiciones por jornada (gold.f_tabla_posiciones), calculada desde
gold.f_partido con las reglas de gold.d_regla_desempate (DDL 002).

Una foto por temporada, jornada y equipo con lo jugado HASTA esa jornada
(incluida). Formato GRUPOS: posición dentro del grupo. Formato LIGA: tabla
única de 36.

Desempate:
  - GRUPOS (G1/G2): H2H entre los empatados (puntos, DG, GF[, goles de
    visitante]); si el H2H separa solo a una parte, se REAPLICA entre los que
    siguen empatados; luego criterios generales.
  - LIGA (L1): posicion_provisional con criterios_en_curso; posicion_final en
    la jornada 8 (fase completa) con la regla entera, incluidos los rivales
    enfrentados.
  - DISCIPLINA no está disponible (no hay tarjetas): si un empate llega hasta
    ahí se resuelve por coeficiente UEFA (o por nombre si no hay coeficiente)
    y la fila queda con desempate_aproximado = TRUE.

Validación (temporadas con eliminatorias cargadas): los clasificados de la
tabla final deben ser exactamente los equipos que juegan la ronda siguiente.

Uso:
    py src/transformacion/tabla_posiciones.py                  # temporada actual
    py src/transformacion/tabla_posiciones.py --temporadas todas
    py src/transformacion/tabla_posiciones.py --temporadas 2023,2024

Con DATABASE_URL lee gold.f_partido y reemplaza las filas de esas temporadas;
sin ella lee data/silver/*_partidos.csv y deja data/gold/f_tabla_posiciones.csv.

Autor : Ruth Choque (con PROMETHEUS) · 2026-10-22
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
ID_GRUPOS, ID_LIGA, ID_PLAYOFF, ID_OCTAVOS = 10, 11, 20, 30
JORNADAS = {ID_GRUPOS: 6, ID_LIGA: 8}

# Reglas de gold.d_regla_desempate (DDL 002), para el modo sin base de datos
REGLAS_LOCAL = {
    "G1": (["H2H_PUNTOS", "H2H_DG", "H2H_GF", "H2H_GOLES_VISITANTE", "REAPLICAR_H2H",
            "DG", "GF", "GOLES_VISITANTE", "VICTORIAS", "VICTORIAS_VISITANTE", "DISCIPLINA", "COEFICIENTE_UEFA"], None),
    "G2": (["H2H_PUNTOS", "H2H_DG", "H2H_GF", "REAPLICAR_H2H",
            "DG", "GF", "GOLES_VISITANTE", "VICTORIAS", "VICTORIAS_VISITANTE", "DISCIPLINA", "COEFICIENTE_UEFA"], None),
    "L1": (["DG", "GF", "GOLES_VISITANTE", "VICTORIAS", "VICTORIAS_VISITANTE",
            "PUNTOS_RIVALES", "DG_RIVALES", "GF_RIVALES", "DISCIPLINA", "COEFICIENTE_UEFA"],
           ["DG", "GF", "GOLES_VISITANTE", "VICTORIAS", "VICTORIAS_VISITANTE"]),
}


def regla_local(id_temporada):
    codigo = "L1" if id_temporada >= 2024 else ("G2" if id_temporada >= 2021 else "G1")
    return REGLAS_LOCAL[codigo]


# -----------------------------------------------------------------------------
# Estadísticas
# -----------------------------------------------------------------------------
def acumular(partidos, equipos=None):
    """Totales por equipo. Si se pasa `equipos`, solo partidos entre ellos (H2H)."""
    st = defaultdict(lambda: dict(pj=0, pg=0, pe=0, pp=0, gf=0, gc=0, puntos=0,
                                  goles_visitante=0, victorias_visitante=0, rivales=[]))
    for p in partidos:
        l, v = p["local"], p["visita"]
        if equipos is not None and (l not in equipos or v not in equipos):
            continue
        gl, gv = p["goles_local_90"], p["goles_visita_90"]
        for eq, gf, gc, visita in ((l, gl, gv, False), (v, gv, gl, True)):
            s = st[eq]
            s["pj"] += 1
            s["gf"] += gf
            s["gc"] += gc
            s["rivales"].append(v if eq == l else l)
            if visita:
                s["goles_visitante"] += gf
            if gf > gc:
                s["pg"] += 1
                s["puntos"] += 3
                s["victorias_visitante"] += visita
            elif gf == gc:
                s["pe"] += 1
                s["puntos"] += 1
            else:
                s["pp"] += 1
    return st


def clave_general(criterio, eq, st):
    s = st[eq]
    if criterio == "DG":
        return s["gf"] - s["gc"]
    if criterio == "GF":
        return s["gf"]
    if criterio == "GOLES_VISITANTE":
        return s["goles_visitante"]
    if criterio == "VICTORIAS":
        return s["pg"]
    if criterio == "VICTORIAS_VISITANTE":
        return s["victorias_visitante"]
    if criterio == "PUNTOS_RIVALES":
        return sum(st[r]["puntos"] for r in s["rivales"])
    if criterio == "DG_RIVALES":
        return sum(st[r]["gf"] - st[r]["gc"] for r in s["rivales"])
    if criterio == "GF_RIVALES":
        return sum(st[r]["gf"] for r in s["rivales"])
    raise ValueError(f"Criterio desconocido: {criterio}")


CLAVES_H2H = {"H2H_PUNTOS": "puntos", "H2H_GF": "gf", "H2H_GOLES_VISITANTE": "goles_visitante"}


def partir(equipos, clave):
    """Agrupa equipos por clave (desc) -> lista de subgrupos empatados, en orden."""
    grupos = defaultdict(list)
    for e in equipos:
        grupos[clave(e)].append(e)
    return [sorted(grupos[k]) for k in sorted(grupos, reverse=True)]


# -----------------------------------------------------------------------------
# Ordenamiento
# -----------------------------------------------------------------------------
def ordenar(equipos, partidos, st, criterios, coeficiente):
    """Devuelve [(equipo, aproximado, empate)] en orden. `equipos` ya empatados en puntos.
    `empate` = conjunto de equipos que siguen empatados en todo (comparten puesto) o None."""
    if len(equipos) == 1:
        return [(equipos[0], False, None)]
    h2h = [c for c in criterios if c.startswith("H2H_")]
    generales = [c for c in criterios if not c.startswith("H2H_") and c != "REAPLICAR_H2H"]

    if h2h:
        mini = acumular(partidos, set(equipos))

        def clave_h2h(e):
            return tuple((mini[e]["gf"] - mini[e]["gc"]) if c == "H2H_DG" else mini[e][CLAVES_H2H[c]] for c in h2h)

        subgrupos = partir(equipos, clave_h2h)
        if len(subgrupos) > 1:
            salida = []
            for sg in subgrupos:
                # Reaplicar: el H2H se recalcula solo entre los que siguen empatados
                sig = criterios if "REAPLICAR_H2H" in criterios else generales
                salida += ordenar(sg, partidos, st, sig, coeficiente)
            return salida
    return ordenar_generales(equipos, st, generales, coeficiente)


def ordenar_generales(equipos, st, criterios, coeficiente):
    if len(equipos) == 1:
        return [(equipos[0], False, None)]
    for i, c in enumerate(criterios):
        if c in ("DISCIPLINA", "COEFICIENTE_UEFA"):
            break
        subgrupos = partir(equipos, lambda e: clave_general(c, e, st))
        if len(subgrupos) > 1:
            return [x for sg in subgrupos for x in ordenar_generales(sg, st, criterios[i + 1:], coeficiente)]
    else:
        # Se acabaron los criterios del ranking provisional: empate real, comparten puesto (como UEFA)
        empate = frozenset(equipos)
        return [(e, False, empate) for e in sorted(equipos)]
    # Llegó a DISCIPLINA: sin tarjetas, se resuelve por coeficiente (o nombre) y se marca
    orden = sorted(equipos, key=lambda e: (-(coeficiente.get(e) or 0), e))
    return [(e, True, None) for e in orden]


def puestos(orden):
    """[(equipo, aproximado, empate)] -> {equipo: (puesto, aproximado)}; los empatados comparten puesto."""
    salida, previo = {}, None
    for i, (e, aprox, empate) in enumerate(orden, 1):
        puesto = salida[previo[0]][0] if empate is not None and previo and previo[1] == empate else i
        salida[e] = (puesto, aprox)
        previo = (e, empate)
    return salida


def tabla(partidos, criterios, coeficiente):
    st = acumular(partidos)
    salida = []
    for sg in partir(st.keys(), lambda e: st[e]["puntos"]):
        salida += ordenar(sg, partidos, st, criterios, coeficiente)
    return st, salida


def franja(id_ronda, pos):
    if id_ronda == ID_LIGA:
        return "TOP8" if pos <= 8 else ("PLAYOFF" if pos <= 24 else "ELIMINADO")
    return "CLASIFICA" if pos <= 2 else ("EUROPA_LEAGUE" if pos == 3 else "ELIMINADO")


def calcular_temporada(id_temporada, partidos, regla, coeficiente):
    """partidos: todos los de la fase (GRUPOS o LIGA) de la temporada, jugados o no."""
    criterios, en_curso = regla
    fase = [p for p in partidos if p["id_ronda"] in JORNADAS]
    if not fase:
        return []
    id_ronda = fase[0]["id_ronda"]
    jugados = [p for p in fase if p["estado"] == "JUGADO"]
    total_jornadas = JORNADAS[id_ronda]
    fase_completa = len(jugados) == len(fase) and max(p["jornada"] for p in fase) == total_jornadas

    filas = []
    for jornada in sorted({p["jornada"] for p in jugados}):
        hasta = [p for p in jugados if p["jornada"] <= jornada]
        fecha_corte = max(p["fecha"] for p in hasta if p["jornada"] == jornada)
        es_final = fase_completa and jornada == total_jornadas
        bloques = defaultdict(list)                       # grupo -> partidos (LIGA: un solo bloque)
        for p in hasta:
            bloques[p["grupo"] if id_ronda == ID_GRUPOS else None].append(p)
        for grupo, ps in bloques.items():
            st, prov = tabla(ps, en_curso or criterios, coeficiente)
            final = puestos(tabla(ps, criterios, coeficiente)[1]) if es_final else {}
            for eq, (pos, aprox) in puestos(prov).items():
                s = st[eq]
                pos_final, aprox_final = final.get(eq, (None, False))
                filas.append({
                    "id_temporada": id_temporada, "jornada": jornada, "equipo": eq,
                    "id_ronda": id_ronda, "grupo": grupo,
                    "posicion_provisional": pos, "posicion_final": pos_final,
                    "pj": s["pj"], "pg": s["pg"], "pe": s["pe"], "pp": s["pp"],
                    "gf": s["gf"], "gc": s["gc"], "puntos": s["puntos"],
                    "goles_visitante": s["goles_visitante"], "victorias_visitante": s["victorias_visitante"],
                    "franja": franja(id_ronda, pos_final or pos),
                    "fecha_corte": fecha_corte,
                    "desempate_aproximado": aprox or aprox_final,
                })
    return filas


def validar(id_temporada, filas, partidos):
    """Los clasificados de la tabla final = los que juegan la ronda siguiente."""
    finales = [f for f in filas if f["posicion_final"] is not None]
    if not finales:
        return
    en_ronda = defaultdict(set)
    for p in partidos:
        en_ronda[p["id_ronda"]] |= {p["local"], p["visita"]}
    if not en_ronda[ID_OCTAVOS]:
        return                                            # eliminatorias aún no cargadas
    if finales[0]["id_ronda"] == ID_GRUPOS:
        esperado = {ID_OCTAVOS: {f["equipo"] for f in finales if f["posicion_final"] <= 2}}
    else:
        esperado = {ID_PLAYOFF: {f["equipo"] for f in finales if 9 <= f["posicion_final"] <= 24},
                    ID_OCTAVOS: {f["equipo"] for f in finales if f["posicion_final"] <= 8}}
    for id_ronda, equipos in esperado.items():
        reales = en_ronda[id_ronda] if id_ronda == ID_PLAYOFF else en_ronda[id_ronda] - en_ronda[ID_PLAYOFF]
        if equipos != reales:
            aprox = any(f["desempate_aproximado"] for f in finales)
            msg = (f"{id_temporada}: clasificados a la ronda {id_ronda} no cuadran "
                   f"(tabla - real: {sorted(equipos - reales)}, real - tabla: {sorted(reales - equipos)})")
            if not aprox:
                raise ValueError(msg)
            print(f"  ! {msg} [hubo desempate aproximado]", file=sys.stderr)


# -----------------------------------------------------------------------------
# Lectura y escritura
# -----------------------------------------------------------------------------
def leer_local():
    """Misma prioridad que gold: un partido de ESPN solo entra si openfootball no lo tiene."""
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
                estado = r.get("estado") or "JUGADO"
                partidos.append({
                    "id_temporada": int(r["id_temporada"]), "id_ronda": int(r["id_ronda"]),
                    "jornada": int(r["jornada"]) if r["jornada"] else None, "grupo": r["grupo"] or None,
                    "fecha": r["fecha"], "local": r["local"], "visita": r["visita"], "estado": estado,
                    "goles_local_90": int(r["goles_local_90"]) if estado == "JUGADO" else None,
                    "goles_visita_90": int(r["goles_visita_90"]) if estado == "JUGADO" else None,
                })
    return partidos


SQL_PARTIDOS = """
    SELECT p.id_temporada, p.id_ronda, p.jornada, p.grupo, p.fecha::TEXT,
           l.nombre_canonico, v.nombre_canonico, p.estado, p.goles_local_90, p.goles_visita_90
    FROM gold.f_partido p
    JOIN gold.d_equipo l ON l.id_equipo = p.id_equipo_local
    JOIN gold.d_equipo v ON v.id_equipo = p.id_equipo_visita
    WHERE p.id_temporada = ANY(%s)"""

SQL_REGLAS = """
    SELECT t.id_temporada, r.criterios, r.criterios_en_curso
    FROM gold.d_temporada t JOIN gold.d_regla_desempate r ON r.id_regla = t.id_regla_desempate
    WHERE t.id_temporada = ANY(%s)"""

SQL_COEFICIENTE = """
    SELECT c.id_temporada, e.nombre_canonico, c.coeficiente
    FROM gold.f_coeficiente_uefa c JOIN gold.d_equipo e ON e.id_equipo = c.id_equipo
    WHERE c.id_temporada = ANY(%s)"""

COLUMNAS = ["id_temporada", "jornada", "id_ronda", "grupo", "posicion_provisional", "posicion_final",
            "pj", "pg", "pe", "pp", "gf", "gc", "puntos", "goles_visitante", "victorias_visitante",
            "franja", "fecha_corte", "desempate_aproximado"]


def leer_postgres(cx, temporadas):
    claves = ["id_temporada", "id_ronda", "jornada", "grupo", "fecha", "local", "visita",
              "estado", "goles_local_90", "goles_visita_90"]
    partidos = [dict(zip(claves, r)) for r in cx.execute(SQL_PARTIDOS, (temporadas,)).fetchall()]
    reglas = {t: (list(c), list(ec) if ec else None)
              for t, c, ec in cx.execute(SQL_REGLAS, (temporadas,)).fetchall()}
    coef = defaultdict(dict)
    for t, eq, c in cx.execute(SQL_COEFICIENTE, (temporadas,)).fetchall():
        coef[t][eq] = float(c)
    return partidos, reglas, coef


def escribir_postgres(cx, temporadas, filas):
    cx.execute("DELETE FROM gold.f_tabla_posiciones WHERE id_temporada = ANY(%s)", (temporadas,))
    with cx.cursor() as cur:
        cur.executemany(
            f"""INSERT INTO gold.f_tabla_posiciones (id_equipo, {", ".join(COLUMNAS)})
                SELECT e.id_equipo, {", ".join(["%s"] * len(COLUMNAS))}
                FROM gold.d_equipo e WHERE e.nombre_canonico = %s""",
            [tuple(f[c] for c in COLUMNAS) + (f["equipo"],) for f in filas])


def guardar_local(filas):
    destino = RAIZ / "data" / "gold"
    destino.mkdir(parents=True, exist_ok=True)
    with (destino / "f_tabla_posiciones.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["equipo"] + COLUMNAS)
        w.writeheader()
        w.writerows(filas)


# -----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Tabla de posiciones por jornada")
    ap.add_argument("--temporadas", default="2026", help="Años de inicio separados por coma, o 'todas'")
    args = ap.parse_args()
    temporadas = (list(range(2015, 2027)) if args.temporadas == "todas"
                  else [int(t) for t in args.temporadas.split(",")])

    url_bd = os.getenv("DATABASE_URL")
    cx = None
    if url_bd:
        import psycopg
        cx = psycopg.connect(url_bd)
        partidos, reglas, coef = leer_postgres(cx, temporadas)
    else:
        partidos = [p for p in leer_local() if p["id_temporada"] in temporadas]
        reglas = {t: regla_local(t) for t in temporadas}
        coef = {}

    filas = []
    for t in temporadas:
        ps = [p for p in partidos if p["id_temporada"] == t]
        ft = calcular_temporada(t, ps, reglas[t], coef.get(t, {}))
        validar(t, ft, ps)
        jornadas = sorted({f["jornada"] for f in ft})
        aprox = sum(f["desempate_aproximado"] for f in ft)
        print(f"{t}: {len(ft)} filas · jornadas {jornadas[0] if jornadas else '-'}..{jornadas[-1] if jornadas else '-'}"
              f" · desempates aproximados {aprox}")
        filas += ft

    if cx:
        try:
            escribir_postgres(cx, temporadas, filas)
            cx.commit()
        finally:
            cx.close()
        print(f"Cargado en PostgreSQL: {len(filas)} filas en gold.f_tabla_posiciones")
    else:
        guardar_local(filas)
        print(f"Sin DATABASE_URL: CSV en {RAIZ / 'data' / 'gold'}")


if __name__ == "__main__":
    main()
