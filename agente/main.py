"""Agente de reparación de bugs: punto de entrada.

Uso (el entorno del bug debe estar instalado con setup.sh):
    python main.py <carpeta_del_bug>
    python main.py ../dataset/black-2

Flujo: localizar -> reparar -> escribir localizacion.json y metricas.json en <carpeta_del_bug>/.
La reparación queda aplicada en <carpeta_del_bug>/repo/, que es lo que se evalúa.

El enunciado corta a los 180 segundos (sin contar setup.sh). A los 174 el temporizador
deja el repo en un estado definitivo: conserva la reparación si ya pasó los tests y
las regresiones, y si no, la revierte. Después escribe los JSON y termina.
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
    (carpeta / "metricas.json").write_text(json.dumps(metricas, indent=2) + "\n")


def guardar_localizacion(carpeta: Path, localizacion: dict):
    (carpeta / "localizacion.json").write_text(
        json.dumps(localizacion, indent=2, ensure_ascii=False) + "\n"
    )


def main():
    if len(sys.argv) != 2:
        sys.exit("Uso: python main.py <carpeta_del_bug>")
    utils.empezar_reloj()
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    repo = carpeta / "repo"
    estado = {"localizacion_guardada": False}

    # Temporizador de seguridad: cierra antes de que el corrector mate el proceso.
    def cortar_por_tiempo():
        utils.matar_todos_los_procesos()
        decision = utils.revertir_si_no_validada(repo)
        if not estado["localizacion_guardada"]:
            guardar_localizacion(carpeta, {"regiones": region_de_relleno(repo)})
        guardar_metricas(carpeta, info["id"])
        print(f"{info['id']}: se acabó el tiempo (reparación {decision})", flush=True)
        os._exit(0)

    temporizador = threading.Timer(utils.SEGUNDOS_DE_SEGURIDAD, cortar_por_tiempo)
    temporizador.daemon = True
    temporizador.start()

    resultado = {"resuelto": False}
    try:
        # Partimos siempre del codigo original. La linea base arranca dentro del
        # localizador, despues del test que falla y antes de llamar a Gemini.
        utils.revertir_cambios(repo)
        try:
            localizacion = localizar(carpeta, info)
        except Exception as e:
            print(f"El localizador falló: {e}", flush=True)
            localizacion = {"regiones": region_de_relleno(repo)}
        guardar_localizacion(carpeta, localizacion)
        estado["localizacion_guardada"] = True
        resultado = reparar(carpeta, info, localizacion)
    except Exception as e:
        print(f"Error: {e}", flush=True)
        utils.revertir_si_no_validada(repo)

    guardar_metricas(carpeta, info["id"])
    temporizador.cancel()
    print(
        f"{info['id']}: {'RESUELTO' if resultado.get('resuelto') else 'NO RESUELTO'} ({resultado.get('nota', '')})",
        flush=True,
    )
    os._exit(0)


if __name__ == "__main__":
    main()
