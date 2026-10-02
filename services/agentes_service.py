"""
Registro de agentes L1.

Hay dos clases de agente:

- Agente de recepción (uno por producto, categoría vacía): lo recibe todo
  cliente que llega por WhatsApp con su producto ya identificado. Entiende
  qué necesita y clasifica la consulta. Si para esa categoría no hay un
  especialista activo, resuelve el caso él mismo.
- Especialista (producto + categoría): atiende solo las consultas de esa
  categoría de ese producto, con instrucciones propias.

Cada agente se puede pausar. Los agentes de recepción no se eliminan.
"""

import sqlite3
import threading
from datetime import datetime, timezone

from services import conversation_service


_LOCK = threading.RLock()

# Especialistas que se crean la primera vez (el resto se agrega desde la pantalla)
CATEGORIAS_SEMILLA = ("Facturación Electrónica", "Ecommerce")


def _ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _conectar():
    conexion = sqlite3.connect(conversation_service.DB_PATH, timeout=15)
    conexion.row_factory = sqlite3.Row
    return conexion


def nombre_por_defecto(producto, categoria=""):

    if not categoria:
        return f"Agente L1 {producto}"

    return f"Agente {categoria} · {producto}"


def init_tablas(productos):
    """Crea la tabla y, si está vacía, los agentes iniciales."""

    with _LOCK, _conectar() as db:

        db.executescript("""
        CREATE TABLE IF NOT EXISTS agentes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            producto TEXT NOT NULL,
            categoria TEXT NOT NULL DEFAULT '',
            activo INTEGER NOT NULL DEFAULT 1,
            instrucciones TEXT NOT NULL DEFAULT '',
            creado TEXT NOT NULL,
            UNIQUE (producto, categoria)
        );
        """)

        if db.execute("SELECT COUNT(*) FROM agentes").fetchone()[0]:
            return

        for producto in productos:

            db.execute(
                "INSERT INTO agentes (nombre, producto, categoria, creado) "
                "VALUES (?, ?, '', ?)",
                (nombre_por_defecto(producto), producto, _ahora())
            )

            for categoria in CATEGORIAS_SEMILLA:
                db.execute(
                    "INSERT INTO agentes (nombre, producto, categoria, creado) "
                    "VALUES (?, ?, ?, ?)",
                    (nombre_por_defecto(producto, categoria), producto,
                     categoria, _ahora())
                )


def _fila(fila):
    return dict(fila) if fila else None


def listar():

    with _conectar() as db:
        return [
            dict(f) for f in db.execute(
                "SELECT * FROM agentes ORDER BY producto, categoria != '' , categoria"
            )
        ]


def obtener(agente_id):

    if not agente_id:
        return None

    with _conectar() as db:
        return _fila(db.execute(
            "SELECT * FROM agentes WHERE id = ?", (agente_id,)
        ).fetchone())


def obtener_base(producto):

    with _conectar() as db:
        return _fila(db.execute(
            "SELECT * FROM agentes WHERE producto = ? AND categoria = ''",
            (producto,)
        ).fetchone())


def obtener_especialista(producto, categoria, solo_activos=True):

    if not categoria:
        return None

    sql = "SELECT * FROM agentes WHERE producto = ? AND categoria = ?"

    if solo_activos:
        sql += " AND activo = 1"

    with _conectar() as db:
        return _fila(db.execute(sql, (producto, categoria)).fetchone())


def categorias_con_especialista(producto):

    with _conectar() as db:
        return [
            f["categoria"] for f in db.execute(
                "SELECT categoria FROM agentes WHERE producto = ? "
                "AND categoria != '' AND activo = 1 ORDER BY categoria",
                (producto,)
            )
        ]


def crear(nombre, producto, categoria, instrucciones, productos, categorias):
    """Crea un especialista. Devuelve un mensaje de error, o ''."""

    if producto not in productos:
        return "Producto inválido."

    if categoria not in categorias:
        return "Elegí una categoría de la lista."

    nombre = (nombre or "").strip()[:80] or nombre_por_defecto(producto, categoria)

    with _LOCK, _conectar() as db:

        if db.execute(
            "SELECT 1 FROM agentes WHERE producto = ? AND categoria = ?",
            (producto, categoria)
        ).fetchone():
            return (
                f"Ya existe un agente de {categoria} para {producto}. "
                "Editalo o activalo en la lista."
            )

        db.execute(
            "INSERT INTO agentes (nombre, producto, categoria, instrucciones, creado) "
            "VALUES (?, ?, ?, ?, ?)",
            (nombre, producto, categoria, (instrucciones or "").strip()[:4000], _ahora())
        )

    return ""


def actualizar(agente_id, nombre, instrucciones):

    with _LOCK, _conectar() as db:

        fila = db.execute(
            "SELECT producto, categoria FROM agentes WHERE id = ?", (agente_id,)
        ).fetchone()

        if not fila:
            return "No se encontró el agente."

        db.execute(
            "UPDATE agentes SET nombre = ?, instrucciones = ? WHERE id = ?",
            (
                (nombre or "").strip()[:80]
                or nombre_por_defecto(fila["producto"], fila["categoria"]),
                (instrucciones or "").strip()[:4000],
                agente_id,
            )
        )

    return ""


def cambiar_activo(agente_id, activo):

    with _LOCK, _conectar() as db:
        db.execute(
            "UPDATE agentes SET activo = ? WHERE id = ?",
            (1 if activo else 0, agente_id)
        )


def eliminar(agente_id):
    """Solo se pueden eliminar especialistas. Devuelve un error, o ''."""

    with _LOCK, _conectar() as db:

        fila = db.execute(
            "SELECT categoria FROM agentes WHERE id = ?", (agente_id,)
        ).fetchone()

        if not fila:
            return "No se encontró el agente."

        if not fila["categoria"]:
            return (
                "El agente de recepción de un producto no se elimina: "
                "si hace falta, pausalo."
            )

        db.execute("DELETE FROM agentes WHERE id = ?", (agente_id,))

    return ""