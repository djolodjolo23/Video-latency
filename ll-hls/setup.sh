#!/usr/bin/env bash
# Bootstrap script for the LL-HLS webcam streamer.
# Installs the necessary GStreamer packages (via apt), provisions a Python
# virtual environment under ll-hls/.venv, and installs Python requirements.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_PATH="${VENV_PATH:-"$SCRIPT_DIR/.venv"}"
REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"

check_command() {
    command -v "$1" >/dev/null 2>&1
}

main() {
    if ! check_command apt-get; then
        echo "This helper currently supports apt-based systems (Debian/Ubuntu)." >&2
        exit 1
    fi

    echo "Updating apt indices..."
    sudo apt-get update

    echo "Installing GStreamer + PyGObject prerequisites..."
    sudo apt-get install -y \
        python3-gi python3-gi-cairo python3-cairo python3-venv python3-pip \
        gir1.2-gstreamer-1.0 libgirepository1.0-dev \
        gstreamer1.0-tools gstreamer1.0-libav \
        gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
        v4l-utils

    if [ ! -d "$VENV_PATH" ]; then
        echo "Creating Python virtual environment at $VENV_PATH (with system packages)"
        python3 -m venv --system-site-packages "$VENV_PATH"
    fi

    # shellcheck disable=SC1090
    source "$VENV_PATH/bin/activate"
    python -m pip install --upgrade pip

    if [ -f "$REQUIREMENTS_FILE" ]; then
        pip install -r "$REQUIREMENTS_FILE"
    fi

    echo
    echo "Environment ready."
    echo "Activate it with: source \"$VENV_PATH/bin/activate\""
    echo "Then run: python streamer.py --device /dev/video0 --http-port 8080"
}

main "$@"
