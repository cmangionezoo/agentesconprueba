"""
Integración con uContact (Net2Phone) - canal WhatsApp.

uContact detecta el cliente y el producto; cuando el caso pasa a Mesa de
Ayuda llama al webhook de esta plataforma con cada mensaje del cliente.
Este módulo:

- vincula el id de conversación de uContact con la conversación interna
  (así toda la charla queda en la misma tabla que usan las métricas)
- recupera el historial y el estado del agente entre mensajes
- evita procesar dos veces el mismo mensaje si uContact reintenta
- traduce el destino de derivación del agente (L2, Ecommerce, Desarrollo,
  MDA) a la cola de WhatsApp que corresponde según el producto
"""

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone

from services import conversation_service


_LOCK = threading.RLock()

# Respaldo opcional por variable de entorno UCONTACT_COLAS (JSON). Solo se usa
# para lo que NO esté cargado en la pantalla Agentes > Campañas de derivación:
# {"Dragonfish": {"L2": "DF_L2"}, "default": {"MDA": "SOPORTE"}}
try:
    COLAS = json.loads(os.environ.get("UCONTACT_COLAS", "") or "{}")
    if not isinstance(COLAS, dict):
        COLAS = {}
except ValueError:
    COLAS = {}

# Campañas de uContact a las que se deriva cada producto (se pueden cambiar
# desde la pantalla). zNube comparte la campaña de Dragonfish. Pantera todavía
# no tiene campaña.
CAMPANIAS_INICIALES = {
    "Dragonfish": "DF_CONSULTAS",
    "zNube": "DF_CONSULTAS",
    "Lince": "LI_CONSULTAS",
}

PATRON_CAMPANIA = re.compile(r"^[A-Za-z0-9_.\- ]{1,60}$")


def _ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _conectar():
    conexion = sqlite3.connect(conversation_service.DB_PATH, timeout=15)
    conexion.row_factory = sqlite3.Row
    return conexion


def init_tablas():

    with _LOCK, _conectar() as db:

        db.executescript("""
        CREATE TABLE IF NOT EXISTS ucontact_sesiones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT NOT NULL,
            conversacion_id TEXT NOT NULL,
            cliente TEXT DEFAULT '',
            telefono TEXT DEFAULT '',
            producto TEXT DEFAULT '',
            creada TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ucontact_ext
            ON ucontact_sesiones (external_id, id);

        CREATE TABLE IF NOT EXISTS colas_derivacion (
            producto TEXT NOT NULL,
            destino TEXT NOT NULL DEFAULT '',
            campania TEXT NOT NULL,
            PRIMARY KEY (producto, destino)
        );

        CREATE TABLE IF NOT EXISTS ucontact_mensajes (
            external_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            respuesta_json TEXT NOT NULL,
            fecha TEXT NOT NULL,
            PRIMARY KEY (external_id, message_id)
        );
        """)


# ============================================================
# SESIONES
# ============================================================

def buscar_sesion(external_id):
    """
    Devuelve la última conversación interna vinculada a ese id de uContact
    (con su estado), o None si no hay ninguna.
    """

    with _conectar() as db:

        fila = db.execute(
            """
            SELECT s.conversacion_id, c.cerrada, c.derivada, c.destino,
                   c.motivo, c.etapa, c.producto, c.resumen_json
            FROM ucontact_sesiones s
            JOIN conversaciones c ON c.id = s.conversacion_id
            WHERE s.external_id = ?
            ORDER BY s.id DESC LIMIT 1
            """,
            (external_id,)
        ).fetchone()

    return dict(fila) if fila else None


def registrar_sesion(external_id, conversacion_id, cliente, telefono, producto):

    with _LOCK, _conectar() as db:

        db.execute(
            "INSERT INTO ucontact_sesiones "
            "(external_id, conversacion_id, cliente, telefono, producto, creada) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (external_id, conversacion_id, cliente, telefono, producto, _ahora())
        )


def cargar_contexto(conversacion_id, maximo=16):
    """Historial y estado del agente guardados de los turnos anteriores."""

    with _conectar() as db:

        conv = db.execute(
            "SELECT estado_json FROM conversaciones WHERE id = ?",
            (conversacion_id,)
        ).fetchone()

        filas = db.execute(
            "SELECT rol, texto FROM mensajes WHERE conversacion_id = ? "
            "ORDER BY orden DESC LIMIT ?",
            (conversacion_id, maximo)
        ).fetchall()

    estado = None

    if conv and conv["estado_json"]:
        try:
            estado = json.loads(conv["estado_json"])
        except ValueError:
            estado = None

    historial = [
        {"rol": f["rol"], "texto": f["texto"]} for f in reversed(filas)
    ]

    return historial, estado


def cerrar_conversacion(conversacion_id):
    """El cliente o uContact cerraron la conversación sin que el agente la cerrara."""

    with _LOCK, _conectar() as db:

        db.execute(
            "UPDATE conversaciones SET cerrada = COALESCE(cerrada, ?), "
            "actualizada = ? WHERE id = ?",
            (_ahora(), _ahora(), conversacion_id)
        )


# ============================================================
# IDEMPOTENCIA (reintentos de uContact)
# ============================================================

def respuesta_ya_procesada(external_id, message_id):

    if not message_id:
        return None

    with _conectar() as db:

        fila = db.execute(
            "SELECT respuesta_json FROM ucontact_mensajes "
            "WHERE external_id = ? AND message_id = ?",
            (external_id, message_id)
        ).fetchone()

    return json.loads(fila["respuesta_json"]) if fila else None


def guardar_respuesta(external_id, message_id, respuesta):

    if not message_id:
        return

    with _LOCK, _conectar() as db:

        db.execute(
            "INSERT OR REPLACE INTO ucontact_mensajes "
            "(external_id, message_id, respuesta_json, fecha) "
            "VALUES (?, ?, ?, ?)",
            (
                external_id, message_id,
                json.dumps(respuesta, ensure_ascii=False), _ahora()
            )
        )


# ============================================================
# CAMPAÑAS DE DERIVACIÓN
# ============================================================
# Cada producto deriva a una campaña de uContact (por ejemplo DF_CONSULTAS).
# Hay una campaña "por defecto" del producto (destino vacío) y, si hace falta,
# una distinta por destino del agente (L2, Ecommerce, Desarrollo, MDA).

def sembrar_campanias():
    """Carga las campañas iniciales una sola vez (después se editan en pantalla)."""

    if conversation_service.obtener_ajuste("campanias_sembradas_v1", "") == "1":
        return

    with _LOCK, _conectar() as db:

        for producto, campania in CAMPANIAS_INICIALES.items():
            db.execute(
                "INSERT OR IGNORE INTO colas_derivacion (producto, destino, campania) "
                "VALUES (?, '', ?)", (producto, campania)
            )

    conversation_service.guardar_ajuste("campanias_sembradas_v1", "1")


def campanias():
    """{producto: {"defecto": str, "destinos": {destino: campaña}}} de lo cargado."""

    datos = {}

    with _conectar() as db:

        for f in db.execute("SELECT producto, destino, campania FROM colas_derivacion"):

            d = datos.setdefault(f["producto"], {"defecto": "", "destinos": {}})

            if f["destino"]:
                d["destinos"][f["destino"]] = f["campania"]
            else:
                d["defecto"] = f["campania"]

    return datos


def guardar_campanias(producto, defecto, por_destino, productos, destinos_validos):
    """Guarda las campañas de un producto. Devuelve un mensaje de error, o ''."""

    if producto not in productos:
        return "Producto inválido."

    defecto = (defecto or "").strip()

    limpio = {}

    for destino, valor in (por_destino or {}).items():

        valor = (valor or "").strip()

        if valor and destino in destinos_validos:
            limpio[destino] = valor

    for nombre in [defecto, *limpio.values()]:

        if nombre and not PATRON_CAMPANIA.match(nombre):
            return (
                f"El nombre de campaña «{nombre[:40]}» no es válido: usá letras, "
                "números, guion, guion bajo o punto."
            )

    with _LOCK, _conectar() as db:

        db.execute("DELETE FROM colas_derivacion WHERE producto = ?", (producto,))

        if defecto:
            db.execute(
                "INSERT INTO colas_derivacion (producto, destino, campania) VALUES (?, '', ?)",
                (producto, defecto)
            )

        for destino, valor in limpio.items():
            db.execute(
                "INSERT INTO colas_derivacion (producto, destino, campania) VALUES (?, ?, ?)",
                (producto, destino, valor)
            )

    return ""


def resolver_cola(producto, destino):
    """
    Campaña de uContact a la que hay que transferir. Orden: la del destino en
    ese producto, la del producto, y por último el respaldo UCONTACT_COLAS.
    Devuelve '' si el producto no tiene campaña (por ejemplo, Pantera hoy).
    """

    if not destino:
        return ""

    cargadas = campanias().get(producto, {})

    if cargadas.get("destinos", {}).get(destino):
        return cargadas["destinos"][destino]

    if cargadas.get("defecto"):
        return cargadas["defecto"]

    for clave in (producto, "default"):

        cola = (COLAS.get(clave) or {}).get(destino)

        if cola:
            return str(cola)

    return ""
