# sap-simulator

Simulador de **SAP Business One Service Layer** (`/b1s/v1`) para desarrollar y probar SCNexus sin acceso a SAP.

No reemplaza al middleware: lo que simula es **SAP**. El `api-middleware-sap` real corre contra este simulador, así que toda la lógica del middleware (colas de jobs, idempotencia, reintentos, validaciones) se ejecuta de verdad:

```
front → api-odc-mba3 (:8000) → api-middleware-sap (:8011) → sap-simulator (:50001)
```

## Requisitos

- Docker.
- El SQL Server local corriendo en el puerto 1433 (`api-core-scnexus/docker-compose.local.yml`).
- El repo `api-middleware-sap` clonado junto a este (`../api-middleware-sap`). No se modifica.

## Uso

```bash
cp .env.example .env        # completar MSSQL_SA_PASSWORD (la misma de api-core-scnexus/.env)
docker compose up -d --build
```

Esto levanta tres contenedores:

| Servicio | Puerto | Qué hace |
|---|---|---|
| `sap-simulator` | 50001 | Service Layer simulado |
| `middleware-db-init` | — | Crea la base `sc_middleware` (una sola vez) y el SP `sp_GetAvailableStock` simulado |
| `api-middleware-sap` | 8011 | Middleware real, configurado contra el simulador |

`api-odc-mba3` ya apunta al middleware en `http://host.docker.internal:8011` (`SAP_MIDDLEWARE_URL`).

## Qué simula

- **Sesión:** `POST /Login` (devuelve `SessionId` y la cookie `B1SESSION`) y `POST /Logout`. Las llamadas sin sesión válida reciben `401`, igual que SAP; el middleware se reconecta solo.
- **OData genérico** sobre cada colección:
  - `GET Coleccion`, `GET Coleccion('clave')`, `GET Coleccion(123)`, `GET Items('X')/ItemPrices`, `GET Coleccion/$count`
  - `$filter` (`eq ne gt ge lt le and or not`, paréntesis, `contains startswith endswith substringof tolower toupper`), `$select`, `$orderby`, `$top`, `$skip`, `$inlinecount`
  - Paginación de 20 en 20 con `odata.nextLink`, como el Service Layer.
  - `POST`, `PATCH` y `DELETE`.
- **Colecciones:**

  | Tipo | Colecciones | Origen |
  |---|---|---|
  | Maestros | `Items`, `Warehouses`, `BusinessPartners`, `ItemGroups`, `PriceLists` | `seed/*.json` |
  | Documentos | `PurchaseOrders`, `InventoryTransferRequests`, `StockTransfers`, `PurchaseDeliveryNotes`, `InventoryGenEntries` | Creados vía `POST` |

  Los documentos reciben `DocEntry`/`DocNum` correlativos y se guardan en el volumen `sap-sim-data`, así que sobreviven reinicios.
- **Validaciones tipo SAP al crear documentos:** proveedor existente y activo, artículos existentes, activos y comprables, cantidades mayores a 0 y bodegas existentes. Los errores usan el formato del Service Layer (`{"error": {"code", "message": {"value"}}}`).
- **Errores:** lo que no está simulado responde `404` y se registra en el log como `NO SIMULADO`.

## Datos semilla

Coinciden con la plantilla de prueba `api-odc-mba3/docs/examples/plantilla_pedido_prueba.xlsx`:

- **8 artículos** `R900x…`. Cada uno trae pack (`U_IXX_PACK`), marca, código de barras, PVP (lista de precios 1 "Precio Minorista") y costo (lista de precios 2).
- **Bodegas** `RL-CD`, `OUT-CD`, `RG-CD` y **tiendas** `TRL-*`, `TOUT-*`, `RG-*`.
- **Proveedor** `P1790389219001` (TEXTILES EL RAYO S.A.) y uno congelado (`P0000000000001`) para probar rechazos.

El stock por bodega se genera automáticamente con `DEFAULT_STOCK` (1000). Para cambiar los datos, edita `seed/*.json` y ejecuta `docker compose up -d --build sap-simulator`.

## Endpoints de apoyo (no existen en SAP)

| Endpoint | Para qué |
|---|---|
| `GET /health` | Estado y cantidad de registros por colección |
| `GET /sim/bitacora` | Últimas llamadas recibidas: qué le pidió el middleware a "SAP" |
| `GET /sim/documentos/{coleccion}` | Documentos creados, por ejemplo `PurchaseOrders` |
| `POST /sim/reiniciar` | Borra los documentos creados y recarga la semilla |
| `GET /docs` | Swagger del simulador |

## Ampliar el simulador

- **Nueva colección maestra:** agrégala en `COLECCIONES` y `MAESTROS_SEED` (`app/store.py`) y crea su `seed/*.json`.
- **Nuevo tipo de documento:** agrégalo en `COLECCIONES` con `documento=True` y el nombre de su colección de líneas.
- **Buscar qué falta:** revisa `GET /sim/bitacora` o el log (`NO SIMULADO`).

## Tests

```bash
docker run --rm -v "$PWD:/srv" -w /srv -e SEED_DIR=/srv/seed python:3.11-slim \
  sh -c "pip install -q -r requirements.txt pytest httpx && python -m pytest -q tests"
```

## Notas

- El middleware carga `appsettings.Development.local.json` **después** de las variables de entorno, y ese archivo viaja dentro de su imagen porque su `.dockerignore` no lo excluye. Por eso `docker-compose.yml` monta encima `config/middleware.appsettings.local.json`, que apunta al simulador.
- Stock por SQL (`/items/stock/batch/sql`): el SP simulado lee `sc_middleware.dbo.stock_simulado` y, si no hay fila, devuelve 1000.
- No simulado todavía: la capa semántica `sml.svc` (stock por lote de la vista `STOCK_BODEGA`), WebPOS/Hitell, depósitos y liquidaciones.
