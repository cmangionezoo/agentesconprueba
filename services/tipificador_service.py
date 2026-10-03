"""
Tipificación automática de documentos con IA.

Lee el nombre, la carpeta de origen y el contenido del PDF y propone:
producto (si no se sabe), categoría, subcategoría, fuente
y descripción. La categoría tiene que ser una de las TIPIFICACIONES del
producto del documento (cada producto tiene las suyas). Solo se aceptan
valores de las listas de la plataforma; lo demás se descarta.
"""

import json
import re
import unicodedata


FUENTES = ["Procedimiento oficial", "Manual", "Documento interno", "Otro"]

MAX_CARACTERES = 7000


class ErrorTipificacion(Exception):
    pass


def _clave(texto):

    texto = unicodedata.normalize("NFKD", str(texto or ""))

    texto = "".join(c for c in texto if not unicodedata.combining(c))

    return re.sub(r"[^a-z0-9]", "", texto.lower())


def categoria_canonica(valor, categorias):
    """
    Lleva lo que escribió el modelo a una tipificación de la lista
    ("Parámetros / seguridad" -> "Parámetros/seguridad"). '' si no coincide.
    """

    buscado = _clave(valor)

    if not buscado or not categorias:
        return ""

    claves = [(c, _clave(c)) for c in categorias]

    for categoria, clave in claves:
        if clave == buscado:
            return categoria

    if len(buscado) < 4:
        return ""

    # El modelo la acortó ("Facturación" por "Facturación electrónica")
    acortadas = [c for c, k in claves if k.startswith(buscado)]

    if len(acortadas) == 1:
        return acortadas[0]

    # El modelo le agregó palabras ("Problema de Lince"): la más específica
    ampliadas = [(c, k) for c, k in claves if buscado.startswith(k) and len(k) >= 4]

    if ampliadas:
        return max(ampliadas, key=lambda x: len(x[1]))[0]

    return ""


def _elegir(valor, opciones, por_defecto):

    buscado = _clave(valor)

    for opcion in opciones:
        if _clave(opcion) == buscado:
            return opcion

    return por_defecto


PROMPT = """
Sos un asistente que clasifica documentación de soporte técnico de Zoo Logic
para una Knowledge Base. Te paso el nombre del archivo, la carpeta donde
estaba y el comienzo de su contenido. Respondé SOLO con un objeto JSON.

Reglas:
- "producto": __PRODUCTO__
- "categoria": __CATEGORIAS__
  Escribila EXACTAMENTE como está en la lista. Si ninguna encaja, "".
- "subcategoria": 1 a 4 palabras que nombren el tema puntual (por ejemplo
  "CAE", "Certificados", "Alta de artículos"). Sin inventar temas que el
  documento no trate.
- "fuente": una de __FUENTES__.
- "descripcion": 1 o 2 oraciones que digan qué problema o procedimiento
  cubre el documento. Solo con lo que dice el contenido.
- "confianza": "alta", "media" o "baja".

Formato exacto:
{"categoria": "", "subcategoria": "", "fuente": "",
  "producto": "", "descripcion": "", "confianza": ""}
""".strip()


def _parsear_json(texto):

    texto = (texto or "").strip()

    texto = re.sub(r"^```(?:json)?|```$", "", texto, flags=re.M).strip()

    try:
        datos = json.loads(texto)
    except ValueError:

        inicio, fin = texto.find("{"), texto.rfind("}")

        if inicio < 0 or fin <= inicio:
            raise ErrorTipificacion("La IA no devolvió un JSON válido.")

        try:
            datos = json.loads(texto[inicio:fin + 1])
        except ValueError:
            raise ErrorTipificacion("La IA no devolvió un JSON válido.")

    if not isinstance(datos, dict):
        raise ErrorTipificacion("La IA no devolvió un JSON válido.")

    return datos


def _normalizar(datos, productos, categorias_por_producto, producto_doc):

    confianza = str(datos.get("confianza") or "").strip().lower()

    producto = producto_doc or _elegir(datos.get("producto"), productos, "")

    categorias = categorias_por_producto.get(producto, []) if producto else []

    categoria = categoria_canonica(datos.get("categoria"), categorias)

    if producto and categorias and not categoria:
        confianza = "baja"

    return {
        "categoria": categoria,
        "subcategoria": str(datos.get("subcategoria") or "").strip()[:60],
        "fuente": _elegir(datos.get("fuente"), FUENTES, "Otro"),
        "producto": producto,
        "descripcion": str(datos.get("descripcion") or "").strip()[:400],
        "confianza": confianza if confianza in ("alta", "media", "baja") else "baja",
    }


# ---- Modo simulación / sin IA: reglas simples ------------------------

_SINONIMOS = {
    "facturacion": ("cae", "afip", "arca", "factura", "comprobante", "punto de venta", "certificado"),
    "ecommerce": ("tienda nube", "ecommerce", "e-commerce", "woocommerce", "mercadolibre", "shopify", "vtex"),
    "stock": ("stock", "inventario", "deposito", "depósito"),
    "mantenimiento": ("mantenimiento", "actualizacion", "actualización", "instalacion", "instalación", "reinstal"),
    "comunicaciones": ("mail", "correo", "whatsapp", "sms", "notificacion", "notificación"),
    "ventas": ("venta", "vendedor", "comprobante de venta"),
    "contabilidad": ("contabilidad", "fondos", "asiento", "caja"),
    "parametros": ("parametro", "parámetro", "seguridad", "permiso", "usuario"),
    "omnicanalidad": ("omnicanal",),
    "uso": ("como se usa", "cómo se usa", "consulta de uso"),
    "diseno": ("diseño", "diseno", "impresion", "impresión", "formato"),
}


def tipificar_por_reglas(nombre, ruta, texto, productos,
                         categorias_por_producto=None, producto_doc=""):
    """Clasificación simple por palabras (modo simulación). Sin IA."""

    categorias_por_producto = categorias_por_producto or {}

    muestra = f"{nombre} {ruta} {texto[:3000]}".lower()

    categorias = categorias_por_producto.get(producto_doc, []) if producto_doc else []

    mejor, mejor_puntaje = "", 0

    for categoria in categorias:

        clave = _clave(categoria)

        puntaje = 0

        for palabra in re.split(r"[^a-záéíóúñ0-9]+", categoria.lower()):
            if len(palabra) >= 4 and palabra in muestra:
                puntaje += 2

        for raiz, palabras in _SINONIMOS.items():

            if raiz in clave:
                puntaje += sum(1 for p in palabras if p in muestra)

        if puntaje > mejor_puntaje:
            mejor, mejor_puntaje = categoria, puntaje

    resumen = re.sub(r"\s+", " ", re.sub(r"--- PÁGINA \d+ ---", " ", texto)).strip()

    return {
        "categoria": mejor,
        "subcategoria": "",
        "fuente": "Documento interno",
        "producto": producto_doc,
        "descripcion": resumen[:200],
        "confianza": "baja",
    }


# ---- Tipificación con IA ---------------------------------------------

def tipificar(client, modelo, nombre, ruta_origen, texto, productos,
              simulado=False, categorias_por_producto=None, producto_doc=""):
    """
    Devuelve un dict con categoria, subcategoria, fuente, producto,
    descripcion, confianza y via ('IA' o 'reglas'). Lanza ErrorTipificacion
    si no se pudo.

    `categorias_por_producto` = {producto: [tipificaciones]}. Si el documento
    ya tiene producto (`producto_doc`), la categoría sale de las
    tipificaciones de ese producto; si no, la IA elige primero el producto.
    """

    categorias_por_producto = categorias_por_producto or {}

    texto = (texto or "").strip()

    if not texto:
        raise ErrorTipificacion("El documento no tiene texto para analizar.")

    if simulado:

        resultado = tipificar_por_reglas(
            nombre, ruta_origen, texto, productos,
            categorias_por_producto, producto_doc
        )

        resultado["via"] = "reglas"

        return resultado

    if client is None:
        raise ErrorTipificacion("La IA no está configurada en el servidor.")

    if producto_doc:

        lista = categorias_por_producto.get(producto_doc, [])

        regla_producto = f'ya es "{producto_doc}"; devolvelo igual.'

        regla_categoria = (
            "una de estas tipificaciones de " + producto_doc + ": "
            + " | ".join(f'"{c}"' for c in lista) + "."
            if lista else
            f'{producto_doc} todavía no tiene tipificaciones definidas: devolvé "".'
        )

    else:

        regla_producto = (
            "uno de " + ", ".join(f'"{p}"' for p in productos)
            + ', solo si el documento claramente trata de ese producto; si '
            'no está claro, "".'
        )

        regla_categoria = (
            "primero elegí el producto y después una tipificación de ESE "
            "producto. Tipificaciones por producto: "
            + " ; ".join(
                f'{p}: ' + " | ".join(f'"{c}"' for c in categorias_por_producto.get(p, []))
                if categorias_por_producto.get(p) else f"{p}: (ninguna todavía)"
                for p in productos
            ) + "."
        )

    sistema = (
        PROMPT
        .replace("__PRODUCTO__", regla_producto)
        .replace("__CATEGORIAS__", regla_categoria)
        .replace("__FUENTES__", ", ".join(f'"{f}"' for f in FUENTES))
    )

    usuario = (
        f"Archivo: {nombre}\n"
        f"Carpeta de origen: {ruta_origen or '(raíz)'}\n\n"
        f"Contenido:\n{texto[:MAX_CARACTERES]}"
    )

    try:

        respuesta = client.chat.completions.create(
            model=modelo,
            messages=[
                {"role": "system", "content": sistema},
                {"role": "user", "content": usuario},
            ],
            response_format={"type": "json_object"},
        )

        bruto = respuesta.choices[0].message.content

    except Exception as e:
        raise ErrorTipificacion(f"No se pudo consultar a la IA: {e}")

    resultado = _normalizar(
        _parsear_json(bruto), productos, categorias_por_producto, producto_doc
    )

    resultado["via"] = "IA"

    return resultado
