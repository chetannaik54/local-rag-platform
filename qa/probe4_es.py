import sys
import tempfile

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from probe3_llm import ask, live, up_ready  # noqa: E402

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
    c = live(t)
    up_ready(c, "holidays.txt", "Company holidays: New Year's Day (1 January), Labour Day (1 May), and Christmas Day (25 December).")
    msg, err, dt = ask(c, "\u00bfCu\u00e1ndo es el D\u00eda del Trabajo en la empresa?", agent="general")
    print("ASCII-ESCAPED ANSWER:", ascii(msg["content"]))
    print("has real accent:", "\u00ed" in msg["content"], "| mojibake marker present:", "\u00c3" in msg["content"])
    c.__exit__(None, None, None)
