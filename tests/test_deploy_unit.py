"""
Pruebas de la unidad systemd de despliegue (`deploy/liga-bot.service`).

El arranque del servicio no debe sincronizar el entorno virtual: `uv run` sin
`--no-sync` instala o actualiza paquetes antes de ejecutar el comando, y con
`Restart=always` un fallo de red se convertiría en un bucle de descargas.
Las dependencias solo se instalan con `uv sync --frozen` (`deploy/README.md`).

La parada debe entregar una única SIGTERM al proceso de Python: con el `KillMode`
por defecto (`control-group`) systemd la envía a todo el grupo y `uv run` la
reenvía además a su hijo, y la segunda señal fuerza la salida antes de completar
el cierre ordenado (`liga_bot.__main__.run_bot`).
"""

import shlex
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy"
UNIT_PATH = DEPLOY_DIR / "liga-bot.service"
README_PATH = DEPLOY_DIR / "README.md"


def _service_directives() -> list[tuple[str, str]]:
    """Devuelve las directivas (clave, valor) de la sección [Service] en orden."""
    directives: list[tuple[str, str]] = []
    section = ""
    for raw_line in UNIT_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "Service" and "=" in line:
            key, value = line.split("=", 1)
            directives.append((key.strip(), value.strip()))
    return directives


def _exec_start_argv() -> list[str]:
    exec_starts = [value for key, value in _service_directives() if key == "ExecStart"]
    assert len(exec_starts) == 1, f"Se esperaba un único ExecStart, hay {len(exec_starts)}"
    return shlex.split(exec_starts[0])


def test_exec_start_runs_liga_bot_with_uv_run() -> None:
    argv = _exec_start_argv()

    assert Path(argv[0]).name == "uv"
    assert argv[1] == "run"
    assert argv[-1] == "liga-bot"


def test_exec_start_does_not_sync_environment_on_start() -> None:
    argv = _exec_start_argv()
    uv_options = argv[2:-1]

    assert "--no-sync" in uv_options, (
        "ExecStart debe usar 'uv run --no-sync': sin esa opción uv sincroniza .venv "
        "(y puede descargar paquetes) en cada arranque"
    )


def test_stop_signal_is_sent_only_to_uv_main_process() -> None:
    kill_modes = [value for key, value in _service_directives() if key == "KillMode"]

    assert kill_modes == ["mixed"], (
        "La unidad debe fijar 'KillMode=mixed': con el valor por defecto "
        "(control-group) el bot recibe SIGTERM de systemd y otra reenviada por uv, "
        "y la segunda fuerza la salida antes de completar el cierre"
    )


def test_readme_explains_how_to_apply_kill_mode_to_installed_unit() -> None:
    update_section = README_PATH.read_text(encoding="utf-8").split(
        "Actualizar a una versión nueva", 1
    )[1]

    assert "KillMode=mixed" in update_section, (
        "La sección de actualización de deploy/README.md debe explicar cómo añadir "
        "'KillMode=mixed' a una unidad instalada"
    )


def test_readme_installs_dependencies_with_frozen_sync_before_restart() -> None:
    update_section = README_PATH.read_text(encoding="utf-8").split(
        "Actualizar a una versión nueva", 1
    )[1]

    assert "uv sync --frozen" in update_section, (
        "La sección de actualización de deploy/README.md debe instalar las dependencias "
        "con 'uv sync --frozen'"
    )
    assert "systemctl restart liga-bot" in update_section, (
        "La sección de actualización de deploy/README.md debe reiniciar el servicio"
    )
    sync_position = update_section.index("uv sync --frozen")
    restart_position = update_section.index("systemctl restart liga-bot")
    assert sync_position < restart_position
