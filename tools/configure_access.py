"""Create/read the installation's private LAN key; intended for the installer."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.access import load_access_key


if __name__ == '__main__':
    print(load_access_key(Path(__file__).resolve().parents[1] / '.local/access.key'))
