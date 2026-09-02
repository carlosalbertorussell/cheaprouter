"""Run the full test suite. Equivalent to `python -m pytest tests/`."""
import subprocess, sys
sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-v", "tests/"]))
