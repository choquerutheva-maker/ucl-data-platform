/* =============================================================================
   UCL Data Platform - Ligas nacionales (fase 1 de la ampliación a 5 ligas)
   Descripción : Fuente football-data.co.uk, dimensión de ligas, partidos de
                 liga en gold.f_partido_liga (tabla separada de f_partido, que
                 sigue siendo solo UCL) y cuotas por casa en silver (privadas).
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-10-31
   Notas       :
     - temporada = año de inicio (2026 = 2026/27), igual que d_temporada, pero
       SIN FK: las ligas se cargan desde 2012/13 para calentar el Elo y
       d_temporada describe solo formatos de la Champions.
     - d_fecha se extiende hacia atrás hasta 2012-07-01.
     - Equipos: se reutiliza gold.d_equipo; los clubes que ya jugaron la UCL
       quedan con el mismo id_equipo (seeds/equipos_football_data.csv).
     - Las cuotas quedan en silver (sin GRANT a anon): el dashboard solo
       muestra agregados de mercado, nunca cuotas por partido.
============================================================================= */

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Fuente
-- -----------------------------------------------------------------------------
INSERT INTO gold.d_fuente (id_fuente, nombre, url, tipo_acceso, licencia, atribucion, republicar_crudo, notas)
VALUES ('football_data_uk', 'Football-Data.co.uk', 'https://www.football-data.co.uk', 'CSV',
        'VERIFICAR', 'Citar football-data.co.uk', FALSE,
        'Ligas nacionales: resultados, estadísticas, xG (2026+) y cuotas previas y de cierre; se actualiza ~2 veces por semana')
ON CONFLICT (id_fuente) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 2. Dimensión de ligas
-- -----------------------------------------------------------------------------
CREATE TABLE gold.d_liga (
    id_liga        TEXT PRIMARY KEY,             -- código propio estable
    nombre         TEXT NOT NULL,
    pais_iso3      CHAR(3) NOT NULL,
    nivel          SMALLINT NOT NULL DEFAULT 1,
    codigo_fd      TEXT NOT NULL UNIQUE,         -- código de división en football-data.co.uk
    orden          SMALLINT NOT NULL
);

INSERT INTO gold.d_liga (id_liga, nombre, pais_iso3, nivel, codigo_fd, orden) VALUES
('ENG1', 'Premier League', 'ENG', 1, 'E0',  1),
('ESP1', 'LaLiga',         'ESP', 1, 'SP1', 2),
('GER1', 'Bundesliga',     'GER', 1, 'D1',  3),
('ITA1', 'Serie A',        'ITA', 1, 'I1',  4),
('FRA1', 'Ligue 1',        'FRA', 1, 'F1',  5);

-- -----------------------------------------------------------------------------
-- 3. Calendario: d_fecha desde 2012-07-01
-- -----------------------------------------------------------------------------
INSERT INTO gold.d_fecha
SELECT d::DATE,
       EXTRACT(YEAR  FROM d),
       EXTRACT(MONTH FROM d),
       EXTRACT(DAY   FROM d),
       (ARRAY['enero','febrero','marzo','abril','mayo','junio','julio','agosto',
              'septiembre','octubre','noviembre','diciembre'])[EXTRACT(MONTH FROM d)],
       EXTRACT(ISODOW FROM d),
       (ARRAY['lunes','martes','miércoles','jueves','viernes','sábado','domingo'])[EXTRACT(ISODOW FROM d)],
       EXTRACT(ISODOW FROM d) IN (6, 7)
FROM generate_series('2012-07-01'::DATE, '2015-05-31'::DATE, INTERVAL '1 day') AS d
ON CONFLICT (fecha) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 4. Partidos de liga
-- -----------------------------------------------------------------------------
CREATE TABLE gold.f_partido_liga (
    id_partido_liga      BIGSERIAL PRIMARY KEY,
    id_liga              TEXT NOT NULL REFERENCES gold.d_liga (id_liga),
    temporada            SMALLINT NOT NULL CHECK (temporada BETWEEN 2000 AND 2100),
    fecha                DATE NOT NULL REFERENCES gold.d_fecha (fecha),
    inicio_utc           TIMESTAMPTZ,                      -- desde 2019/20 (antes sin hora)
    id_equipo_local      INTEGER NOT NULL REFERENCES gold.d_equipo (id_equipo),
    id_equipo_visita     INTEGER NOT NULL REFERENCES gold.d_equipo (id_equipo),
    estado               TEXT NOT NULL DEFAULT 'PROGRAMADO'
                         CHECK (estado IN ('PROGRAMADO','JUGADO')),
    goles_local_90       SMALLINT,
    goles_visita_90      SMALLINT,
    goles_local_ht       SMALLINT,
    goles_visita_ht      SMALLINT,
    xg_local             NUMERIC(4,2),                     -- solo desde 2026/27
    xg_visita            NUMERIC(4,2),
    tiros_local          SMALLINT,
    tiros_visita         SMALLINT,
    tiros_arco_local     SMALLINT,
    tiros_arco_visita    SMALLINT,
    rojas_local          SMALLINT,
    rojas_visita         SMALLINT,
    resultado_90         CHAR(1) GENERATED ALWAYS AS (
                             CASE WHEN goles_local_90 > goles_visita_90 THEN 'L'
                                  WHEN goles_local_90 = goles_visita_90 THEN 'E'
                                  WHEN goles_local_90 < goles_visita_90 THEN 'V' END) STORED,
    goles_total_90       SMALLINT GENERATED ALWAYS AS (goles_local_90 + goles_visita_90) STORED,
    id_fuente_principal  TEXT NOT NULL DEFAULT 'football_data_uk' REFERENCES gold.d_fuente (id_fuente),
    cargado_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (id_equipo_local <> id_equipo_visita),
    CHECK (estado <> 'JUGADO' OR (goles_local_90 IS NOT NULL AND goles_visita_90 IS NOT NULL)),
    UNIQUE (id_liga, temporada, id_equipo_local, id_equipo_visita)
);

CREATE INDEX ix_f_partido_liga_fecha  ON gold.f_partido_liga (fecha);
CREATE INDEX ix_f_partido_liga_local  ON gold.f_partido_liga (id_equipo_local, fecha);
CREATE INDEX ix_f_partido_liga_visita ON gold.f_partido_liga (id_equipo_visita, fecha);

-- -----------------------------------------------------------------------------
-- 5. Cuotas por casa (privadas)
--    PREVIA = la que football-data toma antes de la jornada (viernes / martes)
--    CIERRE = la última antes del inicio (desde 2019/20)
-- -----------------------------------------------------------------------------
CREATE TABLE silver.fd_cuotas_liga (
    id_partido_liga   BIGINT NOT NULL REFERENCES gold.f_partido_liga (id_partido_liga) ON DELETE CASCADE,
    casa              TEXT NOT NULL CHECK (casa IN ('BET365','PINNACLE','PROMEDIO','MAXIMO','BETFAIR_EX')),
    momento           TEXT NOT NULL CHECK (momento IN ('PREVIA','CIERRE')),
    cuota_local       NUMERIC(7,2) NOT NULL,
    cuota_empate      NUMERIC(7,2) NOT NULL,
    cuota_visita      NUMERIC(7,2) NOT NULL,
    cuota_over_2_5    NUMERIC(7,2),
    cuota_under_2_5   NUMERIC(7,2),
    actualizado_en    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id_partido_liga, casa, momento),
    CHECK (cuota_local > 1 AND cuota_empate > 1 AND cuota_visita > 1)
);

-- -----------------------------------------------------------------------------
-- 6. Seguridad: gold de lectura pública, silver privado
-- -----------------------------------------------------------------------------
ALTER TABLE gold.d_liga          ENABLE ROW LEVEL SECURITY;
ALTER TABLE gold.f_partido_liga  ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.fd_cuotas_liga ENABLE ROW LEVEL SECURITY;

CREATE POLICY lectura_publica ON gold.d_liga         FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY lectura_publica ON gold.f_partido_liga FOR SELECT TO anon, authenticated USING (true);
GRANT SELECT ON gold.d_liga, gold.f_partido_liga TO anon, authenticated;

COMMIT;
