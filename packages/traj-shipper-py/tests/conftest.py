import sys
from pathlib import Path

# Make `import traj_shipper` work without installing the package (CI runs from the repo root).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
