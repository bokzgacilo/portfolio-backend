"""Exercise the deployment shell script with simulated VPS commands; no services change."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy.sh"
MOCK = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
scenario = os.environ.get('SCENARIO', '')
root = os.environ['FIXTURE']
with open(os.environ['COMMAND_LOG'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\n')
if name == 'git':
    if args[:2] == ['rev-parse', '--show-toplevel']: print(root)
    elif args[:2] == ['rev-parse', '--absolute-git-dir']: print(root + '/.git')
    elif args[:2] == ['rev-parse', '--short']: print('abc123')
    elif args and args[0] == 'symbolic-ref': print('main')
    elif args[:2] == ['rev-parse', '--abbrev-ref']: print('origin/main')
    elif args and args[0] == 'diff' and scenario == 'dirty': sys.exit(1)
    elif 'pull' in args and scenario == 'pull-fails': sys.exit(1)
elif name == 'systemctl':
    if args[0] == 'show': print('not-found' if scenario == 'missing-service' else 'loaded')
    elif args[0] == 'restart' and scenario == 'restart-fails': sys.exit(1)
elif name == 'python':
    if '-m' in args and 'install' in args and scenario == 'pip-fails': sys.exit(1)
    if '-c' in args and 'json.load' in args[-1]:
        data = json.load(sys.stdin)
        sys.exit(0 if data.get('ready') and data.get('status') == 'ok' else 1)
elif name == 'curl':
    url = args[-1]
    if scenario == 'local-unhealthy' and '127.0.0.1' in url: print('{"status":"ok","ready":false}')
    elif scenario == 'public-unhealthy' and 'https://' in url: sys.exit(22)
    else: print('{"status":"ok","ready":true}')
elif name == 'flock' and scenario == 'locked': sys.exit(1)
elif name == 'sudo':
    if args != ['-v']: os.execvp(args[0], args)
'''

class DeployTests(unittest.TestCase):
    def run_deploy(self, scenario=''):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / '.git').mkdir()
            (root / 'bin').mkdir()
            (root / '.venv/bin').mkdir(parents=True)
            shutil.copy(SCRIPT, root / 'deploy.sh')
            for command in ['git', 'systemctl', 'curl', 'flock', 'sudo', 'python']:
                target = root / ('./.venv/bin/python' if command == 'python' else f'bin/{command}')
                target.write_text(MOCK)
                target.chmod(0o755)
            log = root / 'commands.log'
            env = {**os.environ, 'PATH': f"{root / 'bin'}:{os.environ['PATH']}",
                   'FIXTURE': str(root), 'COMMAND_LOG': str(log), 'SCENARIO': scenario, 'HEALTH_TIMEOUT': '1'}
            result = subprocess.run(['bash', str(root / 'deploy.sh')], env=env, capture_output=True, text=True, timeout=15)
            import json
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            return result, calls

    def test_success_orders_pull_dependencies_backend_health_tunnel_public_health(self):
        result, calls = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        pull = next(i for i, c in enumerate(calls) if c[0] == 'git' and 'pull' in c)
        install = next(i for i, c in enumerate(calls) if c[0] == 'python' and 'install' in c)
        backend = calls.index(['systemctl', 'restart', 'bok-api.service'])
        tunnel = calls.index(['systemctl', 'restart', 'cloudflared.service'])
        checks = [i for i, c in enumerate(calls) if c[0] == 'curl']
        self.assertLess(pull, install)
        self.assertLess(install, backend)
        self.assertLess(backend, checks[0])
        self.assertLess(checks[0], tunnel)
        self.assertLess(tunnel, checks[1])
        self.assertIn('Deployment complete', result.stdout)

    def test_preflight_pull_and_dependency_failures_never_restart(self):
        for scenario in ['dirty', 'locked', 'missing-service', 'pull-fails', 'pip-fails']:
            with self.subTest(scenario=scenario):
                result, calls = self.run_deploy(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c[0] == 'systemctl' and 'restart' in c for c in calls))
                self.assertNotIn('Deployment complete', result.stdout)

    def test_backend_failure_does_not_restart_tunnel(self):
        for scenario in ['restart-fails', 'local-unhealthy']:
            with self.subTest(scenario=scenario):
                result, calls = self.run_deploy(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(['systemctl', 'restart', 'cloudflared.service'], calls)

    def test_failed_public_health_is_not_reported_as_success(self):
        result, _ = self.run_deploy('public-unhealthy')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Deployment complete', result.stdout)
        self.assertIn('public health check', result.stderr)

if __name__ == '__main__': unittest.main()
