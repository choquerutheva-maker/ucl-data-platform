"""
Modelo Dixon-Coles (1997) para pronósticos 1X2 de la Champions League.

    goles_local  ~ Poisson(λ),  log λ = μ + ataque[local]  - defensa[visita] + H·(no neutral)
    goles_visita ~ Poisson(γ),  log γ = μ + ataque[visita] - defensa[local]

    ataque[e]  = βa·x[e] + δa[e]      x = log(coeficiente UEFA) centrado por temporada
    defensa[e] = βd·x[e] + δd[e]      (v2; con x = 0 es exactamente la v1)

  - Ponderación temporal: cada partido pesa exp(-ln2 · días / vida_media).
  - Regularización L2 en los desvíos δ (β sin penalizar): un club con pocos
    partidos se acerca a lo que predice su coeficiente UEFA; en la v1 (sin x)
    se acercaba a un equipo promedio, lo que exageraba con los debutantes.
  - Corrección τ de Dixon-Coles para 0-0, 1-0, 0-1 y 1-1 (parámetro ρ), ajustada
    en una segunda etapa con λ y γ fijos.

Solo usa resultados a los 90' (gold.f_partido.goles_*_90).

Autor : Ruth Choque (con PROMETHEUS) · 2026-10-22 (v2 con coeficiente UEFA: 2026-10-26)
"""

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize, minimize_scalar

MAX_GOLES = 10
N_FIJOS = 4                                           # μ, H, βa, βd


@dataclass
class DixonColes:
    vida_media_dias: float = 365.0
    reg: float = 2.0
    usa_coef: bool = True                             # False = v1 (ignora x_local / x_visita)
    equipos: dict = field(default_factory=dict)      # nombre -> índice
    params: np.ndarray | None = None                  # [μ, H, βa, βd, δa..., δd...]
    rho: float = 0.0

    # ------------------------------------------------------------------
    def _indices(self, partidos):
        for p in partidos:
            for e in (p["local"], p["visita"]):
                if e not in self.equipos:
                    self.equipos[e] = len(self.equipos)
        il = np.array([self.equipos[p["local"]] for p in partidos])
        iv = np.array([self.equipos[p["visita"]] for p in partidos])
        return il, iv

    def _tasas(self, params, il, iv, local_real, xl, xv):
        n = len(self.equipos)
        mu, h, ba, bd = params[:N_FIJOS]
        ata, dfn = params[N_FIJOS:N_FIJOS + n], params[N_FIJOS + n:N_FIJOS + 2 * n]
        eta1 = mu + ba * xl + ata[il] - bd * xv - dfn[iv] + h * local_real
        eta2 = mu + ba * xv + ata[iv] - bd * xl - dfn[il]
        return np.exp(eta1), np.exp(eta2)

    def ajustar(self, partidos, fecha_ref):
        """partidos: dicts con local, visita, goles_local_90, goles_visita_90, es_neutral, fecha (date)
        y opcionalmente x_local / x_visita (log coeficiente centrado; 0 si falta)."""
        il, iv = self._indices(partidos)
        n = len(self.equipos)
        x = np.array([p["goles_local_90"] for p in partidos], dtype=float)
        y = np.array([p["goles_visita_90"] for p in partidos], dtype=float)
        local_real = np.array([0.0 if p["es_neutral"] else 1.0 for p in partidos])
        xl = np.array([p.get("x_local", 0.0) if self.usa_coef else 0.0 for p in partidos])
        xv = np.array([p.get("x_visita", 0.0) if self.usa_coef else 0.0 for p in partidos])
        dias = np.array([(fecha_ref - p["fecha"]).days for p in partidos], dtype=float)
        w = np.exp(-np.log(2) * dias / self.vida_media_dias)

        def objetivo(theta):
            l1, l2 = self._tasas(theta, il, iv, local_real, xl, xv)
            nll = np.sum(w * (l1 - x * np.log(l1) + l2 - y * np.log(l2)))
            ata, dfn = theta[N_FIJOS:N_FIJOS + n], theta[N_FIJOS + n:]
            nll += self.reg * (ata @ ata + dfn @ dfn)
            r1, r2 = w * (l1 - x), w * (l2 - y)          # d nll / d eta
            g = np.zeros_like(theta)
            g[0] = r1.sum() + r2.sum()
            g[1] = (r1 * local_real).sum()
            g[2] = (r1 * xl + r2 * xv).sum()
            g[3] = -(r1 * xv + r2 * xl).sum()
            ga = np.zeros(n)
            gd = np.zeros(n)
            np.add.at(ga, il, r1)
            np.add.at(ga, iv, r2)
            np.add.at(gd, iv, -r1)
            np.add.at(gd, il, -r2)
            g[N_FIJOS:N_FIJOS + n] = ga + 2 * self.reg * ata
            g[N_FIJOS + n:] = gd + 2 * self.reg * dfn
            return nll, g

        inicio = np.zeros(N_FIJOS + 2 * n)
        if self.params is not None:                     # arranque en caliente
            k = (len(self.params) - N_FIJOS) // 2
            inicio[:N_FIJOS] = self.params[:N_FIJOS]
            inicio[N_FIJOS:N_FIJOS + k] = self.params[N_FIJOS:N_FIJOS + k]
            inicio[N_FIJOS + n:N_FIJOS + n + k] = self.params[N_FIJOS + k:]
        else:
            inicio[0] = np.log(max((x.mean() + y.mean()) / 2, 0.1))
        res = minimize(objetivo, inicio, jac=True, method="L-BFGS-B")
        self.params = res.x

        # Segunda etapa: ρ con λ, γ fijos (solo afecta 0-0, 1-0, 0-1, 1-1)
        l1, l2 = self._tasas(self.params, il, iv, local_real, xl, xv)
        bajos = (x <= 1) & (y <= 1)
        xb, yb, l1b, l2b, wb = x[bajos], y[bajos], l1[bajos], l2[bajos], w[bajos]
        tope = min(1 / l1b.max() / l2b.max(), 1.0) * 0.99 if len(l1b) else 0.2

        def nll_rho(rho):
            tau = np.where((xb == 0) & (yb == 0), 1 - l1b * l2b * rho,
                  np.where((xb == 0) & (yb == 1), 1 + l1b * rho,
                  np.where((xb == 1) & (yb == 0), 1 + l2b * rho, 1 - rho)))
            return -np.sum(wb * np.log(np.maximum(tau, 1e-9)))

        self.rho = minimize_scalar(nll_rho, bounds=(-tope, tope), method="bounded").x
        return self

    # ------------------------------------------------------------------
    def tasas(self, local, visita, es_neutral, x_local=0.0, x_visita=0.0):
        n = len(self.equipos)
        mu, h, ba, bd = self.params[:N_FIJOS]
        if not self.usa_coef:
            x_local = x_visita = 0.0
        ata, dfn = self.params[N_FIJOS:N_FIJOS + n], self.params[N_FIJOS + n:]
        a = lambda e, xe: ba * xe + (ata[self.equipos[e]] if e in self.equipos else 0.0)
        d = lambda e, xe: bd * xe + (dfn[self.equipos[e]] if e in self.equipos else 0.0)
        l1 = np.exp(mu + a(local, x_local) - d(visita, x_visita) + (0.0 if es_neutral else h))
        l2 = np.exp(mu + a(visita, x_visita) - d(local, x_local))
        return l1, l2

    def matriz(self, local, visita, es_neutral, x_local=0.0, x_visita=0.0):
        """P(goles local = i, goles visita = j), i, j = 0..MAX_GOLES, con corrección τ."""
        l1, l2 = self.tasas(local, visita, es_neutral, x_local, x_visita)
        k = np.arange(MAX_GOLES + 1)
        fact = np.cumprod(np.r_[1, np.arange(1, MAX_GOLES + 1)])
        p1 = np.exp(-l1) * l1 ** k / fact
        p2 = np.exp(-l2) * l2 ** k / fact
        m = np.outer(p1, p2)
        r = self.rho
        m[0, 0] *= 1 - l1 * l2 * r
        m[0, 1] *= 1 + l1 * r
        m[1, 0] *= 1 + l2 * r
        m[1, 1] *= 1 - r
        return m / m.sum(), l1, l2

    def predecir(self, local, visita, es_neutral, x_local=0.0, x_visita=0.0):
        m, l1, l2 = self.matriz(local, visita, es_neutral, x_local, x_visita)
        i, j = np.indices(m.shape)
        return {
            "prob_local": float(m[i > j].sum()),
            "prob_empate": float(np.trace(m)),
            "prob_visita": float(m[i < j].sum()),
            "goles_esp_local": float(l1),
            "goles_esp_visita": float(l2),
            "prob_over_2_5": float(m[i + j > 2.5].sum()),
        }

    @property
    def ventaja_local(self):
        return float(np.exp(self.params[1]))

    @property
    def betas(self):
        """(βa, βd): efecto del log coeficiente en ataque y defensa."""
        return float(self.params[2]), float(self.params[3])
