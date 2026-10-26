/* =============================================================================
   UCL Data Platform - Escudos, historial y posición en tabla para el dashboard
   Descripción : - gold.d_equipo.codigo (teamCode de UEFA, p. ej. BAY); nombre_corto
                   se llena con displayNameShort. Los carga
                   src/ingesta/uefa_coeficientes.py.
                 - vw_dash_equipos  : identidad visual de cada club; el escudo se
                   sirve desde img.uefa.com (no se copia al repo).
                 - vw_dash_partidos : todos los partidos jugados de la Champions
                   (sin rondas previas), para el popup de enfrentamientos previos.
                 - vw_dash_pronosticos: suma código, escudo, posición y puntos de
                   cada equipo en la tabla vigente ANTES del partido.
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-10-26
============================================================================= */

BEGIN;

ALTER TABLE gold.d_equipo ADD COLUMN codigo TEXT;

-- -----------------------------------------------------------------------------
-- 1. Equipos: nombre, código y escudo
-- -----------------------------------------------------------------------------
CREATE VIEW gold.vw_dash_equipos WITH (security_invoker = true) AS
SELECT e.id_equipo,
       e.nombre_canonico                AS equipo,
       COALESCE(e.nombre_corto, e.nombre_canonico) AS nombre_corto,
       e.codigo,
       e.pais_iso3                      AS pais,
       a.id_externo                     AS uefa_id,
       'https://img.uefa.com/imgml/TP/teams/logos/70x70/'   || a.id_externo || '.png' AS escudo_url,
       'https://img.uefa.com/imgml/TP/teams/logos/240x240/' || a.id_externo || '.png' AS escudo_url_grande
FROM gold.d_equipo e
LEFT JOIN (SELECT DISTINCT ON (id_equipo) id_equipo, id_externo
           FROM gold.d_equipo_alias
           WHERE id_fuente = 'uefa' AND id_externo IS NOT NULL
           ORDER BY id_equipo, alias) a ON a.id_equipo = e.id_equipo;

-- -----------------------------------------------------------------------------
-- 2. Partidos jugados (historial para el cara a cara)
-- -----------------------------------------------------------------------------
CREATE VIEW gold.vw_dash_partidos WITH (security_invoker = true) AS
SELECT p.id_partido,
       p.id_temporada,
       t.etiqueta                       AS temporada,
       r.nombre                         AS ronda,
       r.orden                          AS orden_ronda,
       p.jornada,
       p.grupo,
       p.fecha,
       el.nombre_canonico               AS local,
       el.codigo                        AS codigo_local,
       ev.nombre_canonico               AS visita,
       ev.codigo                        AS codigo_visita,
       p.es_neutral,
       p.goles_local_90,
       p.goles_visita_90,
       p.goles_local_prorroga,
       p.goles_visita_prorroga,
       p.penales_local,
       p.penales_visita,
       p.metodo_definicion,
       p.resultado_90,
       ea.nombre_canonico               AS avanza
FROM gold.f_partido p
JOIN gold.d_temporada t  ON t.id_temporada = p.id_temporada
JOIN gold.d_ronda     r  ON r.id_ronda     = p.id_ronda
JOIN gold.d_equipo    el ON el.id_equipo   = p.id_equipo_local
JOIN gold.d_equipo    ev ON ev.id_equipo   = p.id_equipo_visita
LEFT JOIN gold.d_equipo ea ON ea.id_equipo = p.id_equipo_avanza
WHERE p.estado = 'JUGADO' AND NOT r.es_previa;

-- -----------------------------------------------------------------------------
-- 3. Pronósticos: + código, escudo, posición y puntos antes del partido
--    (columnas nuevas al final para poder usar CREATE OR REPLACE)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW gold.vw_dash_pronosticos WITH (security_invoker = true) AS
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
                                            ELSE pr.prob_visita END, 0.0001)) END AS log_loss,
       -- nuevas (DDL 011)
       ql.nombre_corto                    AS nombre_corto_local,
       ql.codigo                          AS codigo_local,
       ql.escudo_url                      AS escudo_local,
       qv.nombre_corto                    AS nombre_corto_visita,
       qv.codigo                          AS codigo_visita,
       qv.escudo_url                      AS escudo_visita,
       tl.posicion                        AS posicion_local,
       tl.puntos                          AS puntos_local,
       tv.posicion                        AS posicion_visita,
       tv.puntos                          AS puntos_visita,
       tl.jornada                         AS jornada_tabla
FROM gold.f_pronostico pr
JOIN gold.d_modelo    m  ON m.id_modelo     = pr.id_modelo
JOIN gold.f_partido   p  ON p.id_partido    = pr.id_partido
JOIN gold.d_temporada t  ON t.id_temporada  = p.id_temporada
JOIN gold.d_ronda     r  ON r.id_ronda      = p.id_ronda
JOIN gold.d_equipo    el ON el.id_equipo    = p.id_equipo_local
JOIN gold.d_equipo    ev ON ev.id_equipo    = p.id_equipo_visita
LEFT JOIN gold.vw_dash_equipos ql ON ql.id_equipo = p.id_equipo_local
LEFT JOIN gold.vw_dash_equipos qv ON qv.id_equipo = p.id_equipo_visita
-- tabla vigente antes del partido: última jornada cerrada antes de su fecha
LEFT JOIN LATERAL (
    SELECT COALESCE(tp.posicion_final, tp.posicion_provisional) AS posicion, tp.puntos, tp.jornada
    FROM gold.f_tabla_posiciones tp
    WHERE tp.id_temporada = p.id_temporada AND tp.id_equipo = p.id_equipo_local AND tp.fecha_corte < p.fecha
    ORDER BY tp.jornada DESC LIMIT 1) tl ON TRUE
LEFT JOIN LATERAL (
    SELECT COALESCE(tp.posicion_final, tp.posicion_provisional) AS posicion, tp.puntos
    FROM gold.f_tabla_posiciones tp
    WHERE tp.id_temporada = p.id_temporada AND tp.id_equipo = p.id_equipo_visita AND tp.fecha_corte < p.fecha
    ORDER BY tp.jornada DESC LIMIT 1) tv ON TRUE
WHERE NOT pr.es_backtest;

GRANT SELECT ON gold.vw_dash_equipos, gold.vw_dash_partidos TO anon, authenticated;

COMMIT;
