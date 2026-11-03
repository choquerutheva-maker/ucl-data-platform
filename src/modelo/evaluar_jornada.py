"""
Evaluación de una jornada en vivo: resultado real frente a los pronósticos de
los modelos publicados, de Niké y del mercado de cierre.

Fuentes (no necesita DATABASE_URL):
  - Resultados y cuotas: ESPN (scoreboard + summary) del día o días pedidos.
  - Modelos: gold.vw_dash_pronosticos por REST con la publishable key
    (pública, la misma de docs/index.html). Solo pronósticos en vivo: el
    publicador nunca emite después del inicio del partido.
  - Niké: entradas/nike/*.csv, última fila con pronosticado_en ANTERIOR al
    inicio (las revisiones con el once oficial reemplazan a la primera).
  - Mercado: CIERRE DraftKings (casa 100) sin margen proporcional; si falta,
    ESPN BET (casa 58). Se informa qué casa se usó.

Cruce de partidos: id ESPN de cada equipo -> seeds/equipos_espn.csv ->
nombre canónico + fecha, igual que en la vista.

Métricas sobre el subconjunto con TODAS las fuentes y partido terminado
(mismo n para todos): acierto del favorito, log loss, Brier, y diferencia
de log loss contra el mercado con IC 95 % por bootstrap pareado.
Con una jornada (n ≈ 9-18) el IC es muy ancho: sirve para seguimiento.

Uso:
    py src/modelo/evaluar_jornada.py 2026-11-03 2026-11-04 [--csv]

--csv guarda el detalle por partido y fuente en
data/gold/evaluacion_jornada_<fechas>.csv (data/ no se versiona).

Autor : Ruth Choque (con PROMETHEUS) · 2026-11-03
"""

import argparse
import csv
import json
import math
import os
import random
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "src" / "ingesta"))
import espn_cuotas as ec  # noqa: E402

LIGA = "uefa.champions"
CASAS = ("100", "58")  # DraftKings, ESPN BET
RES = {"L": "prob_local", "E": "prob_empate", "V": "prob_visita"}
MERCADO, NIKE = "Mercado", "Niké"


# -----------------------------------------------------------------------------
# Lectura
# -----------------------------------------------------------------------------
def credenciales():
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if url and key:
        return url, key
    html = (RAIZ / "docs" / "index.html").read_text(encoding="utf-8")
    url = re.search(r'SUPABASE_URL\s*=\s*"([^"]+)"', html).group(1)
    key = re.search(r'SUPABASE_KEY\s*=\s*"([^"]+)"', html).group(1)
    return url, key


def rest_gold(vista, query):
    url, key = credenciales()
    req = urllib.request.Request(
        f"{url}/rest/v1/{vista}?{query}",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "gold"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def utc(s):
    s = s.replace("Z", "+00:00")
    if re.match(r".*T\d\d:\d\d([+-])", s):      # 2026-11-03T20:00+00:00 -> con segundos
        s = re.sub(r"T(\d\d:\d\d)([+-])", r"T\1:00\2", s)
    return datetime.fromisoformat(s)


def canonicos():
    with open(RAIZ / "seeds" / "equipos_espn.csv", encoding="utf-8") as f:
        return {r["espn_id"]: r["nombre_canonico"] for r in csv.DictReader(f)}


def sin_margen(fila):
    inv = [1 / fila[k] for k in ("cuota_local", "cuota_empate", "cuota_visita")]
    s = sum(inv)
    return dict(zip(RES.values(), (x / s for x in inv)))


def partidos_espn(fecha, nombres):
    _, sb = ec.http_json(ec.URL_BASE.format(liga=LIGA, recurso="scoreboard") + f"?dates={fecha.replace('-', '')}")
    salida = {}
    for ev in sb.get("events", []):
        comp = ev["competitions"][0]
        eq = {c["homeAway"]: c for c in comp["competitors"]}
        estado = ev["status"]["type"]["name"]
        p = {
            "espn_event_id": ev["id"], "fecha": fecha, "inicio_utc": ev["date"], "estado": estado,
            "local": nombres.get(eq["home"]["team"]["id"], eq["home"]["team"]["displayName"]),
            "visita": nombres.get(eq["away"]["team"]["id"], eq["away"]["team"]["displayName"]),
            "prob": {}, "casa_mercado": None,
        }
        if estado == "STATUS_FULL_TIME":
            # Fase de liga: sin prórroga, el marcador final es el de 90'.
            gl, gv = int(eq["home"]["score"]), int(eq["away"]["score"])
            p.update(goles_local=gl, goles_visita=gv, resultado="L" if gl > gv else "V" if gv > gl else "E")
        _, summ = ec.http_json(ec.URL_BASE.format(liga=LIGA, recurso="summary") + f"?event={ev['id']}")
        filas = ec.filas_cuotas(ev, summ, LIGA, None)
        for casa in CASAS:
            cand = [f for f in filas if str(f["casa_id"]) == casa and f["tipo_linea"] in ("CIERRE", "ACTUAL")]
            if cand:
                p["prob"][MERCADO] = sin_margen(cand[0])
                p["casa_mercado"] = cand[0]["casa_apuestas"]
                break
        salida[ev["id"]] = p
    return salida


def agregar_nike(partidos):
    ultimas = {}
    for archivo in sorted((RAIZ / "entradas" / "nike").glob("*.csv")):
        with open(archivo, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                p = partidos.get(r["espn_event_id"])
                if not p or utc(r["pronosticado_en"]) >= utc(p["inicio_utc"]):
                    continue
                previa = ultimas.get(r["espn_event_id"])
                if previa is None or utc(r["pronosticado_en"]) > utc(previa["pronosticado_en"]):
                    ultimas[r["espn_event_id"]] = r
    for eid, r in ultimas.items():
        partidos[eid]["prob"][NIKE] = {k: float(r[k]) for k in RES.values()}


def agregar_modelos(partidos, fechas):
    filas = rest_gold("vw_dash_pronosticos",
                      "select=fecha,local,visita,modelo,prob_local,prob_empate,prob_visita"
                      f"&fecha=in.({','.join(fechas)})")
    indice = {(p["fecha"], p["local"], p["visita"]): p for p in partidos.values()}
    sin_cruce = set()
    for r in filas:
        p = indice.get((r["fecha"], r["local"], r["visita"]))
        if p is None:
            sin_cruce.add(f"{r['fecha']} {r['local']} - {r['visita']}")
            continue
        p["prob"][r["modelo"]] = {k: float(r[k]) for k in RES.values()}
    return sorted({r["modelo"] for r in filas}), sin_cruce


# -----------------------------------------------------------------------------
# Métricas
# -----------------------------------------------------------------------------
def log_loss(pr, res):
    return -math.log(max(pr[RES[res]], 1e-4))   # mismo piso que vw_dash_pronosticos


def brier(pr, res):
    return sum((pr[c] - (k == res)) ** 2 for k, c in RES.items())


def favorito(pr):
    return max(RES, key=lambda k: pr[RES[k]])


def ic_bootstrap(difs, reps=10000, semilla=42):
    rnd = random.Random(semilla)
    n = len(difs)
    medias = sorted(sum(rnd.choices(difs, k=n)) / n for _ in range(reps))
    return medias[int(0.025 * reps)], medias[int(0.975 * reps)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fechas", nargs="+", help="AAAA-MM-DD (uno o varios días de la jornada)")
    ap.add_argument("--csv", action="store_true", help="guarda el detalle en data/gold/")
    args = ap.parse_args()

    nombres = canonicos()
    partidos = {}
    for fecha in args.fechas:
        partidos.update(partidos_espn(fecha, nombres))
    agregar_nike(partidos)
    modelos, sin_cruce = agregar_modelos(partidos, args.fechas)
    fuentes = modelos + [NIKE, MERCADO]

    # Detalle por partido: probabilidad asignada al resultado real
    print(f"\nJornada {', '.join(args.fechas)} · {len(partidos)} partidos\n")
    ancho = max(len(f"{p['local']} - {p['visita']}") for p in partidos.values())
    print(f"{'Partido':{ancho}}  Res.   " + "  ".join(f"{f[:14]:>14}" for f in fuentes))
    for p in sorted(partidos.values(), key=lambda x: x["inicio_utc"]):
        nombre = f"{p['local']} - {p['visita']}"
        if "resultado" not in p:
            print(f"{nombre:{ancho}}  {p['estado'][7:]}")
            continue
        celdas = []
        for f in fuentes:
            pr = p["prob"].get(f)
            celdas.append(f"{'-':>14}" if pr is None else
                          f"{pr[RES[p['resultado']]] * 100:>11.0f} %{' ' if favorito(pr) == p['resultado'] else 'x'}")
        print(f"{nombre:{ancho}}  {p['goles_local']}-{p['goles_visita']} {p['resultado']}  " + "  ".join(celdas))
    print("\n(% = probabilidad dada al resultado real · x = el favorito de esa fuente no se dio)")

    comunes = [p for p in partidos.values() if "resultado" in p and all(f in p["prob"] for f in fuentes)]
    faltan = [f"{p['local']} - {p['visita']}: falta {', '.join(f for f in fuentes if f not in p['prob'])}"
              for p in partidos.values() if "resultado" in p and p not in comunes]
    n = len(comunes)
    print(f"\nResumen (n = {n}, partidos terminados con todas las fuentes)")
    if n:
        ll_mkt = [log_loss(p["prob"][MERCADO], p["resultado"]) for p in comunes]
        print(f"{'Fuente':16} {'Acierto':>8} {'Log loss':>9} {'Brier':>7}   Dif. vs mercado (IC 95 %)")
        for f in fuentes:
            ll = [log_loss(p["prob"][f], p["resultado"]) for p in comunes]
            ac = sum(favorito(p["prob"][f]) == p["resultado"] for p in comunes)
            br = sum(brier(p["prob"][f], p["resultado"]) for p in comunes) / n
            linea = f"{f:16} {ac:>4}/{n:<3} {sum(ll) / n:>9.3f} {br:>7.3f}"
            if f != MERCADO:
                difs = [a - b for a, b in zip(ll, ll_mkt)]
                lo, hi = ic_bootstrap(difs)
                linea += f"   {sum(difs) / n:+.3f} ({lo:+.3f}..{hi:+.3f})"
            print(linea)
        print("Mercado:", ", ".join(sorted({p['casa_mercado'] for p in comunes})), "· cierre sin margen proporcional")
    for m in faltan + [f"Sin cruce en ESPN: {s}" for s in sorted(sin_cruce)]:
        print("Aviso:", m)

    if args.csv:
        destino = RAIZ / "data" / "gold" / f"evaluacion_jornada_{'_'.join(args.fechas)}.csv"
        destino.parent.mkdir(parents=True, exist_ok=True)
        with open(destino, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["espn_event_id", "fecha", "local", "visita", "goles_local", "goles_visita", "resultado",
                        "fuente", "prob_local", "prob_empate", "prob_visita", "favorito", "log_loss", "brier"])
            for p in partidos.values():
                if "resultado" not in p:
                    continue
                for fuente, pr in p["prob"].items():
                    w.writerow([p["espn_event_id"], p["fecha"], p["local"], p["visita"], p["goles_local"],
                                p["goles_visita"], p["resultado"], fuente, *(round(pr[c], 4) for c in RES.values()),
                                favorito(pr), round(log_loss(pr, p["resultado"]), 4),
                                round(brier(pr, p["resultado"]), 4)])
        print(f"\nDetalle guardado en {destino.relative_to(RAIZ)}")


if __name__ == "__main__":
    main()
