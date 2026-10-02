import os
import json
import uuid
import base64
import threading
from concurrent.futures import ThreadPoolExecutor

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify
)

from markupsafe import escape
from werkzeug.utils import secure_filename

from pypdf import PdfReader
import fitz

from openai import OpenAI, AzureOpenAI

from services import agent_service, conversation_service
from services.mock_client import MockClient


# ============================================================
# CONFIGURACIÓN
# ============================================================

# Cambiá este texto cada vez que quieras confirmar que Render
# está corriendo la versión nueva (se ve en /health).
APP_VERSION = "agente-v5"

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "zoo-logic-ai-agents"
)

# Máximo por envío (puede incluir varios PDFs): 100 MB
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Si en Render agregás un Persistent Disk, definí la variable
# de entorno KNOWLEDGE_DIR apuntando al disco (ej. /var/data/knowledge)
# para que la KB no se borre en cada deploy.
KNOWLEDGE_DIR = os.environ.get(
    "KNOWLEDGE_DIR",
    os.path.join(BASE_DIR, "knowledge")
)

DOCUMENTS_DIR = os.path.join(KNOWLEDGE_DIR, "documents")

IMAGES_DIR = os.path.join(KNOWLEDGE_DIR, "dragonfish", "images")

METADATA_FILE = os.path.join(KNOWLEDGE_DIR, "documents.json")

os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
os.makedirs(DOCUMENTS_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

conversation_service.init_db(
    os.path.join(KNOWLEDGE_DIR, "conversaciones.db")
)


# Parámetros del procesamiento de PDFs
MIN_TEXTO_PAGINA = 50      # menos caracteres que esto + imágenes = página escaneada
MIN_LADO_IMAGEN = 120      # se ignoran íconos/logos más chicos (px)
MIN_AREA_IMAGEN = 20000    # área mínima (px²)
MAX_IMAGENES_POR_PDF = 40  # tope de imágenes a analizar por documento
DPI_PAGINA_ESCANEADA = 150

# ---- Proveedor de IA -------------------------------------------------
# Si están definidas las variables de Azure OpenAI se usa Azure; si no,
# OpenAI directo. En Azure, "model" es el NOMBRE DEL DEPLOYMENT que creaste
# en Azure AI Foundry / Azure OpenAI Studio, no el nombre del modelo.

AZURE_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
AZURE_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
AZURE_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21").strip()
AZURE_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()

USA_AZURE = bool(AZURE_ENDPOINT and AZURE_API_KEY and AZURE_DEPLOYMENT)

# Modo simulación: no usa ninguna API key ni consume crédito. Las respuestas
# salen de reglas simples (no es IA). Sirve para probar toda la plataforma.
MODO_SIMULADO = os.environ.get("MODO_SIMULADO", "").strip().lower() in (
    "1", "true", "si", "sí", "yes", "on"
)

if USA_AZURE:

    # Por defecto el mismo deployment sirve para imágenes y para el agente
    # (tiene que ser un modelo con visión, por ejemplo gpt-4o o gpt-4.1).
    MODELO_VISION = os.environ.get("AZURE_OPENAI_VISION_DEPLOYMENT", AZURE_DEPLOYMENT)

    MODELO_AGENTE = os.environ.get("AZURE_OPENAI_AGENT_DEPLOYMENT", AZURE_DEPLOYMENT)

else:

    MODELO_VISION = os.environ.get("OPENAI_VISION_MODEL", "gpt-4.1-mini")

    # Modelo que usa el agente en el Playground
    MODELO_AGENTE = os.environ.get("OPENAI_AGENT_MODEL", "gpt-4.1-mini")

LOCK = threading.RLock()

# Máximo de PDFs por envío y de PDFs procesándose a la vez.
# Se procesan de a 2 para no pasarse del límite de la API ni de memoria.
MAX_PDFS_POR_ENVIO = 15

COLA_PROCESAMIENTO = ThreadPoolExecutor(max_workers=2)


# ============================================================
# OPENAI
# ============================================================

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if MODO_SIMULADO:

    client = MockClient()

elif USA_AZURE:

    client = AzureOpenAI(
        api_key=AZURE_API_KEY,
        azure_endpoint=AZURE_ENDPOINT,
        api_version=AZURE_API_VERSION
    )

elif OPENAI_API_KEY:

    client = OpenAI(api_key=OPENAI_API_KEY)

else:

    client = None


# ============================================================
# METADATA
# ============================================================

def cargar_documentos():

    with LOCK:

        if not os.path.exists(METADATA_FILE):
            return []

        try:

            with open(METADATA_FILE, "r", encoding="utf-8") as archivo:
                datos = json.load(archivo)

            if not isinstance(datos, list):
                return []

        except Exception as e:
            print(f"Error leyendo metadata: {e}")
            return []

        # Documentos viejos sin id: se les asigna uno y se persiste
        cambiado = False

        for doc in datos:

            if not doc.get("id"):
                doc["id"] = uuid.uuid4().hex
                cambiado = True

            if not doc.get("procesamiento"):
                doc["procesamiento"] = "PROCESADO"
                cambiado = True

        if cambiado:
            guardar_documentos(datos)

        return datos


def guardar_documentos(documentos):

    with LOCK:

        try:

            ruta_tmp = METADATA_FILE + ".tmp"

            with open(ruta_tmp, "w", encoding="utf-8") as archivo:
                json.dump(
                    documentos,
                    archivo,
                    ensure_ascii=False,
                    indent=4
                )

            os.replace(ruta_tmp, METADATA_FILE)

            return True

        except Exception as e:
            print(f"Error guardando metadata: {e}")
            return False


def marcar_interrumpidos():
    """
    Si Render reinicia mientras hay PDFs en cola, esos documentos quedarían
    en 'PROCESANDO' para siempre. Al arrancar se marcan con error.
    """

    with LOCK:

        documentos = cargar_documentos()

        cambiado = False

        for doc in documentos:

            if doc.get("procesamiento") == "PROCESANDO":

                doc["procesamiento"] = "ERROR"
                doc["error"] = (
                    "El procesamiento se interrumpió por un reinicio "
                    "del servidor. Volvé a subir el PDF."
                )
                cambiado = True

        if cambiado:
            guardar_documentos(documentos)


def actualizar_documento(doc_id, cambios):

    with LOCK:

        documentos = cargar_documentos()

        for doc in documentos:

            if doc.get("id") == doc_id:
                doc.update(cambios)
                break

        guardar_documentos(documentos)


def armar_contenido_completo(doc):
    """
    Texto del PDF + descripción de cada imagen / página escaneada.
    Es lo que se muestra en '👁 Ver contenido' y lo que después
    va a consultar el agente.
    """

    partes = []

    texto = (doc.get("texto_extraido") or "").strip()

    if texto:
        partes.append(texto)

    imagenes = doc.get("imagenes") or []

    if imagenes:

        partes.append(
            "\n\n===== CONTENIDO VISUAL "
            "(capturas, imágenes y páginas escaneadas) ====="
        )

        for img in imagenes:

            etiqueta = (
                "Página escaneada"
                if img.get("tipo") == "pagina_escaneada"
                else "Imagen"
            )

            descripcion = img.get("descripcion", "")

            if str(descripcion).startswith("No se pudo"):
                descripcion = "(imagen no analizada)"

            partes.append(
                f"\n[{etiqueta} - página {img.get('pagina')} - "
                f"{img.get('archivo') or 's/archivo'}]\n"
                f"{descripcion}"
            )

    return "\n".join(partes)


def documentos_para_vista():

    documentos = cargar_documentos()

    vista = []

    for doc in documentos:

        copia = dict(doc)

        # Evita errores en la plantilla si a un documento le falta algún campo
        for campo in (
            "nombre", "archivo", "producto", "categoria", "subcategoria",
            "nivel", "estado", "fuente", "descripcion", "texto_extraido"
        ):
            if copia.get(campo) is None:
                copia[campo] = ""

        copia["contenido_completo"] = armar_contenido_completo(doc)
        vista.append(copia)

    return vista


def render_seccion(section, **extra):

    documentos = documentos_para_vista()

    hay_procesando = any(
        d.get("procesamiento") == "PROCESANDO"
        for d in documentos
    )

    return render_template(
        "index.html",
        section=section,
        documentos=documentos,
        hay_procesando=hay_procesando,
        simulado=MODO_SIMULADO,
        **extra
    )


marcar_interrumpidos()


# ============================================================
# ANALIZAR IMAGEN CON IA
# ============================================================

class ErrorFatalOpenAI(Exception):
    """
    Error de OpenAI que va a repetirse en todas las imágenes
    (sin crédito, API key inválida o sin configurar). Si ocurre, el
    documento se marca con ERROR y se corta el procesamiento, en lugar
    de guardar descripciones vacías como si fueran conocimiento.
    """


def es_error_fatal_openai(error):

    texto = str(error).lower()

    return (
        "insufficient_quota" in texto
        or "credit_balance" in texto
        or "no credits remaining" in texto
        or "invalid_api_key" in texto
        or "incorrect api key" in texto
        or "deploymentnotfound" in texto
        or "access denied" in texto
        or "invalid subscription key" in texto
        or getattr(error, "status_code", None) in (401, 404)
    )


def mensaje_error_fatal(error):

    texto = str(error).lower()

    if "deploymentnotfound" in texto or getattr(error, "status_code", None) == 404:

        return (
            "No se encontró el deployment de Azure OpenAI. Revisá que "
            "AZURE_OPENAI_DEPLOYMENT (y AZURE_OPENAI_ENDPOINT) coincidan "
            "con lo que creaste en Azure, y tocá Reprocesar."
        )

    if USA_AZURE and (
        "access denied" in texto
        or "invalid subscription key" in texto
        or getattr(error, "status_code", None) == 401
    ):

        return (
            "Azure OpenAI rechazó la clave. Revisá AZURE_OPENAI_API_KEY y "
            "AZURE_OPENAI_ENDPOINT en Render y tocá Reprocesar."
        )

    if "insufficient_quota" in texto or "credit" in texto:

        return (
            "Tu cuenta de OpenAI no tiene crédito disponible. "
            "Cargá crédito en platform.openai.com/settings/organization/"
            "billing y tocá Reprocesar."
        )

    return (
        "OpenAI rechazó la API key (inválida o sin permisos). "
        "Revisá OPENAI_API_KEY en Render y tocá Reprocesar."
    )


def analizar_imagen_con_ia(ruta_imagen, numero_pagina, tipo="imagen"):

    if not client:

        raise ErrorFatalOpenAI(
            "Falta configurar la IA en Render (OPENAI_API_KEY, o las "
            "variables AZURE_OPENAI_*) para analizar las imágenes. "
            "Configurala y tocá Reprocesar."
        )

    try:

        with open(ruta_imagen, "rb") as archivo:
            imagen_bytes = archivo.read()

        imagen_base64 = base64.b64encode(imagen_bytes).decode("utf-8")

        extension = os.path.splitext(ruta_imagen)[1].lower()

        mime_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp"
        }

        mime_type = mime_types.get(extension, "image/png")

        extra = ""

        if tipo == "pagina_escaneada":
            extra = """
Esta imagen es una PÁGINA COMPLETA ESCANEADA del documento
(no tiene texto digital). Transcribí TODO el texto legible de
la página, respetando títulos, pasos numerados y listas, y
después describí las capturas o diagramas que contenga.
"""

        prompt = f"""
Analizá esta imagen extraída de un documento
técnico utilizado para soporte de software.

La imagen pertenece a la página
{numero_pagina} del documento.
{extra}
El objetivo es generar conocimiento que pueda
ser utilizado posteriormente por un agente de
soporte técnico Nivel 1.

Analizá únicamente información que pueda
observarse realmente en la imagen.

Prestá especial atención a:

- pantallas del sistema
- nombres de botones
- menús
- campos
- mensajes de error
- códigos de error
- configuraciones
- rutas
- valores visibles
- procedimientos
- pasos
- advertencias
- títulos
- opciones seleccionadas
- relaciones entre elementos

Si es una captura de pantalla:

1. Identificá qué aplicación o pantalla aparece,
   si puede determinarse visualmente.

2. Describí los elementos relevantes.

3. Identificá botones, campos y opciones visibles.

4. Indicá cualquier mensaje de error.

5. Describí cualquier procedimiento que pueda
   inferirse directamente de los elementos visibles.

Si contiene texto:

Transcribí los fragmentos técnicos relevantes.

Si contiene un diagrama:

Explicá las relaciones visibles entre los elementos.

IMPORTANTE:

No inventes información.

No supongas acciones que no sean visibles.

No completes información faltante.

La descripción debe ser técnica, clara y útil
para un agente de soporte.

Respondé en español.
"""

        response = client.chat.completions.create(
            model=MODELO_VISION,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url":
                                    f"data:{mime_type};base64,{imagen_base64}"
                            }
                        }
                    ]
                }
            ]
        )

        resultado = response.choices[0].message.content

        if resultado:
            return resultado.strip()

        return (
            "La imagen fue procesada pero "
            "no se obtuvo una descripción."
        )

    except Exception as e:

        print(f"Error analizando imagen: {e}")

        if es_error_fatal_openai(e):
            raise ErrorFatalOpenAI(mensaje_error_fatal(e))

        return f"No se pudo analizar la imagen: {str(e)}"


# ============================================================
# PROCESAMIENTO DEL PDF (se ejecuta en segundo plano)
# ============================================================

def extraer_texto_pagina_pypdf(ruta_pdf, indice):
    """Respaldo por si PyMuPDF no logra leer el texto de una página."""

    try:
        lector = PdfReader(ruta_pdf)
        return (lector.pages[indice].extract_text() or "").strip()
    except Exception:
        return ""


def procesar_documento(doc_id, ruta_pdf):

    print(f"[{doc_id}] Procesando PDF: {ruta_pdf}")

    carpeta_imagenes = os.path.join(IMAGES_DIR, doc_id)
    os.makedirs(carpeta_imagenes, exist_ok=True)

    try:

        documento = fitz.open(ruta_pdf)

        bloques_texto = []
        imagenes = []

        xrefs_vistos = set()
        omitidas = 0
        paginas_escaneadas = 0
        total_paginas = len(documento)

        for numero_pagina, pagina in enumerate(documento, start=1):

            texto = (pagina.get_text() or "").strip()

            if not texto:
                texto = extraer_texto_pagina_pypdf(
                    ruta_pdf,
                    numero_pagina - 1
                )

            try:
                imagenes_pagina = pagina.get_images(full=True)
            except Exception:
                imagenes_pagina = []

            # ---- PÁGINA ESCANEADA: casi sin texto pero con imagen ----
            if len(texto) < MIN_TEXTO_PAGINA and imagenes_pagina:

                if len(imagenes) >= MAX_IMAGENES_POR_PDF:
                    omitidas += 1
                    continue

                paginas_escaneadas += 1

                nombre_imagen = f"pagina_{numero_pagina:03d}_completa.png"
                ruta_imagen = os.path.join(carpeta_imagenes, nombre_imagen)

                try:

                    pix = pagina.get_pixmap(dpi=DPI_PAGINA_ESCANEADA)
                    pix.save(ruta_imagen)

                    descripcion = analizar_imagen_con_ia(
                        ruta_imagen,
                        numero_pagina,
                        tipo="pagina_escaneada"
                    )

                except ErrorFatalOpenAI:

                    raise

                except Exception as e:

                    print(f"Error página escaneada {numero_pagina}: {e}")
                    descripcion = f"No se pudo procesar la página: {e}"

                imagenes.append({
                    "pagina": numero_pagina,
                    "tipo": "pagina_escaneada",
                    "archivo": nombre_imagen,
                    "ruta": ruta_imagen.replace("\\", "/"),
                    "descripcion": descripcion
                })

                bloques_texto.append(
                    f"\n--- PÁGINA {numero_pagina} ---\n"
                    "(página escaneada: ver contenido visual)"
                )

                continue

            # ---- PÁGINA CON TEXTO DIGITAL ----
            if texto:
                bloques_texto.append(
                    f"\n--- PÁGINA {numero_pagina} ---\n{texto}"
                )

            # ---- IMÁGENES EMBEBIDAS ----
            numero_imagen_pagina = 0

            for imagen in imagenes_pagina:

                xref = imagen[0]

                if xref in xrefs_vistos:
                    continue

                xrefs_vistos.add(xref)

                try:

                    pix = fitz.Pixmap(documento, xref)

                    if pix.width < MIN_LADO_IMAGEN or pix.height < MIN_LADO_IMAGEN:
                        continue

                    if pix.width * pix.height < MIN_AREA_IMAGEN:
                        continue

                    if len(imagenes) >= MAX_IMAGENES_POR_PDF:
                        omitidas += 1
                        continue

                    if pix.n - pix.alpha >= 4:
                        pix = fitz.Pixmap(fitz.csRGB, pix)

                    numero_imagen_pagina += 1

                    nombre_imagen = (
                        f"pagina_{numero_pagina:03d}"
                        f"_imagen_{numero_imagen_pagina:03d}.png"
                    )

                    ruta_imagen = os.path.join(carpeta_imagenes, nombre_imagen)

                    pix.save(ruta_imagen)

                    descripcion = analizar_imagen_con_ia(
                        ruta_imagen,
                        numero_pagina
                    )

                    imagenes.append({
                        "pagina": numero_pagina,
                        "tipo": "imagen",
                        "archivo": nombre_imagen,
                        "ruta": ruta_imagen.replace("\\", "/"),
                        "descripcion": descripcion
                    })

                except ErrorFatalOpenAI:

                    raise

                except Exception as e:

                    print(
                        f"Error imagen xref {xref} "
                        f"(página {numero_pagina}): {e}"
                    )

        documento.close()

        con_error = sum(
            1 for i in imagenes
            if str(i.get("descripcion", "")).startswith("No se pudo")
        )

        primer_error = next(
            (
                str(i.get("descripcion", ""))
                for i in imagenes
                if str(i.get("descripcion", "")).startswith("No se pudo")
            ),
            ""
        )

        actualizar_documento(doc_id, {
            "simulado": bool(MODO_SIMULADO),
            "advertencia": (
                f"{con_error} imagen(es) no se pudieron analizar. "
                f"{primer_error[:200]} Tocá Reprocesar para reintentar."
                if con_error else ""
            ),
            "texto_extraido": "\n".join(bloques_texto).strip(),
            "imagenes": imagenes,
            "cantidad_imagenes": len(imagenes),
            "imagenes_omitidas": omitidas,
            "imagenes_con_error": con_error,
            "paginas_escaneadas": paginas_escaneadas,
            "total_paginas": total_paginas,
            "procesamiento": "PROCESADO",
            "error": ""
        })

        print(
            f"[{doc_id}] Listo: {total_paginas} páginas, "
            f"{len(imagenes)} imágenes analizadas, "
            f"{paginas_escaneadas} escaneadas, {omitidas} omitidas."
        )

    except Exception as e:

        print(f"[{doc_id}] ERROR procesando PDF: {e}")

        actualizar_documento(doc_id, {
            "procesamiento": "ERROR",
            "error": str(e),
            "advertencia": ""
        })


# ============================================================
# PÁGINAS
# ============================================================

@app.route("/")
def home():
    return render_seccion(None)


@app.route("/agentes")
def agentes():
    return render_seccion("agentes")


@app.route("/agentes/configurar")
def agentes_configurar():
    return render_seccion("agente_config")


@app.route("/knowledge")
def knowledge():
    return render_seccion("knowledge")


@app.route("/conexiones")
def conexiones():
    return render_seccion("conexiones")


@app.route("/metricas")
def metricas():

    canal = request.args.get("canal", "todos")

    if canal not in ("todos", "playground", "whatsapp", "simulado"):
        canal = "todos"

    datos = conversation_service.calcular_metricas(
        None if canal == "todos" else canal
    )

    return render_seccion("metricas", m=datos, canal=canal)


@app.route("/playground")
def playground():
    return render_seccion("playground")


# ============================================================
# SUBIR DOCUMENTO
# ============================================================

@app.route("/knowledge/upload", methods=["POST"])
def knowledge_upload():

    archivos = [
        a for a in request.files.getlist("archivo")
        if a and a.filename
    ]

    if not archivos:

        flash("No se seleccionó ningún archivo.")

        return redirect(url_for("knowledge"))

    if len(archivos) > MAX_PDFS_POR_ENVIO:

        flash(
            f"Podés subir hasta {MAX_PDFS_POR_ENVIO} PDFs por vez. "
            f"Seleccionaste {len(archivos)}."
        )

        return redirect(url_for("knowledge"))

    varios = len(archivos) > 1

    nombre_manual = request.form.get("nombre_documento", "").strip()

    recibidos = 0
    rechazados = []

    for archivo in archivos:

        nombre_archivo = secure_filename(archivo.filename)

        extension = os.path.splitext(nombre_archivo)[1].lower()

        if extension != ".pdf":

            rechazados.append(f"{archivo.filename} (no es PDF)")

            continue

        doc_id = uuid.uuid4().hex

        # Se guarda con el id adelante para que dos PDFs con el
        # mismo nombre no se pisen entre sí.
        ruta_pdf = os.path.join(
            DOCUMENTS_DIR,
            f"{doc_id[:8]}_{nombre_archivo}"
        )

        archivo.save(ruta_pdf)

        with open(ruta_pdf, "rb") as f:
            cabecera = f.read(5)

        if cabecera != b"%PDF-":

            os.remove(ruta_pdf)

            rechazados.append(f"{archivo.filename} (PDF inválido)")

            continue

        print(f"PDF guardado: {ruta_pdf}")

        # Con varios archivos, cada documento usa el nombre de su archivo;
        # el campo "Nombre del documento" solo aplica a una subida individual.
        if varios or not nombre_manual:
            nombre = os.path.splitext(archivo.filename)[0].strip()
        else:
            nombre = nombre_manual

        documento = {
            "id": doc_id,
            "archivo": nombre_archivo,
            "ruta_pdf": ruta_pdf.replace("\\", "/"),
            "nombre": nombre,
            "producto": request.form.get("producto", "").strip(),
            "categoria": request.form.get("categoria", "").strip(),
            "subcategoria": request.form.get("subcategoria", "").strip(),
            "nivel": request.form.get("nivel", "").strip(),
            "estado": request.form.get("estado", "").strip(),
            "fuente": request.form.get("fuente", "").strip(),
            "descripcion": request.form.get("descripcion", "").strip(),
            "texto_extraido": "",
            "imagenes": [],
            "cantidad_imagenes": 0,
            "procesamiento": "PROCESANDO",
            "error": ""
        }

        with LOCK:

            documentos = cargar_documentos()
            documentos.append(documento)
            guardar_documentos(documentos)

        # Se procesan en segundo plano, de a 2 por vez, para que la
        # subida responda enseguida y no se corte por timeout.
        COLA_PROCESAMIENTO.submit(procesar_documento, doc_id, ruta_pdf)

        recibidos += 1

    if recibidos:

        flash(
            f"{recibidos} PDF recibido(s). Se están procesando el texto y "
            "las imágenes; la página se actualiza sola hasta que terminen."
        )

    if rechazados:

        flash("No se pudieron cargar: " + ", ".join(rechazados) + ".")

    return redirect(url_for("knowledge"))


# ============================================================
# REPROCESAR DOCUMENTO
# ============================================================

@app.route("/knowledge/reprocesar/<doc_id>", methods=["POST"])
def knowledge_reprocesar(doc_id):

    with LOCK:

        documentos = cargar_documentos()

        documento = next(
            (d for d in documentos if d.get("id") == doc_id),
            None
        )

        if not documento:

            flash("No se encontró el documento.")

            return redirect(url_for("knowledge"))

        ruta_pdf = documento.get("ruta_pdf") or ""

        if not os.path.exists(ruta_pdf):

            flash(
                "El PDF original ya no está en el servidor. "
                "Volvé a subirlo."
            )

            return redirect(url_for("knowledge"))

        if documento.get("procesamiento") == "PROCESANDO":

            flash("Ese documento ya se está procesando.")

            return redirect(url_for("knowledge"))

        documento["procesamiento"] = "PROCESANDO"
        documento["error"] = ""
        documento["advertencia"] = ""

        guardar_documentos(documentos)

    COLA_PROCESAMIENTO.submit(procesar_documento, doc_id, ruta_pdf)

    flash("Reprocesando el documento. La página se actualiza sola.")

    return redirect(url_for("knowledge"))


# ============================================================
# PLAYGROUND - CHAT CON EL AGENTE
# ============================================================

@app.route("/playground/chat", methods=["POST"])
def playground_chat():

    datos = request.get_json(silent=True) or {}

    mensaje = str(datos.get("mensaje") or "").strip()

    if not mensaje:
        return jsonify({"error": "Escribí un mensaje."}), 400

    if len(mensaje) > 2000:
        return jsonify({"error": "El mensaje es demasiado largo."}), 400

    if not client:

        return jsonify({
            "error": (
                "Falta configurar la IA en Render: OPENAI_API_KEY, o "
                "AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT + "
                "AZURE_OPENAI_DEPLOYMENT."
            )
        }), 503

    try:

        resultado = agent_service.responder(
            client=client,
            modelo=MODELO_AGENTE,
            documentos=documentos_para_vista(),
            historial=datos.get("historial"),
            mensaje=mensaje,
            estado_previo=datos.get("estado"),
            incluir_pendientes=bool(datos.get("incluir_pendientes", True))
        )

    except Exception as e:

        print(f"Error en el agente: {e}")

        return jsonify({
            "error": f"No se pudo obtener respuesta del agente: {e}"
        }), 502

    # Se guarda la conversación para las métricas. Si falla el guardado
    # no se corta la charla: el cliente igual recibe la respuesta.
    try:

        resultado["conversacion_id"] = conversation_service.guardar_turno(
            conversacion_id=str(datos.get("conversacion_id") or ""),
            canal="simulado" if MODO_SIMULADO else "playground",
            historial_previo=agent_service._limpiar_historial(
                datos.get("historial")
            ),
            mensaje_cliente=mensaje,
            resultado=resultado
        )

    except Exception as e:

        print(f"Error guardando la conversación: {e}")

        resultado["conversacion_id"] = str(datos.get("conversacion_id") or "")

        resultado["avisos"].append(
            "No se pudo guardar esta conversación para las métricas."
        )

    return jsonify(resultado)


# ============================================================
# VER CONTENIDO (JSON, útil para revisar lo que quedó en la KB)
# ============================================================

@app.route("/knowledge/documento/<doc_id>")
def ver_documento(doc_id):

    for doc in documentos_para_vista():

        if doc.get("id") == doc_id:
            return jsonify(doc)

    return jsonify({"error": "Documento no encontrado"}), 404


# ============================================================
# DIAGNÓSTICO
# ============================================================

@app.route("/health")
def health():

    return {
        "status": "ok",
        "service": "Zoo Logic AI Agents",
        "version": APP_VERSION,
        "openai_configurado": bool(client),
        "proveedor": (
            "simulado" if MODO_SIMULADO
            else "azure" if USA_AZURE
            else ("openai" if client else "sin configurar")
        ),
        "modelo_agente": MODELO_AGENTE,
        "conversaciones_db": conversation_service.DB_PATH,
        "knowledge_dir": KNOWLEDGE_DIR
    }


@app.route("/debug/routes")
def debug_routes():

    lineas = []

    for regla in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):

        metodos = sorted(regla.methods - {"HEAD", "OPTIONS"})

        lineas.append(f"{metodos}  {regla.rule}")

    return "<pre>" + "\n".join(lineas) + "</pre>"


@app.errorhandler(405)
def metodo_no_permitido(error):

    permitidos = ", ".join(sorted(error.valid_methods or []))

    return (
        f"<pre>405 - {escape(request.method)} no está permitido en "
        f"{escape(request.path)}.\n"
        f"Métodos permitidos acá: {escape(permitidos)}\n"
        f"Versión de la app: {APP_VERSION}\n"
        f"Revisá /debug/routes.</pre>",
        405
    )


@app.errorhandler(413)
def archivo_muy_grande(error):

    flash("El PDF supera el máximo permitido (50 MB).")

    return redirect(url_for("knowledge"))


# ============================================================
# EJECUCIÓN LOCAL
# ============================================================

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
