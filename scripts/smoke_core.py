"""核心模块隔离回归冒烟（不联网、不触碰真实 runtime）。
用法：python scripts/smoke_core.py
在临时 DSLR_RUNTIME 下验证 paths/core_versions/fix_links/core_launch/core_runner。
"""
import json
import os
import shutil
import sys
import tempfile

APP = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
sys.path.insert(0, APP)


def main():
    base = tempfile.mkdtemp(prefix='dsh-core-smoke-')
    rt = os.path.join(base, 'runtime')
    os.makedirs(rt)
    cfg = {'registry': 'auto', 'tag': 'latest', 'keep': 3, 'port': 3080,
           'install_mode': 'local', 'backup_root': '', 'web_url': '',
           'node_tool': '', 'pnpm_tool': ''}
    with open(os.path.join(rt, 'config.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
    os.environ['DSLR_RUNTIME'] = rt

    import paths
    import fix_links
    import core_versions as cv
    import core_launch as cl

    checks = []

    def ok(name, cond, extra=''):
        checks.append((name, cond, extra))
        print(('PASS ' if cond else 'FAIL ') + name + ((' | ' + extra) if extra and not cond else ''))

    ok('runtime_dir env', paths.runtime_dir() == rt)
    paths.ensure_dirs()
    ok('ensure_dirs made runtime subdirs', all(os.path.isdir(os.path.join(rt, d)) for d in
        ('versions', 'logs', 'npm-cache', 'tmp', 'backups', 'overlays')))
    ok('read_config_key', paths.read_config_key('keep') == 3 and paths.read_config_key('nope', 7) == 7)
    r = cv.list_installed()
    ok('list_installed empty', isinstance(r, list) and len(r) == 0)

    v = os.path.join(paths.versions_dir(), '9.9.9')
    os.makedirs(os.path.join(v, 'package', 'node_modules'))
    os.makedirs(os.path.join(v, 'home'))
    with open(os.path.join(v, 'package', 'filler.bin'), 'wb') as f:
        f.write(b'\0' * 300000)
    meta = {'name': 'dsh-9.9.9', 'version': '9.9.9', 'tag': '', 'installed_at': '2026-09-06T10:00:00+08:00',
            'registry': 'local', 'node': '24.20.0', 'install_mode': 'local'}
    cv.write_meta('9.9.9', meta)
    ok('write_meta/read_meta', cv.read_meta('9.9.9').get('version') == '9.9.9')
    lst = cv.list_installed()
    ok('list_installed 1 version', len(lst) == 1 and lst[0]['version'] == '9.9.9')
    ok('size measured+cached', isinstance(lst[0].get('size_mb'), (int, float)) and lst[0]['size_mb'] > 0,
       'size_mb=%r' % (lst[0].get('size_mb'),))
    m2 = cv.read_meta('9.9.9')
    ok('size persisted into meta', 'size_mb' in m2 and m2['size_mb'] == lst[0]['size_mb'])
    lst2 = cv.list_installed()
    ok('list_installed cached second', lst2[0]['size_mb'] == lst[0]['size_mb'])
    ok('version_port meta-default', paths.version_port('9.9.9') == 3080)
    ok('version_keep meta-default', paths.version_keep('9.9.9') == 3)
    ok('fix_links no-op on clean home',
       not fix_links.mirror_needs_repair(paths.home_dir('9.9.9'),
                                         os.path.join(paths.package_dir('9.9.9'), 'node_modules')))
    ok('fix_links repair 0 on clean home', fix_links.repair_home(paths.home_dir('9.9.9'))['shared'] == 0)
    ok('healthy on closed port', cl.healthy(1) is False)
    pn = cl._port_owner_info(9999)
    ok('port_owner none', pn == (None, '', '') or pn[0] is None)
    ok('latest_log empty', cl.latest_log('9.9.9') == '')
    ok('read_log_tail empty', cl.read_log_tail('9.9.9') == ('', ''))

    failed = [c for c in checks if not c[1]]
    print('SUMMARY: %d checks, %d failed' % (len(checks), len(failed)))
    shutil.rmtree(base, ignore_errors=True)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
