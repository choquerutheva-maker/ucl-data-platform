/* =============================================================================
   UCL Data Platform - Motivo de sede neutral y columnas extra de Niké
   Descripción : - gold.f_partido.motivo_neutral: por qué un partido se jugó en
                   sede neutral, para poder excluir o ponderar los partidos
                   COVID al estimar la localía (Niké, msg #36).
                 - silver.nike_pronosticos: importancia por equipo y marca de
                   pronóstico tardío (Niké, msg #36).
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-10-22
   Notas       :
     - FINAL          = toda final (partido único).
     - FINAL8_COVID   = Lisboa, agosto 2020 (cuartos, semis y final 2019-20
                        a partido único; la final queda como FINAL).
     - TRASLADO_COVID = llaves de 2020-21 jugadas en un tercer país por
                        restricciones de viaje (los loaders los marcan).
============================================================================= */

BEGIN;

ALTER TABLE gold.f_partido
    ADD COLUMN motivo_neutral TEXT
        CHECK (motivo_neutral IN ('FINAL','FINAL8_COVID','TRASLADO_COVID'));

-- Filas ya cargadas: hasta hoy solo eran neutrales las finales y el Final 8
UPDATE gold.f_partido
SET motivo_neutral = CASE WHEN id_ronda = 60 THEN 'FINAL' ELSE 'FINAL8_COVID' END
WHERE es_neutral;

ALTER TABLE gold.f_partido
    ADD CONSTRAINT ck_f_partido_motivo_neutral CHECK (es_neutral = (motivo_neutral IS NOT NULL));

ALTER TABLE silver.nike_pronosticos
    ADD COLUMN importancia_local  TEXT CHECK (importancia_local  IN ('ALTA','MEDIA','BAJA')),
    ADD COLUMN importancia_visita TEXT CHECK (importancia_visita IN ('ALTA','MEDIA','BAJA')),
    ADD COLUMN es_tardio          BOOLEAN NOT NULL DEFAULT FALSE;   -- pronóstico hecho con el partido ya iniciado

COMMIT;
