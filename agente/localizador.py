"""LOCALIZADOR: decide en qué parte del código fuente está el bug.

Uso por separado (para probarlo solo):
    python localizador.py <carpeta_del_bug>      # imprime la localización en JSON

Pasos:
  1. Ejecutar los tests del bug y guardar el traceback
  2. Sacar del traceback las líneas del código del proyecto (frames)
  3. Leer el código del test para ver qué funciones usa
  4. Armar una lista de funciones candidatas con puntaje
  5. Si hay más de una, preguntarle a Gemini cuál es más probable (1 sola llamada)

Devuelve {"regiones": [{"archivo", "inicio", "fin"}], ...}. Los campos extra los usa el reparador.
"""
import builtins
import json
import re
import sys
from pathlib import Path
from utils import (ejecutar_tests, consultar_gemini_json, sacar_frames, listar_funciones,
                   listar_archivos_fuente, leer_lineas, recortar, segundos_restantes,
                   iniciar_linea_base, funcion_que_contiene, funciones_invocadas)

MAX_CANDIDATOS = 12
MAX_REGIONES = 5
MAX_LINEAS = 80  # si una funcion tiene mas lineas que esto la recortamos
VENTANA = 30     # cuantas lineas arriba y abajo dejamos cuando recortamos

# palabras que no sirven para buscar funciones (python, self, etc.)
palabras_comunes = set(dir(builtins)) | {"self", "cls", "super", "mock", "path", "name", "value", "result"}


def sacar_clase_y_test(comando: str):
    """Del comando saca (clase, test). La clase puede ser None si el comando no la nombra."""
    partes = comando.split()
    if len(partes) == 0:
        return None, None
    ultima = partes[-1]
    if ultima.endswith(".py"):
        return None, None
    if "::" in ultima:
        segs = ultima.split("::")
        nombre = re.sub(r"\[.*$", "", segs[-1])
        clase = segs[-2] if len(segs) >= 3 else None
        if clase and clase.endswith(".py"):
            clase = None
        return clase, nombre
    segs = re.split(r"\.", ultima)
    nombre = re.sub(r"\[.*$", "", segs[-1])
    clase = segs[-2] if len(segs) >= 2 and re.match(r"[A-Z]", segs[-2]) else None
    return clase, nombre


def sacar_nombre_del_test(comando: str):
    """Del comando saca el nombre del test. Ej: 'pytest tests/a.py::test_x' -> 'test_x'"""
    _clase, nombre = sacar_clase_y_test(comando)
    return nombre


def sacar_funcion(lineas: list, nombre: str):
    """Busca 'def nombre(' en el archivo y devuelve el texto de esa funcion (o None)."""
    for i in range(len(lineas)):
        if re.match(r"\s*(async\s+)?def " + re.escape(nombre) + r"\(", lineas[i]):
            sangria = len(lineas[i]) - len(lineas[i].lstrip())
            fin = i + 1
            # la funcion termina cuando aparece una linea con menos o igual sangria
            while fin < len(lineas):
                linea = lineas[fin]
                if linea.strip() != "" and len(linea) - len(linea.lstrip()) <= sangria:
                    break
                fin += 1
            return "\n".join(lineas[i:fin])
    return None


def _indice_de_metodo(lineas: list, clase: str, nombre: str):
    """Indice de `def nombre` dentro de `class clase` (o el primero del archivo si no hay clase)."""
    if clase:
        for i, linea in enumerate(lineas):
            if not re.match(r"\s*class\s+" + re.escape(clase) + r"\b", linea):
                continue
            sangria = len(linea) - len(linea.lstrip())
            for j in range(i + 1, len(lineas)):
                actual = lineas[j]
                if actual.strip() and (len(actual) - len(actual.lstrip())) <= sangria:
                    break
                if re.match(r"\s*(async\s+)?def\s+" + re.escape(nombre) + r"\(", actual):
                    return j
            break
    for i, linea in enumerate(lineas):
        if re.match(r"\s*(async\s+)?def\s+" + re.escape(nombre) + r"\(", linea):
            return i
    return None


def leer_test(repo, info: dict):
    """Lee los archivos de test. Devuelve (codigo del test, palabras que usa, archivos de datos chicos)."""
    nombres_test = []
    for comando in info.get("comandos_test", []):
        clase, nombre = sacar_clase_y_test(comando)
        if nombre:
            nombres_test.append((clase, nombre))

    codigo_test = ""
    palabras = set()
    datos = {}

    for archivo in info.get("archivos_test", []):
        ruta = Path(repo) / archivo
        if not ruta.is_file():
            continue
        texto = ruta.read_text(encoding="utf-8", errors="replace")
        lineas = texto.splitlines()
        encontro_test = False

        for clase, nombre in nombres_test:
            indice = _indice_de_metodo(lineas, clase, nombre)
            funcion = None
            if indice is not None:
                funcion = sacar_funcion(lineas[indice:], nombre)
                if funcion is None:
                    funcion = "\n".join(lineas[indice:indice + 40])
            if funcion is None:
                continue
            encontro_test = True
            codigo_test += f"# {archivo}\n{funcion}\n\n"
            usadas = set(re.findall(r"[A-Za-z_]\w*", funcion))
            # si el test llama a una funcion auxiliar del mismo archivo, miramos tambien esa
            for p in list(usadas):
                if p != nombre:
                    auxiliar = sacar_funcion(lineas, p)
                    if auxiliar:
                        usadas = usadas | set(re.findall(r"[A-Za-z_]\w*", auxiliar))
            palabras = palabras | usadas

        # No metemos la ruta del import (urllib.parse haria que "parse" parezca una funcion
        # del proyecto). Los simbolos importados de verdad los resuelve anclajes_desde_tests.

        # si el archivo no tenia el test y es chico, puede ser un archivo de datos del test
        if not encontro_test and len(texto) <= 1500:
            datos[archivo] = texto

    # nos quedamos solo con palabras utiles
    utiles = set()
    for p in palabras:
        if len(p) >= 3 and p not in palabras_comunes and not p.startswith("assert"):
            utiles.add(p)
    return codigo_test, utiles, datos


def _lineas_de_import(texto: str) -> list:
    """Junta imports, tambien los que vienen partidos en varias lineas con parentesis."""
    lineas = []
    pendiente = None
    for linea in texto.splitlines():
        linea = linea.split("#", 1)[0]
        if pendiente is not None:
            pendiente += " " + linea.strip()
            if ")" in linea:
                lineas.append(pendiente)
                pendiente = None
            continue
        if re.match(r"\s*from\s+.+\bimport\s+\(", linea) and ")" not in linea:
            pendiente = linea.strip()
            continue
        limpia = linea.strip()
        if limpia.startswith("from ") or limpia.startswith("import "):
            lineas.append(limpia)
    return lineas


def _parse_from(linea: str):
    """'from mod import A as B, C' -> (mod, [(A, B), (C, C)]) o None."""
    coincidencia = re.match(r"from\s+(\.*[\w\.]*)\s+import\s+\(?\s*(.*?)\s*\)?\s*$", linea.strip())
    if not coincidencia:
        return None
    origen = coincidencia.group(1)
    pares = []
    for trozo in coincidencia.group(2).split(","):
        trozo = trozo.strip()
        if trozo == "":
            continue
        if trozo == "*":
            pares.append(("*", "*"))
            continue
        nombre = re.match(r"(\w+)(?:\s+as\s+(\w+))?$", trozo)
        if nombre:
            pares.append((nombre.group(1), nombre.group(2) or nombre.group(1)))
    if not pares:
        return None
    return origen, pares


def _archivo_de_modulo(repo, modulo: str):
    if not modulo or modulo.startswith("."):
        return None
    relativo = modulo.replace(".", "/")
    for candidato in (relativo + ".py", relativo + "/__init__.py"):
        if (Path(repo) / candidato).is_file():
            return candidato
    return None


def _modulo_absoluto(modulo_actual: str, origen: str, es_paquete: bool):
    if not origen.startswith("."):
        return origen
    puntos = len(origen) - len(origen.lstrip("."))
    resto = origen.lstrip(".")
    base = modulo_actual.split(".") if modulo_actual else []
    paquete = base if es_paquete else base[:-1]
    subir = puntos - 1
    if subir > len(paquete):
        return None
    paquete = paquete[:len(paquete) - subir]
    if resto:
        paquete = paquete + resto.split(".")
    return ".".join(paquete) if paquete else None


def resolver_definicion(repo, modulo: str, nombre: str, vistos=None):
    """Sigue los imports del proyecto hasta la clase o funcion `nombre`."""
    if not modulo or not nombre:
        return None
    vistos = set() if vistos is None else vistos
    clave = (modulo, nombre)
    if clave in vistos or len(vistos) > 12:
        return None
    vistos.add(clave)
    archivo = _archivo_de_modulo(repo, modulo)
    if archivo is None:
        return None
    for funcion in listar_funciones(repo):
        if funcion["archivo"] == archivo and funcion["nombre"] == nombre:
            return funcion
    texto = (Path(repo) / archivo).read_text(encoding="utf-8", errors="replace")
    es_paquete = archivo.endswith("/__init__.py")
    for linea in _lineas_de_import(texto):
        parseado = _parse_from(linea)
        if not parseado:
            continue
        origen, pares = parseado
        for original, alias in pares:
            destino = _modulo_absoluto(modulo, origen, es_paquete)
            if not destino:
                continue
            if alias == "*":
                # from modulo import * : el nombre puede estar definido ahi
                encontrado = resolver_definicion(repo, destino, nombre, vistos)
                if encontrado:
                    return encontrado
                continue
            if alias != nombre:
                continue
            return resolver_definicion(repo, destino, original, vistos)
    return None


def _alias_de_clase(lineas: list, indice_clase: int) -> dict:
    """nombre -> expresion, para atributos de clase y self.x = Nombre en setUp."""
    sangria_clase = len(lineas[indice_clase]) - len(lineas[indice_clase].lstrip(" "))
    fin = indice_clase + 1
    while fin < len(lineas):
        linea = lineas[fin]
        if linea.strip() and (len(linea) - len(linea.lstrip(" "))) <= sangria_clase:
            break
        fin += 1
    alias = {}
    sangrias_def = []
    for i in range(indice_clase + 1, fin):
        if re.match(r"\s*(async\s+)?def\s+\w+\s*\(", lineas[i]):
            sangrias_def.append(len(lineas[i]) - len(lineas[i].lstrip(" ")))
    sangria_metodo = min(sangrias_def) if sangrias_def else sangria_clase + 4
    for i in range(indice_clase + 1, fin):
        linea = lineas[i]
        if not linea.strip():
            continue
        if (len(linea) - len(linea.lstrip(" "))) != sangria_metodo:
            continue
        asignacion = re.match(r"\s*(\w+)\s*=\s*([A-Za-z_][\w\.]*)\s*$", linea)
        if asignacion:
            alias[asignacion.group(1)] = asignacion.group(2)
    for i in range(indice_clase + 1, fin):
        if not re.match(r"\s*(async\s+)?def\s+(setUp|setUpClass|setup|setup_method)\s*\(", lineas[i]):
            continue
        cuerpo = sacar_funcion(lineas[i:], re.match(r"\s*(async\s+)?def\s+(\w+)", lineas[i]).group(2)) or ""
        for encontrado in re.finditer(r"\bself\.(\w+)\s*=\s*([A-Za-z_][\w\.]*)", cuerpo):
            alias[encontrado.group(1)] = encontrado.group(2)
    return alias


def _expandir_ancla(repo, definicion: dict) -> list:
    """De una clase nos quedamos con __init__ y lo que ese __init__ llama, no con la clase entera."""
    if definicion is None:
        return []
    if definicion["tipo"] != "clase":
        salida = [(definicion, 100)]
        for llamada in funciones_invocadas(repo, definicion)[:3]:
            salida.append((llamada, 80))
        return salida
    salida = []
    for funcion in listar_funciones(repo):
        if funcion["archivo"] == definicion["archivo"] and funcion["nombre"] == definicion["nombre"] + ".__init__":
            salida.append((funcion, 100))
            for llamada in funciones_invocadas(repo, funcion)[:4]:
                salida.append((llamada, 90))
            break
    return salida


def _modulo_de_archivo(archivo: str) -> str:
    ruta = archivo.replace("\\", "/")
    if ruta.endswith(".py"):
        ruta = ruta[:-3]
    if ruta.endswith("/__init__"):
        ruta = ruta[: -len("/__init__")]
    return ruta.replace("/", ".")


def _parse_import(linea: str) -> list:
    """'import paquete.mod as alias' -> [(modulo, alias)]."""
    linea = linea.strip()
    if not linea.startswith("import "):
        return []
    salida = []
    for trozo in linea[len("import "):].split(","):
        coincidencia = re.match(r"([\w\.]+)(?:\s+as\s+(\w+))?$", trozo.strip())
        if not coincidencia:
            continue
        modulo = coincidencia.group(1)
        alias = coincidencia.group(2) or modulo.split(".")[0]
        salida.append((modulo, alias))
    return salida


def _mapa_de_imports(archivo: str, texto: str) -> dict:
    """alias -> (modulo_absoluto, nombre_original). nombre_original es None si el alias es un módulo."""
    modulo_actual = _modulo_de_archivo(archivo)
    es_paquete = archivo.replace("\\", "/").endswith("/__init__.py")
    mapa = {}
    for linea in _lineas_de_import(texto):
        parseado = _parse_from(linea)
        if parseado:
            origen, pares = parseado
            if origen.startswith("."):
                origen = _modulo_absoluto(modulo_actual, origen, es_paquete)
                if not origen:
                    continue
            for original, alias in pares:
                mapa[alias] = (origen, original)
            continue
        for modulo, alias in _parse_import(linea):
            if modulo.startswith("."):
                modulo = _modulo_absoluto(modulo_actual, modulo, es_paquete)
                if not modulo:
                    continue
            mapa[alias] = (modulo, None)
    return mapa


def anclajes_desde_tests(repo, info: dict) -> list:
    """Funciones del proyecto que el test usa de verdad (imports y self.atributo = Clase).
    Cada item es (funcion, puntos). Sirve cuando el traceback no entra al proyecto,
    por ejemplo un assertRaises que no llego a lanzar."""
    mejores = {}
    for archivo in info.get("archivos_test", []):
        ruta = Path(repo) / archivo
        if not ruta.is_file():
            continue
        texto = ruta.read_text(encoding="utf-8", errors="replace")
        lineas = texto.splitlines()
        mapa = _mapa_de_imports(archivo, texto)
        for comando in info.get("comandos_test", []):
            clase, nombre = sacar_clase_y_test(comando)
            if not nombre:
                continue
            indice = _indice_de_metodo(lineas, clase, nombre)
            if indice is None:
                continue
            cuerpo = sacar_funcion(lineas[indice:], nombre) or ""
            alias = {}
            if clase:
                for i, linea in enumerate(lineas):
                    if re.match(r"\s*class\s+" + re.escape(clase) + r"\b", linea):
                        alias = _alias_de_clase(lineas, i)
                        break
            simbolos = []
            for atributo in re.findall(r"\bself\.(\w+)", cuerpo):
                destino = alias.get(atributo)
                if not destino:
                    continue
                simbolo = destino.split(".")[-1]
                if simbolo in mapa:
                    simbolos.append(mapa[simbolo])
            for llamado in re.findall(r"(?<![\w\.])([A-Za-z_]\w*)\s*\(", cuerpo):
                destino = mapa.get(llamado)
                if destino and destino[1] not in (None, "*"):
                    simbolos.append(destino)
            atributos = 0
            for nombre_mod, atributo in re.findall(r"(?<![\w\.])([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(", cuerpo):
                if atributos >= 4:
                    break
                destino = mapa.get(nombre_mod)
                if destino and destino[1] is None:
                    simbolos.append((destino[0], atributo))
                    atributos += 1
            extras = 0
            for llamado in re.findall(r"(?<![\w\.])([A-Za-z_]\w*)\s*\(", cuerpo):
                if extras >= 2 or llamado in mapa:
                    continue
                for origen, original in mapa.values():
                    if original != "*":
                        continue
                    simbolos.append((origen, llamado))
                    extras += 1
                    break
            for modulo, simbolo in simbolos:
                definicion = resolver_definicion(repo, modulo, simbolo)
                for funcion, puntos in _expandir_ancla(repo, definicion):
                    clave = (funcion["archivo"], funcion["inicio"], funcion["fin"])
                    if puntos > mejores.get(clave, (None, 0))[1]:
                        mejores[clave] = (funcion, puntos)
    return list(mejores.values())


def buscar_candidatos(repo, frames: list, palabras: set, salida: str, anclajes=None) -> list:
    """Arma la lista de funciones sospechosas, cada una con un puntaje."""
    todas = listar_funciones(repo)
    candidatos = {}  # clave: (archivo, inicio, fin)

    def sumar(funcion, puntos, linea_error=None, maximo=False):
        clave = (funcion["archivo"], funcion["inicio"], funcion["fin"])
        if clave not in candidatos:
            candidatos[clave] = {"funcion": funcion, "puntos": 0, "linea_error": linea_error}
        if maximo:
            candidatos[clave]["puntos"] = max(candidatos[clave]["puntos"], puntos)
        else:
            candidatos[clave]["puntos"] += puntos
        if linea_error and candidatos[clave]["linea_error"] is None:
            candidatos[clave]["linea_error"] = linea_error

    # (a) funciones donde ocurre el error segun el traceback (la mas interna vale mas).
    # El puntaje parte de 200 para que el traceback gane a los anclajes (como mucho 100).
    frames_al_reves = list(reversed(frames))
    for pos in range(len(frames_al_reves)):
        archivo, linea = frames_al_reves[pos]
        mejor = None
        for f in todas:
            if f["archivo"] == archivo and f["tipo"] == "funcion" and f["inicio"] <= linea <= f["fin"]:
                # nos quedamos con la funcion mas chica que contiene la linea
                if mejor is None or (f["fin"] - f["inicio"]) < (mejor["fin"] - mejor["inicio"]):
                    mejor = f
        if mejor is None:
            # la linea no esta dentro de una funcion: usamos unas lineas alrededor
            total = len(leer_lineas(repo, archivo))
            mejor = {"archivo": archivo, "nombre": "<modulo>", "tipo": "modulo",
                     "inicio": max(1, linea - 12), "fin": min(total, linea + 12)}
        sumar(mejor, max(40, 200 - 15 * pos), linea, maximo=True)

    # (a2) simbolos que el test importa o asigna (self.x = Clase), cuando el error
    # no llega a entrar al proyecto
    for funcion, puntos in anclajes or []:
        sumar(funcion, puntos, funcion["inicio"])

    # (b) funciones cuyo nombre aparece en el test
    repeticiones = {}  # cuantas funciones hay con el mismo nombre corto
    for f in todas:
        if f["tipo"] == "funcion":
            corto = f["nombre"].split(".")[-1]
            repeticiones[corto] = repeticiones.get(corto, 0) + 1

    for f in todas:
        if f["tipo"] != "funcion":
            continue
        corto = f["nombre"].split(".")[-1]
        puntos = 0
        if corto in palabras:
            if repeticiones[corto] <= 6:
                puntos += 20
            else:
                puntos += 4  # nombre muy repetido, es poco confiable
            if len(corto) >= 4 and re.search(r"\b" + re.escape(corto) + r"\b", salida):
                puntos += 5
        # si es metodo de una clase que usa el test
        if "." in f["nombre"] and f["nombre"].split(".")[0] in palabras:
            puntos += 6
        if puntos > 0:
            sumar(f, puntos)

    lista = list(candidatos.values())
    lista.sort(key=lambda c: -c["puntos"])
    return lista[:MAX_CANDIDATOS]


def hacer_region(candidato: dict) -> dict:
    f = candidato["funcion"]
    inicio = f["inicio"]
    fin = f["fin"]
    # si la funcion es muy larga, dejamos solo una ventana alrededor de la linea del error
    if fin - inicio + 1 > MAX_LINEAS:
        centro = candidato["linea_error"] or inicio + VENTANA
        inicio = max(f["inicio"], centro - VENTANA)
        fin = min(f["fin"], centro + VENTANA)
    return {"archivo": f["archivo"], "inicio": inicio, "fin": fin, "funcion": f["nombre"]}


def ordenar_con_gemini(repo, candidatos: list, salida: str, codigo_test: str):
    """Una llamada a Gemini: le mostramos el comienzo de cada candidato y nos dice el orden."""
    lista_texto = ""
    for i in range(len(candidatos)):
        f = candidatos[i]["funcion"]
        lineas = leer_lineas(repo, f["archivo"])
        primeras = "\n".join(lineas[f["inicio"] - 1: min(f["fin"], f["inicio"] + 7)])
        lista_texto += f"[{i}] {f['archivo']}:{f['inicio']}-{f['fin']} {f['nombre']}\n{primeras}\n\n"

    prompt = f"""Eres un experto en depurar código Python. Te muestro un test que falla y una lista de funciones candidatas.
Dime cuáles contienen el bug, de la más a la menos probable.

## Test que falla
{recortar(codigo_test, 2500)}

## Salida de los tests
{recortar(salida, 3500)}

## Candidatos (primeras líneas de cada función)
{lista_texto}
Responde SOLO con este JSON:
{{"orden": [ids de los candidatos, del más al menos probable, máximo 4], "razon": "una frase"}}"""

    respuesta = consultar_gemini_json(prompt, 0.0)
    orden = []
    for x in respuesta.get("orden", []):
        if isinstance(x, int) and 0 <= x < len(candidatos) and x not in orden:
            orden.append(x)
    return orden, str(respuesta.get("razon", ""))


def region_de_relleno(repo) -> list:
    """Por si todo falla: una region cualquiera, para que localizacion.json siempre sea valido."""
    for archivo in listar_archivos_fuente(repo):
        total = len(leer_lineas(repo, archivo))
        if total > 0:
            return [{"archivo": archivo, "inicio": 1, "fin": min(total, 40), "funcion": "<modulo>"}]
    return []


def anteponer_fuertes(candidatos: list, orden: list) -> list:
    """Si el traceback ya señala una función, las coincidencias débiles por nombre quedan al final."""
    if not orden or not candidatos:
        return list(orden)
    if max(candidatos[i]["puntos"] for i in orden) < 150:
        return list(orden)
    altos = [i for i in orden if candidatos[i]["puntos"] >= 80]
    bajos = [i for i in orden if i not in altos]
    return altos + bajos


def _como_funcion(repo, definicion):
    if definicion is None:
        return None
    if definicion.get("tipo") == "funcion":
        return definicion
    if definicion.get("tipo") != "clase":
        return None
    buscado = definicion["nombre"] + ".__init__"
    for otra in listar_funciones(repo):
        if otra["archivo"] == definicion["archivo"] and otra["nombre"] == buscado:
            return otra
    return None


def _callees_en_otros_modulos(repo, funcion) -> list:
    """Funciones de otro archivo llamadas por `funcion`, siguiendo sus imports (también relativos)."""
    if not funcion or funcion.get("tipo") == "modulo":
        return []
    archivo = funcion["archivo"]
    try:
        texto = (Path(repo) / archivo).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    mapa = _mapa_de_imports(archivo, texto)
    lineas = leer_lineas(repo, archivo)
    cuerpo = "\n".join(lineas[funcion["inicio"] - 1:funcion["fin"]])
    encontrados = []
    vistos = set()

    def agregar(definicion):
        definicion = _como_funcion(repo, definicion)
        if definicion is None or definicion["archivo"] == archivo:
            return
        clave = (definicion["archivo"], definicion["inicio"])
        if clave in vistos:
            return
        vistos.add(clave)
        encontrados.append(definicion)

    for llamado in re.findall(r"(?<![\w\.])([A-Za-z_]\w*)\s*\(", cuerpo):
        destino = mapa.get(llamado)
        if not destino or destino[1] in (None, "*"):
            continue
        agregar(resolver_definicion(repo, destino[0], destino[1]))
        if len(encontrados) >= 2:
            return encontrados
    for alias, atributo in re.findall(r"(?<![\w\.])([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(", cuerpo):
        destino = mapa.get(alias)
        if not destino or destino[1] is not None:
            continue
        agregar(resolver_definicion(repo, destino[0], atributo))
        if len(encontrados) >= 2:
            return encontrados
    return encontrados


def funciones_relacionadas(repo, funcion, limite=2) -> list:
    """Como mucho `limite` funciones vecinas: primero un callee de otro módulo, después uno del mismo archivo."""
    if not funcion:
        return []
    mismas = funciones_invocadas(repo, funcion)
    otras = _callees_en_otros_modulos(repo, funcion)
    if otras and mismas:
        elegidas = otras[:2] + mismas[:2]
    elif otras:
        elegidas = otras
    else:
        elegidas = mismas
    return elegidas[:limite]


def intercalar_callees(repo, regiones: list) -> list:
    """Después de la región más probable mete como mucho dos funciones que esa llama,
    del mismo archivo o de otro módulo. El resto de las regiones no se expande."""
    salida = []
    vistos = set()

    def agregar(region):
        clave = (region["archivo"], region["inicio"], region["fin"])
        if clave in vistos:
            return
        vistos.add(clave)
        salida.append(region)

    for idx, region in enumerate(regiones):
        agregar(region)
        if idx > 0 or len(salida) >= 4:
            continue
        contenida = funcion_que_contiene(repo, region["archivo"], region["inicio"], region["fin"])
        for funcion in funciones_relacionadas(repo, contenida, limite=2):
            extra = hacer_region({"funcion": funcion, "linea_error": funcion["inicio"]})
            extra["relacionada"] = True
            agregar(extra)
            if len(salida) >= 5:
                break
    return salida


def localizar(carpeta: Path, info: dict) -> dict:
    repo = carpeta / "repo"

    # 1. Ejecutamos los tests y guardamos el traceback
    estado_tests = ejecutar_tests(carpeta, min(45, max(8, segundos_restantes() * 0.25)))
    salida = estado_tests["traceback"]
    # La linea base de regresiones corre en paralelo con la llamada a Gemini de abajo.
    # Tiene que arrancar con el codigo todavia original, y despues de este test para no
    # lanzar dos pytest a la vez.
    iniciar_linea_base(carpeta, info)

    # 2 y 3. Frames del traceback y codigo del test
    frames = sacar_frames(salida, repo)
    codigo_test, palabras, datos = leer_test(repo, info)
    anclajes = anclajes_desde_tests(repo, info)
    if anclajes:
        resumen = ", ".join(f"{funcion['nombre']}@{funcion['archivo']}:{funcion['inicio']}" for funcion, _p in anclajes[:6])
        print(f"   anclajes: {resumen}", flush=True)

    # 4. Candidatos
    candidatos = buscar_candidatos(repo, frames, palabras, salida, anclajes)

    # 5. Ordenamos con Gemini (si falla nos quedamos con el orden por puntaje)
    orden = list(range(len(candidatos)))
    razon = ""
    if len(candidatos) > 1:
        try:
            elegidos, razon = ordenar_con_gemini(repo, candidatos, salida, codigo_test)
            # Un frame del traceback o un ancla fuerte no se deja bajar por el reordenamiento.
            fuertes = [i for i in orden if candidatos[i]["puntos"] >= 90]
            resto_elegidos = [i for i in elegidos if i not in fuertes]
            resto = [i for i in orden if i not in fuertes and i not in resto_elegidos]
            orden = fuertes + resto_elegidos + resto
        except Exception as e:
            razon = f"orden por puntaje (Gemini fallo: {e})"

    orden = anteponer_fuertes(candidatos, orden)

    regiones = []
    for i in orden[:MAX_REGIONES]:
        regiones.append(hacer_region(candidatos[i]))
    if len(regiones) == 0:
        regiones = region_de_relleno(repo)
    regiones = intercalar_callees(repo, regiones)
    for region in regiones:
        print(f"   region {region['archivo']}:{region['inicio']}-{region['fin']} {region.get('funcion', '')}", flush=True)

    # Los campos extra los usa despues el reparador
    return {
        "regiones": regiones,
        "analisis": razon,
        "tests_pasaban": estado_tests["pasan"],
        "salida_tests": salida,
        "codigo_test": codigo_test,
        "datos_test": datos
    }


if __name__ == "__main__":
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    print(json.dumps(localizar(carpeta, info), indent=2, ensure_ascii=False))