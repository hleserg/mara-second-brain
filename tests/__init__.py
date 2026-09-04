"""Песочница для временных каталогов на время прогона тестов.

`scripts/run-tests.sh` ставит `TMPDIR` сам, но `python3 -m unittest
tests.test_contextd` руками мимо гейта — тоже прогон, и мусор от него такой
же (#33). Этот файл выполняется при импорте пакета, то есть ровно в этом
случае: `unittest discover -s tests` грузит модули как `test_contextd`, пакет
не трогая, и там уже отработал скрипт.

Третий вход — `python3 tests/test_contextd.py` — не накрыт ничем: модуль
грузится как `__main__`, пакет не импортируется. Закрыть его отсюда нельзя,
и он остаётся мусорящим (см. `docs/decisions.md`).

Ставим и `tempfile.tempdir`, и переменную окружения: свой процесс смотрит в
первую, а скрипты, которые тесты запускают подпроцессами, — во вторую.
"""
import atexit
import os
import shutil
import tempfile

_песок = tempfile.mkdtemp(prefix="mara-tests.")
tempfile.tempdir = _песок
os.environ["TMPDIR"] = _песок
if not os.environ.get("MARA_KEEP_TMP"):
    atexit.register(shutil.rmtree, _песок, ignore_errors=True)
