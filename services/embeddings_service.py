"""
Búsqueda semántica (por significado) con embeddings.

Cada fragmento de cada documento se convierte en un vector una sola vez y se
guarda en la base. Al consultar, se convierte la pregunta del cliente en un
vector y se buscan los fragmentos más parecidos en significado: "no me deja
facturar" encuentra el manual del "error de CAE" aunque no compartan palabras.
Se combina con la búsqueda por palabras clave (ver knowledge_service).

Si algo falla (sin crédito, sin modelo de embeddings, etc.) el agente sigue
funcionando solo con palabras clave.
"""

import hashlib
import os
import re
import sqlite3
import threading

import numpy as np

from services import conversation_service, knowledge_service

_LOCK = threading.RLock()
_CACHE = {}          # doc_id -> {indice: (hash, vector)}

MAX_CARACTERES = 6000
LOTE = 64


def _conectar():
    c = sqlite3.connect(conversation_service.DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init_tablas():

    with _LOCK, _conectar() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS embeddings (
            doc_id TEXT NOT NULL,
            indice INTEGER NOT NULL,
            hash TEXT NOT NULL,
            modelo TEXT NOT NULL,
            vec BLOB NOT NULL,
            PRIMARY KEY (doc_id, indice)
        );
        """)


def _hash(texto):
    return hashlib.md5(texto.encode("utf-8")).hexdigest()


def sim_min():
    try:
        return float(os.environ.get("EMBEDDINGS_SIM_MIN", "0.32"))
    except ValueError:
        return 0.32


class Embedder:
    """Convierte textos en vectores normalizados (numpy float32)."""

    def __init__(self, client, modelo, dimensiones=None, simulado=False):
        self.client = client
        self.modelo = modelo
        self.dimensiones = dimensiones
        self.simulado = simulado
        self.tokens = 0

    def _simulado(self, textos, dim=256):

        salida = np.zeros((len(textos), dim), dtype=np.float32)

        for n, texto in enumerate(textos):
            for token in knowledge_service.tokenizar(texto):
                salida[n, int(hashlib.md5(token.encode()).hexdigest(), 16) % dim] += 1.0

        return salida

    def embed(self, textos):

        if not textos:
            return np.zeros((0, 1), dtype=np.float32)

        if self.simulado:
            vectores = self._simulado(textos)
        else:

            vectores = []

            for i in range(0, len(textos), LOTE):

                lote = [t[:MAX_CARACTERES] or " " for t in textos[i:i + LOTE]]

                params = {"model": self.modelo, "input": lote}

                if self.dimensiones:
                    params["dimensions"] = self.dimensiones

                try:
                    r = self.client.embeddings.create(**params)
                except Exception as e:
                    # Modelos que no aceptan "dimensions" (por ejemplo ada-002)
                    if "dimension" in str(e).lower() and "dimensions" in params:
                        params.pop("dimensions")
                        r = self.client.embeddings.create(**params)
                    else:
                        raise

                vectores += [d.embedding for d in r.data]

                self.tokens += int(getattr(getattr(r, "usage", None), "total_tokens", 0) or 0)

            vectores = np.array(vectores, dtype=np.float32)

        normas = np.linalg.norm(vectores, axis=1, keepdims=True)

        normas[normas == 0] = 1.0

        return vectores / normas


# ---------------- índice ----------------

def indexar_documento(doc, embedder, contenido):
    """Embebe los fragmentos nuevos o cambiados. Devuelve cuántos calculó."""

    doc_id = doc.get("id")

    fragmentos = [t for _, t in knowledge_service.dividir_en_fragmentos(contenido)]

    hashes = [_hash(t) for t in fragmentos]

    with _conectar() as db:
        guardados = {
            f["indice"]: (f["hash"], f["modelo"])
            for f in db.execute(
                "SELECT indice, hash, modelo FROM embeddings WHERE doc_id = ?", (doc_id,)
            )
        }

    faltan = [
        i for i, h in enumerate(hashes)
        if guardados.get(i) != (h, embedder.modelo)
    ]

    if faltan:

        vectores = embedder.embed([fragmentos[i] for i in faltan])

        with _LOCK, _conectar() as db:

            for i, v in zip(faltan, vectores):
                db.execute(
                    "INSERT OR REPLACE INTO embeddings (doc_id, indice, hash, modelo, vec) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (doc_id, i, hashes[i], embedder.modelo, v.astype(np.float32).tobytes())
                )

    with _LOCK, _conectar() as db:
        db.execute("DELETE FROM embeddings WHERE doc_id = ? AND indice >= ?", (doc_id, len(fragmentos)))

    _CACHE.pop(doc_id, None)

    return len(faltan)


def borrar_documento(doc_id):

    with _LOCK, _conectar() as db:
        db.execute("DELETE FROM embeddings WHERE doc_id = ?", (doc_id,))

    _CACHE.pop(doc_id, None)


def _vectores_de(doc_id):

    if doc_id in _CACHE:
        return _CACHE[doc_id]

    with _conectar() as db:
        datos = {
            f["indice"]: (f["hash"], np.frombuffer(f["vec"], dtype=np.float32))
            for f in db.execute(
                "SELECT indice, hash, vec FROM embeddings WHERE doc_id = ?", (doc_id,)
            )
        }

    _CACHE[doc_id] = datos

    return datos


def estado(documentos, armar_contenido):
    """(fragmentos indexados, fragmentos totales de documentos procesados)."""

    total = 0

    for doc in documentos:

        if doc.get("procesamiento", "PROCESADO") != "PROCESADO" or doc.get("estado") == "Archivado":
            continue

        total += len(knowledge_service.dividir_en_fragmentos(armar_contenido(doc)))

    with _conectar() as db:
        indexados = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    return indexados, total


def indexar_todo(documentos, embedder, armar_contenido):
    """Indexa lo que falte de todos los documentos procesados. Devuelve cuántos."""

    nuevos = 0

    for doc in documentos:

        if doc.get("procesamiento", "PROCESADO") != "PROCESADO" or doc.get("estado") == "Archivado":
            continue

        try:
            nuevos += indexar_documento(doc, embedder, armar_contenido(doc))
        except Exception as e:
            print(f"[{doc.get('id')}] Error indexando: {e}")
            break    # lo más probable es que se repita en todos (sin crédito, etc.)

    return nuevos


# ---------------- consulta ----------------

def crear_similitud(embedder, consulta, doc_ids):
    """
    Devuelve similitud(doc_id, indice, texto) para esa consulta, o None si no
    se pudo calcular (entonces la búsqueda sigue solo por palabras clave).
    """

    if embedder is None or not consulta.strip():
        return None

    try:
        q = embedder.embed([consulta])[0]
    except Exception as e:
        print(f"Embeddings: no se pudo consultar ({e})")
        return None

    sims = {}

    for doc_id in doc_ids:

        for indice, (h, vec) in _vectores_de(doc_id).items():

            if vec.shape == q.shape:
                sims[(doc_id, indice)] = (h, float(vec @ q))

    def similitud(doc_id, indice, texto):

        dato = sims.get((doc_id, indice))

        if dato and dato[0] == _hash(texto):
            return dato[1]

        return None

    return similitud
