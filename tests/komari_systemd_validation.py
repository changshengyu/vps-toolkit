"""Validate generated units with the real systemd parser; never start a service."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('komari_manage', ROOT / 'lib/komari/manage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def main():
    print(subprocess.check_output(['systemd-analyze', '--version'], text=True).splitlines()[0], flush=True)
    with tempfile.TemporaryDirectory(prefix='komari-systemd-') as folder:
        # Spaces and systemd specifiers must work without shell-quoting this scalar.
        for suffix in ('plain', 'with spaces and % sign'):
            paths = m.Paths(Path(folder) / suffix)
            paths.bin.mkdir(parents=True)
            paths.units.mkdir(parents=True)
            for role in ('server', 'agent', 'tunnel'):
                paths.work(role).mkdir()
                paths.binary(role).write_text('#!/bin/sh\nexit 0\n')
                paths.binary(role).chmod(0o755)
                config = {'port': 25774} if role == 'server' else {}
                text = m.service_unit(paths, role, config)
                unit = paths.unit(role)
                unit.write_text(text)
                result = subprocess.run(['systemd-analyze', 'verify', '--man=no', str(unit)],
                                        capture_output=True, text=True)
                assert result.returncode == 0, result.stderr + result.stdout
                # Guard against the actual regression: quoted scalar paths fail.
                unit.write_text(text.replace('WorkingDirectory=' + m.working_directory(paths.work(role)),
                                             'WorkingDirectory=' + m.quote(paths.work(role))))
                old = subprocess.run(['systemd-analyze', 'verify', '--man=no', str(unit)],
                                     capture_output=True, text=True)
                assert old.returncode != 0 and 'WorkingDirectory=' in old.stderr, old.stderr
                print(f'{suffix}: {role} valid; previous quoted path rejected', flush=True)


if __name__ == '__main__':
    main()
