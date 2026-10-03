"""
Adjuntos del cliente (audios y fotos) y capturas en las respuestas.

Entrada: uContact manda la URL del audio o de la imagen que mandó el cliente.
Se descargan de forma segura, el audio se transcribe y la imagen se lee con
visión; el texto resultante se suma al mensaje del cliente.

Salida: el agente puede responder con capturas de los manuales. Se publican
con enlaces firmados y con vencimiento (/media/<token>), para que uContact
los descargue sin iniciar sesión.

Qué soporta uContact (recibir/enviar archivos por WhatsApp) hay que
confirmarlo en su configuración: esta parte deja todo listo de nuestro lado.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import time
from urllib.parse import urljoin, urlparse

import requests

MAX_BYTES = 20 * 1024 * 1024
MAX_ADJUNTOS = 3


class ErrorAdjunto(Exception):
    """Mensaje pensado para el log (no se le muestra al cliente)."""


# ---------------- descarga segura ----------------

def _validar_destino(url, resolver=None):

    partes = urlparse(url)

    if partes.scheme != "https" or not partes.hostname:
        raise ErrorAdjunto("la URL del adjunto tiene que ser https")

    try:
        infos = (resolver or socket.getaddrinfo)(partes.hostname, partes.port or 443)
    except OSError:
        raise ErrorAdjunto("no se pudo resolver el dominio del adjunto")

    for info in infos:

        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ErrorAdjunto("la URL del adjunto apunta a una red interna")


def _cabeceras():

    texto = os.environ.get("UCONTACT_MEDIA_AUTH", "").strip()

    if ":" in texto:
        nombre, valor = texto.split(":", 1)
        return {nombre.strip(): valor.strip()}

    return {}


def descargar(url, http=None, resolver=None, max_bytes=MAX_BYTES):

    http = http or requests

    destino = url

    for _ in range(4):

        _validar_destino(destino, resolver)

        r = http.get(
            destino, headers=_cabeceras(), stream=True, timeout=15, allow_redirects=False
        )

        if r.status_code in (301, 302, 303, 307, 308):
            destino = urljoin(destino, r.headers.get("Location", ""))
            continue

        if r.status_code != 200:
            raise ErrorAdjunto(f"el adjunto no se pudo descargar ({r.status_code})")

        datos = bytearray()

        for trozo in r.iter_content(chunk_size=65536):

            datos += trozo

            if len(datos) > max_bytes:
                raise ErrorAdjunto("el adjunto es demasiado grande")

        return bytes(datos)

    raise ErrorAdjunto("demasiadas redirecciones")


def detectar_tipo(d):
    """('imagen', mime, ext) | ('audio', mime, ext) | None, según los primeros bytes."""

    if d[:8] == b"\x89PNG\r\n\x1a\n":
        return ("imagen", "image/png", "png")
    if d[:3] == b"\xff\xd8\xff":
        return ("imagen", "image/jpeg", "jpg")
    if d[:4] == b"RIFF" and d[8:12] == b"WEBP":
        return ("imagen", "image/webp", "webp")
    if d[:4] == b"RIFF" and d[8:12] == b"WAVE":
        return ("audio", "audio/wav", "wav")
    if d[:4] == b"OggS":
        return ("audio", "audio/ogg", "ogg")
    if d[:3] == b"ID3" or d[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ("audio", "audio/mpeg", "mp3")
    if d[4:8] == b"ftyp":
        return ("audio", "audio/mp4", "m4a")
    if d[:4] == b"\x1aE\xdf\xa3":
        return ("audio", "audio/webm", "webm")

    return None


# ---------------- IA ----------------

PROMPT_IMAGEN = (
    "Esta imagen la mandó un cliente de soporte técnico de un sistema de gestión. "
    "Transcribí textualmente cualquier mensaje de error o texto relevante que se "
    "vea y describí en pocas líneas qué pantalla o situación se ve. No transcribas "
    "datos personales sensibles (tarjetas, contraseñas). Respondé en español."
)


def transcribir(client, modelo, datos, ext):

    r = client.audio.transcriptions.create(
        model=modelo, file=(f"audio.{ext}", datos), language="es"
    )

    return (getattr(r, "text", "") or "").strip()


def leer_imagen(client, modelo, datos, mime):

    url = f"data:{mime};base64,{base64.b64encode(datos).decode()}"

    r = client.chat.completions.create(
        model=modelo,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT_IMAGEN},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }],
    )

    return (r.choices[0].message.content or "").strip()


def procesar(adjuntos, client, modelo_transcripcion, modelo_vision,
             http=None, resolver=None):
    """
    adjuntos: [{"tipo": "audio"|"imagen"|..., "url": "https://..."}]
    Devuelve (textos_para_el_mensaje, errores).
    """

    textos, errores = [], []

    for a in (adjuntos or [])[:MAX_ADJUNTOS]:

        try:

            if client is None:
                raise ErrorAdjunto("la IA no está configurada")

            datos = descargar(str(a.get("url") or ""), http, resolver)

            tipo = detectar_tipo(datos)

            if not tipo:
                raise ErrorAdjunto("formato de adjunto no soportado")

            if tipo[0] == "audio":

                texto = transcribir(client, modelo_transcripcion, datos, tipo[2])

                if not texto:
                    raise ErrorAdjunto("el audio no tiene voz reconocible")

                textos.append(f"[Audio del cliente, transcripto]: {texto}")

            else:

                texto = leer_imagen(client, modelo_vision, datos, tipo[1])

                if not texto:
                    raise ErrorAdjunto("no se pudo leer la imagen")

                textos.append(f"[Imagen que mandó el cliente]: {texto}")

        except ErrorAdjunto as e:
            errores.append(str(e))
        except Exception as e:
            errores.append(f"error al procesar el adjunto: {str(e)[:100]}")

    return textos, errores


def adjuntos_del_payload(datos):
    """Lee los adjuntos de lo que manda uContact (varias formas posibles)."""

    adjuntos = []

    for a in datos.get("adjuntos") or datos.get("attachments") or []:

        if isinstance(a, dict) and (a.get("url") or a.get("link")):
            adjuntos.append({"tipo": a.get("tipo") or a.get("type") or "", "url": a.get("url") or a.get("link")})

    for clave, tipo in (("audio_url", "audio"), ("imagen_url", "imagen"), ("image_url", "imagen"), ("media_url", "")):

        if isinstance(datos.get(clave), str) and datos[clave].strip():
            adjuntos.append({"tipo": tipo, "url": datos[clave].strip()})

    return adjuntos


# ---------------- capturas en las respuestas ----------------

def _secreto():

    base = os.environ.get("FLASK_SECRET_KEY", "").strip()

    if base and base != "zoo-logic-ai-agents":
        return base.encode()

    from services import crypto_service

    return (crypto_service._clave_de_archivo() or "sin-clave").encode()


def firmar(ruta_relativa, vence_seg=86400, ahora=None):

    cuerpo = base64.urlsafe_b64encode(json.dumps(
        {"p": ruta_relativa, "e": int((ahora or time.time()) + vence_seg)}
    ).encode()).decode().rstrip("=")

    firma = hmac.new(_secreto(), cuerpo.encode(), hashlib.sha256).hexdigest()[:32]

    return f"{cuerpo}.{firma}"


def verificar(token, ahora=None):
    """Devuelve la ruta relativa si el enlace es válido y no venció; si no, None."""

    try:
        cuerpo, firma = token.rsplit(".", 1)

        esperada = hmac.new(_secreto(), cuerpo.encode(), hashlib.sha256).hexdigest()[:32]

        if not hmac.compare_digest(firma, esperada):
            return None

        datos = json.loads(base64.urlsafe_b64decode(cuerpo + "=" * (-len(cuerpo) % 4)))

        if datos["e"] < (ahora or time.time()):
            return None

        return str(datos["p"])

    except Exception:
        return None


def imagenes_para_respuesta(resultado, documentos, maximo=2):
    """
    Capturas de los manuales que acompañan la respuesta: las imágenes de las
    páginas de los fragmentos que el agente USÓ. Excluye páginas escaneadas
    completas (salvo que no haya otra). Devuelve [{ruta, descripcion, documento}].
    """

    usados = set(resultado.get("fragmentos_usados") or [])

    if not usados:
        return []

    por_doc = {d.get("id"): d for d in documentos}

    paginas = []

    for f in resultado.get("fragmentos") or []:

        if f.get("id") in usados and f.get("pagina") is not None:
            paginas.append((f.get("doc_id"), f.get("pagina")))

    capturas, escaneadas = [], []

    for doc_id, pagina in paginas:

        doc = por_doc.get(doc_id) or {}

        for img in doc.get("imagenes") or []:

            if img.get("pagina") != pagina or not img.get("archivo"):
                continue

            if str(img.get("descripcion") or "").startswith("No se pudo"):
                continue

            item = {
                "ruta": f"{doc_id}/{img['archivo']}",
                "descripcion": str(img.get("descripcion") or "")[:200],
                "documento": doc.get("nombre") or doc.get("archivo") or "",
            }

            (escaneadas if img.get("tipo") == "pagina_escaneada" else capturas).append(item)

    vistos, salida = set(), []

    for item in capturas + escaneadas:

        if item["ruta"] not in vistos:
            vistos.add(item["ruta"])
            salida.append(item)

    return salida[:maximo]


# alias con el nombre que usa la aplicación
firma_token = firmar
