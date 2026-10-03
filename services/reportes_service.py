"""
Medición avanzada y reporte semanal.

- Serie por día (hora de Argentina), tiempo de resolución, tokens y costo.
- Brechas de la Knowledge Base: consultas que el agente no pudo resolver.
- Reporte semanal automático (lunes 08:00, hora de Argentina) por mail o
  webhook, con el Excel adjunto, y exportaciones a Excel / CSV / JSON.

El costo es una ESTIMACIÓN con los precios por millón de tokens que se
definan en PRECIO_ENTRADA_USD_1M y PRECIO_SALIDA_USD_1M (por defecto, los de
gpt-5.4-mini). No incluye embeddings, transcripciones ni análisis de imágenes.
"""

import csv
import io
import json
import os
import sqlite3
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone

from services import alertas_service, conversation_service

ARG = timezone(timedelta(hours=-3))

_LOCK = threading.RLock()


def _conectar():
    c = sqlite3.connect(conversation_service.DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def _flotante(nombre, defecto):
    try:
        return float(os.environ.get(nombre, defecto))
    except ValueError:
        return defecto


def precios():
    return _flotante("PRECIO_ENTRADA_USD_1M", 0.75), _flotante("PRECIO_SALIDA_USD_1M", 4.5)


def costo_usd(tokens_in, tokens_out):

    p_in, p_out = precios()

    return (tokens_in or 0) * p_in / 1e6 + (tokens_out or 0) * p_out / 1e6


def _utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def init_tablas():

    with _LOCK, _conectar() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS reportes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creado TEXT NOT NULL,
            desde TEXT NOT NULL,
            hasta TEXT NOT NULL,
            canal TEXT NOT NULL,
            automatico INTEGER NOT NULL DEFAULT 0,
            envio TEXT NOT NULL DEFAULT '',
            datos_json TEXT NOT NULL
        );
        """)


def _dt(texto):
    return datetime.strptime(texto, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _estado(f):
    return conversation_service._estado_conversacion(f)


def _minutos(f):

    fin = f["cerrada"] or f["actualizada"]

    try:
        return max(0.0, (_dt(fin) - _dt(f["creada"])).total_seconds() / 60)
    except (TypeError, ValueError):
        return None


def _filas(desde_utc, hasta_utc, canal=None):

    sql = (
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM consultas_kb k WHERE k.conversacion_id = c.id) AS usos_kb, "
        "(SELECT SUM(voto) FROM valoraciones v WHERE v.conversacion_id = c.id) AS valor "
        "FROM conversaciones c WHERE c.creada >= ? AND c.creada < ?"
    )
    p = [desde_utc, hasta_utc]

    if canal:
        sql += " AND c.canal = ?"
        p.append(canal)

    sql += " ORDER BY c.creada"

    with _conectar() as db:
        return db.execute(sql, p).fetchall()


def _pct(parte, total):
    return round(parte * 100 / total, 1) if total else 0.0


def _agrupar(filas, clave):

    grupos = {}

    for f in filas:

        g = grupos.setdefault(clave(f), {"total": 0, "resueltas": 0, "derivadas": 0, "tokens_in": 0, "tokens_out": 0})

        g["total"] += 1
        g["resueltas"] += 1 if (not f["derivada"] and f["etapa"] == "Resuelto") else 0
        g["derivadas"] += 1 if f["derivada"] else 0
        g["tokens_in"] += f["tokens_in"] or 0
        g["tokens_out"] += f["tokens_out"] or 0

    salida = []

    for nombre, g in sorted(grupos.items(), key=lambda x: -x[1]["total"]):
        g["nombre"] = nombre
        g["pct_ia"] = _pct(g["resueltas"], g["total"])
        g["costo_usd"] = round(costo_usd(g["tokens_in"], g["tokens_out"]), 4)
        salida.append(g)

    return salida


def serie_diaria(dias=30, canal=None, hoy=None):
    """Una fila por día (hora de Argentina) con total / resueltas / derivadas / en curso."""

    hoy = (hoy or datetime.now(ARG)).replace(hour=0, minute=0, second=0, microsecond=0)

    primero = hoy - timedelta(days=dias - 1)

    filas = _filas(_utc(primero), _utc(hoy + timedelta(days=1)), canal)

    por_dia = {
        (primero + timedelta(days=i)).strftime("%Y-%m-%d"):
        {"dia": (primero + timedelta(days=i)).strftime("%Y-%m-%d"), "total": 0, "resueltas": 0, "derivadas": 0, "en_curso": 0}
        for i in range(dias)
    }

    for f in filas:

        dia = _dt(f["creada"]).astimezone(ARG).strftime("%Y-%m-%d")

        if dia not in por_dia:
            continue

        d = por_dia[dia]

        d["total"] += 1

        if f["derivada"]:
            d["derivadas"] += 1
        elif f["etapa"] == "Resuelto":
            d["resueltas"] += 1
        else:
            d["en_curso"] += 1

    return list(por_dia.values())


def metricas_avanzadas(dias=30, canal=None, hoy=None):
    """Tiempos de resolución, tokens y costo del período."""

    hoy = (hoy or datetime.now(ARG)).replace(hour=0, minute=0, second=0, microsecond=0)

    filas = _filas(_utc(hoy - timedelta(days=dias - 1)), _utc(hoy + timedelta(days=1)), canal)

    minutos = [
        m for f in filas
        if (not f["derivada"] and f["etapa"] == "Resuelto") and (m := _minutos(f)) is not None
    ]

    t_in = sum(f["tokens_in"] or 0 for f in filas)
    t_out = sum(f["tokens_out"] or 0 for f in filas)
    costo = costo_usd(t_in, t_out)

    return {
        "conversaciones": len(filas),
        "resueltas_n": len(minutos),
        "min_promedio": round(statistics.mean(minutos), 1) if minutos else None,
        "min_mediana": round(statistics.median(minutos), 1) if minutos else None,
        "tokens_in": t_in,
        "tokens_out": t_out,
        "costo_usd": round(costo, 4),
        "costo_por_conversacion": round(costo / len(filas), 4) if filas else 0.0,
        "ia_fallos": sum(1 for f in filas if f["ia_fallo"]),
        "por_producto": _agrupar(filas, lambda f: f["producto"] or "(sin producto)"),
        "por_agente": _agrupar(filas, lambda f: f["agente_nombre"] or "(sin agente)"),
    }


def brechas(dias=30, canal=None, hoy=None):
    """
    Consultas que el agente no pudo resolver por falta de documentación:
    conversaciones derivadas en las que no usó ningún documento (o el motivo
    dice que no hay procedimiento), agrupadas por producto y tipificación.
    No cuenta las derivaciones pedidas por el cliente ni las fallas de la IA.
    """

    hoy = (hoy or datetime.now(ARG)).replace(hour=0, minute=0, second=0, microsecond=0)

    filas = _filas(_utc(hoy - timedelta(days=dias - 1)), _utc(hoy + timedelta(days=1)), canal)

    grupos = {}

    for f in filas:

        if not f["derivada"] or f["ia_fallo"]:
            continue

        motivo = (f["motivo"] or "").lower()

        if "pidi" in motivo and "persona" in motivo:
            continue

        if f["usos_kb"] and not any(
            p in motivo for p in ("procedimiento", "document", "no tengo", "sin informaci")
        ):
            continue

        clave = (
            f["producto"] or "(sin producto)",
            f["categoria"] or "Sin clasificar",
            f["subcategoria"] or "",
        )

        g = grupos.setdefault(clave, {"consultas": 0, "ejemplos": [], "ultima": "", "negativas": 0})

        g["consultas"] += 1
        g["ultima"] = f["creada"]
        g["negativas"] += 1 if (f["valor"] or 0) < 0 else 0

        if f["problema"] and len(g["ejemplos"]) < 3 and f["problema"] not in g["ejemplos"]:
            g["ejemplos"].append(f["problema"][:140])

    return [
        {
            "producto": k[0], "categoria": k[1], "subcategoria": k[2],
            "consultas": g["consultas"], "ejemplos": g["ejemplos"],
            "ultima": g["ultima"].replace("T", " "), "negativas": g["negativas"],
        }
        for k, g in sorted(grupos.items(), key=lambda x: -x[1]["consultas"])
    ]


# ---------------- reporte semanal ----------------

def semana_anterior(ahora=None):
    """(lunes 00:00, lunes siguiente 00:00) de la semana ya terminada, hora ARG."""

    ahora = (ahora or datetime.now(ARG)).astimezone(ARG)

    este_lunes = (ahora - timedelta(days=ahora.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

    return este_lunes - timedelta(days=7), este_lunes


def calcular_reporte(desde, hasta, canal="whatsapp"):
    """desde / hasta: datetimes con zona. Solo conversaciones del canal indicado."""

    filas = _filas(_utc(desde), _utc(hasta), canal)

    total = len(filas)
    resueltas = sum(1 for f in filas if not f["derivada"] and f["etapa"] == "Resuelto")
    derivadas = sum(1 for f in filas if f["derivada"])
    fallos = sum(1 for f in filas if f["ia_fallo"])

    minutos = [m for f in filas if (not f["derivada"] and f["etapa"] == "Resuelto") and (m := _minutos(f)) is not None]

    t_in = sum(f["tokens_in"] or 0 for f in filas)
    t_out = sum(f["tokens_out"] or 0 for f in filas)

    sql_votos = (
        "SELECT SUM(CASE WHEN voto > 0 THEN 1 ELSE 0 END) AS p, "
        "SUM(CASE WHEN voto < 0 THEN 1 ELSE 0 END) AS n FROM valoraciones "
        "WHERE conversacion_id IN (SELECT id FROM conversaciones WHERE creada >= ? AND creada < ?"
        + (" AND canal = ?" if canal else "") + ")"
    )

    with _conectar() as db:
        v = db.execute(
            sql_votos, [_utc(desde), _utc(hasta)] + ([canal] if canal else [])
        ).fetchone()

    dias = max(1, round((hasta - desde).total_seconds() / 86400))

    return {
        "periodo_desde": desde.astimezone(ARG).strftime("%Y-%m-%d %H:%M"),
        "periodo_hasta": hasta.astimezone(ARG).strftime("%Y-%m-%d %H:%M"),
        "canal": canal,
        "consultas": total,
        "resueltas_por_ia": resueltas,
        "derivadas": derivadas,
        "en_curso": total - resueltas - derivadas,
        "pct_resolucion_ia": _pct(resueltas, total),
        "fallos_ia": fallos,
        "min_promedio": round(statistics.mean(minutos), 1) if minutos else None,
        "tokens_in": t_in,
        "tokens_out": t_out,
        "costo_usd": round(costo_usd(t_in, t_out), 4),
        "costo_por_conversacion": round(costo_usd(t_in, t_out) / total, 4) if total else 0.0,
        "votos_positivos": v["p"] or 0,
        "votos_negativos": v["n"] or 0,
        "por_producto": _agrupar(filas, lambda f: f["producto"] or "(sin producto)"),
        "por_agente": _agrupar(filas, lambda f: f["agente_nombre"] or "(sin agente)"),
        "brechas": brechas(dias=dias, canal=canal, hoy=hasta.astimezone(ARG) - timedelta(days=1))[:5],
    }


def texto_reporte(r):

    def fila(g):
        return f"  - {g['nombre']}: {g['total']} consultas, {g['resueltas']} resueltas por IA ({g['pct_ia']}%), {g['derivadas']} derivadas"

    lineas = [
        f"Reporte de agentes IA — {r['periodo_desde']} a {r['periodo_hasta']} (hora de Argentina)",
        f"Canal: {r['canal'] or 'todos'}",
        "",
        f"Consultas de soporte (tickets): {r['consultas']}",
        f"Resueltas solo por IA: {r['resueltas_por_ia']}",
        f"% de resolución por IA: {r['pct_resolucion_ia']}%",
        f"Derivadas a una persona: {r['derivadas']}  |  En curso: {r['en_curso']}",
        f"Fallas de IA (derivadas automáticamente): {r['fallos_ia']}",
        f"Tiempo promedio de resolución: {str(r['min_promedio']) + ' min' if r['min_promedio'] is not None else '—'}",
        f"Costo estimado: USD {r['costo_usd']} (USD {r['costo_por_conversacion']} por consulta)",
        f"Valoración del equipo: 👍 {r['votos_positivos']} / 👎 {r['votos_negativos']}",
        "",
        "Por producto:",
    ] + [fila(g) for g in r["por_producto"]] + ["", "Por agente:"] + [fila(g) for g in r["por_agente"][:10]]

    if r["brechas"]:
        lineas += ["", "Temas sin documentación (consultas que el agente no pudo resolver):"]
        lineas += [
            f"  - {b['producto']} / {b['categoria']}" + (f" / {b['subcategoria']}" if b["subcategoria"] else "") + f": {b['consultas']}"
            for b in r["brechas"]
        ]

    return "\n".join(lineas)


def guardar_reporte(r, desde, hasta, automatico=False, envio=""):

    with _LOCK, _conectar() as db:
        return db.execute(
            "INSERT INTO reportes (creado, desde, hasta, canal, automatico, envio, datos_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_utc(datetime.now(timezone.utc)), r["periodo_desde"], r["periodo_hasta"],
             (r["canal"] or "todos"), 1 if automatico else 0, envio, json.dumps(r, ensure_ascii=False))
        ).lastrowid


def listar_reportes(limite=12):

    with _conectar() as db:
        filas = db.execute("SELECT * FROM reportes ORDER BY id DESC LIMIT ?", (limite,)).fetchall()

    return [
        dict(id=f["id"], creado=f["creado"].replace("T", " "), desde=f["desde"], hasta=f["hasta"],
             automatico=bool(f["automatico"]), envio=f["envio"], datos=json.loads(f["datos_json"]))
        for f in filas
    ]


def obtener_reporte(reporte_id):

    with _conectar() as db:
        f = db.execute("SELECT * FROM reportes WHERE id = ?", (reporte_id,)).fetchone()

    return json.loads(f["datos_json"]) if f else None


def generar_y_enviar(desde, hasta, canal="whatsapp", automatico=False, enviar=True):
    """Calcula, guarda y (si se pide) envía el reporte. Devuelve (id, texto, envio)."""

    r = calcular_reporte(desde, hasta, canal)

    texto = texto_reporte(r)

    envio = ""

    if enviar:

        adjunto = ("reporte-semanal.xlsx", xlsx_reporte(r, desde, hasta, canal),
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        ok, errores = alertas_service.difundir(
            "📊 Reporte semanal de agentes IA", texto, [adjunto],
            webhook_var="REPORTE_WEBHOOK_URL", emails_var="REPORTE_EMAILS"
        )

        envio = ",".join(ok) + (f" (errores: {'; '.join(errores)})" if errores else "")

    return guardar_reporte(r, desde, hasta, automatico, envio), texto, envio


def iniciar_programador(intervalo=300):
    """
    Revisa cada tanto si ya corresponde mandar el reporte de la semana pasada
    (lunes desde las REPORTE_HORA, hora de Argentina; por defecto 8). Si la
    app estaba apagada, lo manda al volver. REPORTE_AUTOMATICO=0 lo apaga.
    """

    if os.environ.get("REPORTE_AUTOMATICO", "1").strip() in ("0", "no", "false"):
        return None

    def bucle():

        time.sleep(60)

        while True:

            try:

                ahora = datetime.now(ARG)

                hora = int(_flotante("REPORTE_HORA", 8))

                lunes = (ahora - timedelta(days=ahora.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

                clave = lunes.strftime("%Y-%m-%d")

                if ahora >= lunes + timedelta(hours=hora) and \
                        conversation_service.obtener_ajuste("reporte_semanal_ultimo", "") != clave:

                    # Se marca antes de enviar para no repetirlo si algo falla
                    conversation_service.guardar_ajuste("reporte_semanal_ultimo", clave)

                    desde, hasta = semana_anterior(ahora)

                    generar_y_enviar(desde, hasta, "whatsapp", automatico=True)

            except Exception as e:
                print(f"Reporte semanal: {e}")

            time.sleep(intervalo)

    hilo = threading.Thread(target=bucle, daemon=True, name="reporte-semanal")

    hilo.start()

    return hilo


# ---------------- exportaciones ----------------

COLUMNAS = [
    "id", "fecha_arg", "canal", "producto", "agente", "categoria", "subcategoria",
    "estado", "destino", "motivo", "mensajes", "minutos", "tokens_in", "tokens_out",
    "costo_usd", "valoracion", "falla_ia", "problema",
]


def conversaciones_filas(desde, hasta, canal=None):

    salida = []

    for f in _filas(_utc(desde), _utc(hasta), canal):

        m = _minutos(f)

        salida.append([
            f["id"], _dt(f["creada"]).astimezone(ARG).strftime("%Y-%m-%d %H:%M:%S"),
            f["canal"], f["producto"], f["agente_nombre"], f["categoria"], f["subcategoria"],
            _estado(f), f["destino"], f["motivo"], f["mensajes"],
            round(m, 1) if m is not None else "", f["tokens_in"] or 0, f["tokens_out"] or 0,
            round(costo_usd(f["tokens_in"], f["tokens_out"]), 5),
            (f["valor"] or 0), f["ia_fallo"] or 0, f["problema"],
        ])

    return salida


def csv_conversaciones(desde, hasta, canal=None):

    salida = io.StringIO()

    w = csv.writer(salida)

    w.writerow(COLUMNAS)
    w.writerows(conversaciones_filas(desde, hasta, canal))

    # BOM para que Excel respete las tildes
    return ("\ufeff" + salida.getvalue()).encode("utf-8")


def xlsx_reporte(r, desde, hasta, canal=None):
    """Excel con resumen, por producto, por agente, por día, conversaciones y brechas."""

    from openpyxl import Workbook
    from openpyxl.styles import Font

    libro = Workbook()

    def hoja(titulo, encabezado, filas, primera=False):

        h = libro.active if primera else libro.create_sheet()
        h.title = titulo
        h.append(encabezado)

        for c in h[1]:
            c.font = Font(bold=True)

        for fila in filas:
            h.append(fila)

        for col in h.columns:
            h.column_dimensions[col[0].column_letter].width = min(60, max(12, max(len(str(c.value or "")) for c in col) + 2))

        return h

    hoja("Resumen", ["Indicador", "Valor"], [
        ["Período (hora ARG)", f"{r['periodo_desde']} a {r['periodo_hasta']}"],
        ["Canal", r["canal"] or "todos"],
        ["Consultas de soporte", r["consultas"]],
        ["Resueltas solo por IA", r["resueltas_por_ia"]],
        ["% resolución por IA", r["pct_resolucion_ia"]],
        ["Derivadas", r["derivadas"]],
        ["En curso", r["en_curso"]],
        ["Fallas de IA", r["fallos_ia"]],
        ["Tiempo promedio de resolución (min)", r["min_promedio"] if r["min_promedio"] is not None else ""],
        ["Tokens de entrada", r["tokens_in"]],
        ["Tokens de salida", r["tokens_out"]],
        ["Costo estimado (USD)", r["costo_usd"]],
        ["Costo por consulta (USD)", r["costo_por_conversacion"]],
        ["Valoraciones 👍", r["votos_positivos"]],
        ["Valoraciones 👎", r["votos_negativos"]],
    ], primera=True)

    for titulo, clave in (("Por producto", "por_producto"), ("Por agente", "por_agente")):
        hoja(titulo, ["Nombre", "Consultas", "Resueltas por IA", "% IA", "Derivadas", "Costo USD"],
             [[g["nombre"], g["total"], g["resueltas"], g["pct_ia"], g["derivadas"], g["costo_usd"]] for g in r[clave]])

    dias = max(1, round((hasta - desde).total_seconds() / 86400))

    hoja("Por día", ["Día", "Consultas", "Resueltas", "Derivadas", "En curso"],
         [[d["dia"], d["total"], d["resueltas"], d["derivadas"], d["en_curso"]]
          for d in serie_diaria(dias, canal, hoy=hasta.astimezone(ARG) - timedelta(days=1))])

    hoja("Conversaciones", COLUMNAS, conversaciones_filas(desde, hasta, canal))

    hoja("Brechas KB", ["Producto", "Tipificación", "Subcategoría", "Consultas sin resolver", "👎", "Ejemplos"],
         [[b["producto"], b["categoria"], b["subcategoria"], b["consultas"], b["negativas"], " | ".join(b["ejemplos"])]
          for b in brechas(dias, canal, hoy=hasta.astimezone(ARG) - timedelta(days=1))])

    salida = io.BytesIO()

    libro.save(salida)

    return salida.getvalue()
