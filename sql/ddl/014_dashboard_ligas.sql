/* =============================================================================
   UCL Data Platform - Vistas del dashboard para las ligas nacionales
   Descripción : Pestaña "Ligas nacionales" del dashboard (docs/ y docs/en/).
                 - vw_dash_ligas          : una fila por liga con su temporada
                                            actual y agregados (sin cuotas).
                 - vw_dash_ligas_partidos : partidos de liga (jugados y
                                            programados) con nombres canónicos,
                                            goles a los 90' y xG.
   Autor       : Ruth Choque (con SEMANTHEUS)
   Fecha       : 2026-11-01
   Notas       :
     - Solo datos de gold.f_partido_liga y gold.d_liga (lectura pública, DDL 012).
       Las cuotas siguen en silver y NO se exponen.
     - La tabla de posiciones se calcula en el navegador (puntos, DG, GF);
       el dashboard avisa que no aplica desempates por enfrentamiento directo.
     - Sin pronósticos de liga: los modelos solo publican la Champions.
============================================================================= */

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Partidos de liga
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW gold.vw_dash_ligas_partidos WITH (security_invoker = true) AS
SELECT pl.id_partido_liga,
       pl.id_liga,
       pl.temporada,
       pl.fecha,
       pl.inicio_utc,
       pl.estado,
       el.nombre_canonico   AS local,
       ev.nombre_canonico   AS visita,
       pl.goles_local_90    AS goles_local,
       pl.goles_visita_90   AS goles_visita,
       pl.resultado_90      AS resultado,
       pl.xg_local,
       pl.xg_visita
FROM gold.f_partido_liga pl
JOIN gold.d_equipo el ON el.id_equipo = pl.id_equipo_local
JOIN gold.d_equipo ev ON ev.id_equipo = pl.id_equipo_visita;

-- -----------------------------------------------------------------------------
-- 2. Resumen por liga (temporada actual = la más reciente cargada)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW gold.vw_dash_ligas WITH (security_invoker = true) AS
WITH actual AS (
    SELECT id_liga, MAX(temporada) AS temporada
    FROM gold.f_partido_liga
    GROUP BY id_liga
)
SELECT l.id_liga,
       l.nombre                                                   AS liga,
       l.pais_iso3                                                AS pais,
       l.orden,
       a.temporada,
       a.temporada || '/' || LPAD(((a.temporada + 1) % 100)::TEXT, 2, '0') AS etiqueta_temporada,
       COUNT(*) FILTER (WHERE pl.estado = 'JUGADO')               AS partidos_jugados,
       COUNT(*) FILTER (WHERE pl.estado = 'PROGRAMADO')           AS partidos_programados,
       MAX(pl.fecha) FILTER (WHERE pl.estado = 'JUGADO')          AS ultima_fecha_jugada,
       MIN(pl.fecha) FILTER (WHERE pl.estado = 'PROGRAMADO')      AS proxima_fecha,
       ROUND(AVG(pl.goles_total_90) FILTER (WHERE pl.estado = 'JUGADO'), 2)              AS goles_por_partido,
       ROUND(AVG((pl.resultado_90 = 'L')::INT) FILTER (WHERE pl.estado = 'JUGADO'), 4)   AS pct_local,
       ROUND(AVG((pl.resultado_90 = 'E')::INT) FILTER (WHERE pl.estado = 'JUGADO'), 4)   AS pct_empate,
       ROUND(AVG((pl.resultado_90 = 'V')::INT) FILTER (WHERE pl.estado = 'JUGADO'), 4)   AS pct_visita
FROM gold.d_liga l
JOIN actual a            ON a.id_liga = l.id_liga
JOIN gold.f_partido_liga pl ON pl.id_liga = l.id_liga AND pl.temporada = a.temporada
GROUP BY l.id_liga, l.nombre, l.pais_iso3, l.orden, a.temporada;

GRANT SELECT ON gold.vw_dash_ligas, gold.vw_dash_ligas_partidos TO anon, authenticated;

COMMIT;
