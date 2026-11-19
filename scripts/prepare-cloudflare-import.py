"""Prepare a verified production copy; never remove local writing or samples."""
import argparse
from pathlib import Path
import shutil
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import create_app
import operations
import posts
from sample_posts import SAMPLE_POSTS


def prepare(source, output):
    staging = ROOT / '.qa' / ('cloudflare-import-' + uuid4().hex)
    staging.mkdir(parents=True)
    try:
        app = create_app({'DATA_DIR': staging, 'PRODUCTION': True, 'PUBLIC_ENABLED': False,
                          'EMAIL_MODE': 'disabled'})
        with app.app_context():
            operations.restore_data(source)
            known_samples = {post['slug'] for post in SAMPLE_POSTS}
            removed = 0
            for post in posts.editor_list_posts():
                if post['slug'] in known_samples and post['title'].startswith(('Sample article:', 'Sample draft:')):
                    working = posts.get_edit(post['id'])
                    posts.permanently_delete_post(post['id'], 'DELETE', working['version'])
                    removed += 1
            operations.review_restored_data()
            result = operations.backup_data(output)
            print({'verified': True, 'posts': result['posts'], 'images': result['images'],
                   'samples_excluded_from_copy': removed, 'output': str(Path(output).resolve())})
    finally:
        assert (ROOT / '.qa').resolve() in staging.resolve().parents
        shutil.rmtree(staging)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    prepare(args.source, args.output)
