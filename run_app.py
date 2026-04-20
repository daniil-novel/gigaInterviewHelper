from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / '.runtime'
PID_FILE = RUNTIME_DIR / 'server.pid'
LOG_FILE = RUNTIME_DIR / 'server.log'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run gigaInterviewHelper')
    subparsers = parser.add_subparsers(dest='command', required=True)

    start_parser = subparsers.add_parser('start', help='Start the app server')
    start_parser.add_argument('--host', default='127.0.0.1')
    start_parser.add_argument('--port', type=int, default=8000)
    start_parser.add_argument('--reload', action='store_true')
    start_parser.add_argument('-i', '--interactive', action='store_true')

    subparsers.add_parser('stop', help='Stop the background app server')
    subparsers.add_parser('status', help='Show background app server status')
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == 'start':
        return start_server(args)
    if args.command == 'stop':
        return stop_server()
    if args.command == 'status':
        return status_server()
    parser.print_help()
    return 1


def start_server(args: argparse.Namespace) -> int:
    cmd = [
        sys.executable,
        '-m',
        'uvicorn',
        'app.main:app',
        '--host',
        args.host,
        '--port',
        str(args.port),
    ]
    if args.reload:
        cmd.append('--reload')

    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    if args.interactive:
        env['GIH_INTERACTIVE_LOG'] = '1'
        print(f'Running in interactive mode on http://{args.host}:{args.port}', flush=True)
        print('Telegram questions and answers will be mirrored to this terminal', flush=True)
        return subprocess.call(cmd, cwd=BASE_DIR, env=env)

    running_pid = read_pid()
    if running_pid and is_process_running(running_pid):
        print(f'Server is already running with PID {running_pid}', flush=True)
        return 1

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open('a', encoding='utf-8') as log_handle:
        popen_kwargs: dict[str, object] = {
            'cwd': str(BASE_DIR),
            'env': env,
            'stdout': log_handle,
            'stderr': subprocess.STDOUT,
        }
        if os.name == 'nt':
            popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            popen_kwargs['start_new_session'] = True

        process = subprocess.Popen(cmd, **popen_kwargs)

    PID_FILE.write_text(str(process.pid), encoding='utf-8')
    print(f'Server started with PID {process.pid}', flush=True)
    print(f'Open http://{args.host}:{args.port}', flush=True)
    print(f'Logs: {LOG_FILE}', flush=True)
    return 0


def stop_server() -> int:
    pid = read_pid()
    if not pid:
        print('No background server PID file found', flush=True)
        return 1

    if not is_process_running(pid):
        PID_FILE.unlink(missing_ok=True)
        print(f'Process {pid} is not running anymore', flush=True)
        return 0

    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], check=False)
    else:
        os.kill(pid, signal.SIGTERM)

    PID_FILE.unlink(missing_ok=True)
    print(f'Stopped server with PID {pid}', flush=True)
    return 0


def status_server() -> int:
    pid = read_pid()
    if pid and is_process_running(pid):
        print(f'Server is running with PID {pid}', flush=True)
        print(f'Logs: {LOG_FILE}', flush=True)
        return 0
    print('Server is not running', flush=True)
    return 1


def read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    raw_value = PID_FILE.read_text(encoding='utf-8').strip()
    if not raw_value.isdigit():
        return None
    return int(raw_value)


def is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


if __name__ == '__main__':
    raise SystemExit(main())
