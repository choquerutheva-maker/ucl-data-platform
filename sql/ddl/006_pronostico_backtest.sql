/* =============================================================================
   UCL Data Platform - Pronósticos de backtest vs en vivo
   Descripción : gold.f_pronostico guarda dos tipos de pronóstico:
                   - es_backtest = TRUE : reconstruido después, entrenando solo
                     con partidos anteriores a la fecha (walk-forward).
                   - es_backtest = FALSE: emitido ANTES del partido (en vivo);
                     el loader nunca lo pisa después del inicio.
                 datos_hasta = último día de partidos usado para entrenar.
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-10-22
============================================================================= */

BEGIN;

ALTER TABLE gold.f_pronostico
    ADD COLUMN es_backtest  BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN datos_hasta  DATE;

DROP VIEW gold.vw_evaluacion_pronosticos;

CREATE VIEW gold.vw_evaluacion_pronosticos WITH (security_invoker = true) AS
SELECT pr.id_pronostico,
       pr.id_modelo,
       m.nombre AS modelo,
       m.version,
       pr.es_backtest,
       p.id_partido,
       p.id_temporada,
       p.fecha,
       pr.pick,
       p.resultado_90,
       (pr.pick = p.resultado_90)                                   AS acierto,
       -- Brier multiclase (0 = perfecto, 2 = peor)
       POWER(pr.prob_local  - (p.resultado_90 = 'L')::INT, 2)
     + POWER(pr.prob_empate - (p.resultado_90 = 'E')::INT, 2)
     + POWER(pr.prob_visita - (p.resultado_90 = 'V')::INT, 2)    AS brier,
       -LN(GREATEST(CASE p.resultado_90 WHEN 'L' THEN pr.prob_local
                                        WHEN 'E' THEN pr.prob_empate
                                        ELSE pr.prob_visita END, 0.0001)) AS log_loss
FROM gold.f_pronostico pr
JOIN gold.f_partido p ON p.id_partido = pr.id_partido
JOIN gold.d_modelo  m ON m.id_modelo  = pr.id_modelo
WHERE p.estado = 'JUGADO';

GRANT SELECT ON gold.vw_evaluacion_pronosticos TO anon, authenticated;

COMMIT;
