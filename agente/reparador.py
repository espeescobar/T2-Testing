"""REPARADOR: modifica el código fuente de repo/ para que los tests del bug pasen.

Uso por separado (con una localización ya guardada):
    python reparador.py <carpeta_del_bug> <localizacion.json>

Ciclo (hasta 6 intentos):
  1. Armar el prompt con el error, el test y el código sospechoso
  2. Pedirle a Gemini los cambios (buscar / reemplazar)
  3. Aplicar los cambios (si no se pueden aplicar o dejan mala sintaxis, cuenta como intento fallido)
  4. Correr los tests del bug
  5. Verificar que no se rompió ningún otro test (regresiones)
  6. Si algo falla: revertir y reintentar contándole a Gemini qué pasó

Devuelve {"resuelto": True/False, ...}
"""
import ast
import difflib
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path
from localizador import intercalar_callees
from utils import (ejecutar_tests, consultar_gemini_json, verificar_regresiones, iniciar_linea_base,
                   esperar_linea_base, revertir_cambios, git, leer_lineas, numerar_lineas, recortar,
                   segundos_restantes, regex_test, puede_llamar_modelo, listar_funciones, puede_editar,
                   marcar_reparacion_validada, reparacion_esta_validada, repo_esta_cerrado, candado_repo, linea_base, medicion,
                   estimado_test, reserva_regresion, MARGEN_CIERRE)

INTENTOS_MAXIMOS = 6
MAX_CARACTERES_CODIGO = 18000  # tope de codigo fuente dentro del prompt
MARGEN = 8                     # lineas extra arriba y abajo de cada region
MAX_LINEAS_REGION = 140
MAX_LINEAS_ARCHIVO = 140       # por encima de esto se muestra solo la region, para no inflar el prompt

INSTRUCCIONES = """Eres un ingeniero experto en arreglar bugs de proyectos Python (se ejecutan con Python 3.8).
Te muestro la salida de tests que fallan, el código del test y el código fuente con número de línea.
Corrige la CAUSA del bug en el código fuente, de forma general (no adaptes el código a los valores del test y no toques los tests).
Haz el cambio más pequeño posible. Si el error es de tipos (bytes frente a str) o de un atributo que ya no existe, corrige todas las líneas afectadas por ese mismo problema, no solo la primera.
El test puede exigir más de un comportamiento. No basta con silenciar la primera excepción: tienen que cumplirse todas las aserciones, también los casos que sí deben fallar."""

FORMATO = """Responde SOLO con este JSON:
{
  "analisis": "causa del bug en 1 a 3 frases",
  "ediciones": [
    {"archivo": "ruta/relativa.py", "inicio": 10, "fin": 12, "reemplazar": "    linea nueva\\n    otra"}
  ]
}
Reglas:
- "inicio" y "fin" son los números de línea del código mostrado, inclusive. Si es una sola línea, los dos valen lo mismo.
- "reemplazar" son las líneas nuevas, con la MISMA indentación que el archivo. No incluyas los números ni el '|'.
- Puedes mandar varias ediciones. Un import nuevo es otra edición.
- Alternativa, si no usas números: {"archivo", "buscar", "reemplazar"}. "buscar" tiene que ser un trozo COPIADO literal del archivo (sin números de línea) que aparezca una sola vez.
- Si cambias un if/elif, inicio y fin tienen que cubrir el bloque entero (todas las ramas), no una sola línea del medio.
- No cortes un docstring ni un string por la mitad: o lo incluyes entero, o empiezas y terminas fuera de él.
- Sustituye la línea vieja: no dejes la llamada original al lado de la nueva.
- No modifiques archivos de test. El código tiene que ser compatible con Python 3.8."""


def codigo_de_archivo(repo: Path, archivo: str):
    """El archivo entero, si es corto. Si no, None (se muestra solo la region)."""
    try:
        lineas = leer_lineas(repo, archivo)
    except Exception:
        return None
    if not lineas or len(lineas) > MAX_LINEAS_ARCHIVO:
        return None
    titulo = f"### {archivo} (archivo completo, {len(lineas)} líneas)"
    return titulo + "\n" + numerar_lineas(lineas, 1, len(lineas))


def codigo_de_region(repo: Path, region: dict) -> str:
    """Devuelve el codigo de una region (con un poco de margen) con numero de linea."""
    lineas = leer_lineas(repo, region["archivo"])
    inicio = max(1, region["inicio"] - MARGEN)
    fin = min(len(lineas), region["fin"] + MARGEN)
    if fin - inicio + 1 > MAX_LINEAS_REGION:
        # funcion enorme: nos quedamos con una ventana en el medio
        medio = (region["inicio"] + region["fin"]) // 2
        inicio = max(1, medio - MAX_LINEAS_REGION // 2)
        fin = min(len(lineas), medio + MAX_LINEAS_REGION // 2)
    titulo = f"### {region['archivo']} (líneas {inicio}-{fin}) {region.get('funcion', '')}"
    return titulo + "\n" + numerar_lineas(lineas, inicio, fin)


def regiones_visibles(regiones: list, intento: int) -> list:
    """El primer intento muestra la región principal y sus funciones relacionadas.
    Cada fallo agrega una región más."""
    grupos = []
    for region in regiones:
        if region.get("relacionada") and grupos:
            grupos[-1].append(region)
        else:
            grupos.append([region])
    vecinas = 2 if intento == 0 else 4
    tope = 4 if intento == 0 else 6
    elegidas = []
    for indice, grupo in enumerate(grupos[:1 + intento]):
        elegidas.append(grupo[0])
        if indice == 0:
            elegidas.extend(grupo[1:1 + vecinas])
    return elegidas[:tope]


def armar_contexto(repo: Path, regiones: list) -> str:
    """Junta el codigo de las primeras regiones (sin pasarse del tope de caracteres).
    Si el archivo es corto se manda entero: ahi estan los metodos que la region llama."""
    partes = []
    total = 0
    archivos_completos = set()
    archivos_vistos = []
    clases_vistas = set()
    for region in regiones:
        if region["archivo"] in archivos_completos:
            continue
        entero = codigo_de_archivo(repo, region["archivo"])
        if entero is not None:
            if total + len(entero) > MAX_CARACTERES_CODIGO and len(partes) > 0:
                break
            partes.append(entero)
            total += len(entero)
            archivos_completos.add(region["archivo"])
            continue
        try:
            bloque = codigo_de_region(repo, region)
            cabecera = cabecera_de_clase(repo, region, clases_vistas)
            if cabecera:
                bloque = cabecera + "\n\n" + bloque
        except Exception:
            continue
        if total + len(bloque) > MAX_CARACTERES_CODIGO and len(partes) > 0:
            break  # ya no cabe mas
        # la primera vez que sale un archivo agregamos sus imports (por si hay que agregar uno)
        if region["archivo"] not in archivos_vistos:
            archivos_vistos.append(region["archivo"])
            imports = []
            for linea in leer_lineas(repo, region["archivo"])[:80]:
                if linea.startswith("import ") or linea.startswith("from "):
                    imports.append(linea)
            if len(imports) > 0:
                bloque += "\n(imports del archivo: " + " ; ".join(imports[:25]) + ")"
        partes.append(bloque)
        total += len(bloque)
    return "\n\n".join(partes)


def armar_prompt(repo: Path, localizacion: dict, regiones: list, historial: list) -> str:
    datos = ""
    for nombre in localizacion.get("datos_test", {}):
        datos += f"\n## Datos de test {nombre}\n{localizacion['datos_test'][nombre]}\n"

    contrato = contrato_del_test(localizacion.get("codigo_test", ""))
    seccion_contrato = ""
    if contrato.strip():
        seccion_contrato = f"\n## Qué exige el test (aserciones y comentarios)\n{recortar(contrato, 2000)}\n"

    prompt = f"""{INSTRUCCIONES}

## Test que falla
{recortar(localizacion.get("codigo_test", ""), 3500)}
{seccion_contrato}{datos}
## Salida de los tests (código original, antes de cualquier arreglo)
{recortar(localizacion.get("salida_tests", ""), 4500)}

## Código fuente sospechoso (de más a menos probable)
{armar_contexto(repo, regiones)}
"""
    if localizacion.get("analisis"):
        prompt += f"\n(Nota del localizador: {localizacion['analisis']})\n"

    # Intentos anteriores que fallaron
    numero = 1
    for cambio, motivo in historial:
        prompt += f"""
## Intento previo {numero} (ya revertido, el código volvió al original)
```diff
{recortar(cambio, 2500)}
```
Resultado: {recortar(motivo, 4000)}
No repitas un parche que no se pudo aplicar: copia el texto del fragmento que te muestro.
Si el parche se aplicó, el error que hay que corregir es el de ESTE intento, no el traceback original de arriba.
Si este intento ya eliminó la excepción inicial y ahora falla una aserción posterior, conserva ese cambio y corrige solo la causa de la aserción nueva.
"""
        numero += 1

    return prompt + "\n" + FORMATO


# ------------------------------------------------------------------
# Aplicar los cambios que propone Gemini
# ------------------------------------------------------------------
def quitar_numeros_de_linea(texto: str) -> str:
    """Si Gemini copio los numeros de linea ('  12| codigo') los quitamos.
    Tambien si solo algunas lineas los traen."""
    lineas = texto.split("\n")
    no_vacias = [linea for linea in lineas if linea.strip() != ""]
    if not no_vacias:
        return texto
    con_numero = [linea for linea in no_vacias if re.match(r"^\s*\d+\|", linea)]
    if len(con_numero) * 2 < len(no_vacias):
        return texto
    limpias = []
    for linea in lineas:
        limpias.append(re.sub(r"^\s*\d+\|\s?", "", linea) if re.match(r"^\s*\d+\|", linea) else linea)
    return "\n".join(limpias)


def rango_numerado(texto: str):
    """Si cada linea trae su numero, devuelve (inicio, fin, codigo_sin_numeros)."""
    lineas = texto.split("\n")
    numeros = []
    codigo = []
    no_vacias = 0
    con_numero = 0
    for linea in lineas:
        if linea.strip() == "":
            codigo.append("")
            continue
        no_vacias += 1
        coincidencia = re.match(r"^\s*(\d+)\|\s?(.*)$", linea)
        if not coincidencia:
            return None
        con_numero += 1
        numeros.append(int(coincidencia.group(1)))
        codigo.append(coincidencia.group(2))
    if no_vacias == 0 or con_numero != no_vacias:
        return None
    if numeros[-1] < numeros[0]:
        return None
    return numeros[0], numeros[-1], "\n".join(codigo)


def como_entero(valor):
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    if isinstance(valor, float) and valor.is_integer():
        return int(valor)
    if isinstance(valor, str) and valor.strip().isdigit():
        return int(valor.strip())
    return None


def como_texto(valor):
    if isinstance(valor, str):
        return valor.replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(valor, list):
        return "\n".join(str(parte) for parte in valor)
    return None


def campo(edicion: dict, *nombres):
    for nombre in nombres:
        if nombre in edicion and edicion[nombre] is not None:
            return edicion[nombre]
    return None


def parecido(a: str, b: str) -> float:
    def normalizar(texto):
        return [linea.strip() for linea in texto.split("\n") if linea.strip()]
    izquierda, derecha = normalizar(a), normalizar(b)
    if not izquierda or not derecha:
        return 0.0
    return difflib.SequenceMatcher(None, izquierda, derecha).ratio()


def fragmento_parecido(lineas: list, lineas_buscar: list) -> str:
    """El trozo del archivo que mas se parece a lo que Gemini quiso buscar, con numeros de linea."""
    if not lineas_buscar:
        return ""
    objetivo = "\n".join(linea.strip() for linea in lineas_buscar)
    if objetivo.strip() == "":
        return ""
    n = max(1, len(lineas_buscar))
    mejor_i, mejor_r = 0, -1.0
    tope = max(1, len(lineas) - n + 1)
    for i in range(tope):
        ventana = "\n".join(linea.strip() for linea in lineas[i:i + n])
        ratio = difflib.SequenceMatcher(None, ventana, objetivo).ratio()
        if ratio > mejor_r:
            mejor_r = ratio
            mejor_i = i
    muestra = lineas[mejor_i:mejor_i + n]
    texto = "\n".join(f"{mejor_i + 1 + k}| {muestra[k]}" for k in range(len(muestra)))
    return f"El parecido más cercano (líneas {mejor_i + 1}-{mejor_i + len(muestra)}) es:\n{texto}"


def sangria(linea: str) -> int:
    return len(linea) - len(linea.lstrip(" "))


def rango_rompe_string(texto: str, inicio: int, fin: int) -> bool:
    """True si el rango entra a un string sin cubrirlo entero (parte un docstring)."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(texto).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return False
    for tok in tokens:
        if tok.type != tokenize.STRING:
            continue
        toca = not (fin < tok.start[0] or inicio > tok.end[0])
        cubre = inicio <= tok.start[0] and fin >= tok.end[0]
        if toca and not cubre:
            return True
    return False


def _rango_de_offset(texto: str, i: int, n: int):
    inicio = texto.count("\n", 0, i) + 1
    ultimo = max(i, i + max(n, 1) - 1)
    return inicio, texto.count("\n", 0, ultimo) + 1


def _error_si_parte_string(texto: str, inicio: int, fin: int) -> str:
    if rango_rompe_string(texto, inicio, fin):
        return "el rango parte un string o un docstring. Incluye el string entero, o empieza y termina fuera de él."
    return ""


def alinear_bloque(originales: list, nuevas: list) -> list:
    """Si cada línea del reemplazo perdió la misma sangría, se la devuelve."""
    def inds(lineas):
        return [sangria(linea) for linea in lineas if linea.strip()]

    originales_i, nuevas_i = inds(originales), inds(nuevas)
    if not originales_i or len(originales_i) != len(nuevas_i):
        return nuevas
    deltas = [o - n for o, n in zip(originales_i, nuevas_i)]
    if len(set(deltas)) != 1 or deltas[0] <= 0:
        return nuevas
    delta = deltas[0]
    return [(" " * delta + linea) if linea.strip() else linea for linea in nuevas]


def normalizar_diff(diff: str) -> str:
    lineas = []
    for linea in (diff or "").splitlines():
        if linea.startswith(("diff ", "index ", "@@")) or linea.startswith(("--- ", "+++ ")):
            continue
        if linea[:1] in "+-":
            lineas.append(linea[0] + " ".join(linea[1:].split()))
    return "\n".join(lineas)


def diff_equivalente(cambio: str, historial: list) -> bool:
    """True si este diff ya se probó, aunque cambien espacios o un detalle mínimo."""
    nuevo = normalizar_diff(cambio)
    if not nuevo.strip():
        return False
    for previo, _motivo in historial:
        viejo = normalizar_diff(previo)
        if not viejo.strip():
            continue
        if viejo == nuevo:
            return True
        if not (0.8 <= len(nuevo) / max(1, len(viejo)) <= 1.25):
            continue
        if difflib.SequenceMatcher(None, viejo, nuevo).ratio() >= 0.88:
            return True
    return False


def contrato_del_test(codigo: str) -> str:
    """Aserciones, with y comentarios del test: el contrato, sin el resto del archivo."""
    lineas = (codigo or "").splitlines()
    elegidas = []
    for i, linea in enumerate(lineas):
        s = linea.strip()
        if not (s.startswith("#") or "assert" in s or s.startswith("with ")):
            continue
        if i > 0 and lineas[i - 1].strip().startswith("#") and (not elegidas or elegidas[-1] != lineas[i - 1]):
            elegidas.append(lineas[i - 1])
        if not elegidas or elegidas[-1] != linea:
            elegidas.append(linea)
    return "\n".join(elegidas[:40])


def _ajustar_sangria(lineas_viejas: list, lineas_buscar: list, reemplazar: str) -> list:
    diferencia = sangria(lineas_viejas[0]) - sangria(lineas_buscar[0] if lineas_buscar else "")
    nuevas = []
    for linea in reemplazar.strip("\n").split("\n"):
        if linea.strip() == "":
            nuevas.append(linea)
        elif diferencia > 0:
            nuevas.append(" " * diferencia + linea)
        elif diferencia < 0:
            nuevas.append(linea[min(-diferencia, sangria(linea)):])
        else:
            nuevas.append(linea)
    return nuevas


def _indices_exactos(lineas: list, lineas_buscar: list, comparar) -> list:
    if not lineas_buscar:
        return []
    encontrados = []
    tope = len(lineas) - len(lineas_buscar) + 1
    for i in range(max(0, tope)):
        if all(comparar(lineas[i + k], lineas_buscar[k]) for k in range(len(lineas_buscar))):
            encontrados.append(i)
    return encontrados


def reemplazar_texto(texto: str, buscar: str, reemplazar: str):
    """Cambia 'buscar' por 'reemplazar'. Devuelve (texto_nuevo, error). Si hay error, texto_nuevo es None."""
    # Solo sustituimos un bloque que ocupa lineas enteras. Si "buscar" es un
    # pedazo de una linea (le falta el final), un replace crudo deja el resto
    # pegado y rompe la sintaxis.
    sitios = []
    inicio_busqueda = 0
    while True:
        i = texto.find(buscar, inicio_busqueda)
        if i < 0:
            break
        fin = i + len(buscar)
        empieza_linea = i == 0 or texto[i - 1] == "\n"
        termina_linea = fin == len(texto) or texto[fin] == "\n" or buscar.endswith("\n")
        if empieza_linea and termina_linea:
            sitios.append(i)
        inicio_busqueda = i + 1
    if len(sitios) == 1:
        i = sitios[0]
        error = _error_si_parte_string(texto, *_rango_de_offset(texto, i, len(buscar)))
        if error:
            return None, error
        return texto[:i] + reemplazar + texto[i + len(buscar):], ""
    if len(sitios) > 1:
        return None, "el texto de 'buscar' aparece varias veces, agrega más contexto"

    lineas = texto.split("\n")
    lineas_buscar = buscar.strip("\n").split("\n")
    # igual salvo espacios al final de cada linea
    coincidencias = _indices_exactos(lineas, lineas_buscar, lambda a, b: a.rstrip() == b.rstrip())
    if len(coincidencias) == 1:
        i = coincidencias[0]
        error = _error_si_parte_string(texto, i + 1, i + len(lineas_buscar))
        if error:
            return None, error
        nuevas = reemplazar.strip("\n").split("\n")
        return "\n".join(lineas[:i] + nuevas + lineas[i + len(lineas_buscar):]), ""
    if len(coincidencias) > 1:
        return None, "el texto de 'buscar' aparece varias veces, agrega más contexto"

    # igual ignorando la indentacion (y se la devolvemos al reemplazo)
    coincidencias = _indices_exactos(lineas, lineas_buscar, lambda a, b: a.strip() == b.strip())
    if len(coincidencias) > 1:
        return None, "el texto de 'buscar' (sin mirar la indentación) aparece varias veces"
    if len(coincidencias) == 1:
        i = coincidencias[0]
        error = _error_si_parte_string(texto, i + 1, i + len(lineas_buscar))
        if error:
            return None, error
        nuevas = _ajustar_sangria(lineas[i:], lineas_buscar, reemplazar)
        return "\n".join(lineas[:i] + nuevas + lineas[i + len(lineas_buscar):]), ""

    # parecido, no igual: solo si hay una ventana claramente mejor que las demas.
    # Las lineas muy cortas tienen que coincidir de verdad (el paso anterior).
    if lineas_buscar and sum(len(linea.strip()) for linea in lineas_buscar) >= 24:
        mejor = []
        n = len(lineas_buscar)
        objetivo = [linea.strip() for linea in lineas_buscar]
        for i in range(max(0, len(lineas) - n + 1)):
            ventana = [linea.strip() for linea in lineas[i:i + n]]
            # Un trozo que es prefijo de la línea (le falta el final) no se acepta:
            # reemplazarlo deja el resto pegado o borra el cierre de la línea.
            if any(a != b and (a.startswith(b) or b.startswith(a)) for a, b in zip(ventana, objetivo)):
                mejor.append((0.0, i))
                continue
            ratio = difflib.SequenceMatcher(None, ventana, objetivo).ratio()
            mejor.append((ratio, i))
        mejor.sort(reverse=True)
        if mejor and mejor[0][0] >= 0.86 and (len(mejor) == 1 or mejor[0][0] - mejor[1][0] >= 0.05):
            i = mejor[0][1]
            error = _error_si_parte_string(texto, i + 1, i + n)
            if error:
                return None, error
            nuevas = _ajustar_sangria(lineas[i:], lineas_buscar, reemplazar)
            return "\n".join(lineas[:i] + nuevas + lineas[i + n:]), ""

    return None, "el texto de 'buscar' no coincide con el código actual\n" + fragmento_parecido(lineas, lineas_buscar)


def reemplazar_rango(texto: str, inicio: int, fin: int, reemplazar: str, buscar: str):
    """Sustituye las lineas inicio..fin (1-based, inclusive)."""
    lineas = texto.split("\n")
    if not (1 <= inicio <= fin <= len(lineas)):
        return None, f"el rango {inicio}-{fin} está fuera del archivo (tiene {len(lineas)} líneas)"
    if fin - inicio > 200:
        return None, f"el rango {inicio}-{fin} es demasiado grande"
    error = _error_si_parte_string(texto, inicio, fin)
    if error:
        return None, error
    actual = "\n".join(lineas[inicio - 1:fin])
    if buscar.strip() and parecido(actual, buscar) < 0.45:
        return None, (
            f"las líneas {inicio}-{fin} no se parecen a 'buscar'. Ahora son:\n"
            + "\n".join(f"{inicio + k}| {lineas[inicio - 1 + k]}" for k in range(fin - inicio + 1))
        )
    if reemplazar.strip() == "":
        nuevas = []
    else:
        nuevas = alinear_bloque(lineas[inicio - 1:fin], reemplazar.strip("\n").split("\n"))
    return "\n".join(lineas[:inicio - 1] + nuevas + lineas[fin:]), ""


def ediciones_de_respuesta(respuesta) -> list:
    if not isinstance(respuesta, dict):
        return []
    for clave in ("ediciones", "edits", "cambios"):
        valor = respuesta.get(clave)
        if isinstance(valor, list):
            return valor
        if isinstance(valor, dict):
            return [valor]
    if "archivo" in respuesta and (campo(respuesta, "reemplazar", "replace", "nuevo") is not None or "buscar" in respuesta):
        return [respuesta]
    return []


_NODOS_DESPUES_DE_38 = {"Match", "TryStar", "TypeAlias", "TypeVar", "ParamSpec", "TypeVarTuple"}


def interprete_py38(repo: Path):
    """El Python del entorno del bug. setup.sh lo deja en repo/env y es 3.8.x."""
    candidato = Path(repo) / "env" / "bin" / "python"
    if candidato.is_file() and os.access(candidato, os.X_OK):
        return candidato
    return None


def _detalle_sintaxis(texto: str, error: SyntaxError) -> str:
    alrededor = ""
    lineas = texto.split("\n")
    if error.lineno:
        desde = max(1, error.lineno - 3)
        hasta = min(len(lineas), error.lineno + 3)
        alrededor = "\n" + "\n".join(f"{i}| {lineas[i - 1]}" for i in range(desde, hasta + 1))
    return f"error de sintaxis: {error.msg} (línea {error.lineno}){alrededor}"


def sintaxis_py38(texto: str, interprete) -> tuple:
    """False si el código no corre en Python 3.8. El intérprete del bug tiene la última palabra."""
    try:
        arbol = ast.parse(texto)
    except SyntaxError as e:
        return False, _detalle_sintaxis(texto, e)
    vistos = sorted({type(nodo).__name__ for nodo in ast.walk(arbol) if type(nodo).__name__ in _NODOS_DESPUES_DE_38})
    if vistos:
        return False, "usa sintaxis posterior a Python 3.8 (" + ", ".join(vistos) + ")"
    if interprete is None:
        return True, ""
    try:
        proceso = subprocess.run(
            [str(interprete), "-c", "import ast,sys; ast.parse(sys.stdin.read())"],
            input=texto, text=True, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"no se pudo validar con Python 3.8 ({e})"
    if proceso.returncode != 0:
        detalle = (proceso.stderr or proceso.stdout or "Python 3.8 rechazó el código").strip()
        return False, "Python 3.8 rechazó el código: " + detalle[-500:]
    return True, ""


def escribir_archivo(ruta: Path, datos: bytes):
    temporal = ruta.with_name(ruta.name + ".agente-tmp")
    try:
        temporal.write_bytes(datos)
        os.replace(temporal, ruta)
    except Exception:
        if temporal.exists():
            try:
                temporal.unlink()
            except OSError:
                pass
        raise


def _especificar(texto: str, edicion: dict, relativa: str):
    """('rango', inicio, fin, reemplazar, buscar) o ('texto', buscar, reemplazar). Los números son del archivo original."""
    if not any(clave in edicion for clave in ("reemplazar", "replace", "nuevo")):
        return None, f"edición de {relativa}: falta 'reemplazar'"
    reemplazar = como_texto(campo(edicion, "reemplazar", "replace", "nuevo"))
    buscar = como_texto(campo(edicion, "buscar", "search", "viejo"))
    if reemplazar is None:
        return None, f"edición de {relativa}: 'reemplazar' no es texto"
    reemplazar = quitar_numeros_de_linea(reemplazar)
    inicio = como_entero(campo(edicion, "inicio", "start"))
    fin = como_entero(campo(edicion, "fin", "end"))
    if buscar:
        rango = rango_numerado(buscar)
        if rango and inicio is None:
            inicio, fin, buscar = rango
        else:
            buscar = quitar_numeros_de_linea(buscar)

    if inicio is not None:
        if fin is None:
            fin = inicio
        prueba, error = reemplazar_rango(texto, inicio, fin, reemplazar, buscar or "")
        if prueba is not None:
            return ("rango", inicio, fin, reemplazar, buscar or ""), ""
        if buscar and buscar.strip():
            por_texto, error_texto = reemplazar_texto(texto, buscar, reemplazar)
            if por_texto is not None:
                return ("texto", buscar, reemplazar), ""
            return None, error + "\n" + error_texto
        return None, error

    if not buscar or buscar.strip() == "":
        return None, f"edición de {relativa}: 'buscar' vacío y no hay números de línea"
    prueba, error = reemplazar_texto(texto, buscar, reemplazar)
    if prueba is None:
        return None, error
    return ("texto", buscar, reemplazar), ""


def _rangos_superpuestos(rangos: list) -> bool:
    pares = sorted((inicio, fin) for _tipo, inicio, fin, _reemplazar, _buscar in rangos)
    for i in range(1, len(pares)):
        if pares[i][0] <= pares[i - 1][1]:
            return True
    return False


def _aplicar_grupo(texto: str, ediciones: list, relativa: str):
    """Aplica las ediciones de un archivo sobre el texto original. Los rangos van de abajo hacia arriba."""
    rangos = []
    textos = []
    for edicion in ediciones:
        spec, error = _especificar(texto, edicion, relativa)
        if spec is None:
            return None, error
        if spec[0] == "rango":
            rangos.append(spec)
        else:
            textos.append(spec)
    if _rangos_superpuestos(rangos):
        return None, f"hay ediciones superpuestas en {relativa}; no se aplicó ninguna"
    actual = texto
    for _tipo, inicio, fin, reemplazar, buscar in sorted(rangos, key=lambda spec: spec[1], reverse=True):
        actual, error = reemplazar_rango(actual, inicio, fin, reemplazar, buscar)
        if actual is None:
            return None, error
    for _tipo, buscar, reemplazar in textos:
        actual, error = reemplazar_texto(actual, buscar, reemplazar)
        if actual is None:
            return None, error
    return actual, ""


def _ruta_de_edicion(raiz: Path, edicion: dict):
    if not isinstance(edicion, dict) or not isinstance(edicion.get("archivo"), str):
        return None, "edición mal formada (falta el archivo)"
    ruta_bruta = edicion["archivo"].strip().lstrip("./").replace("\\", "/")
    ruta = (raiz / ruta_bruta).resolve()
    try:
        relativa = ruta.relative_to(raiz).as_posix()
    except ValueError:
        return None, f"{edicion['archivo']} está fuera del repositorio"
    if regex_test.search(relativa):
        return None, f"{relativa} es un archivo de test, no se puede modificar"
    if not ruta.is_file():
        return None, f"no existe el archivo {relativa}"
    return relativa, ""


def aplicar_ediciones(repo: Path, ediciones):
    """Aplica todas las ediciones o ninguna. Si la escritura falla, restaura los archivos tocados."""
    if not isinstance(ediciones, list) or len(ediciones) == 0:
        return False, "no se propuso ninguna edición"

    raiz = repo.resolve()
    originales = {}
    saltos = {}
    grupos = {}
    orden = []
    for edicion in ediciones:
        relativa, error = _ruta_de_edicion(raiz, edicion)
        if relativa is None:
            return False, error
        if relativa not in grupos:
            crudo = (raiz / relativa).read_bytes()
            saltos[relativa] = b"\r\n" if b"\r\n" in crudo else b"\n"
            originales[relativa] = crudo
            grupos[relativa] = []
            orden.append(relativa)
        grupos[relativa].append(edicion)

    nuevos = {}
    for relativa in orden:
        texto = originales[relativa].decode("utf-8", errors="replace").replace("\r\n", "\n")
        nuevo, error = _aplicar_grupo(texto, grupos[relativa], relativa)
        if nuevo is None:
            return False, f"{relativa}: {error}"
        if nuevo == texto:
            return False, f"{relativa}: la edición no modifica el archivo"
        nuevos[relativa] = nuevo

    interprete = interprete_py38(repo)
    for relativa, nuevo in nuevos.items():
        ok, error = sintaxis_py38(nuevo, interprete)
        if not ok:
            return False, f"{relativa}: {error}"

    with candado_repo:
        if repo_esta_cerrado():
            return False, "se acabó el tiempo; no se escribió nada"
        try:
            for relativa, nuevo in nuevos.items():
                datos = nuevo.encode("utf-8")
                if saltos.get(relativa) == b"\r\n":
                    datos = datos.replace(b"\n", b"\r\n")
                escribir_archivo(raiz / relativa, datos)
        except Exception as e:
            for relativa, crudo in originales.items():
                try:
                    (raiz / relativa).write_bytes(crudo)
                except Exception:
                    revertir_cambios(repo)
                    break
            return False, f"falló la escritura ({e}); se restauraron los archivos"
    return True, ""


def cabecera_de_clase(repo: Path, region: dict, vistas: set) -> str:
    nombre = region.get("funcion", "")
    if "." not in nombre:
        return ""
    clase = nombre.rsplit(".", 1)[0]
    clave = (region["archivo"], clase)
    if clave in vistas:
        return ""
    todas = [f for f in listar_funciones(repo) if f["archivo"] == region["archivo"]]
    definicion = next((f for f in todas if f["tipo"] == "clase" and f["nombre"] == clase), None)
    if definicion is None:
        return ""
    metodos = [f["inicio"] for f in todas if f["tipo"] == "funcion" and f["nombre"].startswith(clase + ".")]
    fin = min(metodos) - 1 if metodos else definicion["fin"]
    fin = min(fin, definicion["inicio"] + 30)
    if fin < definicion["inicio"]:
        return ""
    vistas.add(clave)
    return (f"### {region['archivo']} cabecera de la clase {clase} (líneas {definicion['inicio']}-{fin})\n"
            + numerar_lineas(leer_lineas(repo, region["archivo"]), definicion["inicio"], fin))

# ------------------------------------------------------------------
# Funcion principal
# ------------------------------------------------------------------
def _linea_que_sigue_en_el_repo(traceback: str, repo: Path) -> str:
    """Si el traceback señala una línea de código del proyecto que sigue en el archivo, la devuelve."""
    lineas = (traceback or "").splitlines()
    raiz = str(repo.resolve())
    for i, linea in enumerate(lineas[:-1]):
        coincidencia = re.search(r'File "([^"]+)"', linea)
        if not coincidencia or i + 1 >= len(lineas):
            continue
        ruta = coincidencia.group(1)
        try:
            absoluta = str(Path(ruta).resolve())
        except OSError:
            continue
        if not absoluta.startswith(raiz) or regex_test.search(absoluta):
            continue
        codigo = lineas[i + 1].strip()
        if not codigo or codigo.startswith("File "):
            continue
        try:
            texto = Path(absoluta).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if codigo in texto:
            return codigo
    return ""


def _firma_del_error(traceback: str) -> str:
    """Una línea distintiva del error, para saber si un intento cambió el fallo."""
    for linea in reversed((traceback or "").splitlines()):
        limpia = linea.strip()
        if not limpia or set(limpia) <= set("-="):
            continue
        if limpia.startswith(("File ", "Traceback", "FAILED", "OK", "Ran ")):
            continue
        return limpia[:240]
    return ""


def _cerrar_por_regresion(carpeta, info, cambio, historial):
    """Mide regresiones. Devuelve 'ok', 'regresion' o 'no_verificado'."""
    estado, rotos = verificar_regresiones(carpeta, info)
    if estado == "regresion":
        historial.append((cambio, "El test del bug pasa, pero este cambio rompe tests que pasaban:\n" + "\n".join(rotos[:20])))
    return estado


def reparar(carpeta: Path, info: dict, localizacion: dict) -> dict:
    repo = carpeta / "repo"
    iniciar_linea_base(carpeta, info)  # si el localizador ya la inicio, no hace nada
    localizacion = dict(localizacion)
    localizacion["regiones"] = intercalar_callees(repo, localizacion.get("regiones") or [])

    historial = []  # lista de (cambio, por que fallo)
    intentos = 0
    nota = ""

    for intento in range(INTENTOS_MAXIMOS):
        if repo_esta_cerrado():
            nota = "se acabó el tiempo"
            break

        def cabe_llamada():
            return puede_llamar_modelo(
                segundos_restantes(), linea_base["listo"].is_set(),
                linea_base.get("duracion"), linea_base.get("completo"), medicion.get("test_bug"),
            )

        def cabe_edicion():
            return puede_editar(
                segundos_restantes(), linea_base.get("duracion"),
                linea_base.get("completo"), medicion.get("test_bug"),
            )

        if linea_base["iniciada"] and linea_base["listo"].is_set() and not cabe_llamada():
            nota = "no queda tiempo para un intento y para repetir las regresiones"
            break

        # El primer intento ve la región principal y sus funciones relacionadas.
        # Cada fallo agrega una región más.
        prompt = armar_prompt(repo, localizacion, regiones_visibles(localizacion["regiones"], intento), historial)
        temperatura = min(0.4, 0.1 + 0.08 * intento)
        try:
            respuesta = consultar_gemini_json(prompt, temperatura)
        except Exception as e:
            nota = f"falló Gemini: {e}"
            print(f"[Intento {intento + 1}] {nota}", flush=True)
            if "Se acabo el tiempo" in str(e) or "Error de la API" in str(e):
                break
            continue
        intentos += 1
        if repo_esta_cerrado():
            nota = "se acabó el tiempo"
            break

        # No editamos hasta que la linea base haya terminado: el paquete es editable
        # y pytest estaria importando el archivo mientras lo cambiamos.
        esperar_linea_base()
        if repo_esta_cerrado():
            nota = "se acabó el tiempo"
            break
        if not linea_base.get("completo") or linea_base.get("tests") is None:
            nota = "no se pudo medir la línea base completa; no se edita el repo"
            break
        if not cabe_edicion():
            nota = "no queda tiempo para editar y volver a medir las regresiones"
            break
        revertir_cambios(repo)
        aplicado, error = aplicar_ediciones(repo, ediciones_de_respuesta(respuesta))
        if not aplicado:
            revertir_cambios(repo)
            print(f"[Intento {intento + 1}] No se pudo aplicar el cambio: {error}", flush=True)
            propuesta = json.dumps(ediciones_de_respuesta(respuesta), ensure_ascii=False)[:2500]
            historial.append((propuesta, f"No se pudo aplicar el cambio: {error}"))
            continue
        cambio = git(repo, "diff")
        if diff_equivalente(cambio, historial):
            print(f"[Intento {intento + 1}] Cambio equivalente a uno anterior. No se vuelven a correr los tests.", flush=True)
            revertir_cambios(repo)
            historial.append((
                cambio,
                "Este cambio es equivalente a uno que ya falló. Cambia la estrategia: "
                "tienen que cumplirse todas las aserciones, no basta con ocultar la primera excepción.",
            ))
            continue

        reserva = reserva_regresion(linea_base.get("duracion"), True) or 0
        disponible = segundos_restantes() - reserva - MARGEN_CIERRE
        if disponible < 8:
            revertir_cambios(repo)
            nota = "no queda tiempo para el test del bug y las regresiones"
            break
        estado_tests = ejecutar_tests(carpeta, min(45, max(8, disponible)))
        if repo_esta_cerrado():
            nota = "se acabó el tiempo"
            break
        if not estado_tests["pasan"]:
            print(f"[Intento {intento + 1}] Los tests del bug siguen fallando", flush=True)
            print(recortar(cambio, 900), flush=True)
            firma_original = _firma_del_error(localizacion.get("salida_tests", ""))
            firma_nueva = _firma_del_error(estado_tests["traceback"])
            if firma_nueva and firma_nueva != firma_original:
                aviso = "El fallo cambió respecto al código original. Conserva este arreglo y corrige el error nuevo:\n"
            else:
                aviso = "El fallo sigue siendo el mismo:\n"
            if any(cambio == previo for previo, _motivo in historial):
                aviso += "Este diff es igual a uno que ya falló. No lo repitas.\n"
            sigue = _linea_que_sigue_en_el_repo(estado_tests["traceback"], repo)
            if sigue:
                aviso += f"Esta línea sigue en el archivo y el traceback la señala: {sigue}\nSustitúyela; no añadas otra al lado.\n"
            revertir_cambios(repo)
            historial.append((cambio, aviso + recortar(estado_tests["traceback"], 2500)))
            continue

        print(f"[Intento {intento + 1}] El test del bug pasó.", flush=True)
        if repo_esta_cerrado():
            nota = "se acabó el tiempo"
            break
        estado = _cerrar_por_regresion(carpeta, info, cambio, historial)
        if repo_esta_cerrado():
            nota = "se acabó el tiempo"
            break
        if estado == "regresion":
            print("[-] La reparación causó regresiones. Revirtiendo...", flush=True)
            revertir_cambios(repo)
            continue
        if estado == "ok":
            if marcar_reparacion_validada():
                print("[+] Reparación exitosa y sin regresiones.", flush=True)
                return {"resuelto": True, "intentos": intentos, "nota": "tests del bug OK y sin regresiones"}
            nota = "se acabó el tiempo y se revirtió el arreglo"
            break

        # Sin medición de regresiones no se aprueba.
        print("[-] No se pudieron comprobar las regresiones. No se acepta el arreglo.", flush=True)
        revertir_cambios(repo)
        nota = "tests del bug OK, pero no se pudieron verificar regresiones"
        if not cabe_llamada():
            break
        historial.append((cambio, "El test del bug pasó, pero no se pudo comprobar que no rompe otros tests. Se revirtió."))
        continue

    if not reparacion_esta_validada():
        revertir_cambios(repo)
    return {"resuelto": False, "intentos": intentos, "nota": nota}


if __name__ == "__main__":
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    loc = json.loads(Path(sys.argv[2]).read_text())
    print(json.dumps(reparar(carpeta, info, loc), indent=2, ensure_ascii=False))