#!/usr/bin/env python3
"""Pick local and SSH Git projects and worktrees. Python standard library only."""
import argparse
import base64
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

VERSION = '0.2.0'
SKIP = {'.git', 'node_modules', '.venv', 'venv', 'vendor', 'dist', 'build', '__pycache__', '.cache'}


class Error(Exception):
    pass


def run(args, **kwargs):
    try:
        return subprocess.run(args, check=True, **kwargs)
    except FileNotFoundError as exc:
        raise Error(f'Command not found: {args[0]}') from exc
    except subprocess.TimeoutExpired as exc:
        raise Error(f'Timed out: {args[0]}') from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or '').strip()
        if isinstance(detail, bytes):
            detail = os.fsdecode(detail)
        raise Error(detail or f'{args[0]} exited with status {exc.returncode}') from exc


def capture(args, **kwargs):
    return run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs).stdout


def xdg(kind):
    fallback = {'CONFIG': '.config', 'DATA': '.local/share', 'CACHE': '.cache'}[kind]
    return Path(os.environ.get(f'XDG_{kind}_HOME', Path.home() / fallback)) / 'projects'


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError) as exc:
        raise Error(f'Invalid JSON in {path}: {exc}') from exc


def config():
    path = Path(os.environ.get('PROJECTS_CONFIG', xdg('CONFIG') / 'config.json')).expanduser()
    cfg = read_json(path, {})
    if not isinstance(cfg, dict):
        raise Error('Configuration must be a JSON object')
    cfg.setdefault('host', 'local')
    cfg.setdefault('roots', ['~/Projects', '~/code'])
    cfg.setdefault('remotes', [])
    cfg.setdefault('max_depth', 5)
    cfg.setdefault('cache_seconds', 300)
    cfg.setdefault('scan_workers', 4)
    cfg.setdefault('ssh_timeout', 12)
    cfg.setdefault('backend', 'direct')
    cfg.setdefault('opener', ['nvim', '.'])
    if not isinstance(cfg['host'], str) or not cfg['host']:
        raise Error('host must be a nonempty stable machine name')
    if not isinstance(cfg['roots'], list) or not all(isinstance(x, str) for x in cfg['roots']):
        raise Error('roots must be an array of paths')
    if not isinstance(cfg['opener'], list) or not cfg['opener'] or not all(isinstance(x, str) for x in cfg['opener']):
        raise Error('opener must be a nonempty argv array (no shell interpolation)')
    if cfg['backend'] not in ('direct', 'herdr'):
        raise Error('backend must be direct or herdr')
    for key in ('max_depth', 'cache_seconds', 'ssh_timeout'):
        if not isinstance(cfg[key], int) or cfg[key] < (1 if key == 'ssh_timeout' else 0):
            raise Error(f'{key} must be a valid nonnegative integer')
    if type(cfg['scan_workers']) is not int or not 1 <= cfg['scan_workers'] <= 16:
        raise Error('scan_workers must be an integer between 1 and 16')
    if not isinstance(cfg['remotes'], list):
        raise Error('remotes must be an array')
    names = {cfg['host']}
    for remote in cfg['remotes']:
        if not isinstance(remote, dict) or not all(isinstance(remote.get(k), str) and remote[k] for k in ('name', 'ssh')):
            raise Error('Each remote needs name and ssh strings')
        if remote['name'] in names:
            raise Error('Machine names must be unique')
        names.add(remote['name'])
        if remote['ssh'].startswith('-') or any(c.isspace() for c in remote['ssh']):
            raise Error('ssh must be an SSH alias or user@host, not command-line options')
        if not isinstance(remote.get('command', '~/.local/bin/p'), str):
            raise Error('remote command must be a path')
        if remote.get('backend', 'herdr') not in ('direct', 'herdr'):
            raise Error('remote backend must be direct or herdr')
    return cfg


def worktrees(repo):
    raw = capture(['git', '-C', str(repo), 'worktree', 'list', '--porcelain', '-z'], timeout=5)
    records = []
    record = {}
    for field in raw.split(b'\0'):
        if not field:
            if record.get('path'):
                records.append(record)
            record = {}
        elif field.startswith(b'worktree '):
            record['path'] = os.fsdecode(field[9:])
        elif field.startswith(b'branch '):
            record['branch'] = os.fsdecode(field[7:]).removeprefix('refs/heads/')
        elif field == b'detached':
            record['branch'] = 'detached'
        elif field == b'bare':
            record['bare'] = True
    if record.get('path'):
        records.append(record)
    return records


def scan_root(root, max_depth):
    root = Path(root).expanduser().resolve()
    candidates = []
    if not root.is_dir():
        return candidates
    for directory, dirs, files in os.walk(root):
        current = Path(directory)
        depth = len(current.relative_to(root).parts)
        dirs[:] = sorted(d for d in dirs if d not in SKIP and
                         (not d.startswith('.') or d == '.worktrees')) if depth < max_depth else []
        if (current / '.git').exists() or (current / 'HEAD').is_file():
            candidates.append(str(current.resolve()))
    return candidates


def resolve_repo(directory):
    try:
        common = capture(['git', '-C', directory, 'rev-parse', '--path-format=absolute',
                          '--git-common-dir'], timeout=5).removesuffix(b'\n')
        return os.fsdecode(common), directory
    except Error:
        return None


def repo_entries(item, host):
    common, directory = item
    try:
        trees = worktrees(directory)
    except Error:
        return []
    main = next((r['path'] for r in trees if not r.get('bare')), directory)
    repo_name = Path(common).parent.name if Path(common).name == '.git' else Path(main).name
    entries = []
    for tree in trees:
        if tree.get('bare') or not Path(tree['path']).is_dir():
            continue
        path = str(Path(tree['path']).resolve())
        entries.append(dict(host=host, path=path, repo=repo_name,
                            branch=tree.get('branch', Path(path).name)))
    return entries


def discover(cfg):
    version = capture(['git', '--version'], timeout=5).decode('ascii', errors='replace')
    match = re.search(r'(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (2, 36):
        raise Error('Git 2.36 or newer is required for NUL-delimited worktree output')
    # One bounded pool: independent roots, then Git resolution, then one query
    # per common repository. Never spawn one process per worktree at once.
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg['scan_workers']) as pool:
        candidates = set()
        for batch in pool.map(lambda root: scan_root(root, cfg['max_depth']), cfg['roots']):
            candidates.update(batch)
        repos = {}
        for resolved in pool.map(resolve_repo, sorted(candidates)):
            if resolved:
                common, directory = resolved
                repos.setdefault(common, directory)
        entries = {}
        for batch in pool.map(lambda item: repo_entries(item, cfg['host']), sorted(repos.items())):
            entries.update((entry['path'], entry) for entry in batch)
    return sorted(entries.values(), key=lambda e: (e['repo'], e['path']))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def identity(entry):
    return json.dumps([entry['host'], entry['path']], ensure_ascii=True)


def remember(entry):
    path = xdg('DATA') / 'history.json'
    with lock(path.with_suffix('.lock')):
        history = read_json(path, {})
        history[identity(entry)] = time.time()
        atomic_json(path, history)


def encoded_command(argv):
    # Only a base64 payload crosses the login shell. Paths never become shell code.
    payload = base64.b64encode(json.dumps(argv).encode()).decode()
    code = ("import os,json,base64; a=json.loads(base64.b64decode('" + payload +
            "')); a[0]=os.path.expanduser(a[0]); os.execvp(a[0],a)")
    return 'python3 -c ' + shlex.quote(code)


def remote_args(remote, args, tty=False, timeout=12):
    result = ['ssh', '-o', f'ConnectTimeout={timeout}']
    result += ['-t'] if tty else ['-o', 'BatchMode=yes']
    return result + [remote['ssh'], encoded_command([remote.get('command', '~/.local/bin/p')] + args)]


def remote_discover(remote, cfg):
    data = capture(remote_args(remote, ['--json', '--local'], timeout=cfg['ssh_timeout']),
                   stdin=subprocess.DEVNULL, timeout=cfg['ssh_timeout'] + 20)
    try:
        entries = json.loads(data)
        if not isinstance(entries, list):
            raise ValueError('expected a list')
        for entry in entries:
            if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ('host', 'path', 'repo', 'branch')):
                raise ValueError('invalid project record')
            if entry['host'] != remote['name']:
                raise ValueError(f"remote host must be configured as {remote['name']!r}")
            if not os.path.isabs(entry['path']):
                raise ValueError('remote path must be absolute')
        return entries
    except (ValueError, UnicodeError) as exc:
        raise Error(f"Invalid catalog from {remote['name']}: {exc}") from exc


def cache_path(cfg):
    # UI, opener, Herdr session, worker count and TTL cannot change the catalog.
    discovery = dict(schema=2, host=cfg['host'], max_depth=cfg['max_depth'],
                     roots=sorted({str(Path(r).expanduser().resolve()) for r in cfg['roots']}),
                     remotes=sorted((dict(name=r['name'], ssh=r['ssh'],
                                          command=r.get('command', '~/.local/bin/p'))
                                     for r in cfg['remotes']), key=lambda r: r['name']))
    key = hashlib.sha256(json.dumps(discovery, sort_keys=True).encode()).hexdigest()[:16]
    return xdg('CACHE') / (key + '.json')


def refresh_catalog(cfg, path, cache):
    entries = []
    # Local discovery starts alongside SSH, not before it. Its own Git pool is bounded.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, 1 + len(cfg['remotes']))) as pool:
        local = pool.submit(discover, cfg)
        pending = {pool.submit(remote_discover, remote, cfg): remote for remote in cfg['remotes']}
        pending[local] = None
        for future in concurrent.futures.as_completed(pending):
            remote = pending[future]
            try:
                entries.extend(future.result())
            except Error as exc:
                if remote is None:
                    raise  # Keep the last complete cache when local discovery fails.
                print(f"projects: {remote['name']}: {exc}; keeping cached entries", file=sys.stderr)
                entries.extend(dict(e, offline=True) for e in cache.get('entries', []) if e['host'] == remote['name'])
    entries = sorted({identity(e): e for e in entries}.values(),
                     key=lambda e: (e['host'], e['repo'], e['path']))
    atomic_json(path, dict(updated=time.time(), entries=entries))
    return entries


def start_refresh(cfg, path):
    # Pass the locked descriptor to the detached worker. The parent's close does
    # not release flock while the child still holds it, so concurrent pickers
    # cannot start duplicate refreshes, even before the worker starts executing.
    try:
        with path.with_suffix('.lock').open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            latest = read_json(path, {})
            if latest and time.time() - latest.get('updated', 0) < cfg['cache_seconds']:
                return  # Another worker finished after this caller read the cache.
            with path.with_suffix('.log').open('w') as log:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                  '--refresh-worker', path.stem,
                                  '--refresh-lock-fd', str(stream.fileno())],
                                 pass_fds=(stream.fileno(),), stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, start_new_session=True)
    except OSError as exc:
        print(f'projects: background refresh unavailable: {exc}', file=sys.stderr)


def catalog(cfg, refresh=False):
    path = cache_path(cfg)
    cache = read_json(path, {})
    if not refresh and cache:
        if time.time() - cache.get('updated', 0) >= cfg['cache_seconds']:
            start_refresh(cfg, path)
        return cache['entries']
    with lock(path.with_suffix('.lock')):
        cache = read_json(path, {})
        # Another foreground/first-run caller may have filled it while we waited.
        if not refresh and cache:
            return cache['entries']
        return refresh_catalog(cfg, path, cache)


def display(text):
    return ''.join(c if c.isprintable() else '?' for c in text)


def label(entry):
    return display(f"{entry['host']} · {entry['repo']} › {entry['branch']}  {entry['path']}" +
                   (' [offline cache]' if entry.get('offline') else ''))


def pick(entries, query):
    history = read_json(xdg('DATA') / 'history.json', {})
    entries = sorted(entries, key=lambda e: (-history.get(identity(e), 0), e['host'], e['repo'], e['path']))
    if not entries:
        raise Error('No projects found. Configure roots in ~/.config/projects/config.json')
    records = ''.join(f'{i}\t{label(e)}\0' for i, e in enumerate(entries)).encode()
    env = dict(os.environ, FZF_DEFAULT_OPTS='', FZF_DEFAULT_OPTS_FILE=os.devnull)
    try:
        result = subprocess.run(['fzf', '--read0', '--print0', '--delimiter=\t', '--with-nth=2..',
                                 '--no-sort', '--height=80%', '--layout=reverse', '--prompt=projects> ',
                                 '--query', query], input=records, stdout=subprocess.PIPE, env=env)
    except FileNotFoundError as exc:
        raise Error('fzf is required') from exc
    if result.returncode in (1, 130):
        return None
    if result.returncode:
        raise Error(f'fzf exited with status {result.returncode}')
    try:
        return entries[int(result.stdout.split(b'\t', 1)[0])]
    except (ValueError, IndexError) as exc:
        raise Error('Invalid picker result') from exc


def opener(cfg, path):
    # {path} is replaced within argv elements, never evaluated by a shell here.
    return [arg.replace('{path}', path) for arg in cfg['opener']]


def herdr_prepare(cfg, path):
    try:
        path.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise Error('Herdr requires a UTF-8 path; use the direct backend for this directory') from exc
    inside = os.environ.get('HERDR_ENV') == '1'
    prefix = ['herdr'] if inside else ['herdr', '--session', cfg.get('herdr_session', 'projects')]
    def api(*args):
        raw = capture(prefix + list(args), timeout=10)
        if not raw.strip() and args[:2] == ('pane', 'run'):
            return {}  # pane run succeeds without a JSON response in Herdr 0.8.2.
        try:
            return json.loads(raw)['result']
        except (ValueError, KeyError) as exc:
            raise Error('Unexpected Herdr response') from exc
    with lock(xdg('DATA') / 'herdr.lock'):
        try:
            state = api('workspace', 'list')
        except Error:
            if inside:
                raise
            subprocess.Popen(prefix + ['server'], cwd=path, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + 8
            while True:
                try:
                    state = api('workspace', 'list')
                    break
                except Error:
                    if time.monotonic() >= deadline:
                        raise Error('Herdr server did not become ready')
                    time.sleep(.1)
        # Only workspaces created by projects are reused. Never type into an existing pane.
        workspace_label = Path(path).name + ' [p:' + hashlib.sha256(os.fsencode(path)).hexdigest()[:12] + ']'
        for ws in state['workspaces']:
            if ws['label'] == workspace_label:
                api('workspace', 'focus', ws['workspace_id'])
                return
        created = api('workspace', 'create', '--cwd', path, '--label', workspace_label + ' (starting)', '--focus')
        pane = created['root_pane']['pane_id']
        api('pane', 'run', pane, encoded_command(opener(cfg, path)))
        # Only successfully submitted startup commands make a workspace reusable.
        api('workspace', 'rename', created['workspace']['workspace_id'], workspace_label)


def open_local(cfg, path, backend=None, prepare=False):
    path = str(Path(path).expanduser().resolve())
    if not Path(path).is_dir():
        raise Error(f'Directory no longer exists: {path}. Run p --refresh')
    backend = backend or cfg['backend']
    if backend == 'herdr':
        herdr_prepare(cfg, path)
        remember(dict(host=cfg['host'], path=path))
        if not prepare and os.environ.get('HERDR_ENV') != '1':
            return subprocess.call(['herdr', '--session', cfg.get('herdr_session', 'projects')], cwd=path)
        return 0
    result = run(opener(cfg, path), cwd=path)
    remember(dict(host=cfg['host'], path=path))
    return result.returncode


def open_entry(cfg, entry):
    if entry['host'] == cfg['host']:
        # Within Herdr, focus/create a workspace instead of nesting a client.
        backend = 'herdr' if os.environ.get('HERDR_ENV') == '1' else cfg['backend']
        return open_local(cfg, entry['path'], backend)
    remote = next(r for r in cfg['remotes'] if r['name'] == entry['host'])
    backend = remote.get('backend', 'herdr')
    if backend == 'herdr' and os.environ.get('HERDR_ENV') == '1':
        print('projects: inside Herdr; opening remote editor through SSH to avoid a nested client', file=sys.stderr)
        backend = 'direct'
    args = ['--open-local', entry['path'], '--backend', backend]
    if backend == 'herdr':
        args += ['--prepare', '--herdr-session', remote.get('herdr_session', 'projects')]
        run(remote_args(remote, args, timeout=cfg['ssh_timeout']), timeout=cfg['ssh_timeout'] + 30)
        remember(entry)
        return subprocess.call(['herdr', '--remote', remote['ssh'], '--session', remote.get('herdr_session', 'projects')])
    run(remote_args(remote, args, tty=True, timeout=cfg['ssh_timeout']))
    remember(entry)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('query', nargs='*', help='initial fuzzy query')
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('--refresh', action='store_true', help='refresh the catalog and exit')
    parser.add_argument('--list', action='store_true', help='print the catalog without opening')
    parser.add_argument('--json', action='store_true', help='print machine-readable catalog')
    parser.add_argument('--local', action='store_true', help='discover this machine only; do not write cache')
    parser.add_argument('--open-local', metavar='PATH', help=argparse.SUPPRESS)
    parser.add_argument('--backend', choices=['direct', 'herdr'], help=argparse.SUPPRESS)
    parser.add_argument('--prepare', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--herdr-session', help=argparse.SUPPRESS)
    parser.add_argument('--refresh-worker', help=argparse.SUPPRESS)
    parser.add_argument('--refresh-lock-fd', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        cfg = config()
        if args.refresh_worker:
            if args.refresh_lock_fd is None:
                raise Error('Background refresh requires an inherited lock')
            with os.fdopen(args.refresh_lock_fd, 'a') as stream:
                path = cache_path(cfg)
                if path.stem != args.refresh_worker:
                    return 0  # Configuration changed between scheduling and startup.
                # Verify the inherited descriptor names this cache's lock file.
                actual = os.fstat(stream.fileno())
                expected = path.with_suffix('.lock').stat()
                if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                    raise Error('Invalid refresh lock')
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                refresh_catalog(cfg, path, read_json(path, {}))
            return 0
        if args.herdr_session:
            cfg['herdr_session'] = args.herdr_session
        if args.open_local:
            return open_local(cfg, args.open_local, args.backend, args.prepare)
        entries = discover(cfg) if args.local else catalog(cfg, args.refresh)
        if args.json:
            print(json.dumps(entries, ensure_ascii=True))
        elif args.list:
            for entry in entries:
                print(label(entry))
        elif args.refresh:
            print(f'projects: indexed {len(entries)} contexts')
        else:
            entry = pick(entries, ' '.join(args.query))
            if entry:
                return open_entry(cfg, entry)
        return 0
    except (Error, OSError, ValueError) as exc:
        print(f'projects: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
