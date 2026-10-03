"""
Alertas para TI y avisos del sistema.

Canales (se configuran en Render con variables de entorno):
- Webhook (Slack, Teams, Google Chat...): ALERTA_WEBHOOK_URL. Se envía
  {"text": "..."}.
- Mail: ALERTA_EMAILS (separados por coma) + SMTP_HOST, SMTP_PORT (587),
  SMTP_USUARIO, SMTP_CLAVE, SMTP_REMITENTE y SMTP_TLS (1 por defecto).

Tipos de alerta:
- incidente: muchos clientes reportan lo mismo en poco tiempo.
- ia: falla de OpenAI (sin crédito, caído, lento, clave inválida).
- gasto: se superó el tope de gasto diario.
Cada alerta tiene un "enfriamiento": la misma no se repite hasta pasado ese tiempo.
"""

import os
import re
import smtplib
import sqlite3
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import requests

from services import conversation_service

_LOCK = threading.RLock()


def _entero(nombre, defecto):
    try:
        return int(os.environ.get(nombre, defecto))
    except ValueError:
        return defecto


def umbral():
    return _entero("ALERTA_UMBRAL", 5)


def ventana_min():
    return _entero("ALERTA_VENTANA_MIN", 30)


def enfriamiento_min():
    return _entero("ALERTA_ENFRIAMIENTO_MIN", 60)


def _ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _conectar():
    c = sqlite3.connect(conversation_service.DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init_tablas():

    with _LOCK, _conectar() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS alertas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            tipo TEXT NOT NULL,
            clave TEXT NOT NULL,
            titulo TEXT NOT NULL,
            detalle TEXT NOT NULL DEFAULT '',
            enviada TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_alertas_clave ON alertas (clave, fecha);
        """)


def canales():
    """Qué canales hay configurados (para mostrar en pantalla)."""

    emails = [e.strip() for e in os.environ.get("ALERTA_EMAILS", "").split(",") if e.strip()]

    return {
        "webhook": bool(os.environ.get("ALERTA_WEBHOOK_URL", "").strip()),
        "email": bool(emails and os.environ.get("SMTP_HOST", "").strip()),
        "destinatarios": emails,
    }


# ---------------- envío ----------------

def enviar_webhook(url, texto):

    r = requests.post(url, json={"text": texto}, timeout=10)

    if r.status_code >= 300:
        raise RuntimeError(f"webhook respondió {r.status_code}")


def enviar_email(destinos, asunto, texto, adjuntos=None):
    """adjuntos: lista de (nombre, bytes, mime)."""

    host = os.environ.get("SMTP_HOST", "").strip()
    puerto = _entero("SMTP_PORT", 587)
    usuario = os.environ.get("SMTP_USUARIO", "").strip()
    clave = os.environ.get("SMTP_CLAVE", "")
    remitente = os.environ.get("SMTP_REMITENTE", "").strip() or usuario or "agentes@localhost"

    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = remitente
    msg["To"] = ", ".join(destinos)
    msg.set_content(texto)

    for nombre, datos, mime in (adjuntos or []):
        tipo, _, sub = mime.partition("/")
        msg.add_attachment(datos, maintype=tipo, subtype=sub or "octet-stream", filename=nombre)

    with smtplib.SMTP(host, puerto, timeout=20) as s:

        if os.environ.get("SMTP_TLS", "1") != "0":
            s.starttls()

        if usuario:
            s.login(usuario, clave)

        s.send_message(msg)


def difundir(titulo, texto, adjuntos=None, webhook_var="ALERTA_WEBHOOK_URL",
             emails_var="ALERTA_EMAILS"):
    """Manda el aviso por todos los canales. Devuelve (canales_ok, errores)."""

    ok, errores = [], []

    url = (os.environ.get(webhook_var, "") or os.environ.get("ALERTA_WEBHOOK_URL", "")).strip()

    if url:
        try:
            enviar_webhook(url, f"{titulo}\n{texto}")
            ok.append("webhook")
        except Exception as e:
            errores.append(f"webhook: {e}")

    destinos = [
        e.strip() for e in
        (os.environ.get(emails_var, "") or os.environ.get("ALERTA_EMAILS", "")).split(",")
        if e.strip()
    ]

    if destinos and os.environ.get("SMTP_HOST", "").strip():
        try:
            enviar_email(destinos, titulo, texto, adjuntos)
            ok.append("email")
        except Exception as e:
            errores.append(f"email: {e}")

    return ok, errores


# ---------------- alertas ----------------

def notificar(tipo, clave, titulo, detalle="", asincrono=True, enfriamiento=None):
    """
    Registra la alerta y la envía, salvo que la misma clave ya haya
    alertado hace menos de `enfriamiento` minutos. Devuelve True si alertó.
    """

    minutos = enfriamiento_min() if enfriamiento is None else enfriamiento

    limite = (datetime.now(timezone.utc) - timedelta(minutes=minutos)).strftime("%Y-%m-%dT%H:%M:%S")

    with _LOCK, _conectar() as db:

        if db.execute(
            "SELECT 1 FROM alertas WHERE clave = ? AND fecha >= ?", (clave, limite)
        ).fetchone():
            return False

        alerta_id = db.execute(
            "INSERT INTO alertas (fecha, tipo, clave, titulo, detalle) VALUES (?, ?, ?, ?, ?)",
            (_ahora(), tipo, clave, titulo, detalle[:1500])
        ).lastrowid

    def enviar():

        ok, errores = difundir(f"⚠️ {titulo}", detalle)

        with _LOCK, _conectar() as db:
            db.execute(
                "UPDATE alertas SET enviada = ?, error = ? WHERE id = ?",
                (",".join(ok), "; ".join(errores)[:500], alerta_id)
            )

    if asincrono:
        threading.Thread(target=enviar, daemon=True).start()
    else:
        enviar()

    return True


def listar(limite=30):

    with _conectar() as db:
        return [dict(f) for f in db.execute(
            "SELECT * FROM alertas ORDER BY id DESC LIMIT ?", (limite,)
        )]


def _clave_tema(texto):

    texto = unicodedata.normalize("NFKD", str(texto or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))

    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


def detectar_incidente(producto, asincrono=True):
    """
    Mira las conversaciones del último rato de ese producto: si UN mismo tema
    (subcategoría, o categoría si no hay) juntó `umbral` consultas o más,
    avisa a TI. Devuelve el texto del aviso o ''.
    """

    desde = (datetime.now(timezone.utc) - timedelta(minutes=ventana_min())).strftime("%Y-%m-%dT%H:%M:%S")

    with _conectar() as db:
        filas = db.execute(
            "SELECT id, categoria, subcategoria, problema FROM conversaciones "
            "WHERE producto = ? AND canal = 'whatsapp' AND creada >= ?",
            (producto, desde)
        ).fetchall()

    temas = {}

    for f in filas:

        tema = _clave_tema(f["subcategoria"]) or _clave_tema(f["categoria"])

        if tema:
            temas.setdefault(tema, []).append(f)

    for tema, items in sorted(temas.items(), key=lambda x: -len(x[1])):

        if len(items) < umbral():
            break

        ejemplos = "\n".join(f"- {(i['problema'] or '(sin detalle)')[:120]}" for i in items[:3])

        titulo = (
            f"Posible incidente en {producto}: {len(items)} consultas sobre "
            f"«{items[0]['subcategoria'] or items[0]['categoria']}» en {ventana_min()} min"
        )

        if notificar("incidente", f"incidente:{producto}:{tema}", titulo, ejemplos, asincrono):
            return titulo

    return ""
