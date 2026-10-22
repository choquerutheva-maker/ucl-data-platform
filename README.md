# UCL Data Platform

Plataforma de datos de la UEFA Champions League: ingesta automatizada, modelo
dimensional en PostgreSQL (Supabase), modelo de pronóstico y dashboard público.

> En construcción.

## Arquitectura

```
Fuentes (openfootball, football-data.org, ClubElo, ESPN, ...)
   │  Python + GitHub Actions
   ▼
bronze  → payload crudo (privado)
silver  → datos limpios por fuente (privado)
gold    → modelo estrella, lectura pública
   ▼
Modelo de pronóstico (gold.f_pronostico) · Dashboard
```

## Estructura

| Ruta | Contenido |
|---|---|
| `sql/ddl/` | Scripts de base de datos, en orden numérico |
| `src/ingesta/` | Scripts de captura por fuente |
| `.github/workflows/` | Ejecución programada |

## Puesta en marcha

1. Crear un proyecto en Supabase y ejecutar `sql/ddl/*.sql` en orden (SQL Editor).
2. En Supabase: *Settings → API → Exposed schemas*, agregar `gold`.
3. En GitHub: *Settings → Secrets → Actions*, crear `DATABASE_URL` con la cadena de
   conexión de Supabase (Session pooler).
4. Captura manual local (sin `DATABASE_URL` guarda en `data/`):

   ```
   pip install -r requirements.txt
   python src/ingesta/espn_cuotas.py
   python src/ingesta/espn_cuotas.py --desde 2024-09-17 --hasta 2025-05-31
   ```

## Fuentes y atribución

El catálogo completo, con licencia y atribución, está en `gold.d_fuente`. Los datos
de fuentes sin licencia de redistribución (ESPN, API-Football) se usan solo para
calcular variables y agregados; no se republican crudos.
