/* =============================================================================
   UCL Data Platform - Coeficiente de club UEFA
   Descripción : Fuente 'uefa' (API pública comp.uefa.com, sin clave) y detalle
                 del coeficiente en gold.f_coeficiente_uefa.
                 id_temporada = S usa el ranking seasonYear = S de la API: el que
                 se cierra al terminar la temporada S-1/S y sirve para el sorteo
                 de la temporada S/S+1 (sin leakage para el modelo).
                 Un club fuera del ranking recibe el piso de su asociación
                 (es_piso_asociacion = TRUE).
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-10-26
============================================================================= */

BEGIN;

INSERT INTO gold.d_fuente (id_fuente, nombre, url, tipo_acceso, licencia, atribucion, republicar_crudo, notas)
VALUES ('uefa', 'UEFA (API pública)', 'https://comp.uefa.com/v2/coefficients', 'API',
        'Sin términos públicos', 'Citar UEFA', FALSE, 'Coeficiente de club; control cruzado de resultados')
ON CONFLICT (id_fuente) DO NOTHING;

ALTER TABLE gold.f_coeficiente_uefa
    ADD COLUMN puntos_club         NUMERIC(7,3),          -- NULL si el club no está en el ranking
    ADD COLUMN piso_asociacion     NUMERIC(7,3),          -- parte de la asociación (nationalAssociationPoints)
    ADD COLUMN es_piso_asociacion  BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN posicion            SMALLINT,
    ADD COLUMN actualizado_en      TIMESTAMPTZ NOT NULL DEFAULT now();

COMMIT;
