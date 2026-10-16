import ast
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from google import genai
from google.genai import types

MODELO = "gemini-3.5-flash-lite"
LIMITE_SEGUNDOS = 172  # el limite real es 180, dejamos un poco de margen

# Para saber cuanto tiempo llevamos
inicio_agente = time.time()

# Procesos que estan corriendo (para matarlos si se acaba el tiempo)
procesos_activos = []

# Aca se van sumando las llamadas y los tokens (los lee main.py para metricas.json)
contadores = {"llamadas": 0, "tokens_entrada": 0, "tokens_salida": 0}

# Cliente de Gemini (se crea una sola vez)
cliente = None

# Expresion para saber si un archivo es de test (es la misma de run_test.sh)
regex_test = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*$|_test\.py$|(^|/)conftest\.py$")

# Carpetas que no sirven cuando buscamos codigo del proyecto
carpetas_ignoradas = ["env", "venv", ".venv", ".git", "__pycache__", "build", "dist", "docs", "doc", ".tox", ".eggs"]


# ------------------------------------------------------------------
# Tiempo
# ------------------------------------------------------------------
def empezar_reloj():
    global inicio_agente
    inicio_agente = time.time()


def segundos_transcurridos() -> float:
    return time.time() - inicio_agente


def segundos_restantes() -> float:
    return LIMITE_SEGUNDOS - segundos_transcurridos()


# ------------------------------------------------------------------
# Correr comandos y tests
# ------------------------------------------------------------------
def correr_comando(comando: str, carpeta, timeout: float):
    """Corre un comando con bash y devuelve (codigo, salida). Si se pasa del timeout lo mata."""
    if timeout < 1:
        timeout = 1
    carpeta_cache = tempfile.mkdtemp()
    entorno = dict(os.environ)
    entorno["XDG_CACHE_HOME"] = carpeta_cache

    proceso = subprocess.Popen(["bash", "-c", comando], cwd=carpeta, env=entorno,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors="replace", start_new_session=True)
    procesos_activos.append(proceso)
    try:
        salida, _ = proceso.communicate(timeout=timeout)
        codigo = proceso.returncode
    except subprocess.TimeoutExpired:
        matar_proceso(proceso)
        salida, _ = proceso.communicate()
        codigo = -1
    procesos_activos.remove(proceso)
    shutil.rmtree(carpeta_cache, ignore_errors=True)
    return codigo, salida or ""


def matar_proceso(proceso):
    try:
        os.killpg(proceso.pid, signal.SIGKILL)
    except Exception:
        pass


def matar_todos_los_procesos():
    for proceso in list(procesos_activos):
        matar_proceso(proceso)


def recortar(texto: str, maximo: int) -> str:
    """Si el texto es muy largo dejamos el principio y sobre todo el final (donde sale el error)."""
    if len(texto) <= maximo:
        return texto
    principio = maximo // 4
    return texto[:principio] + "\n[... recortado ...]\n" + texto[-(maximo - principio):]


def ejecutar_tests(carpeta_bug: Path, timeout: float = 60) -> dict:
    """Ejecuta run_test.sh y devuelve si pasan y el traceback."""
    script = Path(carpeta_bug) / "run_test.sh"
    codigo, salida = correr_comando(f"bash '{script}'", carpeta_bug, timeout)
    return {
        "pasan": codigo == 0,
        "traceback": recortar(salida, 6000)
    }


# ------------------------------------------------------------------
# Git
# ------------------------------------------------------------------
def git(repo, *args) -> str:
    resultado = subprocess.run(["git"] + list(args), cwd=repo, capture_output=True, text=True, errors="replace")
    return resultado.stdout


def revertir_cambios(repo):
    """Deja el repo como estaba en el commit original."""
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo, capture_output=True)


def restaurar_tests(repo):
    """Por si algun test se modifico solo al ejecutarse."""
    archivos_test = []
    for archivo in git(repo, "ls-files").splitlines():
        if regex_test.search(archivo):
            archivos_test.append(archivo)
    for i in range(0, len(archivos_test), 200):
        subprocess.run(["git", "checkout", "-q", "--"] + archivos_test[i:i + 200], cwd=repo, capture_output=True)


# ------------------------------------------------------------------
# Gemini
# ------------------------------------------------------------------
def limpiar_json(texto: str) -> dict:
    """Convierte el texto que devuelve Gemini a diccionario (por si viene con ```)."""
    texto = (texto or "").strip()
    texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto).strip()
    try:
        return json.loads(texto)
    except Exception:
        # a veces hay texto antes o despues del JSON
        i = texto.find("{")
        j = texto.rfind("}")
        return json.loads(texto[i:j + 1])


def consultar_gemini_json(prompt: str, temperatura: float = 0.2) -> dict:
    """Envia el prompt a Gemini y devuelve la respuesta como diccionario.
    Si hay error 429/503 espera y vuelve a intentar (esperando cada vez mas) mientras quede tiempo."""
    global cliente
    if cliente is None:
        cliente = genai.Client()  # toma GEMINI_API_KEY del entorno

    espera = 2
    fallos_json = 0
    ultimo_error = ""

    while True:
        tiempo = segundos_restantes() - 5
        if tiempo < 6:
            raise RuntimeError(f"Se acabo el tiempo para llamar a Gemini ({ultimo_error})")

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=temperatura,
            max_output_tokens=8192,
            http_options=types.HttpOptions(timeout=int(min(70, tiempo) * 1000))
        )

        try:
            respuesta = cliente.models.generate_content(model=MODELO, contents=prompt, config=config)
        except Exception as e:
            ultimo_error = str(e)
            codigo = getattr(e, "code", None)
            print(f"   Error en la API ({codigo}), espero {espera}s y reintento")
            # errores como 400 o 403 no se arreglan reintentando
            if codigo in (400, 401, 403, 404):
                raise RuntimeError(f"Error de la API de Gemini: {e}")
            time.sleep(min(espera, max(0, tiempo - 6)))
            espera = min(espera * 2, 20)
            continue

        # La llamada termino con respuesta, asi que la contamos (aunque el JSON venga malo)
        contadores["llamadas"] += 1
        if respuesta.usage_metadata is not None:
            contadores["tokens_entrada"] += respuesta.usage_metadata.prompt_token_count or 0
            contadores["tokens_salida"] += respuesta.usage_metadata.candidates_token_count or 0

        try:
            resultado = limpiar_json(respuesta.text)
            if isinstance(resultado, dict):
                return resultado
            ultimo_error = "la respuesta no era un objeto JSON"
        except Exception:
            ultimo_error = "JSON invalido"

        fallos_json += 1
        if fallos_json >= 3:
            raise RuntimeError("Gemini devolvio una respuesta invalida 3 veces")


# ------------------------------------------------------------------
# Regresiones
# ------------------------------------------------------------------
# La "linea base" son los tests que pasaban con el codigo ORIGINAL.
# Se compara contra eso porque algunos tests ya fallaban antes de arreglar nada.
# Se calcula en segundo plano (en una copia del repo) mientras esperamos a Gemini.
linea_base = {"listo": threading.Event(), "tests": None, "iniciada": False}

# Tests que se rompieron en la ultima verificacion (para contarselo a Gemini)
tests_rotos = []


def tests_que_pasan(carpeta_codigo, carpeta_env, modulos: list, timeout: float):
    """Corre pytest con el python del entorno del bug y devuelve el conjunto de tests que pasan.
    Si no se pudo medir devuelve None."""
    archivo_xml = tempfile.mktemp(suffix=".xml")
    # si esta instalado pytest-timeout lo usamos para que un test colgado no se coma todo el tiempo
    opcion_timeout = "$(python -c 'import pytest_timeout' 2>/dev/null && echo --timeout=40)"
    comando = (f"source '{carpeta_env}/bin/activate' && python -m pytest -p no:cacheprovider -q "
               f"--continue-on-collection-errors {opcion_timeout} --junitxml={archivo_xml} {' '.join(modulos)}")
    codigo, salida = correr_comando(comando, carpeta_codigo, timeout)
    if codigo == -1:
        return None

    try:
        raiz = ET.parse(archivo_xml).getroot()
    except Exception:
        return None

    pasan = set()
    for caso in raiz.iter("testcase"):
        fallo = False
        for hijo in caso:
            if hijo.tag in ("failure", "error", "skipped"):
                fallo = True
        if not fallo:
            pasan.add(f"{caso.get('classname')}::{caso.get('name')}")
    os.remove(archivo_xml)
    return pasan


def calcular_linea_base(copia, repo, modulos):
    try:
        tiempo = min(110, max(10, segundos_restantes() - 15))
        linea_base["tests"] = tests_que_pasan(copia, repo / "env", modulos, tiempo)
    except Exception:
        linea_base["tests"] = None
    linea_base["listo"].set()
    shutil.rmtree(os.path.dirname(copia), ignore_errors=True)


def iniciar_linea_base(carpeta_bug: Path, info: dict):
    """Hace una copia del repo ORIGINAL y mide en segundo plano que tests pasan.
    Hay que llamarla antes de modificar el codigo."""
    if linea_base["iniciada"]:
        return
    linea_base["iniciada"] = True

    repo = Path(carpeta_bug) / "repo"
    modulos = info.get("modulos_test", [])
    if len(modulos) == 0:
        linea_base["listo"].set()
        return

    try:
        carpeta_tmp = tempfile.mkdtemp()
        copia = os.path.join(carpeta_tmp, "repo")

        # no copiamos el entorno virtual ni .git (el entorno original se usa igual)
        def ignorar(carpeta, nombres):
            ignorados = []
            for nombre in nombres:
                if nombre == "__pycache__":
                    ignorados.append(nombre)
                if carpeta == str(repo) and nombre in ("env", ".git"):
                    ignorados.append(nombre)
            return ignorados

        shutil.copytree(str(repo), copia, symlinks=True, ignore=ignorar)
        hilo = threading.Thread(target=calcular_linea_base, args=(copia, repo, modulos), daemon=True)
        hilo.start()
    except Exception:
        linea_base["listo"].set()


def verificar_regresiones(carpeta_bug: Path, info: dict) -> bool:
    """Devuelve True si el arreglo no rompio ningun test que pasaba antes.
    Tambien devuelve True si no se pudo medir (por ejemplo si no queda tiempo)."""
    tests_rotos.clear()
    repo = Path(carpeta_bug) / "repo"
    modulos = info.get("modulos_test", [])
    if len(modulos) == 0:
        return True

    iniciar_linea_base(carpeta_bug, info)
    linea_base["listo"].wait(max(0, segundos_restantes() - 45))
    if not linea_base["listo"].is_set() or not linea_base["tests"]:
        print("   (no hay linea base, no se revisan regresiones)")
        return True

    tiempo = min(100, segundos_restantes() - 12)
    if tiempo < 15:
        return True

    restaurar_tests(repo)
    despues = tests_que_pasan(repo, repo / "env", modulos, tiempo)
    if despues is None:
        return True

    for test in sorted(linea_base["tests"] - despues):
        tests_rotos.append(test)
    return len(tests_rotos) == 0


# ------------------------------------------------------------------
# Leer el codigo del repo
# ------------------------------------------------------------------
def listar_archivos_fuente(repo) -> list:
    """Todos los .py del proyecto (sin tests ni carpetas ignoradas), con ruta relativa a repo/."""
    archivos = []
    for carpeta, subcarpetas, nombres in os.walk(repo):
        subcarpetas[:] = [c for c in subcarpetas if c not in carpetas_ignoradas]
        for nombre in nombres:
            if nombre.endswith(".py"):
                ruta = os.path.relpath(os.path.join(carpeta, nombre), repo).replace(os.sep, "/")
                if not regex_test.search(ruta):
                    archivos.append(ruta)
    return archivos


def leer_lineas(repo, archivo: str) -> list:
    texto = (Path(repo) / archivo).read_text(encoding="utf-8", errors="replace")
    return texto.splitlines()


def buscar_funciones_en_arbol(nodo, prefijo: str, archivo: str, lista: list):
    """Recorre el arbol del archivo y guarda cada funcion/clase con su linea de inicio y de fin."""
    for hijo in ast.iter_child_nodes(nodo):
        if not isinstance(hijo, ast.stmt):
            continue
        if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nombre = prefijo + hijo.name
            inicio = hijo.lineno
            for decorador in hijo.decorator_list:
                inicio = min(inicio, decorador.lineno)
            tipo = "funcion"
            if isinstance(hijo, ast.ClassDef):
                tipo = "clase"
            lista.append({"archivo": archivo, "nombre": nombre, "tipo": tipo,
                          "inicio": inicio, "fin": hijo.end_lineno})
            buscar_funciones_en_arbol(hijo, nombre + ".", archivo, lista)
        else:
            buscar_funciones_en_arbol(hijo, prefijo, archivo, lista)


# Guardamos el resultado para no recorrer el repo cada vez
funciones_guardadas = {}


def listar_funciones(repo) -> list:
    """Lista de todas las funciones y clases del proyecto."""
    if str(repo) in funciones_guardadas:
        return funciones_guardadas[str(repo)]
    lista = []
    for archivo in listar_archivos_fuente(repo):
        try:
            texto = (Path(repo) / archivo).read_text(encoding="utf-8", errors="replace")
            arbol = ast.parse(texto)
        except Exception:
            continue
        buscar_funciones_en_arbol(arbol, "", archivo, lista)
    funciones_guardadas[str(repo)] = lista
    return lista


def ruta_del_proyecto(repo, ruta: str):
    """Convierte una ruta del traceback en ruta relativa a repo/.
    Si no es codigo del proyecto (libreria, test, etc.) devuelve None."""
    repo_absoluto = Path(repo).resolve()
    p = Path(ruta)
    if not p.is_absolute():
        p = repo_absoluto / p
    try:
        p = p.resolve()
        relativa = p.relative_to(repo_absoluto).as_posix()
    except Exception:
        return None
    if not p.is_file():
        return None
    for parte in relativa.split("/"):
        if parte in carpetas_ignoradas:
            return None
    if regex_test.search(relativa):
        return None
    return relativa


def sacar_frames(salida: str, repo) -> list:
    """Busca en la salida de los tests las lineas de codigo del proyecto que salen en el traceback.
    Devuelve una lista de (archivo, linea) desde la mas externa a la mas interna."""
    encontrados = []
    # formato de python/unittest:  File "ruta", line 12
    for m in re.finditer(r'File "([^"]+)", line (\d+)', salida):
        encontrados.append((m.start(), m.group(1), int(m.group(2))))
    # formato de pytest:  ruta/archivo.py:12: Error
    for m in re.finditer(r'^\s*([^\s:"]+\.py):(\d+):', salida, re.M):
        encontrados.append((m.start(), m.group(1), int(m.group(2))))
    encontrados.sort()

    frames = []
    for _, ruta, linea in encontrados:
        relativa = ruta_del_proyecto(repo, ruta)
        if relativa is not None and (relativa, linea) not in frames:
            frames.append((relativa, linea))
    return frames


def numerar_lineas(lineas: list, inicio: int, fin: int) -> str:
    """Devuelve el codigo con el numero de linea al lado."""
    texto = ""
    for i in range(inicio, fin + 1):
        texto += str(i).rjust(5) + "| " + lineas[i - 1] + "\n"
    return texto.rstrip("\n")