import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("SAP_COMPANY_DB", "SIMULADOR")
    import app.main as main
    main = importlib.reload(main)
    c = TestClient(main.app)
    r = c.post("/b1s/v1/Login", json={"CompanyDB": "SIMULADOR", "UserName": "u", "Password": "p"})
    assert r.status_code == 200 and r.json()["SessionId"]
    c.cookies.set("B1SESSION", r.json()["SessionId"])
    return c


def test_login_rechaza_compania_incorrecta(client):
    r = client.post("/b1s/v1/Login", json={"CompanyDB": "OTRA", "UserName": "u", "Password": "p"})
    assert r.status_code == 401 and r.json()["error"]["code"] == -304


def test_sin_sesion_devuelve_401(client):
    client.cookies.clear()
    assert client.get("/b1s/v1/Items").status_code == 401


def test_item_por_clave_select_y_precios(client):
    r = client.get("/b1s/v1/Items('R9001.101.NEG')", params={"$select": "ItemCode,U_IXX_PACK"})
    assert r.json()["U_IXX_PACK"] == "X6" and "ItemName" not in r.json()
    precios = client.get("/b1s/v1/Items('R9001.101.NEG')/ItemPrices").json()["ItemPrices"]
    assert {"PriceList": 1, "Price": 12.99, "Currency": "USD"} in precios


def test_item_inexistente_404_formato_sap(client):
    r = client.get("/b1s/v1/Items('NO-EXISTE')")
    assert r.status_code == 404 and r.json()["error"]["code"] == -2028


def test_paginacion_next_link(client):
    r = client.get("/b1s/v1/Warehouses", params={"$select": "WarehouseCode"})
    cuerpo = r.json()
    assert len(cuerpo["value"]) == 12 and "odata.nextLink" not in cuerpo  # 12 < página de 20


def test_crear_orden_de_compra_e_idempotencia(client):
    oc = {"CardCode": "P1790389219001", "U_sap_id": "ODC-1",
          "DocumentLines": [{"ItemCode": "R9001.101.NEG", "Quantity": 2, "UnitPrice": 5.6, "WarehouseCode": "RL-CD"}]}
    r = client.post("/b1s/v1/PurchaseOrders", json=oc)
    assert r.status_code == 201
    doc = r.json()
    assert doc["DocEntry"] == 1 and doc["DocNum"] == 900001 and doc["CardName"] == "TEXTILES EL RAYO S.A."
    busqueda = client.get("/b1s/v1/PurchaseOrders", params={
        "$filter": "U_sap_id eq 'ODC-1'", "$select": "DocEntry,DocNum", "$orderby": "DocEntry desc", "$top": "1"})
    assert busqueda.json()["value"] == [{"DocEntry": 1, "DocNum": 900001}]


@pytest.mark.parametrize("cambio, mensaje", [
    ({"CardCode": "NO-EXISTE"}, "Invalid BP code"),
    ({"CardCode": "P0000000000001"}, "inactive"),
    ({"DocumentLines": [{"ItemCode": "NO-EXISTE", "Quantity": 1}]}, "does not exist"),
    ({"DocumentLines": [{"ItemCode": "R9001.101.NEG", "Quantity": 0}]}, "greater than zero"),
    ({"DocumentLines": [{"ItemCode": "R9001.101.NEG", "Quantity": 1, "WarehouseCode": "XX"}]}, "Warehouse 'XX'"),
])
def test_validaciones_tipo_sap(client, cambio, mensaje):
    oc = {"CardCode": "P1790389219001", "DocumentLines": [{"ItemCode": "R9001.101.NEG", "Quantity": 1}], **cambio}
    r = client.post("/b1s/v1/PurchaseOrders", json=oc)
    assert r.status_code == 400 and mensaje in r.json()["error"]["message"]["value"]


def test_recurso_no_simulado(client):
    r = client.get("/b1s/v1/JournalEntries")
    assert r.status_code == 404 and "no simulado" in r.json()["error"]["message"]["value"]
