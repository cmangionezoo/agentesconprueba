"""
Agente Técnico L1 - Dragonfish, zNube, Lince y Pantera.

Recibe el historial de la conversación y el estado interno anterior,
busca documentación en la Knowledge Base y devuelve:

- la respuesta para el cliente
- el estado interno actualizado (categoría, etapa, diagnóstico, etc.)
- el resumen técnico si corresponde derivar
- los fragmentos de la KB que se consultaron
"""

import json
import os
import re

from services.knowledge_service import (
    buscar_fragmentos,
    categorias_disponibles,
)
from services.tipificador_service import CATEGORIAS


# Productos que atiende el agente. El primero es el de por defecto.
PRODUCTOS = ["Dragonfish", "zNube", "Lince", "Pantera"]

PRODUCTO = PRODUCTOS[0]

_ALIAS_PRODUCTOS = {
    "dragonfish": "Dragonfish",
    "znube": "zNube",
    "zoonube": "zNube",
    "lince": "Lince",
    "pantera": "Pantera",
}


def normalizar_producto(valor, por_defecto=None):
    """
    Devuelve el nombre canónico del producto ("zNube", "Lince", etc.) o
    `por_defecto` si no coincide con ninguno. Ignora mayúsculas, tildes,
    espacios y guiones ("Z Nube", "z-nube" y "ZNUBE" dan "zNube").
    """

    clave = re.sub(r"[^a-z0-9]", "", str(valor or "").lower())

    return _ALIAS_PRODUCTOS.get(clave, por_defecto)

ETAPAS = [
    "Recepción",
    "Comprensión",
    "Clasificación",
    "Recopilación",
    "Diagnóstico",
    "Solución propuesta",
    "Validación",
    "Resuelto",
    "Derivado",
]

DESTINOS = ["L2", "Ecommerce", "Desarrollo", "MDA"]

# Tope de seguridad: cantidad de procedimientos documentados completos que
# no resolvieron el caso antes de derivar. Se puede cambiar desde Render con
# la variable AGENTE_MAX_INTENTOS.
try:
    MAX_INTENTOS = max(1, int(os.environ.get("AGENTE_MAX_INTENTOS", "5")))
except ValueError:
    MAX_INTENTOS = 5

MAX_MENSAJES_HISTORIAL = 16
MAX_CHARS_MENSAJE = 2000


SYSTEM_PROMPT = """
Sos el Agente Técnico L1 de __PRODUCTO__ de Zoo Logic. Atendés por WhatsApp
las consultas de los clientes sobre el sistema. El producto ya fue detectado
antes de que intervengas: es __PRODUCTO__. Hablá solo de __PRODUCTO__ y no
mezcles información de otros productos de Zoo Logic.

Una consulta puede ser una falla o error, una duda sobre cómo hacer algo, o
un pedido de información (por ejemplo, qué módulos hacen falta para una
integración). Todas son consultas y las atendés con el mismo criterio:
entender qué necesita el cliente y ayudarlo con lo que está documentado.
NO asumas que el cliente tiene un error ni le pidas un mensaje de error si
no está reportando una falla.

ALCANCE ACTUAL (piloto): Facturación Electrónica y Ecommerce. Si la consulta
es de otra categoría (Base de Datos, Mantenimiento, Configuración, Otros),
igual la clasificás y la tratás con las mismas reglas.

CÓMO TRABAJÁS
1. Si el cliente todavía no planteó su consulta, preguntale en qué lo podés
   ayudar.
2. Entendé qué necesita y clasificalo (categoría y subcategoría).
3. Identificá el tipo de mensaje y actuá según corresponda:
   - Falla o error ("no puedo obtener CAE", "me da un error"): recopilá la
     información que falta (mensaje exacto, qué estaba haciendo, desde cuándo
     pasa, en qué puesto o caja), diagnosticá con la documentación y guiá la
     solución UN paso por vez, esperando que el cliente confirme cada paso.
   - Cómo hacer algo ("cómo facturo", "cómo configuro la tienda"): NO pidas
     un mensaje de error. Si hace falta, preguntá qué quiere lograr y en qué
     punto está, y respondé con el procedimiento documentado, paso a paso.
   - Pedido de información ("qué módulos necesito", "qué incluye"): respondé
     directo con lo que dice la documentación, en pocas líneas y con tus
     palabras. No copies tablas ni textos largos tal cual, y no agregues
     datos que no estén en los fragmentos. Preguntá solo si te falta algo
     para responder bien.
   - Pregunta de seguimiento ("¿y cómo doy de alta los artículos?"):
     respondela con la documentación. No cuenta como un intento fallido.
   - Confusión ("no entiendo"): explicá lo mismo de otra manera, más simple,
     o dividilo en pasos más chicos. No cuenta como un intento fallido.
4. Validá antes de cerrar. En una falla, preguntá si se resolvió y confirmá
   el resultado final (por ejemplo, que ya pudo facturar). En una consulta
   informativa, preguntá si la información le sirvió o si necesita algo más.
   Nunca des algo por resuelto sin que el cliente lo confirme.
5. Si no se puede resolver o excede el nivel L1, derivá.

Etapas en consultas informativas: usá "Solución propuesta" cuando entregás la
información y "Validación" cuando preguntás si le sirvió. Cuando el cliente
confirma que quedó conforme, la etapa es "Resuelto" y el resultado "Resuelto".

REGLAS ESTRICTAS
- No inventes procedimientos, pasos, rutas de menú, nombres de botones ni
  datos técnicos. Usá únicamente lo que aparece en los fragmentos de
  documentación. Si un dato no está, no lo afirmes.
- No indiques procedimientos de nivel L2 como si fueran L1.
- Que una consulta sea informativa no es motivo para derivar: si la
  respuesta está en la documentación, respondela vos.
- Si el cliente quiere contratar, pedir un presupuesto o gestionar algo
  comercial que la documentación no resuelve, derivá a MDA aclarando en el
  motivo que es una solicitud de contratación o comercial.
- No pidas ni propongas modificaciones directas sobre bases de datos.
- Esforzate de verdad por resolver el caso. No derives por apuro ni solo
  porque pasaron algunos mensajes: seguí ayudando mientras haya
  procedimientos o pasos documentados que todavía no se probaron y el
  cliente esté avanzando.
- Derivá únicamente cuando: (1) ya no quedan procedimientos documentados
  distintos para probar, (2) el cliente repite el mismo resultado después de
  los pasos documentados, (3) el cliente pide hablar con una persona,
  (4) llegaste a __MAX_INTENTOS__ procedimientos documentados completos sin
  éxito (tope de seguridad), o (5) corresponde por las reglas de derivación.
- Si la documentación cubre solo una parte de la consulta, respondé esa
  parte y aclarale al cliente qué queda fuera de lo que podés resolver.
- Si no hay documentación relevante, no improvises. Hacé preguntas para
  entender el caso; cuando esté claro y siga sin haber documentación, derivá
  a MDA.
- No hagas más de dos preguntas por mensaje ni mensajes largos.
- Tono cordial y claro, en español rioplatense (voseo), sin tecnicismos
  innecesarios.

REGLAS DE DERIVACIÓN
- Consulta o problema de base de datos -> L2
- Ecommerce / Tienda Nube -> Ecommerce (si hay un procedimiento L1 documentado
  para ese caso, guialo primero; si no hay, recopilá la información y derivá)
- Desarrollo / error de la aplicación -> Desarrollo
- No existe procedimiento documentado -> MDA
- Un procedimiento L1 no resuelve -> L2
Cuando derives, avisale al cliente a qué equipo pasa el caso y que ya tiene
toda la información, para que no tenga que repetir lo que contó.

FORMATO DE RESPUESTA
Respondé SIEMPRE con un único objeto JSON válido, sin texto fuera del JSON:
{
  "respuesta": "mensaje para el cliente",
  "estado": {
    "categoria": "",
    "subcategoria": "",
    "etapa": "Recepción | Comprensión | Clasificación | Recopilación | Diagnóstico | Solución propuesta | Validación | Resuelto | Derivado",
    "problema_informado": "la consulta del cliente, resumida (puede ser un error, una duda o un pedido de información)",
    "informacion_recopilada": ["dato confirmado por el cliente", "..."],
    "informacion_faltante": ["dato que todavía necesitás", "..."],
    "diagnostico": "en una falla, la causa probable; en una consulta informativa, de qué trata lo consultado",
    "solucion_propuesta": "el procedimiento o la información que le diste al cliente",
    "pasos_realizados": ["paso que el cliente ya hizo y qué resultó (vacío en consultas informativas)", "..."],
    "resultado": "Pendiente | Resuelto | No resuelto",
    "validacion": "qué confirmó el cliente (se resolvió / le sirvió la información), o vacío",
    "intentos_solucion": 0,
    "derivacion": {
      "derivar": false,
      "destino": "L2 | Ecommerce | Desarrollo | MDA | vacío",
      "motivo": ""
    }
  },
  "fragmentos_usados": ["F1", "F2"]
}
- "fragmentos_usados": ids de los fragmentos de documentación en los que te
  basaste en este mensaje. Lista vacía si no usaste ninguno.
- "informacion_recopilada" solo incluye lo que el cliente dijo realmente.
- "intentos_solucion" cuenta solo los procedimientos documentados que el
  cliente completó y no resolvieron el caso. No cuentes preguntas, dudas ni
  pasos intermedios.
""".strip().replace("__MAX_INTENTOS__", str(MAX_INTENTOS))


def _prompt_para(producto, agente=None):
    """
    Prompt del agente. `agente` (opcional) es un dict con:
    categoria ('' = recepción), instrucciones y especialistas (lista de
    categorías que tienen un especialista activo en este producto).
    """

    agente = agente or {}

    categoria = agente.get("categoria") or ""
    especialistas = agente.get("especialistas") or []
    instrucciones = (agente.get("instrucciones") or "").strip()

    secciones = [
        "CATEGORÍAS VÁLIDAS\n"
        "El campo \"categoria\" del estado tiene que ser EXACTAMENTE una de "
        "estas: " + ", ".join(CATEGORIAS) + ". Usá \"Otros\" solo si "
        "ninguna encaja."
    ]

    if categoria:

        secciones.append(
            "ROL DE ESTE AGENTE\n"
            f"Sos el especialista en {categoria} de {producto}. El caso ya "
            "fue clasificado por el agente de recepción (mirá el estado "
            "actual). Resolvé solo consultas de esa categoría, con la "
            "documentación que te llegue. No te presentes de nuevo ni "
            "saludes otra vez: seguí la conversación con naturalidad, como "
            f"el mismo asistente de {producto}. Si el cliente pasa a otro "
            "tema que corresponde a otra categoría, poné esa categoría en "
            "el estado y el sistema lo pasa al especialista que corresponda."
        )

    elif especialistas:

        secciones.append(
            "ROL DE ESTE AGENTE\n"
            f"Sos el agente de recepción de {producto}. Tu tarea principal "
            "es entender qué necesita el cliente y clasificar la consulta "
            "(categoría y subcategoría) lo antes posible. Hay especialistas "
            "para: " + ", ".join(especialistas) + ". En cuanto la categoría "
            "esté clara, completala en el estado: el sistema pasa el caso "
            "automáticamente al especialista, así que no hace falta que lo "
            "resuelvas vos. Si todavía no está claro qué necesita, hacé UNA "
            "pregunta corta. Para las categorías sin especialista, resolvé "
            "el caso vos como siempre."
        )

    if instrucciones:

        secciones.append(
            "INSTRUCCIONES PROPIAS DE ESTE AGENTE (tienen prioridad sobre "
            "las generales, salvo las reglas estrictas y el formato de "
            "respuesta)\n" + instrucciones
        )

    texto = SYSTEM_PROMPT.replace("__PRODUCTO__", producto)

    adicional = "\n\n".join(secciones)

    if "FORMATO DE RESPUESTA" in texto:
        return texto.replace(
            "FORMATO DE RESPUESTA",
            adicional + "\n\nFORMATO DE RESPUESTA",
            1
        )

    return texto + "\n\n" + adicional


# ============================================================
# UTILIDADES
# ============================================================

def _limpiar_historial(historial):

    limpio = []

    if not isinstance(historial, list):
        return limpio

    for item in historial[-MAX_MENSAJES_HISTORIAL:]:

        if not isinstance(item, dict):
            continue

        rol = item.get("rol")

        texto = str(item.get("texto") or "").strip()[:MAX_CHARS_MENSAJE]

        if rol not in ("cliente", "agente") or not texto:
            continue

        limpio.append({"rol": rol, "texto": texto})

    return limpio


def _como_lista(valor):

    if isinstance(valor, list):
        return [str(v).strip() for v in valor if str(v).strip()]

    if isinstance(valor, str) and valor.strip():
        return [valor.strip()]

    return []


def _parsear_json(texto):

    texto = (texto or "").strip()

    texto = re.sub(r"^```(?:json)?|```$", "", texto, flags=re.MULTILINE).strip()

    try:
        return json.loads(texto)
    except Exception:
        pass

    inicio = texto.find("{")
    fin = texto.rfind("}")

    if inicio != -1 and fin > inicio:
        return json.loads(texto[inicio:fin + 1])

    raise ValueError("La respuesta del modelo no es un JSON válido.")


def estado_inicial(producto=None):

    return {
        "producto": normalizar_producto(producto, PRODUCTO),
        "categoria": "",
        "subcategoria": "",
        "etapa": "Recepción",
        "problema_informado": "",
        "informacion_recopilada": [],
        "informacion_faltante": [],
        "diagnostico": "",
        "documentos_consultados": [],
        "solucion_propuesta": "",
        "pasos_realizados": [],
        "resultado": "Pendiente",
        "validacion": "",
        "intentos_solucion": 0,
        "derivacion": {"derivar": False, "destino": "", "motivo": ""},
    }


def _normalizar_estado(bruto, previo, fragmentos, usados, producto=None):

    base = estado_inicial(producto)

    if isinstance(previo, dict):
        base.update({k: v for k, v in previo.items() if k in base})

    if not isinstance(bruto, dict):
        bruto = {}

    estado = dict(base)

    estado["producto"] = normalizar_producto(producto, PRODUCTO)

    for clave in (
        "categoria", "subcategoria", "problema_informado",
        "diagnostico", "solucion_propuesta", "validacion"
    ):
        estado[clave] = str(bruto.get(clave, base.get(clave, "")) or "").strip()

    etapa = str(bruto.get("etapa") or base["etapa"]).strip()

    estado["etapa"] = etapa if etapa in ETAPAS else base["etapa"]

    for clave in (
        "informacion_recopilada", "informacion_faltante", "pasos_realizados"
    ):
        estado[clave] = _como_lista(bruto.get(clave, base.get(clave)))

    resultado = str(bruto.get("resultado") or "Pendiente").strip()

    estado["resultado"] = (
        resultado if resultado in ("Pendiente", "Resuelto", "No resuelto")
        else "Pendiente"
    )

    try:
        estado["intentos_solucion"] = int(bruto.get("intentos_solucion", 0))
    except Exception:
        estado["intentos_solucion"] = base.get("intentos_solucion", 0)

    # Documentos consultados: se arman del lado del servidor a partir de
    # los fragmentos realmente recuperados (el modelo no puede inventarlos).
    por_id = {f["id"]: f for f in fragmentos}

    consultados = list(base.get("documentos_consultados") or [])

    for fid in _como_lista(usados):

        f = por_id.get(fid)

        if not f:
            continue

        item = {"documento": f["documento"], "pagina": f["pagina"]}

        if item not in consultados:
            consultados.append(item)

    estado["documentos_consultados"] = consultados

    deriv = bruto.get("derivacion") if isinstance(bruto.get("derivacion"), dict) else {}

    derivar = bool(deriv.get("derivar"))

    destino = str(deriv.get("destino") or "").strip()

    if derivar and destino not in DESTINOS:
        destino = "MDA"

    estado["derivacion"] = {
        "derivar": derivar,
        "destino": destino if derivar else "",
        "motivo": str(deriv.get("motivo") or "").strip() if derivar else "",
    }

    if derivar:
        estado["etapa"] = "Derivado"

    return estado


def armar_resumen_tecnico(estado):

    d = estado["derivacion"]

    return {
        "producto": estado["producto"],
        "categoria": estado["categoria"],
        "subcategoria": estado["subcategoria"],
        "problema_informado": estado["problema_informado"],
        "informacion_recopilada": estado["informacion_recopilada"],
        "diagnostico": estado["diagnostico"],
        "procedimientos_realizados": estado["pasos_realizados"],
        "documentos_consultados": estado["documentos_consultados"],
        "resultado": estado["resultado"],
        "destino": d["destino"],
        "motivo_derivacion": d["motivo"],
    }


def _formatear_contexto(estado_previo, fragmentos, categorias):

    partes = []

    partes.append(
        "ESTADO ACTUAL DE LA CONVERSACIÓN (JSON, de tu mensaje anterior):\n"
        + json.dumps(
            {k: v for k, v in estado_previo.items()
             if k != "documentos_consultados"},
            ensure_ascii=False
        )
    )

    if categorias:

        partes.append(
            "CATEGORÍAS / SUBCATEGORÍAS QUE EXISTEN EN LA KNOWLEDGE BASE "
            "(usalas para clasificar cuando coincidan):\n"
            + "\n".join(
                f"- {c} / {s}" if s else f"- {c}"
                for c, s in categorias
            )
        )

    if fragmentos:

        bloques = []

        for f in fragmentos:

            aviso = (
                " (PENDIENTE DE REVISIÓN - solo pruebas)"
                if f["estado"] != "Vigente" else ""
            )

            pagina = f" | Página {f['pagina']}" if f["pagina"] else ""

            bloques.append(
                f"[{f['id']}] Documento: {f['documento']} | "
                f"{f['categoria']} / {f['subcategoria']} | "
                f"Nivel {f['nivel']}{pagina}{aviso}\n{f['texto']}"
            )

        partes.append(
            "DOCUMENTACIÓN RELEVANTE ENCONTRADA EN LA KNOWLEDGE BASE "
            "(única fuente permitida para diagnosticar y proponer "
            "soluciones):\n\n" + "\n\n---\n\n".join(bloques)
        )

    else:

        partes.append(
            "DOCUMENTACIÓN RELEVANTE: no se encontró documentación en la "
            "Knowledge Base para esta consulta. No podés proponer ni guiar "
            "ninguna solución. Podés hacer preguntas para entender el caso; "
            "si el problema ya está claro, derivá a MDA."
        )

    return "\n\n".join(partes)


def _usa_temperatura(modelo):

    modelo = (modelo or "").lower()

    return not (modelo.startswith("o") or modelo.startswith("gpt-5"))


# ============================================================
# RESPUESTA DEL AGENTE
# ============================================================

def responder(
    client,
    modelo,
    documentos,
    historial,
    mensaje,
    estado_previo=None,
    incluir_pendientes=True,
    producto=None,
    agente=None
):

    historial = _limpiar_historial(historial)

    # Producto de la conversación: el que se pidió; si no, el que ya traía
    # el estado; si no, el de por defecto.
    producto = normalizar_producto(
        producto,
        normalizar_producto(
            (estado_previo or {}).get("producto")
            if isinstance(estado_previo, dict) else None,
            PRODUCTO
        )
    )

    estado_previo = (
        estado_previo if isinstance(estado_previo, dict)
        else estado_inicial(producto)
    )

    # Consulta a la KB: último mensaje + contexto reciente del cliente
    mensajes_cliente = [m["texto"] for m in historial if m["rol"] == "cliente"]

    consulta = " ".join(
        mensajes_cliente[-3:]
        + [
            mensaje,
            str(estado_previo.get("problema_informado") or ""),
            str(estado_previo.get("categoria") or ""),
            str(estado_previo.get("subcategoria") or ""),
        ]
    )

    fragmentos = buscar_fragmentos(
        documentos,
        consulta,
        producto=producto,
        incluir_pendientes=incluir_pendientes,
        categoria=(
            str(estado_previo.get("categoria") or "")
            or str((agente or {}).get("categoria") or "")
            or None
        )
    )

    categorias = categorias_disponibles(
        documentos,
        incluir_pendientes=incluir_pendientes,
        producto=producto
    )

    mensajes = [
        {"role": "system", "content": _prompt_para(producto, agente)},
        {
            "role": "system",
            "content": _formatear_contexto(
                estado_previo, fragmentos, categorias
            )
        },
    ]

    for item in historial:

        mensajes.append({
            "role": "user" if item["rol"] == "cliente" else "assistant",
            "content": item["texto"]
        })

    mensajes.append({"role": "user", "content": mensaje})

    parametros = {
        "model": modelo,
        "messages": mensajes,
        "response_format": {"type": "json_object"},
    }

    if _usa_temperatura(modelo):
        parametros["temperature"] = 0.2

    try:

        respuesta_modelo = client.chat.completions.create(**parametros)

    except Exception as e:

        # Algunos modelos (por ejemplo de razonamiento) no aceptan
        # "temperature". Se reintenta sin ese parámetro.
        if "temperature" in str(e).lower() and "temperature" in parametros:

            parametros.pop("temperature")

            respuesta_modelo = client.chat.completions.create(**parametros)

        else:

            raise

    bruto = _parsear_json(respuesta_modelo.choices[0].message.content)

    estado = _normalizar_estado(
        bruto.get("estado"),
        estado_previo,
        fragmentos,
        bruto.get("fragmentos_usados"),
        producto
    )

    avisos = []

    if (
        not fragmentos
        and estado["etapa"] in ("Solución propuesta", "Validación")
        and not estado["derivacion"]["derivar"]
    ):
        avisos.append(
            "El agente propuso o validó una solución pero no se recuperó "
            "documentación de la KB en este turno. Revisá la respuesta."
        )

    if (
        estado["intentos_solucion"] >= MAX_INTENTOS
        and not estado["derivacion"]["derivar"]
    ):
        avisos.append(
            f"Se alcanzó el tope de {MAX_INTENTOS} intentos sin derivar. "
            "Revisá si el agente debería haber derivado."
        )

    if (
        fragmentos
        and any(f["estado"] != "Vigente" for f in fragmentos)
    ):
        avisos.append(
            "Se usaron documentos pendientes de revisión (modo prueba)."
        )

    resumen = (
        armar_resumen_tecnico(estado)
        if estado["derivacion"]["derivar"] else None
    )

    return {
        "respuesta": str(bruto.get("respuesta") or "").strip()
        or "No pude generar una respuesta. ¿Podés repetir el mensaje?",
        "estado": estado,
        "resumen_tecnico": resumen,
        "fragmentos_usados": [
            fid for fid in _como_lista(bruto.get("fragmentos_usados"))
            if fid in {f["id"] for f in fragmentos}
        ],
        "fragmentos": [
            {
                "id": f["id"],
                "doc_id": f["doc_id"],
                "documento": f["documento"],
                "pagina": f["pagina"],
                "puntaje": f["puntaje"],
                "estado": f["estado"],
            }
            for f in fragmentos
        ],
        "avisos": avisos,
    }
