"""REPARADOR: modifica el código fuente de repo/ para que los tests del bug pasen.

Uso por separado (con una localización ya guardada):
    python reparador.py <carpeta_del_bug> <localizacion.json>

Ciclo (hasta 4 intentos):
  1. Armar el prompt con el error, el test y el código sospechoso
  2. Pedirle a Gemini los cambios (buscar / reemplazar)
  3. Aplicar los cambios (si no se pueden aplicar o dejan mala sintaxis, cuenta como intento fallido)
  4. Correr los tests del bug
  5. Verificar que no se rompió ningún otro test (regresiones)
  6. Si algo falla: revertir y reintentar contándole a Gemini qué pasó

Devuelve {"resuelto": True/False, ...}
"""
import ast
import json
import re
import sys
from pathlib import Path
from utils import (ejecutar_tests, consultar_gemini_json, verificar_regresiones, iniciar_linea_base,
                   revertir_cambios, git, leer_lineas, numerar_lineas, recortar, segundos_restantes,
                   regex_test, tests_rotos)

INTENTOS_MAXIMOS = 4
MAX_CARACTERES_CODIGO = 18000  # tope de codigo fuente dentro del prompt
MARGEN = 8                     # lineas extra arriba y abajo de cada region
MAX_LINEAS_REGION = 140
TIEMPO_MINIMO = 25             # si quedan menos segundos que esto no empezamos otro intento

INSTRUCCIONES = """Eres un ingeniero experto en arreglar bugs de proyectos Python (se ejecutan con Python 3.8).
Te muestro la salida de tests que fallan, el código del test y fragmentos del código fuente con número de línea.
Corrige la CAUSA del bug en el código fuente, de forma general (no adaptes el código al test y no toques los tests).
Haz el cambio más pequeño posible."""

FORMATO = """Responde SOLO con este JSON:
{
  "analisis": "causa del bug en 1 a 3 frases",
  "ediciones": [
    {"archivo": "ruta/relativa/en/repo.py", "buscar": "líneas EXACTAS del código actual", "reemplazar": "líneas nuevas"}
  ]
}
Reglas:
- "buscar" debe copiar literalmente líneas seguidas del código mostrado (SIN los números de línea ni el '|'), con su indentación, y tiene que ser único en el archivo. Si hace falta incluye 1 o 2 líneas de contexto.
- "reemplazar" es el texto que lo va a sustituir.
- Si necesitas un import nuevo, haz otra edición que lo agregue.
- No modifiques archivos de test. El código debe ser compatible con Python 3.8."""


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


def armar_contexto(repo: Path, regiones: list, cuantas: int) -> str:
    """Junta el codigo de las primeras regiones (sin pasarse del tope de caracteres)."""
    partes = []
    total = 0
    archivos_vistos = []
    for region in regiones[:cuantas]:
        try:
            bloque = codigo_de_region(repo, region)
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


def armar_prompt(repo: Path, localizacion: dict, cuantas_regiones: int, historial: list) -> str:
    datos = ""
    for nombre in localizacion.get("datos_test", {}):
        datos += f"\n## Datos de test {nombre}\n{localizacion['datos_test'][nombre]}\n"

    prompt = f"""{INSTRUCCIONES}

## Test que falla
{recortar(localizacion.get("codigo_test", ""), 3500)}
{datos}
## Salida de los tests (código original)
{recortar(localizacion.get("salida_tests", ""), 4500)}

## Código fuente sospechoso (de más a menos probable)
{armar_contexto(repo, localizacion["regiones"], cuantas_regiones)}
"""
    if localizacion.get("analisis"):
        prompt += f"\n(Nota del localizador: {localizacion['analisis']})\n"

    # Intentos anteriores que fallaron
    numero = 1
    for cambio, motivo in historial:
        prompt += f"""
## Intento previo {numero} (ya revertido)
```diff
{recortar(cambio, 2500)}
```
Resultado: {recortar(motivo, 2500)}
Propón una solución DIFERENTE y mejor.
"""
        numero += 1

    return prompt + "\n" + FORMATO


# ------------------------------------------------------------------
# Aplicar los cambios que propone Gemini
# ------------------------------------------------------------------
def quitar_numeros_de_linea(texto: str) -> str:
    """Si Gemini copio los numeros de linea ('  12| codigo') los quitamos."""
    lineas = texto.split("\n")
    hay_codigo = False
    todas_con_numero = True
    for linea in lineas:
        if linea.strip() != "":
            hay_codigo = True
            if not re.match(r"^\s*\d+\|", linea):
                todas_con_numero = False
    if hay_codigo and todas_con_numero:
        return "\n".join(re.sub(r"^\s*\d+\|\s?", "", l) for l in lineas)
    return texto


def sangria(linea: str) -> int:
    return len(linea) - len(linea.lstrip(" "))


def reemplazar_texto(texto: str, buscar: str, reemplazar: str):
    """Cambia 'buscar' por 'reemplazar'. Devuelve (texto_nuevo, error). Si hay error, texto_nuevo es None."""
    # primero probamos con coincidencia exacta
    if buscar in texto:
        if texto.count(buscar) > 1:
            return None, "el texto de 'buscar' aparece varias veces, agrega más contexto"
        return texto.replace(buscar, reemplazar, 1), ""

    # si no coincide exacto, probamos sin mirar la indentacion, linea por linea
    lineas = texto.split("\n")
    lineas_buscar = buscar.strip("\n").split("\n")
    coincidencias = []
    for i in range(len(lineas) - len(lineas_buscar) + 1):
        igual = True
        for k in range(len(lineas_buscar)):
            if lineas[i + k].strip() != lineas_buscar[k].strip():
                igual = False
                break
        if igual:
            coincidencias.append(i)

    if len(coincidencias) == 0:
        return None, "el texto de 'buscar' no coincide con el código actual"
    if len(coincidencias) > 1:
        return None, "el texto de 'buscar' (sin mirar la indentación) aparece varias veces"

    # arreglamos la indentacion del reemplazo para que calce con la del archivo
    i = coincidencias[0]
    diferencia = sangria(lineas[i]) - sangria(lineas_buscar[0])
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
    return "\n".join(lineas[:i] + nuevas + lineas[i + len(lineas_buscar):]), ""


def aplicar_ediciones(repo: Path, ediciones):
    """Aplica todas las ediciones o ninguna. Devuelve (True, "") o (False, motivo del error)."""
    if not isinstance(ediciones, list) or len(ediciones) == 0:
        return False, "no se propuso ninguna edición"

    raiz = repo.resolve()
    textos = {}  # archivo -> texto ya modificado (todavia no se escribe nada)

    for edicion in ediciones:
        if not isinstance(edicion, dict) or not isinstance(edicion.get("archivo"), str):
            return False, "edición mal formada (faltan archivo, buscar o reemplazar)"
        ruta = (raiz / edicion["archivo"].strip().lstrip("./")).resolve()
        try:
            relativa = ruta.relative_to(raiz).as_posix()
        except ValueError:
            return False, f"{edicion['archivo']} está fuera del repositorio"
        if regex_test.search(relativa):
            return False, f"{relativa} es un archivo de test, no se puede modificar"
        if not ruta.is_file():
            return False, f"no existe el archivo {relativa}"

        buscar = edicion.get("buscar")
        reemplazar = edicion.get("reemplazar")
        if not isinstance(buscar, str) or not isinstance(reemplazar, str) or buscar.strip() == "":
            return False, f"edición de {relativa}: 'buscar' vacío o falta 'reemplazar'"

        if relativa not in textos:
            textos[relativa] = ruta.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
        nuevo, error = reemplazar_texto(textos[relativa], quitar_numeros_de_linea(buscar), quitar_numeros_de_linea(reemplazar))
        if nuevo is None:
            return False, f"{relativa}: {error}"
        textos[relativa] = nuevo

    # revisamos que el codigo siga teniendo sintaxis valida antes de escribir
    for relativa in textos:
        try:
            ast.parse(textos[relativa])
        except SyntaxError as e:
            return False, f"{relativa} quedó con un error de sintaxis: {e.msg} (línea {e.lineno})"

    for relativa in textos:
        (raiz / relativa).write_bytes(textos[relativa].encode("utf-8"))
    return True, ""


# ------------------------------------------------------------------
# Funcion principal
# ------------------------------------------------------------------
def reparar(carpeta: Path, info: dict, localizacion: dict) -> dict:
    repo = carpeta / "repo"
    iniciar_linea_base(carpeta, info)  # si main.py ya la inicio, no hace nada

    historial = []  # lista de (cambio, por que fallo)
    intentos = 0
    nota = ""

    for intento in range(INTENTOS_MAXIMOS):
        if segundos_restantes() < TIEMPO_MINIMO:
            nota = "no quedó tiempo para más intentos"
            break

        # En cada intento mostramos una region mas y subimos un poco la temperatura
        prompt = armar_prompt(repo, localizacion, min(3 + intento, 5), historial)
        temperatura = min(0.8, 0.2 + 0.2 * intento)
        try:
            respuesta = consultar_gemini_json(prompt, temperatura)
        except Exception as e:
            nota = f"falló Gemini: {e}"
            break
        intentos += 1

        # Aplicamos el cambio
        revertir_cambios(repo)
        aplicado, error = aplicar_ediciones(repo, respuesta.get("ediciones"))
        if not aplicado:
            revertir_cambios(repo)
            print(f"[Intento {intento + 1}] No se pudo aplicar el cambio: {error}")
            propuesta = json.dumps(respuesta.get("ediciones"), ensure_ascii=False)[:2500]
            historial.append((propuesta, f"No se pudo aplicar el cambio: {error}"))
            continue
        cambio = git(repo, "diff")

        # Corremos los tests del bug
        estado_tests = ejecutar_tests(carpeta, max(10, min(60, segundos_restantes() - 10)))
        if not estado_tests["pasan"]:
            print(f"[Intento {intento + 1}] Los tests del bug siguen fallando")
            revertir_cambios(repo)
            historial.append((cambio, "Los tests del bug siguen fallando:\n" + recortar(estado_tests["traceback"], 2500)))
            continue

        # Revisamos que no se haya roto otra cosa
        print(f"[Intento {intento + 1}] El test objetivo pasó. Verificando regresiones...")
        if not verificar_regresiones(carpeta, info):
            print("[-] La reparación causó regresiones. Revirtiendo...")
            revertir_cambios(repo)
            historial.append((cambio, "El test del bug pasa, pero este cambio rompe tests que pasaban:\n" + "\n".join(tests_rotos[:15])))
            continue

        print("[+] Reparación exitosa y sin regresiones.")
        return {"resuelto": True, "intentos": intentos, "nota": "tests del bug OK y sin regresiones"}

    # Si llegamos aca no se pudo: dejamos el codigo original
    revertir_cambios(repo)
    return {"resuelto": False, "intentos": intentos, "nota": nota}


if __name__ == "__main__":
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    loc = json.loads(Path(sys.argv[2]).read_text())
    print(json.dumps(reparar(carpeta, info, loc), indent=2, ensure_ascii=False))