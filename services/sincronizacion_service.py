"""
Sincronización de las carpetas conectadas con la Knowledge Base.

Por cada archivo de la carpeta remota:
- nuevo       -> se descarga y se crea el documento (queda "Pendiente de
                 revisión" hasta que alguien lo confirme)
- modificado  -> se descarga de nuevo, se reprocesa y vuelve a "Pendiente"
- sin cambios -> no se toca
- quitado de la carpeta -> el documento se archiva (no se borra)

La parte que escribe en la KB la pone la aplicación (`host`), para no
depender de app.py desde acá.
"""

import os
import threading
import time
import uuid
from datetime import datetime, timezone

from werkzeug.utils import secure_filename

from services import conexiones_service, proveedores_nube
from services.proveedores_nube import ErrorProveedor


MAX_ERRORES_VISIBLES = 5


def _mb(valor):
    return max(1, int(valor)) * 1024 * 1024


def _producto_de(conexion, remoto, normalizar_producto):
    """Producto fijo de la conexión o, en 'auto', el de la primera subcarpeta."""

    fijo = (conexion.get("producto") or "auto").strip()

    if fijo and fijo != "auto":
        return fijo

    primera = (remoto.ruta or "").split("/")[0]

    return normalizar_producto(primera, "") or ""


def sincronizar(conexion_id, host, http=None, max_mb=40):
    """
    Sincroniza una conexión. Devuelve un dict con el resumen o con 'error'.
    `host` aporta: ruta_pdf(doc_id, nombre), existe(doc_id), crear(...),
    actualizar(...), archivar(doc_id), normalizar_producto(valor, defecto).
    """

    conexion = conexiones_service.obtener(conexion_id)

    if not conexion:
        return {"error": "No se encontró la conexión."}

    if not conexiones_service.iniciar_sync(conexion_id):
        return {"error": "Ya hay una sincronización en curso."}

    nuevos = actualizados = archivados = sin_producto = 0
    errores = []

    try:

        secreto = conexiones_service.obtener_secreto(conexion_id)

        if not secreto:
            raise ErrorProveedor(
                "No se pueden leer las credenciales guardadas. Volvé a "
                "cargarlas en esta conexión."
            )

        proveedor = proveedores_nube.crear_proveedor(
            conexion["tipo"], conexion["url"], secreto, http
        )

        remotos = proveedor.listar()

        mapa = conexiones_service.archivos_mapeados(conexion_id)

        # Protección: una carpeta que de golpe viene vacía suele ser un
        # problema de permisos, no que se hayan borrado todos los manuales.
        if not remotos and mapa:
            raise ErrorProveedor(
                f"La carpeta devolvió 0 archivos pero antes había {len(mapa)}. "
                "No se archivó nada: revisá los permisos de la cuenta."
            )

        vistos = set()

        for remoto in remotos:

            vistos.add(remoto.id)

            previo = mapa.get(remoto.id)

            existe = bool(previo) and host.existe(previo["doc_id"])

            if existe and previo["firma"] == remoto.firma:
                continue

            try:

                doc_id = previo["doc_id"] if existe else uuid.uuid4().hex

                nombre_pdf = secure_filename(remoto.nombre) or "documento.pdf"

                if not nombre_pdf.lower().endswith(".pdf"):
                    nombre_pdf += ".pdf"

                ruta_pdf = host.ruta_pdf(doc_id, nombre_pdf)

                # Se descarga a un archivo temporal: si falla, el PDF que ya
                # estaba guardado (en una actualización) no se pierde.
                temporal = ruta_pdf + ".part"

                try:
                    proveedor.descargar(remoto, temporal, _mb(max_mb))
                    os.replace(temporal, ruta_pdf)
                finally:
                    if os.path.exists(temporal):
                        os.remove(temporal)

                datos = {
                    "nombre": os.path.splitext(remoto.nombre)[0].strip(),
                    "archivo": nombre_pdf,
                    "producto": _producto_de(
                        conexion, remoto, host.normalizar_producto
                    ),
                    "ruta_origen": remoto.ruta,
                    "origen": {
                        "conexion_id": conexion_id,
                        "conexion": conexion["nombre"],
                        "tipo": conexion["tipo"],
                        "ruta": remoto.ruta,
                    },
                    "tipificar_ia": bool(conexion["tipificar_ia"]),
                    "vigente_auto": bool(conexion["auto_vigente"]),
                }

                if existe:
                    host.actualizar(doc_id, ruta_pdf, datos)
                    actualizados += 1
                else:
                    estado = host.crear(doc_id, ruta_pdf, datos)
                    nuevos += 1

                    # Con "Vigente automático", los que no tienen producto
                    # quedan Pendientes (se usarían para todos los productos)
                    if datos["vigente_auto"] and estado != "Vigente":
                        sin_producto += 1

                conexiones_service.guardar_mapa(
                    conexion_id, remoto.id, doc_id, remoto.firma,
                    remoto.nombre, remoto.ruta
                )

            except ErrorProveedor as e:
                errores.append(f"{remoto.nombre}: {e}")

            except Exception as e:
                errores.append(f"{remoto.nombre}: {e}")

        # Archivos que ya no están en la carpeta
        for remoto_id, fila in mapa.items():

            if remoto_id in vistos:
                continue

            host.archivar(fila["doc_id"])

            conexiones_service.borrar_mapa(conexion_id, remoto_id)

            archivados += 1

        partes = [
            f"{nuevos} nuevo(s)",
            f"{actualizados} actualizado(s)",
            f"{archivados} archivado(s)",
            f"{len(errores)} con error",
        ]

        if sin_producto:
            partes.append(
                f"{sin_producto} sin producto (quedaron Pendientes: "
                "definí su producto y confirmalos)"
            )

        resultado = ", ".join(partes)

        detalle = "; ".join(errores[:MAX_ERRORES_VISIBLES])

        if len(errores) > MAX_ERRORES_VISIBLES:
            detalle += f" (y {len(errores) - MAX_ERRORES_VISIBLES} más)"

        conexiones_service.terminar_sync(conexion_id, resultado, detalle)

        return {
            "nuevos": nuevos, "actualizados": actualizados,
            "archivados": archivados, "errores": errores,
            "resultado": resultado,
        }

    except ErrorProveedor as e:

        conexiones_service.terminar_sync(conexion_id, "", str(e))

        return {"error": str(e)}

    except Exception as e:

        conexiones_service.terminar_sync(conexion_id, "", f"Error inesperado: {e}")

        return {"error": f"Error inesperado: {e}"}


def probar(conexion_id, http=None):
    """Prueba la conexión sin descargar nada. (mensaje, ok)."""

    conexion = conexiones_service.obtener(conexion_id)

    if not conexion:
        return "No se encontró la conexión.", False

    secreto = conexiones_service.obtener_secreto(conexion_id)

    if not secreto:
        return (
            "No se pueden leer las credenciales guardadas. Volvé a "
            "cargarlas en esta conexión.", False
        )

    try:

        proveedor = proveedores_nube.crear_proveedor(
            conexion["tipo"], conexion["url"], secreto, http
        )

        nombre, cantidad = proveedor.probar()

        return (
            f"Conexión correcta: carpeta «{nombre}», {cantidad} archivo(s) "
            "PDF/Word/Docs encontrados.", True
        )

    except ErrorProveedor as e:
        return str(e), False

    except Exception as e:
        return f"No se pudo conectar: {e}", False


def _parsear(fecha):

    try:
        return datetime.strptime(fecha, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (ValueError, TypeError):
        return 0


def iniciar_programador(encolar, intervalo_minutos):
    """
    Hilo que cada minuto revisa qué conexiones con sincronización automática
    ya cumplieron el intervalo y las manda a sincronizar. `encolar(id)` es
    quien las ejecuta. Con intervalo 0 no se inicia.
    """

    if intervalo_minutos <= 0:
        return None

    def bucle():

        time.sleep(45)

        while True:

            try:

                ahora = time.time()

                for c in conexiones_service.listar():

                    if not (c["activa"] and c["auto"]) or c["sincronizando"]:
                        continue

                    if ahora - _parsear(c["ultimo_sync"]) >= intervalo_minutos * 60:
                        encolar(c["id"])

            except Exception as e:
                print(f"Programador de sincronización: {e}")

            time.sleep(60)

    hilo = threading.Thread(target=bucle, daemon=True, name="sync-programador")

    hilo.start()

    return hilo
