#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Instaleap Control Tower Bot  –  Lanzador macOS
#  Doble clic en este archivo para abrir el bot en Terminal.
# ─────────────────────────────────────────────────────────────────────────────
cd "$(dirname "$0")"

# Verificar Chromium (requerido por Playwright)
PW_CACHE="$HOME/.cache/ms-playwright"
PW_CACHE_MAC="$HOME/Library/Caches/ms-playwright"

if [ ! -d "$PW_CACHE" ] && [ ! -d "$PW_CACHE_MAC" ]; then
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║   Primera ejecución: instalando Chromium para Playwright    ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""
    echo "Esto solo ocurre una vez (~150 MB). Espera..."
    echo ""
    pip3 install playwright --quiet 2>/dev/null || python3 -m pip install playwright --quiet
    python3 -m playwright install chromium
    echo ""
    echo "✅  Chromium instalado. Iniciando bot..."
    echo ""
fi

# Ejecutar el bot
./instaleap_bot
