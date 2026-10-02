"""
Búsqueda de fragmentos relevantes dentro de la Knowledge Base.

Por ahora es una búsqueda por palabras clave (sin embeddings): alcanza
para el piloto con pocos documentos. Más adelante se puede reemplazar
`buscar_fragmentos` por una búsqueda vectorial sin tocar el agente.
"""

import math
import re
import unicodedata


STOPWORDS = {
    "para", "pero", "como", "cuando", "donde", "porque", "que", "con",
    "una", "uno", "unos", "unas", "los", "las", "del", "por", "sin",
    "sus", "esta", "este", "esto", "estos", "estas", "ese", "esa",
    "hay", "ser", "son", "fue", "muy", "mas", "ya", "yo", "mi", "me",
    "te", "se", "lo", "le", "les", "nos", "al", "en", "el", "la", "de",
    "un", "es", "no", "si", "y", "o", "a", "tengo", "tiene", "puedo",
    "hacer", "quiero", "necesito", "estoy", "esta", "aparece", "sale",
    "cuando", "intento", "hola", "gracias", "buenas", "tardes", "dias",
    # palabras que aparecen en casi cualquier consulta o documento
    "error", "errores", "problema", "problemas", "sistema", "dragonfish", "znube", "lince", "pantera",
    "dice", "ningun", "ninguna", "nada", "mensaje", "ayer", "hoy",
}

# Nivel de soporte que el agente L1 puede usar como procedimiento
NIVELES_PERMITIDOS = {"l1", "todos", ""}

MARCADOR = re.compile(
    r"(?=\n?--- PÁGINA \d+ ---)"
    r"|(?=\n\[(?:Imagen|Página escaneada) - página \d+)"
)

PAGINA = re.compile(r"(?:PÁGINA|página) (\d+)")


def normalizar(texto):

    texto = unicodedata.normalize("NFD", (texto or "").lower())

    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def tokenizar(texto):

    return [
        t for t in re.findall(r"[a-z0-9]{3,}", normalizar(texto))
        if t not in STOPWORDS
    ]


def dividir_en_fragmentos(contenido, max_chars=1400):
    """Parte el contenido por página / imagen y luego por párrafos."""

    fragmentos = []

    for bloque in MARCADOR.split(contenido or ""):

        bloque = bloque.strip()

        if not bloque:
            continue

        coincidencia = PAGINA.search(bloque[:80])

        pagina = int(coincidencia.group(1)) if coincidencia else None

        if len(bloque) <= max_chars:
            fragmentos.append((pagina, bloque))
            continue

        actual = ""

        for parrafo in re.split(r"\n\s*\n|\n(?=\s*\d+[.)] )", bloque):

            if actual and len(actual) + len(parrafo) > max_chars:
                fragmentos.append((pagina, actual.strip()))
                actual = ""

            while len(parrafo) > max_chars:
                fragmentos.append((pagina, parrafo[:max_chars]))
                parrafo = parrafo[max_chars:]

            actual += "\n\n" + parrafo

        if actual.strip():
            fragmentos.append((pagina, actual.strip()))

    return fragmentos


def _clave_producto(valor):
    """'zNube', 'Z Nube' y 'z-nube' dan la misma clave."""

    return re.sub(r"[^a-z0-9]", "", normalizar(valor or ""))


def documento_elegible(doc, incluir_pendientes, producto):

    if doc.get("procesamiento", "PROCESADO") != "PROCESADO":
        return False

    estado = (doc.get("estado") or "").strip()

    if estado == "Archivado":
        return False

    if estado != "Vigente":

        # Pendiente de revisión: solo si se pidió explícitamente (pruebas)
        if not (incluir_pendientes and estado == "Pendiente de revisión"):
            return False

    nivel = normalizar(doc.get("nivel") or "")

    if nivel not in NIVELES_PERMITIDOS:
        return False

    if producto:

        doc_producto = _clave_producto(doc.get("producto"))

        if doc_producto and doc_producto != _clave_producto(producto):
            return False

    return True


def buscar_fragmentos(
    documentos,
    consulta,
    producto="Dragonfish",
    incluir_pendientes=True,
    max_fragmentos=6,
    max_chars_total=7000,
    categoria=None,
    categoria_estricta=False
):

    candidatos = []

    for doc in documentos:

        if not documento_elegible(doc, incluir_pendientes, producto):
            continue

        # Búsqueda estricta (agente especialista): solo documentos de su
        # categoría o sin categorizar. Los de otra categoría quedan afuera.
        if categoria and categoria_estricta:

            cat_doc = normalizar(doc.get("categoria") or "")

            if cat_doc and cat_doc != normalizar(categoria):
                continue

        meta = " ".join([
            doc.get("nombre") or "",
            doc.get("archivo") or "",
            doc.get("categoria") or "",
            doc.get("subcategoria") or "",
            doc.get("descripcion") or "",
        ])

        meta_tokens = set(tokenizar(meta))

        for pagina, texto in dividir_en_fragmentos(
            doc.get("contenido_completo")
            or doc.get("texto_extraido")
            or ""
        ):

            candidatos.append({
                "doc": doc,
                "pagina": pagina,
                "texto": texto,
                "tokens": tokenizar(texto),
                "meta_tokens": meta_tokens,
            })

    consulta_tokens = set(tokenizar(consulta))

    if not candidatos or not consulta_tokens:
        return []

    total = len(candidatos)

    frecuencia_doc = {}

    for c in candidatos:
        for t in set(c["tokens"]):
            frecuencia_doc[t] = frecuencia_doc.get(t, 0) + 1

    resultados = []

    for c in candidatos:

        puntaje = 0.0

        coincidencias = set()

        conteo = {}

        for t in c["tokens"]:
            conteo[t] = conteo.get(t, 0) + 1

        for t in consulta_tokens:

            if t in conteo:

                idf = math.log(1 + total / frecuencia_doc.get(t, 1))

                puntaje += (1 + math.log(conteo[t])) * idf

                coincidencias.add(t)

            if t in c["meta_tokens"]:

                puntaje += 2.5

                coincidencias.add(t)

        # Con una consulta de varias palabras, una sola coincidencia suelta
        # no alcanza para considerar relevante un fragmento.
        if len(consulta_tokens) >= 3 and len(coincidencias) < 2:
            continue

        # Si el caso ya está clasificado, los documentos de otra categoría
        # pesan menos (no se descartan por si la clasificación fue errónea).
        if categoria and c["doc"].get("categoria"):

            if normalizar(c["doc"]["categoria"]) != normalizar(categoria):
                puntaje *= 0.5

        if puntaje > 0:
            resultados.append((puntaje, c))

    resultados.sort(key=lambda x: x[0], reverse=True)

    elegidos = []
    usados = 0

    for puntaje, c in resultados:

        if len(elegidos) >= max_fragmentos:
            break

        if usados + len(c["texto"]) > max_chars_total and elegidos:
            continue

        doc = c["doc"]

        elegidos.append({
            "id": f"F{len(elegidos) + 1}",
            "doc_id": doc.get("id"),
            "documento": doc.get("nombre") or doc.get("archivo"),
            "categoria": doc.get("categoria") or "",
            "subcategoria": doc.get("subcategoria") or "",
            "nivel": doc.get("nivel") or "",
            "estado": doc.get("estado") or "",
            "pagina": c["pagina"],
            "texto": c["texto"],
            "puntaje": round(puntaje, 2),
        })

        usados += len(c["texto"])

    return elegidos


def categorias_disponibles(documentos, incluir_pendientes=True, producto="Dragonfish"):
    """Pares categoría / subcategoría que realmente existen en la KB."""

    pares = set()

    for doc in documentos:

        if not documento_elegible(doc, incluir_pendientes, producto):
            continue

        pares.add((
            doc.get("categoria") or "",
            doc.get("subcategoria") or ""
        ))

    return sorted(pares)
