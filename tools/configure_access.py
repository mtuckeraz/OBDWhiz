"""Create/read the installation's private LAN key; intended for the installer."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.access import load_access_key
from core.storage import Storage
from dotenv_settings import deployment_settings


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    values = deployment_settings(root / '.env')
    data, _ = Storage(root, values).paths()
    print(load_access_key(data / 'access.key'))
