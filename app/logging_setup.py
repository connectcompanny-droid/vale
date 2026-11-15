import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import get_settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record), "level": record.levelname, "logger": record.name,
               "msg": record.getMessage(), "instance": get_settings().instance_id}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging() -> None:
    fmt = JsonFormatter() if get_settings().log_json else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file = os.environ.get("LOG_FILE", "")
    if log_file:  # local mode: each process also writes to logs/<role>.log (rotated, a few MB)
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore"):  # request lines would put phone numbers and tokens in the log
        logging.getLogger(noisy).setLevel(logging.WARNING)
