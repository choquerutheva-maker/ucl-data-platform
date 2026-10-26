/* =============================================================================
   UCL Data Platform - Vistas públicas para el dashboard
   Descripción : Contrato de datos del dashboard (GitHub Pages + supabase-js con
                 la clave anon). Todo en gold, solo lectura:
                   - vw_dash_pronosticos       : pronósticos en vivo por partido y modelo
                   - vw_dash_tabla             : tabla de posiciones por jornada
                   - vw_dash_desempeno_modelos : métricas por modelo, tipo y temporada
                   - vw_dash_mercado_en_vivo   : modelo vs mercado, SOLO agregados
                   - vw_dash_temporadas        : resumen histórico por temporada
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-10-26
   Notas       :
     - vw_dash_mercado_en_vivo y vw_pronostico_vs_mercado corren con los
       permisos del dueño (sin security_invoker) porque leen
       silver.espn_cuotas, que es privada. La primera expone solo
       promedios por jornada con al menos 5 partidos: ninguna cuota ni
       probabilidad del mercado por partido (ESPN no permite republicarlas).
============================================================================= */

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Pronósticos en vivo (emitidos antes del partido), con resultado si se jugó
-- -----------------------------------------------------------------------------
CREATE VIEW gold.vw_dash_pronosticos WITH (security_invoker = true) AS
SELECT p.id_partido,
       p.id_temporada,
       t.etiqueta                         AS temporada,
       r.nombre                           AS ronda,
       r.orden                            AS orden_ronda,
       p.jornada,
       p.fecha,
       p.inicio_utc,
       el.nombre_canonico                 AS local,
       el.pais_iso3                       AS pais_local,
       ev.nombre_canonico                 AS visita,
       ev.pais_iso3                       AS pais_visita,
       m.nombre || ' ' || m.version       AS modelo,
       pr.prob_local,
       pr.prob_empate,
       pr.prob_visita,
       pr.goles_esp_local,
       pr.goles_esp_visita,
       pr.prob_over_2_5,
       pr.pick,
       pr.datos_hasta,
       pr.generado_en,
       p.estado,
       p.goles_local_90,
       p.goles_visita_90,
       p.resultado_90,
       CASE WHEN p.estado = 'JUGADO' THEN pr.pick = p.resultado_90 END AS acierto,
       CASE WHEN p.estado = 'JUGADO' THEN
           -LN(GREATEST(CASE p.resultado_90 WHEN 'L' THEN pr.prob_local
                                            WHEN 'E' THEN pr.prob_empate
                                            ELSE pr.prob_visita END, 0.0001)) END AS log_loss
FROM gold.f_pronostico pr
JOIN gold.d_modelo    m  ON m.id_modelo     = pr.id_modelo
JOIN gold.f_partido   p  ON p.id_partido    = pr.id_partido
JOIN gold.d_temporada t  ON t.id_temporada  = p.id_temporada
JOIN gold.d_ronda     r  ON r.id_ronda      = p.id_ronda
JOIN gold.d_equipo    el ON el.id_equipo    = p.id_equipo_local
JOIN gold.d_equipo    ev ON ev.id_equipo    = p.id_equipo_visita
WHERE NOT pr.es_backtest;

-- -----------------------------------------------------------------------------
-- 2. Tabla de posiciones, todas las jornadas (el dashboard elige cuál mostrar)
-- -----------------------------------------------------------------------------
CREATE VIEW gold.vw_dash_tabla WITH (security_invoker = true) AS
SELECT tp.id_temporada,
       t.etiqueta                         AS temporada,
       t.formato,
       tp.jornada,
       tp.jornada = MAX(tp.jornada) OVER (PARTITION BY tp.id_temporada) AS es_ultima_jornada,
       tp.fecha_corte,
       tp.grupo,
       COALESCE(tp.posicion_final, tp.posicion_provisional) AS posicion,
       (tp.posicion_final IS NOT NULL)    AS es_posicion_final,
       e.nombre_canonico                  AS equipo,
       e.pais_iso3                        AS pais,
       tp.pj, tp.pg, tp.pe, tp.pp, tp.gf, tp.gc, tp.dg, tp.puntos,
       tp.franja,
       tp.desempate_aproximado
FROM gold.f_tabla_posiciones tp
JOIN gold.d_temporada t ON t.id_temporada = tp.id_temporada
JOIN gold.d_equipo    e ON e.id_equipo    = tp.id_equipo;

-- -----------------------------------------------------------------------------
-- 3. Desempeño de los modelos (backtest y en vivo) por temporada
-- -----------------------------------------------------------------------------
CREATE VIEW gold.vw_dash_desempeno_modelos WITH (security_invoker = true) AS
SELECT e.modelo || ' ' || e.version            AS modelo,
       CASE WHEN e.es_backtest THEN 'BACKTEST' ELSE 'EN_VIVO' END AS tipo,
       e.id_temporada,
       t.etiqueta                               AS temporada,
       COUNT(*)                                 AS partidos,
       ROUND(AVG(e.log_loss), 4)                AS log_loss,
       ROUND(AVG(e.brier), 4)                   AS brier,
       ROUND(AVG(e.acierto::INT), 4)            AS acierto
FROM gold.vw_evaluacion_pronosticos e
JOIN gold.d_temporada t ON t.id_temporada = e.id_temporada
GROUP BY e.modelo, e.version, e.es_backtest, e.id_temporada, t.etiqueta;

-- -----------------------------------------------------------------------------
-- 4. Modelo vs mercado en vivo: solo agregados por jornada (>= 5 partidos)
-- -----------------------------------------------------------------------------
-- vw_pronostico_vs_mercado pasa a correr con los permisos del dueño para que
-- esta vista la pueda leer; sigue cerrada a anon / authenticated (REVOKE en 008).
ALTER VIEW gold.vw_pronostico_vs_mercado SET (security_invoker = false);

CREATE VIEW gold.vw_dash_mercado_en_vivo AS
SELECT v.id_temporada,
       t.etiqueta                                   AS temporada,
       v.jornada,
       v.modelo || ' ' || v.version                 AS modelo,
       COUNT(*)                                     AS partidos,
       ROUND(AVG(v.log_loss_modelo), 4)             AS log_loss_modelo,
       ROUND(AVG(v.log_loss_mercado), 4)            AS log_loss_mercado,
       ROUND(AVG(v.log_loss_modelo - v.log_loss_mercado), 4) AS diferencia,   -- negativo = el modelo gana
       ROUND(AVG(v.dif_max_mercado), 4)             AS dif_max_media
FROM gold.vw_pronostico_vs_mercado v
JOIN gold.d_temporada t ON t.id_temporada = v.id_temporada
WHERE v.estado = 'JUGADO'
  AND v.log_loss_mercado IS NOT NULL
GROUP BY v.id_temporada, t.etiqueta, v.jornada, v.modelo, v.version
HAVING COUNT(*) >= 5;

-- -----------------------------------------------------------------------------
-- 5. Resumen histórico por temporada (sin rondas previas)
-- -----------------------------------------------------------------------------
CREATE VIEW gold.vw_dash_temporadas WITH (security_invoker = true) AS
WITH jugados AS (
    SELECT p.*
    FROM gold.f_partido p
    JOIN gold.d_ronda r ON r.id_ronda = p.id_ronda
    WHERE p.estado = 'JUGADO' AND NOT r.es_previa
),
resumen AS (
    SELECT id_temporada,
           COUNT(*)                                       AS partidos,
           ROUND(AVG((resultado_90 = 'L')::INT), 4)       AS pct_local,
           ROUND(AVG((resultado_90 = 'E')::INT), 4)       AS pct_empate,
           ROUND(AVG((resultado_90 = 'V')::INT), 4)       AS pct_visita,
           ROUND(AVG(goles_total_90), 2)                  AS goles_por_partido
    FROM jugados
    GROUP BY id_temporada
),
final AS (
    SELECT p.id_temporada,
           c.nombre_canonico                              AS campeon,
           CASE WHEN p.id_equipo_avanza = p.id_equipo_local THEN v.nombre_canonico
                ELSE l.nombre_canonico END                AS finalista,
           p.metodo_definicion                            AS final_definida_por
    FROM jugados p
    JOIN gold.d_equipo l ON l.id_equipo = p.id_equipo_local
    JOIN gold.d_equipo v ON v.id_equipo = p.id_equipo_visita
    LEFT JOIN gold.d_equipo c ON c.id_equipo = p.id_equipo_avanza
    WHERE p.id_ronda = 60
)
SELECT t.id_temporada,
       t.etiqueta          AS temporada,
       t.formato,
       t.equipos_fase_inicial,
       r.partidos,
       r.pct_local,
       r.pct_empate,
       r.pct_visita,
       r.goles_por_partido,
       f.campeon,
       f.finalista,
       f.final_definida_por
FROM gold.d_temporada t
JOIN resumen r ON r.id_temporada = t.id_temporada
LEFT JOIN final f ON f.id_temporada = t.id_temporada;

GRANT SELECT ON gold.vw_dash_pronosticos, gold.vw_dash_tabla, gold.vw_dash_desempeno_modelos,
                gold.vw_dash_mercado_en_vivo, gold.vw_dash_temporadas
    TO anon, authenticated;

COMMIT;
