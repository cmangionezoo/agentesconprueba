"""
Cifrado de las credenciales de las conexiones (Drive, SharePoint, OneDrive).

Las credenciales se guardan cifradas en la base de datos. La clave sale, en
este orden, de:

1. la variable de entorno CONEXIONES_CLAVE,
2. la variable de entorno FLASK_SECRET_KEY,
3. un archivo con una clave al azar que se crea solo junto a la base de datos
   (carpeta de la Knowledge Base). Así funciona aunque no se haya definido
   ninguna variable. Si esa carpeta no es un disco persistente, el archivo se
   pierde en cada deploy y hay que volver a cargar las credenciales.

Lo recomendable es definir FLASK_SECRET_KEY (fija) en el servidor. Si la clave
cambia, las credenciales guardadas ya no se pueden leer y hay que cargarlas de
nuevo.
"""

import base64
import hashlib
import json
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

from services import conversation_service

NOMBRE_ARCHIVO_CLAVE = ".clave_conexiones"


def _clave_de_archivo():
    """Clave guardada en disco; si no existe, se crea. None si no se puede."""

    try:

        carpeta = os.path.dirname(conversation_service.DB_PATH)

        ruta = os.path.join(carpeta, NOMBRE_ARCHIVO_CLAVE)

        if os.path.exists(ruta):

            with open(ruta, "r", encoding="utf-8") as f:
                return f.read().strip() or None

        valor = secrets.token_urlsafe(48)

        # "x": falla si otro proceso la creó justo antes
        with open(ruta, "x", encoding="utf-8") as f:
            f.write(valor)

        try:
            os.chmod(ruta, 0o600)
        except OSError:
            pass

        return valor

    except FileExistsError:

        with open(ruta, "r", encoding="utf-8") as f:
            return f.read().strip() or None

    except (OSError, TypeError):
        return None


def origen_de_la_clave():
    """'variable' o 'archivo' (o '' si no hay clave); para mostrar en /health."""

    for nombre in ("CONEXIONES_CLAVE", "FLASK_SECRET_KEY"):

        valor = os.environ.get(nombre, "").strip()

        if valor and valor != "zoo-logic-ai-agents":
            return "variable"

    return "archivo" if _clave_de_archivo() else ""


def _clave():

    base = (
        os.environ.get("CONEXIONES_CLAVE", "").strip()
        or os.environ.get("FLASK_SECRET_KEY", "").strip()
    )

    if not base or base == "zoo-logic-ai-agents":
        base = _clave_de_archivo()

    if not base:
        return None

    return base64.urlsafe_b64encode(hashlib.sha256(base.encode("utf-8")).digest())


def disponible():
    """True si hay una clave fija para cifrar."""

    return _clave() is not None


def cifrar(datos):

    clave = _clave()

    if not clave:
        raise RuntimeError(
            "No se pudo obtener una clave para cifrar las credenciales. "
            "Definí FLASK_SECRET_KEY en el servidor."
        )

    return Fernet(clave).encrypt(
        json.dumps(datos, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def descifrar(texto):
    """Devuelve el dict original, o None si no se puede leer."""

    clave = _clave()

    if not clave or not texto:
        return None

    try:
        return json.loads(Fernet(clave).decrypt(texto.encode("ascii")))
    except (InvalidToken, ValueError):
        return None
