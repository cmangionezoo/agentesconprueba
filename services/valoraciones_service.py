"""
Valoración humana de las conversaciones (👍 / 👎 + corrección).

El equipo de mesa de ayuda marca cada conversación como buena o mala y, si
es mala, anota qué debió responder o hacer el agente. Esas correcciones se
muestran en la configuración de cada agente para mejorar sus instrucciones.
"""

import sqlite3
from datetime import datetime, timezone

from services import conversation_service


def _ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _conectar():
    c = sqlite3.connect(conversation_service.DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def votar(conversacion_id, usuario, voto, correccion=""):
    """Un voto por usuario y conversación (se puede cambiar). '' o error."""

    if voto not in (1, -1):
        return "Voto inválido."

    with _conectar() as db:

        if not db.execute(
            "SELECT 1 FROM conversaciones WHERE id = ?", (conversacion_id,)
        ).fetchone():
            return "No se encontró la conversación."

        db.execute(
            "INSERT INTO valoraciones (conversacion_id, usuario, voto, correccion, fecha) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(conversacion_id, usuario) DO UPDATE SET "
            "voto = excluded.voto, correccion = excluded.correccion, fecha = excluded.fecha",
            (conversacion_id, usuario, voto, (correccion or "").strip()[:1500], _ahora())
        )

    return ""


def por_agente():
    """{agente_id: {positivos, negativos}} según el agente con el que terminó."""

    with _conectar() as db:

        filas = db.execute(
            """
            SELECT c.agente_id AS agente_id,
                   SUM(CASE WHEN v.voto > 0 THEN 1 ELSE 0 END) AS positivos,
                   SUM(CASE WHEN v.voto < 0 THEN 1 ELSE 0 END) AS negativos
            FROM valoraciones v JOIN conversaciones c ON c.id = v.conversacion_id
            WHERE c.agente_id > 0 GROUP BY c.agente_id
            """
        ).fetchall()

    return {
        f["agente_id"]: {"positivos": f["positivos"] or 0, "negativos": f["negativos"] or 0}
        for f in filas
    }


def correcciones(agente_id, limite=15):
    """Últimas correcciones (👎 con texto) de las conversaciones de un agente."""

    with _conectar() as db:

        return [
            dict(f) for f in db.execute(
                """
                SELECT v.fecha, v.usuario, v.correccion, c.id AS conversacion_id,
                       c.problema, c.categoria
                FROM valoraciones v JOIN conversaciones c ON c.id = v.conversacion_id
                WHERE c.agente_id = ? AND v.voto < 0 AND v.correccion != ''
                ORDER BY v.fecha DESC LIMIT ?
                """,
                (agente_id, limite)
            )
        ]


def totales(desde=None, hasta=None):

    sql = (
        "SELECT SUM(CASE WHEN voto > 0 THEN 1 ELSE 0 END) AS p, "
        "SUM(CASE WHEN voto < 0 THEN 1 ELSE 0 END) AS n FROM valoraciones"
    )
    parametros = []

    if desde and hasta:
        sql += " WHERE fecha >= ? AND fecha < ?"
        parametros = [desde, hasta]

    with _conectar() as db:
        f = db.execute(sql, parametros).fetchone()

    return {"positivos": f["p"] or 0, "negativos": f["n"] or 0}
