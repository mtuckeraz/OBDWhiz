"""Create/read the installation's private LAN key; intended for the installer."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.access import load_access_key
from dotenv_settings import deployment_settings


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    values = deployment_settings(root / '.env')
    data = Path(values.get('OBDWHIZ_DATA_DIR') or root / '.local')
    print(load_access_key(data / 'access.key'))
