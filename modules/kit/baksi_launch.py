"""Запуск модуля платформой: python -m baksi_launch <пакет>.

Ставит ограничения процессу (приоритет ниже хаба, без core-дампов) и запускает пакет модуля как `python -m`.
"""
import os
import resource
import runpy
import sys


def _limits() -> None:
    try:
        os.nice(5)
    except OSError:
        pass
    for res, value in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_NOFILE, 4096)):
        try:
            _soft, hard = resource.getrlimit(res)
            resource.setrlimit(res, (value if hard == resource.RLIM_INFINITY else min(value, hard), hard))
        except (ValueError, OSError):
            pass


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m baksi_launch <package>")
    entry = sys.argv[1]
    _limits()
    sys.argv = [entry, *sys.argv[2:]]
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    runpy.run_module(entry, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
