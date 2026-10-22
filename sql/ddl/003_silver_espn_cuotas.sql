/* =============================================================================
   UCL Data Platform - silver.espn_cuotas
   Descripción : Cuotas parseadas de ESPN (scoreboard + summary), una fila por
                 partido, casa de apuestas, tipo de línea y captura.
                 Se mapea a gold.f_cuotas_snapshot vía gold.d_equipo_alias.
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-10-21
   Notas       :
     - ACTUAL se acumula en cada captura (serie temporal de la línea).
     - APERTURA y CIERRE se guardan una sola vez por partido y casa.
     - Privada: RLS activo sin políticas (no se expone en la API).
============================================================================= */

BEGIN;

CREATE TABLE silver.espn_cuotas (
    id_cuota         BIGSERIAL PRIMARY KEY,
    id_ingesta       BIGINT REFERENCES bronze.ingesta (id_ingesta),
    espn_event_id    TEXT NOT NULL,
    liga             TEXT NOT NULL,
    inicio_utc       TIMESTAMPTZ NOT NULL,
    estado_partido   TEXT NOT NULL,
    local_espn_id    TEXT NOT NULL,
    local_nombre     TEXT NOT NULL,
    visita_espn_id   TEXT NOT NULL,
    visita_nombre    TEXT NOT NULL,
    estadio          TEXT,
    ciudad           TEXT,
    casa_id          TEXT NOT NULL,
    casa_apuestas    TEXT NOT NULL,
    tipo_linea       TEXT NOT NULL CHECK (tipo_linea IN ('ACTUAL','APERTURA','CIERRE')),
    cuota_local      NUMERIC(8,3) NOT NULL CHECK (cuota_local  > 1),
    cuota_empate     NUMERIC(8,3) NOT NULL CHECK (cuota_empate > 1),
    cuota_visita     NUMERIC(8,3) NOT NULL CHECK (cuota_visita > 1),
    linea_ou         NUMERIC(3,1),
    cuota_over       NUMERIC(8,3),
    cuota_under      NUMERIC(8,3),
    formato_origen   TEXT NOT NULL CHECK (formato_origen IN ('americana','fraccional')),
    capturado_en     TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX uq_espn_cuotas_actual
    ON silver.espn_cuotas (espn_event_id, casa_id, capturado_en)
    WHERE tipo_linea = 'ACTUAL';

CREATE UNIQUE INDEX uq_espn_cuotas_apertura_cierre
    ON silver.espn_cuotas (espn_event_id, casa_id, tipo_linea)
    WHERE tipo_linea IN ('APERTURA','CIERRE');

CREATE INDEX ix_espn_cuotas_inicio ON silver.espn_cuotas (inicio_utc);

ALTER TABLE silver.espn_cuotas ENABLE ROW LEVEL SECURITY;

COMMIT;
