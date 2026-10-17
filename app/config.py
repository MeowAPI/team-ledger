import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def load_env():
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"').strip("'"))


load_env()
DATA = Path(os.getenv('DATA_DIR', str(ROOT / 'data'))).resolve()
DB_PATH = DATA / 'ledger.sqlite3'
TIMEZONE = os.getenv('TEAM_TIMEZONE', 'Asia/Shanghai')


def today():
    return datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()


def now():
    return datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec='seconds')
