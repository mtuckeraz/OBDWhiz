"""Read the project deployment environment for command-line tools."""
import os


def deployment_settings(path):
    values = {}
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return {**values, **os.environ}
