"""Bundle an explicit source allowlist. Never walk data, backups or credentials."""
import base64
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'cloudflare' / 'src'
SOURCES = ('app.py', 'auth.py', 'editor_routes.py', 'media.py', 'newsletter.py',
           'operations.py', 'posts.py', 'rendering.py', 'sample_posts.py', 'subscription_routes.py')


def build():
    TARGET.mkdir(parents=True, exist_ok=True)
    for name in SOURCES:
        shutil.copyfile(ROOT / name, TARGET / name)
    for source, target in (('storage.py', 'cloud_storage.py'), ('worker.py', 'worker.py')):
        shutil.copyfile(ROOT / 'cloudflare' / source, TARGET / target)
    templates = {path.name: path.read_text(encoding='utf-8') for path in sorted((ROOT / 'templates').glob('*.html'))}
    assets = {path.name: base64.b64encode(path.read_bytes()).decode('ascii')
              for path in sorted((ROOT / 'static').iterdir()) if path.is_file() and path.suffix in {'.css', '.js', '.svg', '.ico'}}
    # JSON is data parsed at runtime, never Python evaluated from article input.
    (TARGET / '_bundle.py').write_text('import json\nTEMPLATES = json.loads(' + repr(json.dumps(templates)) + ')\nASSETS = json.loads(' + repr(json.dumps(assets)) + ')\n', encoding='utf-8')
    print(f'Bundled {len(SOURCES) + 2} source modules, {len(templates)} templates and {len(assets)} code assets. No writing data included.')


if __name__ == '__main__':
    build()
