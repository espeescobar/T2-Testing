"""Agente de reparación de bugs: punto de entrada.

Uso (el entorno del bug debe estar instalado con setup.sh):
    python main.py <carpeta_del_bug>
    python main.py ../dataset/black-2

Flujo: localizar -> reparar -> escribir localizacion.json y metricas.json en <carpeta_del_bug>/.
La reparación queda aplicada en <carpeta_del_bug>/repo/, que es lo que se evalúa.

Puedes cambiar este archivo como quieras mientras se mantengan:
  - el comando de arriba,
  - el formato de localizacion.json (ver localizador.py) y de metricas.json (ver README).
"""
import json
import sys
import time
from pathlib import Path

from localizador import localizar
from reparador import reparar

MODELO = "gemini-3.5-flash-lite"

def main():
    if len(sys.argv) != 2:
        sys.exit("Uso: python main.py <carpeta_del_bug>")
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    t0 = time.perf_counter()

    # 1. Localizar
    localizacion = localizar(carpeta, info)
    """Agente de reparación de bugs: punto de entrada.

Uso (el entorno del bug debe estar instalado con setup.sh):
    python main.py <carpeta_del_bug>
    python main.py ../dataset/black-2

Flujo: localizar -> reparar -> escribir localizacion.json y metricas.json en <carpeta_del_bug>/.
La reparación queda aplicada en <carpeta_del_bug>/repo/, que es lo que se evalúa.

Hay un temporizador de seguridad: si a los 175 segundos no terminamos, se escriben los JSON igual
y se corta el programa (el limite del enunciado son 180 segundos).
"""
import json
import os
import sys
import threading
from pathlib import Path

from localizador import localizar, region_de_relleno
from reparador import reparar
import utils

MODELO = "gemini-3.5-flash-lite"
SEGUNDOS_DE_SEGURIDAD = 175


def guardar_metricas(carpeta: Path, id_bug: str):
    # las llamadas y los tokens se van sumando solos en utils.contadores
    metricas = {
        "bug": id_bug,
        "modelo": MODELO,
        "llamadas": utils.contadores["llamadas"],
        "tokens_entrada": utils.contadores["tokens_entrada"],
        "tokens_salida": utils.contadores["tokens_salida"],
        "segundos": round(utils.segundos_transcurridos(), 1),
    }
    (carpeta / "metricas.json").write_text(json.dumps(metricas, indent=2))


def guardar_localizacion(carpeta: Path, localizacion: dict):
    (carpeta / "localizacion.json").write_text(json.dumps(localizacion, indent=2, ensure_ascii=False))


def main():
    if len(sys.argv) != 2:
        sys.exit("Uso: python main.py <carpeta_del_bug>")
    utils.empezar_reloj()
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    repo = carpeta / "repo"
    estado = {"localizacion_guardada": False}

    # Temporizador de seguridad: si se acaba el tiempo guardamos lo que haya y salimos
    def cortar_por_tiempo():
        utils.matar_todos_los_procesos()
        if not estado["localizacion_guardada"]:
            guardar_localizacion(carpeta, {"regiones": region_de_relleno(repo)})
        guardar_metricas(carpeta, info["id"])
        print(f"{info['id']}: se acabó el tiempo", flush=True)
        os._exit(0)

    temporizador = threading.Timer(SEGUNDOS_DE_SEGURIDAD, cortar_por_tiempo)
    temporizador.daemon = True
    temporizador.start()

    resultado = {"resuelto": False}
    try:
        # Partimos siempre del codigo original
        utils.revertir_cambios(repo)
        # Mide en segundo plano que tests pasan con el codigo original (para detectar regresiones)
        utils.iniciar_linea_base(carpeta, info)

        # 1. Localizar
        try:
            localizacion = localizar(carpeta, info)
        except Exception as e:
            print(f"El localizador falló: {e}", flush=True)
            localizacion = {"regiones": region_de_relleno(repo)}
        guardar_localizacion(carpeta, localizacion)
        estado["localizacion_guardada"] = True

        # 2. Reparar
        resultado = reparar(carpeta, info, localizacion)
    except Exception as e:
        print(f"Error: {e}", flush=True)

    # 3. Guardar métricas (suma de las dos etapas)
    guardar_metricas(carpeta, info["id"])
    temporizador.cancel()
    print(f"{info['id']}: {'RESUELTO' if resultado.get('resuelto') else 'NO RESUELTO'} ({resultado.get('nota', '')})")
    os._exit(0)  # asi no esperamos a los hilos de fondo


if __name__ == "__main__":
    main()
    # Limpiamos los tokens temporales antes de guardar el JSON oficial
    # Limpiamos los tokens temporales antes de guardar el JSON oficial
    tokens_loc_in = localizacion.pop("_tokens_entrada", 0)
    tokens_loc_out = localizacion.pop("_tokens_salida", 0)
    llamadas_loc = 1
    
    (carpeta / "localizacion.json").write_text(json.dumps(localizacion, indent=2, ensure_ascii=False))

    # 2. Reparar
    resultado = reparar(carpeta, info, localizacion)
    
    tokens_rep_in = resultado.get("_tokens_entrada", 0)
    tokens_rep_out = resultado.get("_tokens_salida", 0)
    llamadas_rep = resultado.get("_llamadas", 1)

    # 3. Guardar métricas sumando ambas etapas
    metricas = {
        "bug": info["id"],
        "modelo": MODELO,
        "llamadas": llamadas_loc + llamadas_rep,
        "tokens_entrada": tokens_loc_in + tokens_rep_in,
        "tokens_salida": tokens_loc_out + tokens_rep_out,
        "segundos": round(time.perf_counter() - t0, 1),
    }
    (carpeta / "metricas.json").write_text(json.dumps(metricas, indent=2))
    print(f"{info['id']}: {'RESUELTO' if resultado.get('resuelto') else 'NO RESUELTO'}")

if __name__ == "__main__":
    main()