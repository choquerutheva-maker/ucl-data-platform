/* =============================================================================
   UCL Data Platform - Más campos en silver.nike_pronosticos
   Descripción : Campos que Niké entrega por partido (msg #44): Over/Under 2.5,
                 ambos marcan, confianza y once (probable u oficial), más el
                 archivo de origen (CSV de la carpeta entradas/nike, cargado
                 por src/ingesta/nike_pronosticos.py).
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-10-26
============================================================================= */

BEGIN;

ALTER TABLE silver.nike_pronosticos
    ADD COLUMN prob_over_2_5   NUMERIC(6,4) CHECK (prob_over_2_5 BETWEEN 0 AND 1),
    ADD COLUMN prob_btts       NUMERIC(6,4) CHECK (prob_btts     BETWEEN 0 AND 1),
    ADD COLUMN confianza       TEXT CHECK (confianza IN ('ALTA','MEDIA','BAJA')),
    ADD COLUMN tipo_once       TEXT CHECK (tipo_once IN ('PROBABLE','OFICIAL')),
    ADD COLUMN once_local      TEXT,
    ADD COLUMN once_visita     TEXT,
    ADD COLUMN inicio_utc      TIMESTAMPTZ,              -- inicio del partido según ESPN al cargar
    ADD COLUMN archivo_origen  TEXT;

COMMIT;
