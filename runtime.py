"""One bounded worker inside the single Gunicorn process; SQLite leases persist."""

from pathlib import Path
import threading

import media
import newsletter
import operations


def run_once(app):
    with app.app_context():
        if not app.config['PUBLIC_ENABLED'] or (Path(app.config['DATA_DIR']) / 'restore-review-required.json').exists() or operations.production_samples_present():
            return
        newsletter.process_outbox(limit=10)
        media.cleanup_media()


def start_workers(app):
    if app.testing or app.extensions.get('blog_worker'):
        return
    stop = threading.Event()

    def work():
        while not stop.is_set():
            try:
                run_once(app)
            except Exception:
                # Exception values may contain recipient addresses or secrets.
                app.logger.error('Background maintenance failed; durable jobs remain queued.')
            stop.wait(20)

    worker = threading.Thread(target=work, name='blog-maintenance', daemon=True)
    app.extensions['blog_worker'] = (worker, stop)
    worker.start()
