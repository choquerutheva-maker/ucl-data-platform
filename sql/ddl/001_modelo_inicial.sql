/* =============================================================================
   UCL Data Platform - Modelo inicial (PostgreSQL / Supabase)
   Descripción : Esquemas bronze / silver / gold, dimensiones y hechos del
                 modelo dimensional de la UEFA Champions League, más permisos
                 de lectura pública sobre gold.
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-10-21
   Notas       :
     - Nomenclatura en minúsculas (convención PostgreSQL): d_ = dimensión,
       f_ = hecho, vw_ = vista.
     - Resultado oficial del 1X2 y O/U = marcador a los 90 minutos.
     - f_estado_prepartido solo admite datos disponibles ANTES del inicio
       (evita leakage).
     - silver se llena en la fase de ETL (staging por fuente).
============================================================================= */

BEGIN;

-- -----------------------------------------------------------------------------
-- 0. Esquemas
-- -----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS bronze;   -- payloads crudos tal como llegan
CREATE SCHEMA IF NOT EXISTS silver;   -- datos limpios por fuente (ETL)
CREATE SCHEMA IF NOT EXISTS gold;     -- modelo estrella (expuesto en la API)

-- -----------------------------------------------------------------------------
-- 1. Catálogo de fuentes (con licencia y atribución)
-- -----------------------------------------------------------------------------
CREATE TABLE gold.d_fuente (
    id_fuente          TEXT PRIMARY KEY,
    nombre             TEXT NOT NULL,
    url                TEXT,
    tipo_acceso        TEXT NOT NULL CHECK (tipo_acceso IN ('API','CSV','JSON_GIT','HTML')),
    licencia           TEXT,
    atribucion         TEXT,
    republicar_crudo   BOOLEAN,             -- NULL = pendiente de verificar
    notas              TEXT
);

INSERT INTO gold.d_fuente (id_fuente, nombre, url, tipo_acceso, licencia, atribucion, republicar_crudo, notas) VALUES
('openfootball', 'openfootball',           'https://github.com/openfootball/football.json', 'JSON_GIT', 'Dominio público',        NULL,                  TRUE,  'Histórico de resultados'),
('rsssf',        'RSSSF European Cups',    'https://www.rsssf.org/ec/ecomp.html',           'HTML',     'Copia citando al autor', 'Citar RSSSF y autor', NULL,  'Validación del histórico'),
('football_data','football-data.org',      'https://www.football-data.org',                 'API',      'VERIFICAR',              'VERIFICAR',           NULL,  'Temporada actual; 10 req/min'),
('clubelo',      'ClubElo',                'http://api.clubelo.com',                        'CSV',      'VERIFICAR',              'Citar clubelo.com',   NULL,  'Elo diario'),
('espn',         'ESPN API no oficial',    'https://site.api.espn.com',                     'API',      'Sin términos públicos',  'Citar ESPN',          FALSE, 'Solo features/agregados; cuotas actuales'),
('api_football', 'API-Football',           'https://www.api-football.com',                  'API',      'VERIFICAR',              'VERIFICAR',           FALSE, 'Lesiones; 100 req/día'),
('statsbomb',    'StatsBomb Open Data',    'https://github.com/statsbomb/open-data',        'JSON_GIT', 'User agreement StatsBomb','Logo + cita StatsBomb',NULL, 'Solo visualización de eventos');

-- -----------------------------------------------------------------------------
-- 2. Bronze: ingesta cruda genérica
-- -----------------------------------------------------------------------------
CREATE TABLE bronze.ingesta (
    id_ingesta      BIGSERIAL PRIMARY KEY,
    id_fuente       TEXT NOT NULL REFERENCES gold.d_fuente (id_fuente),
    endpoint        TEXT NOT NULL,           -- ruta, archivo o URL consultada
    parametros      JSONB,
    http_status     SMALLINT,
    payload_json    JSONB,                   -- respuestas JSON
    payload_texto   TEXT,                    -- CSV / HTML
    payload_hash    TEXT NOT NULL,           -- md5 del contenido, para deduplicar
    capturado_en    TIMESTAMPTZ NOT NULL DEFAULT now(),
    procesado       BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT ck_ingesta_payload CHECK (payload_json IS NOT NULL OR payload_texto IS NOT NULL),
    CONSTRAINT uq_ingesta_contenido UNIQUE (id_fuente, endpoint, payload_hash)
);

CREATE INDEX ix_ingesta_pendiente ON bronze.ingesta (id_fuente, capturado_en) WHERE NOT procesado;

-- -----------------------------------------------------------------------------
-- 3. Dimensiones
-- -----------------------------------------------------------------------------

-- Criterios de desempate por formato (orden pendiente de Niké, msg #30)
CREATE TABLE gold.d_regla_desempate (
    id_regla        SMALLSERIAL PRIMARY KEY,
    formato         TEXT NOT NULL CHECK (formato IN ('GRUPOS','LIGA')),
    vigente_desde   SMALLINT NOT NULL,       -- id_temporada
    criterios       TEXT[] NOT NULL,         -- en orden de aplicación
    fuente_oficial  TEXT,
    UNIQUE (formato, vigente_desde)
);

-- id_temporada = año de inicio (2025 = 2025/26)
CREATE TABLE gold.d_temporada (
    id_temporada         SMALLINT PRIMARY KEY,
    etiqueta             TEXT NOT NULL UNIQUE,
    formato              TEXT NOT NULL CHECK (formato IN ('GRUPOS','LIGA')),
    equipos_fase_inicial SMALLINT NOT NULL,
    regla_gol_visitante  BOOLEAN NOT NULL,
    id_regla_desempate   SMALLINT REFERENCES gold.d_regla_desempate (id_regla),
    fecha_inicio         DATE,
    fecha_fin            DATE,
    es_actual            BOOLEAN NOT NULL DEFAULT FALSE
);

INSERT INTO gold.d_temporada (id_temporada, etiqueta, formato, equipos_fase_inicial, regla_gol_visitante, es_actual)
SELECT y,
       y::TEXT || '/' || RIGHT((y + 1)::TEXT, 2),
       CASE WHEN y >= 2024 THEN 'LIGA' ELSE 'GRUPOS' END,
       CASE WHEN y >= 2024 THEN 36 ELSE 32 END,
       y <= 2020,                            -- gol de visitante abolido desde 2021/22
       y = 2026
FROM generate_series(2015, 2026) AS y;

CREATE TABLE gold.d_ronda (
    id_ronda         SMALLINT PRIMARY KEY,
    codigo           TEXT NOT NULL UNIQUE,
    nombre           TEXT NOT NULL,
    orden            SMALLINT NOT NULL,
    es_previa        BOOLEAN NOT NULL DEFAULT FALSE,
    es_eliminatoria  BOOLEAN NOT NULL,
    es_ida_vuelta    BOOLEAN NOT NULL
);

INSERT INTO gold.d_ronda (id_ronda, codigo, nombre, orden, es_previa, es_eliminatoria, es_ida_vuelta) VALUES
( 1, 'PREVIA',   'Rondas previas',        1, TRUE,  TRUE,  TRUE),   -- fuera de v1
(10, 'GRUPOS',   'Fase de grupos',       10, FALSE, FALSE, FALSE),
(11, 'LIGA',     'Fase de liga',         11, FALSE, FALSE, FALSE),
(20, 'PLAYOFF',  'Playoff de eliminatoria', 20, FALSE, TRUE, TRUE),
(30, 'OCTAVOS',  'Octavos de final',     30, FALSE, TRUE,  TRUE),
(40, 'CUARTOS',  'Cuartos de final',     40, FALSE, TRUE,  TRUE),
(50, 'SEMIS',    'Semifinales',          50, FALSE, TRUE,  TRUE),
(60, 'FINAL',    'Final',                60, FALSE, TRUE,  FALSE);

CREATE TABLE gold.d_estadio (
    id_estadio   SERIAL PRIMARY KEY,
    nombre       TEXT NOT NULL,
    ciudad       TEXT NOT NULL,
    pais_iso3    CHAR(3) NOT NULL,
    latitud      NUMERIC(9,6),
    longitud     NUMERIC(9,6),
    altitud_m    SMALLINT,
    capacidad    INTEGER,
    UNIQUE (nombre, ciudad)
);

CREATE TABLE gold.d_equipo (
    id_equipo            SERIAL PRIMARY KEY,
    nombre_canonico      TEXT NOT NULL UNIQUE,
    nombre_corto         TEXT,
    pais_iso3            CHAR(3) NOT NULL,
    id_estadio_principal INTEGER REFERENCES gold.d_estadio (id_estadio)
);

-- Resolución de entidades: cómo nombra cada fuente a cada equipo
CREATE TABLE gold.d_equipo_alias (
    id_fuente    TEXT NOT NULL REFERENCES gold.d_fuente (id_fuente),
    alias        TEXT NOT NULL,
    id_externo   TEXT,                        -- id del equipo en la fuente, si tiene
    id_equipo    INTEGER NOT NULL REFERENCES gold.d_equipo (id_equipo),
    validado     BOOLEAN NOT NULL DEFAULT FALSE,  -- FALSE = match automático por revisar
    PRIMARY KEY (id_fuente, alias)
);

CREATE UNIQUE INDEX uq_equipo_alias_externo ON gold.d_equipo_alias (id_fuente, id_externo) WHERE id_externo IS NOT NULL;
CREATE INDEX ix_equipo_alias_equipo ON gold.d_equipo_alias (id_equipo);

CREATE TABLE gold.d_fecha (
    fecha          DATE PRIMARY KEY,
    anio           SMALLINT NOT NULL,
    mes            SMALLINT NOT NULL,
    dia            SMALLINT NOT NULL,
    nombre_mes     TEXT NOT NULL,
    dia_semana     SMALLINT NOT NULL,        -- 1 = lunes (ISO)
    nombre_dia     TEXT NOT NULL,
    es_fin_semana  BOOLEAN NOT NULL
);

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
FROM generate_series('2015-06-01'::DATE, '2027-12-31'::DATE, INTERVAL '1 day') AS d;

-- Versiones de modelos (incluye a Niké como benchmark)
CREATE TABLE gold.d_modelo (
    id_modelo            SERIAL PRIMARY KEY,
    nombre               TEXT NOT NULL,
    version              TEXT NOT NULL,
    algoritmo            TEXT NOT NULL,      -- 'ELO','POISSON','DIXON_COLES','XGBOOST','EXPERTO','MERCADO'
    temporadas_train     TEXT,
    entrenado_en         TIMESTAMPTZ,
    metricas_validacion  JSONB,
    descripcion          TEXT,
    UNIQUE (nombre, version)
);

-- -----------------------------------------------------------------------------
-- 4. Hechos
-- -----------------------------------------------------------------------------

-- Llave de eliminatoria a ida y vuelta (la final es partido único: sin serie)
CREATE TABLE gold.f_serie (
    id_serie                 SERIAL PRIMARY KEY,
    id_temporada             SMALLINT NOT NULL REFERENCES gold.d_temporada (id_temporada),
    id_ronda                 SMALLINT NOT NULL REFERENCES gold.d_ronda (id_ronda),
    id_equipo_a              INTEGER NOT NULL REFERENCES gold.d_equipo (id_equipo),  -- local en la ida
    id_equipo_b              INTEGER NOT NULL REFERENCES gold.d_equipo (id_equipo),
    global_a                 SMALLINT,
    global_b                 SMALLINT,
    goles_visitante_a        SMALLINT,
    goles_visitante_b        SMALLINT,
    metodo_definicion        TEXT CHECK (metodo_definicion IN ('GLOBAL','GOL_VISITANTE','PRORROGA','PENALES')),
    id_equipo_avanza         INTEGER REFERENCES gold.d_equipo (id_equipo),
    UNIQUE (id_temporada, id_ronda, id_equipo_a, id_equipo_b),
    CHECK (id_equipo_a <> id_equipo_b)
);

CREATE TABLE gold.f_partido (
    id_partido             BIGSERIAL PRIMARY KEY,
    id_temporada           SMALLINT NOT NULL REFERENCES gold.d_temporada (id_temporada),
    id_ronda               SMALLINT NOT NULL REFERENCES gold.d_ronda (id_ronda),
    jornada                SMALLINT,                     -- solo grupos / liga
    grupo                  CHAR(1),                      -- 'A'..'H' hasta 2023/24
    id_serie               INTEGER REFERENCES gold.f_serie (id_serie),
    partido_serie          SMALLINT CHECK (partido_serie IN (1, 2)),  -- 1 ida, 2 vuelta
    fecha                  DATE NOT NULL REFERENCES gold.d_fecha (fecha),
    inicio_utc             TIMESTAMPTZ,
    id_equipo_local        INTEGER NOT NULL REFERENCES gold.d_equipo (id_equipo),
    id_equipo_visita       INTEGER NOT NULL REFERENCES gold.d_equipo (id_equipo),
    id_estadio             INTEGER REFERENCES gold.d_estadio (id_estadio),
    es_neutral             BOOLEAN NOT NULL DEFAULT FALSE,
    asistencia             INTEGER,
    estado                 TEXT NOT NULL DEFAULT 'PROGRAMADO'
                           CHECK (estado IN ('PROGRAMADO','JUGADO','SUSPENDIDO','APLAZADO')),
    goles_local_90         SMALLINT,
    goles_visita_90        SMALLINT,
    goles_local_prorroga   SMALLINT,                     -- acumulado al 120'; NULL si no hubo
    goles_visita_prorroga  SMALLINT,
    penales_local          SMALLINT,
    penales_visita         SMALLINT,
    metodo_definicion      TEXT CHECK (metodo_definicion IN ('90','PRORROGA','PENALES')),
    resultado_90           CHAR(1) GENERATED ALWAYS AS (
                               CASE WHEN goles_local_90 > goles_visita_90 THEN 'L'
                                    WHEN goles_local_90 = goles_visita_90 THEN 'E'
                                    WHEN goles_local_90 < goles_visita_90 THEN 'V' END) STORED,
    goles_total_90         SMALLINT GENERATED ALWAYS AS (goles_local_90 + goles_visita_90) STORED,
    id_equipo_avanza       INTEGER REFERENCES gold.d_equipo (id_equipo),  -- vuelta / final
    id_fuente_principal    TEXT NOT NULL REFERENCES gold.d_fuente (id_fuente),
    cargado_en             TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (id_equipo_local <> id_equipo_visita),
    CHECK (estado <> 'JUGADO' OR (goles_local_90 IS NOT NULL AND goles_visita_90 IS NOT NULL)),
    UNIQUE (id_temporada, id_ronda, id_equipo_local, id_equipo_visita)
);

CREATE INDEX ix_f_partido_fecha        ON gold.f_partido (fecha);
CREATE INDEX ix_f_partido_local        ON gold.f_partido (id_equipo_local, fecha);
CREATE INDEX ix_f_partido_visita       ON gold.f_partido (id_equipo_visita, fecha);
CREATE INDEX ix_f_partido_temp_jornada ON gold.f_partido (id_temporada, jornada);

-- Foto de la tabla después de cada jornada (calculada desde f_partido)
CREATE TABLE gold.f_tabla_posiciones (
    id_temporada   SMALLINT NOT NULL REFERENCES gold.d_temporada (id_temporada),
    jornada        SMALLINT NOT NULL,
    id_equipo      INTEGER  NOT NULL REFERENCES gold.d_equipo (id_equipo),
    id_ronda       SMALLINT NOT NULL REFERENCES gold.d_ronda (id_ronda),   -- GRUPOS o LIGA
    grupo          CHAR(1),                                                -- NULL en fase de liga
    posicion       SMALLINT NOT NULL,
    pj             SMALLINT NOT NULL,
    pg             SMALLINT NOT NULL,
    pe             SMALLINT NOT NULL,
    pp             SMALLINT NOT NULL,
    gf             SMALLINT NOT NULL,
    gc             SMALLINT NOT NULL,
    dg             SMALLINT GENERATED ALWAYS AS (gf - gc) STORED,
    puntos         SMALLINT NOT NULL,
    franja         TEXT NOT NULL,     -- LIGA: TOP8/PLAYOFF/ELIMINADO · GRUPOS: CLASIFICA/EUROPA_LEAGUE/ELIMINADO
    fecha_corte    DATE NOT NULL,
    PRIMARY KEY (id_temporada, jornada, id_equipo)
);

-- Elo de ClubElo guardado por rangos (desde/hasta) en lugar de una fila por día
CREATE TABLE gold.f_elo_historico (
    id_equipo     INTEGER NOT NULL REFERENCES gold.d_equipo (id_equipo),
    fecha_desde   DATE NOT NULL,
    fecha_hasta   DATE NOT NULL,
    elo           NUMERIC(7,2) NOT NULL,
    ranking       SMALLINT,
    PRIMARY KEY (id_equipo, fecha_desde),
    CHECK (fecha_hasta >= fecha_desde)
);

-- Fotos de cuotas pre-partido (histórico propio desde 2026-10)
CREATE TABLE gold.f_cuotas_snapshot (
    id_snapshot       BIGSERIAL PRIMARY KEY,
    id_partido        BIGINT NOT NULL REFERENCES gold.f_partido (id_partido),
    id_fuente         TEXT NOT NULL REFERENCES gold.d_fuente (id_fuente),
    casa_apuestas     TEXT NOT NULL,
    capturado_en      TIMESTAMPTZ NOT NULL,
    cuota_local       NUMERIC(6,2),
    cuota_empate      NUMERIC(6,2),
    cuota_visita      NUMERIC(6,2),
    linea_ou          NUMERIC(3,1),
    cuota_over        NUMERIC(6,2),
    cuota_under       NUMERIC(6,2),
    margen_1x2        NUMERIC(6,4) GENERATED ALWAYS AS (
                          1/cuota_local + 1/cuota_empate + 1/cuota_visita - 1) STORED,
    prob_local        NUMERIC(6,4) GENERATED ALWAYS AS (
                          (1/cuota_local)  / (1/cuota_local + 1/cuota_empate + 1/cuota_visita)) STORED,
    prob_empate       NUMERIC(6,4) GENERATED ALWAYS AS (
                          (1/cuota_empate) / (1/cuota_local + 1/cuota_empate + 1/cuota_visita)) STORED,
    prob_visita       NUMERIC(6,4) GENERATED ALWAYS AS (
                          (1/cuota_visita) / (1/cuota_local + 1/cuota_empate + 1/cuota_visita)) STORED,
    UNIQUE (id_partido, id_fuente, casa_apuestas, capturado_en),
    CHECK (cuota_local > 1 AND cuota_empate > 1 AND cuota_visita > 1)
);

-- Estado pre-partido: SOLO información disponible antes del inicio
CREATE TABLE gold.f_estado_prepartido (
    id_partido              BIGINT NOT NULL REFERENCES gold.f_partido (id_partido),
    version_features        TEXT NOT NULL,
    calculado_en            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Fuerza
    elo_local               NUMERIC(7,2),
    elo_visita              NUMERIC(7,2),
    dif_elo                 NUMERIC(7,2) GENERATED ALWAYS AS (elo_local - elo_visita) STORED,
    -- Tabla previa (jornada N-1)
    posicion_local_pre      SMALLINT,
    posicion_visita_pre     SMALLINT,
    puntos_local_pre        SMALLINT,
    puntos_visita_pre       SMALLINT,
    franja_local_pre        TEXT,
    franja_visita_pre       TEXT,
    -- Forma reciente (todas las competiciones disponibles)
    forma_pts_local         NUMERIC(5,2),        -- ponderada, últimos 6
    forma_pts_visita        NUMERIC(5,2),
    gf_prom_local           NUMERIC(4,2),
    gc_prom_local           NUMERIC(4,2),
    gf_prom_visita          NUMERIC(4,2),
    gc_prom_visita          NUMERIC(4,2),
    -- Calendario y logística
    dias_descanso_local     SMALLINT,
    dias_descanso_visita    SMALLINT,
    dias_hasta_prox_local   SMALLINT,
    dias_hasta_prox_visita  SMALLINT,
    distancia_viaje_km      NUMERIC(7,1),
    altitud_estadio_m       SMALLINT,
    ventaja_local_temporada NUMERIC(5,3),        -- localía estimada, no constante
    -- Eliminatoria (vuelta)
    global_previo_local     SMALLINT,
    global_previo_visita    SMALLINT,
    llave_resuelta          BOOLEAN,
    -- Incentivo ("puntos en juego")
    stake_local             NUMERIC(5,4),
    stake_visita            NUMERIC(5,4),
    dead_rubber_local       BOOLEAN,
    dead_rubber_visita      BOOLEAN,
    draw_suits_both         BOOLEAN,
    importancia_local       TEXT CHECK (importancia_local  IN ('ALTA','MEDIA','BAJA')),
    importancia_visita      TEXT CHECK (importancia_visita IN ('ALTA','MEDIA','BAJA')),
    -- Mercado (última foto antes del inicio)
    prob_mercado_local      NUMERIC(6,4),
    prob_mercado_empate     NUMERIC(6,4),
    prob_mercado_visita     NUMERIC(6,4),
    PRIMARY KEY (id_partido, version_features)
);

CREATE TABLE gold.f_pronostico (
    id_pronostico      BIGSERIAL PRIMARY KEY,
    id_partido         BIGINT NOT NULL REFERENCES gold.f_partido (id_partido),
    id_modelo          INTEGER NOT NULL REFERENCES gold.d_modelo (id_modelo),
    generado_en        TIMESTAMPTZ NOT NULL DEFAULT now(),
    prob_local         NUMERIC(6,4) NOT NULL,
    prob_empate        NUMERIC(6,4) NOT NULL,
    prob_visita        NUMERIC(6,4) NOT NULL,
    goles_esp_local    NUMERIC(4,2),
    goles_esp_visita   NUMERIC(4,2),
    prob_over_2_5      NUMERIC(6,4),
    pick               CHAR(1) GENERATED ALWAYS AS (
                           CASE WHEN prob_local >= prob_empate AND prob_local >= prob_visita THEN 'L'
                                WHEN prob_visita >= prob_empate THEN 'V'
                                ELSE 'E' END) STORED,
    UNIQUE (id_partido, id_modelo),
    CHECK (ABS(prob_local + prob_empate + prob_visita - 1) < 0.005)
);

-- -----------------------------------------------------------------------------
-- 5. Vistas
-- -----------------------------------------------------------------------------

-- Tabla final de cada temporada = última jornada registrada
-- security_invoker: las vistas respetan el RLS de las tablas base (PG15+)
CREATE VIEW gold.vw_tabla_final WITH (security_invoker = true) AS
SELECT t.*
FROM gold.f_tabla_posiciones t
WHERE t.jornada = (SELECT MAX(t2.jornada)
                   FROM gold.f_tabla_posiciones t2
                   WHERE t2.id_temporada = t.id_temporada);

-- Evaluación de pronósticos contra el resultado a los 90'
CREATE VIEW gold.vw_evaluacion_pronosticos WITH (security_invoker = true) AS
SELECT pr.id_pronostico,
       pr.id_modelo,
       m.nombre AS modelo,
       m.version,
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

-- -----------------------------------------------------------------------------
-- 6. Seguridad Supabase: gold de solo lectura pública; bronze/silver privados
--    (además, agregar 'gold' en Settings > API > Exposed schemas)
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA gold TO anon, authenticated;
GRANT SELECT ON ALL TABLES IN SCHEMA gold TO anon, authenticated;

DO $$
DECLARE t RECORD;
BEGIN
    FOR t IN SELECT schemaname, tablename FROM pg_tables
             WHERE schemaname IN ('bronze','silver','gold')
    LOOP
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', t.schemaname, t.tablename);
        IF t.schemaname = 'gold' THEN
            EXECUTE format('CREATE POLICY lectura_publica ON %I.%I FOR SELECT TO anon, authenticated USING (true)',
                           t.schemaname, t.tablename);
        END IF;
    END LOOP;
END $$;

COMMIT;
