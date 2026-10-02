"""
Lectura de carpetas en la nube: Google Drive, SharePoint y OneDrive.

- Google Drive: con una cuenta de servicio (JSON) a la que se le compartió
  la carpeta, o con una API key si la carpeta es pública ("cualquiera con el
  enlace").
- SharePoint / OneDrive: con Microsoft Graph y una aplicación registrada en
  Entra ID (tenant, client id y client secret) con permisos de solo lectura.

Todo es solo lectura. Las llamadas HTTP van por `requests` y se pueden
reemplazar (parámetro `http`) para probar sin red.
"""

import base64
import json
import re
import time
from urllib.parse import urlparse

import jwt
import requests


TIPOS = {
    "google_drive": "Google Drive",
    "sharepoint": "SharePoint",
    "onedrive": "OneDrive (empresa)",
}

TIMEOUT = 30
MAX_PROFUNDIDAD = 6
MAX_ARCHIVOS = 500

MIME_PDF = "application/pdf"


class ErrorProveedor(Exception):
    """Error con un mensaje pensado para mostrarle al usuario."""


class Remoto:
    """Un archivo de la carpeta remota."""

    def __init__(self, id, nombre, ruta, firma, tamano=0, extra=None):
        self.id = id                  # id en el proveedor
        self.nombre = nombre          # nombre original (puede no terminar en .pdf)
        self.ruta = ruta              # subcarpetas, ej. "Dragonfish/Facturación"
        self.firma = firma            # cambia cuando el archivo cambia
        self.tamano = tamano
        self.extra = extra or {}


# ============================================================
# VALIDACIONES
# ============================================================

_ID_DRIVE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def extraer_id_carpeta_google(url):

    url = (url or "").strip()

    if _ID_DRIVE.match(url):
        return url

    coincidencia = re.search(r"/folders/([A-Za-z0-9_-]+)", url) \
        or re.search(r"[?&]id=([A-Za-z0-9_-]+)", url)

    return coincidencia.group(1) if coincidencia else ""


def validar_url(tipo, url):
    """Devuelve un mensaje de error, o '' si la URL sirve para ese tipo."""

    url = (url or "").strip()

    if tipo not in TIPOS:
        return "Tipo de conexión inválido."

    if tipo == "google_drive":

        if not extraer_id_carpeta_google(url):
            return (
                "No se pudo leer el ID de la carpeta. Usá el enlace de la "
                "carpeta de Drive (termina en /folders/...)."
            )

        if not _ID_DRIVE.match(url):

            host = (urlparse(url).hostname or "").lower()

            if urlparse(url).scheme != "https" or host not in (
                "drive.google.com", "docs.google.com"
            ):
                return "La URL tiene que ser de drive.google.com (https)."

        return ""

    partes = urlparse(url)

    host = (partes.hostname or "").lower()

    if partes.scheme != "https" or not host:
        return "La URL tiene que empezar con https://."

    permitido = host.endswith(".sharepoint.com") or host in (
        "1drv.ms", "onedrive.live.com"
    )

    if not permitido:
        return (
            "La URL tiene que ser de SharePoint u OneDrive de la empresa "
            "(termina en .sharepoint.com)."
        )

    if host == "onedrive.live.com":
        return (
            "OneDrive personal no se puede conectar así: solo OneDrive de "
            "empresa o SharePoint."
        )

    return ""


def secreto_desde_form(tipo, form, archivos=None):
    """
    Arma las credenciales a partir del formulario.
    Devuelve (secreto | None, error). None sin error = no se cargó nada
    (al editar, significa 'conservar las actuales').
    """

    archivos = archivos or {}

    if tipo == "google_drive":

        modo = (form.get("g_modo") or "cuenta_servicio").strip()

        if modo == "api_key":

            clave = (form.get("g_api_key") or "").strip()

            if not clave:
                return None, ""

            if not re.match(r"^[A-Za-z0-9_\-]{20,}$", clave):
                return None, "La API key no tiene un formato válido."

            return {"modo": "api_key", "api_key": clave}, ""

        texto = (form.get("g_json") or "").strip()

        archivo = archivos.get("g_json_archivo")

        if archivo and archivo.filename:
            texto = archivo.read().decode("utf-8", errors="replace").strip()

        if not texto:
            return None, ""

        try:
            info = json.loads(texto)
        except ValueError:
            return None, "El JSON de la cuenta de servicio no es válido."

        if (
            not isinstance(info, dict)
            or info.get("type") != "service_account"
            or not info.get("client_email")
            or not info.get("private_key")
        ):
            return None, (
                "Ese JSON no parece una clave de cuenta de servicio "
                "(tiene que traer client_email y private_key)."
            )

        return {
            "modo": "cuenta_servicio",
            "json": {
                "type": "service_account",
                "client_email": info["client_email"],
                "private_key": info["private_key"],
            },
        }, ""

    tenant = (form.get("ms_tenant") or "").strip()
    cliente = (form.get("ms_client_id") or "").strip()
    secreto = (form.get("ms_client_secret") or "").strip()

    if not (tenant or cliente or secreto):
        return None, ""

    if not (tenant and cliente and secreto):
        return None, "Completá Tenant ID, Client ID y Client Secret."

    if not re.match(r"^[A-Za-z0-9.\-]{3,100}$", tenant):
        return None, "El Tenant ID tiene caracteres inválidos."

    if not re.match(r"^[A-Za-z0-9\-]{10,100}$", cliente):
        return None, "El Client ID tiene caracteres inválidos."

    return {
        "tenant_id": tenant, "client_id": cliente, "client_secret": secreto
    }, ""


# ============================================================
# UTILIDADES
# ============================================================

def _json(respuesta):

    try:
        return respuesta.json()
    except ValueError:
        return {}


def _guardar_stream(respuesta, destino, max_bytes):
    """Escribe la descarga en disco con tope de tamaño y verifica que sea PDF."""

    total = 0

    with open(destino, "wb") as salida:

        for trozo in respuesta.iter_content(chunk_size=1024 * 256):

            if not trozo:
                continue

            total += len(trozo)

            if total > max_bytes:
                salida.close()
                _borrar(destino)
                raise ErrorProveedor(
                    f"El archivo supera el máximo de {max_bytes // (1024*1024)} MB."
                )

            salida.write(trozo)

    with open(destino, "rb") as f:
        cabecera = f.read(5)

    if cabecera != b"%PDF-":
        _borrar(destino)
        raise ErrorProveedor("El archivo descargado no es un PDF válido.")


def _borrar(ruta):

    import os

    try:
        os.remove(ruta)
    except OSError:
        pass


# ============================================================
# GOOGLE DRIVE
# ============================================================

class GoogleDrive:

    API = "https://www.googleapis.com/drive/v3"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    ALCANCE = "https://www.googleapis.com/auth/drive.readonly"

    MIME_CARPETA = "application/vnd.google-apps.folder"

    # Documentos de Google que se exportan a PDF
    MIME_EXPORTABLES = (
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.presentation",
    )

    def __init__(self, url, secreto, http=None):

        self.http = http or requests.Session()
        self.secreto = secreto or {}
        self.carpeta_id = extraer_id_carpeta_google(url)
        self._token = None
        self._vence = 0

    # -- autenticación

    def _obtener_token(self):

        if self._token and time.time() < self._vence - 60:
            return self._token

        info = self.secreto.get("json") or {}

        ahora = int(time.time())

        try:
            asercion = jwt.encode(
                {
                    "iss": info["client_email"],
                    "scope": self.ALCANCE,
                    "aud": self.TOKEN_URL,
                    "iat": ahora,
                    "exp": ahora + 3600,
                },
                info["private_key"],
                algorithm="RS256"
            )
        except Exception:
            raise ErrorProveedor(
                "La clave de la cuenta de servicio no es válida. Volvé a "
                "cargar el JSON completo."
            )

        r = self.http.post(
            self.TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": asercion,
            },
            timeout=TIMEOUT
        )

        if r.status_code != 200:
            detalle = _json(r).get("error_description") or _json(r).get("error") or r.status_code
            raise ErrorProveedor(f"Google rechazó las credenciales ({detalle}).")

        datos = _json(r)

        self._token = datos.get("access_token")
        self._vence = time.time() + int(datos.get("expires_in", 3600))

        return self._token

    def _auth(self):

        if self.secreto.get("modo") == "api_key":
            return {}, {"key": self.secreto.get("api_key", "")}

        return {"Authorization": f"Bearer {self._obtener_token()}"}, {}

    def _get(self, url, params=None, stream=False):

        cabeceras, extra = self._auth()

        parametros = dict(params or {})
        parametros.update(extra)

        r = self.http.get(
            url, headers=cabeceras, params=parametros,
            stream=stream, timeout=TIMEOUT
        )

        if r.status_code in (200, 206):
            return r

        mensaje = (_json(r).get("error") or {})

        motivo = mensaje.get("message") if isinstance(mensaje, dict) else ""

        if r.status_code == 404:
            raise ErrorProveedor(
                "No se encontró la carpeta o la cuenta no tiene acceso. "
                "Compartila (como lector) con el mail de la cuenta de servicio."
            )

        if r.status_code in (401, 403):
            raise ErrorProveedor(
                "Google denegó el acceso" + (f": {motivo}" if motivo else ".")
                + " Revisá que la Drive API esté habilitada y que la carpeta "
                "esté compartida con la cuenta."
            )

        raise ErrorProveedor(f"Error de Google Drive ({r.status_code}). {motivo or ''}".strip())

    # -- operaciones

    def probar(self):
        """(nombre_de_la_carpeta, cantidad_de_archivos)."""

        r = self._get(
            f"{self.API}/files/{self.carpeta_id}",
            {"fields": "id,name,mimeType", "supportsAllDrives": "true"}
        )

        info = _json(r)

        if info.get("mimeType") != self.MIME_CARPETA:
            raise ErrorProveedor("Ese enlace no es una carpeta de Drive.")

        return info.get("name", ""), len(self.listar())

    def listar(self):

        resultado = []

        pendientes = [(self.carpeta_id, [])]

        while pendientes:

            carpeta, ruta = pendientes.pop(0)

            if len(ruta) > MAX_PROFUNDIDAD:
                continue

            pagina = None

            while True:

                params = {
                    "q": f"'{carpeta}' in parents and trashed = false",
                    "fields": (
                        "nextPageToken, files(id,name,mimeType,"
                        "modifiedTime,size,md5Checksum)"
                    ),
                    "pageSize": 1000,
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                }

                if pagina:
                    params["pageToken"] = pagina

                datos = _json(self._get(f"{self.API}/files", params))

                for f in datos.get("files", []):

                    mime = f.get("mimeType", "")
                    nombre = f.get("name", "")

                    if mime == self.MIME_CARPETA:
                        pendientes.append((f["id"], ruta + [nombre]))
                        continue

                    exportar = mime in self.MIME_EXPORTABLES

                    es_pdf = mime == MIME_PDF or nombre.lower().endswith(".pdf")

                    if not (es_pdf or exportar):
                        continue

                    resultado.append(Remoto(
                        id=f["id"],
                        nombre=nombre + (".pdf" if exportar else ""),
                        ruta="/".join(ruta),
                        firma=(f.get("md5Checksum") or f.get("modifiedTime") or ""),
                        tamano=int(f.get("size") or 0),
                        extra={"exportar": exportar}
                    ))

                    if len(resultado) > MAX_ARCHIVOS:
                        raise ErrorProveedor(
                            f"La carpeta tiene más de {MAX_ARCHIVOS} archivos. "
                            "Conectá una subcarpeta más chica."
                        )

                pagina = datos.get("nextPageToken")

                if not pagina:
                    break

        return resultado

    def descargar(self, remoto, destino, max_bytes):

        if remoto.extra.get("exportar"):

            r = self._get(
                f"{self.API}/files/{remoto.id}/export",
                {"mimeType": MIME_PDF}, stream=True
            )

        else:

            if remoto.tamano and remoto.tamano > max_bytes:
                raise ErrorProveedor(
                    f"El archivo supera el máximo de {max_bytes // (1024*1024)} MB."
                )

            r = self._get(
                f"{self.API}/files/{remoto.id}",
                {"alt": "media", "supportsAllDrives": "true"}, stream=True
            )

        _guardar_stream(r, destino, max_bytes)


# ============================================================
# SHAREPOINT / ONEDRIVE (Microsoft Graph)
# ============================================================

class MicrosoftGraph:

    GRAPH = "https://graph.microsoft.com/v1.0"

    EXTENSIONES_CONVERTIBLES = (".docx", ".pptx")

    def __init__(self, url, secreto, http=None):

        self.http = http or requests.Session()
        self.url = (url or "").strip()
        self.secreto = secreto or {}
        self._token = None
        self._vence = 0
        self._raiz = None

    def _obtener_token(self):

        if self._token and time.time() < self._vence - 60:
            return self._token

        tenant = self.secreto.get("tenant_id", "")

        r = self.http.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.secreto.get("client_id", ""),
                "client_secret": self.secreto.get("client_secret", ""),
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=TIMEOUT
        )

        if r.status_code != 200:
            detalle = _json(r).get("error_description", "") or _json(r).get("error", "")
            raise ErrorProveedor(
                "Microsoft rechazó las credenciales. Revisá Tenant ID, "
                "Client ID y el Client Secret (que no esté vencido). "
                + (detalle.split("\n")[0][:160] if detalle else "")
            )

        datos = _json(r)

        self._token = datos.get("access_token")
        self._vence = time.time() + int(datos.get("expires_in", 3600))

        return self._token

    def _cabeceras(self):
        return {"Authorization": f"Bearer {self._obtener_token()}"}

    def _get(self, url, params=None, stream=False, con_auth=True):

        if con_auth and not url.startswith(self.GRAPH):
            raise ErrorProveedor("Respuesta inesperada de Microsoft Graph.")

        r = self.http.get(
            url,
            headers=self._cabeceras() if con_auth else {},
            params=params, stream=stream, timeout=TIMEOUT
        )

        if r.status_code in (200, 206):
            return r

        if r.status_code == 401:
            raise ErrorProveedor("Microsoft denegó el acceso: credenciales inválidas o vencidas.")

        if r.status_code == 403:
            raise ErrorProveedor(
                "La aplicación no tiene permiso sobre esa carpeta. Hace falta "
                "el permiso de aplicación Files.Read.All (o Sites.Read.All) "
                "con consentimiento del administrador."
            )

        if r.status_code == 404:
            raise ErrorProveedor(
                "No se encontró la carpeta. Revisá el enlace y que sea de "
                "SharePoint / OneDrive de la empresa."
            )

        raise ErrorProveedor(f"Error de Microsoft Graph ({r.status_code}).")

    def _resolver_raiz(self):

        if self._raiz:
            return self._raiz

        codigo = base64.urlsafe_b64encode(self.url.encode("utf-8")).decode().rstrip("=")

        r = self._get(
            f"{self.GRAPH}/shares/u!{codigo}/driveItem",
            {"$select": "id,name,folder,parentReference"}
        )

        item = _json(r)

        if "folder" not in item:
            raise ErrorProveedor("Ese enlace no es una carpeta.")

        drive_id = (item.get("parentReference") or {}).get("driveId")

        if not drive_id:
            raise ErrorProveedor("No se pudo identificar la biblioteca de documentos.")

        self._raiz = (drive_id, item["id"], item.get("name", ""))

        return self._raiz

    def probar(self):

        _, _, nombre = self._resolver_raiz()

        return nombre, len(self.listar())

    def listar(self):

        drive_id, raiz_id, _ = self._resolver_raiz()

        resultado = []

        pendientes = [(raiz_id, [])]

        while pendientes:

            carpeta, ruta = pendientes.pop(0)

            if len(ruta) > MAX_PROFUNDIDAD:
                continue

            url = f"{self.GRAPH}/drives/{drive_id}/items/{carpeta}/children"

            params = {
                "$top": 200,
                "$select": (
                    "id,name,size,file,folder,eTag,cTag,"
                    "lastModifiedDateTime,@microsoft.graph.downloadUrl"
                ),
            }

            while url:

                datos = _json(self._get(url, params))

                params = None   # el nextLink ya trae sus parámetros

                for f in datos.get("value", []):

                    nombre = f.get("name", "")

                    if "folder" in f:
                        pendientes.append((f["id"], ruta + [nombre]))
                        continue

                    minuscula = nombre.lower()

                    es_pdf = minuscula.endswith(".pdf")

                    convertir = minuscula.endswith(self.EXTENSIONES_CONVERTIBLES)

                    if not (es_pdf or convertir):
                        continue

                    resultado.append(Remoto(
                        id=f["id"],
                        # "Guia.docx" -> "Guia.pdf"
                        nombre=(
                            nombre.rsplit(".", 1)[0] + ".pdf"
                            if convertir else nombre
                        ),
                        ruta="/".join(ruta),
                        firma=(
                            f.get("cTag") or f.get("eTag")
                            or f"{f.get('lastModifiedDateTime', '')}-{f.get('size', 0)}"
                        ),
                        tamano=int(f.get("size") or 0),
                        extra={
                            "drive_id": drive_id,
                            "convertir": convertir,
                            "download_url": f.get("@microsoft.graph.downloadUrl", ""),
                        }
                    ))

                    if len(resultado) > MAX_ARCHIVOS:
                        raise ErrorProveedor(
                            f"La carpeta tiene más de {MAX_ARCHIVOS} archivos. "
                            "Conectá una subcarpeta más chica."
                        )

                url = datos.get("@odata.nextLink")

        return resultado

    def descargar(self, remoto, destino, max_bytes):

        if remoto.tamano and remoto.tamano > max_bytes:
            raise ErrorProveedor(
                f"El archivo supera el máximo de {max_bytes // (1024*1024)} MB."
            )

        drive_id = remoto.extra.get("drive_id")

        contenido = f"{self.GRAPH}/drives/{drive_id}/items/{remoto.id}/content"

        if remoto.extra.get("convertir"):

            r = self._get(contenido, {"format": "pdf"}, stream=True)

        else:

            r = None

            enlace = remoto.extra.get("download_url")

            # El enlace de descarga ya viene autorizado (por eso sin token);
            # si venció, se pide de nuevo con la sesión de la aplicación.
            if enlace and enlace.startswith("https://"):

                try:
                    r = self._get(enlace, stream=True, con_auth=False)
                except ErrorProveedor:
                    r = None

            if r is None:
                r = self._get(contenido, stream=True)

        _guardar_stream(r, destino, max_bytes)


# ============================================================
# FÁBRICA
# ============================================================

def crear_proveedor(tipo, url, secreto, http=None):

    if tipo == "google_drive":
        return GoogleDrive(url, secreto, http)

    if tipo in ("sharepoint", "onedrive"):
        return MicrosoftGraph(url, secreto, http)

    raise ErrorProveedor("Tipo de conexión inválido.")
