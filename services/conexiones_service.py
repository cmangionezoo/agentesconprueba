"""
Conexiones a fuentes de documentos (Google Drive, SharePoint, OneDrive).

Cada conexión tiene su propia cuenta/credenciales (cifradas), la carpeta a
leer, el producto y las opciones de sincronización automática y tipificación
con IA. También guarda qué archivo remoto corresponde a qué documento de la
Knowledge Base, para no duplicar y detectar cambios.
"""

import sqlite3
import threading
from datetime import datetime, timezone

from services import conversation_service, crypto_service


_LOCK = threading.RLock()


def _ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _conectar():
    conexion = sqlite3.connect(conversation_service.DB_PATH, timeout=15)
    conexion.row_factory = sqlite3.Row
    return conexion


def init_tablas():

    with _LOCK, _conectar() as db:

        db.executescript("""
        CREATE TABLE IF NOT EXISTS conexiones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL,
            nombre TEXT NOT NULL,
            url TEXT NOT NULL,
            producto TEXT NOT NULL DEFAULT 'auto',
            auto INTEGER NOT NULL DEFAULT 1,
            tipificar_ia INTEGER NOT NULL DEFAULT 1,
            auto_vigente INTEGER NOT NULL DEFAULT 1,
            activa INTEGER NOT NULL DEFAULT 1,
            secreto_cifrado TEXT NOT NULL DEFAULT '',
            creada TEXT NOT NULL,
            ultimo_sync TEXT DEFAULT '',
            ultimo_resultado TEXT DEFAULT '',
            ultimo_error TEXT DEFAULT '',
            sincronizando INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS conexion_archivos (
            conexion_id INTEGER NOT NULL,
            remoto_id TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            firma TEXT NOT NULL DEFAULT '',
            nombre TEXT DEFAULT '',
            ruta TEXT DEFAULT '',
            PRIMARY KEY (conexion_id, remoto_id)
        );
        """)

        # Bases creadas antes de existir la opción "Vigente automático"
        columnas = [f["name"] for f in db.execute("PRAGMA table_info(conexiones)")]

        if "auto_vigente" not in columnas:
            db.execute(
                "ALTER TABLE conexiones "
                "ADD COLUMN auto_vigente INTEGER NOT NULL DEFAULT 1"
            )

        # Si el servidor se reinició a mitad de una sincronización
        db.execute("UPDATE conexiones SET sincronizando = 0")


def _publica(fila):

    datos = dict(fila)

    datos["tiene_credenciales"] = bool(datos.pop("secreto_cifrado", ""))

    return datos


def listar():

    with _conectar() as db:
        return [
            _publica(f) for f in db.execute(
                "SELECT * FROM conexiones ORDER BY nombre COLLATE NOCASE"
            )
        ]


def obtener(conexion_id):

    with _conectar() as db:

        fila = db.execute(
            "SELECT * FROM conexiones WHERE id = ?", (conexion_id,)
        ).fetchone()

    return _publica(fila) if fila else None


def obtener_secreto(conexion_id):
    """Credenciales descifradas, o None si no existen o no se pueden leer."""

    with _conectar() as db:

        fila = db.execute(
            "SELECT secreto_cifrado FROM conexiones WHERE id = ?",
            (conexion_id,)
        ).fetchone()

    if not fila:
        return None

    return crypto_service.descifrar(fila["secreto_cifrado"])


def crear(tipo, nombre, url, producto, auto, tipificar_ia, secreto,
          auto_vigente=True):

    with _LOCK, _conectar() as db:

        cursor = db.execute(
            "INSERT INTO conexiones (tipo, nombre, url, producto, auto, "
            "tipificar_ia, auto_vigente, activa, secreto_cifrado, creada) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                tipo, nombre, url, producto,
                1 if auto else 0, 1 if tipificar_ia else 0,
                1 if auto_vigente else 0,
                crypto_service.cifrar(secreto), _ahora()
            )
        )

        return cursor.lastrowid


def actualizar(conexion_id, nombre, url, producto, auto, tipificar_ia,
               secreto=None, auto_vigente=True):
    """Si `secreto` es None se conservan las credenciales actuales."""

    with _LOCK, _conectar() as db:

        db.execute(
            "UPDATE conexiones SET nombre = ?, url = ?, producto = ?, "
            "auto = ?, tipificar_ia = ?, auto_vigente = ? WHERE id = ?",
            (
                nombre, url, producto,
                1 if auto else 0, 1 if tipificar_ia else 0,
                1 if auto_vigente else 0, conexion_id
            )
        )

        if secreto is not None:
            db.execute(
                "UPDATE conexiones SET secreto_cifrado = ? WHERE id = ?",
                (crypto_service.cifrar(secreto), conexion_id)
            )


def cambiar_activa(conexion_id, activa):

    with _LOCK, _conectar() as db:
        db.execute(
            "UPDATE conexiones SET activa = ? WHERE id = ?",
            (1 if activa else 0, conexion_id)
        )


def eliminar(conexion_id):
    """Se borra la conexión; los documentos ya incorporados quedan en la KB."""

    with _LOCK, _conectar() as db:
        db.execute("DELETE FROM conexion_archivos WHERE conexion_id = ?", (conexion_id,))
        db.execute("DELETE FROM conexiones WHERE id = ?", (conexion_id,))


# ---- Estado de la sincronización -------------------------------------

def iniciar_sync(conexion_id):
    """Marca la conexión como 'sincronizando'. False si ya lo estaba."""

    with _LOCK, _conectar() as db:

        cursor = db.execute(
            "UPDATE conexiones SET sincronizando = 1 "
            "WHERE id = ? AND sincronizando = 0",
            (conexion_id,)
        )

        return cursor.rowcount == 1


def terminar_sync(conexion_id, resultado="", error=""):

    with _LOCK, _conectar() as db:
        db.execute(
            "UPDATE conexiones SET sincronizando = 0, ultimo_sync = ?, "
            "ultimo_resultado = ?, ultimo_error = ? WHERE id = ?",
            (_ahora(), resultado, error, conexion_id)
        )


def hay_sincronizando():

    with _conectar() as db:
        return db.execute(
            "SELECT COUNT(*) FROM conexiones WHERE sincronizando = 1"
        ).fetchone()[0] > 0


# ---- Archivos remotos <-> documentos ---------------------------------

def archivos_mapeados(conexion_id):

    with _conectar() as db:
        return {
            f["remoto_id"]: dict(f) for f in db.execute(
                "SELECT * FROM conexion_archivos WHERE conexion_id = ?",
                (conexion_id,)
            )
        }


def guardar_mapa(conexion_id, remoto_id, doc_id, firma, nombre, ruta):

    with _LOCK, _conectar() as db:
        db.execute(
            "INSERT OR REPLACE INTO conexion_archivos "
            "(conexion_id, remoto_id, doc_id, firma, nombre, ruta) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conexion_id, remoto_id, doc_id, firma, nombre, ruta)
        )


def borrar_mapa(conexion_id, remoto_id):

    with _LOCK, _conectar() as db:
        db.execute(
            "DELETE FROM conexion_archivos "
            "WHERE conexion_id = ? AND remoto_id = ?",
            (conexion_id, remoto_id)
        )
