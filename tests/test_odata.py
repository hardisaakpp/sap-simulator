import pytest

from app.odata import ODataError, consultar, parse_filter

ITEMS = [
    {"ItemCode": "A-1", "ItemName": "Camisa Azul", "Price": 10, "Valid": "tYES", "U_sap_id": "x'1"},
    {"ItemCode": "B-2", "ItemName": "Jean Negro", "Price": 30, "Valid": "tNO", "U_sap_id": None},
    {"ItemCode": "C-3", "ItemName": "Gorra Azul", "Price": 5, "Valid": "tYES", "U_sap_id": None},
]


def codigos(filtro):
    pred = parse_filter(filtro)
    return [i["ItemCode"] for i in ITEMS if pred(i)]


def test_eq_or_como_items_batch_del_middleware():
    assert codigos("ItemCode eq 'A-1' or ItemCode eq 'C-3'") == ["A-1", "C-3"]


def test_and_parentesis_y_not():
    assert codigos("(ItemCode eq 'A-1' or ItemCode eq 'B-2') and Valid eq 'tYES'") == ["A-1"]
    assert codigos("not (Valid eq 'tYES')") == ["B-2"]


def test_comparadores_numericos():
    assert codigos("Price ge 10") == ["A-1", "B-2"]
    assert codigos("Price lt 10") == ["C-3"]


def test_contains_sin_distinguir_mayusculas_y_comillas_escapadas():
    assert codigos("contains(ItemName,'azul')") == ["A-1", "C-3"]
    assert codigos("contains(ItemCode,'a') or contains(ItemName,'NEGRO')") == ["A-1", "B-2"]
    assert codigos("U_sap_id eq 'x''1'") == ["A-1"]
    assert codigos("U_sap_id eq null") == ["B-2", "C-3"]


def test_sintaxis_invalida():
    with pytest.raises(ODataError):
        parse_filter("ItemCode eq")
    with pytest.raises(ODataError):
        parse_filter("xmatch(ItemCode,'A')")


def test_paginacion_como_service_layer():
    r = consultar(ITEMS, {}, tamano_pagina=2)
    assert [v["ItemCode"] for v in r.valores] == ["A-1", "B-2"] and r.siguiente_skip == 2
    r = consultar(ITEMS, {"$skip": "2"}, tamano_pagina=2)
    assert [v["ItemCode"] for v in r.valores] == ["C-3"] and r.siguiente_skip is None


def test_idempotencia_orderby_top_select():
    docs = [{"DocEntry": 1, "U_sap_id": "k"}, {"DocEntry": 2, "U_sap_id": "k"}, {"DocEntry": 3, "U_sap_id": "z"}]
    r = consultar(docs, {"$filter": "U_sap_id eq 'k'", "$orderby": "DocEntry desc", "$top": "1",
                         "$select": "DocEntry"}, tamano_pagina=20)
    assert r.valores == [{"DocEntry": 2}] and r.siguiente_skip is None
