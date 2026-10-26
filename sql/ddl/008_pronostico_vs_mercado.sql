/* =============================================================================
   UCL Data Platform - Pronósticos en vivo frente al mercado
   Descripción : gold.vw_pronostico_vs_mercado pone cada pronóstico EN VIVO
                 (es_backtest = FALSE; Dixon-Coles v2 y Elo UCL desde la J4
                 2026-27) junto a la probabilidad implícita de DraftKings
                 (casa_id 100) sin margen, método proporcional.
                 Línea del mercado: CIERRE si existe; si no, la última ACTUAL
                 capturada antes del inicio.
                 Con el partido jugado agrega el log loss de ambos.
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-10-26
   Notas       :
     - Privada, como silver.espn_cuotas (ESPN no permite republicar cuotas):
       sin GRANT a anon / authenticated.
============================================================================= */

BEGIN;

CREATE VIEW gold.vw_pronostico_vs_mercado WITH (security_invoker = true) AS
WITH linea AS (
    SELECT DISTINCT ON (c.espn_event_id)
           c.espn_event_id, c.inicio_utc, c.local_espn_id, c.visita_espn_id,
           c.tipo_linea, c.capturado_en,
           1 / c.cuota_local AS q_local, 1 / c.cuota_empate AS q_empate, 1 / c.cuota_visita AS q_visita
    FROM silver.espn_cuotas c
    WHERE c.casa_id = '100'
      AND (c.tipo_linea = 'CIERRE' OR (c.tipo_linea = 'ACTUAL' AND c.capturado_en < c.inicio_utc))
    ORDER BY c.espn_event_id, (c.tipo_linea = 'CIERRE') DESC, c.capturado_en DESC
),
alias_espn AS (
    SELECT DISTINCT id_externo, id_equipo
    FROM gold.d_equipo_alias
    WHERE id_fuente = 'espn' AND id_externo IS NOT NULL
),
mercado AS (
    SELECT p.id_partido, l.tipo_linea, l.capturado_en,
           ROUND(l.q_local  / (l.q_local + l.q_empate + l.q_visita), 4) AS prob_local,
           ROUND(l.q_empate / (l.q_local + l.q_empate + l.q_visita), 4) AS prob_empate,
           ROUND(l.q_visita / (l.q_local + l.q_empate + l.q_visita), 4) AS prob_visita,
           ROUND((l.q_local + l.q_empate + l.q_visita - 1) * 100, 2)    AS margen_pct
    FROM linea l
    JOIN alias_espn al ON al.id_externo = l.local_espn_id
    JOIN alias_espn av ON av.id_externo = l.visita_espn_id
    JOIN gold.f_partido p ON p.id_equipo_local  = al.id_equipo
                         AND p.id_equipo_visita = av.id_equipo
                         AND ABS(p.fecha - (l.inicio_utc AT TIME ZONE 'UTC')::DATE) <= 1
)
SELECT p.id_partido,
       p.id_temporada,
       p.jornada,
       p.fecha,
       p.inicio_utc,
       el.nombre_canonico                AS local,
       ev.nombre_canonico                AS visita,
       m.nombre                          AS modelo,
       m.version,
       pr.generado_en,
       pr.datos_hasta,
       pr.prob_local,
       pr.prob_empate,
       pr.prob_visita,
       mk.prob_local                     AS mercado_local,
       mk.prob_empate                    AS mercado_empate,
       mk.prob_visita                    AS mercado_visita,
       mk.tipo_linea                     AS linea_mercado,
       mk.capturado_en                   AS mercado_capturado_en,
       mk.margen_pct                     AS mercado_margen_pct,
       GREATEST(ABS(pr.prob_local  - mk.prob_local),
                ABS(pr.prob_empate - mk.prob_empate),
                ABS(pr.prob_visita - mk.prob_visita))           AS dif_max_mercado,
       p.estado,
       p.goles_local_90,
       p.goles_visita_90,
       p.resultado_90,
       -LN(GREATEST(CASE p.resultado_90 WHEN 'L' THEN pr.prob_local
                                        WHEN 'E' THEN pr.prob_empate
                                        WHEN 'V' THEN pr.prob_visita END, 0.0001)) AS log_loss_modelo,
       -LN(GREATEST(CASE p.resultado_90 WHEN 'L' THEN mk.prob_local
                                        WHEN 'E' THEN mk.prob_empate
                                        WHEN 'V' THEN mk.prob_visita END, 0.0001)) AS log_loss_mercado
FROM gold.f_pronostico pr
JOIN gold.d_modelo  m  ON m.id_modelo  = pr.id_modelo
JOIN gold.f_partido p  ON p.id_partido = pr.id_partido
JOIN gold.d_equipo  el ON el.id_equipo = p.id_equipo_local
JOIN gold.d_equipo  ev ON ev.id_equipo = p.id_equipo_visita
LEFT JOIN mercado   mk ON mk.id_partido = p.id_partido
WHERE NOT pr.es_backtest;

REVOKE ALL ON gold.vw_pronostico_vs_mercado FROM anon, authenticated;

COMMIT;
