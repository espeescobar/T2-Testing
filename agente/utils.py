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
# El enunciado cuenta 180 segundos desde que arranca `python main.py` hasta que
# termina. Entran la localización, las llamadas a Gemini, los tests del bug y
# las dos pasadas de regresión (línea base y comprobación). No entra setup.sh:
# el entorno ya está instalado. Si el proceso no sale antes de los 180, el
# corrector lo mata y el bug queda sin reparar, así que el cierre tiene que
# ocurrir antes y dejar el repo en un estado definitivo.
LIMITE_ENUNCIADO = 180
SEGUNDOS_DE_SEGURIDAD = 174  # revertir o conservar, y escribir los JSON
LIMITE_SEGUNDOS = 168        # no se empieza un trabajo que no quepa antes del cierre
MARGEN_CIERRE = 4

# Para saber cuanto tiempo llevamos
inicio_agente = time.time()

# Procesos que estan corriendo (para matarlos si se acaba el tiempo)
procesos_activos = []

# Aca se van sumando las llamadas y los tokens (los lee main.py para metricas.json)
contadores = {"llamadas": 0, "tokens_entrada": 0, "tokens_salida": 0}

# La reparación solo queda en disco si pasó el test del bug y las regresiones.
# El temporizador y el reparador comparten este estado.
candado_repo = threading.Lock()
_repo_ok = {"validada": False, "cerrado": False}
medicion = {"test_bug": None}

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
    """Arranca el reloj y limpia el estado de una ejecucion anterior (mismo proceso)."""
    global inicio_agente
    inicio_agente = time.time()
    contadores["llamadas"] = 0
    contadores["tokens_entrada"] = 0
    contadores["tokens_salida"] = 0
    linea_base["listo"].clear()
    linea_base["tests"] = None
    linea_base["completo"] = False
    linea_base["iniciada"] = False
    linea_base["cancelada"] = False
    linea_base["duracion"] = None
    linea_base["holder"] = []
    tests_rotos.clear()
    funciones_guardadas.clear()
    with candado_repo:
        _repo_ok["validada"] = False
        _repo_ok["cerrado"] = False
    medicion["test_bug"] = None


def segundos_transcurridos() -> float:
    return time.time() - inicio_agente


def segundos_restantes() -> float:
    return LIMITE_SEGUNDOS - segundos_transcurridos()


def estimado_test(duracion) -> float:
    """Cuánto reservar para volver a correr el test del bug, según lo que ya tardó."""
    if not duracion or duracion <= 0:
        return 12.0
    return max(8.0, float(duracion) * 1.25)


def reserva_regresion(duracion, completo: bool):
    """Segundos para repetir la suite. Crece con la duración medida: no hay un techo fijo.
    None si la línea base no llegó a completarse (no se puede aceptar un arreglo)."""
    if not completo or duracion is None:
        return None
    if duracion <= 0:
        return 0.0
    return float(duracion) * 1.5 + 10


def presupuesto_linea_base(restantes: float) -> float:
    """Tope de la primera pasada. La segunda pasada tiene que caber después,
    junto con un intento de reparación, dentro del presupuesto de trabajo."""
    libre = restantes - 15 - estimado_test(None) - MARGEN_CIERRE
    if libre < 16:
        return 0.0
    return libre / 2


def puede_llamar_modelo(restantes, base_lista, duracion_base, completo, duracion_test) -> bool:
    """False si una llamada nueva no dejaría tiempo para el test y las regresiones."""
    if restantes < MARGEN_CIERRE + 12:
        return False
    if not base_lista:
        return True
    reserva = reserva_regresion(duracion_base, completo)
    if reserva is None:
        return False
    return restantes > 15 + estimado_test(duracion_test) + reserva + MARGEN_CIERRE


def puede_editar(restantes, duracion_base, completo, duracion_test) -> bool:
    """False si, hecha ya la llamada, no cabe el test del bug más la regresión."""
    reserva = reserva_regresion(duracion_base, completo)
    if reserva is None:
        return False
    return restantes > estimado_test(duracion_test) + reserva + MARGEN_CIERRE


def marcar_reparacion_validada() -> bool:
    """El parche ya pasó todas las comprobaciones y tiene que sobrevivir al temporizador."""
    with candado_repo:
        if _repo_ok["cerrado"]:
            return False
        _repo_ok["validada"] = True
        return True


def reparacion_esta_validada() -> bool:
    return _repo_ok["validada"]


def repo_esta_cerrado() -> bool:
    return _repo_ok["cerrado"]


def revertir_si_no_validada(repo) -> str:
    """El temporizador de seguridad. Conserva el repo solo si la reparación ya se confirmó."""
    with candado_repo:
        if _repo_ok["validada"]:
            return "conservada"
        _repo_ok["cerrado"] = True
    revertir_cambios(repo)
    return "revertida"


# ------------------------------------------------------------------
# Correr comandos y tests
# ------------------------------------------------------------------
def correr_comando(comando: str, carpeta, timeout: float, titular=None):
    """Corre un comando con bash y devuelve (codigo, salida). Si se pasa del timeout lo mata.
    Si se pasa `titular` (una lista), el proceso se anota ahi para poder cancelarlo aparte."""
    if timeout < 1:
        timeout = 1
    carpeta_cache = tempfile.mkdtemp()
    entorno = dict(os.environ)
    entorno["XDG_CACHE_HOME"] = carpeta_cache

    proceso = subprocess.Popen(["bash", "-c", comando], cwd=carpeta, env=entorno,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors="replace", start_new_session=True)
    procesos_activos.append(proceso)
    if titular is not None:
        titular.append(proceso)
    try:
        salida, _ = proceso.communicate(timeout=timeout)
        codigo = proceso.returncode
    except subprocess.TimeoutExpired:
        matar_proceso(proceso)
        salida, _ = proceso.communicate()
        codigo = -1
    finally:
        if proceso in procesos_activos:
            procesos_activos.remove(proceso)
        if titular is not None and proceso in titular:
            titular.remove(proceso)
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
    t0 = time.time()
    codigo, salida = correr_comando(f"bash '{script}'", carpeta_bug, timeout)
    medicion["test_bug"] = time.time() - t0
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
# El proyecto esta instalado en modo editable, asi que pytest importa el repo real:
# hay que medir ANTES de editar (en segundo plano, mientras Gemini responde) y no
# tocar el codigo hasta que esa medicion termine.
# "tests" es None si no se pudo medir. Un conjunto vacio significa que se midio
# y ninguno pasaba: no es lo mismo.
linea_base = {
    "listo": threading.Event(),
    "tests": None,
    "completo": False,
    "iniciada": False,
    "cancelada": False,
    "duracion": None,
    "holder": [],
}

# Tests que se rompieron en la ultima verificacion (se actualiza in-place)
tests_rotos = []


def _entrecomillar(texto) -> str:
    return "'" + str(texto).replace("'", "'\"'\"'") + "'"


def _junit_a_conjuntos(archivo_xml: str):
    """Devuelve (pasan, fallan) o None si el xml no se puede leer o no tiene tests."""
    try:
        if not os.path.isfile(archivo_xml) or os.path.getsize(archivo_xml) == 0:
            return None
        raiz = ET.parse(archivo_xml).getroot()
    except Exception:
        return None
    pasan, fallan = set(), set()
    cantidad = 0
    for caso in raiz.iter("testcase"):
        cantidad += 1
        nombre = f"{caso.get('classname')}::{caso.get('name')}"
        etiquetas = [hijo.tag for hijo in caso]
        if "failure" in etiquetas or "error" in etiquetas:
            fallan.add(nombre)
        elif "skipped" not in etiquetas:
            pasan.add(nombre)
    if cantidad == 0:
        return None
    return pasan, fallan


def tests_que_pasan(carpeta_codigo, carpeta_env, modulos: list, timeout: float, titular=None):
    """Corre pytest y devuelve (pasan, fallan, completo) o None si no hubo resultados.
    `completo` es False si el comando se corto por tiempo (pytest escribe el junit al final,
    asi que en ese caso normalmente no hay xml y devolvemos None)."""
    archivo_xml = tempfile.mktemp(suffix=".xml")
    rutas = []
    for modulo in modulos:
        if str(modulo).endswith("__init__.py"):
            rutas.append(str(Path(modulo).parent))
        else:
            rutas.append(str(modulo))
    opciones = ""
    if any(str(modulo).endswith("__init__.py") for modulo in modulos):
        opciones = "-o 'python_files=__init__.py test_*.py *_test.py' "
    # pytest-timeout evita que un solo test colgado se coma el presupuesto
    opcion_timeout = "$(python -c 'import pytest_timeout' 2>/dev/null && echo --timeout=40)"
    comando = (
        f"source {_entrecomillar(carpeta_env / 'bin' / 'activate')} && "
        f"python -m pytest -p no:cacheprovider -q --tb=no "
        f"--continue-on-collection-errors {opcion_timeout} "
        f"--junitxml={_entrecomillar(archivo_xml)} {opciones}"
        + " ".join(_entrecomillar(ruta) for ruta in rutas)
    )
    codigo, _salida = correr_comando(comando, carpeta_codigo, timeout, titular=titular)
    try:
        medido = _junit_a_conjuntos(archivo_xml)
    finally:
        if os.path.exists(archivo_xml):
            os.remove(archivo_xml)
    if medido is None:
        return None
    pasan, fallan = medido
    return pasan, fallan, codigo != -1


def calcular_linea_base(repo, modulos):
    t0 = time.time()
    try:
        tiempo = presupuesto_linea_base(segundos_restantes())
        if tiempo < 8:
            linea_base["tests"] = None
            linea_base["completo"] = False
            print("   linea base: no queda tiempo para medirla y repetirla", flush=True)
            return
        print(f"   midiendo linea base ({len(modulos)} modulo(s), tope {int(tiempo)}s)", flush=True)
        resultado = tests_que_pasan(repo, Path(repo) / "env", modulos, tiempo, titular=linea_base["holder"])
        # Una pasada cortada no sirve como línea base: faltarían tests y un arreglo
        # podría aprobarse sin haberlos mirado.
        if linea_base["cancelada"] or resultado is None or not resultado[2]:
            linea_base["tests"] = None
            linea_base["completo"] = False
            print("   linea base: no se pudo medir completa", flush=True)
        else:
            pasan, _fallan, _completo = resultado
            linea_base["tests"] = pasan
            linea_base["completo"] = True
            print(f"   linea base: {len(pasan)} tests pasaban", flush=True)
    except Exception as e:
        linea_base["tests"] = None
        linea_base["completo"] = False
        print(f"   linea base: error ({e})", flush=True)
    finally:
        linea_base["duracion"] = time.time() - t0
        linea_base["listo"].set()


def iniciar_linea_base(carpeta_bug: Path, info: dict):
    """Mide en segundo plano que tests pasan con el codigo ORIGINAL.
    Hay que llamarla antes de modificar el codigo. El proyecto es editable: se mide
    sobre el repo real, no sobre una copia."""
    if linea_base["iniciada"]:
        return
    linea_base["iniciada"] = True
    linea_base["cancelada"] = False
    linea_base["holder"] = []

    repo = Path(carpeta_bug) / "repo"
    modulos = info.get("modulos_test") or []
    if len(modulos) == 0:
        linea_base["tests"] = None
        linea_base["completo"] = False
        linea_base["duracion"] = None
        linea_base["listo"].set()
        return
    try:
        hilo = threading.Thread(target=calcular_linea_base, args=(repo, modulos), daemon=True)
        hilo.start()
    except Exception:
        linea_base["tests"] = None
        linea_base["listo"].set()


def esperar_linea_base():
    """Espera a la medicion. Si queda poco tiempo, la corta para poder editar el repo
    (mientras corre, el codigo tiene que seguir siendo el original)."""
    if not linea_base["iniciada"]:
        return
    while not linea_base["listo"].is_set():
        if segundos_restantes() < MARGEN_CIERRE:
            linea_base["cancelada"] = True
            for proceso in list(linea_base["holder"]):
                matar_proceso(proceso)
            linea_base["listo"].wait(5)
            if not linea_base["listo"].is_set():
                linea_base["tests"] = None
                linea_base["completo"] = False
                linea_base["listo"].set()
            return
        linea_base["listo"].wait(0.4)


def verificar_regresiones(carpeta_bug: Path, info: dict):
    """Comprueba que el arreglo no rompio tests que pasaban con el codigo original.

    Devuelve (estado, rotos):
      - "ok": se midio y no hay regresiones
      - "regresion": hay tests que antes pasaban y ahora no
      - "no_verificado": no se pudo medir (no hay que darlo por bueno)
    """
    tests_rotos.clear()
    repo = Path(carpeta_bug) / "repo"
    modulos = info.get("modulos_test") or []
    if len(modulos) == 0:
        print("   regresiones no verificadas: bug.json no trae modulos_test", flush=True)
        return "no_verificado", []

    if not linea_base["iniciada"]:
        iniciar_linea_base(carpeta_bug, info)
    esperar_linea_base()

    if linea_base["tests"] is None or not linea_base.get("completo"):
        print("   regresiones no verificadas: no hay línea base completa", flush=True)
        return "no_verificado", []

    necesario = reserva_regresion(linea_base.get("duracion"), True)
    if necesario is None or segundos_restantes() < necesario + MARGEN_CIERRE:
        print("   regresiones no verificadas: no queda tiempo para repetir la suite", flush=True)
        return "no_verificado", []
    # El tope es la duración medida más una holgura, no un máximo fijo.
    tiempo = min(segundos_restantes() - MARGEN_CIERRE, necesario)

    print("   comprobando regresiones...", flush=True)
    restaurar_tests(repo)
    resultado = tests_que_pasan(repo, repo / "env", modulos, tiempo)
    if resultado is None:
        print("   regresiones no verificadas: pytest no produjo resultados", flush=True)
        return "no_verificado", []

    pasan, fallan, completo = resultado
    base = linea_base["tests"]
    rotos = set(base) & set(fallan)
    if completo:
        rotos |= set(base) - set(pasan)
    lista = sorted(rotos)
    tests_rotos.extend(lista)
    if lista:
        print(f"   regresiones: {len(lista)} test(s) que antes pasaban ahora fallan", flush=True)
        return "regresion", lista
    if not completo:
        print("   regresiones no verificadas por completo (pytest cortado, sin fallos vistos)", flush=True)
        return "no_verificado", []
    print(f"   sin regresiones ({len(base)} tests que pasaban siguen pasando)", flush=True)
    return "ok", []


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


# Nombres que parecen llamadas pero no son funciones del proyecto
_nombres_no_llamada = {
    "if", "for", "while", "with", "return", "and", "or", "not", "in", "is", "lambda",
    "yield", "assert", "raise", "print", "len", "str", "int", "list", "dict", "set",
    "tuple", "super", "isinstance", "issubclass", "range", "enumerate", "zip", "map",
    "filter", "type", "getattr", "setattr", "hasattr", "any", "all", "min", "max",
    "sum", "open", "repr", "bytes", "bytearray", "bool", "float", "sorted", "reversed",
    "next", "iter", "callable", "classmethod", "staticmethod", "property", "Exception",
    "ValueError", "TypeError", "KeyError", "AttributeError", "RuntimeError",
    "NotImplementedError", "ImportError", "OSError", "StopIteration",
}


def funcion_que_contiene(repo, archivo: str, inicio: int, fin: int):
    """La funcion o metodo mas chico que cubre el rango [inicio, fin]."""
    mejor = None
    for funcion in listar_funciones(repo):
        if funcion["archivo"] != archivo:
            continue
        if funcion["inicio"] <= inicio and funcion["fin"] >= fin:
            if mejor is None or (funcion["fin"] - funcion["inicio"]) < (mejor["fin"] - mejor["inicio"]):
                mejor = funcion
    return mejor


def funciones_invocadas(repo, funcion: dict) -> list:
    """Otras funciones del mismo archivo llamadas desde `funcion` (prioriza la misma clase)."""
    if not funcion or funcion.get("tipo") == "modulo":
        return []
    lineas = leer_lineas(repo, funcion["archivo"])
    cuerpo = "\n".join(lineas[funcion["inicio"] - 1:funcion["fin"]])
    # self.metodo( ... ) y tambien self.metodo pasado como callback (sin parentesis)
    nombres = set(re.findall(r"\bself\.(\w+)\b", cuerpo))
    nombres |= set(re.findall(r"(?<![\w\.])([A-Za-z_]\w*)\s*\(", cuerpo))
    nombres -= _nombres_no_llamada
    nombres.discard(funcion["nombre"].split(".")[-1])
    clase = funcion["nombre"].rsplit(".", 1)[0] if "." in funcion["nombre"] else ""
    encontrados = []
    for otra in listar_funciones(repo):
        if otra["archivo"] != funcion["archivo"] or otra["inicio"] == funcion["inicio"]:
            continue
        corto = otra["nombre"].split(".")[-1]
        if corto not in nombres:
            continue
        misma_clase = bool(clase) and (otra["nombre"] == clase or otra["nombre"].startswith(clase + "."))
        encontrados.append((0 if misma_clase else 1, otra["inicio"], otra))
    encontrados.sort()
    vistos = set()
    salida = []
    for _orden, inicio, otra in encontrados:
        if inicio in vistos:
            continue
        vistos.add(inicio)
        salida.append(otra)
    return salida