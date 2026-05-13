#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║         INSTALEAP CONTROL TOWER BOT  v1.2               ║
║    Gestión de Pedidos · Asignación de Shoppers          ║
╚══════════════════════════════════════════════════════════╝

Funcionalidades:
  • Login automático con Auth0
  • Pedidos CREADOS del slot actual + 2 horas siguientes
  • Pedidos CREADOS por fecha elegida (todos los slots, via API)
  • Filtro por tienda
  • Listado de shoppers (activos e inactivos)
  • Asignación de shopper a pedido
"""

import asyncio
import sys
import re
import json
import urllib.parse
import os
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any


# ─── Verificación de dependencias ──────────────────────────────────────────────

def _check_deps() -> None:
    missing: list[str] = []
    extras: list[str] = []

    for pkg in ("playwright", "rich", "questionary"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if missing:
        print("❌  Faltan dependencias:", ", ".join(missing))
        print(f"\n  pip install {' '.join(missing)}")
        if "playwright" in missing:
            extras.append("  playwright install chromium")
        for line in extras:
            print(line)
        sys.exit(1)


_check_deps()

# ─── Imports (garantizados tras check) ────────────────────────────────────────

from playwright.async_api import (
    async_playwright,
    Page,
    Browser,
    BrowserContext,
)
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.rule import Rule
import questionary
from questionary import Style as QStyle

# ─── Configuración ─────────────────────────────────────────────────────────────

BASE_URL          = "https://control.instaleap.io"
METRICS_URL       = f"{BASE_URL}/metrics/order-status"
API_JOBS_URL      = "https://avt-backend.instaleap.io/hela/api/v2/jobs"
API_SHOPPERS_URL  = "https://avt-backend.instaleap.io/odin/api/capacity/retrieve/stores"
API_ASSIGN_URL    = "https://avt-backend.instaleap.io/odin/api/job"

CREDS = {
    "email":    "sebastian.opazo@karri.com.mx",
    "password": "sebastian.opazo",
}

BOT_STYLE = QStyle([
    ("qmark",       "fg:#00c853 bold"),
    ("question",    "bold"),
    ("answer",      "fg:#00c853 bold"),
    ("pointer",     "fg:#00c853 bold"),
    ("highlighted", "fg:#00c853 bold"),
    ("selected",    "fg:#00c853"),
    ("separator",   "fg:#444444"),
    ("instruction", "fg:#888888"),
    ("disabled",    "fg:#555555 italic"),
])

console = Console()

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instaleap_bot.log")


def _write_log(msg: str) -> None:
    """Escribe una línea al archivo de log (sin markup Rich)."""
    try:
        plain = re.sub(r"\[/?[a-zA-Z0-9 _#]*\]", "", msg).strip()
        if plain:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {plain}\n")
    except Exception:
        pass


# ─── Bot ───────────────────────────────────────────────────────────────────────

class ControlTowerBot:
    """Automatiza la gestión de pedidos en Instaleap Control Tower."""

    def __init__(self, headless: bool = False) -> None:
        self.headless = headless
        self._pw          = None
        self.browser: Optional[Browser]        = None
        self.ctx:     Optional[BrowserContext] = None
        self.page:    Optional[Page]           = None
        self._auth_token: Optional[str]        = None
        self._api_schema_logged: bool          = False
        self._last_shoppers: List[Dict]        = []   # caché de shoppers para asignación
        self._auto_assign_task: Optional[asyncio.Task]  = None
        self._auto_assign_stop: Optional[asyncio.Event] = None
        self._bg_refresh_task: Optional[asyncio.Task]   = None
        self._bg_refresh_stop: Optional[asyncio.Event]  = None
        self._cached_orders: List[Dict]                 = []
        self._orders_updated_at: str                    = ""
        self._karri_token: Optional[str]        = None
        self._karri_phone_index: Dict[str, Dict] = {}   # phone → {status, locationId, …}
        self._karri_locations: Dict[int, str]    = {}   # locationId → nombre de tienda
        self._karri_email: Optional[str]      = "francisco.martinez@karri.com.mx"
        self._karri_password: Optional[str]   = "Karri.2027"
        self._karri_token_at: float           = 0.0     # timestamp del último login Karri
        self._pending_log: List[str]          = []      # mensajes de background, se muestran entre iteraciones del menú

    def _log(self, msg: str) -> None:
        """Acumula un mensaje de background y lo escribe al archivo de log."""
        self._pending_log.append(msg)
        _write_log(msg)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "ControlTowerBot":
        await self._start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._stop()

    async def _start(self) -> None:
        self._pw     = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=self.headless,
            slow_mo=80,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self.ctx  = await self.browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        self.page = await self.ctx.new_page()
        # Interceptar requests a avt-backend para capturar el JWT (async, garantizado)
        await self.page.route(
            "https://avt-backend.instaleap.io/**",
            self._route_capture_token,
        )

    async def _stop(self) -> None:
        try:
            if self.browser:
                await self.browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    # ── Captura de JWT ─────────────────────────────────────────────────────────

    @staticmethod
    def _is_valid_jwt(token: str) -> bool:
        """Un JWT real comienza con 'eyJ' y tiene >100 caracteres."""
        return bool(token) and token.startswith("eyJ") and len(token) > 100

    async def _route_capture_token(self, route: Any) -> None:
        """Route handler async: captura el JWT y deja pasar el request.
        Solo acepta tokens que parecen JWTs reales (eyJ…).
        """
        try:
            auth = route.request.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                candidate = auth[7:]
                if self._is_valid_jwt(candidate):
                    self._auth_token = candidate
        except Exception:
            pass
        await route.continue_()

    async def _extract_token_from_page(self) -> Optional[str]:
        """Extrae el JWT del localStorage/sessionStorage de Auth0."""
        try:
            return await self.page.evaluate("""
                () => {
                    const storages = [localStorage, sessionStorage];
                    for (const st of storages) {
                        for (const key of Object.keys(st)) {
                            try {
                                const raw = st.getItem(key);
                                if (!raw) continue;
                                // JWT directo
                                if (raw.startsWith('eyJ') && raw.length > 200) return raw;
                                // JSON con access_token (Auth0 SPA SDK v1/v2)
                                if (!raw.includes('access_token')) continue;
                                const obj = JSON.parse(raw);
                                const at = (
                                    obj?.access_token ||
                                    obj?.body?.access_token ||
                                    obj?.decodedToken?.user && obj?.access_token
                                );
                                if (at && String(at).startsWith('eyJ')) return String(at);
                            } catch(e) {}
                        }
                    }
                    return null;
                }
            """)
        except Exception:
            return None

    async def _ensure_token(self) -> bool:
        """Garantiza que tenemos un JWT válido. Si la sesión expiró, re-hace login."""
        if self._auth_token and not self._is_valid_jwt(self._auth_token):
            self._auth_token = None

        if self._auth_token:
            return True

        # Intento 1: recargar métricas para disparar requests autenticados
        await self._goto(METRICS_URL)
        for _ in range(40):
            if self._auth_token and self._is_valid_jwt(self._auth_token):
                return True
            await asyncio.sleep(0.3)

        # Intento 2: localStorage / sessionStorage
        token = await self._extract_token_from_page()
        if token and self._is_valid_jwt(token):
            self._auth_token = token
            return True

        # Intento 3: sesión expirada → re-login completo automático
        self._log("  [yellow]  Sesión Instaleap expirada — re-login automático...[/yellow]")
        try:
            await self.login()
        except Exception as exc:
            self._log(f"  [red]  Re-login falló: {exc}[/red]")

        if not self._auth_token:
            self._log("  [red]  No se pudo renovar la sesión de Instaleap.[/red]")
        return bool(self._auth_token)

    # ── Login ──────────────────────────────────────────────────────────────────

    async def login(self) -> None:
        """Inicia sesión en Control Tower via Auth0."""
        # Limpiar cookies y storage para evitar redirecciones de sesiones anteriores
        await self.ctx.clear_cookies()
        await self.page.goto("about:blank")
        try:
            await self.page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
        except Exception:
            pass

        await self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        await self.page.wait_for_load_state("networkidle", timeout=30_000)

        # Paso 1: email
        email_sel = 'input[type="email"], input[name="email"], input[type="text"]'
        await self.page.wait_for_selector(email_sel, timeout=15_000)
        await self.page.fill(email_sel, "")          # limpiar campo antes de escribir
        await self.page.fill(email_sel, CREDS["email"])
        await self.page.click(
            'button[type="submit"], button:has-text("Continuar"), button:has-text("Continue")'
        )
        await self.page.wait_for_load_state("networkidle", timeout=20_000)

        # Paso 2: password (Auth0)
        pwd_sel = 'input[type="password"], input[name="password"]'
        await self.page.wait_for_selector(pwd_sel, timeout=20_000)
        await self.page.fill(pwd_sel, CREDS["password"])
        await self.page.click(
            'button[type="submit"], button:has-text("Continuar"), button:has-text("Continue")'
        )

        try:
            await self.page.wait_for_url(f"{BASE_URL}/**", timeout=30_000)
        except Exception:
            pass
        await self.page.wait_for_load_state("networkidle", timeout=30_000)

        # Navegar a métricas para disparar requests autenticados y capturar el JWT
        await self._goto(METRICS_URL)
        for _ in range(40):
            if self._auth_token and self._is_valid_jwt(self._auth_token):
                break
            await asyncio.sleep(0.3)
        # Fallback: localStorage / sessionStorage
        if not (self._auth_token and self._is_valid_jwt(self._auth_token)):
            self._auth_token = None
            token = await self._extract_token_from_page()
            if token and self._is_valid_jwt(token):
                self._auth_token = token

    # ── Helpers de slot ────────────────────────────────────────────────────────

    @staticmethod
    def current_slots() -> List[Dict[str, Any]]:
        """Devuelve el slot actual y los 2 siguientes."""
        now = datetime.now()
        slots = []
        for i in range(3):
            h = (now + timedelta(hours=i)).hour
            slots.append({
                "hour":     h,
                "slot_str": f"{h:02d}:00-{h:02d}:59",
                "label":    f"{h:02d}:00 – {h:02d}:59",
            })
        return slots

    # ── Navegación ─────────────────────────────────────────────────────────────

    async def _goto(self, url: str, timeout: int = 20_000) -> None:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        await self.page.wait_for_load_state("networkidle", timeout=timeout)
        await asyncio.sleep(1.5)

    # ── API: pedidos de un slot ────────────────────────────────────────────────

    async def _api_get_jobs(
        self,
        date_str: str,
        hour: int,
        page: int = 1,
        page_size: int = 100,
    ) -> Any:
        """
        Llama directamente al endpoint /hela/api/v2/jobs de Instaleap.
        Ejecuta fetch() desde el contexto del navegador (evita CORS).
        Retorna el JSON crudo o {} si falla.
        """
        slot_from = {
            "start": f"{date_str} {hour:02d}:00:00",
            "end":   f"{date_str} {hour:02d}:00:59",
        }
        slot_to = {
            "start": f"{date_str} {hour:02d}:59:00",
            "end":   f"{date_str} {hour:02d}:59:59",
        }
        # quote_via=quote usa %20 en vez de + para los espacios en las fechas
        params = urllib.parse.urlencode(
            {
                "page":               page,
                "page_size":          page_size,
                "status":             "CREATED",
                "operative_models":   "FULL_SERVICE",
                "slot_from_store_tz": json.dumps(slot_from, separators=(",", ":")),
                "slot_to_store_tz":   json.dumps(slot_to,   separators=(",", ":")),
                "slot_reason":        "STATIC",
            },
            quote_via=urllib.parse.quote,
        )
        url = f"{API_JOBS_URL}?{params}"

        # Playwright APIRequestContext: HTTP directo, sin CORS
        try:
            response = await self.ctx.request.get(
                url,
                headers={
                    "Authorization": f"Bearer {self._auth_token}",
                    "Accept": "application/json",
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/",
                },
            )
            if not response.ok:
                return {"_http_error": response.status}
            return await response.json()
        except Exception:
            return {}

    # ── Parseo de respuesta de la API ─────────────────────────────────────────

    def _parse_jobs_response(self, data: Any, slot_str: str) -> List[Dict]:
        """Convierte el JSON de la API al formato interno del bot."""
        if not data or not isinstance(data, (dict, list)):
            return []

        # Error HTTP visible
        if isinstance(data, dict) and "_http_error" in data:
            console.print(f"  [red]  API HTTP {data['_http_error']} en slot {slot_str}[/red]")
            return []

        items: Optional[List] = None
        LIST_KEYS = ("data", "jobs", "orders", "items", "results", "content", "records")

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # Nivel superior
            for key in LIST_KEYS:
                val = data.get(key)
                if isinstance(val, list):
                    items = val
                    break
                # Un nivel de anidamiento: {"data": {"jobs": [...]}}
                if isinstance(val, dict):
                    for inner_key in LIST_KEYS:
                        inner = val.get(inner_key)
                        if isinstance(inner, list):
                            items = inner
                            break
                if items:
                    break

        if not items:
            return []

        orders: List[Dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            # Toda la info del pedido vive bajo "nebula_job"
            nebula = item.get("nebula_job") or {}
            custom = item.get("custom_fields") or {}
            store_tz = custom.get("store_tz_dates") or {}

            # ── Referencia: nebula_job.job_number ────────────────────────────
            reference = str(nebula.get("job_number") or item.get("id") or "").strip()

            # ── Tienda: nebula_job.store.name ────────────────────────────────
            store_obj = nebula.get("store") or {}
            store = str(store_obj.get("name") or "") if isinstance(store_obj, dict) else ""
            if not store:
                origin_obj = nebula.get("origin") or {}
                store = str(origin_obj.get("name") or "") if isinstance(origin_obj, dict) else ""

            # ── Cliente: custom_fields.receiver_full_name + receiver.phone_number
            client_name  = str(custom.get("receiver_full_name") or "")
            receiver     = nebula.get("receiver") or {}
            if not client_name and isinstance(receiver, dict):
                fn = str(receiver.get("first_name") or "")
                ln = str(receiver.get("last_name") or "")
                client_name = f"{fn} {ln}".strip()
            client_phone = str(receiver.get("phone_number") or "") if isinstance(receiver, dict) else ""

            # ── Fecha creación: custom_fields.store_tz_dates.created_at (hora local)
            creation = str(store_tz.get("created_at") or nebula.get("created_at") or "")
            if "T" in creation:
                creation = creation.replace("T", " ")[:19]
            elif creation:
                creation = creation[:19]

            # ── Slot display: custom_fields.store_tz_dates.slot (hora local)
            slot_tz_data = store_tz.get("slot") or {}
            from_raw = str(slot_tz_data.get("from") or "")
            to_raw   = str(slot_tz_data.get("to")   or "")
            if len(from_raw) >= 16 and len(to_raw) >= 16:
                display_slot = f"{from_raw[11:16]}-{to_raw[11:16]}"
            else:
                display_slot = slot_str

            # ── Pago: nebula_job.payment_info.payment ────────────────────────
            payment_info = nebula.get("payment_info") or {}
            pay_obj      = payment_info.get("payment") or {}
            if isinstance(pay_obj, dict):
                details = str(pay_obj.get("payment_status_details") or "")
                method  = str(pay_obj.get("method") or "")
                payment = details if details else method
            else:
                payment = ""

            # ── Estado / order_id ─────────────────────────────────────────────
            status    = str(nebula.get("status") or custom.get("partial_status") or "Creado")
            order_id  = str(item.get("id") or reference)
            order_uuid = str(item.get("id") or "")

            # ── Coordenadas de origen ─────────────────────────────────────────
            origin_obj = nebula.get("origin") or {}
            origin_lat = float(origin_obj.get("latitude")  or 0.0) if isinstance(origin_obj, dict) else 0.0
            origin_lon = float(origin_obj.get("longitude") or 0.0) if isinstance(origin_obj, dict) else 0.0

            # ── Store ID (para endpoint nebula de shoppers) ───────────────────
            store_obj = nebula.get("store") or {}
            store_id  = str(store_obj.get("id") or "") if isinstance(store_obj, dict) else ""

            # ── Tareas (para API de asignación y de shoppers) ─────────────────
            task_list = nebula.get("tasks") or []
            tasks = [
                {"id": str(t.get("id") or ""), "type": str(t.get("type") or "")}
                for t in task_list
                if isinstance(t, dict) and t.get("id")
            ]

            # ── Items del pedido ──────────────────────────────────────────────
            _items_raw = (
                nebula.get("products")
                or nebula.get("items")
                or nebula.get("order_items")
                or custom.get("products")
                or custom.get("items")
                or None
            )
            if isinstance(_items_raw, list):
                items_count = str(len(_items_raw))
            elif _items_raw is not None:
                items_count = str(_items_raw)
            else:
                _num = (
                    nebula.get("num_products")
                    or nebula.get("num_items")
                    or nebula.get("total_items")
                    or custom.get("num_products")
                    or custom.get("total_items")
                )
                items_count = str(_num) if _num is not None else "-"

            if not reference:
                continue

            orders.append({
                "reference":    reference,
                "store":        store,
                "creation":     creation,
                "client_name":  client_name,
                "client_phone": client_phone,
                "payment":      payment,
                "delivery":     display_slot,
                "status":       status,
                "slot":         slot_str,
                "order_id":     order_id,
                "order_uuid":   order_uuid,
                "origin_lat":   origin_lat,
                "origin_lon":   origin_lon,
                "store_id":     store_id,
                "tasks":        tasks,
                "odin_job_id":  "",
                "items_count":  items_count,
            })

        return orders

    # ── Obtención de pedidos ───────────────────────────────────────────────────

    async def fetch_orders_for_slot(self, slot: Dict[str, Any]) -> List[Dict]:
        """Pedidos CREADOS de un slot del día actual via API."""
        await self._ensure_token()
        date_str = datetime.now().strftime("%Y-%m-%d")
        data     = await self._api_get_jobs(date_str, slot["hour"])
        return self._parse_jobs_response(data, slot["slot_str"])

    async def fetch_all_orders_for_date(self, target_date: datetime) -> List[Dict]:
        """
        Todos los pedidos CREADOS de un día completo via API.
        Itera cada hora del rango de negocio (06:00 – 22:59).
        """
        await self._ensure_token()
        if self._auth_token:
            preview = f"…{self._auth_token[-12:]}"
            console.print(f"  [dim]  JWT capturado ({preview})[/dim]")
        else:
            console.print("  [red]  Sin JWT — todas las llamadas fallarán[/red]")
            return []

        date_str   = target_date.strftime("%Y-%m-%d")
        all_orders: List[Dict] = []

        for h in range(6, 23):
            slot_str = f"{h:02d}:00-{h:02d}:59"
            data     = await self._api_get_jobs(date_str, h)
            orders   = self._parse_jobs_response(data, slot_str)
            all_orders.extend(orders)

        return all_orders

    # ── Shoppers y Asignación (API directa) ───────────────────────────────────

    async def _get_odin_job_id(self, order: Dict) -> Optional[str]:
        """
        Extrae el ID numérico de odin (routing ID) del pedido.
        Estrategia 1: buscar <a href> con /orders/{id} (React link).
        Estrategia 2: hacer clic en la fila y leer el ID del URL resultante.
        """
        reference = order.get("reference", "")
        await self._goto(
            f"{METRICS_URL}?page=1&page_size=200&status=CREATED&operative_models=FULL_SERVICE",
            timeout=25_000,
        )
        await asyncio.sleep(5)

        # Estrategia 1: buscar links <a href="/orders/...">
        odin_id: Optional[str] = await self.page.evaluate(
            """
            (ref) => {
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null
                );
                let node;
                while ((node = walker.nextNode())) {
                    if (node.textContent.trim() !== ref) continue;
                    let el = node.parentElement;
                    while (el && el !== document.body) {
                        if (el.tagName === 'A' && el.href) {
                            const parts = el.href.split('/orders/');
                            if (parts.length > 1) {
                                const m = parts[1].match(/^([0-9]+)/);
                                if (m) return m[1];
                            }
                        }
                        const links = el.querySelectorAll('a[href*="/orders/"]');
                        for (const lk of links) {
                            const parts = lk.href.split('/orders/');
                            if (parts.length > 1) {
                                const m = parts[1].match(/^([0-9]+)/);
                                if (m) return m[1];
                            }
                        }
                        const role = (el.getAttribute('role') || '').toLowerCase();
                        if (el.tagName === 'TR' || role === 'row') break;
                        el = el.parentElement;
                    }
                }
                return null;
            }
            """,
            reference,
        )
        if odin_id:
            return odin_id

        # Estrategia 2: clic en la fila y extraer ID del URL de React Router
        clicked: bool = await self.page.evaluate(
            """
            (ref) => {
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null
                );
                let node;
                while ((node = walker.nextNode())) {
                    if (!node.textContent.includes(ref)) continue;
                    let el = node.parentElement;
                    for (let i = 0; i < 15; i++) {
                        if (!el || el === document.body) break;
                        const tag = el.tagName;
                        const role = (el.getAttribute('role') || '').toLowerCase();
                        if (tag === 'TR' || tag === 'LI' || role === 'row') {
                            el.click();
                            return true;
                        }
                        el = el.parentElement;
                    }
                    // Fallback: clic en el elemento de texto directamente
                    node.parentElement.click();
                    return true;
                }
                return false;
            }
            """,
            reference,
        )

        if clicked:
            try:
                await self.page.wait_for_url("**/orders/**", timeout=12_000)
            except Exception:
                await asyncio.sleep(3)
            url = self.page.url
            if "/orders/" in url:
                parts = url.split("/orders/")
                if len(parts) > 1:
                    m = re.search(r"^([0-9]+)", parts[1])
                    if m:
                        return m.group(1)

        return None

    @staticmethod
    def _get_assignment_task_id(order: Dict) -> Optional[str]:
        """Selecciona la tarea más apropiada del pedido para la asignación de shopper."""
        tasks = order.get("tasks") or []
        if not tasks:
            return None
        # Prioridad de tipos de tarea para asignación de shopper
        priority = [
            "delivery_with_storage",
            "pick_up_for_delivery",
            "picking_and_storage",
            "full_service",
        ]
        for target in priority:
            for t in tasks:
                if target in t.get("type", "").lower().replace(" ", "_"):
                    return t["id"]
        return tasks[0]["id"]   # fallback: primera tarea

    async def _api_get_shoppers_nebula(self, task_id: str, store_id: str) -> List[Dict]:
        """
        GET /nebula/resources/can-take-task/v2/{task_id}?quotaless=true&limit=200&offset=0&store_id={store_id}
        Endpoint oficial que usa la UI de Control Tower para obtener los shoppers disponibles.
        """
        if not task_id or not store_id:
            console.print("  [red]  Falta task_id o store_id para nebula shoppers API.[/red]")
            return []
        url = (
            f"https://avt-backend.instaleap.io/nebula/resources/can-take-task/v2/{task_id}"
            f"?quotaless=true&limit=200&offset=0&store_id={store_id}"
        )
        console.print(f"  [dim]  Nebula shoppers API → task={task_id[:8]}... store={store_id[:8]}...[/dim]")
        try:
            resp = await self.ctx.request.get(
                url,
                headers={
                    "Authorization": f"Bearer {self._auth_token}",
                    "Accept":        "application/json",
                    "Content-Type":  "application/json",
                    "Origin":        BASE_URL,
                    "Referer":       f"{BASE_URL}/",
                },
            )
            console.print(f"  [dim]  Nebula shoppers status: {resp.status}[/dim]")
            if not resp.ok:
                return []
            data = await resp.json()

            # Guardar debug
            try:
                with open("/tmp/instaleap_nebula_shoppers_debug.json", "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

            # Extraer lista de recursos
            resources: List[Dict] = []
            if isinstance(data, list):
                resources = [r for r in data if isinstance(r, dict) and r.get("id")]
            elif isinstance(data, dict):
                for key in ("resources", "data", "items", "results", "can_take"):
                    val = data.get(key)
                    if isinstance(val, list):
                        resources = [r for r in val if isinstance(r, dict) and r.get("id")]
                        break

            if resources:
                first = resources[0]
                console.print(f"  [dim]  Primer recurso keys: {list(first.keys())[:10]}[/dim]")
                name = first.get("name") or first.get("fullName") or first.get("displayName") or "(sin nombre)"
                console.print(f"  [dim]  Primer recurso: {name}[/dim]")

            return resources
        except Exception as exc:
            console.print(f"  [red]  Error nebula shoppers API: {exc}[/red]")
            return []

    async def _api_get_shoppers(self, lat: float, lon: float) -> List[Dict]:
        """
        Obtiene recursos (shoppers) disponibles cerca de una tienda via API directa.
        POST /odin/api/capacity/retrieve/stores con lat/lon y radio de 100 km.
        """
        console.print(f"  [dim]  Shoppers API → lat={lat}, lon={lon}[/dim]")
        if not lat and not lon:
            console.print("  [red]  Coordenadas 0,0 — sin datos de origen en el pedido.[/red]")
            return []
        try:
            resp = await self.ctx.request.post(
                API_SHOPPERS_URL,
                headers={
                    "Authorization": f"Bearer {self._auth_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/",
                },
                data=json.dumps({
                    "limit": 200, "offset": 0,
                    "latitude": lat, "longitude": lon,
                    "radius": 100_000,
                }),
            )
            console.print(f"  [dim]  Shoppers API status: {resp.status}[/dim]")
            if not resp.ok:
                console.print(f"  [red]  Shoppers API HTTP {resp.status}[/red]")
                return []
            data = await resp.json()

            # ── Debug: volcar respuesta cruda ──────────────────────────────────
            debug_path = "/tmp/instaleap_shoppers_debug.json"
            try:
                with open(debug_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                console.print(f"  [dim]  Shoppers raw → {debug_path}[/dim]")
            except Exception:
                pass

            # Mostrar tipo de respuesta
            if isinstance(data, list):
                console.print(f"  [dim]  Respuesta: lista de {len(data)} items[/dim]")
                if data:
                    first = data[0]
                    console.print(f"  [dim]  Keys primer item: {list(first.keys()) if isinstance(first, dict) else type(first)}[/dim]")
            elif isinstance(data, dict):
                console.print(f"  [dim]  Respuesta: dict con keys {list(data.keys())}[/dim]")

            # Extraer recursos de distintas estructuras posibles
            resources: List[Dict] = []

            def _extract_from_store_list(store_list: list) -> List[Dict]:
                """Dado un array de tiendas extrae todos sus recursos."""
                found: List[Dict] = []
                for store in store_list:
                    if not isinstance(store, dict):
                        continue
                    for sub_key in ("resources", "items", "shoppers", "data"):
                        sub = store.get(sub_key)
                        if isinstance(sub, list) and sub:
                            found.extend(sub)
                            break
                    else:
                        if store.get("id"):
                            found.append(store)
                return found

            if isinstance(data, list):
                resources = _extract_from_store_list(data)
            elif isinstance(data, dict):
                # {"stores": [...], "numberOfPages": N}  ← estructura real
                stores_val = data.get("stores")
                if isinstance(stores_val, list):
                    resources = _extract_from_store_list(stores_val)
                else:
                    for key in ("resources", "data", "items", "results", "shoppers"):
                        val = data.get(key)
                        if isinstance(val, list):
                            resources = val
                            break

            valid = [r for r in resources if isinstance(r, dict) and r.get("id")]
            console.print(f"  [dim]  Recursos extraídos: {len(valid)}[/dim]")
            if valid:
                first_r = valid[0]
                name = first_r.get("name") or first_r.get("fullName") or first_r.get("displayName") or "(sin nombre)"
                console.print(f"  [dim]  Primer recurso: id={first_r.get('id')}, name={name}[/dim]")
            return valid
        except Exception as exc:
            console.print(f"  [red]  Error shoppers API: {exc}[/red]")
            return []

    async def _api_assign_shopper(
        self, odin_job_id: str, task_id: str, resource: Dict
    ) -> bool:
        """
        Asigna un shopper a un pedido via API directa.
        POST /odin/api/job/{odin_job_id}/task/{task_id}/manualAssignation/{resource_id}
        """
        url = (
            f"{API_ASSIGN_URL}/{odin_job_id}"
            f"/task/{task_id}"
            f"/manualAssignation/{resource['id']}"
        )
        try:
            resp = await self.ctx.request.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._auth_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/",
                },
                data=json.dumps({"resource": resource}),
            )
            return resp.ok
        except Exception:
            return False

    async def _api_search_odin_job(self, reference: str) -> Optional[str]:
        """
        Busca el odin_job_id en la API de Odin usando la referencia del pedido.
        Intenta varios endpoints comunes del API.
        """
        ODIN_BASE = "https://avt-backend.instaleap.io/odin/api"
        headers = {
            "Authorization": f"Bearer {self._auth_token}",
            "Accept":        "application/json",
            "Origin":        BASE_URL,
            "Referer":       f"{BASE_URL}/",
        }
        candidates = [
            f"{ODIN_BASE}/job?externalId={reference}",
            f"{ODIN_BASE}/job?referenceId={reference}",
            f"{ODIN_BASE}/job?jobNumber={reference}",
            f"{ODIN_BASE}/jobs?referenceId={reference}",
            f"{ODIN_BASE}/jobs?externalId={reference}",
        ]
        for url in candidates:
            try:
                resp = await self.ctx.request.get(url, headers=headers)
                if not resp.ok:
                    continue
                text = await resp.text()
                if not text:
                    continue
                data = json.loads(text)
                # Buscar cualquier ID numérico de 8-12 dígitos en la respuesta
                ids = re.findall(r'["\s:]([0-9]{8,12})[",\s\}]', text)
                if ids:
                    preferred = [i for i in ids if len(i) == 9 and i.startswith("17")]
                    return preferred[0] if preferred else ids[0]
            except Exception:
                pass
        return None

    async def _intercept_odin_id_from_metrics(self, reference: str) -> Optional[str]:
        """
        Navega a la página de métricas e intercepta TODAS las respuestas de API
        para encontrar el odin_job_id del pedido (buscando el número de referencia
        dentro de cualquier respuesta que contenga IDs numéricos de 8-12 dígitos).
        """
        found_id: Optional[str] = None

        async def on_response(response: Any) -> None:
            nonlocal found_id
            if found_id:
                return
            try:
                text = await response.text()
                if not text or reference not in text:
                    return
                # La respuesta contiene la referencia — buscar IDs numéricos de 8-12 dígitos
                ids = re.findall(r'["\s:]([0-9]{8,12})[",\s\}]', text)
                numeric = [i for i in ids if len(i) >= 8 and not i.startswith("528") and not i.startswith("55")]
                if numeric:
                    found_id = numeric[0]
                    console.print(
                        f"  [dim]  odin_job_id hallado en {response.url.split('//')[1][:60]}: {found_id}[/dim]"
                    )
            except Exception:
                pass

        self.page.on("response", on_response)
        await self._goto(
            f"{METRICS_URL}?page=1&page_size=200&status=CREATED&operative_models=FULL_SERVICE",
            timeout=25_000,
        )
        await asyncio.sleep(6)
        self.page.remove_listener("response", on_response)
        return found_id

    async def _navigate_to_reassign(self, order: Dict) -> bool:
        """
        Navega al panel re-assign de un pedido.
        Estrategias (en orden):
          0. API Odin directa para buscar el job por referencia
          1. Interceptar red en la página de métricas y encontrar el odin_job_id
          2. Extraer href con /orders/ de la tabla de métricas (si React los genera)
          3. Clic en la fila de la tabla + wait_for_url
        """
        reference = order.get("reference", "")

        # ── Estrategia 0: usar odin_job_id ya conocido ────────────────────────
        if order.get("odin_job_id"):
            target = f"{BASE_URL}/orders/{order['odin_job_id']}/re-assign"
            await self._goto(target, timeout=20_000)
            return "/orders/" in self.page.url

        # ── Estrategia 1: API Odin directa ───────────────────────────────────
        console.print("  [dim]  Buscando odin_job_id via API Odin...[/dim]")
        odin_id = await self._api_search_odin_job(reference)
        if odin_id:
            order["odin_job_id"] = odin_id
            await self._goto(f"{BASE_URL}/orders/{odin_id}/re-assign", timeout=20_000)
            return "/orders/" in self.page.url

        # ── Estrategia 2: interceptar red en métricas ─────────────────────────
        console.print("  [dim]  Interceptando red en métricas para odin_job_id...[/dim]")
        odin_id = await self._intercept_odin_id_from_metrics(reference)
        if odin_id:
            order["odin_job_id"] = odin_id
            await self._goto(f"{BASE_URL}/orders/{odin_id}/re-assign", timeout=20_000)
            return "/orders/" in self.page.url

        # ── Estrategia 3: href en DOM ─────────────────────────────────────────
        order_url: Optional[str] = None
        for page_num in range(1, 4):
            await self._goto(
                f"{METRICS_URL}?page={page_num}&page_size=100&status=CREATED&operative_models=FULL_SERVICE",
                timeout=25_000,
            )
            await asyncio.sleep(5)

            order_url = await self.page.evaluate(
                """
                (ref) => {
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                    let node;
                    while ((node = walker.nextNode())) {
                        if (!node.textContent.includes(ref)) continue;
                        let el = node.parentElement;
                        while (el && el !== document.body) {
                            if (el.tagName === 'A' && el.href && el.href.includes('/orders/')) {
                                return el.href;
                            }
                            const links = el.querySelectorAll('a[href*="/orders/"]');
                            if (links.length > 0) return links[0].href;
                            const role = (el.getAttribute('role') || '').toLowerCase();
                            if (el.tagName === 'TR' || role === 'row') break;
                            el = el.parentElement;
                        }
                    }
                    return null;
                }
                """,
                reference,
            )
            if order_url:
                break
            has_rows = await self.page.evaluate(
                "() => document.querySelectorAll('tr, [role=\"row\"]').length > 2"
            )
            if not has_rows:
                break

        if order_url:
            target = order_url.rstrip("/")
            if "/re-assign" not in target:
                target += "/re-assign"
            await self._goto(target)
            return "/orders/" in self.page.url

        # ── Fallback: clic en la fila (para el caso en que no haya <a> con href)
        await self._goto(
            f"{METRICS_URL}?page=1&page_size=100&status=CREATED&operative_models=FULL_SERVICE",
            timeout=25_000,
        )
        await asyncio.sleep(5)

        # Intentar con Playwright text selector primero (más confiable que TreeWalker)
        try:
            row_locator = self.page.get_by_text(reference, exact=True).first
            await row_locator.click(timeout=8_000)
            clicked = True
        except Exception:
            # Fallback: TreeWalker con includes (no strict equality) y clic directo
            clicked = await self.page.evaluate(
                """
                (ref) => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    let node;
                    while ((node = walker.nextNode())) {
                        if (!node.textContent.includes(ref)) continue;
                        // Intentar TR / role=row / cualquier ancestor clickeable
                        let el = node.parentElement;
                        for (let i = 0; i < 12; i++) {
                            if (!el || el === document.body) break;
                            const tag  = el.tagName;
                            const role = (el.getAttribute('role') || '').toLowerCase();
                            if (tag === 'TR' || tag === 'LI' ||
                                role === 'row' || role === 'gridcell' || role === 'button') {
                                el.click();
                                return true;
                            }
                            el = el.parentElement;
                        }
                        // Último recurso: clic directo en el elemento de texto
                        node.parentElement.click();
                        return true;
                    }
                    return false;
                }
                """,
                reference,
            )

        if clicked:
            try:
                await self.page.wait_for_url("**/orders/**", timeout=15_000)
            except Exception:
                await asyncio.sleep(3)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                await asyncio.sleep(2)
            current = self.page.url
            if "/orders/" in current and "/re-assign" not in current:
                await self._goto(current.rstrip("/") + "/re-assign")
            return "/orders/" in self.page.url

        console.print(
            f"  [yellow]  Pedido {reference} no encontrado en el dashboard de métricas. "
            f"¿Ya fue asignado o cambió de estado?[/yellow]"
        )
        return False

    @staticmethod
    def _resources_from_any(data: Any) -> List[Dict]:
        """Extrae objetos de recurso (shoppers) de cualquier estructura JSON."""
        candidates: List[Dict] = []

        def is_resource(obj: Any) -> bool:
            if not isinstance(obj, dict):
                return False
            return bool(obj.get("id")) and (
                "wantsToReceiveTasks" in obj
                or "numberOfOrders" in obj
                or "withQuota" in obj
                or "distance" in obj
            )

        def collect(obj: Any) -> None:
            if isinstance(obj, list):
                for item in obj:
                    if is_resource(item):
                        candidates.append(item)
                    else:
                        collect(item)
            elif isinstance(obj, dict):
                if is_resource(obj):
                    candidates.append(obj)
                else:
                    for v in obj.values():
                        collect(v)

        collect(data)
        return candidates

    @staticmethod
    def _resource_to_shopper(i: int, r: Dict) -> Dict:
        """Convierte un objeto de recurso (nebula/odin) al formato de la UI del bot."""
        name = (
            r.get("name") or r.get("fullName") or r.get("displayName")
            or r.get("resource_name") or f"Shopper {i+1}"
        )

        # Distancia — puede venir en km, metros, o como campo anidado
        dist_raw = r.get("distance") or r.get("distanceKm") or r.get("distance_km") or 0
        try:
            dist_km = float(dist_raw)
            if dist_km > 1000:   # viene en metros
                dist_km /= 1000
        except (TypeError, ValueError):
            dist_km = 0.0

        # Disponibilidad — nebula usa snake_case; fallback a camelCase / string status
        status_str = str(r.get("status") or r.get("resourceStatus") or "").upper()
        available = bool(
            r.get("wants_to_receive_tasks")   # nebula snake_case ✓
            or r.get("wantsToReceiveTasks")   # odin camelCase
            or r.get("available")
            or r.get("isAvailable")
            or r.get("active")
            or r.get("isActive")
            or status_str in ("ACTIVE", "ACTIVO", "AVAILABLE", "ENABLED")
        )

        # Pedidos asignados — nebula usa number_of_orders
        assigned = (
            r.get("number_of_orders")         # nebula snake_case ✓
            or r.get("numberOfOrders")         # odin camelCase
            or r.get("assignedOrders")
            or r.get("orders_count")
            or 0
        )

        # Cupo — nebula usa with_quota
        has_quota = bool(
            r.get("with_quota")               # nebula snake_case ✓
            or r.get("withQuota")              # odin camelCase
            or r.get("hasQuota")
        )
        cupo_label = "Con cupo" if has_quota else "Sin cupo"

        # can_assign = activo: los shoppers activos son la primera opción
        # los inactivos aún pueden asignarse (la UI muestra el botón), se marcan distintos
        can_assign = available

        vehicle = str(r.get("vehicle_type") or r.get("vehicleType") or "-")
        phone   = str(r.get("phone_number") or r.get("phoneNumber") or r.get("phone") or "-")

        return {
            "btn_index":       i,
            "name":            name,
            "distance":        f"{dist_km:.2f} km",
            "availability":    "Activo" if available else "Inactivo",
            "assigned_orders": str(assigned),
            "can_assign":      can_assign,
            "cupo":            cupo_label,
            "vehicle":         vehicle,
            "phone":           phone,
            "_resource":       r,
        }

    @staticmethod
    def _parse_queued_ts(raw: Any) -> float:
        """Convierte un timestamp ISO/epoch de Karri a float unix. inf si no parseable."""
        if not raw:
            return float("inf")
        try:
            if isinstance(raw, (int, float)):
                ts = float(raw)
                return ts / 1000 if ts > 1e10 else ts   # epoch ms → s
            s = str(raw).strip().replace("Z", "+00:00")
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return float("inf")

    @staticmethod
    def _fmt_queued(raw: Any) -> str:
        """Formatea hora de entrada a la cola como HH:MM. '-' si no disponible."""
        ts = ControlTowerBot._parse_queued_ts(raw)
        if ts == float("inf"):
            return "-"
        try:
            return datetime.fromtimestamp(ts).strftime("%H:%M")
        except Exception:
            return "-"

    def _apply_karri_status(self, shoppers: List[Dict]) -> List[Dict]:
        """
        Cruza la lista de shoppers de Instaleap con _karri_phone_index por phone.
        Añade 'karri_status': 'READY' | 'FREE' | '-' a cada shopper.
        """
        if not self._karri_phone_index:
            for s in shoppers:
                s.setdefault("karri_status", "-")
            return shoppers
        for s in shoppers:
            phone = s.get("phone", "").strip()
            entry = self._karri_phone_index.get(phone)
            s["karri_status"]    = entry["status"]       if entry else "-"
            s["karri_location"]  = entry["locationName"] if entry else "-"
            s["karri_queued_at"] = self._fmt_queued(entry.get("queued_at")) if entry else "-"
            s["karri_queued_ts"] = self._parse_queued_ts(entry.get("queued_at")) if entry else float("inf")
        return shoppers

    async def fetch_shoppers(self, order: Dict) -> List[Dict]:
        """
        Obtiene shoppers disponibles para un pedido.
        Estrategias (en orden):
          1. GET /nebula/resources/can-take-task/v2/{task_id}?...&store_id={store_id}
          2. Navegar al panel re-assign e interceptar red
          3. Scraping del DOM
        """
        await self._ensure_token()

        # ── Estrategia 1: endpoint nebula directo ─────────────────────────────
        task_id  = self._get_assignment_task_id(order)
        store_id = order.get("store_id", "")

        if task_id and store_id:
            resources = await self._api_get_shoppers_nebula(task_id, store_id)
            if resources:
                shoppers = [self._resource_to_shopper(i, r) for i, r in enumerate(resources)]
                # Ordenar: activos primero, luego por pedidos asignados, luego por distancia
                shoppers.sort(key=lambda s: (
                    0 if s["availability"] == "Activo" else 1,
                    int(s["assigned_orders"]) if str(s["assigned_orders"]).isdigit() else 999,
                    float(s["distance"].replace(" km", "")) if s["distance"] != "-" else 999,
                ))
                # Re-numerar btn_index tras el sort
                for idx, s in enumerate(shoppers):
                    s["btn_index"] = idx
                self._apply_karri_status(shoppers)
                self._last_shoppers = shoppers
                # Buscar odin_job_id si no lo tenemos
                if not order.get("odin_job_id"):
                    odin_id = await self._api_search_odin_job(order.get("reference", ""))
                    if odin_id:
                        order["odin_job_id"] = odin_id
                return shoppers

        # ── Estrategia 2: navegar + interceptar red ───────────────────────────
        console.print("  [dim]  Navegando al panel de asignación...[/dim]")
        captured: List[Dict] = []

        async def on_response(response: Any) -> None:
            if "avt-backend.instaleap.io" not in response.url:
                return
            if response.status not in (200, 201):
                return
            try:
                text = await response.text()
                if not text or len(text) < 20:
                    return
                data = json.loads(text)
                resources = self._resources_from_any(data)
                if resources:
                    captured.extend(resources)
                    console.print(
                        f"  [dim]  Recursos interceptados ({len(resources)}) desde "
                        f"{response.url.split('avt-backend.instaleap.io')[1][:60]}[/dim]"
                    )
            except Exception:
                pass

        self.page.on("response", on_response)
        success = await self._navigate_to_reassign(order)
        await asyncio.sleep(4)
        self.page.remove_listener("response", on_response)

        # Extraer odin_job_id del URL
        current_url = self.page.url
        if "/orders/" in current_url and not order.get("odin_job_id"):
            parts = current_url.split("/orders/")
            if len(parts) > 1:
                m = re.search(r"^([0-9]+)", parts[1])
                if m:
                    order["odin_job_id"] = m.group(1)
                    console.print(f"  [dim]  odin_job_id del URL: {order['odin_job_id']}[/dim]")

        if captured:
            seen: set = set()
            unique = [r for r in captured if r.get("id") and not seen.add(r["id"])]
            shoppers = [self._resource_to_shopper(i, r) for i, r in enumerate(unique)]
            self._apply_karri_status(shoppers)
            self._last_shoppers = shoppers
            return shoppers

        # ── Estrategia 3: scraping del DOM ────────────────────────────────────
        if success:
            console.print("  [dim]  Sin datos de red — scraping del DOM...[/dim]")
            raw = await self._scrape_shoppers()
            if raw:
                for i, s in enumerate(raw):
                    s["btn_index"] = i
                    s.setdefault("_resource", {})
                self._apply_karri_status(raw)
                self._last_shoppers = raw
                return raw

        console.print("  [yellow]  No se encontraron shoppers.[/yellow]")
        return []

    async def _scrape_shoppers(self) -> List[Dict]:
        """Extrae tarjetas de shoppers del panel 'Recursos cercanos' (lado derecho)."""
        try:
            raw: List[Dict] = await self.page.evaluate("""
                () => {
                    const results = [];

                    // Encontrar todos los botones 'Asignar' que pertenecen al panel
                    // de recursos (lado derecho), excluyendo el botón de tarea del
                    // panel izquierdo (que aparece junto a "Todavía no hay un recurso").
                    const assignBtns = Array.from(document.querySelectorAll('button'))
                        .filter(b => {
                            if (b.innerText.trim() !== 'Asignar') return false;
                            // Excluir botones del panel de tareas (lado izquierdo)
                            let el = b.parentElement;
                            while (el && el !== document.body) {
                                const txt = el.textContent || '';
                                if (txt.includes('Pedidos asignados:')) return true;   // panel shopper ✓
                                if (txt.includes('Todavía no hay un recurso')) return false; // panel tarea ✗
                                if (txt.includes('Recursos cercanos')) return true;   // panel shopper ✓
                                el = el.parentElement;
                            }
                            return true;
                        });

                    assignBtns.forEach((btn, idx) => {
                        let card = btn.parentElement;
                        for (let i = 0; i < 8; i++) {
                            if (!card) break;
                            if (card.innerText.trim().split('\\n').length >= 3) break;
                            card = card.parentElement;
                        }
                        const text  = card ? card.innerText.trim() : '';
                        const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
                        const name  = lines[0] || `Shopper ${idx + 1}`;

                        const distLine = lines.find(l => /\\d+[.,]\\d+\\s*km/i.test(l));
                        const distance = distLine ? distLine.replace(/[^\\d.,km]/gi, '').trim() : '-';
                        const avail    = text.includes('Activo') ? 'Activo' : 'Inactivo';

                        const ordersLine = lines.find(l => l.includes('Pedidos asignados:'));
                        const assigned   = ordersLine
                            ? ordersLine.replace('Pedidos asignados:', '').trim()
                            : '?';

                        const cupoTag   = card ? card.querySelector('[class*="cupo"], [class*="quota"]') : null;
                        const cupoText  = cupoTag ? cupoTag.innerText : '';
                        const canAssign = !cupoText.toLowerCase().includes('sin cupo');

                        results.push({
                            btn_index      : idx,
                            name           : name,
                            distance       : distance ? distance + ' km' : '-',
                            availability   : avail,
                            assigned_orders: assigned,
                            can_assign     : canAssign,
                        });
                    });
                    return results;
                }
            """)
        except Exception:
            return []
        return raw or []

    # ── Asignación ─────────────────────────────────────────────────────────────

    async def assign_shopper(self, order: Dict, btn_index: int) -> bool:
        """
        Asigna el shopper seleccionado al pedido via API directa.
        btn_index es el índice en self._last_shoppers (lista mostrada al usuario).
        """
        # Usar caché de shoppers (ya se llamó fetch_shoppers antes)
        shoppers = self._last_shoppers
        if not shoppers or btn_index >= len(shoppers):
            # Re-fetch si no hay caché
            shoppers = await self.fetch_shoppers(order)
        if not shoppers or btn_index >= len(shoppers):
            return False

        resource    = shoppers[btn_index].get("_resource", {})
        odin_job_id = order.get("odin_job_id", "")
        task_id     = self._get_assignment_task_id(order)

        # ── Vía API (requiere resource.id + odin_job_id + task_id) ───────────
        if resource.get("id") and odin_job_id and task_id:
            return await self._api_assign_shopper(odin_job_id, task_id, resource)

        # ── Fallback: clic en el botón "Asignar" del browser ─────────────────
        # (cuando los shoppers vienen del DOM scraping sin _resource)
        console.print("  [dim]  Asignando via clic en browser...[/dim]")
        if "/orders/" not in self.page.url:
            # Ya deberíamos estar en la re-assign page desde fetch_shoppers
            console.print("  [yellow]  El browser ya no está en la página de asignación.[/yellow]")
            return False
        return await self._click_assign_button(btn_index)

    async def _click_assign_button(self, btn_index: int) -> bool:
        """Hace clic en el botón 'Asignar' número btn_index en el panel de recursos."""
        try:
            clicked: bool = await self.page.evaluate(
                """
                (idx) => {
                    const assignBtns = Array.from(document.querySelectorAll('button'))
                        .filter(b => {
                            if (b.innerText.trim() !== 'Asignar') return false;
                            let el = b.parentElement;
                            while (el && el !== document.body) {
                                const txt = el.textContent || '';
                                if (txt.includes('Pedidos asignados:')) return true;
                                if (txt.includes('Todavía no hay un recurso')) return false;
                                if (txt.includes('Recursos cercanos')) return true;
                                el = el.parentElement;
                            }
                            return true;
                        });
                    if (idx >= assignBtns.length) return false;
                    assignBtns[idx].click();
                    return true;
                }
                """,
                btn_index,
            )
            if clicked:
                await asyncio.sleep(2)
            return clicked
        except Exception:
            return False

    # ── Karri API ──────────────────────────────────────────────────────────────

    async def _login_karri_and_capture_token(self) -> bool:
        """
        Abre el dashboard de Karri en una nueva pestaña, hace login con
        _karri_email/_karri_password, intercepta el Bearer de los requests
        a la API gateway y lo almacena en _karri_token.
        """
        if not self._karri_email or not self._karri_password:
            return False

        KARRI_DASHBOARD = "https://dashboard-walmart.karri.com.mx/"
        KARRI_API_HOST  = "karri-walmart-apigateway-5g8g1c06.uk.gateway.dev"
        captured: list  = []

        karri_page = await self.browser.new_page()
        try:
            def on_request(request: Any) -> None:
                auth = request.headers.get("authorization", "")
                if auth.startswith("Bearer ") and KARRI_API_HOST in request.url:
                    token = auth[7:]
                    if token and not captured:
                        captured.append(token)

            karri_page.on("request", on_request)

            await karri_page.goto(KARRI_DASHBOARD, wait_until="domcontentloaded", timeout=30_000)

            # Esperar y llenar el formulario de login (Firebase/React)
            await karri_page.wait_for_selector(
                'input[type="email"], input[name="email"], input[placeholder*="mail" i]',
                timeout=15_000,
            )
            await karri_page.fill(
                'input[type="email"], input[name="email"], input[placeholder*="mail" i]',
                self._karri_email,
            )
            await karri_page.fill(
                'input[type="password"], input[name="password"]',
                self._karri_password,
            )
            await karri_page.click('button[type="submit"]')

            # Esperar a que el dashboard cargue y dispare requests a la API
            try:
                await karri_page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass

            # Espera extra si aún no capturamos el token
            for _ in range(10):
                if captured:
                    break
                await asyncio.sleep(1)

            if captured:
                self._karri_token    = captured[0]
                self._karri_token_at = asyncio.get_event_loop().time()
                console.print("  [bold green]✓  Token Karri capturado automáticamente.[/bold green]")
                return True

            console.print("  [yellow]  No se pudo capturar el token de Karri.[/yellow]")
            return False

        except Exception as exc:
            console.print(f"  [red]  Error login Karri: {exc}[/red]")
            return False
        finally:
            await karri_page.close()

    async def _ensure_karri_token(self) -> None:
        """Renueva el token de Karri si pasaron más de 55 minutos."""
        if not self._karri_email or not self._karri_password:
            return
        elapsed = asyncio.get_event_loop().time() - self._karri_token_at
        if elapsed > 55 * 60:   # 55 minutos
            self._log("  [dim]  Renovando token Karri...[/dim]")
            await self._login_karri_and_capture_token()

    async def _api_refresh_karri_index(self) -> None:
        """
        Descarga FREE + READY shoppers de la API de Karri y construye
        _karri_phone_index  →  { phone_normalizado: {status, locationId, …} }
        """
        await self._ensure_karri_token()
        if not self._karri_token:
            return
        KARRI_BASE = "https://karri-walmart-apigateway-5g8g1c06.uk.gateway.dev/v1"
        headers = {
            "authorization": f"Bearer {self._karri_token}",
            "content-type":  "application/json",
            "origin":        "https://dashboard-walmart.karri.com.mx",
        }

        # ── Catálogo de ubicaciones (solo si aún no lo tenemos) ──────────────
        if not self._karri_locations:
            try:
                resp_loc = await self.ctx.request.get(
                    f"{KARRI_BASE}/locations?limit=1000&page=1",
                    headers=headers,
                )
                if resp_loc.ok:
                    loc_data = await resp_loc.json()
                    locs = (
                        loc_data.get("data", {}).get("data", [])
                        if isinstance(loc_data.get("data"), dict)
                        else loc_data.get("data", [])
                    )
                    self._karri_locations = {
                        int(l["id"]): str(l.get("name", ""))
                        for l in locs if l.get("id")
                    }
            except Exception:
                pass

        index: Dict[str, Dict] = {}

        for status in ("READY", "FREE"):
            try:
                resp = await self.ctx.request.get(
                    f"{KARRI_BASE}/shoppers?status={status}&limit=10000&page=1",
                    headers=headers,
                )
                if not resp.ok:
                    continue
                data = await resp.json()
                for s in data.get("shoppers", []):
                    phone = str(s.get("phone") or "").strip()
                    if not phone:
                        continue

                    loc_id   = s.get("locationId")
                    loc_name = self._karri_locations.get(int(loc_id), "-") if loc_id else "-"
                    # READY tiene prioridad sobre FREE si el mismo teléfono aparece en ambos
                    if phone not in index or status == "READY":
                        # locationAssignedAt = momento en que el shopper entró a la geocerca
                        queued_raw = (
                            s.get("locationAssignedAt")
                            or s.get("readyAt")
                            or s.get("queuedAt")
                            or s.get("lastLocationAt")
                        )
                        index[phone] = {
                            "karri_id":      s.get("id"),
                            "status":        status,
                            "locationId":    loc_id,
                            "locationName":  loc_name,
                            "firstName":     s.get("firstName", ""),
                            "lastName":      s.get("lastName", ""),
                            "queued_at":     queued_raw,
                        }
            except Exception:
                pass

        self._karri_phone_index = index

    # ── Refresco automático en background ──────────────────────────────────────

    async def _bg_refresh_loop(self, stop_event: asyncio.Event) -> None:
        """Cada 60 s actualiza pedidos del turno y el índice Karri en background."""
        CYCLE_SECS = 60
        while not stop_event.is_set():
            try:
                await self._ensure_token()
                await self._ensure_karri_token()

                now      = datetime.now()
                date_str = now.strftime("%Y-%m-%d")
                orders: List[Dict] = []
                for i in range(3):
                    h    = (now + timedelta(hours=i)).hour
                    data = await self._api_get_jobs(date_str, h)
                    orders.extend(self._parse_jobs_response(data, f"{h:02d}:00-{h:02d}:59"))

                await self._api_refresh_karri_index()

                self._cached_orders     = orders
                self._orders_updated_at = now.strftime("%H:%M:%S")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log(f"  [dim red]  BG refresh error: {exc}[/dim red]")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=CYCLE_SECS)
            except asyncio.TimeoutError:
                pass

    async def start_bg_refresh(self) -> None:
        if self._bg_refresh_task and not self._bg_refresh_task.done():
            return
        self._bg_refresh_stop = asyncio.Event()
        self._bg_refresh_task = asyncio.create_task(
            self._bg_refresh_loop(self._bg_refresh_stop)
        )

    async def stop_bg_refresh(self) -> None:
        if self._bg_refresh_stop:
            self._bg_refresh_stop.set()
        if self._bg_refresh_task:
            try:
                await asyncio.wait_for(self._bg_refresh_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._bg_refresh_task.cancel()
            self._bg_refresh_task = None
            self._bg_refresh_stop = None

    # ── Auto-asignación (algoritmo tipo Uber) ──────────────────────────────────

    async def _auto_assign_loop(
        self,
        stop_event: asyncio.Event,
        allowed_stores: Optional[List[str]] = None,
    ) -> None:
        """
        Ciclo de auto-asignación.  Cada CYCLE_SECS segundos:
          1. Obtiene pedidos CREADOS de la hora actual.
          2. Filtra por tienda si allowed_stores no es None.
          3. Para cada pedido sin shopper asignado:
             a. Llama al endpoint nebula para obtener shoppers activos.
             b. Filtra: wants_to_receive_tasks=True  AND  distancia <= RADIUS_M.
             c. Puntúa cada shopper:  score = 0.7*(dist/RADIUS_M) + 0.3*(orders/10)
                (menor puntaje = mejor candidato).
             d. Asigna el shopper con menor puntaje si odin_job_id disponible.
        """
        CYCLE_SECS   = 30
        MAX_PER_SLOT = 2      # máximo de pedidos por shopper en el mismo slot
        # Ciclos consecutivos como CREATED que se exigen antes de asumir rechazo.
        # Evita falsos positivos cuando el backend demora en actualizar el estado.
        REJECTION_GRACE = 2

        # ref → (shopper_id, slot)  para detectar rechazos
        order_shopper_map: Dict[str, tuple] = {}
        # shopper_id → {slot → cantidad asignada esta sesión}
        assigned_per_shopper: Dict[str, Dict[str, int]] = {}
        # pedidos ya asignados esta sesión (se elimina si el shopper rechaza)
        assigned_refs: set = set()
        # ref → contador de ciclos seguidos en que apareció como CREATED post-asignación
        rejection_seen: Dict[str, int] = {}

        store_label = (
            ", ".join(allowed_stores) if allowed_stores else "todas las tiendas"
        )
        self._log(
            "\n  [bold green]▶  Auto-asignación iniciada "
            f"(ciclo {CYCLE_SECS}s, máx {MAX_PER_SLOT} pedidos/shopper/slot, "
            f"prioridad: hora de entrada a cola, "
            f"tiendas: {store_label})[/bold green]\n"
        )

        cycle_n = 0
        while not stop_event.is_set():
            try:
                cycle_n += 1
                await self._ensure_token()
                await self._api_refresh_karri_index()
                now      = datetime.now()
                date_str = now.strftime("%Y-%m-%d")
                orders: List[Dict] = []
                for i in range(3):
                    h    = (now + timedelta(hours=i)).hour
                    data = await self._api_get_jobs(date_str, h)
                    orders.extend(self._parse_jobs_response(data, f"{h:02d}:00-{h:02d}:59"))

                # Filtrar solo las tiendas activas para el conteo del log
                orders_scope = [
                    o for o in orders
                    if not allowed_stores or o.get("store", "") in allowed_stores
                ]
                pendientes = [o for o in orders_scope if o.get("reference", "") not in assigned_refs]
                ready_n    = sum(1 for v in self._karri_phone_index.values() if v["status"] == "READY")
                self._log(
                    f"  [dim]── Ciclo {cycle_n} · {now.strftime('%H:%M:%S')} · "
                    f"{len(orders_scope)} pedido(s) en tienda(s) · "
                    f"{len(pendientes)} sin asignar · "
                    f"{ready_n} shoppers READY[/dim]"
                )

                # ── Detectar rechazos con período de gracia ───────────────────
                current_refs = {o.get("reference", "") for o in orders}
                for ref in list(assigned_refs):
                    if ref in current_refs:
                        rejection_seen[ref] = rejection_seen.get(ref, 0) + 1
                        if rejection_seen[ref] >= REJECTION_GRACE:
                            assigned_refs.discard(ref)
                            rejection_seen.pop(ref, None)
                            if ref in order_shopper_map:
                                prev_sid, prev_slot = order_shopper_map.pop(ref)
                                slots = assigned_per_shopper.get(prev_sid, {})
                                if slots.get(prev_slot, 0) > 0:
                                    slots[prev_slot] -= 1
                            self._log(
                                f"  [yellow]↩ Pedido {ref} rechazado por el shopper "
                                f"→ disponible para reasignación[/yellow]"
                            )
                    else:
                        rejection_seen.pop(ref, None)

                for order in orders:
                    ref = order.get("reference", "")
                    if ref in assigned_refs:
                        continue

                    if allowed_stores and order.get("store", "") not in allowed_stores:
                        continue

                    task_id  = self._get_assignment_task_id(order)
                    store_id = order.get("store_id", "")
                    if not task_id or not store_id:
                        self._log(f"  [dim]  {ref}: sin task_id/store_id → omitido[/dim]")
                        continue

                    if not order.get("odin_job_id"):
                        odin_id = await self._api_search_odin_job(ref)
                        if odin_id:
                            order["odin_job_id"] = odin_id

                    odin_job_id = order.get("odin_job_id", "")
                    if not odin_job_id:
                        self._log(f"  [dim]  {ref}: sin odin_job_id → omitido[/dim]")
                        continue

                    resources = await self._api_get_shoppers_nebula(task_id, store_id)
                    if not resources:
                        self._log(f"  [dim]  {ref}: sin shoppers en nebula[/dim]")
                        continue

                    slot_str = order.get("slot", "")

                    # Contadores de filtros para el log
                    cnt_total = len(resources)
                    cnt_no_karri = cnt_no_active = cnt_no_ready = cnt_at_limit = 0

                    candidates = []
                    for r in resources:
                        shopper_name = (
                            r.get("name") or r.get("fullName") or r.get("displayName") or ""
                        )
                        if "karri" not in shopper_name.lower():
                            cnt_no_karri += 1
                            continue

                        active = bool(
                            r.get("wants_to_receive_tasks")
                            or r.get("wantsToReceiveTasks")
                            or r.get("available")
                            or r.get("isAvailable")
                            or r.get("active")
                            or r.get("isActive")
                            or str(r.get("status") or "").upper() in ("ACTIVE", "ACTIVO", "AVAILABLE", "ENABLED")
                        )
                        if not active:
                            cnt_no_active += 1
                            continue

                        phone = str(r.get("phone_number") or r.get("phoneNumber") or r.get("phone") or "").strip()
                        queued_ts = float("inf")
                        if self._karri_phone_index:
                            entry = self._karri_phone_index.get(phone)
                            if not entry or entry["status"] != "READY":
                                cnt_no_ready += 1
                                continue
                            queued_ts = self._parse_queued_ts(entry.get("queued_at"))

                        shopper_id = str(r.get("id") or r.get("resourceId") or phone)
                        if assigned_per_shopper.get(shopper_id, {}).get(slot_str, 0) >= MAX_PER_SLOT:
                            cnt_at_limit += 1
                            continue

                        vehicle = str(r.get("vehicle_type") or r.get("vehicleType") or "").strip()
                        candidates.append((queued_ts, vehicle, shopper_id, r))

                    self._log(
                        f"  [dim]  {ref} [{order.get('store','')}] "
                        f"· {cnt_total} shoppers nebula "
                        f"· -{cnt_no_karri} no-KARRI "
                        f"· -{cnt_no_active} inactivos "
                        f"· -{cnt_no_ready} sin READY "
                        f"· -{cnt_at_limit} al límite "
                        f"· {len(candidates)} candidatos[/dim]"
                    )

                    if not candidates:
                        continue

                    try:
                        items_n = int(order.get("items_count") or 0)
                    except (ValueError, TypeError):
                        items_n = 0

                    motos = [(sc, v, sid, r) for sc, v, sid, r in candidates if "moto" in v.lower()]
                    autos = [(sc, v, sid, r) for sc, v, sid, r in candidates if "moto" not in v.lower()]

                    if items_n > 0 and items_n <= 15:
                        pool = motos if motos else autos
                        vehicle_note = "moto (≤15 items)" if motos else "auto (sin motos disponibles)"
                    else:
                        pool = autos if autos else candidates
                        vehicle_note = "auto (>15 items)" if items_n > 15 else "auto (sin dato de items)"

                    pool.sort(key=lambda x: x[0])
                    best_ts, _, best_sid, best_resource = pool[0]

                    ok = await self._api_assign_shopper(odin_job_id, task_id, best_resource)
                    shopper_name = (
                        best_resource.get("name")
                        or best_resource.get("fullName")
                        or best_resource.get("id", "?")
                    )
                    if ok:
                        assigned_refs.add(ref)
                        order_shopper_map[ref] = (best_sid, slot_str)
                        slots = assigned_per_shopper.setdefault(best_sid, {})
                        slots[slot_str] = slots.get(slot_str, 0) + 1
                        cola_str = datetime.fromtimestamp(best_ts).strftime("%H:%M") if best_ts != float("inf") else "-"
                        self._log(
                            f"  [bold green]✓ Auto-asignado:[/bold green] {ref} → "
                            f"{shopper_name}  [{vehicle_note}]  "
                            f"(cola desde {cola_str}, slot {slot_str}: "
                            f"{slots[slot_str]}/{MAX_PER_SLOT})"
                        )
                    else:
                        self._log(
                            f"  [yellow]✗ Fallo auto-asignación:[/yellow] {ref} → {shopper_name}"
                        )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log(f"  [red]  Error en ciclo auto-asignación: {exc}[/red]")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=CYCLE_SECS)
            except asyncio.TimeoutError:
                pass

        self._log("\n  [bold yellow]⏹  Auto-asignación detenida.[/bold yellow]\n")

    async def start_auto_assign(
        self, stores: Optional[List[str]] = None
    ) -> None:
        """Inicia el loop de auto-asignación en background.

        stores: lista de nombres de tienda a procesar, o None para todas.
        """
        if self._auto_assign_task and not self._auto_assign_task.done():
            self._log("  [yellow]  La auto-asignación ya está activa.[/yellow]")
            return
        self._auto_assign_stop = asyncio.Event()
        self._auto_assign_task = asyncio.create_task(
            self._auto_assign_loop(self._auto_assign_stop, allowed_stores=stores or None)
        )

    async def stop_auto_assign(self) -> None:
        """Detiene el loop de auto-asignación y espera que termine."""
        if self._auto_assign_stop:
            self._auto_assign_stop.set()
        if self._auto_assign_task:
            try:
                await asyncio.wait_for(self._auto_assign_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._auto_assign_task.cancel()
            self._auto_assign_task = None
            self._auto_assign_stop = None


# ─── Helpers de UI ─────────────────────────────────────────────────────────────

def _print_header() -> None:
    console.print()
    console.print(
        Panel(
            Align.center(
                Text.from_markup(
                    "[bold white]INSTALEAP CONTROL TOWER BOT  v1.2[/bold white]\n"
                    "[dim cyan]Gestión de Pedidos · Asignación de Shoppers[/dim cyan]\n"
                    f"[dim]{datetime.now().strftime('%A  %d/%m/%Y  %H:%M')}[/dim]"
                )
            ),
            border_style="bold green",
            box=box.DOUBLE_EDGE,
            padding=(1, 6),
        )
    )
    console.print()


def _orders_table(orders: List[Dict], title: str) -> Table:
    t = Table(
        title=f"[bold]{title}[/bold]",
        box=box.ROUNDED,
        border_style="green",
        header_style="bold on dark_green",
        show_lines=True,
        padding=(0, 1),
    )
    t.add_column("#",          style="dim",       width=3,  justify="right")
    t.add_column("Referencia", style="bold cyan", min_width=16)
    t.add_column("Tienda",     style="yellow",    min_width=18)
    t.add_column("Slot",       style="blue",      min_width=13)
    t.add_column("Creación",   style="white",     min_width=16)
    t.add_column("Cliente",    style="green",     min_width=18)
    t.add_column("Teléfono",   style="magenta",   min_width=14)
    t.add_column("Items",      style="cyan",      width=6,  justify="right")
    t.add_column("Pago",       style="white",     min_width=14)
    t.add_column("Estado",     style="bold",      min_width=10)

    for i, o in enumerate(orders, 1):
        t.add_row(
            str(i),
            o.get("reference", ""),
            o.get("store", ""),
            o.get("slot", ""),
            o.get("creation", "").replace("UTC-6", "").strip(),
            o.get("client_name", ""),
            o.get("client_phone", ""),
            o.get("items_count", "-"),
            o.get("payment", ""),
            "[bold green]Creado[/bold green]",
        )
    return t


def _shoppers_table(shoppers: List[Dict]) -> Table:
    t = Table(
        title="[bold]Shoppers Disponibles[/bold]",
        box=box.ROUNDED,
        border_style="blue",
        header_style="bold on dark_blue",
        show_lines=True,
        padding=(0, 1),
    )
    t.add_column("#",              style="dim",        width=3,  justify="right")
    t.add_column("Nombre",         style="bold white", min_width=30)
    t.add_column("Teléfono",       style="magenta",    min_width=14)
    t.add_column("Karri",          style="bold",       min_width=8,  justify="center")
    t.add_column("Tienda Karri",   style="yellow",     min_width=20)
    t.add_column("En cola desde",  style="cyan",       min_width=12, justify="center")
    t.add_column("Disponibilidad", style="bold",       min_width=14)
    t.add_column("Pedidos",        style="yellow",     min_width=10, justify="center")
    t.add_column("Vehículo",       style="magenta",    min_width=12, justify="center")
    t.add_column("Cupo",           style="bold",       min_width=12, justify="center")

    for i, s in enumerate(shoppers, 1):
        avail     = s.get("availability", "")
        is_active = "Activo" in avail
        avail_str = "[green]● Activo[/green]" if is_active else "[dim]○ Inactivo[/dim]"
        cupo_raw  = s.get("cupo", "")
        cap = "[yellow]Con cupo[/yellow]" if cupo_raw == "Con cupo" else "[dim]Sin cupo[/dim]"
        ks = s.get("karri_status", "-")
        if ks == "READY":
            karri_str = "[bold green]READY[/bold green]"
        elif ks == "FREE":
            karri_str = "[cyan]FREE[/cyan]"
        else:
            karri_str = "[dim]-[/dim]"
        t.add_row(
            str(i),
            f"[bold white]{s.get('name', f'Shopper {i}')}[/bold white]" if is_active
            else s.get("name", f"Shopper {i}"),
            s.get("phone", "-"),
            karri_str,
            s.get("karri_location", "-"),
            s.get("karri_queued_at", "-"),
            avail_str,
            s.get("assigned_orders", "-"),
            s.get("vehicle", "-"),
            cap,
        )
    return t


def _unique_stores(orders: List[Dict]) -> List[str]:
    return sorted({o.get("store", "").strip() for o in orders if o.get("store", "").strip()})


def _filter(orders: List[Dict], store: str) -> List[Dict]:
    ALL = "─ Todas las tiendas ─"
    if not store or store == ALL:
        return orders
    return [o for o in orders if o.get("store", "").strip() == store]


# ─── Flujo principal ───────────────────────────────────────────────────────────

async def run() -> None:
    _print_header()

    headless = await questionary.confirm(
        "¿Ejecutar en modo silencioso? (sin ventana del navegador)",
        default=False,
        style=BOT_STYLE,
    ).ask_async()

    async with ControlTowerBot(headless=headless) as bot:

        # ── Login ──────────────────────────────────────────────────────────────
        console.print()
        console.print(Rule("[bold green]Autenticación[/bold green]"))
        with console.status("[bold green]Iniciando sesión en Control Tower...[/bold green]"):
            await bot.login()

        token_ok = "[green]✓ API conectada[/green]" if bot._auth_token else "[yellow]⚠ sin token API[/yellow]"
        console.print(f"  [bold green]✓[/bold green] Sesión iniciada · {token_ok}\n")

        # ── Login Karri automático ─────────────────────────────────────────────
        console.print(Rule("[bold yellow]Karri Dashboard[/bold yellow]"))
        with console.status("[bold yellow]Iniciando sesión en Karri...[/bold yellow]"):
            ok = await bot._login_karri_and_capture_token()
        if ok:
            with console.status("[bold yellow]Cargando fila de shoppers Karri...[/bold yellow]"):
                await bot._api_refresh_karri_index()
            console.print(
                f"  [bold green]✓[/bold green] Karri conectado  "
                f"({len(bot._karri_phone_index)} shoppers indexados)\n"
            )
        else:
            console.print("  [yellow]  No se pudo conectar a Karri — continuando sin cruce.[/yellow]\n")

        # ── Refresco automático en background ──────────────────────────────────
        _write_log(f"Bot iniciado — log en {LOG_FILE}")
        await bot.start_bg_refresh()

        # ── Menú principal ─────────────────────────────────────────────────────
        _auto_stores: Optional[List[str]] = None   # tiendas activas en el loop actual

        while True:
            console.print()
            # Vaciar mensajes pendientes de loops en background
            if bot._pending_log:
                for msg in bot._pending_log:
                    console.print(msg)
                bot._pending_log.clear()
                console.print()
            # Mostrar estado del refresh en background
            if bot._orders_updated_at:
                ready_count  = sum(
                    1 for v in bot._karri_phone_index.values() if v["status"] == "READY"
                )
                console.print(
                    f"  [dim]🔄 Auto-actualización activa  ·  "
                    f"{len(bot._cached_orders)} pedido(s) en turno  ·  "
                    f"Karri: {ready_count} READY  ·  "
                    f"última actualización {bot._orders_updated_at}[/dim]"
                )
            auto_running = (
                bot._auto_assign_task is not None
                and not bot._auto_assign_task.done()
            )
            if auto_running:
                store_tag = (
                    f"  [{', '.join(_auto_stores)}]" if _auto_stores else "  [todas las tiendas]"
                )
                auto_choice = questionary.Choice(
                    f"⏹  Detener asignación automática{store_tag}", value="auto_stop"
                )
            else:
                auto_choice = questionary.Choice(
                    "▶  Activar asignación automática", value="auto_start"
                )
            action = await questionary.select(
                "Menú principal:",
                choices=[
                    questionary.Choice("📋  Actualizar pedidos del turno actual", value="refresh"),
                    questionary.Choice("📅  Ver pedidos por fecha",               value="bydate"),
                    questionary.Choice("🔍  Asignar shopper a un pedido",         value="assign"),
                    auto_choice,
                    questionary.Choice("📄  Ver actividad reciente",              value="activity"),
                    questionary.Choice("❌  Salir",                               value="exit"),
                ],
                style=BOT_STYLE,
            ).ask_async()

            # ── Ver actividad reciente ──────────────────────────────────────────
            if action == "activity":
                console.print()
                console.print(Rule("[bold]Actividad reciente[/bold]"))
                try:
                    with open(LOG_FILE, encoding="utf-8") as f:
                        lines = f.readlines()
                    last = lines[-50:] if len(lines) > 50 else lines
                    if last:
                        for line in last:
                            console.print(f"  [dim]{line.rstrip()}[/dim]")
                    else:
                        console.print("  [dim]Sin actividad registrada aún.[/dim]")
                except FileNotFoundError:
                    console.print("  [dim]Sin actividad registrada aún.[/dim]")
                console.print(Rule())
                console.print(f"  [dim]Log completo: {LOG_FILE}[/dim]")
                console.print(f"  [dim]Para monitoreo en tiempo real: tail -f {LOG_FILE}[/dim]\n")
                continue

            # ── Salir ──────────────────────────────────────────────────────────
            if action == "exit":
                await bot.stop_auto_assign()
                await bot.stop_bg_refresh()
                console.print("\n[dim]  Cerrando bot. ¡Hasta luego![/dim]\n")
                break

            # ── Auto-asignación: iniciar ───────────────────────────────────────
            if action == "auto_start":
                ALL_TAG = "─ Todas las tiendas ─"
                stores_in_cache = _unique_stores(bot._cached_orders)

                if stores_in_cache:
                    selected_stores = await questionary.checkbox(
                        "¿En qué tiendas activar la auto-asignación? "
                        "(Espacio = seleccionar, Enter = confirmar)",
                        choices=[ALL_TAG] + stores_in_cache,
                        style=BOT_STYLE,
                    ).ask_async()

                    if selected_stores is None:
                        continue

                    if not selected_stores or ALL_TAG in selected_stores:
                        _auto_stores = None   # todas
                    else:
                        _auto_stores = selected_stores
                else:
                    # Sin pedidos en caché → activar para todas
                    _auto_stores = None

                await bot.start_auto_assign(stores=_auto_stores)
                continue

            # ── Auto-asignación: detener ───────────────────────────────────────
            if action == "auto_stop":
                await bot.stop_auto_assign()
                _auto_stores = None
                continue

            # ── Pedidos por fecha elegida ──────────────────────────────────────
            if action == "bydate":
                today    = datetime.now()
                tomorrow = today + timedelta(days=1)

                date_option = await questionary.select(
                    "¿Qué día quieres consultar?",
                    choices=[
                        questionary.Choice(f"Hoy      ({today.strftime('%d/%m/%Y')})",    value="today"),
                        questionary.Choice(f"Mañana   ({tomorrow.strftime('%d/%m/%Y')})", value="tomorrow"),
                        questionary.Choice("Otra fecha (escribe YYYY-MM-DD)",             value="custom"),
                    ],
                    style=BOT_STYLE,
                ).ask_async()

                if date_option == "today":
                    target_date = today
                elif date_option == "tomorrow":
                    target_date = tomorrow
                else:
                    raw_date = await questionary.text(
                        "Fecha (YYYY-MM-DD):",
                        validate=lambda v: (
                            bool(re.match(r"^\d{4}-\d{2}-\d{2}$", v)) or
                            "Formato incorrecto, usa YYYY-MM-DD"
                        ),
                        style=BOT_STYLE,
                    ).ask_async()
                    try:
                        target_date = datetime.strptime(raw_date.strip(), "%Y-%m-%d")
                    except ValueError:
                        console.print("[red]  Fecha inválida.[/red]")
                        continue

                date_label = target_date.strftime("%A %d/%m/%Y")

                console.print()
                console.print(
                    Panel(
                        f"  Buscando pedidos CREADOS para [bold]{date_label}[/bold]\n"
                        "  Consultando slots 06:00 – 22:59 via API…",
                        title="[bold blue]Pedidos por Fecha[/bold blue]",
                        border_style="blue",
                    )
                )

                all_orders_date: List[Dict] = []
                with console.status(
                    f"[bold blue]Consultando {target_date.strftime('%Y-%m-%d')} (slots 06–22)...[/bold blue]"
                ):
                    all_orders_date = await bot.fetch_all_orders_for_date(target_date)

                if not all_orders_date:
                    console.print(
                        f"[yellow]  No se encontraron pedidos CREADOS para {date_label}.[/yellow]"
                    )
                    continue

                ALL_STORES = "─ Todas las tiendas ─"
                stores         = _unique_stores(all_orders_date)
                selected_store = await questionary.select(
                    "Filtrar por tienda:",
                    choices=[ALL_STORES] + stores,
                    style=BOT_STYLE,
                ).ask_async()

                visible = _filter(all_orders_date, selected_store)
                console.print()
                console.print(
                    _orders_table(
                        visible,
                        f"Pedidos ({date_label}) — {selected_store}",
                    )
                )
                console.print(
                    f"  [dim]{len(visible)} pedido(s) · {len(all_orders_date)} total[/dim]\n"
                )

                usar = await questionary.confirm(
                    "¿Usar estos pedidos para asignación en esta sesión?",
                    default=False,
                    style=BOT_STYLE,
                ).ask_async()
                if usar:
                    bot._cached_orders     = all_orders_date
                    bot._orders_updated_at = datetime.now().strftime("%H:%M:%S")
                    console.print(
                        "  [dim]Pedidos cargados en caché. "
                        "Usa '🔍 Asignar shopper' para continuar.[/dim]"
                    )
                continue

            # ── Cargar / refrescar pedidos del turno ───────────────────────────
            if action == "refresh" or (action == "assign" and not bot._cached_orders):
                slots = bot.current_slots()

                console.print()
                console.print(
                    Panel(
                        "\n".join(f"  • {s['label']}" for s in slots),
                        title="[bold green]Slots consultados[/bold green]",
                        border_style="green",
                    )
                )

                all_orders: List[Dict] = []
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                    transient=True,
                ) as prog:
                    task = prog.add_task("Consultando...", total=len(slots))
                    for slot in slots:
                        prog.update(task, description=f"  Slot {slot['label']}…")
                        orders = await bot.fetch_orders_for_slot(slot)
                        all_orders.extend(orders)
                        prog.advance(task)

                bot._cached_orders     = all_orders
                bot._orders_updated_at = datetime.now().strftime("%H:%M:%S")

                if not all_orders:
                    console.print(
                        "[yellow]  No se encontraron pedidos CREADOS "
                        "para los slots actuales.[/yellow]"
                    )
                    continue

                ALL_STORES = "─ Todas las tiendas ─"
                stores         = _unique_stores(all_orders)
                selected_store = await questionary.select(
                    "Filtrar por tienda:",
                    choices=[ALL_STORES] + stores,
                    style=BOT_STYLE,
                ).ask_async()

                visible = _filter(all_orders, selected_store)
                console.print()
                console.print(_orders_table(visible, f"Pedidos Creados — {selected_store}"))
                console.print(f"  [dim]{len(visible)} pedido(s) · {len(all_orders)} total[/dim]\n")

                if action == "refresh":
                    continue

            # ── Asignación ─────────────────────────────────────────────────────
            if action == "assign":
                if not bot._cached_orders:
                    console.print("[yellow]  Carga los pedidos primero.[/yellow]")
                    continue

                ALL_STORES     = "─ Todas las tiendas ─"
                stores         = _unique_stores(bot._cached_orders)
                selected_store = await questionary.select(
                    "Filtrar tienda:",
                    choices=[ALL_STORES] + stores,
                    style=BOT_STYLE,
                ).ask_async()

                visible = _filter(bot._cached_orders, selected_store)
                if not visible:
                    console.print("[yellow]  Sin pedidos para esa tienda.[/yellow]")
                    continue

                console.print()
                console.print(_orders_table(visible, "Elige un pedido"))

                order_choices = [
                    questionary.Choice(
                        f"[{o.get('slot','')}]  {o.get('reference','')}  ·  "
                        f"{o.get('store','')}  ·  {o.get('client_name','')}  "
                        f"({o.get('client_phone','')})",
                        value=o,
                    )
                    for o in visible
                ] + [questionary.Choice("← Volver al menú", value="__back__")]

                selected_order = await questionary.select(
                    "Pedido a asignar:",
                    choices=order_choices,
                    style=BOT_STYLE,
                ).ask_async()

                if not selected_order or selected_order == "__back__":
                    continue

                o = selected_order
                console.print()
                console.print(
                    Panel(
                        f"[cyan]Referencia:[/cyan]  [bold]{o.get('reference','')}[/bold]\n"
                        f"[yellow]Tienda:[/yellow]      {o.get('store','')}\n"
                        f"[green]Cliente:[/green]     {o.get('client_name','')}  "
                        f"[magenta]{o.get('client_phone','')}[/magenta]\n"
                        f"[blue]Entrega:[/blue]     {o.get('delivery','')}\n"
                        f"[white]Pago:[/white]        {o.get('payment','')}",
                        title="[bold]Detalle del Pedido[/bold]",
                        border_style="cyan",
                    )
                )

                with console.status("[bold blue]Cargando shoppers disponibles...[/bold blue]"):
                    shoppers = await bot.fetch_shoppers(selected_order)

                # Solo shoppers KARRI con estado READY
                shoppers = [
                    s for s in shoppers
                    if "karri" in s.get("name", "").lower()
                    and s.get("karri_status") == "READY"
                ]

                if not shoppers:
                    console.print(
                        "[yellow]  No se encontraron shoppers KARRI con estado READY.[/yellow]\n"
                        "[dim]  Verifica manualmente en Control Tower.[/dim]"
                    )
                    continue

                console.print()
                console.print(_shoppers_table(shoppers))

                shopper_choices = []
                for s in shoppers:
                    avail_icon = "🟢" if "Activo" in s.get("availability", "") else "🔴"
                    ks = s.get("karri_status", "-")
                    karri_tag = "[READY]" if ks == "READY" else "[FREE]" if ks == "FREE" else ""
                    karri_loc = s.get("karri_location", "-")
                    label = (
                        f"{avail_icon}  {s.get('name', 'Shopper')}  ·  "
                        f"{s.get('phone', '-')}  ·  "
                        f"cola: {s.get('karri_queued_at', '-')}  ·  "
                        f"{s.get('vehicle', '-')}  ·  "
                        f"{s.get('assigned_orders', '?')} pedidos"
                        + (f"  {karri_tag} {karri_loc}" if karri_tag else "")
                    )
                    shopper_choices.append(
                        questionary.Choice(label, value=s.get("btn_index", 0))
                    )
                shopper_choices.append(questionary.Choice("← Cancelar", value="__cancel__"))

                btn_idx = await questionary.select(
                    "Shopper a asignar:",
                    choices=shopper_choices,
                    style=BOT_STYLE,
                ).ask_async()

                if btn_idx is None or btn_idx == "__cancel__":
                    console.print("[dim]  Asignación cancelada.[/dim]")
                    continue

                shopper_name = (
                    shoppers[btn_idx].get("name", "Shopper")
                    if btn_idx < len(shoppers) else "Shopper"
                )

                confirmed = await questionary.confirm(
                    f"¿Asignar '{shopper_name}' al pedido {o.get('reference', '')}?",
                    default=True,
                    style=BOT_STYLE,
                ).ask_async()

                if not confirmed:
                    console.print("[dim]  Asignación cancelada.[/dim]")
                    continue

                with console.status("[bold green]Procesando asignación...[/bold green]"):
                    success = await bot.assign_shopper(selected_order, btn_idx)

                if success:
                    console.print(
                        f"\n  [bold green]✓  Shopper asignado correctamente "
                        f"al pedido {o.get('reference', '')}[/bold green]\n"
                    )
                    bot._cached_orders = [
                        c for c in bot._cached_orders
                        if c.get("reference") != o.get("reference")
                    ]
                else:
                    console.print(
                        "\n  [bold red]✗  No se pudo completar la asignación automática.[/bold red]\n"
                        "  [dim]Verifica y completa el proceso manualmente en Control Tower.[/dim]\n"
                    )


# ─── Entrada ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print("\n\n[dim]  Interrumpido por el usuario.[/dim]\n")
        sys.exit(0)
