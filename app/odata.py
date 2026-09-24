"""Subconjunto de OData v3 que usa SAP Business One Service Layer.

Soporta lo que consume api-middleware-sap:
  $filter  -> eq, ne, gt, ge, lt, le, and, or, not, paréntesis,
              contains(), startswith(), endswith(), substringof(), tolower(), toupper()
  $select, $orderby (asc/desc, varios campos), $top, $skip, $inlinecount=allpages
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

Entity = Dict[str, Any]
Predicate = Callable[[Entity], bool]


class ODataError(ValueError):
    """Consulta OData inválida o no soportada (el Service Layer responde 400)."""


# ------------------------------------------------------------------ tokenizer
_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<string>'(?:[^']|'')*')          |
        (?P<number>-?\d+(?:\.\d+)?)(?![\w]) |
        (?P<ident>[A-Za-z_][\w/]*)          |
        (?P<punct>[(),])
    )""",
    re.VERBOSE,
)


@dataclass
class _Tok:
    kind: str
    value: Any


def _tokenize(text: str) -> List[_Tok]:
    tokens: List[_Tok] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise ODataError(f"Sintaxis $filter no soportada cerca de: '{text[pos:pos + 30]}'")
        pos = m.end()
        if m.group("string") is not None:
            tokens.append(_Tok("str", m.group("string")[1:-1].replace("''", "'")))
        elif m.group("number") is not None:
            raw = m.group("number")
            tokens.append(_Tok("num", float(raw) if "." in raw else int(raw)))
        elif m.group("ident") is not None:
            tokens.append(_Tok("ident", m.group("ident")))
        else:
            tokens.append(_Tok("punct", m.group("punct")))
    return tokens


# ------------------------------------------------------------------ parser
_COMPARADORES = {"eq", "ne", "gt", "ge", "lt", "le"}
_FUNCIONES_BOOL = {"contains", "startswith", "endswith", "substringof"}
_FUNCIONES_VALOR = {"tolower", "toupper", "trim"}
_LITERALES = {"null": None, "true": True, "false": False}

Operand = Callable[[Entity], Any]


def _norm(valor: Any) -> Any:
    return valor.lower() if isinstance(valor, str) else valor


def _comparar(op: str, a: Any, b: Any) -> bool:
    if op == "eq":
        return a == b
    if op == "ne":
        return a != b
    if a is None or b is None:
        return False
    try:
        return {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[op]
    except TypeError:
        return False


class _Parser:
    def __init__(self, tokens: List[_Tok]):
        self.toks = tokens
        self.i = 0

    def _peek(self) -> Optional[_Tok]:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _next(self) -> _Tok:
        tok = self._peek()
        if tok is None:
            raise ODataError("$filter incompleto")
        self.i += 1
        return tok

    def _es_palabra(self, palabra: str) -> bool:
        tok = self._peek()
        return tok is not None and tok.kind == "ident" and tok.value.lower() == palabra

    def _esperar(self, punct: str) -> None:
        tok = self._next()
        if tok.kind != "punct" or tok.value != punct:
            raise ODataError(f"Se esperaba '{punct}' en $filter")

    def parse(self) -> Predicate:
        pred = self._or()
        if self._peek() is not None:
            raise ODataError(f"Token inesperado en $filter: {self._peek().value!r}")
        return pred

    def _or(self) -> Predicate:
        partes = [self._and()]
        while self._es_palabra("or"):
            self._next()
            partes.append(self._and())
        return partes[0] if len(partes) == 1 else (lambda e, p=partes: any(f(e) for f in p))

    def _and(self) -> Predicate:
        partes = [self._unario()]
        while self._es_palabra("and"):
            self._next()
            partes.append(self._unario())
        return partes[0] if len(partes) == 1 else (lambda e, p=partes: all(f(e) for f in p))

    def _unario(self) -> Predicate:
        if self._es_palabra("not"):
            self._next()
            interno = self._unario()
            return lambda e: not interno(e)
        tok = self._peek()
        if tok is not None and tok.kind == "punct" and tok.value == "(":
            self._next()
            interno = self._or()
            self._esperar(")")
            return interno
        return self._comparacion()

    def _comparacion(self) -> Predicate:
        izq = self._operando()
        tok = self._peek()
        if tok is not None and tok.kind == "ident" and tok.value.lower() in _COMPARADORES:
            op = self._next().value.lower()
            der = self._operando()
            return lambda e: _comparar(op, izq(e), der(e))
        return lambda e: bool(izq(e))

    def _operando(self) -> Operand:
        tok = self._next()
        if tok.kind in ("str", "num"):
            return lambda e, v=tok.value: v
        if tok.kind != "ident":
            raise ODataError(f"Operando inesperado en $filter: {tok.value!r}")
        nombre = tok.value
        siguiente = self._peek()
        if siguiente is not None and siguiente.kind == "punct" and siguiente.value == "(":
            return self._funcion(nombre.lower())
        if nombre.lower() in _LITERALES:
            return lambda e, v=_LITERALES[nombre.lower()]: v
        return lambda e, campo=nombre: _valor_campo(e, campo)

    def _funcion(self, nombre: str) -> Operand:
        self._esperar("(")
        args: List[Operand] = []
        while True:
            args.append(self._operando())
            tok = self._next()
            if tok.kind == "punct" and tok.value == ")":
                break
            if not (tok.kind == "punct" and tok.value == ","):
                raise ODataError(f"Se esperaba ',' o ')' en {nombre}()")
        if nombre in _FUNCIONES_VALOR and len(args) == 1:
            fn = {"tolower": lambda v: v.lower(), "toupper": lambda v: v.upper(), "trim": lambda v: v.strip()}[nombre]
            return lambda e, a=args[0]: fn(a(e)) if isinstance(a(e), str) else a(e)
        if nombre in _FUNCIONES_BOOL and len(args) == 2:
            # SAP sobre SQL Server compara sin distinguir mayúsculas; aquí igual.
            if nombre == "substringof":  # substringof('texto', Campo)
                return lambda e, t=args[0], c=args[1]: _norm(t(e)) in _norm(c(e) or "")
            fn = {
                "contains": lambda campo, txt: txt in campo,
                "startswith": lambda campo, txt: campo.startswith(txt),
                "endswith": lambda campo, txt: campo.endswith(txt),
            }[nombre]
            return lambda e, c=args[0], t=args[1]: isinstance(c(e), str) and fn(_norm(c(e)), _norm(t(e)))
        raise ODataError(f"Función OData no soportada por el simulador: {nombre}()")


def _valor_campo(entidad: Entity, campo: str) -> Any:
    valor: Any = entidad
    for parte in campo.split("/"):
        valor = valor.get(parte) if isinstance(valor, dict) else None
    return valor


def parse_filter(texto: Optional[str]) -> Optional[Predicate]:
    if not texto or not texto.strip():
        return None
    return _Parser(_tokenize(texto)).parse()


# ------------------------------------------------------------------ consulta completa
def _ordenar(entidades: List[Entity], orderby: str) -> List[Entity]:
    resultado = list(entidades)
    # Orden estable: se aplica del último criterio al primero.
    for criterio in reversed([c.strip() for c in orderby.split(",") if c.strip()]):
        partes = criterio.split()
        campo = partes[0]
        desc = len(partes) > 1 and partes[1].lower() == "desc"
        resultado.sort(
            key=lambda e: (_valor_campo(e, campo) is None, _valor_campo(e, campo) if _valor_campo(e, campo) is not None else 0),
            reverse=desc,
        )
    return resultado


def seleccionar(entidad: Entity, select: Optional[str]) -> Entity:
    if not select:
        return dict(entidad)
    campos = [c.strip() for c in select.split(",") if c.strip()]
    return {c: entidad[c] for c in campos if c in entidad}


@dataclass
class ResultadoConsulta:
    valores: List[Entity]
    total: int
    siguiente_skip: Optional[int]


def consultar(
    entidades: List[Entity],
    params: Dict[str, str],
    tamano_pagina: int,
) -> ResultadoConsulta:
    predicado = parse_filter(params.get("$filter"))
    filtradas = [e for e in entidades if predicado is None or predicado(e)]
    if params.get("$orderby"):
        filtradas = _ordenar(filtradas, params["$orderby"])

    skip = int(params.get("$skip", 0) or 0)
    top = int(params["$top"]) if params.get("$top") else None
    # Como SAP: la página nunca supera tamano_pagina aunque $top pida más.
    limite = tamano_pagina if top is None else min(top, tamano_pagina)
    pagina = filtradas[skip:skip + limite]

    restantes_solicitados = (len(filtradas) - skip) if top is None else min(top, len(filtradas) - skip)
    siguiente = skip + len(pagina) if restantes_solicitados > len(pagina) else None
    valores = [seleccionar(e, params.get("$select")) for e in pagina]
    return ResultadoConsulta(valores=valores, total=len(filtradas), siguiente_skip=siguiente)
