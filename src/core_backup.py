"""DSH Launcher Rebuild - S3 每版本备份/恢复核心（纯 stdlib + paths + robocopy 外部命令）。

* 快照 = runtime/backups/<版本>/snap-<kind>-<yyyyMMdd-HHmmss>/，robocopy /MIR 镜像该版本 home。
* 备份成功写 _DONE.txt（完成标记）；健康判定见 _snap_health：
    - 完全空（排除 _DONE/_GOOD 后无任何内容、也无 profiles）→ issues='empty'，不健康（UI 提示“未启动/空数据目录”）；
    - 否则含 _DONE.txt → 健康（robocopy 干净完成）；
    - 无 _DONE 的旧版/手工映像 → 结构检查：目录非空 且 (settings.yaml 非空 或 profiles 目录存在)。
* 保留策略（check prune）：先删不健康份（若该版本仅剩 1 份且不健康则保留并记 kept-last），
  再把健康份裁到 keep_count()（config keep，默认 3）份最新。
* 恢复前 guard：该版本正在运行则拒绝（CoreError）。恢复后若 home/profiles/web 有 package.json
  而缺 node_modules -> pnpm install --prefer-offline / npm install（npm cache 隔离到 runtime），
  失败仅作为 warning 文本放进返回 hint，不抛错。
* 约束：不得 import core_launch / core_versions（防循环）；路径一律 paths.*；utf-8；
  所有 subprocess 带 creationflags=0x08000000（隐藏窗口）；robocopy 直接跑外部 exe，不包 PowerShell。
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import time

try:
    from . import paths
except ImportError:
    import paths

CREATE_NO_WINDOW = 0x08000000
_DEFAULT_KEEP = 3
_DONE = '_DONE.txt'
_GOOD = '_GOOD.txt'
_SNAP_RE = re.compile(r'^snap-(?P<kind>[^-]+)-(?P<ts>\d{8}-\d{6})$')
_ROBO_ARGS = [
    '/MIR',
    '/R:1',
    '/W:1',
    '/NFL',
    '/NDL',
    '/NP',
    '/NJH',
    '/MT:8']
_DEPS_TIMEOUT = 600


class CoreError(Exception):
    """备份模块错误（GUI 直接展示 str(e)）。"""


def _progress(progress, msg):
    if progress is None:
        return None
    try:
        progress(str(msg))
    except Exception:
        return None
    return None


def _robocopy_exe():
    sysroot = os.environ.get('SystemRoot') or 'C:\\Windows'
    cand = os.path.join(sysroot, 'System32', 'Robocopy.exe')
    if os.path.isfile(cand):
        return cand
    found = shutil.which('robocopy')
    if found:
        return found
    raise CoreError('未找到 robocopy（需 Windows 系统自带 Robocopy.exe）')


def _run_robocopy(src, dst, progress=None, note='', excludes=None):
    """robocopy <src> <dst> /MIR ...（隐藏窗口）。返回码 <8 视为成功，否则抛 CoreError。
excludes: 需排除的子目录绝对路径列表（/XD 追加）。"""
    if not os.path.isdir(src):
        os.makedirs(src, exist_ok=True)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    args = [
        _robocopy_exe(),
        src,
        dst] + list(_ROBO_ARGS)
    if not excludes:
        excludes = []
    for e in excludes:
        args += ['/XD', e]
    _progress(progress, f'{note} robocopy {src} -> {dst}')
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=1800,
            creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise CoreError(f'{note}超时（robocopy 1800s 未完成）: {dst}')
    if p.returncode >= 8:
        tail = (p.stdout or '')[-800:]
        raise CoreError(f'{note}失败（robocopy rc={p.returncode}）:\n{tail}')
    return p.returncode


def _rm_tree(d):
    try:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        return not os.path.exists(d)
    except Exception:
        return False


def _ps(cmd):
    """执行 PowerShell 命令（隐藏窗口）；返回 stdout 字符串；失败返回 ''（调用方按未运行处理）。"""
    try:
        p = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', cmd],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=15,
            creationflags=CREATE_NO_WINDOW)
        if p.returncode == 0:
            return p.stdout or ''
        return ''
    except Exception:
        return ''


def _running_guard(ver, port=3080):
    """若该版本正在运行则抛 CoreError('版本 X 正在运行，请先停止')。
探测方式（轻量，防跨模块依赖）：Get-CimInstance 列 node.exe，CommandLine 包含
paths.data_home(ver) 或 paths.version_cli(ver)（meta 感知：外部实例/全局共存内核
可位于 runtime 外）即视为运行中；超时/失败一律按未运行处理。
注意：port 参数仅保留签名——本模块不做端口探测（HTTP 健康属 core_launch 职责）。"""
    needles = []
    try:
        h = paths.data_home(ver)
        if h:
            needles.append(h)
    except Exception:
        pass
    try:
        c = paths.version_cli(ver)
        if c:
            needles.append(c)
    except Exception:
        pass
    for nd in needles:
        esc = nd.replace("'", "''")
        cmd = 'Get-CimInstance Win32_Process -Filter "Name=\'node.exe\'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and ($_.CommandLine -like \'*%s*\') } | Select-Object -ExpandProperty ProcessId' % esc
        if _ps(cmd).strip():
            raise CoreError('版本 %s 正在运行，请先停止' % ver)
    return None


def keep_count(ver=None):
    '''备份保留份数：ver 给定时优先该版本 meta.keep（实例设置），否则 config keep；默认 3。'''
    if ver is not None:
        try:
            k = int(paths.version_keep(ver))
            if 1 <= k <= 30:
                return k
        except Exception:
            pass
    try:
        with open(paths.config_file(), 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        k = int(cfg.get('keep') or _DEFAULT_KEEP)
        if k >= 1:
            return k
        return _DEFAULT_KEEP
    except Exception:
        return _DEFAULT_KEEP


def backup(ver, kind='manual', full=False, progress=None):
    """备份该版本数据目录(data_home) -> backups_for(ver)/snap-<kind>-<yyyyMMdd-HHmmss>。

robocopy data_home dst /MIR /R:1 /W:1 /NFL /NDL /NP /NJH /MT:8（隐藏窗口）。
默认轻量：排除可重建的 <data_home>/profiles/node_modules 与 profiles/web/node_modules
（避免把几百 MB 依赖也备份）；full=True 时才全量。
数据目录不存在（未首启）→ 先 makedirs 再提示“空数据目录”。
成功写 _DONE.txt(时间戳) 并返回 {'ok','dir','marker'}；失败删除残目录并抛 CoreError。
"""
    _running_guard(ver)
    home = paths.data_home(ver)
    if not os.path.isdir(home):
        os.makedirs(home, exist_ok=True)
        _progress(progress, '数据目录不存在（版本未启动过），已创建空目录：%s' % home)
    _progress(progress, f'备份版本 {ver} (kind={kind}) ...')
    excludes = None
    if not full:
        excludes = [
            os.path.join(home, 'profiles', 'node_modules'),
            os.path.join(home, 'profiles', 'web', 'node_modules')]
    dst_root = paths.backups_for(ver)
    os.makedirs(dst_root, exist_ok=True)
    dst = None
    for _ in range(120):
        cand = os.path.join(
            dst_root,
            f'snap-{kind}-{datetime.datetime.now().strftime("%Y%m%d-%H%M%S")}')
        if not os.path.exists(cand):
            dst = cand
            break
        time.sleep(1.0)
    if dst is None:
        raise CoreError('备份目录名冲突（同秒多次备份且目录存在）')
    try:
        _run_robocopy(home, dst, progress, note='备份', excludes=excludes)
    except CoreError:
        _rm_tree(dst)
        raise
    try:
        with open(os.path.join(dst, _DONE), 'w', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    except Exception:
        _rm_tree(dst)
        raise CoreError('备份失败：无法写入完成标记 _DONE.txt')
    _progress(progress, '备份完成: %s' % dst)
    return {'ok': True, 'dir': dst, 'marker': True}


def _snap_health(directory):
    """-> (healthy: bool, issues: str)。
判定顺序（详见 SPEC S3 / 旧引擎语义；本模块自有备份必写 _DONE.txt）：
  1) 目录缺失 → 不健康；
  2) 空目录（顶层除 _DONE/_GOOD 标记外无任何文件、无任何子目录、且无 profiles）→
     issues='empty'，不健康（UI 层据此提示“版本未启动过 / 空数据目录”；
     空快照 restore 会经 /MIR 清空现有 home，必须拒绝）；
  3) 含 _DONE.txt（本工具 robocopy 干净完成标记）→ 健康；
  4) 无 _DONE 的旧版/手工映像 → 结构检查：目录非空 且
     (settings.yaml 存在且长度>0) 且 (profiles 目录存在)。
     （SPEC/旧引擎为 AND：缺 settings.yaml 或缺 profiles 均判不健康、不可恢复。）
扫描只到顶层（settings.yaml/profiles/标记），不递归 node_modules，避免大目录拖慢列表。
"""
    if not os.path.isdir(directory):
        return (False, '目录不存在')
    try:
        entries = os.listdir(directory)
    except OSError as e:
        return (False, '目录不可读: %s' % e)
    has_done = _DONE in entries
    has_good = _GOOD in entries
    profiles_ok = os.path.isdir(os.path.join(directory, 'profiles'))
    data_files = [
        n for n in entries
        if n not in (_DONE, _GOOD) and os.path.isfile(os.path.join(directory, n))]
    data_dirs = [
        n for n in entries
        if n not in (_DONE, _GOOD) and os.path.isdir(os.path.join(directory, n))]
    if not data_files and not data_dirs and not profiles_ok:
        return (False, 'empty')
    if has_done or has_good:
        return (True, '')
    issues = []
    settings = os.path.join(directory, 'settings.yaml')
    settings_ok = False
    try:
        settings_ok = os.path.isfile(settings) and os.path.getsize(settings) > 0
    except OSError:
        settings_ok = False
    if not settings_ok:
        issues.append('缺 settings.yaml' if not os.path.isfile(settings) else 'settings.yaml 为空')
    if not profiles_ok:
        issues.append('缺 profiles 目录')
    if issues:
        return (False, '; '.join(issues))
    return (True, '')


def list_backups(ver):
    '''按时间降序（新→旧）返回 [{idx,time,kind,state,dir,healthy,files,issues}]。
state: GOOD(含 _GOOD.txt, 兼容旧)/OK(健康)/BAD(不健康, 不可恢复)。
排序按快照名内 yyyyMMdd-HHmmss 时间戳降序（而非整名，避免 boot/manual 前缀干扰）；解析不出则按 mtime。'''
    root = paths.backups_for(ver)
    out = []
    if os.path.isdir(root):
        try:
            names = os.listdir(root)
        except OSError:
            names = []
        for name in names:
            d = os.path.join(root, name)
            m = _SNAP_RE.match(name)
            if not m or not os.path.isdir(d):
                continue
            healthy, issues = _snap_health(d)
            has_good = os.path.isfile(os.path.join(d, _GOOD))
            if has_good and healthy:
                state = 'GOOD'
            elif healthy:
                state = 'OK'
            else:
                state = 'BAD'
            ts = m.group('ts')
            try:
                t = datetime.datetime.strptime(ts, '%Y%m%d-%H%M%S')
                tstr = t.strftime('%Y-%m-%d %H:%M:%S')
                key = ts
            except Exception:
                try:
                    tstr = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(d)))
                    key = '0' + '%020d' % int(os.path.getmtime(d) * 1000)
                except OSError:
                    tstr, key = (ts[:13] + ':00' if len(ts) >= 13 else '?'), '0' + ts
            files, _d = _top_scan(d)
            out.append({
                'idx': None,
                'time': tstr,
                'kind': m.group('kind'),
                'state': state,
                'dir': d,
                'healthy': healthy,
                'files': len(files),
                'issues': issues,
                '_key': key,
                'name': name})
    out.sort(key=lambda x: x['_key'], reverse=True)
    for i, item in enumerate(out):
        item['idx'] = i
    return out


def _top_scan(directory):
    files, dirs = [], []
    try:
        for e in os.scandir(directory):
            if e.is_dir():
                dirs.append(e.name)
            elif e.is_file():
                files.append(e.name)
    except OSError:
        return (files, dirs)
    return (files, dirs)


def _rebuild_deps(home, progress=None):
    """恢复后若缺 home/profiles/web/node_modules 且有 package.json -> 尝试重建依赖。
返回 ''（无需/成功）或 warning 文本 hint（不抛错）。"""
    web = os.path.join(home, 'profiles', 'web')
    pkg = os.path.join(web, 'package.json')
    nm = os.path.join(web, 'node_modules')
    if not os.path.isfile(pkg) or os.path.isdir(nm):
        return ''
    _progress(progress, '恢复后缺 profiles/web/node_modules，尝试重建依赖 ...')
    cache = paths.npm_cache_dir()
    os.makedirs(cache, exist_ok=True)
    comspec = os.environ.get('ComSpec') or os.environ.get('COMSPEC') or 'cmd.exe'
    tail = ''

    def _exec(argv):
        line = subprocess.list2cmdline([str(a) for a in argv])
        try:
            p = subprocess.run(
                [comspec, '/d', '/s', '/c', line],
                cwd=web,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=_DEPS_TIMEOUT,
                creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return (124, 'timeout')
        except OSError as e:
            return (-1, str(e))
        text = ((p.stdout or '') + '\n' + (p.stderr or '')).strip()[-500:]
        return (p.returncode, text)

    pnpm = shutil.which('pnpm') or shutil.which('pnpm.cmd')
    if pnpm:
        _progress(progress, 'pnpm install --prefer-offline (cwd=%s)' % web)
        rc, tail = _exec([pnpm, 'install', '--prefer-offline'])
        if rc == 0 and os.path.isdir(nm):
            return ''
    else:
        _progress(progress, '未找到 pnpm，改用 npm install --no-audit --no-fund --cache %s' % cache)
        rc, tail = _exec(['npm', 'install', '--no-audit', '--no-fund', '--cache', cache])
        if rc == 0 and os.path.isdir(nm):
            return ''
    if pnpm and rc != 0:
        _progress(progress, 'pnpm 失败(rc=%s)，尝试 npm 兜底 ...' % rc)
        rc2, tail = _exec(['npm', 'install', '--no-audit', '--no-fund', '--cache', cache])
        if rc2 == 0 and os.path.isdir(nm):
            return ''
        rc = rc2
    hint = f'依赖自动重建失败(rc={rc})，可手工在 {web} 执行: pnpm install（或 npm install）'
    if tail:
        hint += '\n输出尾部:\n%s' % tail
    _progress(progress, '提示: %s' % hint)
    return hint


def restore(ver, idx, progress=None):
    """把 list_backups(ver)[idx] 那份快照镜像回该版本数据目录(data_home)。
前置：版本未在运行、快照健康（healthy False -> CoreError('该备份不健康不可恢复')）。
返回 {'ok':True,'dir':src,'hint':''|str}（hint 为依赖重建 warning，不抛错）。"""
    _running_guard(ver)
    snaps = list_backups(ver)
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        raise CoreError(f'备份序号无效: {idx!r}')
    if idx < 0 or idx >= len(snaps):
        raise CoreError('无效备份序号 %s（可用 0..%d）' % (idx, len(snaps) - 1))
    s = snaps[idx]
    src = s['dir']
    if not s['healthy']:
        raise CoreError(f'该备份不健康不可恢复（{os.path.basename(src)}）：{s["issues"] or "状态 BAD"}')
    home = paths.data_home(ver)
    os.makedirs(home, exist_ok=True)
    _run_robocopy(src, home, progress, note='恢复')
    hint = _rebuild_deps(home, progress=progress)
    _progress(progress, f'恢复完成: {src} -> {home}')
    return {'ok': True, 'dir': src, 'hint': hint}


def check(ver, prune=False, progress=None):
    """检查该版本全部备份健康度并返回 {'total','healthy','bad','removed':[...]}。
prune=True：删除不健康份（若仅剩 1 份且不健康则保留该份，removed 记 'kept-last'），
再把健康份裁到 keep_count() 份最新。removed 为文本列表（UI 可直接拼接展示）。"""
    snaps = list_backups(ver)
    total = len(snaps)
    healthy = sum(1 for s in snaps if s['healthy'])
    bad = total - healthy
    removed = []
    if prune and snaps:
        for s in list(snaps):
            if s['healthy']:
                continue
            remaining = [x for x in list_backups(ver) if os.path.isdir(x['dir'])]
            if len(remaining) <= 1:
                removed.append('kept-last: %s（仅剩 1 份且不健康，为安全保留）' % s['name'])
                _progress(progress, '仅剩 1 份且不健康，保留不删: %s' % s['name'])
                continue
            _progress(progress, f'删除不健康备份: {s["name"]}（{s["issues"] or "BAD"}）')
            if _rm_tree(s['dir']):
                removed.append('deleted-unhealthy: %s' % s['name'])
                continue
            removed.append('delete-failed: %s' % s['name'])
            continue
        keep = keep_count(ver)
        ok_snaps = [s for s in list_backups(ver) if s['healthy']]
        while len(ok_snaps) > keep:
            old = ok_snaps[-1]
            if _rm_tree(old['dir']):
                removed.append('deleted-over-limit: %s（保留最新 %d 份健康）' % (old['name'], keep))
            else:
                removed.append('delete-failed: %s' % old['name'])
                break
            ok_snaps = [s for s in list_backups(ver) if s['healthy']]
    return {'total': total, 'healthy': healthy, 'bad': bad, 'removed': removed}


def verify_text(ver):
    '''人类可读概要（UI 日志用）。'''
    snaps = list_backups(ver)
    if not snaps:
        return '版本 %s 暂无备份。' % ver
    lines = ['版本 %s 备份（共 %d 份，健康 %d，不健康 %d，保留上限 %d）：' % (
        ver,
        len(snaps),
        sum(1 for s in snaps if s['healthy']),
        sum(1 for s in snaps if not s['healthy']),
        keep_count(ver))]
    for s in snaps:
        note = s['issues'] if not s['healthy'] else ''
        lines.append('  [%d] %-4s %-8s %s  %s%s' % (
            s['idx'],
            s['state'],
            s['kind'],
            s['time'],
            s['name'],
            '  (%s)' % note if note else ''))
    return '\n'.join(lines)
