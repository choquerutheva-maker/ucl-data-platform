/* =============================================================================
   UCL Data Platform - silver.espn_prob_externa y silver.nike_pronosticos
   Descripción : Probabilidades que NO son cuotas de apuestas:
                   - espn_prob_externa: % de victoria de modelos externos que
                     publica ESPN (p. ej. 'bloomberg', único dato en 2016-17).
                   - nike_pronosticos: pronósticos 1X2 diarios de Niké, para
                     calibrarlos contra el cierre del mercado (msg #33 / #35).
                 Ambas se mapean a gold.f_pronostico vía gold.d_modelo.
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-10-22
   Notas       :
     - El cruce con silver.espn_cuotas es por espn_event_id.
     - bloomberg no publica el empate: prob_empate queda NULL y se deriva
       en gold como 1 - prob_local - prob_visita.
     - Privadas: RLS activo sin políticas (no se exponen en la API).
============================================================================= */

BEGIN;

-- Una fila por partido y proveedor (la probabilidad no cambia entre capturas)
CREATE TABLE silver.espn_prob_externa (
    id_prob          BIGSERIAL PRIMARY KEY,
    id_ingesta       BIGINT REFERENCES bronze.ingesta (id_ingesta),
    espn_event_id    TEXT NOT NULL,
    liga             TEXT NOT NULL,
    inicio_utc       TIMESTAMPTZ NOT NULL,
    local_espn_id    TEXT NOT NULL,
    visita_espn_id   TEXT NOT NULL,
    proveedor_id     TEXT NOT NULL,
    proveedor        TEXT NOT NULL,
    prob_local       NUMERIC(6,4) NOT NULL CHECK (prob_local  BETWEEN 0 AND 1),
    prob_visita      NUMERIC(6,4) NOT NULL CHECK (prob_visita BETWEEN 0 AND 1),
    prob_empate      NUMERIC(6,4)          CHECK (prob_empate BETWEEN 0 AND 1),
    capturado_en     TIMESTAMPTZ NOT NULL,
    UNIQUE (espn_event_id, proveedor_id),
    CHECK (prob_local + prob_visita + COALESCE(prob_empate, 0) <= 1.005)
);

ALTER TABLE silver.espn_prob_externa ENABLE ROW LEVEL SECURITY;

-- Pronósticos de Niké: puede haber varios por partido (se re-pronostica);
-- para evaluar se toma el último con pronosticado_en < inicio del partido.
CREATE TABLE silver.nike_pronosticos (
    id_pronostico_nike  BIGSERIAL PRIMARY KEY,
    espn_event_id       TEXT NOT NULL,
    pronosticado_en     TIMESTAMPTZ NOT NULL,
    prob_local          NUMERIC(6,4) NOT NULL CHECK (prob_local  BETWEEN 0 AND 1),
    prob_empate         NUMERIC(6,4) NOT NULL CHECK (prob_empate BETWEEN 0 AND 1),
    prob_visita         NUMERIC(6,4) NOT NULL CHECK (prob_visita BETWEEN 0 AND 1),
    goles_esp_local     NUMERIC(4,2) CHECK (goles_esp_local  >= 0),
    goles_esp_visita    NUMERIC(4,2) CHECK (goles_esp_visita >= 0),
    version_metodo      TEXT NOT NULL,
    comentario          TEXT,
    cargado_en          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (espn_event_id, version_metodo, pronosticado_en),
    CHECK (ABS(prob_local + prob_empate + prob_visita - 1) < 0.005)
);

CREATE INDEX ix_nike_pronosticos_evento ON silver.nike_pronosticos (espn_event_id, pronosticado_en);

ALTER TABLE silver.nike_pronosticos ENABLE ROW LEVEL SECURITY;

COMMIT;
