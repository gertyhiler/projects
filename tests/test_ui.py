import contextlib
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import projects as p
import projects_ui as ui


def has_fzf():
    try:
        ui.ensure_fzf()
        return True
    except ValueError:
        return False


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.entries = [dict(host='local', repo='app', branch='main', path='/a/app'),
                        dict(host='remote', repo='api', branch='feature', path='/b/api')]

    def test_toggles_are_independent(self):
        opts = ui.settings(dict(style='plain', navigation='vim', icons=True, show_path=False, show_help=False))
        rows = ui.rows(self.entries, opts, 'local')
        self.assertNotIn('\x1b', rows[0])
        self.assertIn('\uf109', rows[0])
        self.assertNotIn('/a/app', rows[0])
        args = ui.fzf_options(opts)
        self.assertIn('--no-color', args)
        self.assertIn('--header=INSERT', args)
        self.assertNotIn('Type to search', ' '.join(args))
        self.assertIn('j:down', args)
        opts.update(style='accented', navigation='plain', icons=False, show_path=True)
        rows = ui.rows(self.entries, opts)
        self.assertIn('\x1b[36m', rows[0])
        self.assertNotIn('\uf109', rows[0])
        self.assertIn('/a/app', rows[0])
        self.assertNotIn('j:down', ui.fzf_options(opts))

    def test_hidden_path_collisions_get_unique_suffixes(self):
        entries = [dict(self.entries[0], path='/a/repo/main'), dict(self.entries[0], path='/b/repo/main')]
        rows = ui.rows(entries, ui.settings({'show_path': False}))
        self.assertNotEqual(rows[0], rows[1])
        self.assertIn('[a/repo/main]', rows[0])
        self.assertIn('[b/repo/main]', rows[1])

    def test_untrusted_text_cannot_inject_terminal_controls(self):
        entries = [dict(self.entries[0], repo='bad\x1b[2J\n\tname')]
        row = ui.rows(entries, ui.settings())[0]
        self.assertNotIn('\x1b', row)
        self.assertNotIn('\n', row)
        self.assertNotIn('\t', row)

    def test_settings_validation(self):
        for value in ({'style': 'vim'}, {'navigation': 'accented'}, {'icons': 'yes'}, {'show_path': 1}, {'typo': True}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ui.settings(value)

    def test_missing_fzf_fails_before_discovery(self):
        with patch.object(p, 'config', return_value={'ui': ui.settings()}), \
             patch.object(ui.shutil, 'which', return_value=None), patch.object(p, 'catalog') as catalog, \
             contextlib.redirect_stderr(__import__('io').StringIO()) as output:
            self.assertEqual(p.main([]), 1)
        catalog.assert_not_called()
        self.assertIn('brew install fzf', output.getvalue())

    def test_noninteractive_does_not_require_fzf(self):
        with patch.object(p, 'config', return_value={}), patch.object(p, 'catalog', return_value=[]), \
             patch.object(p, 'ensure_fzf', side_effect=AssertionError('must not check')), \
             contextlib.redirect_stdout(__import__('io').StringIO()):
            for args in (['--json'], ['--list'], ['--refresh']):
                self.assertEqual(p.main(args), 0)

    def test_old_fzf_rejected(self):
        with patch.object(ui.shutil, 'which', return_value='/fzf'), \
             patch.object(ui.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '0.44.1')):
            with self.assertRaisesRegex(ValueError, 'Unsupported fzf version'):
                ui.ensure_fzf()


class PickerTerminal:
    """A real controlling terminal; never touches the user's editor or SSH."""
    def __init__(self, ui_config, query='alpha', count=30):
        self.tmp = tempfile.TemporaryDirectory()
        self.result = Path(self.tmp.name)/'result.json'
        records = ''.join(f'{i}\talpha{i:02d}\0' for i in range(count)).encode()
        args = ['fzf'] + ui.fzf_options(ui.settings(ui_config)) + ['--query', query, '--print-query', '--sync']
        env = dict(os.environ, TERM='xterm-256color', FZF_DEFAULT_OPTS='', FZF_DEFAULT_OPTS_FILE=os.devnull)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                result = subprocess.run(args, input=records, stdout=subprocess.PIPE, env=env)
                self.result.write_text(json.dumps(dict(code=result.returncode, output=result.stdout.decode())))
                os._exit(0)
            except BaseException:
                os._exit(1)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 140, 0, 0))
        try:
            self.wait_text(b'INSERT' if ui_config.get('navigation') == 'vim' else b'projects>')
        except BaseException:
            self.close()
            raise

    def wait_text(self, marker):
        output = b''
        deadline = time.monotonic() + 8
        while marker not in output and time.monotonic() < deadline:
            if select.select([self.fd], [], [], .1)[0]:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    break
                output += chunk
                if b'\x1b[6n' in chunk:
                    os.write(self.fd, b'\x1b[1;1R')
        if marker not in output:
            raise AssertionError(f'{marker!r} not rendered: {output[-1500:]!r}')

    def send(self, keys):
        os.write(self.fd, keys)
        time.sleep(.25)

    def normal(self):
        self.send(b'\x1b')
        self.wait_text(b'NORMAL')

    def finish(self, keys=b'\r'):
        self.send(keys)
        deadline = time.monotonic()+5
        while not self.result.exists() and time.monotonic()<deadline:
            if select.select([self.fd], [], [], .1)[0]:
                try:
                    os.read(self.fd,65536)
                except OSError:
                    break
        return json.loads(self.result.read_text())

    def close(self):
        # Closing the PTY before waitpid also avoids a Darwin terminal-exit hang.
        waited, _ = os.waitpid(self.pid, os.WNOHANG)
        if not waited:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        os.close(self.fd)
        if not waited:
            os.waitpid(self.pid, 0)
        self.tmp.cleanup()


@unittest.skipUnless(has_fzf(), 'requires fzf >= 0.67.0')
class NavigationTests(unittest.TestCase):
    def terminal(self, **settings):
        terminal = PickerTerminal(settings)
        self.addCleanup(terminal.close)
        return terminal

    def test_modes_preserve_query_and_selection(self):
        terminal = self.terminal(navigation='vim')
        terminal.send(b'\x1b[B')
        terminal.normal()
        terminal.send('zжhl'.encode())  # Unbound letters must not modify the query.
        terminal.send(b'i')
        terminal.wait_text(b'INSERT')
        result = terminal.finish()
        self.assertEqual(result['code'], 0)
        self.assertEqual(result['output'].split('\0')[:2], ['alpha', '1\talpha01'])

    def test_first_last_and_line_movements(self):
        terminal = self.terminal(navigation='vim', show_help=False)
        terminal.normal()
        terminal.send(b'Gkgjjk')
        result = terminal.finish()
        self.assertEqual(result['output'].split('\0')[:2], ['alpha', '1\talpha01'])

    def test_half_page_and_search_resume(self):
        terminal = self.terminal(navigation='vim')
        terminal.normal()
        terminal.send(b'\x04')  # Ctrl-d
        result = terminal.finish()
        selected = int(result['output'].split('\0')[1].split('\t')[0])
        self.assertGreater(selected, 0)
        self.assertLess(selected, 29)

    def test_page_moves_and_accented_selection(self):
        terminal = self.terminal(navigation='vim', style='accented', icons=True)
        terminal.normal()
        terminal.send(b'\x06')  # Ctrl-f
        terminal.send(b'\x02')  # Ctrl-b
        result = terminal.finish()
        self.assertEqual(result['output'].split('\0')[:2], ['alpha', '0\talpha00'])

    def test_slash_returns_to_insert(self):
        terminal = self.terminal(navigation='vim')
        terminal.normal()
        terminal.send(b'/')
        terminal.wait_text(b'INSERT')
        terminal.send(b'29')
        terminal.wait_text(b'1/30')
        self.assertEqual(terminal.finish()['output'].split('\0')[:2], ['alpha29', '29\talpha29'])

    def test_normal_q_and_plain_escape_cancel(self):
        terminal = self.terminal(navigation='vim')
        terminal.normal()
        self.assertEqual(terminal.finish(b'q')['code'], 130)
        plain = self.terminal(navigation='plain')
        self.assertEqual(plain.finish(b'\x1b')['code'], 130)

    def test_plain_letters_are_search_text(self):
        terminal = self.terminal(navigation='plain')
        terminal.send(b'29')
        terminal.wait_text(b'1/30')
        self.assertEqual(terminal.finish()['output'].split('\0')[:2], ['alpha29', '29\talpha29'])


if __name__ == '__main__':
    unittest.main()
