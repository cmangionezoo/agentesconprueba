"""
Enrutador de agentes.

Flujo de una conversación:

1. El cliente llega con su producto ya identificado (uContact).
2. Lo recibe el agente de recepción de ese producto.
3. Cuando ese agente clasifica la consulta en una categoría y hay un
   especialista activo para (producto, categoría), el caso pasa a él en el
   mismo turno: el cliente recibe directamente la respuesta del especialista.
4. Si más adelante el cliente cambia de tema y el especialista detecta otra
   categoría con especialista, se reasigna. Hay un tope de reasignaciones por
   conversación para que nunca entre en un ida y vuelta.
"""

import re
import unicodedata

from services import agentes_service
from services.tipificador_service import CATEGORIAS


MAX_REASIGNACIONES = 3


def _clave(texto):

    texto = unicodedata.normalize("NFKD", str(texto or ""))

    texto = "".join(c for c in texto if not unicodedata.combining(c))

    return re.sub(r"[^a-z0-9]", "", texto.lower())


def categoria_canonica(valor):
    """
    Lleva lo que escribió el modelo a una categoría de la lista
    ("facturacion" -> "Facturación Electrónica"). '' si no coincide.
    """

    buscado = _clave(valor)

    if not buscado:
        return ""

    for categoria in CATEGORIAS:
        if _clave(categoria) == buscado:
            return categoria

    # "Facturación" a secas, "Ecommerce / Tienda Nube", etc.
    if len(buscado) >= 5:

        for categoria in CATEGORIAS:

            clave = _clave(categoria)

            if clave.startswith(buscado) or buscado.startswith(clave):
                return categoria

    return ""


def _contexto(agente, producto):
    """Lo que el prompt necesita saber del agente."""

    return {
        "categoria": agente["categoria"] if agente else "",
        "instrucciones": agente["instrucciones"] if agente else "",
        "especialistas": agentes_service.categorias_con_especialista(producto),
    }


def _elegir_inicial(producto, agente_id, ignorar_pausa_recepcion):

    actual = agentes_service.obtener(agente_id)

    if (
        actual and actual["producto"] == producto and actual["activo"]
    ):
        return actual

    base = agentes_service.obtener_base(producto)

    if base and (base["activo"] or ignorar_pausa_recepcion):
        return base

    return None


def _destino(producto, categoria, agente):
    """El agente al que hay que pasar el caso, o None si no hay que moverlo."""

    if not categoria:
        return None

    if agente and agente["categoria"] == categoria:
        return None

    especialista = agentes_service.obtener_especialista(producto, categoria)

    if especialista and (not agente or especialista["id"] != agente["id"]):
        return especialista

    return None


def atender(responder, producto, estado_previo, agente_id=None,
            reasignaciones=0, ignorar_pausa_recepcion=False):
    """
    `responder(contexto_agente, estado_previo)` ejecuta un turno con un agente
    y devuelve el resultado del agente (con "estado").

    Devuelve el resultado final más:
    - agente: {id, nombre, categoria} del agente que respondió
    - agente_inicial: id del agente con el que arrancó este turno
    - reasignaciones: total acumulado en la conversación
    - ruta: lista de nombres de agentes que intervinieron en este turno
    """

    agente = _elegir_inicial(producto, agente_id, ignorar_pausa_recepcion)

    inicial = agente

    ruta = [agente["nombre"]] if agente else []

    resultado = responder(_contexto(agente, producto), estado_previo)

    estado = resultado["estado"]

    canonica = categoria_canonica(estado.get("categoria"))

    if canonica:
        estado["categoria"] = canonica

    terminado = (
        estado.get("etapa") in ("Resuelto", "Derivado")
        or (estado.get("derivacion") or {}).get("derivar")
    )

    # Un solo cambio de agente por turno (máximo dos consultas a la IA)
    if not terminado and reasignaciones < MAX_REASIGNACIONES:

        destino = _destino(producto, canonica, agente)

        if destino:

            agente = destino

            reasignaciones += 1

            ruta.append(agente["nombre"])

            resultado = responder(_contexto(agente, producto), estado)

            estado = resultado["estado"]

            nueva = categoria_canonica(estado.get("categoria"))

            if nueva:
                estado["categoria"] = nueva

    resultado["agente"] = (
        {
            "id": agente["id"], "nombre": agente["nombre"],
            "categoria": agente["categoria"],
        } if agente else {"id": 0, "nombre": "", "categoria": ""}
    )

    resultado["agente_inicial"] = inicial["id"] if inicial else 0
    resultado["reasignaciones"] = reasignaciones
    resultado["ruta"] = ruta

    return resultado