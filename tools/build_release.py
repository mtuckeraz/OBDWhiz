"""Build a Pi source release from an allowlist; exclude keys and vehicle data."""
import hashlib
import io
from pathlib import Path
import re
import tarfile


ROOT = Path(__file__).resolve().parents[1]
FILES = ('VERSION', 'README.md', 'requirements.txt', '.env.example',
         'IMPLEMENTATION_PLAN.md', 'FEATURE_MATRIX.md', 'DATABASE_SCHEMA.md', 'HARDWARE_COMPATIBILITY.md',
         'BLE_PROTOCOL.md', 'API.md', 'DEPLOYMENT.md', 'TESTING.md', 'CHANGELOG.md',
         'server.py', 'install-pi.sh', 'Start-OBDWhiz.sh', 'Start-ProActive.cmd', 'Start-ProActive.ps1')
DIRECTORIES = {'core': {'.py'}, 'web': {'.html', '.css', '.js', '.svg'},
               'tools': {'.py'}, 'tests': {'.py'}, 'data': {'.csv'}}


def release_files(root):
    files = [root / name for name in FILES]
    for directory, suffixes in DIRECTORIES.items():
        files.extend(p for p in sorted((root / directory).rglob('*'))
                     if p.is_file() and p.suffix in suffixes and '__pycache__' not in p.parts)
    for path in files:
        if path.is_symlink() or root.resolve() not in path.resolve().parents:
            raise ValueError(f'Refusing release file outside the source tree: {path}')
    return files


def build(root=ROOT):
    version = (root / 'VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('VERSION must use major.minor.patch.')
    output = root / 'output'
    output.mkdir(exist_ok=True)
    archive = output / f'obdwhiz-pi-{version}.tar.gz'
    with tarfile.open(archive, 'w:gz', format=tarfile.PAX_FORMAT) as bundle:
        for source in release_files(root):
            name = source.relative_to(root).as_posix()
            data = source.read_bytes()
            # Windows checkouts must still ship executable, LF-only Pi scripts.
            if source.suffix == '.sh':
                data = data.replace(b'\r\n', b'\n')
            info = tarfile.TarInfo(f'obdwhiz/{name}')
            info.size = len(data)
            info.mode = 0o755 if source.suffix == '.sh' else 0o644
            bundle.addfile(info, io.BytesIO(data))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + '.sha256').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
    return archive


if __name__ == '__main__':
    print(build())
