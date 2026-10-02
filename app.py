import os
import io
import hmac
import json
import shutil
import sqlite3
import tempfile
import zipfile
import uuid
import base64
import threading
import time
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import (
    send_file,
    session,
    g,
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

from services import (
    agent_service, conversation_service, ucontact_service, usuarios_service,
    conexiones_service, crypto_service, proveedores_nube,
    sincronizacion_service, tipificador_service,
    agentes_service, enrutador_service
)
from services.tipificador_service import ErrorTipificacion
from services.mock_client import MockClient


# ============================================================
# CONFIGURACIÓN
# ============================================================

# Cambiá este texto cada vez que quieras confirmar que Render
# está corriendo la versión nueva (se ve en /health).
APP_VERSION = "agente-v6-ucontact"

app = Flask(__name__)

# La clave firma las sesiones de login: tiene que ser secreta. Si no está
# definida en Render, se genera una al azar (los logins se pierden en cada
# reinicio, pero nadie puede falsificar una sesión).
_SECRET = os.environ.get("FLASK_SECRET_KEY", "").strip()

if not _SECRET or _SECRET == "zoo-logic-ai-agents":
    print("AVISO: definí FLASK_SECRET_KEY en Render para que el login persista.")
    _SECRET = secrets.token_hex(32)

app.secret_key = _SECRET

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # En Render (https) la cookie viaja solo por conexión segura
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    PERMANENT_SESSION_LIFETIME=12 * 60 * 60,
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

ucontact_service.init_tablas()

usuarios_service.init_tablas()

conexiones_service.init_tablas()

agentes_service.init_tablas(agent_service.PRODUCTOS)

# Administrador inicial (se define en Render con ADMIN_USUARIO y ADMIN_CLAVE)
_resultado_admin = usuarios_service.asegurar_admin(
    os.environ.get("ADMIN_USUARIO", ""),
    os.environ.get("ADMIN_CLAVE", ""),
    resetear=os.environ.get("ADMIN_RESETEAR_CLAVE", "").strip().lower()
    in ("1", "true", "si", "sí", "yes", "on")
)

print(f"Administrador inicial: {_resultado_admin}")


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


# ---- uContact (WhatsApp) ----------------------------------------------
# Clave que uContact debe enviar en el header X-API-Key (o Authorization:
# Bearer ...) al llamar al webhook. Definila en Render; si no existe, el
# webhook queda deshabilitado.
UCONTACT_API_KEY = os.environ.get("UCONTACT_API_KEY", "").strip()

LOCK = threading.RLock()

# ---- Sincronización con Drive / SharePoint / OneDrive ----------------
# Cada cuántos minutos se revisan las conexiones con sincronización
# automática (0 = solo manual) y tamaño máximo por archivo descargado.
try:
    SYNC_INTERVALO_MIN = int(os.environ.get("SYNC_INTERVALO_MIN", "60"))
except ValueError:
    SYNC_INTERVALO_MIN = 60

try:
    SYNC_MAX_MB = int(os.environ.get("SYNC_MAX_MB", "40"))
except ValueError:
    SYNC_MAX_MB = 40

# En WhatsApp el agente usa solo documentos "Vigente". Los "Pendiente de
# revisión" (por ejemplo los recién sincronizados) se prueban en el Playground.
# Para que WhatsApp también los use: WHATSAPP_INCLUIR_PENDIENTES=1.
WHATSAPP_INCLUIR_PENDIENTES = os.environ.get(
    "WHATSAPP_INCLUIR_PENDIENTES", ""
).strip().lower() in ("1", "true", "si", "sí", "yes", "on")

# Se reemplaza solo en las pruebas (para no usar la red)
HTTP_NUBE = None

# Máximo de PDFs por envío y de PDFs procesándose a la vez.
# Se procesan de a 2 para no pasarse del límite de la API ni de memoria.
MAX_PDFS_POR_ENVIO = 15

COLA_PROCESAMIENTO = ThreadPoolExecutor(max_workers=2)

# Las sincronizaciones se hacen de a una
COLA_SINCRONIZACION = ThreadPoolExecutor(max_workers=1)


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


def kb_en_disco_persistente():
    """
    True si la carpeta de la KB está en un disco distinto al del código
    (por ejemplo un Persistent Disk de Render montado en /var/data). En el
    disco normal de Render todo se borra en cada deploy o reinicio.
    """

    try:
        return os.stat(KNOWLEDGE_DIR).st_dev != os.stat(BASE_DIR).st_dev
    except OSError:
        return False


def resolver_ruta_pdf(doc):
    """Ruta real del PDF guardado, aunque haya cambiado la carpeta base."""

    ruta = doc.get("ruta_pdf") or ""

    if ruta and os.path.exists(ruta):
        return ruta

    if ruta:

        alternativa = os.path.join(DOCUMENTS_DIR, os.path.basename(ruta))

        if os.path.exists(alternativa):
            return alternativa

    return ruta


def render_seccion(section, **extra):

    documentos = documentos_para_vista()

    hay_procesando = any(
        d.get("procesamiento") == "PROCESANDO"
        for d in documentos
    ) or bool(extra.get("hay_sincronizando"))

    return render_template(
        "index.html",
        section=section,
        documentos=documentos,
        hay_procesando=hay_procesando,
        simulado=MODO_SIMULADO,
        kb_persistente=kb_en_disco_persistente(),
        productos=agent_service.PRODUCTOS,
        max_pdfs=MAX_PDFS_POR_ENVIO,
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

# ============================================================
# ATENCIÓN CON AGENTES POR PRODUCTO Y TIPIFICACIÓN
# ============================================================

def atender_con_agentes(
    producto, mensaje, historial, estado_previo, conversacion_id,
    incluir_pendientes, ignorar_pausa_recepcion=False
):
    """
    Recibe la consulta con el agente de recepción del producto y, cuando la
    clasifica, la pasa al especialista de esa categoría (si existe y está
    activo). Devuelve el resultado del agente que terminó respondiendo.
    """

    documentos = documentos_para_vista()

    agente_id, reasignaciones = conversation_service.agente_de(conversacion_id)

    def responder(contexto_agente, estado):

        return agent_service.responder(
            client=client,
            modelo=MODELO_AGENTE,
            documentos=documentos,
            historial=historial,
            mensaje=mensaje,
            estado_previo=estado,
            incluir_pendientes=incluir_pendientes,
            producto=producto,
            agente=contexto_agente
        )

    return enrutador_service.atender(
        responder, producto, estado_previo,
        agente_id=agente_id, reasignaciones=reasignaciones,
        ignorar_pausa_recepcion=ignorar_pausa_recepcion
    )


def agentes_con_metricas():
    """Lista de agentes con sus números, para la pantalla Agentes."""

    numeros = conversation_service.metricas_por_agente_id()

    vacio = {
        "total": 0, "resueltas": 0, "derivadas": 0, "en_curso": 0,
        "pasadas": 0, "pct_resueltas": "—", "pct_derivadas": "—",
    }

    return [
        dict(agente, **numeros.get(agente["id"], vacio))
        for agente in agentes_service.listar()
    ]


# ============================================================
# TIPIFICACIÓN AUTOMÁTICA Y SINCRONIZACIÓN
# ============================================================

def _ahora_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


CAMPOS_TIPIFICABLES = (
    "producto", "categoria", "subcategoria", "nivel", "fuente", "descripcion"
)


def _ya_revisado(doc):
    return bool((doc.get("tipificacion") or {}).get("revisada"))


def tipificar_documento(
    doc_id, campos=None, sobrescribir=False, forzar_pendiente=False,
    respetar_revision=False
):
    """
    Completa con IA los datos del documento. Por defecto solo rellena lo que
    está vacío; con sobrescribir=True vuelve a decidir todo menos el producto
    (el producto nunca se pisa si ya tenía uno). Devuelve un mensaje de
    error, o '' si salió bien.
    """

    campos = list(campos or CAMPOS_TIPIFICABLES)

    with LOCK:

        doc = next(
            (d for d in cargar_documentos() if d.get("id") == doc_id), None
        )

    if not doc:
        return "No se encontró el documento."

    if doc.get("procesamiento") != "PROCESADO":
        return "El documento todavía no terminó de procesarse."

    # Tipificación automática: si una persona ya revisó o editó este
    # documento, la IA no lo toca.
    if respetar_revision and _ya_revisado(doc):
        return ""

    try:

        resultado = tipificador_service.tipificar(
            client=client,
            modelo=MODELO_AGENTE,
            nombre=doc.get("nombre") or doc.get("archivo") or "",
            ruta_origen=doc.get("ruta_origen") or "",
            texto=armar_contenido_completo(doc),
            productos=agent_service.PRODUCTOS,
            simulado=MODO_SIMULADO
        )

    except ErrorTipificacion as e:

        with LOCK:

            documentos = cargar_documentos()

            for d in documentos:

                if d.get("id") != doc_id:
                    continue

                if respetar_revision and _ya_revisado(d):
                    return ""

                d["tipificacion"] = {
                    "origen": "IA", "error": str(e), "revisada": False,
                    "fecha": _ahora_iso()
                }

                guardar_documentos(documentos)

                break

        return str(e)

    cambios = {}

    for campo in campos:

        nuevo = resultado.get(campo, "")

        if not nuevo:
            continue

        actual = str(doc.get(campo) or "").strip()

        if campo == "producto":
            permitido = not actual
        else:
            permitido = sobrescribir or not actual

        if permitido:
            cambios[campo] = nuevo

    if forzar_pendiente and cambios:
        cambios["estado"] = "Pendiente de revisión"

    cambios["tipificacion"] = {
        "origen": "IA",
        "via": resultado.get("via", "IA"),
        "confianza": resultado.get("confianza", ""),
        "campos": [c for c in cambios if c in CAMPOS_TIPIFICABLES],
        "revisada": False,
        "error": "",
        "fecha": _ahora_iso(),
    }

    # La consulta a la IA tarda: mientras tanto alguien pudo editar el
    # documento. Se vuelve a mirar justo antes de guardar.
    with LOCK:

        documentos = cargar_documentos()

        for d in documentos:

            if d.get("id") != doc_id:
                continue

            if respetar_revision and _ya_revisado(d):
                return ""

            d.update(cambios)

            guardar_documentos(documentos)

            break

    return ""


def procesar_y_tipificar(doc_id, ruta_pdf, tipificar=None):
    """Procesa el PDF y, si se pidió, lo tipifica con IA a continuación."""

    procesar_documento(doc_id, ruta_pdf)

    if tipificar:

        try:
            tipificar_documento(doc_id, respetar_revision=True, **tipificar)
        except Exception as e:
            print(f"[{doc_id}] Error tipificando: {e}")


class SyncHost:
    """Lo que necesita la sincronización para escribir en la Knowledge Base."""

    normalizar_producto = staticmethod(agent_service.normalizar_producto)

    def ruta_pdf(self, doc_id, nombre):
        return os.path.join(DOCUMENTS_DIR, f"{doc_id[:8]}_{nombre}")

    def existe(self, doc_id):

        with LOCK:
            return any(d.get("id") == doc_id for d in cargar_documentos())

    def crear(self, doc_id, ruta_pdf, datos):
        """Crea el documento y devuelve el estado con el que quedó."""

        # "Vigente automático": entra Vigente, salvo que no se sepa su
        # producto (sin producto, el agente lo usaría en TODOS los productos).
        vigente = bool(datos.get("vigente_auto")) and bool(datos.get("producto"))

        estado = "Vigente" if vigente else "Pendiente de revisión"

        documento = {
            "id": doc_id,
            "archivo": datos["archivo"],
            "ruta_pdf": ruta_pdf.replace("\\", "/"),
            "nombre": datos["nombre"],
            "producto": datos["producto"],
            "categoria": "",
            "subcategoria": "",
            "nivel": "L1",
            "estado": estado,
            "fuente": "Otro",
            "descripcion": "",
            "ruta_origen": datos["ruta_origen"],
            "origen": datos["origen"],
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

        cfg = (
            {"sobrescribir": True, "forzar_pendiente": not vigente}
            if datos["tipificar_ia"] else None
        )

        COLA_PROCESAMIENTO.submit(procesar_y_tipificar, doc_id, ruta_pdf, cfg)

        return estado

    def actualizar(self, doc_id, ruta_pdf, datos):

        with LOCK:

            documentos = cargar_documentos()

            doc = next((d for d in documentos if d.get("id") == doc_id), None)

            if not doc:
                return

            anterior = doc.get("ruta_pdf") or ""

            if anterior and anterior != ruta_pdf.replace("\\", "/") \
                    and os.path.exists(anterior):
                try:
                    os.remove(anterior)
                except OSError:
                    pass

            tip = doc.get("tipificacion") or {}

            retipificar = (
                datos["tipificar_ia"]
                and tip.get("origen") == "IA"
                and not tip.get("revisada")
            )

            doc.update({
                "archivo": datos["archivo"],
                "ruta_pdf": ruta_pdf.replace("\\", "/"),
                "ruta_origen": datos["ruta_origen"],
                "origen": datos["origen"],
                "texto_extraido": "",
                "imagenes": [],
                "cantidad_imagenes": 0,
                "procesamiento": "PROCESANDO",
                "error": "",
                "advertencia": "",
            })

            # El contenido cambió: con "Vigente automático" conserva su
            # estado; si no, vuelve a revisión.
            if not datos.get("vigente_auto"):
                doc["estado"] = "Pendiente de revisión"

            if not doc.get("producto") and datos["producto"]:
                doc["producto"] = datos["producto"]

            guardar_documentos(documentos)

        cfg = (
            {
                "sobrescribir": True,
                "forzar_pendiente": not datos.get("vigente_auto")
            }
            if retipificar else None
        )

        COLA_PROCESAMIENTO.submit(procesar_y_tipificar, doc_id, ruta_pdf, cfg)

    def archivar(self, doc_id):

        actualizar_documento(doc_id, {
            "estado": "Archivado",
            "archivado_motivo": "Ya no está en la carpeta de origen."
        })


SYNC_HOST = SyncHost()


def ejecutar_sincronizacion(conexion_id):

    try:

        resultado = sincronizacion_service.sincronizar(
            conexion_id, SYNC_HOST, http=HTTP_NUBE, max_mb=SYNC_MAX_MB
        )

        if resultado.get("error"):
            print(f"Sincronización {conexion_id}: {resultado['error']}")

    except Exception as e:
        print(f"Sincronización {conexion_id} falló: {e}")


def encolar_sincronizacion(conexion_id):
    COLA_SINCRONIZACION.submit(ejecutar_sincronizacion, conexion_id)


sincronizacion_service.iniciar_programador(
    encolar_sincronizacion, SYNC_INTERVALO_MIN
)


# ============================================================
# LOGIN Y USUARIOS
# ============================================================
# Los usuarios se crean desde la pantalla "Usuarios" (solo administradores)
# y se guardan en la base de datos. El primer administrador se crea al
# arrancar con las variables de entorno ADMIN_USUARIO y ADMIN_CLAVE.
# Para desactivar el login en una PC local: AUTH_DESACTIVADA=1.

AUTH_DESACTIVADA = os.environ.get("AUTH_DESACTIVADA", "").strip().lower() in (
    "1", "true", "si", "sí", "yes", "on"
)

# Rutas que no piden sesión: el login mismo, los archivos estáticos y /health.
# El webhook de uContact se protege con su propia clave UCONTACT_API_KEY.
RUTAS_PUBLICAS = ("login", "static", "health")

PREFIJOS_PUBLICOS = ("/api/ucontact/",)

# Anti fuerza bruta: 5 intentos fallidos por IP = 5 minutos bloqueado
_INTENTOS = {}
MAX_INTENTOS_LOGIN = 5
BLOQUEO_SEGUNDOS = 300


def _ip_cliente():

    reenviada = request.headers.get("X-Forwarded-For", "")

    return (reenviada.split(",")[0].strip() if reenviada else "") \
        or request.remote_addr or "?"


def _bloqueado(ip):

    registro = _INTENTOS.get(ip)

    if not registro:
        return False

    fallos, hasta = registro

    if hasta and time.time() < hasta:
        return True

    if hasta and time.time() >= hasta:
        _INTENTOS.pop(ip, None)

    return False


def _registrar_fallo(ip):

    fallos, hasta = _INTENTOS.get(ip, (0, 0))

    fallos += 1

    hasta = time.time() + BLOQUEO_SEGUNDOS if fallos >= MAX_INTENTOS_LOGIN else 0

    _INTENTOS[ip] = (fallos, hasta)


def _destino_seguro(destino):
    """Solo se vuelve a rutas internas (evita redirecciones a otros sitios)."""

    destino = destino or ""

    if destino.startswith("/") and not destino.startswith("//") \
            and "\\" not in destino:
        return destino

    return "/"


def _usuario_de_la_sesion():
    """
    Usuario de la sesión, releído de la base en cada pedido: si se lo
    desactiva, se le cambia la clave o se lo elimina, la sesión deja de
    valer enseguida.
    """

    uid = session.get("uid")

    if not uid:
        return None

    fila = usuarios_service.obtener(uid)

    if (
        not fila
        or not fila["activo"]
        or session.get("sv") != usuarios_service.firma_sesion(fila)
    ):
        session.clear()
        return None

    return fila


def _iniciar_sesion(fila):

    session.clear()
    session["uid"] = fila["id"]
    session["sv"] = usuarios_service.firma_sesion(fila)
    session.permanent = True


@app.before_request
def exigir_login():

    g.usuario = None

    if AUTH_DESACTIVADA:
        g.usuario = {"id": 0, "usuario": "local", "rol": "admin"}
        return None

    if request.endpoint in RUTAS_PUBLICAS and request.endpoint != "health":
        return None

    if request.path.startswith(PREFIJOS_PUBLICOS):
        return None

    g.usuario = _usuario_de_la_sesion()

    if request.endpoint in RUTAS_PUBLICAS:
        return None

    if g.usuario:
        return None

    # Llamadas de la página (fetch): se responde JSON, no una redirección
    if request.path.startswith("/playground/chat"):

        return jsonify({
            "error": "Tu sesión venció. Recargá la página e iniciá sesión."
        }), 401

    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.context_processor
def datos_de_sesion():

    return {"usuario_actual": getattr(g, "usuario", None)}


@app.after_request
def no_guardar_en_cache(respuesta):

    # Las pantallas con datos internos no se guardan en el navegador
    if getattr(g, "usuario", None):
        respuesta.headers["Cache-Control"] = "no-store"

    return respuesta


def solo_admin(vista):
    """Decorador: la ruta solo la puede usar un administrador."""

    from functools import wraps

    @wraps(vista)
    def envoltura(*args, **kwargs):

        usuario = getattr(g, "usuario", None)

        if not usuario or usuario["rol"] != "admin":

            flash("Esta sección es solo para administradores.")

            return redirect(url_for("home"))

        return vista(*args, **kwargs)

    return envoltura


@app.route("/login", methods=["GET", "POST"])
def login():

    destino = _destino_seguro(
        request.values.get("next") or request.args.get("next")
    )

    if AUTH_DESACTIVADA or _usuario_de_la_sesion():
        return redirect(destino)

    if request.method == "GET":
        return render_template("login.html", error="", next=destino)

    ip = _ip_cliente()

    if not usuarios_service.cantidad():

        return render_template(
            "login.html",
            error="Todavía no hay usuarios: falta definir ADMIN_USUARIO y "
                  "ADMIN_CLAVE en el servidor.",
            next=destino
        ), 503

    if _bloqueado(ip):

        return render_template(
            "login.html",
            error="Demasiados intentos. Probá de nuevo en unos minutos.",
            next=destino
        ), 429

    fila = usuarios_service.verificar(
        request.form.get("usuario", ""),
        request.form.get("clave", "")
    )

    if not fila:

        _registrar_fallo(ip)

        return render_template(
            "login.html",
            error="Usuario o contraseña incorrectos.",
            next=destino
        ), 401

    _INTENTOS.pop(ip, None)

    _iniciar_sesion(fila)

    return redirect(destino)


@app.route("/logout", methods=["POST"])
def logout():

    session.clear()

    return redirect(url_for("login"))


# ---- Mi cuenta (cualquier usuario) ------------------------------------

@app.route("/cuenta")
def cuenta():
    return render_seccion("cuenta")


@app.route("/cuenta/clave", methods=["POST"])
def cuenta_clave():

    usuario = g.usuario

    if AUTH_DESACTIVADA or not usuario:

        flash("El login está desactivado en este servidor.")

        return redirect(url_for("cuenta"))

    actual = request.form.get("clave_actual", "")
    nueva = request.form.get("clave_nueva", "")
    repetida = request.form.get("clave_repetida", "")

    if not usuarios_service.verificar(usuario["usuario"], actual):

        flash("La contraseña actual no es correcta.")

    elif nueva != repetida:

        flash("La contraseña nueva y su repetición no coinciden.")

    else:

        error = usuarios_service.cambiar_clave(usuario["id"], nueva)

        if error:

            flash(error)

        else:

            # Se renueva la sesión de este usuario; las demás quedan cerradas
            _iniciar_sesion(usuarios_service.obtener(usuario["id"]))

            flash("Contraseña actualizada.")

    return redirect(url_for("cuenta"))


# ---- Administración de usuarios (solo admin) --------------------------

@app.route("/usuarios")
@solo_admin
def usuarios():

    return render_seccion(
        "usuarios",
        lista_usuarios=usuarios_service.listar(),
        roles=usuarios_service.ROLES
    )


@app.route("/usuarios/crear", methods=["POST"])
@solo_admin
def usuarios_crear():

    error = usuarios_service.crear(
        request.form.get("usuario", ""),
        request.form.get("clave", ""),
        request.form.get("rol", "usuario")
    )

    flash(error or "Usuario creado.")

    return redirect(url_for("usuarios"))


@app.route("/usuarios/<int:usuario_id>/clave", methods=["POST"])
@solo_admin
def usuarios_clave(usuario_id):

    if not usuarios_service.obtener(usuario_id):

        flash("No se encontró el usuario.")

    else:

        error = usuarios_service.cambiar_clave(
            usuario_id, request.form.get("clave", "")
        )

        flash(error or "Contraseña restablecida. Se cerró su sesión.")

        # Si el admin se cambió la propia, se le renueva la sesión
        if not error and usuario_id == g.usuario["id"] and not AUTH_DESACTIVADA:
            _iniciar_sesion(usuarios_service.obtener(usuario_id))

    return redirect(url_for("usuarios"))


@app.route("/usuarios/<int:usuario_id>/estado", methods=["POST"])
@solo_admin
def usuarios_estado(usuario_id):

    if usuario_id == g.usuario["id"]:

        flash("No podés desactivar tu propio usuario.")

        return redirect(url_for("usuarios"))

    objetivo = usuarios_service.obtener(usuario_id)

    if not objetivo:

        flash("No se encontró el usuario.")

    else:

        error = usuarios_service.cambiar_activo(
            usuario_id, not objetivo["activo"]
        )

        flash(error or (
            "Usuario desactivado." if objetivo["activo"]
            else "Usuario activado."
        ))

    return redirect(url_for("usuarios"))


@app.route("/usuarios/<int:usuario_id>/rol", methods=["POST"])
@solo_admin
def usuarios_rol(usuario_id):

    if usuario_id == g.usuario["id"]:

        flash("No podés cambiar tu propio rol.")

        return redirect(url_for("usuarios"))

    error = usuarios_service.cambiar_rol(
        usuario_id, request.form.get("rol", "")
    )

    flash(error or "Rol actualizado.")

    return redirect(url_for("usuarios"))


@app.route("/usuarios/<int:usuario_id>/eliminar", methods=["POST"])
@solo_admin
def usuarios_eliminar(usuario_id):

    if usuario_id == g.usuario["id"]:

        flash("No podés eliminar tu propio usuario.")

        return redirect(url_for("usuarios"))

    error = usuarios_service.eliminar(usuario_id)

    flash(error or "Usuario eliminado.")

    return redirect(url_for("usuarios"))


def agente_pausado():
    return conversation_service.obtener_ajuste("agente_pausado", "0") == "1"


def _datos_resumen():

    vigentes = sum(
        1 for d in cargar_documentos()
        if (d.get("estado") == "Vigente"
            and d.get("procesamiento", "PROCESADO") == "PROCESADO")
    )

    return {
        "resumen": conversation_service.resumen_rapido(),
        "pausado": agente_pausado(),
        "docs_vigentes": vigentes,
        "agentes": agentes_con_metricas(),
        "categorias_agente": tipificador_service.CATEGORIAS,
    }


@app.route("/")
def home():
    return render_seccion(None, **_datos_resumen())


@app.route("/agentes")
def agentes():
    return render_seccion("agentes", **_datos_resumen())


@app.route("/agentes/crear", methods=["POST"])
@solo_admin
def agentes_crear():

    error = agentes_service.crear(
        request.form.get("nombre", ""),
        request.form.get("producto", ""),
        request.form.get("categoria", ""),
        request.form.get("instrucciones", ""),
        agent_service.PRODUCTOS,
        tipificador_service.CATEGORIAS
    )

    flash(error or "Agente creado. Ya recibe los casos de esa categoría.")

    return redirect(url_for("agentes") + "#agentesLista")


@app.route("/agentes/<int:agente_id>/editar", methods=["POST"])
@solo_admin
def agentes_editar(agente_id):

    error = agentes_service.actualizar(
        agente_id,
        request.form.get("nombre", ""),
        request.form.get("instrucciones", "")
    )

    flash(error or "Agente actualizado.")

    return redirect(url_for("agentes") + "#agentesLista")


@app.route("/agentes/<int:agente_id>/estado", methods=["POST"])
@solo_admin
def agentes_estado(agente_id):

    agente = agentes_service.obtener(agente_id)

    if not agente:

        flash("No se encontró el agente.")

    else:

        agentes_service.cambiar_activo(agente_id, not agente["activo"])

        if agente["activo"]:

            flash(
                f"«{agente['nombre']}» pausado: "
                + (
                    "en WhatsApp ese producto ya no responde (el flujo de "
                    "uContact tiene que seguir con una persona)."
                    if not agente["categoria"] else
                    "los casos de esa categoría los resuelve el agente de "
                    "recepción del producto."
                )
            )

        else:

            flash(f"«{agente['nombre']}» activado.")

    return redirect(url_for("agentes") + "#agentesLista")


@app.route("/agentes/<int:agente_id>/eliminar", methods=["POST"])
@solo_admin
def agentes_eliminar(agente_id):

    error = agentes_service.eliminar(agente_id)

    flash(error or "Agente eliminado.")

    return redirect(url_for("agentes") + "#agentesLista")


@app.route("/agentes/pausar", methods=["POST"])
@solo_admin
def agentes_pausar():
    """
    Pausa o reanuda al agente en WhatsApp. Pausado, el webhook de uContact
    no usa la IA y responde 'fuera_de_alcance' para que el flujo siga con
    una persona. El Playground sigue funcionando.
    """

    pausar = not agente_pausado()

    conversation_service.guardar_ajuste("agente_pausado", "1" if pausar else "0")

    flash(
        "Agente pausado: en WhatsApp ya no responde y el flujo de uContact "
        "tiene que seguir con una persona." if pausar
        else "Agente reanudado: vuelve a atender en WhatsApp."
    )

    return redirect(url_for("agentes"))


@app.route("/agentes/configurar")
def agentes_configurar():
    return render_seccion("agente_config")


@app.route("/knowledge")
def knowledge():
    return render_seccion("knowledge")


@app.route("/conexiones")
def conexiones():

    return render_seccion(
        "conexiones",
        lista_conexiones=conexiones_service.listar(),
        tipos_conexion=proveedores_nube.TIPOS,
        cifrado_ok=crypto_service.disponible(),
        sync_intervalo=SYNC_INTERVALO_MIN,
        hay_sincronizando=conexiones_service.hay_sincronizando()
    )


def _datos_conexion_del_form(tipo_forzado=None):
    """Lee y valida nombre, URL, producto y opciones. (datos, error)."""

    tipo = tipo_forzado or request.form.get("tipo", "").strip()
    nombre = request.form.get("nombre", "").strip()[:80]
    url = request.form.get("url", "").strip()
    producto = request.form.get("producto", "auto").strip()

    if not nombre:
        return None, "Poné un nombre para la conexión."

    error = proveedores_nube.validar_url(tipo, url)

    if error:
        return None, error

    if producto != "auto" and producto not in agent_service.PRODUCTOS:
        return None, "Producto inválido."

    return {
        "tipo": tipo, "nombre": nombre, "url": url, "producto": producto,
        "auto": request.form.get("auto") == "1",
        "tipificar_ia": request.form.get("tipificar_ia") == "1",
        "auto_vigente": request.form.get("auto_vigente") == "1",
    }, ""


@app.route("/conexiones/crear", methods=["POST"])
@solo_admin
def conexiones_crear():

    if not crypto_service.disponible():

        flash(
            "No se pudo preparar el cifrado de las credenciales. Definí "
            "FLASK_SECRET_KEY en Render (una clave larga y fija)."
        )

        return redirect(url_for("conexiones"))

    datos, error = _datos_conexion_del_form()

    if not error:

        secreto, error = proveedores_nube.secreto_desde_form(
            datos["tipo"], request.form, request.files
        )

        if not error and not secreto:
            error = "Cargá las credenciales de la cuenta."

    if error:

        flash(error)

        return redirect(url_for("conexiones"))

    conexiones_service.crear(
        datos["tipo"], datos["nombre"], datos["url"], datos["producto"],
        datos["auto"], datos["tipificar_ia"], secreto,
        auto_vigente=datos["auto_vigente"]
    )

    flash("Conexión creada. Probala y después sincronizá.")

    return redirect(url_for("conexiones"))


@app.route("/conexiones/<int:conexion_id>/editar", methods=["POST"])
@solo_admin
def conexiones_editar(conexion_id):

    actual = conexiones_service.obtener(conexion_id)

    if not actual:

        flash("No se encontró la conexión.")

        return redirect(url_for("conexiones"))

    # El tipo no se cambia al editar
    datos, error = _datos_conexion_del_form(actual["tipo"])

    secreto = None

    if not error:

        secreto, error = proveedores_nube.secreto_desde_form(
            actual["tipo"], request.form, request.files
        )

        if secreto and not crypto_service.disponible():
            error = "Falta definir FLASK_SECRET_KEY en Render."

    if error:

        flash(error)

        return redirect(url_for("conexiones"))

    conexiones_service.actualizar(
        conexion_id, datos["nombre"], datos["url"], datos["producto"],
        datos["auto"], datos["tipificar_ia"], secreto,
        auto_vigente=datos["auto_vigente"]
    )

    flash("Conexión actualizada.")

    return redirect(url_for("conexiones"))


@app.route("/conexiones/<int:conexion_id>/probar", methods=["POST"])
@solo_admin
def conexiones_probar(conexion_id):

    mensaje, ok = sincronizacion_service.probar(conexion_id, http=HTTP_NUBE)

    flash(("✔ " if ok else "✖ ") + mensaje)

    return redirect(url_for("conexiones"))


@app.route("/conexiones/<int:conexion_id>/sincronizar", methods=["POST"])
@solo_admin
def conexiones_sincronizar(conexion_id):

    conexion = conexiones_service.obtener(conexion_id)

    if not conexion:
        flash("No se encontró la conexión.")
    elif not conexion["activa"]:
        flash("La conexión está desactivada.")
    elif conexion["sincronizando"]:
        flash("Ya se está sincronizando.")
    else:
        encolar_sincronizacion(conexion_id)
        flash(
            "Sincronización iniciada. Los documentos nuevos van a aparecer "
            "en la Knowledge Base; la página se actualiza sola."
        )

    return redirect(url_for("conexiones"))


@app.route("/conexiones/<int:conexion_id>/publicar", methods=["POST"])
@solo_admin
def conexiones_publicar(conexion_id):
    """
    Pasa a Vigente los documentos de esta conexión que quedaron Pendientes
    de revisión (por ejemplo, sincronizados antes de activar el Vigente
    automático). Los que no tienen producto se dejan Pendientes.
    """

    if not conexiones_service.obtener(conexion_id):

        flash("No se encontró la conexión.")

        return redirect(url_for("conexiones"))

    publicados = sin_producto = 0

    with LOCK:

        documentos = cargar_documentos()

        for doc in documentos:

            origen = doc.get("origen") or {}

            if (
                origen.get("conexion_id") != conexion_id
                or doc.get("estado") != "Pendiente de revisión"
                or doc.get("procesamiento") != "PROCESADO"
            ):
                continue

            if not doc.get("producto"):
                sin_producto += 1
                continue

            doc["estado"] = "Vigente"
            publicados += 1

        if publicados:
            guardar_documentos(documentos)

    flash(
        f"{publicados} documento(s) pasaron a Vigente."
        + (
            f" {sin_producto} quedaron Pendientes porque no tienen producto: "
            "editalos y asignales uno."
            if sin_producto else ""
        )
    )

    return redirect(url_for("conexiones"))


@app.route("/conexiones/<int:conexion_id>/estado", methods=["POST"])
@solo_admin
def conexiones_estado(conexion_id):

    conexion = conexiones_service.obtener(conexion_id)

    if conexion:

        conexiones_service.cambiar_activa(conexion_id, not conexion["activa"])

        flash(
            "Conexión desactivada." if conexion["activa"]
            else "Conexión activada."
        )

    return redirect(url_for("conexiones"))


@app.route("/conexiones/<int:conexion_id>/eliminar", methods=["POST"])
@solo_admin
def conexiones_eliminar(conexion_id):

    conexiones_service.eliminar(conexion_id)

    flash(
        "Conexión eliminada. Los documentos que ya se habían incorporado "
        "siguen en la Knowledge Base."
    )

    return redirect(url_for("conexiones"))


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

def _es_automatico(valor):
    """Valores que significan 'que lo complete la IA' (o que quedó sin elegir)."""

    valor = (valor or "").strip().lower()

    return (
        not valor or valor == "auto" or valor.startswith("seleccionar")
        or "automático" in valor or "automatico" in valor
    )


def _valor_de_lista(lista, indice, por_defecto=""):
    """
    Valor del campo para el archivo número `indice`. Cada PDF trae su propia
    configuración (los campos del formulario se repiten, uno por archivo y
    en el mismo orden). Si falta alguno, se usa el primero.
    """

    if indice < len(lista):
        return str(lista[indice]).strip()

    if lista:
        return str(lista[0]).strip()

    return por_defecto


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

    # Configuración de cada archivo (una entrada por PDF, en el mismo orden)
    campos = {
        nombre: request.form.getlist(nombre)
        for nombre in (
            "producto", "categoria", "subcategoria", "nivel",
            "estado", "fuente", "descripcion"
        )
    }

    # Una casilla sin tildar no se envía: solo se usa la IA si vino tildada
    usar_ia = "1" in request.form.getlist("tipificar_ia")

    recibidos = 0
    rechazados = []

    for indice, archivo in enumerate(archivos):

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

        # El nombre del documento es directamente el nombre del archivo
        nombre = os.path.splitext(archivo.filename)[0].strip()

        producto = _valor_de_lista(campos["producto"], indice)

        # "Automático (IA)" o sin elegir = lo completa la IA (si está activada)
        valores = {
            nombre_campo: _valor_de_lista(campos[nombre_campo], indice)
            for nombre_campo in (
                "categoria", "subcategoria", "nivel", "fuente", "descripcion"
            )
        }

        a_completar = []

        for nombre_campo, valor in valores.items():

            if _es_automatico(valor):

                valores[nombre_campo] = ""

                if nombre_campo != "producto":
                    a_completar.append(nombre_campo)

        estado_elegido = _valor_de_lista(campos["estado"], indice)

        documento = {
            "id": doc_id,
            "archivo": nombre_archivo,
            "ruta_pdf": ruta_pdf.replace("\\", "/"),
            "nombre": nombre,
            "producto": agent_service.normalizar_producto(producto, producto),
            "categoria": valores["categoria"],
            "subcategoria": valores["subcategoria"],
            "nivel": valores["nivel"],
            "estado": estado_elegido,
            "fuente": valores["fuente"],
            "descripcion": valores["descripcion"],
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
        # subida responda enseguida y no se corte por timeout. Si la IA
        # completa algo, el documento queda "Pendiente de revisión".
        cfg = None

        if usar_ia and a_completar:
            cfg = {
                "campos": a_completar,
                "sobrescribir": False,
                "forzar_pendiente": True
            }

        COLA_PROCESAMIENTO.submit(
            procesar_y_tipificar, doc_id, ruta_pdf, cfg
        )

        recibidos += 1

    if recibidos:

        flash(
            f"{recibidos} PDF recibido(s). Se están procesando el texto y "
            "las imágenes; la página se actualiza sola hasta que terminen."
            + (
                " Lo que dejaste en automático lo completa la IA y esos "
                "documentos quedan Pendientes de revisión."
                if usar_ia else ""
            )
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

        ruta_pdf = resolver_ruta_pdf(documento)

        if not ruta_pdf or not os.path.exists(ruta_pdf):

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

        # El Playground usa el mismo recorrido que WhatsApp (recepción ->
        # especialista), pero puede probar un producto aunque su agente de
        # recepción esté pausado.
        resultado = atender_con_agentes(
            producto=agent_service.normalizar_producto(
                datos.get("producto"), agent_service.PRODUCTO
            ),
            mensaje=mensaje,
            historial=agent_service._limpiar_historial(datos.get("historial")),
            estado_previo=datos.get("estado"),
            conversacion_id=str(datos.get("conversacion_id") or ""),
            incluir_pendientes=bool(datos.get("incluir_pendientes", True)),
            ignorar_pausa_recepcion=True
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
# UCONTACT - WEBHOOK PARA WHATSAPP
# ============================================================

def _primero(datos, *claves):
    """Primer valor no vacío entre varios nombres posibles de campo."""

    for clave in claves:

        valor = datos.get(clave)

        if isinstance(valor, (str, int, float)) and str(valor).strip():
            return str(valor).strip()

    return ""


def _ucontact_autorizado():

    if not UCONTACT_API_KEY:
        return False

    token = request.headers.get("X-API-Key", "").strip()

    if not token:

        auth = request.headers.get("Authorization", "")

        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()

    return hmac.compare_digest(token, UCONTACT_API_KEY)


def _rechazo_ucontact():
    """Devuelve la respuesta de error si la llamada no está autorizada."""

    if not UCONTACT_API_KEY:

        return jsonify({
            "ok": False,
            "accion": "error",
            "error": "Falta definir UCONTACT_API_KEY en el servidor."
        }), 503

    if not _ucontact_autorizado():

        return jsonify({
            "ok": False,
            "accion": "error",
            "error": "No autorizado."
        }), 401

    return None


def _payload_ucontact(conversacion_id, producto, respuesta, estado,
                      resumen, derivar, destino, motivo, agente_nombre=""):

    etapa = estado.get("etapa", "") if estado else ""

    if derivar:
        accion = "derivar"
    elif etapa == "Resuelto":
        accion = "cerrar"
    else:
        accion = "responder"

    return {
        "ok": True,
        "conversacion_id": conversacion_id,
        "accion": accion,
        "respuesta": respuesta,
        "derivar": bool(derivar),
        "destino": destino or "",
        "cola": ucontact_service.resolver_cola(producto, destino) if derivar else "",
        "motivo_derivacion": motivo or "",
        "resuelto": accion == "cerrar",
        "etapa": etapa,
        "categoria": (estado or {}).get("categoria", ""),
        "subcategoria": (estado or {}).get("subcategoria", ""),
        "resumen_tecnico": resumen,
        "agente": agente_nombre
    }


@app.route("/api/ucontact/mensaje", methods=["POST"])
def ucontact_mensaje():
    """
    uContact llama acá con cada mensaje del cliente (ya identificado el
    producto). Responde qué debe hacer el flujo: responder, cerrar o
    derivar a una cola.
    """

    rechazo = _rechazo_ucontact()

    if rechazo:
        return rechazo

    datos = request.get_json(silent=True) or {}

    external_id = _primero(
        datos, "conversation_id", "conversacion_id", "session_id",
        "chat_id", "ticket_id", "interaction_id"
    )

    mensaje = _primero(datos, "message", "mensaje", "text", "texto")

    message_id = _primero(datos, "message_id", "mensaje_id", "msg_id")

    cliente = _primero(datos, "customer_name", "cliente", "nombre", "name")

    telefono = _primero(datos, "phone", "telefono", "customer_phone", "from")

    producto_recibido = _primero(datos, "product", "producto")

    if not external_id or not mensaje:

        return jsonify({
            "ok": False,
            "accion": "error",
            "error": "Faltan datos: se necesita conversation_id y message."
        }), 400

    mensaje = mensaje[:2000]

    # Producto que detectó uContact: debe ser uno de los que atiende el agente
    producto = agent_service.normalizar_producto(producto_recibido)

    if not producto:

        return jsonify({
            "ok": True,
            "accion": "fuera_de_alcance",
            "respuesta": "",
            "derivar": False,
            "cola": "",
            "error": (
                "El agente L1 no atiende el producto "
                f"'{producto_recibido or '(vacío)'}'. Productos: "
                + ", ".join(agent_service.PRODUCTOS) + "."
            )
        })

    # Reintento de uContact del mismo mensaje: se devuelve lo ya respondido
    previa = ucontact_service.respuesta_ya_procesada(external_id, message_id)

    if previa:
        return jsonify(previa)

    # Agente pausado desde la pantalla Agentes: no se usa la IA
    if agente_pausado():

        return jsonify({
            "ok": True,
            "accion": "fuera_de_alcance",
            "pausado": True,
            "respuesta": "",
            "derivar": False,
            "cola": "",
            "error": "El agente está pausado."
        })

    # Agente de recepción de este producto pausado
    recepcion = agentes_service.obtener_base(producto)

    if recepcion and not recepcion["activo"]:

        return jsonify({
            "ok": True,
            "accion": "fuera_de_alcance",
            "pausado": True,
            "respuesta": "",
            "derivar": False,
            "cola": "",
            "error": f"El agente de {producto} está pausado."
        })

    sesion = ucontact_service.buscar_sesion(external_id)

    # Conversación ya derivada: no se vuelve a llamar a la IA
    if sesion and sesion["derivada"]:

        return jsonify(_payload_ucontact(
            sesion["conversacion_id"], producto,
            "Tu consulta ya fue derivada. En breve te va a atender un "
            "especialista con todo lo que nos contaste.",
            {"etapa": "Derivado"}, None, True,
            sesion["destino"], sesion["motivo"]
        ))

    # Conversación ya resuelta: si el cliente vuelve a escribir, es otra consulta
    if sesion and sesion["cerrada"]:
        sesion = None

    if not client:

        return jsonify({
            "ok": False,
            "accion": "error",
            "error": "La IA no está configurada en el servidor."
        }), 503

    if sesion:
        historial, estado_previo = ucontact_service.cargar_contexto(
            sesion["conversacion_id"]
        )
        conversacion_id = sesion["conversacion_id"]
    else:
        historial, estado_previo, conversacion_id = [], None, ""

    try:

        resultado = atender_con_agentes(
            producto=producto,
            mensaje=mensaje,
            historial=historial,
            estado_previo=estado_previo,
            conversacion_id=conversacion_id,
            incluir_pendientes=WHATSAPP_INCLUIR_PENDIENTES
        )

    except Exception as e:

        print(f"Error en el agente (uContact): {e}")

        return jsonify({
            "ok": False,
            "accion": "error",
            "error": f"No se pudo obtener respuesta del agente: {e}"
        }), 502

    canal = "simulado" if MODO_SIMULADO else "whatsapp"

    try:

        conversacion_id = conversation_service.guardar_turno(
            conversacion_id=conversacion_id,
            canal=canal,
            historial_previo=[],
            mensaje_cliente=mensaje,
            resultado=resultado
        )

        if not sesion:
            ucontact_service.registrar_sesion(
                external_id, conversacion_id, cliente, telefono, producto
            )

    except Exception as e:

        # No se corta la atención: el cliente recibe igual la respuesta
        print(f"Error guardando la conversación (uContact): {e}")

    estado = resultado["estado"]

    deriv = estado.get("derivacion") or {}

    payload = _payload_ucontact(
        conversacion_id, producto, resultado["respuesta"], estado,
        resultado.get("resumen_tecnico"), deriv.get("derivar"),
        deriv.get("destino"), deriv.get("motivo"),
        agente_nombre=(resultado.get("agente") or {}).get("nombre", "")
    )

    ucontact_service.guardar_respuesta(external_id, message_id, payload)

    return jsonify(payload)


@app.route("/api/ucontact/cierre", methods=["POST"])
def ucontact_cierre():
    """uContact avisa que la conversación terminó (el cliente dejó de responder, etc.)."""

    rechazo = _rechazo_ucontact()

    if rechazo:
        return rechazo

    datos = request.get_json(silent=True) or {}

    external_id = _primero(
        datos, "conversation_id", "conversacion_id", "session_id",
        "chat_id", "ticket_id", "interaction_id"
    )

    sesion = ucontact_service.buscar_sesion(external_id) if external_id else None

    if not sesion:
        return jsonify({"ok": False, "error": "Conversación no encontrada."}), 404

    ucontact_service.cerrar_conversacion(sesion["conversacion_id"])

    return jsonify({"ok": True, "conversacion_id": sesion["conversacion_id"]})


# ============================================================
# EDITAR, CONFIRMAR Y TIPIFICAR DOCUMENTOS
# ============================================================

ESTADOS_DOC = ("Vigente", "Pendiente de revisión", "Archivado")


def _marcar_revisada(doc_id, **cambios):

    with LOCK:

        documentos = cargar_documentos()

        for doc in documentos:

            if doc.get("id") != doc_id:
                continue

            tip = dict(doc.get("tipificacion") or {})

            tip["revisada"] = True
            tip["revisada_por"] = (g.usuario or {}).get("usuario", "")
            tip["fecha_revision"] = _ahora_iso()

            doc.update(cambios)
            doc["tipificacion"] = tip

            guardar_documentos(documentos)

            return True

    return False


@app.route("/knowledge/documento/<doc_id>/editar", methods=["POST"])
def knowledge_editar(doc_id):

    def campo(nombre):
        return request.form.get(nombre, "").strip()

    producto = agent_service.normalizar_producto(campo("producto"), "")

    categoria = campo("categoria")
    nivel = campo("nivel")
    estado = campo("estado")
    fuente = campo("fuente")

    if estado not in ESTADOS_DOC:

        flash("Estado inválido.")

        return redirect(url_for("knowledge"))

    cambios = {
        "producto": producto,
        "categoria": categoria if categoria in tipificador_service.CATEGORIAS else "",
        "subcategoria": campo("subcategoria")[:80],
        "nivel": nivel if nivel in tipificador_service.NIVELES else "",
        "estado": estado,
        "fuente": fuente if fuente in tipificador_service.FUENTES else "",
        "descripcion": campo("descripcion")[:600],
    }

    if _marcar_revisada(doc_id, **cambios):
        flash("Documento actualizado.")
    else:
        flash("No se encontró el documento.")

    return redirect(url_for("knowledge"))


@app.route("/knowledge/documento/<doc_id>/confirmar", methods=["POST"])
def knowledge_confirmar(doc_id):

    if _marcar_revisada(doc_id, estado="Vigente"):
        flash("Documento confirmado: ahora está Vigente.")
    else:
        flash("No se encontró el documento.")

    return redirect(url_for("knowledge"))


@app.route("/knowledge/documento/<doc_id>/tipificar", methods=["POST"])
def knowledge_tipificar(doc_id):

    if not client:

        flash("Falta configurar la IA en el servidor.")

        return redirect(url_for("knowledge"))

    error = tipificar_documento(doc_id, sobrescribir=True)

    flash(
        error or
        "Documento tipificado con IA. Revisá la clasificación y corregila "
        "con ✏ Editar si hace falta."
    )

    return redirect(url_for("knowledge"))


# ============================================================
# COPIA DE SEGURIDAD DE LA KNOWLEDGE BASE
# ============================================================

@app.route("/knowledge/backup")
@solo_admin
def knowledge_backup():
    """Descarga un ZIP con todos los PDFs, lo procesado y las conversaciones."""

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)

    tmp.close()

    with LOCK:

        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:

            if os.path.exists(METADATA_FILE):
                z.write(METADATA_FILE, "documents.json")

            for carpeta, _, archivos in os.walk(DOCUMENTS_DIR):
                for nombre in archivos:
                    ruta = os.path.join(carpeta, nombre)
                    z.write(ruta, os.path.join(
                        "documents", os.path.relpath(ruta, DOCUMENTS_DIR)
                    ))

            for carpeta, _, archivos in os.walk(IMAGES_DIR):
                for nombre in archivos:
                    ruta = os.path.join(carpeta, nombre)
                    z.write(ruta, os.path.join(
                        "images", os.path.relpath(ruta, IMAGES_DIR)
                    ))

            # Copia consistente de la base de conversaciones
            try:

                copia_db = tmp.name + ".db"

                origen = sqlite3.connect(conversation_service.DB_PATH)
                destino = sqlite3.connect(copia_db)

                with destino:
                    origen.backup(destino)

                origen.close()
                destino.close()

                z.write(copia_db, "conversaciones.db")

                os.remove(copia_db)

            except Exception as e:
                print(f"No se pudo incluir conversaciones.db en el backup: {e}")

    fecha = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")

    return send_file(
        tmp.name,
        as_attachment=True,
        download_name=f"kb-backup-{fecha}.zip",
        mimetype="application/zip"
    )


@app.route("/knowledge/restore", methods=["POST"])
@solo_admin
def knowledge_restore():
    """Restaura PDFs y documentos desde un ZIP generado por /knowledge/backup."""

    archivo = request.files.get("backup")

    if not archivo or not archivo.filename.lower().endswith(".zip"):

        flash("Seleccioná el archivo ZIP de la copia de seguridad.")

        return redirect(url_for("knowledge"))

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)

    tmp.close()

    archivo.save(tmp.name)

    try:

        with zipfile.ZipFile(tmp.name) as z:

            nombres = z.namelist()

            if "documents.json" not in nombres:
                raise ValueError("el ZIP no contiene documents.json")

            with LOCK:

                restaurados = 0

                for nombre in nombres:

                    if nombre.endswith("/"):
                        continue

                    if nombre == "documents.json":
                        destino = METADATA_FILE
                    elif nombre.startswith("documents/"):
                        destino = os.path.join(DOCUMENTS_DIR, nombre[len("documents/"):])
                    elif nombre.startswith("images/"):
                        destino = os.path.join(IMAGES_DIR, nombre[len("images/"):])
                    else:
                        # conversaciones.db y otros archivos no se restauran
                        continue

                    destino = os.path.abspath(destino)

                    base = os.path.abspath(KNOWLEDGE_DIR)

                    # Protección contra rutas maliciosas dentro del ZIP
                    if not destino.startswith(base + os.sep):
                        continue

                    os.makedirs(os.path.dirname(destino), exist_ok=True)

                    with z.open(nombre) as origen, open(destino, "wb") as out:
                        shutil.copyfileobj(origen, out)

                    restaurados += 1

        # Las rutas guardadas pueden apuntar a otra carpeta: se corrigen
        with LOCK:

            documentos = cargar_documentos()

            for doc in documentos:

                real = resolver_ruta_pdf(doc)

                if real:
                    doc["ruta_pdf"] = real.replace("\\", "/")

            guardar_documentos(documentos)

        flash(f"Copia restaurada: {restaurados} archivos.")

    except Exception as e:

        flash(f"No se pudo restaurar la copia: {e}")

    finally:

        os.remove(tmp.name)

    return redirect(url_for("knowledge"))


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

    # Público (lo usan los controles de Render): solo estado y versión.
    # El detalle de la configuración se muestra únicamente con sesión.
    basico = {
        "status": "ok",
        "service": "Zoo Logic AI Agents",
        "version": APP_VERSION,
    }

    if not getattr(g, "usuario", None):
        return basico

    basico.update({
        "openai_configurado": bool(client),
        "proveedor": (
            "simulado" if MODO_SIMULADO
            else "azure" if USA_AZURE
            else ("openai" if client else "sin configurar")
        ),
        "modelo_agente": MODELO_AGENTE,
        "ucontact_configurado": bool(UCONTACT_API_KEY),
        "login_configurado": usuarios_service.cantidad() > 0,
        "conexiones_cifrado_ok": crypto_service.disponible(),
        "conexiones_clave_origen": crypto_service.origen_de_la_clave(),
        "agente_pausado": agente_pausado(),
        "agentes_activos": sum(1 for x in agentes_service.listar() if x["activo"]),
        "sync_intervalo_min": SYNC_INTERVALO_MIN,
        "whatsapp_incluye_pendientes": WHATSAPP_INCLUIR_PENDIENTES,
        "productos": agent_service.PRODUCTOS,
        "kb_en_disco_persistente": kb_en_disco_persistente(),
        "documentos_en_kb": len(cargar_documentos()),
        "conversaciones_db": conversation_service.DB_PATH,
        "knowledge_dir": KNOWLEDGE_DIR
    })

    return basico


@app.route("/debug/routes")
@solo_admin
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

    flash("La subida supera el máximo permitido (100 MB en total por envío).")

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
