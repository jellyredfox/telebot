"""Refuse ephemeral SQLite storage on Railway instead of silently losing results."""
import os
from pathlib import Path


def database_path():
    mount = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', '').strip()
    railway = bool(os.getenv('RAILWAY_ENVIRONMENT_ID') or os.getenv('RAILWAY_PROJECT_ID'))
    if railway and not mount:
        raise SystemExit('Нужен Railway Volume: подключите его к сервису с Mount Path /data и примените Deploy. Без постоянного хранилища бот не запустится, чтобы не потерять результаты.')
    configured = os.getenv('DB_PATH', '').strip()
    path = Path(configured or (str(Path(mount) / 'quiz.sqlite3') if mount else 'data/quiz.sqlite3')).resolve()
    if mount:
        base = Path(mount).resolve()
        if not base.is_dir() or not path.is_relative_to(base) or path == base:
            raise SystemExit('DB_PATH должен указывать на файл внутри подключённого Volume. Например /data/quiz.sqlite3 при Mount Path /data.')
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)
