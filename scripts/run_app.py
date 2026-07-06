#!/usr/bin/env python
"""Launch the Streamlit UI for the AI-GAP trading system."""

import subprocess
import sys
from pathlib import Path

# Ensure the project root is on sys.path and the cwd is correct.
PROJECT_ROOT = Path(__file__).parent.parent
import os
os.chdir(PROJECT_ROOT)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AI-GAP Streamlit UI launcher")
    parser.add_argument("--port", type=int, default=8501, help="Streamlit server port (default: 8501)")
    args = parser.parse_args()

    target = str(PROJECT_ROOT / "app" / "ui" / "streamlit_app.py")
    cmd = [
        sys.executable, "-m", "streamlit", "run", target,
        f"--server.port={args.port}",
        "--server.headless=false",
    ]
    print(f"[run_app] Starting Streamlit on port {args.port} ...")
    print(f"[run_app] Command: {' '.join(cmd)}")
    subprocess.run(cmd)
