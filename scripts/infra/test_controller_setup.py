"""Controller check/install guards are verified without network or installation."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'setup-controller.sh'


@unittest.skipUnless(sys.platform == 'linux' and sys.version_info[:2] == (3, 12),
                     'Controller installer targets Linux CPython 3.12')
class ControllerSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='retail controller test ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'ansible').mkdir()
        (self.root / 'ansible/requirements.txt').write_text('ansible-core==2.21.6\n', encoding='utf-8')
        (self.root / 'ansible/requirements.yml').write_text('collections: []\n', encoding='utf-8')
        self.script = self.root / 'scripts/setup-controller.sh'
        shutil.copyfile(SCRIPT, self.script)
        self.bin = self.root / 'fake-bin'
        self.bin.mkdir()
        self.probe = self.root / 'unexpected-network-or-install'
        self.command('python3.12', 'exec ' + shlex.quote(sys.executable) + ' "$@"')
        self.command('uname', 'if [ "$1" = -s ]; then echo Linux; else echo x86_64; fi')
        for name in ('git', 'curl', 'gpg', 'install', 'mktemp'):
            self.command(name, 'printf "%s\\n" "$0 $*" >> "$CONTROLLER_TEST_PROBE"; exit 91')
        for name in ('aws', 'helm', 'terraform', 'kubectl'):
            self.command(name, 'exit 1')
        self.environment = dict(os.environ, PATH=str(self.bin) + ':/usr/local/bin:/usr/bin:/bin',
                                CONTROLLER_TEST_PROBE=str(self.probe))

    def command(self, name, content):
        path = self.bin / name
        path.write_text('#!/bin/bash\n' + content + '\n', encoding='utf-8')
        path.chmod(0o755)

    def run_setup(self, *arguments):
        return subprocess.run(['/bin/bash', str(self.script), *arguments], cwd=self.root,
                              env=self.environment, text=True, capture_output=True, check=False, timeout=30)

    def test_default_check_creates_no_directories_and_never_downloads(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        result = self.run_setup()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('MISSING/DIFFERENT: aws', result.stdout)
        self.assertIn('No AWS identity/API check was performed', result.stdout)
        self.assertFalse(self.probe.exists())
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')))

    def test_unknown_mode_is_rejected_before_platform_or_network_checks(self):
        result = self.run_setup('--install-everywhere')
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / '.ci-tools').exists())
        self.assertFalse(self.probe.exists())

    def test_help_is_read_only(self):
        result = self.run_setup('--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('--install-tools', result.stdout)
        self.assertFalse((self.root / '.ci-tools').exists())
        self.assertFalse(self.probe.exists())

    def test_non_linux_controller_is_rejected_before_installation(self):
        self.command('uname', 'echo Darwin')
        result = self.run_setup('--install-tools')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / '.ci-tools').exists())
        self.assertFalse(self.probe.exists())

    def test_install_refuses_a_symlinked_controller_destination(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / '.ci-tools').mkdir()
        (self.root / '.ci-tools/controller').symlink_to(outside, target_is_directory=True)
        result = self.run_setup('--install-tools')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.probe.exists())


if __name__ == '__main__':
    unittest.main()
