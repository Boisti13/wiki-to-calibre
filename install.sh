#!/usr/bin/env bash
# Interactive installer for wiki-to-calibre.
# Works either from a cloned repo (./install.sh) or piped straight from GitHub:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Boisti13/wiki-to-calibre/master/install.sh)"
set -euo pipefail

RAW_URL="https://raw.githubusercontent.com/Boisti13/wiki-to-calibre/master/import_wiki.py"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"

# Reading from /dev/tty makes prompts work even when this script is itself
# piped in via curl | bash (stdin is the script, not the terminal, otherwise).
ask() {
    local prompt="$1" default="${2:-}" reply
    if [ -n "$default" ]; then
        read -rp "$prompt [$default]: " reply </dev/tty
        echo "${reply:-$default}"
    else
        read -rp "$prompt: " reply </dev/tty
        echo "$reply"
    fi
}
confirm() {
    local prompt="$1" default="${2:-Y}" reply
    read -rp "$prompt [$([ "$default" = Y ] && echo Y/n || echo y/N)]: " reply </dev/tty
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[Yy] ]]
}

echo "=== wiki-to-calibre installer ==="
echo

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Install Python 3.9+ and re-run this script." >&2
    exit 1
fi
echo "Found: $(python3 --version)"

if ! command -v ebook-convert >/dev/null 2>&1 || ! command -v calibredb >/dev/null 2>&1; then
    echo
    echo "Calibre's command-line tools (ebook-convert, calibredb) were not found on PATH."
    if confirm "Install calibre now via apt?"; then
        if [ "$(id -u)" -ne 0 ]; then
            echo "Installing calibre needs root. Re-run this installer with sudo, or install calibre yourself first." >&2
            exit 1
        fi
        apt-get update -qq
        apt-get install -y calibre
    else
        echo "Skipping -- install calibre and make sure ebook-convert/calibredb are on PATH before running the importer."
    fi
else
    echo "Found calibre CLI tools: $(calibredb --version | head -1)"
fi

echo
LIBRARY="$(ask "Path to your Calibre library (folder containing/to contain metadata.db)")"
mkdir -p "$LIBRARY"
if [ ! -f "$LIBRARY/metadata.db" ]; then
    echo "No metadata.db there yet -- it'll be created automatically on the first import."
fi

PORT="$(ask "Port to serve the importer on" "8084")"
LIBRARY_URL="$(ask "URL of your Calibre/Calibre-Web instance (shown as a link after each import)" "http://localhost:8083")"
INSTALL_DIR="$(ask "Install directory" "/opt/wiki-to-calibre")"

mkdir -p "$INSTALL_DIR"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/import_wiki.py" ]; then
    cp "$SCRIPT_DIR/import_wiki.py" "$INSTALL_DIR/import_wiki.py"
else
    echo "Fetching import_wiki.py from GitHub..."
    curl -fsSL "$RAW_URL" -o "$INSTALL_DIR/import_wiki.py"
fi

python3 - "$INSTALL_DIR/import_wiki.py" "$LIBRARY" "$PORT" "$LIBRARY_URL" <<'PYEOF'
import re, sys
path, library, port, library_url = sys.argv[1:5]
text = open(path, encoding="utf-8").read()
text = re.sub(r'^LIBRARY = .*$', f'LIBRARY = {library!r}', text, count=1, flags=re.M)
text = re.sub(r'^PORT = .*$', f'PORT = {int(port)}', text, count=1, flags=re.M)
text = re.sub(r'^LIBRARY_URL = .*$', f'LIBRARY_URL = {library_url!r}', text, count=1, flags=re.M)
open(path, "w", encoding="utf-8").write(text)
PYEOF

echo
echo "Installed to $INSTALL_DIR/import_wiki.py"
echo "  LIBRARY      = $LIBRARY"
echo "  PORT         = $PORT"
echo "  LIBRARY_URL  = $LIBRARY_URL"
echo

if confirm "Set up a systemd service so this runs automatically?"; then
    if [ "$(id -u)" -ne 0 ]; then
        echo "Installing a systemd unit needs root. Skipping -- run it manually with:"
        echo "  python3 $INSTALL_DIR/import_wiki.py"
    else
        cat > /etc/systemd/system/wiki-to-calibre.service <<EOF
[Unit]
Description=Wikipedia-to-Calibre Import Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/import_wiki.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable -q --now wiki-to-calibre
        echo "Service started. Check it with: systemctl status wiki-to-calibre"
    fi
else
    echo "Skipping systemd setup -- run it manually with:"
    echo "  python3 $INSTALL_DIR/import_wiki.py"
fi

echo
echo "Done! Open http://localhost:$PORT to use it."
