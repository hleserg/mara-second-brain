"""Импорт session-note.py: дефис в имени модуля python не пускает."""
import importlib.util, os
_p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session-note.py")
_s = importlib.util.spec_from_file_location("session_note", _p)
session_note = importlib.util.module_from_spec(_s); _s.loader.exec_module(session_note)
messages = session_note.messages
parse = session_note.parse
