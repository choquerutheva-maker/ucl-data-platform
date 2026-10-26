# Pronósticos de Niké

Un CSV por fecha de partidos, con nombre `AAAA-MM-DD_<jornada>.csv` (p. ej.
`2026-11-03_j4.csv`), en UTF-8 y separado por comas. Una fila por partido y
pronóstico. Para revisar un pronóstico (p. ej. con el once oficial) se agrega
una fila nueva con otro `pronosticado_en`; las filas ya cargadas no se cambian.

Hay que hacer commit y push **antes del inicio del partido**. El workflow
`carga-nike-pronosticos` valida y carga en `silver.nike_pronosticos`, y marca
`es_tardio` si el pronóstico o la carga son posteriores al inicio.

Validación local, sin base de datos:

    py src/ingesta/nike_pronosticos.py --archivo entradas/nike/2026-11-03_j4.csv

| Columna | Obligatoria | Formato |
|---|---|---|
| `espn_event_id` | sí | id del evento en ESPN (p. ej. `401915311`) |
| `pronosticado_en` | sí | ISO 8601 con zona horaria (p. ej. `2026-11-02T20:00-04:00`) |
| `prob_local`, `prob_empate`, `prob_visita` | sí | 0..1, suman 1 (± 0,005) |
| `version_metodo` | sí | texto corto (p. ej. `nike_v1`) |
| `goles_esp_local`, `goles_esp_visita` | no | número ≥ 0 |
| `prob_over_2_5`, `prob_btts` | no | 0..1 |
| `importancia_local`, `importancia_visita`, `confianza` | no | `ALTA`, `MEDIA` o `BAJA` |
| `tipo_once` | no | `PROBABLE` u `OFICIAL` |
| `once_local`, `once_visita` | no | jugadores separados por `;` |
| `comentario` | no | texto libre (entre comillas si tiene comas) |

Ejemplo:

```csv
espn_event_id,pronosticado_en,prob_local,prob_empate,prob_visita,goles_esp_local,goles_esp_visita,prob_over_2_5,prob_btts,importancia_local,importancia_visita,confianza,tipo_once,once_local,once_visita,version_metodo,comentario
401915311,2026-11-02T20:00-04:00,0.45,0.27,0.28,1.55,1.20,0.52,0.55,ALTA,MEDIA,MEDIA,PROBABLE,,,nike_v1,"Ejemplo, no es un pronóstico real"
```
