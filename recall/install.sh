#!/usr/bin/env bash
# recall installer
set -e

INSTALL_DIR="$HOME/.local/bin"
DATA_DIR="$HOME/.local/share/recall"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR"
if [[ -f "$SCRIPT_DIR/recall/Cargo.toml" && -f "$SCRIPT_DIR/recall/src/main.rs" ]]; then
    APP_DIR="$SCRIPT_DIR/recall"
fi
PY_APP="$APP_DIR/recall.py"
RUNTIME_DIR="$DATA_DIR/runtime"
NATIVE_DIR="$DATA_DIR/bin"

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RESET='\033[0m'

echo -e "${CYAN}  ◈  recall installer${RESET}"
echo ""

# ── Create directories ────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR"
mkdir -p "$DATA_DIR"
mkdir -p "$NATIVE_DIR"

# ── Check Python ──────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install it with: sudo apt install python3 python3-pip"
    exit 1
fi
if ! python3 -m venv --help >/dev/null 2>&1; then
    echo "ERROR: python3 venv support not found. Install it with: sudo apt install python3-venv"
    exit 1
fi

# ── Prepare isolated Python runtime ───────────────────────────────────────────
if [[ ! -f "$PY_APP" ]]; then
    echo "ERROR: recall.py not found at $PY_APP"
    exit 1
fi

echo -e "${YELLOW}  → Creating isolated Python runtime...${RESET}"
python3 -m venv "$RUNTIME_DIR"

echo -e "${YELLOW}  → Installing Python dependencies into app runtime...${RESET}"
"$RUNTIME_DIR/bin/python" -m pip install --quiet --upgrade pip
"$RUNTIME_DIR/bin/python" -m pip install --quiet textual rich rapidfuzz

echo -e "${GREEN}  ✓ Dependencies installed${RESET}"

# ── Copy Python app payload ───────────────────────────────────────────────────
cp "$PY_APP" "$DATA_DIR/recall.py"

# ── Install stable launcher ───────────────────────────────────────────────────
cat >"$INSTALL_DIR/recall" <<'EOF'
#!/usr/bin/env bash
set -e

DATA_DIR="$HOME/.local/share/recall"
NATIVE_BIN="$DATA_DIR/bin/recall"
PYTHON_BIN="$DATA_DIR/runtime/bin/python"
PY_APP="$DATA_DIR/recall.py"

if [[ -x "$PYTHON_BIN" && -f "$PY_APP" ]]; then
    if [[ "${RECALL_USE_NATIVE:-0}" != "1" ]]; then
        exec "$PYTHON_BIN" "$PY_APP" "$@"
    fi
fi

if [[ -x "$NATIVE_BIN" ]]; then
    exec "$NATIVE_BIN" "$@"
fi

echo "recall is not installed correctly. Re-run install.sh." >&2
exit 1
EOF
chmod +x "$INSTALL_DIR/recall"

echo -e "${GREEN}  ✓ Installed to $INSTALL_DIR/recall${RESET}"

# ── Rust build (optional) ─────────────────────────────────────────────────────
if command -v cargo &>/dev/null; then
    echo -e "${YELLOW}  → Rust found — building native binary (faster startup)...${RESET}"
    cd "$APP_DIR"
    if cargo build --release 2>/dev/null; then
        cp "$APP_DIR/target/release/recall" "$NATIVE_DIR/recall"
        echo -e "${GREEN}  ✓ Native Rust binary installed${RESET}"
    else
        echo -e "${YELLOW}  ⚠ Rust build failed — using Python fallback (fully functional)${RESET}"
    fi
else
    echo -e "${YELLOW}  ⚠ Rust not found — using Python version (fully functional)${RESET}"
    echo "    To build native binary later: install rustup, then run: cargo build --release"
fi

# ── PATH check ────────────────────────────────────────────────────────────────
if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
    echo ""
    echo -e "${YELLOW}  ⚠ $INSTALL_DIR is not in your PATH${RESET}"
    echo "    Add this to your ~/.zshrc or ~/.bashrc:"
    echo ""
    echo "      export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
fi

echo ""
echo -e "${GREEN}  ✓ recall is ready${RESET}"
echo ""
echo "  Usage:"
echo "    recall                          — launch TUI"
echo "    recall add -t 'title' -c 'cmd' -C command --tags 'nmap,recon'"
echo "    recall search <query>           — search from terminal"
echo "    recall db-path                  — show database location"
echo ""
echo "  Keybindings:"
echo "    Ctrl+N   add entry"
echo "    Ctrl+E   edit selected"
echo "    Ctrl+X   delete selected"
echo "    Ctrl+Y   yank content to clipboard"
echo "    F1-F4    filter by category"
echo "    Ctrl+O   open \$EDITOR for content field (in form)"
echo "    Ctrl+Q   quit"
echo ""
echo "  Database: $DATA_DIR/recall.db"
