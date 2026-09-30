import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import projects as p


class ProjectsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env = patch.dict(os.environ, {**clean_env,
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': os.devnull,
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_DATA_HOME': str(self.root / 'data'),
            'XDG_CACHE_HOME': str(self.root / 'cache'),
            'PROJECTS_CONFIG': str(self.root / 'config/projects/config.json'),
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = p.config()
        self.cfg.update(host='test-machine', roots=[str(self.root / 'repos')])

    def git(self, *args):
        return subprocess.run(['git', *map(str, args)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def repo(self):
        repo = self.root / 'repos' / 'project with spaces'
        repo.mkdir(parents=True)
        self.git('init', '-b', 'main', repo)
        self.git('-C', repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'initial')
        return repo

    def test_discovery_external_worktree_and_detached(self):
        repo = self.repo()
        other = self.root / "external ' $(bad) worktree"
        self.git('-C', repo, 'worktree', 'add', '-b', 'feature', other)
        detached = self.root / 'detached'
        self.git('-C', repo, 'worktree', 'add', '--detach', detached)
        self.cfg['roots'].append(str(other))
        entries = p.discover(self.cfg)
        self.assertEqual(len(entries), 3)
        self.assertEqual({e['branch'] for e in entries}, {'main', 'feature', 'detached'})
        self.assertEqual({e['repo'] for e in entries}, {repo.name})

    def test_discovery_skips_dependencies(self):
        self.repo()
        dep = self.root / 'repos/node_modules/dependency'
        dep.mkdir(parents=True)
        self.git('init', dep)
        self.assertEqual(len(p.discover(self.cfg)), 1)

    def test_remote_shell_quoting(self):
        # Execute the exact login-shell command with hostile arguments on each shell.
        value = "space ' \" ; $(touch nope) `touch nope`\nnew line"
        command = p.encoded_command([sys.executable, '-c', 'import sys,json; print(json.dumps(sys.argv[1:]))', value])
        for shell in ('bash', 'zsh', 'fish'):
            if not shutil.which(shell):
                continue
            flags = ['--no-config', '-c'] if shell == 'fish' else ['-f', '-c'] if shell == 'zsh' else ['--noprofile', '--norc', '-c']
            result = subprocess.run([shell, *flags, command], cwd=self.root, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), [value])
        self.assertFalse((self.root / 'nope').exists())

    def test_invalid_remote_configuration(self):
        target = p.xdg('CONFIG') / 'config.json'
        p.atomic_json(target, {'remotes': [{'name': 'other', 'ssh': '-oProxyCommand=bad'}]})
        with self.assertRaises(p.Error):
            p.config()

    def test_remote_machine_identity_must_match(self):
        data = json.dumps([dict(host='wrong', path='/repo', repo='repo', branch='main')]).encode()
        with patch.object(p, 'capture', return_value=data):
            with self.assertRaises(p.Error):
                p.remote_discover(dict(name='correct', ssh='host'), self.cfg)

    def test_offline_refresh_keeps_remote_cache(self):
        self.cfg['remotes'] = [dict(name='remote', ssh='remote')]
        entry = dict(host='remote', path='/repo', repo='repo', branch='main')
        with patch.object(p, 'remote_discover', return_value=[entry]):
            self.assertEqual(p.catalog(self.cfg, True), [entry])
        with patch.object(p, 'remote_discover', side_effect=p.Error('offline')), contextlib.redirect_stderr(io.StringIO()):
            refreshed = p.catalog(self.cfg, True)
        self.assertTrue(refreshed[0]['offline'])
        self.assertEqual(refreshed[0]['path'], '/repo')

    def test_cancel_does_not_open_or_record(self):
        entry = dict(host='test-machine', path='/repo', repo='repo', branch='main')
        with patch.object(p, 'config', return_value=self.cfg), patch.object(p, 'catalog', return_value=[entry]), \
             patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([], 130, b'')), \
             patch.object(p, 'open_entry') as opened:
            self.assertEqual(p.main([]), 0)
            opened.assert_not_called()
        self.assertFalse((p.xdg('DATA') / 'history.json').exists())

    def test_picker_recent_first_and_original_path(self):
        one = dict(host='test-machine', path='/repo\nwith newline', repo='one', branch='main')
        two = dict(host='test-machine', path='/second', repo='two', branch='main')
        p.remember(two)
        with patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'1\tlabel\0')) as run:
            selected = p.pick([one, two], 'query')
        self.assertEqual(selected, one)
        self.assertIn(b'0\ttest-machine', run.call_args.kwargs['input'])
        self.assertNotIn(b'\n', run.call_args.kwargs['input'])

    def test_failed_editor_not_added_to_history(self):
        with patch.object(p, 'run', side_effect=p.Error('failed')):
            with self.assertRaises(p.Error):
                p.open_local(self.cfg, str(self.root))
        self.assertFalse((p.xdg('DATA') / 'history.json').exists())

    def test_existing_herdr_workspace_only_focused(self):
        path = str(self.root)
        label = self.root.name + ' [p:' + p.hashlib.sha256(path.encode()).hexdigest()[:12] + ']'
        responses = [json.dumps({'result': {'workspaces': [{'label': label, 'workspace_id': 'w8'}]}}).encode(),
                     b'{"result": {}}']
        with patch.dict(os.environ, {'HERDR_ENV': '1'}), patch.object(p, 'capture', side_effect=responses) as capture:
            p.herdr_prepare(self.cfg, path)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(capture.call_args.args[0], ['herdr', 'workspace', 'focus', 'w8'])

    def test_new_herdr_workspace_uses_returned_pane(self):
        responses = [b'{"result":{"workspaces":[]}}',
                     b'{"result":{"root_pane":{"pane_id":"w12:p7"},"workspace":{"workspace_id":"w12"}}}', b'', b'{"result":{}}']
        with patch.dict(os.environ, {'HERDR_ENV': '1'}), patch.object(p, 'capture', side_effect=responses) as capture:
            p.herdr_prepare(self.cfg, str(self.root))
        self.assertEqual(capture.call_args_list[2].args[0][0:4], ['herdr', 'pane', 'run', 'w12:p7'])
        self.assertEqual(capture.call_args.args[0][0:4], ['herdr', 'workspace', 'rename', 'w12'])

    def test_old_git_reports_requirement(self):
        with patch.object(p, 'capture', return_value=b'git version 2.35.0'):
            with self.assertRaisesRegex(p.Error, '2.36'):
                p.discover(self.cfg)

    def test_failed_herdr_command_does_not_mark_workspace_ready(self):
        responses = [b'{"result":{"workspaces":[]}}',
                     b'{"result":{"root_pane":{"pane_id":"w12:p7"},"workspace":{"workspace_id":"w12"}}}',
                     p.Error('command failed')]
        with patch.dict(os.environ, {'HERDR_ENV': '1'}), patch.object(p, 'capture', side_effect=responses) as capture:
            with self.assertRaises(p.Error):
                p.herdr_prepare(self.cfg, str(self.root))
        self.assertIn('(starting)', capture.call_args_list[1].args[0][-2])
        self.assertEqual(capture.call_count, 3)

    def test_non_utf8_herdr_path_has_actionable_error(self):
        with self.assertRaisesRegex(p.Error, 'direct backend'):
            p.herdr_prepare(self.cfg, '/repo' + chr(0xdcff))

    def test_removed_path_fails_before_editor(self):
        with patch.object(p, 'run') as run:
            with self.assertRaises(p.Error):
                p.open_local(self.cfg, str(self.root / 'removed'))
            run.assert_not_called()

    def test_launcher_works_via_symlink(self):
        launcher = self.root / 'p'
        launcher.symlink_to(Path(p.__file__).parent / 'p')
        result = subprocess.run([str(launcher), '--version'], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), p.VERSION)


if __name__ == '__main__':
    unittest.main()
