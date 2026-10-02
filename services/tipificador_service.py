"""
Tipificación automática de documentos con IA.

Lee el nombre, la carpeta de origen y el contenido del PDF y propone:
producto (si no se sabe), categoría, subcategoría, nivel de soporte, fuente
y descripción. Solo se aceptan valores de las listas de la plataforma; todo
lo demás se descarta o se reemplaza por un valor seguro.
"""

import json
import re
import unicodedata


CATEGORIAS = [
    "Facturación Electrónica",
    "Mantenimiento",
    "Base de Datos",
    "Configuración",
    "Ecommerce",
    "Otros",
]

NIVELES = ["L1", "L2", "L3", "Todos"]

FUENTES = ["Procedimiento oficial", "Manual", "Documento interno", "Otro"]

MAX_CARACTERES = 7000


class ErrorTipificacion(Exception):
    pass


def _clave(texto):

    texto = unicodedata.normalize("NFKD", str(texto or ""))

    texto = "".join(c for c in texto if not unicodedata.combining(c))

    return re.sub(r"[^a-z0-9]", "", texto.lower())


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
- "categoria": una de {categorias}. Si ninguna encaja, "Otros".
- "subcategoria": 1 a 4 palabras que nombren el tema puntual (por ejemplo
  "CAE", "Certificados", "Alta de artículos", "Tienda Nube"). Sin inventar
  temas que el documento no trate.
- "nivel": quién puede aplicar el procedimiento.
  "L1" = soporte de primer nivel, sin acceso a base de datos ni a código.
  "L2" = requiere acceso a la base de datos, al servidor o configuración
  avanzada. "L3" = requiere desarrollo. "Todos" = información general que
  sirve para cualquier nivel. Si dudás entre dos, elegí el más alto.
- "fuente": una de {fuentes}.
- "producto": uno de {productos}, solo si el documento claramente trata de
  ese producto; si no está claro, "".
- "descripcion": 1 o 2 oraciones que digan qué problema o procedimiento
  cubre el documento. Solo con lo que dice el contenido.
- "confianza": "alta", "media" o "baja".

Formato exacto:
{{"categoria": "", "subcategoria": "", "nivel": "", "fuente": "",
  "producto": "", "descripcion": "", "confianza": ""}}
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


def _normalizar(datos, productos):

    confianza = str(datos.get("confianza") or "").strip().lower()

    return {
        "categoria": _elegir(datos.get("categoria"), CATEGORIAS, "Otros"),
        "subcategoria": str(datos.get("subcategoria") or "").strip()[:60],
        # Ante la duda, un nivel alto: el agente L1 no usa documentos L2/L3
        "nivel": _elegir(datos.get("nivel"), NIVELES, "L2"),
        "fuente": _elegir(datos.get("fuente"), FUENTES, "Otro"),
        "producto": _elegir(datos.get("producto"), productos, ""),
        "descripcion": str(datos.get("descripcion") or "").strip()[:400],
        "confianza": confianza if confianza in ("alta", "media", "baja") else "baja",
    }


# ---- Modo simulación / sin IA: reglas simples ------------------------

_REGLAS = [
    ("Facturación Electrónica", ("cae", "factura", "afip", "arca", "comprobante", "punto de venta", "certificado")),
    ("Ecommerce", ("tienda nube", "ecommerce", "e-commerce", "woocommerce", "mercadolibre", "shopify", "vtex", "pedidos web")),
    ("Base de Datos", ("base de datos", "sql", "backup", "restaur", "consulta sql", "tabla")),
    ("Mantenimiento", ("mantenimiento", "actualizacion", "actualización", "instalacion", "instalación", "reinstal")),
    ("Configuración", ("configurac", "parametro", "parámetro", "alta de", "usuario")),
]


def tipificar_por_reglas(nombre, ruta, texto, productos):

    muestra = f"{nombre} {ruta} {texto[:3000]}".lower()

    categoria = "Otros"

    for nombre_categoria, palabras in _REGLAS:
        if any(p in muestra for p in palabras):
            categoria = nombre_categoria
            break

    resumen = re.sub(r"\s+", " ", re.sub(r"--- PÁGINA \d+ ---", " ", texto)).strip()

    return {
        "categoria": categoria,
        "subcategoria": "",
        "nivel": "L2" if categoria == "Base de Datos" else "L1",
        "fuente": "Documento interno",
        "producto": "",
        "descripcion": resumen[:200],
        "confianza": "baja",
    }


# ---- Tipificación con IA ---------------------------------------------

def tipificar(client, modelo, nombre, ruta_origen, texto, productos,
              simulado=False):
    """
    Devuelve un dict con categoria, subcategoria, nivel, fuente, producto,
    descripcion, confianza y via ('IA' o 'reglas'). Lanza ErrorTipificacion
    si no se pudo.
    """

    texto = (texto or "").strip()

    if not texto:
        raise ErrorTipificacion("El documento no tiene texto para analizar.")

    if simulado:

        resultado = tipificar_por_reglas(nombre, ruta_origen, texto, productos)

        resultado["via"] = "reglas"

        return resultado

    if client is None:
        raise ErrorTipificacion("La IA no está configurada en el servidor.")

    sistema = PROMPT.format(
        categorias=", ".join(f'"{c}"' for c in CATEGORIAS),
        fuentes=", ".join(f'"{f}"' for f in FUENTES),
        productos=", ".join(f'"{p}"' for p in productos),
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

    resultado = _normalizar(_parsear_json(bruto), productos)

    resultado["via"] = "IA"

    return resultado
