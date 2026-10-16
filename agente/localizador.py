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
                   listar_archivos_fuente, leer_lineas, recortar, segundos_restantes)

MAX_CANDIDATOS = 12
MAX_REGIONES = 5
MAX_LINEAS = 80  # si una funcion tiene mas lineas que esto la recortamos
VENTANA = 30     # cuantas lineas arriba y abajo dejamos cuando recortamos

# palabras que no sirven para buscar funciones (python, self, etc.)
palabras_comunes = set(dir(builtins)) | {"self", "cls", "super", "mock", "path", "name", "value", "result"}


def sacar_nombre_del_test(comando: str):
    """Del comando saca el nombre del test. Ej: 'pytest tests/a.py::test_x' -> 'test_x'"""
    partes = comando.split()
    if len(partes) == 0:
        return None
    ultima = partes[-1]
    if ultima.endswith(".py"):
        return None  # el comando corre un archivo completo
    nombre = re.split(r"::|\.", ultima)[-1]
    nombre = re.sub(r"\[.*$", "", nombre)  # quita los parametros [..]
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


def leer_test(repo, info: dict):
    """Lee los archivos de test. Devuelve (codigo del test, palabras que usa, archivos de datos chicos)."""
    nombres_test = []
    for comando in info.get("comandos_test", []):
        nombre = sacar_nombre_del_test(comando)
        if nombre:
            nombres_test.append(nombre)

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

        for nombre in nombres_test:
            funcion = sacar_funcion(lineas, nombre)
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

        # lo que el test importa tambien nos sirve
        if encontro_test:
            for linea in lineas:
                if linea.startswith("from ") or linea.startswith("import "):
                    palabras = palabras | set(re.findall(r"[A-Za-z_]\w*", linea))

        # si el archivo no tenia el test y es chico, puede ser un archivo de datos del test
        if not encontro_test and len(texto) <= 1500:
            datos[archivo] = texto

    # nos quedamos solo con palabras utiles
    utiles = set()
    for p in palabras:
        if len(p) >= 3 and p not in palabras_comunes and not p.startswith("assert"):
            utiles.add(p)
    return codigo_test, utiles, datos


def buscar_candidatos(repo, frames: list, palabras: set, salida: str) -> list:
    """Arma la lista de funciones sospechosas, cada una con un puntaje."""
    todas = listar_funciones(repo)
    candidatos = {}  # clave: (archivo, inicio, fin)

    def sumar(funcion, puntos, linea_error=None):
        clave = (funcion["archivo"], funcion["inicio"], funcion["fin"])
        if clave not in candidatos:
            candidatos[clave] = {"funcion": funcion, "puntos": 0, "linea_error": linea_error}
        candidatos[clave]["puntos"] += puntos

    # (a) funciones donde ocurre el error segun el traceback (la mas interna vale mas)
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
        sumar(mejor, max(10, 60 - 8 * pos), linea)

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


def localizar(carpeta: Path, info: dict) -> dict:
    repo = carpeta / "repo"

    # 1. Ejecutamos los tests y guardamos el traceback
    estado_tests = ejecutar_tests(carpeta, min(60, max(10, segundos_restantes() / 3)))
    salida = estado_tests["traceback"]

    # 2 y 3. Frames del traceback y codigo del test
    frames = sacar_frames(salida, repo)
    codigo_test, palabras, datos = leer_test(repo, info)

    # 4. Candidatos
    candidatos = buscar_candidatos(repo, frames, palabras, salida)

    # 5. Ordenamos con Gemini (si falla nos quedamos con el orden por puntaje)
    orden = list(range(len(candidatos)))
    razon = ""
    if len(candidatos) > 1:
        try:
            elegidos, razon = ordenar_con_gemini(repo, candidatos, salida, codigo_test)
            resto = [i for i in orden if i not in elegidos]
            orden = elegidos + resto
        except Exception as e:
            razon = f"orden por puntaje (Gemini fallo: {e})"

    regiones = []
    for i in orden[:MAX_REGIONES]:
        regiones.append(hacer_region(candidatos[i]))
    if len(regiones) == 0:
        regiones = region_de_relleno(repo)

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