"""Presentation and native fzf key bindings; independent of discovery and opening."""
from collections import defaultdict
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

DEFAULT_UI = dict(style='plain', navigation='plain', icons=False,
                  show_path=True, show_help=True)
MIN_FZF = (0, 67, 0)
NORMAL_KEYS = 'j,k,g,G,ctrl-u,ctrl-d,ctrl-b,ctrl-f,i,/,q'


def settings(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('ui must be a JSON object')
    unknown = set(value) - set(DEFAULT_UI)
    if unknown:
        raise ValueError('Unknown ui settings: ' + ', '.join(sorted(unknown)))
    ui = dict(DEFAULT_UI, **value)
    if ui['style'] not in ('plain', 'accented'):
        raise ValueError('ui.style must be plain or accented')
    if ui['navigation'] not in ('plain', 'vim'):
        raise ValueError('ui.navigation must be plain or vim')
    for key in ('icons', 'show_path', 'show_help'):
        if type(ui[key]) is not bool:
            raise ValueError(f'ui.{key} must be true or false')
    return ui


def ensure_fzf():
    help_text = ('Interactive selection requires fzf >= 0.67.0.\n'
                 'macOS: brew install fzf\n'
                 'Debian/Ubuntu: sudo apt install fzf\n'
                 'If your package is older, see https://github.com/junegunn/fzf#installation\n'
                 'Noninteractive --list, --json and --refresh do not require fzf.')
    executable = shutil.which('fzf')
    if not executable:
        raise ValueError(help_text)
    try:
        version = subprocess.run([executable, '--version'], check=True, capture_output=True,
                                 text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('Could not check fzf version.\n' + help_text) from exc
    match = re.match(r'(\d+)\.(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < MIN_FZF:
        raise ValueError('Unsupported fzf version: ' + version.strip() + '\n' + help_text)


def safe_text(value):
    return ''.join(c if c.isprintable() else '?' for c in value)


def width(value):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in value)


def pad(value, size):
    return value + ' ' * max(0, size - width(value))


def disambiguators(entries):
    groups = defaultdict(list)
    for entry in entries:
        groups[(entry['host'], entry['repo'], entry['branch'])].append(entry['path'])
    suffixes = {}
    for paths in groups.values():
        if len(set(paths)) < 2:
            continue
        for count in range(1, max(len(Path(path).parts) for path in paths) + 1):
            names = {path: '/'.join(Path(path).parts[-count:]) for path in paths}
            if len(set(names.values())) == len(names):
                suffixes.update(names)
                break
    return suffixes


def rows(entries, ui, local_host=None):
    suffixes = disambiguators(entries) if not ui['show_path'] else {}
    hosts = [safe_text(e['host']) for e in entries]
    repos = [safe_text(e['repo']) for e in entries]
    host_width = max(map(width, hosts), default=0)
    repo_width = max(map(width, repos), default=0)
    def color(text, code):
        return f'\x1b[{code}m{text}\x1b[0m' if ui['style'] == 'accented' else text
    result = []
    for entry, host, repo in zip(entries, hosts, repos):
        if ui['icons']:
            host_icon = '\uf109' if entry['host'] == local_host else '\uf233'
            host = host_icon + ' ' + pad(host, host_width)
            repo = '\uf07b ' + pad(repo, repo_width)
            branch = '\ue0a0 ' + safe_text(entry['branch'])
        else:
            host, repo = pad(host, host_width), pad(repo, repo_width)
            branch = safe_text(entry['branch'])
        text = color(host, '36') + '  ' + color(repo, '1') + '  ' + color(branch, '32')
        if entry['path'] in suffixes:
            text += '  ' + color('[' + safe_text(suffixes[entry['path']]) + ']', '2')
        if ui['show_path']:
            text += '  ' + color(safe_text(entry['path']), '2')
        if entry.get('offline'):
            text += '  ' + color('[offline cache]', '33')
        result.append(text)
    return result


def mode_header(mode, ui):
    if not ui['show_help']:
        return mode
    if mode == 'INSERT':
        return 'INSERT | Type to search | Up/Down move | Esc normal | Enter open'
    return 'NORMAL | j/k move | g/G first/last | C-u/d half-page | i or / search | Enter open | q quit'


def fzf_options(ui):
    args = ['--read0', '--print0', '--delimiter=\t', '--with-nth=2..', '--no-sort',
            '--height=80%', '--layout=reverse', '--prompt=projects> ', '--header-first']
    if ui['style'] == 'accented':
        args += ['--ansi', '--color=header:8,prompt:6,pointer:6']
    else:
        args += ['--no-color']
    if ui['navigation'] == 'plain':
        if ui['show_help']:
            args += ['--header=Type to search | Up/Down move | Enter open | Esc quit']
        return args
    insert_header = mode_header('INSERT', ui)
    normal_header = mode_header('NORMAL', ui)
    insert = f'show-input+unbind({NORMAL_KEYS})+change-header({insert_header})'
    normal = f'hide-input+rebind({NORMAL_KEYS})+change-header({normal_header})'
    binds = ['start:unbind(' + NORMAL_KEYS + ')', 'esc:' + normal,
             'i:' + insert, '/:' + insert, 'j:down', 'k:up', 'g:first', 'G:last',
             'ctrl-u:half-page-up', 'ctrl-d:half-page-down',
             'ctrl-b:page-up', 'ctrl-f:page-down', 'q:abort']
    args += ['--header=' + insert_header]
    for binding in binds:
        args += ['--bind', binding]
    return args
