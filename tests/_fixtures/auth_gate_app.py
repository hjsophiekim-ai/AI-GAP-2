import sys
from pathlib import Path
_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st
from app.ui.auth_gate import require_login

require_login()
st.write("PROTECTED_CONTENT")
