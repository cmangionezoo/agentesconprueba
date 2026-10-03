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
import sqlite3
import threading
from datetime import datetime, timezone

from services import conversation_service


_LOCK = threading.RLock()

# Colas por defecto: si no se configura nada, la cola se llama igual que el
# destino. Se pueden definir desde Render con UCONTACT_COLAS (JSON), por
# ejemplo:
# {"Dragonfish": {"L2": "Dragonfish - L2", "Ecommerce": "Dragonfish - Ecommerce",
#                 "Desarrollo": "Dragonfish - Desarrollo", "MDA": "Dragonfish - MDA"},
#  "default": {"L2": "Soporte L2"}}
try:
    COLAS = json.loads(os.environ.get("UCONTACT_COLAS", "") or "{}")
    if not isinstance(COLAS, dict):
        COLAS = {}
except ValueError:
    COLAS = {}


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
# COLAS DE DERIVACIÓN
# ============================================================

def resolver_cola(producto, destino):
    """Nombre de la cola de WhatsApp a la que uContact debe transferir."""

    if not destino:
        return ""

    for clave in (producto, "default"):

        cola = (COLAS.get(clave) or {}).get(destino)

        if cola:
            return str(cola)

    return destino
