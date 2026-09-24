"""Almacén en memoria de las colecciones del Service Layer simulado.

- Datos maestros (Items, Warehouses, BusinessPartners, ...) se cargan de seed/*.json.
- Documentos (PurchaseOrders, InventoryTransferRequests, ...) se crean vía POST, reciben
  DocEntry/DocNum correlativos y se persisten en STATE_FILE para sobrevivir reinicios.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sap-simulator")

Entity = Dict[str, Any]


class SapError(Exception):
    """Error con el formato del Service Layer: {"error": {"code", "message": {"lang", "value"}}}."""

    def __init__(self, status: int, code: int, mensaje: str):
        super().__init__(mensaje)
        self.status = status
        self.code = code
        self.mensaje = mensaje

    def body(self) -> Dict[str, Any]:
        return {"error": {"code": self.code, "message": {"lang": "en-us", "value": self.mensaje}}}


def no_encontrado() -> SapError:
    return SapError(404, -2028, "No matching records found (ODBC -2028)")


@dataclass(frozen=True)
class Coleccion:
    clave: str
    tipo_clave: type
    documento: bool = False          # True: POST asigna DocEntry/DocNum
    lineas: Optional[str] = None     # nombre de la colección de líneas del documento


COLECCIONES: Dict[str, Coleccion] = {
    # Datos maestros (seed/)
    "Items": Coleccion("ItemCode", str),
    "Warehouses": Coleccion("WarehouseCode", str),
    "BusinessPartners": Coleccion("CardCode", str),
    "ItemGroups": Coleccion("Number", int),
    "PriceLists": Coleccion("PriceListNo", int),
    # Documentos (creados por el middleware)
    "PurchaseOrders": Coleccion("DocEntry", int, True, "DocumentLines"),
    "InventoryTransferRequests": Coleccion("DocEntry", int, True, "StockTransferLines"),
    "StockTransfers": Coleccion("DocEntry", int, True, "StockTransferLines"),
    "PurchaseDeliveryNotes": Coleccion("DocEntry", int, True, "DocumentLines"),
    "InventoryGenEntries": Coleccion("DocEntry", int, True, "DocumentLines"),
}
MAESTROS_SEED = {
    "Items": "items.json",
    "Warehouses": "warehouses.json",
    "BusinessPartners": "business_partners.json",
    "ItemGroups": "item_groups.json",
    "PriceLists": "price_lists.json",
}
# Numeración inicial por tipo de documento (solo para que los DocNum sean reconocibles).
DOCNUM_INICIAL = {
    "PurchaseOrders": 900001,
    "InventoryTransferRequests": 800001,
    "StockTransfers": 700001,
    "PurchaseDeliveryNotes": 600001,
    "InventoryGenEntries": 500001,
}


class Store:
    def __init__(self, seed_dir: Path, state_file: Path, stock_por_defecto: float):
        self._lock = threading.RLock()
        self.seed_dir = seed_dir
        self.state_file = state_file
        self.stock_por_defecto = stock_por_defecto
        self.datos: Dict[str, List[Entity]] = {}
        self.secuencias: Dict[str, int] = {}
        self.cargar()

    # ------------------------------------------------------------- carga / persistencia
    def cargar(self) -> None:
        with self._lock:
            self.datos = {nombre: [] for nombre in COLECCIONES}
            for nombre, archivo in MAESTROS_SEED.items():
                ruta = self.seed_dir / archivo
                if ruta.exists():
                    self.datos[nombre] = json.loads(ruta.read_text(encoding="utf-8"))
            self._completar_articulos()
            self.secuencias = {n: 0 for n, c in COLECCIONES.items() if c.documento}
            if self.state_file.exists():
                estado = json.loads(self.state_file.read_text(encoding="utf-8"))
                for nombre, docs in estado.get("documentos", {}).items():
                    if nombre in self.datos:
                        self.datos[nombre] = docs
                self.secuencias.update(estado.get("secuencias", {}))
            resumen = {n: len(v) for n, v in self.datos.items() if v}
            logger.info("Datos cargados: %s", resumen)

    def _completar_articulos(self) -> None:
        """Rellena ItemWarehouseInfoCollection/QuantityOnStock si la semilla no los trae."""
        bodegas = [w["WarehouseCode"] for w in self.datos["Warehouses"]]
        for item in self.datos["Items"]:
            if "ItemWarehouseInfoCollection" not in item:
                item["ItemWarehouseInfoCollection"] = [
                    {"ItemCode": item["ItemCode"], "WarehouseCode": b, "InStock": self.stock_por_defecto,
                     "Committed": 0.0, "Ordered": 0.0, "Locked": "tNO"}
                    for b in bodegas
                ]
            item.setdefault(
                "QuantityOnStock", sum(w.get("InStock", 0) for w in item["ItemWarehouseInfoCollection"])
            )

    def _guardar(self) -> None:
        estado = {
            "secuencias": self.secuencias,
            "documentos": {n: self.datos[n] for n, c in COLECCIONES.items() if c.documento},
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def reiniciar_documentos(self) -> None:
        with self._lock:
            if self.state_file.exists():
                self.state_file.unlink()
            self.cargar()

    # ------------------------------------------------------------- lectura
    def listar(self, nombre: str) -> List[Entity]:
        with self._lock:
            return copy.deepcopy(self.datos[nombre])

    def obtener(self, nombre: str, clave: Any) -> Entity:
        col = COLECCIONES[nombre]
        with self._lock:
            for entidad in self.datos[nombre]:
                if str(entidad.get(col.clave)) == str(clave):
                    return copy.deepcopy(entidad)
        raise no_encontrado()

    # ------------------------------------------------------------- escritura
    def crear(self, nombre: str, cuerpo: Entity) -> Entity:
        col = COLECCIONES[nombre]
        with self._lock:
            if not col.documento:
                if any(str(e.get(col.clave)) == str(cuerpo.get(col.clave)) for e in self.datos[nombre]):
                    raise SapError(400, -2035, f"This entry already exists in the following tables ({nombre})")
                self.datos[nombre].append(copy.deepcopy(cuerpo))
                return copy.deepcopy(cuerpo)

            self._validar_documento(nombre, col, cuerpo)
            self.secuencias[nombre] = self.secuencias.get(nombre, 0) + 1
            doc = copy.deepcopy(cuerpo)
            doc["DocEntry"] = self.secuencias[nombre]
            doc["DocNum"] = DOCNUM_INICIAL.get(nombre, 100001) + self.secuencias[nombre] - 1
            hoy = date.today().isoformat()
            doc.setdefault("DocDate", hoy)
            doc.setdefault("DocDueDate", doc["DocDate"])
            doc.setdefault("DocumentStatus", "bost_Open")
            doc.setdefault("Cancelled", "tNO")
            if nombre == "PurchaseOrders":
                bp = self.obtener("BusinessPartners", doc["CardCode"])
                doc.setdefault("CardName", bp.get("CardName"))
            for i, linea in enumerate(doc.get(col.lineas, []) if col.lineas else []):
                linea["LineNum"] = i
                linea.setdefault("LineStatus", "bost_Open")
                if "ItemCode" in linea:
                    item = self.obtener("Items", linea["ItemCode"])
                    linea.setdefault("ItemDescription", item.get("ItemName"))
            self.datos[nombre].append(doc)
            self._guardar()
            logger.info("%s creado: DocEntry=%s DocNum=%s U_sap_id=%s",
                        nombre, doc["DocEntry"], doc["DocNum"], doc.get("U_sap_id"))
            return copy.deepcopy(doc)

    def actualizar(self, nombre: str, clave: Any, cambios: Entity) -> None:
        col = COLECCIONES[nombre]
        with self._lock:
            for entidad in self.datos[nombre]:
                if str(entidad.get(col.clave)) == str(clave):
                    entidad.update(copy.deepcopy(cambios))
                    if col.documento:
                        self._guardar()
                    return
        raise no_encontrado()

    def eliminar(self, nombre: str, clave: Any) -> None:
        col = COLECCIONES[nombre]
        with self._lock:
            antes = len(self.datos[nombre])
            self.datos[nombre] = [e for e in self.datos[nombre] if str(e.get(col.clave)) != str(clave)]
            if len(self.datos[nombre]) == antes:
                raise no_encontrado()
            if col.documento:
                self._guardar()

    # ------------------------------------------------------------- reglas tipo SAP
    def _existe(self, nombre: str, clave: Any) -> Optional[Entity]:
        try:
            return self.obtener(nombre, clave)
        except SapError:
            return None

    def _validar_bodega(self, codigo: Optional[str], campo: str) -> None:
        if codigo and not self._existe("Warehouses", codigo):
            raise SapError(400, -10, f"{campo}: Warehouse '{codigo}' does not exist")

    def _validar_documento(self, nombre: str, col: Coleccion, cuerpo: Entity) -> None:
        if nombre in ("PurchaseOrders", "PurchaseDeliveryNotes"):
            card = cuerpo.get("CardCode")
            bp = self._existe("BusinessPartners", card) if card else None
            if not bp:
                raise SapError(400, -10, f"Invalid BP code '{card}'")
            if bp.get("Frozen") == "tYES" or bp.get("Valid") == "tNO":
                raise SapError(400, -10, f"Business partner '{card}' is inactive")
        self._validar_bodega(cuerpo.get("FromWarehouse"), "FromWarehouse")
        self._validar_bodega(cuerpo.get("ToWarehouse"), "ToWarehouse")
        lineas = cuerpo.get(col.lineas) if col.lineas else None
        if not lineas:
            raise SapError(400, -5002, "To generate this document, first define the item")
        for linea in lineas:
            item = self._existe("Items", linea.get("ItemCode"))
            if not item:
                raise SapError(400, -10, f"Item '{linea.get('ItemCode')}' does not exist")
            if item.get("Frozen") == "tYES" or item.get("Valid") == "tNO":
                raise SapError(400, -10, f"Item '{linea.get('ItemCode')}' is inactive")
            if nombre == "PurchaseOrders" and item.get("PurchaseItem") == "tNO":
                raise SapError(400, -10, f"Item '{linea.get('ItemCode')}' is not a purchase item")
            cantidad = linea.get("Quantity")
            if cantidad is None or float(cantidad) <= 0:
                raise SapError(400, -5002, f"Quantity must be greater than zero (item '{linea.get('ItemCode')}')")
            self._validar_bodega(linea.get("WarehouseCode"), "WarehouseCode")
