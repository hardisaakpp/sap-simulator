"""sap-simulator — simulador de SAP Business One Service Layer (/b1s/v1) para desarrollo local.

Pensado para que api-middleware-sap funcione sin SAP real: implementa Login/Logout con
cookie B1SESSION y un motor OData genérico sobre las colecciones de app/store.py.
Todo lo que no está simulado responde 404 y queda registrado como "NO SIMULADO" en el log.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Set
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .odata import ODataError, consultar, seleccionar
from .store import COLECCIONES, SapError, Store, no_encontrado

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sap-simulator")

BASE = "/b1s/v1"
TAMANO_PAGINA = int(os.getenv("PAGE_SIZE", "20"))            # igual que el Service Layer por defecto
COMPANY_DB = os.getenv("SAP_COMPANY_DB", "")                 # vacío = acepta cualquier CompanyDB
SAP_USER = os.getenv("SAP_USER", "")                         # vacío = acepta cualquier usuario
SAP_PASSWORD = os.getenv("SAP_PASSWORD", "")                 # vacío = acepta cualquier contraseña
SESSION_TIMEOUT_MIN = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))

store = Store(
    seed_dir=Path(os.getenv("SEED_DIR", Path(__file__).resolve().parent.parent / "seed")),
    state_file=Path(os.getenv("STATE_FILE", "/data/state.json")),
    stock_por_defecto=float(os.getenv("DEFAULT_STOCK", "1000")),
)
sesiones: Set[str] = set()
bitacora: Deque[Dict[str, Any]] = deque(maxlen=int(os.getenv("REQUEST_LOG_SIZE", "200")))

app = FastAPI(title="sap-simulator", description="Simulador de SAP Business One Service Layer para desarrollo local")


def _error(exc: SapError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.body())


def _registrar(request: Request, status: int, detalle: Optional[str] = None) -> None:
    bitacora.append({
        "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metodo": request.method,
        "url": request.url.path.removeprefix(BASE + "/") + (f"?{request.url.query}" if request.url.query else ""),
        "status": status,
        "detalle": detalle,
    })


# ------------------------------------------------------------------ sesión
@app.post(f"{BASE}/Login")
async def login(request: Request) -> Response:
    try:
        cuerpo = await request.json()
    except json.JSONDecodeError:
        cuerpo = {}
    if (COMPANY_DB and cuerpo.get("CompanyDB") != COMPANY_DB) or (SAP_USER and cuerpo.get("UserName") != SAP_USER) \
            or (SAP_PASSWORD and cuerpo.get("Password") != SAP_PASSWORD):
        _registrar(request, 401, "credenciales inválidas")
        return _error(SapError(401, -304, "Fail to get DB Credentials from SLD"))
    sesion = str(uuid.uuid4())
    sesiones.add(sesion)
    _registrar(request, 200, f"CompanyDB={cuerpo.get('CompanyDB')} UserName={cuerpo.get('UserName')}")
    respuesta = JSONResponse({
        "odata.metadata": f"{BASE}/$metadata#B1Sessions/@Element",
        "SessionId": sesion,
        "Version": "1000180",
        "SessionTimeout": SESSION_TIMEOUT_MIN,
    })
    respuesta.set_cookie("B1SESSION", sesion, httponly=True, path=BASE)
    return respuesta


@app.post(f"{BASE}/Logout")
async def logout(request: Request) -> Response:
    sesiones.discard(request.cookies.get("B1SESSION", ""))
    return Response(status_code=204)


# ------------------------------------------------------------------ endpoints de apoyo (no son de SAP)
@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "Healthy", "colecciones": {n: len(store.listar(n)) for n in COLECCIONES}}


@app.get("/sim/documentos/{coleccion}")
async def ver_documentos(coleccion: str) -> Response:
    if coleccion not in COLECCIONES:
        return JSONResponse(status_code=404, content={"detail": f"Colección desconocida: {coleccion}"})
    return JSONResponse(store.listar(coleccion))


@app.get("/sim/bitacora")
async def ver_bitacora() -> list:
    """Últimas llamadas recibidas (útil para ver qué pidió el middleware a SAP)."""
    return list(reversed(bitacora))


@app.post("/sim/reiniciar")
async def reiniciar() -> Dict[str, str]:
    """Borra los documentos creados y recarga los datos semilla."""
    store.reiniciar_documentos()
    return {"status": "ok", "detalle": "Documentos eliminados y datos semilla recargados"}


# ------------------------------------------------------------------ OData genérico
def _parsear_ruta(ruta: str) -> tuple[str, Optional[str], Optional[str]]:
    """'Items' | "Items('X')" | 'PurchaseOrders(5)' | "Items('X')/ItemPrices" | 'Items/$count'."""
    navegacion = None
    if ")/" in ruta:
        ruta, navegacion = ruta.split(")/", 1)
        ruta += ")"
    elif "/" in ruta:
        ruta, navegacion = ruta.split("/", 1)
    clave = None
    if ruta.endswith(")") and "(" in ruta:
        nombre, clave = ruta[:-1].split("(", 1)
        if clave.startswith("'") and clave.endswith("'"):
            clave = clave[1:-1].replace("''", "'")
    else:
        nombre = ruta
    return nombre, clave, navegacion


def _next_link(nombre: str, params: Dict[str, str], skip: int) -> str:
    nuevos = {k: v for k, v in params.items() if k != "$skip"}
    if "$top" in nuevos:
        nuevos["$top"] = str(int(nuevos["$top"]) - (skip - int(params.get("$skip", 0) or 0)))
    nuevos["$skip"] = str(skip)
    return nombre + "?" + "&".join(f"{k}={quote(v, safe=chr(39) + '$,():/')}" for k, v in nuevos.items())


@app.api_route(f"{BASE}/{{ruta:path}}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
async def odata(ruta: str, request: Request) -> Response:
    if request.cookies.get("B1SESSION") not in sesiones:
        _registrar(request, 401, "sesión inválida o expirada")
        return _error(SapError(401, 301, "Invalid session or session already timeout."))

    nombre, clave, navegacion = _parsear_ruta(ruta)
    if nombre not in COLECCIONES:
        logger.warning("NO SIMULADO: %s %s", request.method, ruta)
        _registrar(request, 404, "recurso no simulado")
        return _error(SapError(404, -1, f"Unrecognized resource path: {nombre} (no simulado en sap-simulator)"))

    params = dict(request.query_params)
    try:
        respuesta = await _despachar(request, nombre, clave, navegacion, params)
    except SapError as exc:
        _registrar(request, exc.status, exc.mensaje)
        return _error(exc)
    except ODataError as exc:
        _registrar(request, 400, str(exc))
        return _error(SapError(400, -1000, str(exc)))
    _registrar(request, respuesta.status_code)
    return respuesta


async def _despachar(request: Request, nombre: str, clave: Optional[str], navegacion: Optional[str],
                     params: Dict[str, str]) -> Response:
    metodo = request.method
    if metodo == "GET" and clave is None:
        if navegacion == "$count":
            return Response(str(consultar(store.listar(nombre), params, 10**9).total), media_type="text/plain")
        resultado = consultar(store.listar(nombre), params, TAMANO_PAGINA)
        cuerpo: Dict[str, Any] = {"odata.metadata": f"{BASE}/$metadata#{nombre}", "value": resultado.valores}
        if params.get("$inlinecount") == "allpages":
            cuerpo["odata.count"] = resultado.total
        if resultado.siguiente_skip is not None:
            cuerpo["odata.nextLink"] = _next_link(nombre, params, resultado.siguiente_skip)
        return JSONResponse(cuerpo)

    if metodo == "GET":
        entidad = store.obtener(nombre, clave)
        if navegacion:
            if navegacion not in entidad:
                raise no_encontrado()
            return JSONResponse({"odata.metadata": f"{BASE}/$metadata#{nombre}/{navegacion}", navegacion: entidad[navegacion]})
        return JSONResponse({"odata.metadata": f"{BASE}/$metadata#{nombre}/@Element",
                             **seleccionar(entidad, params.get("$select"))})

    cuerpo_req = await _json(request)
    if metodo == "POST" and clave is None:
        creado = store.crear(nombre, cuerpo_req)
        return JSONResponse(status_code=201, content={"odata.metadata": f"{BASE}/$metadata#{nombre}/@Element", **creado})
    if metodo in ("PATCH", "PUT") and clave is not None:
        store.actualizar(nombre, clave, cuerpo_req)
        return Response(status_code=204)
    if metodo == "DELETE" and clave is not None:
        store.eliminar(nombre, clave)
        return Response(status_code=204)
    raise SapError(405, -1, f"Operación no soportada: {metodo} {nombre}")


async def _json(request: Request) -> Dict[str, Any]:
    crudo = await request.body()
    if not crudo:
        return {}
    try:
        return json.loads(crudo)
    except json.JSONDecodeError as exc:
        raise SapError(400, -1000, f"Invalid JSON body: {exc}") from exc
