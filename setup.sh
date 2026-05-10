#!/usr/bin/env bash
# ─── Instaleap Control Tower Bot – Setup ──────────────────────────────────────
set -e

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Instaleap Control Tower Bot – Setup       ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Python 3.10+
if ! command -v python3 &>/dev/null; then
  echo "❌  Python 3 no encontrado. Instálalo en https://python.org"
  exit 1
fi

PY_VER=$(python3 -c 'import sys; print(sys.version_info >= (3,10))')
if [ "$PY_VER" != "True" ]; then
  echo "❌  Se requiere Python 3.10 o superior."
  exit 1
fi

echo "✓  Python: $(python3 --version)"

# Virtualenv
if [ ! -d ".venv" ]; then
  echo "→  Creando entorno virtual..."
  python3 -m venv .venv
fi

source .venv/bin/activate
echo "✓  Entorno virtual activado"

# Dependencias
echo "→  Instalando dependencias..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# Playwright browser
echo "→  Instalando navegador Chromium para Playwright..."
playwright install chromium

echo ""
echo "✅  Setup completado."
echo ""
echo "Para ejecutar el bot:"
echo "  source .venv/bin/activate"
echo "  python instaleap_bot.py"
echo ""
