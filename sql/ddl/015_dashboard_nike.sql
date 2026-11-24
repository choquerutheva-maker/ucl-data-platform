/* =============================================================================
   UCL Data Platform - Pronósticos de Niké en el dashboard
   Descripción : gold.vw_dash_nike expone un pronóstico de Niké por partido:
                 el último hecho ANTES del inicio (sin filas tardías). Tiene las
                 mismas columnas base que gold.vw_dash_pronosticos (modelo =
                 'Niké') para que el dashboard la muestre junto a los modelos,
                 más los campos propios de Niké (ambos marcan, confianza, tipo
                 de once, cantidad de revisiones).
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-11-24
   Notas       :
     - Lee silver.nike_pronosticos y silver.espn_cuotas, que son privadas:
       la vista corre con permisos del dueño (sin security_invoker), igual que
       vw_dash_mercado_en_vivo. No expone cuotas, once ni comentarios.
     - silver.nike_pronosticos no tiene equipos: el partido se identifica por
       el espn_event_id que aparece en silver.espn_cuotas o
       silver.espn_prob_externa (ids de equipo ESPN -> d_equipo_alias -> f_partido),
       con el mismo cruce de fecha (± 1 día) que vw_pronostico_vs_mercado.
     - Partido sin cuotas capturadas = no aparece (hoy todos las tienen).
============================================================================= */

BEGIN;

CREATE VIEW gold.vw_dash_nike AS
WITH ultimo AS (
    -- último pronóstico de Niké antes del inicio, por partido
    SELECT DISTINCT ON (n.espn_event_id)
           n.espn_event_id, n.pronosticado_en, n.version_metodo,
           n.prob_local, n.prob_empate, n.prob_visita,
           n.goles_esp_local, n.goles_esp_visita, n.prob_over_2_5, n.prob_btts,
           n.confianza, n.tipo_once,
           COUNT(*) OVER (PARTITION BY n.espn_event_id) AS n_pronosticos
    FROM silver.nike_pronosticos n
    WHERE NOT n.es_tardio
      AND (n.inicio_utc IS NULL OR n.pronosticado_en < n.inicio_utc)
    ORDER BY n.espn_event_id, n.pronosticado_en DESC
),
evento AS (
    -- equipos ESPN de cada evento
    SELECT DISTINCT ON (espn_event_id) espn_event_id, inicio_utc, local_espn_id, visita_espn_id
    FROM (SELECT espn_event_id, inicio_utc, local_espn_id, visita_espn_id FROM silver.espn_cuotas
          UNION ALL
          SELECT espn_event_id, inicio_utc, local_espn_id, visita_espn_id FROM silver.espn_prob_externa) x
    ORDER BY espn_event_id, inicio_utc DESC
),
alias_espn AS (
    SELECT DISTINCT id_externo, id_equipo
    FROM gold.d_equipo_alias
    WHERE id_fuente = 'espn' AND id_externo IS NOT NULL
),
partido AS (
    SELECT u.*, p.id_partido
    FROM ultimo u
    JOIN evento e      ON e.espn_event_id = u.espn_event_id
    JOIN alias_espn al ON al.id_externo   = e.local_espn_id
    JOIN alias_espn av ON av.id_externo   = e.visita_espn_id
    JOIN gold.f_partido p ON p.id_equipo_local  = al.id_equipo
                         AND p.id_equipo_visita = av.id_equipo
                         AND ABS(p.fecha - (e.inicio_utc AT TIME ZONE 'UTC')::DATE) <= 1
)
SELECT p.id_partido,
       p.id_temporada,
       t.etiqueta                         AS temporada,
       r.nombre                           AS ronda,
       r.orden                            AS orden_ronda,
       p.jornada,
       p.fecha,
       p.inicio_utc,
       el.nombre_canonico                 AS local,
       ev.nombre_canonico                 AS visita,
       'Niké'::TEXT                       AS modelo,
       n.version_metodo,
       n.prob_local,
       n.prob_empate,
       n.prob_visita,
       n.goles_esp_local,
       n.goles_esp_visita,
       n.prob_over_2_5,
       n.prob_btts,
       CASE WHEN n.prob_local >= n.prob_empate AND n.prob_local >= n.prob_visita THEN 'L'
            WHEN n.prob_visita >= n.prob_empate THEN 'V'
            ELSE 'E' END                  AS pick,
       n.confianza,
       n.tipo_once,
       n.pronosticado_en,
       n.n_pronosticos,
       p.estado,
       p.goles_local_90,
       p.goles_visita_90,
       p.resultado_90,
       CASE WHEN p.estado = 'JUGADO' THEN
           (CASE WHEN n.prob_local >= n.prob_empate AND n.prob_local >= n.prob_visita THEN 'L'
                 WHEN n.prob_visita >= n.prob_empate THEN 'V'
                 ELSE 'E' END) = p.resultado_90 END AS acierto,
       CASE WHEN p.estado = 'JUGADO' THEN
           -LN(GREATEST(CASE p.resultado_90 WHEN 'L' THEN n.prob_local
                                            WHEN 'E' THEN n.prob_empate
                                            ELSE n.prob_visita END, 0.0001)) END AS log_loss,
       ql.codigo                          AS codigo_local,
       ql.escudo_url                      AS escudo_local,
       qv.codigo                          AS codigo_visita,
       qv.escudo_url                      AS escudo_visita
FROM partido n
JOIN gold.f_partido   p  ON p.id_partido   = n.id_partido
JOIN gold.d_temporada t  ON t.id_temporada = p.id_temporada
JOIN gold.d_ronda     r  ON r.id_ronda     = p.id_ronda
JOIN gold.d_equipo    el ON el.id_equipo   = p.id_equipo_local
JOIN gold.d_equipo    ev ON ev.id_equipo   = p.id_equipo_visita
LEFT JOIN gold.vw_dash_equipos ql ON ql.id_equipo = p.id_equipo_local
LEFT JOIN gold.vw_dash_equipos qv ON qv.id_equipo = p.id_equipo_visita;

GRANT SELECT ON gold.vw_dash_nike TO anon, authenticated;

COMMIT;
