"""
Sets up the test environment once: tests/.venv with what the tests need
(tests/requirements.txt). Safe to run again, e.g. after requirements.txt
changes.

    py -3 tests\\opsaet_testmiljoe.py

Then, from the repo root:

    tests\\.venv\\Scripts\\python.exe -m unittest discover -s tests -t .
"""
import pathlib
import subprocess
import sys
import venv

TESTS = pathlib.Path(__file__).resolve().parent
VENV = TESTS / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

if not PYTHON.exists():
    print(f"Opretter {VENV} …")
    venv.create(VENV, with_pip=True)

subprocess.run(
    [str(PYTHON), "-m", "pip", "install", "--disable-pip-version-check", "-q",
     "-r", str(TESTS / "requirements.txt")],
    check=True,
)
print(f"Testmiljøet er klar: {PYTHON}")
