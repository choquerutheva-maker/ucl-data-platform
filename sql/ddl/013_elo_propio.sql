/* =============================================================================
   UCL Data Platform - Elo propio en gold.f_elo_historico
   Descripción : La tabla se pensó para ClubElo (api.clubelo.com), que está caído
                 desde el 22-10-2026 y nunca se cargó. Se agrega la columna
                 fuente para guardar el Elo propio (src/modelo/elo_ligas.py,
                 publicado por src/modelo/publicar_pronosticos.py) sin mezclarlo
                 con ClubElo si algún día vuelve.
   Autor       : Ruth Choque (con PROMETHEUS)
   Fecha       : 2026-11-01
   Notas       :
     - Una fila por club y tramo: elo = rating después de los partidos de
       fecha_desde, vigente hasta fecha_hasta; el tramo actual termina en
       9999-12-31.
     - ranking queda NULL para la fuente propia (cambia con cada partido de
       cualquier club; se calcula al consultar).
     - Lectura pública: la política y el GRANT de gold ya cubren la tabla.
============================================================================= */

BEGIN;

ALTER TABLE gold.f_elo_historico
    ADD COLUMN fuente TEXT NOT NULL DEFAULT 'clubelo' CHECK (fuente IN ('clubelo', 'propio'));

ALTER TABLE gold.f_elo_historico DROP CONSTRAINT f_elo_historico_pkey;
ALTER TABLE gold.f_elo_historico ADD PRIMARY KEY (fuente, id_equipo, fecha_desde);

COMMIT;
