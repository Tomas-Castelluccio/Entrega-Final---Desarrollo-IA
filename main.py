"""System Doctor: diagnóstico local y asistente para Windows.

No modifica configuraciones del equipo. Ejecuta comprobaciones de solo lectura
y presenta hipótesis; no sustituye la revisión de un técnico cuando hay fallos
de hardware o pérdida de datos.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from typing import Callable

import streamlit as st


APP_NAME = "System Doctor"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_LLM_MODEL = "gpt-5.6"


@dataclass
class Finding:
    severity: str
    title: str
    detail: str
    recommendation: str


@dataclass
class Snapshot:
    computer_name: str
    windows: str
    cpu_model: str
    cpu_usage: float | None
    memory_total_gb: float | None
    memory_used_gb: float | None
    memory_usage: float | None
    disk_total_gb: float | None
    disk_free_gb: float | None
    disk_free_percent: float | None
    uptime_hours: float | None
    process_count: int | None
    timestamp: str


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def run_command(command: list[str]) -> str:
    """Run a short, read-only system command without showing a console."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def powershell(expression: str) -> str:
    return run_command(["powershell", "-NoProfile", "-Command", expression])


def read_cpu_usage() -> float | None:
    """Estimate total CPU usage with the Windows GetSystemTimes API."""
    if os.name != "nt":
        return None
    idle1 = ctypes.c_ulonglong()
    kernel1 = ctypes.c_ulonglong()
    user1 = ctypes.c_ulonglong()
    idle2 = ctypes.c_ulonglong()
    kernel2 = ctypes.c_ulonglong()
    user2 = ctypes.c_ulonglong()
    get_times = ctypes.windll.kernel32.GetSystemTimes
    if not get_times(ctypes.byref(idle1), ctypes.byref(kernel1), ctypes.byref(user1)):
        return None
    time.sleep(0.7)
    if not get_times(ctypes.byref(idle2), ctypes.byref(kernel2), ctypes.byref(user2)):
        return None
    total = (kernel2.value - kernel1.value) + (user2.value - user1.value)
    idle = idle2.value - idle1.value
    return round(max(0, min(100, (total - idle) * 100 / total)), 1) if total else None


def read_memory() -> tuple[float | None, float | None, float | None]:
    if os.name != "nt":
        return None, None, None
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None, None, None
    total = status.ullTotalPhys / 1024**3
    used = (status.ullTotalPhys - status.ullAvailPhys) / 1024**3
    return round(total, 1), round(used, 1), round(status.dwMemoryLoad, 1)


def read_cpu_model() -> str:
    value = powershell("(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name)")
    return value or platform.processor() or "No disponible"


def read_process_count() -> int | None:
    output = run_command(["tasklist", "/NH"])
    return len([line for line in output.splitlines() if line.strip()]) if output else None


def collect_snapshot(progress: Callable[[str], None] | None = None) -> Snapshot:
    def report(text: str) -> None:
        if progress:
            progress(text)

    report("Midiendo actividad del procesador...")
    cpu_usage = read_cpu_usage()
    report("Leyendo memoria y almacenamiento...")
    memory_total, memory_used, memory_usage = read_memory()
    drive = os.environ.get("SystemDrive", "C:") + "\\"
    try:
        disk = shutil.disk_usage(drive)
        disk_total = round(disk.total / 1024**3, 1)
        disk_free = round(disk.free / 1024**3, 1)
        disk_free_percent = round(disk.free * 100 / disk.total, 1)
    except OSError:
        disk_total = disk_free = disk_free_percent = None
    report("Consultando información del sistema...")
    uptime = round(ctypes.windll.kernel32.GetTickCount64() / 3_600_000, 1) if os.name == "nt" else None
    return Snapshot(
        computer_name=socket.gethostname(),
        windows=platform.platform(),
        cpu_model=read_cpu_model(),
        cpu_usage=cpu_usage,
        memory_total_gb=memory_total,
        memory_used_gb=memory_used,
        memory_usage=memory_usage,
        disk_total_gb=disk_total,
        disk_free_gb=disk_free,
        disk_free_percent=disk_free_percent,
        uptime_hours=uptime,
        process_count=read_process_count(),
        timestamp=time.strftime("%d/%m/%Y %H:%M:%S"),
    )


def diagnose(snapshot: Snapshot) -> list[Finding]:
    findings: list[Finding] = []
    if snapshot.cpu_usage is not None:
        if snapshot.cpu_usage >= 90:
            findings.append(Finding("Crítico", "Uso de CPU muy alto", f"El procesador está al {snapshot.cpu_usage}% durante la medición.", "Abra el Administrador de tareas, ordene por CPU e identifique el proceso sostenido. Reinicie solo aplicaciones que reconozca."))
        elif snapshot.cpu_usage >= 70:
            findings.append(Finding("Atención", "Uso de CPU elevado", f"El procesador está al {snapshot.cpu_usage}%.", "Si este nivel se mantiene mientras el equipo está inactivo, revise procesos en segundo plano y actualizaciones en curso."))
    if snapshot.memory_usage is not None:
        if snapshot.memory_usage >= 90:
            findings.append(Finding("Crítico", "Memoria RAM casi agotada", f"Se usan {snapshot.memory_used_gb} de {snapshot.memory_total_gb} GB ({snapshot.memory_usage}%).", "Cierre aplicaciones que no utilice y reduzca pestañas del navegador. Si se repite, valore ampliar la RAM."))
        elif snapshot.memory_usage >= 75:
            findings.append(Finding("Atención", "Uso de memoria elevado", f"Se usan {snapshot.memory_used_gb} de {snapshot.memory_total_gb} GB ({snapshot.memory_usage}%).", "Revise aplicaciones abiertas; observe si el consumo aumenta continuamente, señal de una posible fuga de memoria."))
    if snapshot.disk_free_percent is not None:
        if snapshot.disk_free_percent < 5:
            findings.append(Finding("Crítico", "Espacio en disco crítico", f"Solo quedan {snapshot.disk_free_gb} GB libres ({snapshot.disk_free_percent}%).", "Libere espacio con Archivos temporales de Windows y mueva archivos personales. Evite borrar archivos del sistema manualmente."))
        elif snapshot.disk_free_percent < 15:
            findings.append(Finding("Atención", "Poco espacio en disco", f"Quedan {snapshot.disk_free_gb} GB libres ({snapshot.disk_free_percent}%).", "Mantenga al menos 15–20% libre para actualizaciones, memoria virtual y buen rendimiento."))
    if snapshot.uptime_hours is not None and snapshot.uptime_hours > 168:
        findings.append(Finding("Información", "Tiempo prolongado sin reinicio", f"El sistema lleva {snapshot.uptime_hours:.0f} horas activo.", "Un reinicio programado puede completar actualizaciones y liberar recursos acumulados."))
    if not findings:
        findings.append(Finding("Correcto", "No se detectaron anomalías inmediatas", "CPU, memoria y almacenamiento están dentro de los umbrales de esta medición.", "Si el problema es intermitente, ejecute el análisis mientras ocurra o describa el síntoma al asistente."))
    return findings


def format_report(snapshot: Snapshot, findings: list[Finding]) -> str:
    lines = [
        f"INFORME DE SYSTEM DOCTOR — {snapshot.timestamp}",
        "=" * 58,
        f"Equipo: {snapshot.computer_name}",
        f"Sistema: {snapshot.windows}",
        f"CPU: {snapshot.cpu_model}",
        f"Actividad de CPU: {snapshot.cpu_usage if snapshot.cpu_usage is not None else 'N/D'}%",
        f"RAM: {snapshot.memory_used_gb if snapshot.memory_used_gb is not None else 'N/D'} / {snapshot.memory_total_gb if snapshot.memory_total_gb is not None else 'N/D'} GB ({snapshot.memory_usage if snapshot.memory_usage is not None else 'N/D'}%)",
        f"Disco del sistema: {snapshot.disk_free_gb if snapshot.disk_free_gb is not None else 'N/D'} GB libres de {snapshot.disk_total_gb if snapshot.disk_total_gb is not None else 'N/D'} GB ({snapshot.disk_free_percent if snapshot.disk_free_percent is not None else 'N/D'}% libre)",
        f"Procesos activos: {snapshot.process_count if snapshot.process_count is not None else 'N/D'}",
        f"Tiempo activo: {snapshot.uptime_hours if snapshot.uptime_hours is not None else 'N/D'} horas",
        "\nHALLAZGOS",
        "-" * 58,
    ]
    for item in findings:
        lines.extend([f"[{item.severity}] {item.title}", item.detail, f"Acción sugerida: {item.recommendation}", ""])
    return "\n".join(lines)


def snapshot_context(snapshot: Snapshot | None, findings: list[Finding]) -> str:
    """Build the factual context sent to the model; it must not invent readings."""
    if snapshot is None:
        return "No hay una medición local disponible. Indica qué comprobaciones realizar primero."
    return json.dumps(
        {
            "medicion_local": asdict(snapshot),
            "hallazgos_por_reglas": [asdict(item) for item in findings],
        },
        ensure_ascii=False,
        indent=2,
    )


def extract_response_text(response: dict) -> str:
    """Read text from a Responses API object without relying on the SDK."""
    text = response.get("output_text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    pieces: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                pieces.append(content["text"])
    return "\n".join(pieces).strip()


def generate_llm_diagnosis(problem: str, snapshot: Snapshot | None, findings: list[Finding]) -> str:
    """Ask the configured LLM for a safe, ordered diagnosis report."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Falta configurar OPENAI_API_KEY. Define esa variable de entorno y reinicia la aplicación."
        )

    instructions = """Eres System Doctor, un asistente de diagnóstico de PC para usuarios de Windows.
Genera un informe en español, concreto y accionable. Usa exclusivamente el problema
descrito y las métricas recibidas; distingue siempre hechos observados de hipótesis.
No indiques cambios automáticos ni acciones destructivas. No inventes comandos,
temperaturas, malware ni fallas de hardware. Antes de sugerir una acción con posible
pérdida de datos o cambio de configuración, indica una precaución o copia de seguridad.

Usa exactamente esta estructura:
1. RESUMEN DEL DIAGNÓSTICO: 2 a 4 frases, con nivel de urgencia (crítico, alto, medio o bajo).
2. EVIDENCIA DISPONIBLE: viñetas; separa métricas locales, hechos del usuario y limitaciones.
3. PLAN DE ACCIÓN PRIORIZADO: una lista numerada de 3 a 6 pasos. Cada paso debe incluir
   Prioridad (P0/P1/P2), qué hacer, por qué se hace ahora y qué resultado comprobar.
   P0 es inmediato/seguridad/datos; P1 resuelve o aísla la causa; P2 optimiza o previene.
4. CUÁNDO ESCALAR: señales concretas para recurrir a soporte técnico.
5. DATOS QUE FALTAN: máximo 3 preguntas, solo si cambiarían el siguiente paso.
No des una certeza diagnóstica si la evidencia no la sustenta."""
    prompt = (
        f"PROBLEMA DESCRITO POR LA PERSONA:\n{problem.strip()}\n\n"
        f"CONTEXTO LOCAL DE SYSTEM DOCTOR:\n{snapshot_context(snapshot, findings)}"
    )
    payload = json.dumps(
        {
            "model": os.environ.get("SYSTEM_DOCTOR_MODEL", DEFAULT_LLM_MODEL),
            "instructions": instructions,
            "input": prompt,
            "store": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail).get("error", {}).get("message", detail)
        except json.JSONDecodeError:
            pass
        raise RuntimeError(f"El servicio de IA devolvió un error ({error.code}): {detail}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"No se pudo generar el diagnóstico con IA: {error}") from error
    result = extract_response_text(data)
    if not result:
        raise RuntimeError("El servicio de IA no devolvió texto para el diagnóstico.")
    return result


def answer_question(question: str, snapshot: Snapshot | None, findings: list[Finding]) -> str:
    q = question.lower().strip()
    if not snapshot:
        return "Primero ejecuta «Analizar dispositivo». Necesito una medición actual para relacionar la respuesta con tu equipo."
    context = "\n".join(f"• {item.title}: {item.detail}" for item in findings)
    if any(word in q for word in ("disco", "espacio", "almacenamiento", "lento", "lentitud")):
        return f"El disco del sistema tiene {snapshot.disk_free_gb} GB libres ({snapshot.disk_free_percent}%).\n\n{context}\n\nLa lentitud también puede venir de CPU, RAM, programas de inicio o un disco con problemas físicos. Este análisis no prueba el estado SMART del hardware."
    if any(word in q for word in ("ram", "memoria", "navegador", "pestaña")):
        return f"La memoria usa {snapshot.memory_used_gb} de {snapshot.memory_total_gb} GB ({snapshot.memory_usage}%).\n\n{context}\n\nSi el uso sube y no baja tras cerrar programas, identifica el proceso que más memoria consume en el Administrador de tareas."
    if any(word in q for word in ("cpu", "procesador", "calienta", "ventilador")):
        return f"La muestra registró CPU al {snapshot.cpu_usage}%.\n\n{context}\n\nPara investigar calor o ventiladores, comprueba polvo, ventilación y temperaturas con una herramienta de hardware; Windows no las expone de forma fiable en todos los equipos."
    if any(word in q for word in ("virus", "malware", "seguridad", "infect")):
        return "No puedo confirmar malware con estas métricas. Ejecuta un análisis completo desde Seguridad de Windows, mantén las definiciones actualizadas y no elimines archivos solo por su nombre. Si detectas procesos desconocidos, consulta su editor y ruta antes de actuar."
    return f"Diagnóstico actual: CPU {snapshot.cpu_usage}%, RAM {snapshot.memory_usage}% y disco libre {snapshot.disk_free_percent}%.\n\nHallazgos:\n{context}\n\nDescribe el síntoma con detalle (por ejemplo, «el equipo se congela al abrir Chrome») para una orientación más específica."


def render_app() -> None:
    """Render the System Doctor page through Streamlit."""
    st.set_page_config(page_title=APP_NAME, page_icon="🩺", layout="wide")
    st.title("🩺 System Doctor")
    st.write(
        "System Doctor te ayuda a entender problemas de rendimiento o funcionamiento de tu PC. "
        "Combina una medición local de recursos con tu descripción del síntoma para generar un "
        "informe de diagnóstico ordenado, seguro y accionable."
    )

    for key, default in (("snapshot", None), ("findings", []), ("llm_diagnosis", "")):
        if key not in st.session_state:
            st.session_state[key] = default

    with st.expander("Cómo usar esta página", expanded=True):
        st.markdown(
            """1. Pulsa **Analizar dispositivo** para obtener una medición local de CPU, memoria,
            disco y tiempo de actividad. Esta acción solo lee información: no cambia configuraciones.
            2. Escribe el síntoma que observas en **Problema observado**; incluye cuándo ocurre y qué
            aplicación o acción lo desencadena.
            3. Pulsa **Generar diagnóstico con IA**. Recibirás un informe con evidencia, prioridades
            P0/P1/P2 y los pasos recomendados en orden.
            4. Si lo necesitas, usa **Descargar informe JSON** para guardar la medición y el diagnóstico
            y compartirlos con soporte técnico."""
        )

    left, right = st.columns([1, 2])
    with left:
        if st.button("Analizar dispositivo", type="primary", use_container_width=True):
            try:
                with st.spinner("Analizando el dispositivo..."):
                    snapshot = collect_snapshot()
                    st.session_state.snapshot = snapshot
                    st.session_state.findings = diagnose(snapshot)
                st.success("Análisis completado.")
            except Exception as error:
                st.error(f"No se pudo completar el análisis: {error}")

    with right:
        problem = st.text_area(
            "Problema observado",
            placeholder="Ej.: El equipo se congela al abrir Chrome y el ventilador hace mucho ruido.",
            height=90,
        )
        if st.button("Generar diagnóstico con IA", use_container_width=True):
            if not problem.strip():
                st.warning("Describe el problema antes de generar el diagnóstico.")
            else:
                try:
                    with st.spinner("Generando informe de diagnóstico..."):
                        st.session_state.llm_diagnosis = generate_llm_diagnosis(
                            problem, st.session_state.snapshot, st.session_state.findings
                        )
                    st.success("Diagnóstico generado.")
                except Exception as error:
                    st.error(str(error))

    if st.session_state.snapshot:
        st.subheader("Medición local")
        st.code(format_report(st.session_state.snapshot, st.session_state.findings), language=None)
        export = {
            "snapshot": asdict(st.session_state.snapshot),
            "findings": [asdict(item) for item in st.session_state.findings],
            "llm_diagnosis": st.session_state.llm_diagnosis or None,
        }
        st.download_button(
            "Descargar informe JSON",
            data=json.dumps(export, ensure_ascii=False, indent=2),
            file_name=f"system_doctor_{time.strftime('%Y%m%d_%H%M%S')}.json",
            mime="application/json",
        )

    if st.session_state.llm_diagnosis:
        st.subheader("Informe de diagnóstico con IA")
        st.markdown(st.session_state.llm_diagnosis)

    st.caption(
        "La descripción del problema y las métricas obtenidas se envían al modelo configurado "
        "para elaborar el informe."
    )


render_app()
