#!/usr/bin/env bash
# Installs a cron job that runs the Bid News Agent every 30 minutes.
#
# Usage:
#   bash install_cron.sh
#
# What it does:
#   1. Creates a small wrapper script (run_agent.sh) that activates the
#      venv and runs `python bid_agent.py`.
#   2. Writes a cron entry that runs the wrapper every 30 minutes.
#   3. Adds an entry to your crontab (does NOT overwrite existing jobs).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$SCRIPT_DIR/run_agent.sh"
VENV_DIR="$SCRIPT_DIR/.venv"

# ---- Step 1: create venv if needed ----------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtualenv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
    echo "  -> dependencies installed."
fi

# ---- Step 2: write the wrapper --------------------------------------------
cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
source "$VENV_DIR/bin/activate"
cd "$SCRIPT_DIR"
python bid_agent.py >> "\$SCRIPT_DIR/data/cron.log" 2>&1
EOF
chmod +x "$WRAPPER"
echo "Wrote wrapper: $WRAPPER"

# ---- Step 3: install cron entry -------------------------------------------
CRON_LINE="*/30 * * * * $WRAPPER"
if crontab -l 2>/dev/null | grep -qF "$WRAPPER"; then
    echo "Cron job already installed. Skipping."
else
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "Installed cron job: $CRON_LINE"
fi

echo ""
echo "Done. The agent will run every 30 minutes."
echo "Check logs at: $SCRIPT_DIR/data/agent.log"
echo "Run once manually now?  bash $WRAPPER"
