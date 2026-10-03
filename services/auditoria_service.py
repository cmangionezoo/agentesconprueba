"""Registro de cambios: quién hizo qué y cuándo (acciones que modifican algo)."""

import sqlite3
import threading
from datetime import datetime, timezone

from services import conversation_service

_LOCK = threading.RLock()


def _ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _conectar():
    c = sqlite3.connect(conversation_service.DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init_tablas():

    with _LOCK, _conectar() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS auditoria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            usuario TEXT NOT NULL DEFAULT '',
            rol TEXT NOT NULL DEFAULT '',
            accion TEXT NOT NULL,
            detalle TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT '',
            resultado TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_auditoria_fecha ON auditoria (fecha);
        """)


def registrar(usuario, rol, accion, detalle="", ip="", resultado=""):

    try:
        with _LOCK, _conectar() as db:
            db.execute(
                "INSERT INTO auditoria (fecha, usuario, rol, accion, detalle, ip, resultado) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_ahora(), usuario or "", rol or "", accion,
                 (detalle or "")[:600], ip or "", resultado or "")
            )
    except Exception as e:
        print(f"Auditoría: no se pudo registrar ({e})")


def listar(limite=300, usuario="", texto=""):

    sql = "SELECT * FROM auditoria WHERE 1=1"
    p = []

    if usuario:
        sql += " AND usuario = ?"
        p.append(usuario)

    if texto:
        sql += " AND (accion LIKE ? OR detalle LIKE ?)"
        p += [f"%{texto}%", f"%{texto}%"]

    sql += " ORDER BY id DESC LIMIT ?"
    p.append(limite)

    with _conectar() as db:
        return [dict(f) for f in db.execute(sql, p)]
