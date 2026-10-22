/* =============================================================================
   UCL Data Platform - Reglas de desempate y ranking por jornada
   Descripción : Carga los criterios de desempate por formato (Niké, msg #31),
                 separa ranking provisional / final en f_tabla_posiciones y
                 agrega el coeficiente UEFA por temporada (último desempate).
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-10-21
   Pendiente   : cotejar cada versión con el PDF "Regulations of the UEFA
                 Champions League" de la temporada (art. Equality of points).
============================================================================= */

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Reglas de desempate
--    Códigos: H2H_* = solo entre empatados · *_RIVALES = rivales enfrentados
--    REAPLICAR_H2H = repetir H2H entre el subconjunto que siga empatado
-- -----------------------------------------------------------------------------
ALTER TABLE gold.d_regla_desempate
    ADD COLUMN codigo               TEXT,
    ADD COLUMN criterios_en_curso   TEXT[],      -- los que aplican antes de cerrar la fase
    ADD COLUMN pendiente_verificar  TEXT;

INSERT INTO gold.d_regla_desempate (codigo, formato, vigente_desde, criterios, criterios_en_curso, fuente_oficial, pendiente_verificar) VALUES
('G1', 'GRUPOS', 2015,
 ARRAY['H2H_PUNTOS','H2H_DG','H2H_GF','H2H_GOLES_VISITANTE','REAPLICAR_H2H',
       'DG','GF','GOLES_VISITANTE','VICTORIAS','VICTORIAS_VISITANTE','DISCIPLINA','COEFICIENTE_UEFA'],
 NULL,
 'UEFA CL Regulations (Equality of points); orden confirmado para 2020/21',
 'Confirmar si VICTORIAS y VICTORIAS_VISITANTE existían en 2015/16-2017/18'),
('G2', 'GRUPOS', 2021,
 ARRAY['H2H_PUNTOS','H2H_DG','H2H_GF','REAPLICAR_H2H',
       'DG','GF','GOLES_VISITANTE','VICTORIAS','VICTORIAS_VISITANTE','DISCIPLINA','COEFICIENTE_UEFA'],
 NULL,
 'UEFA, abolición del gol de visitante (24-jun-2021); se mantiene GOLES_VISITANTE del total del grupo',
 NULL),
('L1', 'LIGA', 2024,
 ARRAY['DG','GF','GOLES_VISITANTE','VICTORIAS','VICTORIAS_VISITANTE',
       'PUNTOS_RIVALES','DG_RIVALES','GF_RIVALES','DISCIPLINA','COEFICIENTE_UEFA'],
 ARRAY['DG','GF','GOLES_VISITANTE','VICTORIAS','VICTORIAS_VISITANTE'],
 'Wikipedia fase de liga 2024/25 y 2026/27 (réplica del reglamento)',
 'Confirmar COEFICIENTE_UEFA como último criterio en el PDF de UEFA');

ALTER TABLE gold.d_regla_desempate
    ALTER COLUMN codigo SET NOT NULL,
    ADD CONSTRAINT uq_regla_desempate_codigo UNIQUE (codigo);

UPDATE gold.d_temporada t
SET id_regla_desempate = r.id_regla
FROM gold.d_regla_desempate r
WHERE r.codigo = CASE WHEN t.id_temporada >= 2024 THEN 'L1'
                      WHEN t.id_temporada >= 2021 THEN 'G2'
                      ELSE 'G1' END;

-- -----------------------------------------------------------------------------
-- 2. Destino del 3º de grupo y regla de eliminatoria por temporada
-- -----------------------------------------------------------------------------
ALTER TABLE gold.d_temporada
    ADD COLUMN destino_tercero_grupo TEXT,      -- NULL en formato LIGA
    ADD COLUMN desempate_eliminatoria TEXT;

UPDATE gold.d_temporada
SET destino_tercero_grupo  = CASE WHEN id_temporada >= 2024 THEN NULL
                                  WHEN id_temporada >= 2021 THEN 'EL_KNOCKOUT_PLAYOFF'
                                  ELSE 'EL_DIECISEISAVOS' END,
    desempate_eliminatoria = CASE WHEN regla_gol_visitante
                                  THEN 'GOL_VISITANTE (incl. prórroga) > PENALES'
                                  ELSE 'PRORROGA 2x15 > PENALES' END;

-- -----------------------------------------------------------------------------
-- 3. Ranking provisional (criterios en curso) vs final (al cierre de la fase)
-- -----------------------------------------------------------------------------
ALTER TABLE gold.f_tabla_posiciones RENAME COLUMN posicion TO posicion_provisional;

ALTER TABLE gold.f_tabla_posiciones
    ADD COLUMN posicion_final         SMALLINT,   -- solo en la última jornada de la fase
    ADD COLUMN goles_visitante        SMALLINT,
    ADD COLUMN victorias_visitante    SMALLINT,
    ADD COLUMN desempate_aproximado   BOOLEAN NOT NULL DEFAULT FALSE;  -- TRUE si faltó DISCIPLINA

COMMENT ON COLUMN gold.f_tabla_posiciones.posicion_provisional IS
    'Formato LIGA: criterios_en_curso (DG, GF, goles visitante, victorias, victorias visitante). GRUPOS: regla completa.';
COMMENT ON COLUMN gold.f_tabla_posiciones.desempate_aproximado IS
    'TRUE cuando el orden dependió de DISCIPLINA y se resolvió con COEFICIENTE_UEFA por falta de datos de tarjetas.';

-- -----------------------------------------------------------------------------
-- 4. Coeficiente UEFA de club por temporada (último criterio de desempate)
-- -----------------------------------------------------------------------------
CREATE TABLE gold.f_coeficiente_uefa (
    id_equipo     INTEGER  NOT NULL REFERENCES gold.d_equipo (id_equipo),
    id_temporada  SMALLINT NOT NULL REFERENCES gold.d_temporada (id_temporada),
    coeficiente   NUMERIC(7,3) NOT NULL,
    PRIMARY KEY (id_equipo, id_temporada)
);

ALTER TABLE gold.f_coeficiente_uefa ENABLE ROW LEVEL SECURITY;
CREATE POLICY lectura_publica ON gold.f_coeficiente_uefa FOR SELECT TO anon, authenticated USING (true);
GRANT SELECT ON gold.f_coeficiente_uefa TO anon, authenticated;

-- -----------------------------------------------------------------------------
-- 5. Recrear vw_tabla_final con las columnas nuevas
-- -----------------------------------------------------------------------------
DROP VIEW gold.vw_tabla_final;

CREATE VIEW gold.vw_tabla_final WITH (security_invoker = true) AS
SELECT t.*,
       COALESCE(t.posicion_final, t.posicion_provisional) AS posicion
FROM gold.f_tabla_posiciones t
WHERE t.jornada = (SELECT MAX(t2.jornada)
                   FROM gold.f_tabla_posiciones t2
                   WHERE t2.id_temporada = t.id_temporada);

GRANT SELECT ON gold.vw_tabla_final TO anon, authenticated;

COMMIT;
