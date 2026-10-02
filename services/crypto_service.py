"""
Cifrado de las credenciales de las conexiones (Drive, SharePoint, OneDrive).

Las credenciales se guardan cifradas en la base de datos. La clave sale de la
variable de entorno CONEXIONES_CLAVE o, si no está, de FLASK_SECRET_KEY. Esa
variable tiene que ser FIJA: si cambia, las credenciales guardadas ya no se
pueden leer y hay que volver a cargarlas.
"""

import base64
import hashlib
import json
import os

from cryptography.fernet import Fernet, InvalidToken


def _clave():

    base = (
        os.environ.get("CONEXIONES_CLAVE", "").strip()
        or os.environ.get("FLASK_SECRET_KEY", "").strip()
    )

    if not base or base == "zoo-logic-ai-agents":
        return None

    return base64.urlsafe_b64encode(hashlib.sha256(base.encode("utf-8")).digest())


def disponible():
    """True si hay una clave fija para cifrar."""

    return _clave() is not None


def cifrar(datos):

    clave = _clave()

    if not clave:
        raise RuntimeError(
            "Falta definir FLASK_SECRET_KEY (o CONEXIONES_CLAVE) en el "
            "servidor para poder guardar credenciales."
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
