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

# Tipificaciones de cada producto. Cada tipificación tiene su agente
# especialista. Pantera y zNube todavía no tienen: se cargan desde la
# pantalla Agentes cuando estén definidas.
TIPIFICACIONES_INICIALES = {
    "Dragonfish": [
        "Mantenimiento", "Comunicaciones", "Diseños", "Parámetros/seguridad",
        "Ventas", "zNube", "Fondos/Contabilidad", "Stock/Producción",
        "Facturación", "e-commerce",
    ],
    "Lince": [
        "Códigos de desactivación", "Diseños/Pasajes datos/Configur",
        "Problema TI Lince Indumentaria", "Problema", "Consulta de uso",
        "Omnicanalidad",
    ],
}

# Especialistas que creaba la versión anterior (se retiran al migrar)
_CATEGORIAS_VIEJAS = ("Facturación Electrónica", "Ecommerce")

MIGRACION = "estructura_tipificaciones_v1"


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
    """
    Crea las tablas y, una sola vez, arma la estructura inicial:
    tipificaciones por producto, un agente de recepción por producto y un
    especialista por tipificación. Los productos sin tipificaciones
    (Pantera y zNube hoy) quedan con su recepción PAUSADA y sin especialistas.
    """

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

        CREATE TABLE IF NOT EXISTS tipificaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto TEXT NOT NULL,
            nombre TEXT NOT NULL,
            orden INTEGER NOT NULL DEFAULT 0,
            UNIQUE (producto, nombre)
        );
        """)

    if conversation_service.obtener_ajuste(MIGRACION, "") == "1":
        return

    with _LOCK, _conectar() as db:

        # 1) Tipificaciones iniciales (si todavía no hay ninguna)
        if not db.execute("SELECT COUNT(*) FROM tipificaciones").fetchone()[0]:

            for producto, nombres in TIPIFICACIONES_INICIALES.items():
                for orden, nombre in enumerate(nombres):
                    db.execute(
                        "INSERT OR IGNORE INTO tipificaciones "
                        "(producto, nombre, orden) VALUES (?, ?, ?)",
                        (producto, nombre, orden)
                    )

        # 2) Se retiran los especialistas de ejemplo de la versión anterior
        #    (los que no tienen instrucciones ni nombre personalizado)
        for f in db.execute(
            "SELECT id, producto, categoria, nombre, instrucciones FROM agentes "
            "WHERE categoria != ''"
        ).fetchall():

            if (
                f["categoria"] in _CATEGORIAS_VIEJAS
                and not f["instrucciones"]
                and f["nombre"] == nombre_por_defecto(f["producto"], f["categoria"])
            ):
                db.execute("DELETE FROM agentes WHERE id = ?", (f["id"],))

        # 3) Un especialista que se haya personalizado conserva su categoría
        for f in db.execute(
            "SELECT producto, categoria FROM agentes WHERE categoria != ''"
        ).fetchall():
            db.execute(
                "INSERT OR IGNORE INTO tipificaciones (producto, nombre, orden) "
                "VALUES (?, ?, 99)", (f["producto"], f["categoria"])
            )

        # 4) Recepción por producto y un especialista por tipificación
        for producto in productos:

            tiene = db.execute(
                "SELECT COUNT(*) FROM tipificaciones WHERE producto = ?",
                (producto,)
            ).fetchone()[0] > 0

            recepcion = db.execute(
                "SELECT id FROM agentes WHERE producto = ? AND categoria = ''",
                (producto,)
            ).fetchone()

            if not recepcion:
                db.execute(
                    "INSERT INTO agentes (nombre, producto, categoria, activo, creado) "
                    "VALUES (?, ?, '', ?, ?)",
                    (nombre_por_defecto(producto), producto,
                     1 if tiene else 0, _ahora())
                )
            elif not tiene:
                # Producto todavía sin definir: no atiende en WhatsApp
                db.execute(
                    "UPDATE agentes SET activo = 0 WHERE id = ?", (recepcion["id"],)
                )

            for t in db.execute(
                "SELECT nombre FROM tipificaciones WHERE producto = ? ORDER BY orden, id",
                (producto,)
            ).fetchall():
                db.execute(
                    "INSERT OR IGNORE INTO agentes "
                    "(nombre, producto, categoria, creado) VALUES (?, ?, ?, ?)",
                    (nombre_por_defecto(producto, t["nombre"]), producto,
                     t["nombre"], _ahora())
                )

    conversation_service.guardar_ajuste(MIGRACION, "1")


# ============================================================
# TIPIFICACIONES (categorías por producto)
# ============================================================

def tipificaciones(producto):
    """Nombres de las tipificaciones de un producto, en orden."""

    with _conectar() as db:
        return [
            f["nombre"] for f in db.execute(
                "SELECT nombre FROM tipificaciones WHERE producto = ? "
                "ORDER BY orden, id", (producto,)
            )
        ]


def tipificaciones_por_producto(productos):

    return {p: tipificaciones(p) for p in productos}


def listar_tipificaciones():

    with _conectar() as db:
        return [
            dict(f) for f in db.execute(
                "SELECT id, producto, nombre FROM tipificaciones "
                "ORDER BY producto, orden, id"
            )
        ]


def obtener_tipificacion(tipificacion_id):

    with _conectar() as db:
        return _fila(db.execute(
            "SELECT id, producto, nombre FROM tipificaciones WHERE id = ?",
            (tipificacion_id,)
        ).fetchone())


def crear_tipificacion(producto, nombre, productos):
    """
    Agrega una tipificación y crea su agente especialista (activo).
    Devuelve un mensaje de error, o ''.
    """

    nombre = " ".join((nombre or "").split())[:80]

    if producto not in productos:
        return "Producto inválido."

    if not nombre:
        return "Escribí el nombre de la tipificación."

    with _LOCK, _conectar() as db:

        if db.execute(
            "SELECT 1 FROM tipificaciones WHERE producto = ? "
            "AND lower(nombre) = lower(?)", (producto, nombre)
        ).fetchone():
            return f"{producto} ya tiene una tipificación «{nombre}»."

        orden = db.execute(
            "SELECT COALESCE(MAX(orden), -1) + 1 FROM tipificaciones "
            "WHERE producto = ?", (producto,)
        ).fetchone()[0]

        db.execute(
            "INSERT INTO tipificaciones (producto, nombre, orden) VALUES (?, ?, ?)",
            (producto, nombre, orden)
        )

        db.execute(
            "INSERT OR IGNORE INTO agentes (nombre, producto, categoria, creado) "
            "VALUES (?, ?, ?, ?)",
            (nombre_por_defecto(producto, nombre), producto, nombre, _ahora())
        )

        # El primer agente de un producto sin definir: se activa la recepción
        # solo si el administrador la había dejado pausada por falta de datos
        # (no se toca: se activa a mano desde la lista).

    return ""


def renombrar_tipificacion(tipificacion_id, nombre_nuevo):
    """
    Cambia el nombre de la tipificación y de su especialista (si conservaba
    el nombre automático). Devuelve (error, nombre_anterior, producto).
    """

    nombre_nuevo = " ".join((nombre_nuevo or "").split())[:80]

    if not nombre_nuevo:
        return "Escribí el nombre de la tipificación.", "", ""

    with _LOCK, _conectar() as db:

        t = db.execute(
            "SELECT id, producto, nombre FROM tipificaciones WHERE id = ?",
            (tipificacion_id,)
        ).fetchone()

        if not t:
            return "No se encontró la tipificación.", "", ""

        if db.execute(
            "SELECT 1 FROM tipificaciones WHERE producto = ? "
            "AND lower(nombre) = lower(?) AND id != ?",
            (t["producto"], nombre_nuevo, t["id"])
        ).fetchone():
            return f"{t['producto']} ya tiene una tipificación «{nombre_nuevo}».", "", ""

        db.execute(
            "UPDATE tipificaciones SET nombre = ? WHERE id = ?",
            (nombre_nuevo, t["id"])
        )

        agente = db.execute(
            "SELECT id, nombre FROM agentes WHERE producto = ? AND categoria = ?",
            (t["producto"], t["nombre"])
        ).fetchone()

        if agente:

            nuevo_nombre_agente = (
                nombre_por_defecto(t["producto"], nombre_nuevo)
                if agente["nombre"] == nombre_por_defecto(t["producto"], t["nombre"])
                else agente["nombre"]
            )

            db.execute(
                "UPDATE agentes SET categoria = ?, nombre = ? WHERE id = ?",
                (nombre_nuevo, nuevo_nombre_agente, agente["id"])
            )

    return "", t["nombre"], t["producto"]


def eliminar_tipificacion(tipificacion_id):
    """Elimina la tipificación y su especialista. (error, producto, nombre)."""

    with _LOCK, _conectar() as db:

        t = db.execute(
            "SELECT id, producto, nombre FROM tipificaciones WHERE id = ?",
            (tipificacion_id,)
        ).fetchone()

        if not t:
            return "No se encontró la tipificación.", "", ""

        db.execute("DELETE FROM tipificaciones WHERE id = ?", (t["id"],))

        db.execute(
            "DELETE FROM agentes WHERE producto = ? AND categoria = ?",
            (t["producto"], t["nombre"])
        )

    return "", t["producto"], t["nombre"]


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
    """
    Los agentes se eliminan junto con su tipificación. Acá solo se permite
    quitar un especialista suelto (por ejemplo, uno de una versión anterior).
    Devuelve un error, o ''.
    """

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
