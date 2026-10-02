"""
Persistencia de conversaciones y cálculo de métricas.

Usa SQLite (viene con Python, no requiere dependencias nuevas).
El archivo se guarda en la misma carpeta que la Knowledge Base, así que
si configurás un Persistent Disk en Render (KNOWLEDGE_DIR), las
conversaciones también sobreviven a los deploys.
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone


DB_PATH = None
_LOCK = threading.Lock()

ETAPAS_CIERRE = ("Resuelto", "Derivado")


# ============================================================
# BASE DE DATOS
# ============================================================

def _ahora():

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _conectar():

    conexion = sqlite3.connect(DB_PATH, timeout=15)

    conexion.row_factory = sqlite3.Row

    return conexion


def init_db(ruta):

    global DB_PATH

    DB_PATH = ruta

    os.makedirs(os.path.dirname(ruta), exist_ok=True)

    with _LOCK, _conectar() as db:

        db.execute("PRAGMA journal_mode=WAL")

        db.executescript("""
        CREATE TABLE IF NOT EXISTS conversaciones (
            id TEXT PRIMARY KEY,
            canal TEXT NOT NULL DEFAULT 'playground',
            creada TEXT NOT NULL,
            actualizada TEXT NOT NULL,
            cerrada TEXT,
            producto TEXT DEFAULT '',
            categoria TEXT DEFAULT '',
            subcategoria TEXT DEFAULT '',
            etapa TEXT DEFAULT '',
            resultado TEXT DEFAULT '',
            problema TEXT DEFAULT '',
            diagnostico TEXT DEFAULT '',
            solucion TEXT DEFAULT '',
            intentos INTEGER DEFAULT 0,
            derivada INTEGER DEFAULT 0,
            destino TEXT DEFAULT '',
            motivo TEXT DEFAULT '',
            mensajes INTEGER DEFAULT 0,
            turnos_sin_kb INTEGER DEFAULT 0,
            estado_json TEXT DEFAULT '',
            resumen_json TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS mensajes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversacion_id TEXT NOT NULL,
            orden INTEGER NOT NULL,
            rol TEXT NOT NULL,
            texto TEXT NOT NULL,
            fecha TEXT NOT NULL,
            etapa TEXT DEFAULT '',
            fragmentos_json TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS consultas_kb (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversacion_id TEXT NOT NULL,
            fecha TEXT NOT NULL,
            doc_id TEXT,
            documento TEXT,
            pagina INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_mensajes_conv
            ON mensajes (conversacion_id, orden);

        CREATE INDEX IF NOT EXISTS idx_consultas_conv
            ON consultas_kb (conversacion_id);
        """)


# ============================================================
# GUARDAR UN TURNO
# ============================================================

def guardar_turno(
    conversacion_id,
    canal,
    historial_previo,
    mensaje_cliente,
    resultado
):
    """
    Guarda el mensaje del cliente, la respuesta del agente, el estado
    interno y los documentos consultados. Devuelve el id de la conversación.
    """

    estado = resultado["estado"]

    deriv = estado.get("derivacion") or {}

    ahora = _ahora()

    with _LOCK, _conectar() as db:

        existe = None

        if conversacion_id:

            existe = db.execute(
                "SELECT id, cerrada, mensajes, turnos_sin_kb "
                "FROM conversaciones WHERE id = ?",
                (conversacion_id,)
            ).fetchone()

        if not existe:

            conversacion_id = uuid.uuid4().hex

            db.execute(
                "INSERT INTO conversaciones (id, canal, creada, actualizada) "
                "VALUES (?, ?, ?, ?)",
                (conversacion_id, canal, ahora, ahora)
            )

            orden = 0

            # Mensajes previos (por ejemplo, el saludo inicial del agente)
            for item in historial_previo or []:

                db.execute(
                    "INSERT INTO mensajes "
                    "(conversacion_id, orden, rol, texto, fecha) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (conversacion_id, orden, item["rol"], item["texto"], ahora)
                )

                orden += 1

            cerrada_previa = None
            turnos_sin_kb = 0

        else:

            orden = existe["mensajes"]
            cerrada_previa = existe["cerrada"]
            turnos_sin_kb = existe["turnos_sin_kb"]

        db.execute(
            "INSERT INTO mensajes "
            "(conversacion_id, orden, rol, texto, fecha) "
            "VALUES (?, ?, 'cliente', ?, ?)",
            (conversacion_id, orden, mensaje_cliente, ahora)
        )

        db.execute(
            "INSERT INTO mensajes "
            "(conversacion_id, orden, rol, texto, fecha, etapa, "
            "fragmentos_json) VALUES (?, ?, 'agente', ?, ?, ?, ?)",
            (
                conversacion_id,
                orden + 1,
                resultado["respuesta"],
                ahora,
                estado.get("etapa", ""),
                json.dumps(resultado.get("fragmentos", []), ensure_ascii=False)
            )
        )

        citados = set(resultado.get("fragmentos_usados", []))

        for f in resultado.get("fragmentos", []):

            # Solo se cuentan los documentos que el agente usó en este turno
            if f["id"] not in citados:
                continue

            db.execute(
                "INSERT INTO consultas_kb "
                "(conversacion_id, fecha, doc_id, documento, pagina) "
                "VALUES (?, ?, ?, ?, ?)",
                (conversacion_id, ahora, f.get("doc_id"),
                 f["documento"], f["pagina"])
            )

        if not resultado.get("fragmentos"):
            turnos_sin_kb += 1

        cerrada = cerrada_previa

        if not cerrada and estado.get("etapa") in ETAPAS_CIERRE:
            cerrada = ahora

        db.execute(
            """
            UPDATE conversaciones SET
                actualizada = ?, cerrada = ?, producto = ?, categoria = ?,
                subcategoria = ?, etapa = ?, resultado = ?, problema = ?,
                diagnostico = ?, solucion = ?, intentos = ?, derivada = ?,
                destino = ?, motivo = ?, mensajes = ?, turnos_sin_kb = ?,
                estado_json = ?, resumen_json = ?
            WHERE id = ?
            """,
            (
                ahora,
                cerrada,
                estado.get("producto", ""),
                estado.get("categoria", ""),
                estado.get("subcategoria", ""),
                estado.get("etapa", ""),
                estado.get("resultado", ""),
                estado.get("problema_informado", ""),
                estado.get("diagnostico", ""),
                estado.get("solucion_propuesta", ""),
                int(estado.get("intentos_solucion") or 0),
                1 if deriv.get("derivar") else 0,
                deriv.get("destino", ""),
                deriv.get("motivo", ""),
                orden + 2,
                turnos_sin_kb,
                json.dumps(estado, ensure_ascii=False),
                json.dumps(resultado.get("resumen_tecnico"), ensure_ascii=False)
                if resultado.get("resumen_tecnico") else "",
                conversacion_id
            )
        )

    return conversacion_id


# ============================================================
# CONSULTAS
# ============================================================

def _estado_conversacion(fila):

    if fila["derivada"]:
        return "Derivada"

    if fila["etapa"] == "Resuelto":
        return "Resuelta por IA"

    return "En curso"


def listar_conversaciones(canal=None, limite=30):

    consulta = "SELECT * FROM conversaciones"

    parametros = []

    if canal:
        consulta += " WHERE canal = ?"
        parametros.append(canal)

    consulta += " ORDER BY creada DESC LIMIT ?"

    parametros.append(limite)

    with _conectar() as db:

        filas = db.execute(consulta, parametros).fetchall()

    return [
        {
            "id": f["id"],
            "canal": f["canal"],
            "fecha": f["creada"].replace("T", " ") + " UTC",
            "categoria": f["categoria"] or "Sin clasificar",
            "subcategoria": f["subcategoria"],
            "problema": f["problema"],
            "estado": _estado_conversacion(f),
            "destino": f["destino"],
            "mensajes": f["mensajes"],
        }
        for f in filas
    ]


def obtener_transcripcion(conversacion_id):
    """Texto plano de la conversación + datos para el modal."""

    with _conectar() as db:

        conv = db.execute(
            "SELECT * FROM conversaciones WHERE id = ?",
            (conversacion_id,)
        ).fetchone()

        if not conv:
            return None

        mensajes = db.execute(
            "SELECT rol, texto, fecha FROM mensajes "
            "WHERE conversacion_id = ? ORDER BY orden",
            (conversacion_id,)
        ).fetchall()

    lineas = [
        f"Producto: {conv['producto'] or '-'}",
        f"Categoría: {conv['categoria'] or '-'} / {conv['subcategoria'] or '-'}",
        f"Estado: {_estado_conversacion(conv)}",
    ]

    if conv["derivada"]:
        lineas.append(f"Derivada a: {conv['destino']} — {conv['motivo']}")

    lineas.append("")

    for m in mensajes:

        quien = "CLIENTE" if m["rol"] == "cliente" else "AGENTE"

        lineas.append(f"{quien}: {m['texto']}\n")

    if conv["resumen_json"]:

        lineas.append("----- RESUMEN TÉCNICO DE DERIVACIÓN -----")

        try:
            lineas.append(
                json.dumps(
                    json.loads(conv["resumen_json"]),
                    ensure_ascii=False,
                    indent=2
                )
            )
        except Exception:
            lineas.append(conv["resumen_json"])

    return "\n".join(lineas)


def _transcripciones_para_lista(lista):

    for item in lista:
        item["transcripcion"] = obtener_transcripcion(item["id"]) or ""

    return lista


# ============================================================
# MÉTRICAS
# ============================================================

def _formatear_duracion(segundos):

    if segundos is None:
        return "-"

    segundos = int(segundos)

    if segundos < 60:
        return f"{segundos} s"

    minutos = segundos / 60

    if minutos < 60:
        return f"{minutos:.1f} min"

    return f"{minutos / 60:.1f} h"


def calcular_metricas(canal=None):

    filtro = "WHERE canal = ?" if canal else "WHERE 1=1"

    parametros = [canal] if canal else []

    with _conectar() as db:

        def uno(sql, extra=()):
            return db.execute(sql, parametros + list(extra)).fetchone()[0]

        def varias(sql, extra=()):
            return [
                dict(f) for f in db.execute(sql, parametros + list(extra))
            ]

        total = uno(f"SELECT COUNT(*) FROM conversaciones {filtro}")

        resueltas = uno(
            f"SELECT COUNT(*) FROM conversaciones {filtro} "
            "AND derivada = 0 AND etapa = 'Resuelto'"
        )

        derivadas = uno(
            f"SELECT COUNT(*) FROM conversaciones {filtro} AND derivada = 1"
        )

        en_curso = total - resueltas - derivadas

        promedio = uno(
            f"SELECT AVG((julianday(cerrada) - julianday(creada)) * 86400) "
            f"FROM conversaciones {filtro} AND cerrada IS NOT NULL"
        )

        por_categoria = varias(
            f"""
            SELECT
                COALESCE(NULLIF(categoria, ''), 'Sin clasificar') AS categoria,
                COUNT(*) AS total,
                SUM(CASE WHEN derivada = 0 AND etapa = 'Resuelto'
                    THEN 1 ELSE 0 END) AS resueltas,
                SUM(derivada) AS derivadas
            FROM conversaciones {filtro}
            GROUP BY 1 ORDER BY total DESC
            """
        )

        motivos = varias(
            f"""
            SELECT destino, motivo, COUNT(*) AS cantidad
            FROM conversaciones {filtro} AND derivada = 1
            GROUP BY destino, motivo
            ORDER BY cantidad DESC LIMIT 10
            """
        )

        docs_consultados = varias(
            f"""
            SELECT k.documento AS documento,
                   COUNT(*) AS consultas,
                   COUNT(DISTINCT k.conversacion_id) AS conversaciones
            FROM consultas_kb k
            JOIN conversaciones c ON c.id = k.conversacion_id
            {filtro.replace('canal', 'c.canal')}
            GROUP BY k.documento
            ORDER BY consultas DESC LIMIT 10
            """
        )

        soluciones = varias(
            f"""
            SELECT k.documento AS documento,
                   COUNT(DISTINCT k.conversacion_id) AS casos
            FROM consultas_kb k
            JOIN conversaciones c ON c.id = k.conversacion_id
            {filtro.replace('canal', 'c.canal')}
            AND c.derivada = 0 AND c.etapa = 'Resuelto'
            GROUP BY k.documento
            ORDER BY casos DESC LIMIT 10
            """
        )

        sin_documentacion = varias(
            f"""
            SELECT c.id, c.categoria, c.subcategoria, c.problema, c.destino
            FROM conversaciones c
            {filtro.replace('canal', 'c.canal')}
            AND c.problema != ''
            AND (
                c.destino = 'MDA'
                OR NOT EXISTS (
                    SELECT 1 FROM consultas_kb k
                    WHERE k.conversacion_id = c.id
                )
            )
            ORDER BY c.creada DESC LIMIT 10
            """
        )

        no_resolvio = varias(
            f"""
            SELECT id, categoria, subcategoria, problema, destino, motivo,
                   intentos
            FROM conversaciones {filtro}
            AND derivada = 1
            AND (intentos > 0 OR resultado = 'No resuelto')
            ORDER BY creada DESC LIMIT 10
            """
        )

    porcentaje = round(resueltas * 100 / total, 1) if total else 0

    return {
        "total": total,
        "resueltas": resueltas,
        "derivadas": derivadas,
        "en_curso": en_curso,
        "porcentaje_resolucion": porcentaje,
        "tiempo_promedio": _formatear_duracion(promedio),
        "por_categoria": por_categoria,
        "motivos_derivacion": motivos,
        "docs_consultados": docs_consultados,
        "soluciones": soluciones,
        "sin_documentacion": sin_documentacion,
        "no_resolvio": no_resolvio,
        "conversaciones": _transcripciones_para_lista(
            listar_conversaciones(canal, limite=30)
        ),
    }
