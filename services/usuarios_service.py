"""
Usuarios de la plataforma (login).

Se guardan en la misma base SQLite que las conversaciones, así quedan en el
disco persistente. Las contraseñas se guardan con hash (nunca en texto).

Roles:
- admin:   todo, más crear y administrar usuarios, backup y restauración.
- usuario: usa la plataforma (KB, Playground, Métricas) pero no administra.
- analista: SOLO LECTURA. Ve todo (KB, agentes, métricas, conversaciones) y
            puede valorar conversaciones, pero no modifica nada.
"""

import os
import re
import sqlite3
import threading
from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from services import conversation_service


_LOCK = threading.RLock()

ROLES = ("admin", "usuario", "analista")

MIN_CLAVE = 8

PATRON_USUARIO = re.compile(r"^[a-z0-9._-]{3,30}$")

# Para que verificar un usuario inexistente tarde lo mismo que uno real
_HASH_FALSO = generate_password_hash("clave-falsa-para-igualar-tiempos")


def _ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _conectar():
    conexion = sqlite3.connect(conversation_service.DB_PATH, timeout=15)
    conexion.row_factory = sqlite3.Row
    return conexion


def init_tablas():

    with _LOCK, _conectar() as db:

        db.executescript("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario TEXT NOT NULL UNIQUE,
            clave_hash TEXT NOT NULL,
            rol TEXT NOT NULL DEFAULT 'usuario',
            activo INTEGER NOT NULL DEFAULT 1,
            creado TEXT NOT NULL,
            ultimo_acceso TEXT DEFAULT ''
        );
        """)


def cantidad():

    with _conectar() as db:
        return db.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]


def _fila(fila):
    return dict(fila) if fila else None


def obtener(usuario_id):

    with _conectar() as db:
        return _fila(db.execute(
            "SELECT * FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone())


def obtener_por_nombre(usuario):

    with _conectar() as db:
        return _fila(db.execute(
            "SELECT * FROM usuarios WHERE usuario = ?",
            (str(usuario or "").strip().lower(),)
        ).fetchone())


def listar():

    with _conectar() as db:
        return [
            dict(f) for f in db.execute(
                "SELECT id, usuario, rol, activo, creado, ultimo_acceso "
                "FROM usuarios ORDER BY rol = 'admin' DESC, usuario"
            )
        ]


def _admins_activos(db, excluyendo=None):

    sql = "SELECT COUNT(*) FROM usuarios WHERE rol = 'admin' AND activo = 1"
    params = []

    if excluyendo is not None:
        sql += " AND id != ?"
        params.append(excluyendo)

    return db.execute(sql, params).fetchone()[0]


def validar_clave(clave):

    if len(clave or "") < MIN_CLAVE:
        return f"La contraseña tiene que tener al menos {MIN_CLAVE} caracteres."

    return ""


def crear(usuario, clave, rol="usuario"):
    """Devuelve un mensaje de error, o '' si se creó."""

    usuario = str(usuario or "").strip().lower()

    if not PATRON_USUARIO.match(usuario):
        return (
            "El usuario debe tener entre 3 y 30 caracteres: letras, "
            "números, punto, guion o guion bajo (sin espacios)."
        )

    if rol not in ROLES:
        return "Rol inválido."

    error = validar_clave(clave)

    if error:
        return error

    with _LOCK, _conectar() as db:

        if db.execute(
            "SELECT 1 FROM usuarios WHERE usuario = ?", (usuario,)
        ).fetchone():
            return f"Ya existe el usuario «{usuario}»."

        db.execute(
            "INSERT INTO usuarios (usuario, clave_hash, rol, activo, creado) "
            "VALUES (?, ?, ?, 1, ?)",
            (usuario, generate_password_hash(clave), rol, _ahora())
        )

    return ""


def asegurar_admin(usuario, clave, resetear=False):
    """
    Crea el administrador inicial a partir de las variables de entorno
    ADMIN_USUARIO / ADMIN_CLAVE. Si ya existe, no toca nada, salvo que
    resetear=True (ADMIN_RESETEAR_CLAVE=1): ahí le vuelve a poner esa clave,
    lo reactiva y lo deja como admin (sirve si se olvidó la contraseña).
    """

    usuario = str(usuario or "").strip().lower()

    if not usuario or not clave:
        return "sin_variables"

    existente = obtener_por_nombre(usuario)

    if not existente:

        error = crear(usuario, clave, "admin")

        return error or "creado"

    if resetear:

        error = validar_clave(clave)

        if error:
            return error

        with _LOCK, _conectar() as db:
            db.execute(
                "UPDATE usuarios SET clave_hash = ?, rol = 'admin', "
                "activo = 1 WHERE id = ?",
                (generate_password_hash(clave), existente["id"])
            )

        return "reseteado"

    return "existente"


def verificar(usuario, clave):
    """El usuario si las credenciales son correctas y está activo; si no, None."""

    fila = obtener_por_nombre(usuario)

    # Se compara siempre contra un hash, exista o no el usuario
    ok = check_password_hash(
        fila["clave_hash"] if fila else _HASH_FALSO, clave or ""
    )

    if not (fila and ok and fila["activo"]):
        return None

    with _LOCK, _conectar() as db:
        db.execute(
            "UPDATE usuarios SET ultimo_acceso = ? WHERE id = ?",
            (_ahora(), fila["id"])
        )

    return fila


def firma_sesion(fila):
    """Cambia cuando cambia la contraseña: las sesiones viejas dejan de valer."""

    return fila["clave_hash"][-16:]


def cambiar_clave(usuario_id, clave_nueva):
    """Devuelve un mensaje de error, o ''."""

    error = validar_clave(clave_nueva)

    if error:
        return error

    with _LOCK, _conectar() as db:
        db.execute(
            "UPDATE usuarios SET clave_hash = ? WHERE id = ?",
            (generate_password_hash(clave_nueva), usuario_id)
        )

    return ""


def cambiar_activo(usuario_id, activo):

    with _LOCK, _conectar() as db:

        fila = db.execute(
            "SELECT rol FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()

        if not fila:
            return "No se encontró el usuario."

        if (
            not activo and fila["rol"] == "admin"
            and _admins_activos(db, excluyendo=usuario_id) == 0
        ):
            return "No podés desactivar al último administrador."

        db.execute(
            "UPDATE usuarios SET activo = ? WHERE id = ?",
            (1 if activo else 0, usuario_id)
        )

    return ""


def cambiar_rol(usuario_id, rol):

    if rol not in ROLES:
        return "Rol inválido."

    with _LOCK, _conectar() as db:

        fila = db.execute(
            "SELECT rol, activo FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()

        if not fila:
            return "No se encontró el usuario."

        if (
            rol != "admin" and fila["rol"] == "admin" and fila["activo"]
            and _admins_activos(db, excluyendo=usuario_id) == 0
        ):
            return "No podés quitarle el rol al último administrador."

        db.execute(
            "UPDATE usuarios SET rol = ? WHERE id = ?", (rol, usuario_id)
        )

    return ""


def eliminar(usuario_id):

    with _LOCK, _conectar() as db:

        fila = db.execute(
            "SELECT rol, activo FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()

        if not fila:
            return "No se encontró el usuario."

        if (
            fila["rol"] == "admin" and fila["activo"]
            and _admins_activos(db, excluyendo=usuario_id) == 0
        ):
            return "No podés eliminar al último administrador."

        db.execute("DELETE FROM usuarios WHERE id = ?", (usuario_id,))

    return ""
