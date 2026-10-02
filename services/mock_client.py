"""
Cliente simulado (modo demo).

Imita lo mínimo del cliente de OpenAI/Azure que usa la plataforma
(`client.chat.completions.create`) para poder probar TODO el circuito
sin una API key: subida de PDFs, Knowledge Base, búsqueda de fragmentos,
chat del Playground, derivaciones y métricas.

IMPORTANTE: no es inteligencia artificial. Responde con reglas simples
y con fragmentos reales de tus PDFs. Sirve para validar que la plataforma
funciona, NO para evaluar la calidad de las respuestas del agente.
"""

import json
import re
from types import SimpleNamespace


# ============================================================
# UTILIDADES
# ============================================================

def _respuesta(contenido):

    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=contenido)
            )
        ]
    )


def _normalizar(texto):

    import unicodedata

    texto = unicodedata.normalize("NFD", (texto or "").lower())

    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def _contiene(texto, palabras):

    texto = _normalizar(texto)

    return any(_normalizar(p) in texto for p in palabras)


NEGATIVAS = [
    "no funciono", "no funciona", "no anda", "no sirvio", "no sirve",
    "sigue", "no se solucion", "no se resolvio", "no pude", "tampoco",
    "mismo error", "persiste", "igual que antes", "nada", "no se"
]

AFIRMATIVAS = [
    "funciono", "funciona", "listo", "resuelto", "solucionado",
    "se soluciono", "ya anda", "ya esta", "ya pude", "ya factura",
    "perfecto", "genial", "dale", "gracias"
]


def _es_afirmativo(texto):
    """Negaciones primero: 'no funciona' contiene 'funciona'."""

    t = _normalizar(texto).strip()

    if _contiene(t, NEGATIVAS):
        return False

    palabras = set(re.findall(r"[a-z]+", t))

    if palabras & {"si", "ok", "okey"}:
        return True

    return _contiene(t, AFIRMATIVAS)


def _estado_previo(contexto):

    coincidencia = re.search(
        r"ESTADO ACTUAL DE LA CONVERSACIÓN[^\n]*\n(\{.*\})",
        contexto
    )

    if not coincidencia:
        return {}

    try:
        return json.loads(coincidencia.group(1))
    except Exception:
        return {}


def _fragmentos(contexto):
    """Fragmentos de la KB que el servidor puso en el contexto."""

    resultado = []

    patron = re.compile(
        r"\[(F\d+)\] Documento: (.*?) \| .*?\n(.*?)(?=\n\n---\n\n\[F\d+\]|\Z)",
        re.DOTALL
    )

    for fid, documento, texto in patron.findall(contexto):

        pagina = re.search(r"Página (\d+)", contexto.split(f"[{fid}]")[1][:200])

        resultado.append({
            "id": fid,
            "documento": documento.strip(),
            "pagina": int(pagina.group(1)) if pagina else None,
            "texto": texto.strip(),
        })

    return resultado


def _extracto(texto, maximo=700):
    """Texto del fragmento, sin marcadores ni ruido, cortado en una frase."""

    limpio = re.sub(r"--- PÁGINA \d+ ---", "", texto)

    limpio = re.sub(
        r"\[(?:Imagen|Página escaneada) - página \d+ - [^\]]*\]",
        "",
        limpio
    )

    # Ruido típico de páginas web impresas: URLs, fechas y "1/1"
    limpio = re.sub(r"https?://\S+", "", limpio)

    limpio = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", "", limpio)

    limpio = re.sub(r"\b\d+/\d+\b", "", limpio)

    limpio = re.sub(r"\s+", " ", limpio).strip()

    if len(limpio) <= maximo:
        return limpio

    corte = limpio[:maximo]

    ultimo_punto = corte.rfind(". ")

    if ultimo_punto > maximo * 0.5:
        return corte[:ultimo_punto + 1] + " (…)"

    return corte.rsplit(" ", 1)[0] + " (…)"


# ============================================================
# AGENTE SIMULADO
# ============================================================

def _clasificar(mensaje):

    if _contiene(mensaje, ["base de datos", "sql", "tabla corrupta"]):
        return "Base de Datos", ""

    if (
        _contiene(mensaje, ["mercadolibre", "mercado libre", "meli",
                            "publicacion"])
        or re.search(r"\bml\b", _normalizar(mensaje))
    ):
        return "Ecommerce", "Mercado Libre"

    if _contiene(mensaje, ["tienda nube", "tiendanube", "znube", "ecommerce",
                           "e-commerce", "tienda online", "pedido",
                           "stock web"]):
        return "Ecommerce", "Tienda Nube"

    if _contiene(mensaje, ["cae", "factur", "comprobante", "afip", "arca",
                           "certificado", "punto de venta", "nota de credito"]):

        if _contiene(mensaje, ["cae"]):
            return "Facturación Electrónica", "CAE"

        return "Facturación Electrónica", ""

    if _contiene(mensaje, ["error de aplicacion", "excepcion", "se cierra",
                           "bug", "pantalla en blanco"]):
        return "Desarrollo", ""

    return "Otros", ""


def _estado_base(previo):

    estado = {
        "categoria": previo.get("categoria", ""),
        "subcategoria": previo.get("subcategoria", ""),
        "etapa": previo.get("etapa", "Recepción"),
        "problema_informado": previo.get("problema_informado", ""),
        "informacion_recopilada": list(previo.get("informacion_recopilada") or []),
        "informacion_faltante": [],
        "diagnostico": previo.get("diagnostico", ""),
        "solucion_propuesta": previo.get("solucion_propuesta", ""),
        "pasos_realizados": list(previo.get("pasos_realizados") or []),
        "resultado": previo.get("resultado", "Pendiente"),
        "validacion": previo.get("validacion", ""),
        "intentos_solucion": int(previo.get("intentos_solucion") or 0),
        "derivacion": previo.get("derivacion") or {
            "derivar": False, "destino": "", "motivo": ""
        },
    }

    return estado


def _derivar(estado, destino, motivo, texto):

    estado["etapa"] = "Derivado"
    estado["resultado"] = "No resuelto"
    estado["derivacion"] = {
        "derivar": True, "destino": destino, "motivo": motivo
    }

    return {
        "respuesta": texto,
        "estado": estado,
        "fragmentos_usados": [],
    }


def _agente(mensajes):

    contexto = mensajes[1]["content"]

    mensaje = mensajes[-1]["content"]

    previo = _estado_previo(contexto)

    estado = _estado_base(previo)

    fragmentos = _fragmentos(contexto)

    etapa = estado["etapa"]

    # ---- conversación ya cerrada ----
    if etapa in ("Resuelto", "Derivado"):

        return {
            "respuesta": (
                "Este caso ya está cerrado. Si tenés otro problema, "
                "iniciá una nueva conversación."
            ),
            "estado": estado,
            "fragmentos_usados": [],
        }

    # ---- 1) recepción: clasificar y preguntar ----
    if etapa in ("Recepción", "Comprensión", "Clasificación", ""):

        categoria, subcategoria = _clasificar(mensaje)

        if categoria == "Otros" and len(mensaje.split()) < 4:

            return {
                "respuesta": (
                    "¡Hola! Contame cuál es el problema que estás "
                    "teniendo con Dragonfish."
                ),
                "estado": estado,
                "fragmentos_usados": [],
            }

        estado["categoria"] = categoria
        estado["subcategoria"] = subcategoria
        estado["problema_informado"] = mensaje.strip()[:300]

        if categoria == "Base de Datos":

            estado["diagnostico"] = "Problema que involucra la base de datos."

            return _derivar(
                estado, "L2", "Problema de base de datos",
                "Entiendo. Este tipo de problema lo resuelve el equipo L2, "
                "así que te lo derivo con todo lo que me contaste, para que "
                "no tengas que repetirlo."
            )

        if categoria == "Desarrollo":

            return _derivar(
                estado, "Desarrollo", "Error de la aplicación",
                "Entiendo. Esto lo tiene que revisar el equipo de Desarrollo; "
                "te derivo el caso con la información que me diste."
            )

        estado["etapa"] = "Recopilación"
        estado["informacion_faltante"] = [
            "mensaje de error exacto", "qué estaba haciendo al ocurrir"
        ]

        return {
            "respuesta": (
                f"Gracias por contarme. Lo clasifiqué como {categoria}"
                f"{' / ' + subcategoria if subcategoria else ''}. "
                "Para ayudarte necesito dos datos: ¿qué mensaje de error "
                "te aparece exactamente? y ¿qué estabas haciendo cuando pasó?"
            ),
            "estado": estado,
            "fragmentos_usados": [],
        }

    # ---- 2) recopilación: proponer solución desde la KB, o derivar ----
    if etapa == "Recopilación":

        estado["informacion_recopilada"].append(mensaje.strip()[:200])

        if not fragmentos:

            destino = "Ecommerce" if estado["categoria"] == "Ecommerce" else "MDA"

            estado["diagnostico"] = (
                "No hay un procedimiento L1 documentado para este caso."
            )

            return _derivar(
                estado, destino,
                "No existe procedimiento documentado" if destino == "MDA"
                else "Caso de Ecommerce / Tienda Nube",
                "No tengo un procedimiento documentado para resolver esto "
                f"en nivel 1, así que lo derivo al equipo {destino} con "
                "toda la información que me diste."
            )

        f = fragmentos[0]

        estado["etapa"] = "Solución propuesta"
        estado["diagnostico"] = (
            f"Coincide con el procedimiento «{f['documento']}»."
        )
        estado["solucion_propuesta"] = f["documento"]

        return {
            "respuesta": (
                f"Encontré un procedimiento que aplica: «{f['documento']}»"
                f"{' (pág. ' + str(f['pagina']) + ')' if f['pagina'] else ''}."
                f"\n\nProbá lo siguiente:\n{_extracto(f['texto'])}\n\n"
                "¿Pudiste hacerlo? ¿Se solucionó?"
            ),
            "estado": estado,
            "fragmentos_usados": [f["id"]],
        }

    # ---- 3) solución propuesta: ¿funcionó? ----
    positivo = _es_afirmativo(mensaje)

    if etapa == "Solución propuesta" and positivo:

        estado["etapa"] = "Validación"
        estado["pasos_realizados"].append(
            f"{estado['solucion_propuesta']}: el cliente informa que funcionó"
        )

        return {
            "respuesta": (
                "¡Qué bueno! Para dar el caso por cerrado: ¿confirmás que "
                "ya podés trabajar con normalidad?"
            ),
            "estado": estado,
            "fragmentos_usados": [],
        }

    # ---- 4) validación final ----
    if etapa == "Validación" and positivo:

        estado["etapa"] = "Resuelto"
        estado["resultado"] = "Resuelto"
        estado["validacion"] = "Confirmado por el cliente (respuesta simulada)."

        return {
            "respuesta": "Perfecto, dejo el caso resuelto. ¡Gracias por escribir!",
            "estado": estado,
            "fragmentos_usados": [],
        }

    estado["intentos_solucion"] += 1

    estado["pasos_realizados"].append(
        f"{estado['solucion_propuesta']}: no resolvió"
    )

    if (
        etapa == "Validación"
        or estado["intentos_solucion"] >= 2
        or len(fragmentos) < 2
    ):

        return _derivar(
            estado, "L2", "El procedimiento L1 no resolvió el problema",
            "Lamentablemente el procedimiento no resolvió el problema, así "
            "que te derivo al equipo L2 con todo lo que probamos."
        )

    f = fragmentos[1]

    estado["etapa"] = "Solución propuesta"
    estado["solucion_propuesta"] = f["documento"]

    return {
        "respuesta": (
            "Probemos otra alternativa documentada: «"
            f"{f['documento']}»{' (pág. ' + str(f['pagina']) + ')' if f['pagina'] else ''}."
            f"\n\n{_extracto(f['texto'])}\n\n¿Se solucionó?"
        ),
        "estado": estado,
        "fragmentos_usados": [f["id"]],
    }


# ============================================================
# CLIENTE
# ============================================================

class _Completions:

    def create(self, **kwargs):

        mensajes = kwargs.get("messages") or []

        # Agente: pide respuesta JSON
        if kwargs.get("response_format"):

            return _respuesta(
                json.dumps(_agente(mensajes), ensure_ascii=False)
            )

        # Análisis de imagen
        return _respuesta(
            "[SIMULADO] Descripción de ejemplo de la imagen. En modo "
            "simulado no se analiza el contenido visual real: configurá una "
            "API key para obtener la descripción verdadera de la captura."
        )


class MockClient:

    tipo = "simulado"

    def __init__(self):

        self.chat = SimpleNamespace(completions=_Completions())
