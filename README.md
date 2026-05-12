# Instaleap Control Tower Bot

Bot de terminal para gestión y asignación automática de pedidos en Instaleap Control Tower, con integración al dashboard de Karri.

---

## Diagrama de funcionamiento

```
╔══════════════════════════════════════════════════════════════════════════════════╗
║                      INSTALEAP CONTROL TOWER BOT — DIAGRAMA                   ║
╚══════════════════════════════════════════════════════════════════════════════════╝

┌─ ARRANQUE ───────────────────────────────────────────────────────────────────────┐
│                                                                                  │
│   ./instaleap_bot                                                                │
│        │                                                                         │
│        ├─► Playwright lanza Chromium (visible o headless)                        │
│        │                                                                         │
│        ├─► LOGIN INSTALEAP ──────────────────────────────────────────────────►  │
│        │    control.instaleap.io  →  Auth0  →  captura JWT Bearer               │
│        │                                                                         │
│        └─► LOGIN KARRI (automático) ───────────────────────────────────────►    │
│             Nueva pestaña  →  dashboard-walmart.karri.com.mx                    │
│             Intercepta Authorization header  →  cierra pestaña                  │
│             Descarga /v1/locations  →  604 tiendas  (locationId → nombre)       │
│             Descarga /v1/shoppers?status=READY  →  ~12 shoppers                 │
│             Descarga /v1/shoppers?status=FREE   →  ~6300 shoppers               │
│             Construye índice:  { phone → {status, locationName} }               │
│             Token se renueva automáticamente cada 55 min                        │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─ MENÚ PRINCIPAL ─────────────────────────────────────────────────────────────────┐
│                                                                                  │
│  📋 Actualizar pedidos    📅 Ver por fecha    🔍 Asignar    ▶/⏹ Auto-asignación  │
│                                                                                  │
└──────┬──────────────────────┬────────────────────┬──────────────────────────────┘
       │                      │                    │
       ▼                      ▼                    ▼
┌─ PEDIDOS ──────┐   ┌─ PEDIDOS ──────┐   ┌─ ASIGNACIÓN MANUAL ────────────────┐
│                │   │  POR FECHA     │   │                                    │
│ Slots actuales │   │                │   │ 1. Elige pedido de la lista        │
│ hora + 2 sig.  │   │ Hoy / Mañana   │   │                                    │
│                │   │ / Otra fecha   │   │ 2. fetch_shoppers()                │
│ API por hora:  │   │                │   │    ├─ Nebula API (directo)         │
│ /hela/api/v2/  │   │ Itera 06–22h   │   │    ├─ Navegar + interceptar red    │
│ jobs?status=   │   │ misma API      │   │    └─ DOM scraping (fallback)      │
│ CREATED        │   │                │   │                                    │
│ &operative_    │   │                │   │ 3. Filtra: solo KARRI en nombre    │
│ models=        │   │                │   │                                    │
│ FULL_SERVICE,  │   │                │   │ 4. Cruza con índice Karri          │
│ PICK_AND_...   │   │                │   │    por phone_number                │
│                │   │                │   │    → añade karri_status            │
│                │   │                │   │    → añade karri_location          │
│                │   │                │   │                                    │
│ Tabla muestra: │   │ Tabla muestra: │   │ 5. Tabla shoppers:                 │
│ Ref · Tienda   │   │ Ref · Tienda   │   │    Nombre · Tel · Karri · Tienda   │
│ Slot · Fecha   │   │ Slot · Fecha   │   │    Karri · Dist · Status · Pedidos │
│ Cliente · Tel  │   │ Cliente · Tel  │   │    Vehículo · Cupo                 │
│ Items · Pago   │   │ Items · Pago   │   │                                    │
│ Estado         │   │ Estado         │   │ 6. Menú: elige shopper             │
│                │   │                │   │    Muestra: READY/FREE + tienda    │
└────────────────┘   └────────────────┘   │                                    │
                                          │ 7. assign_shopper()                │
                                          │    ├─ API directa (preferido)      │
                                          │    │   POST /odin/api/job/         │
                                          │    │   {odin_job_id}/task/         │
                                          │    │   {task_id}/                  │
                                          │    │   manualAssignation/          │
                                          │    │   {resource_id}               │
                                          │    └─ Clic en browser (fallback)   │
                                          │                                    │
                                          │ Para resolver odin_job_id:         │
                                          │  0. Caché                          │
                                          │  1. GET /odin/api/jobs?ref=...     │
                                          │  2. Interceptar red en métricas    │
                                          │  3. Click DOM + extraer del URL    │
                                          └────────────────────────────────────┘

┌─ AUTO-ASIGNACIÓN (background asyncio.Task) ──────────────────────────────────────┐
│                                                                                  │
│   ┌─ cada 30 segundos ──────────────────────────────────────────────────────┐   │
│   │                                                                         │   │
│   │  1. Renueva índice Karri (si token expiró, re-login automático)         │   │
│   │                                                                         │   │
│   │  2. Fetch pedidos CREATED  hora actual + 2 slots siguientes             │   │
│   │                                                                         │   │
│   │  3. Por cada pedido no asignado en esta sesión:                         │   │
│   │                                                                         │   │
│   │     ┌─ Nebula API → shoppers del pedido ──────────────────────────┐    │   │
│   │     │                                                              │    │   │
│   │     │  Filtro 1: nombre contiene "karri"                          │    │   │
│   │     │  Filtro 2: wants_to_receive_tasks = true  (activo)          │    │   │
│   │     │  Filtro 3: distancia ≤ 4,000 m                              │    │   │
│   │     │  Filtro 4: phone en índice Karri  (si Karri activo)         │    │   │
│   │     │                                                              │    │   │
│   │     │  Vehículo según items:                                       │    │   │
│   │     │   ≤ 15 items → pool Motos  (fallback: Autos)                │    │   │
│   │     │   > 15 items → pool Autos únicamente                        │    │   │
│   │     │                                                              │    │   │
│   │     │  Score = 0.6×(dist/4000) + 0.3×(pedidos/10) + karri_bonus  │    │   │
│   │     │          READY → bonus 0.0  (prioridad máxima)              │    │   │
│   │     │          FREE  → bonus 0.1                                  │    │   │
│   │     │                                                              │    │   │
│   │     │  Asigna: POST manualAssignation al de menor score           │    │   │
│   │     └──────────────────────────────────────────────────────────────┘    │   │
│   │                                                                         │   │
│   │  4. Marca referencia como asignada (no reasigna en la misma sesión)     │   │
│   │                                                                         │   │
│   └─────────────────────────────────────────────────────────────────────────┘   │
│                                                                                  │
│   ▶ Activar  →  start_auto_assign()  crea asyncio.Task                          │
│   ⏹ Detener  →  stop_auto_assign()   setea asyncio.Event, espera Task           │
│   Al salir   →  stop_auto_assign()   se llama automáticamente                   │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘

┌─ APIs EXTERNAS ──────────────────────────────────────────────────────────────────┐
│                                                                                  │
│  INSTALEAP  control.instaleap.io / avt-backend.instaleap.io                     │
│  ├─ /hela/api/v2/jobs           pedidos CREATED por slot horario                │
│  ├─ /nebula/resources/          shoppers disponibles para un pedido             │
│  │   can-take-task/v2/{task_id}                                                  │
│  ├─ /odin/api/jobs              búsqueda de odin_job_id por referencia          │
│  └─ /odin/api/job/…/            asignación manual de shopper                   │
│       manualAssignation/…                                                        │
│                                                                                  │
│  KARRI  karri-walmart-apigateway-5g8g1c06.uk.gateway.dev                        │
│  ├─ /v1/locations               catálogo de 604 tiendas/geocercas               │
│  ├─ /v1/shoppers?status=READY   shoppers en cola (con locationId)               │
│  └─ /v1/shoppers?status=FREE    shoppers disponibles globalmente                │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

---

## Requisitos

- macOS (ARM64)
- Python 3.10+
- Playwright con Chromium instalado

## Ejecución

### Binario compilado (recomendado)
```bash
cd dist_final
./instaleap_bot
# o doble clic en "Abrir Bot.command"
```

### Desde fuente (desarrollo)
```bash
cd instaleap_bot
.venv/bin/python instaleap_bot.py
```

## Modelos operativos cubiertos

- `FULL_SERVICE`
- `PICK_AND_DELIVERY_WITH_STORAGE_NO_TRANSFER`

## Algoritmo de auto-asignación

| Factor | Peso |
|---|---|
| Distancia al pedido (≤ 4 km) | 60% |
| Pedidos ya asignados al shopper | 30% |
| Estado Karri (READY = 0, FREE = 0.1) | 10% |

**Regla de vehículo:**
- ≤ 15 items → Motocicleta preferida; si no hay, Auto
- \> 15 items → Auto exclusivamente
