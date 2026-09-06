"""S1 版本下载/安装/导入/删除/列表/配置核心模块（纯 stdlib + paths）。

约定（SPEC.md S1 + 集成任务）：
* 所有 npm 调用都带 npm_base_args()：--cache <paths.npm_cache_dir()>（系统 npm 默认
  cache 位于 Program Files 不可写）、--no-audit --no-fund；一律 creationflags=0x08000000。
* 版本目录名 = 具体版本号（paths.version_dir(ver)）。标签安装会把解析结果用作目录名，
  保证 meta['version'] == 目录名，S2/S3/S4 用版本号即可定位 version/home/package 目录。
* 目录型本地导入会先打成 tgz（npm install <dir> 会产生指向源目录的 junction，不自包含），
  保证版本目录完全独立。tgz 源直接安装。
* 异常抛 CoreError；耗时函数可接收 progress(msg)->None 回调（GUI 只展示 str(e)）。
* 测试：设 DSLR_RUNTIME 指向 _test 内目录；绝不触碰真实 ~/.dsh 或 3080 端口。
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import uuid

try:
    from . import paths
except ImportError:
    # 独立/打包运行：app 目录直接在 sys.path 上，回退普通导入
    import paths

__all__ = [
    'CoreError',
    'load_config',
    'save_config',
    'registry_url',
    'npm_base_args',
    'list_remote',
    'resolve_version',
    'install_version',
    'import_local',
    'list_installed',
    'version_exists',
    'remove_version',
    'read_meta',
    'write_meta',
    'node_cmd']

PKG = '@deepseek-ai/dsh'
CREATE_NO_WINDOW = 0x08000000
VIEW_TIMEOUT = 120
INSTALL_TIMEOUT = 600
ERROR_TAIL = 500
_REGISTRIES = {
    'auto': 'https://registry.npmjs.org/',
    'npmjs': 'https://registry.npmjs.org/',
    'npmmirror': 'https://registry.npmmirror.com/'}
_DEFAULTS = {
    'registry': 'auto',
    'tag': 'latest',
    'keep': 3,
    'port': 3080,
    'install_mode': 'local',
    'backup_root': '',
    'web_url': ''}
_SAFE_VERSION = re.compile(r'^[0-9A-Za-z][0-9A-Za-z.+-]*$')


class CoreError(Exception):
    """S1 模块统一异常。"""


def load_config():
    """读 paths.config_file() 的 JSON；缺失/损坏时回退默认值（registry/tag/keep/port）。"""
    cfg = dict(_DEFAULTS)
    try:
        with open(paths.config_file(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if v is not None})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    """把配置写回 paths.config_file()（JSON, utf-8）。成功返回 True，失败返回 False（不抛，
兼容旧实现与设置弹窗的 `if not save_config(...)` 用法）。"""
    data = dict(cfg or {})
    try:
        os.makedirs(os.path.dirname(paths.config_file()), exist_ok=True)
        tmp = paths.config_file() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, paths.config_file())
        return True
    except OSError:
        return False


def _registry_url(value):
    """把 registry 配置项转成 URL：'auto'|'npmjs'|'npmmirror'|自定义 url。"""
    v = str(value or '').strip()
    if v.lower() in _REGISTRIES:
        return _REGISTRIES[v.lower()]
    if not v:
        return _REGISTRIES['auto']
    if '://' not in v:
        v = 'https://' + v
    return v.rstrip('/') + '/'


def registry_url():
    """按配置返回 registry URL：auto→官方 npmjs（本函数不重试，调用方可再试 npmmirror）。"""
    return _registry_url(load_config().get('registry', 'auto'))


def npm_base_args():
    """所有 npm 调用必带参数：cache 落到 runtime（可写），跳过 audit/fund。"""
    return [
        '--cache',
        paths.npm_cache_dir(),
        '--no-audit',
        '--no-fund']


def node_cmd():
    """返回 (node路径, npm路径)。npm 优先 npm.cmd（本机 npm 是 .ps1，不能直接 subprocess）。

便携优先：runtime\\tools\\node 内的捆绑 node/npm 存在时优先使用（随目录走、不依赖 PATH）。
之后才回退 PATH / 常见安装位置。"""
    bundled_node = paths.node_exe()
    bundled_npm = paths.npm_exe()
    if os.path.isfile(bundled_node) and os.path.isfile(bundled_npm):
        return (bundled_node, bundled_npm)
    node = shutil.which('node')
    npm = shutil.which('npm.cmd')
    if not npm:
        if node:
            cand = os.path.join(os.path.dirname(node), 'npm.cmd')
            if os.path.isfile(cand):
                npm = cand
        if not npm:
            cand = shutil.which('npm')
            if cand and not cand.lower().endswith(('.ps1', '.sh')):
                npm = cand
    if not node:
        raise CoreError('未检测到 node，请先安装 Node.js（https://nodejs.org）并加入 PATH，或在 runtime\\tools\\node 放入便携 node。')
    if not npm:
        raise CoreError('未检测到 npm.cmd，请确认 Node.js 安装完整（npm 与 node 同目录）。')
    return (node, npm)


def _node_version():
    """取 node 版本号（如 24.20.0）；失败返回 ''。"""
    try:
        node, _ = node_cmd()
        r = subprocess.run([node, '--version'], capture_output=True, timeout=30,
                           creationflags=CREATE_NO_WINDOW)
        if r.returncode == 0:
            return (r.stdout or b'').decode('utf-8', 'replace').strip().lstrip('v')
        return ''
    except Exception:
        return ''


_PROBE_CACHE = {
    'v': None,
    't': 0.0}
_PROBE_TTL = 180.0
_PROBE_TIMEOUT = 4.0


def _reg_candidates(cfg=None):
    """按配置展开候选 registry URL（有序：先官方后镜像/自定义优先）。"""
    c = cfg or (load_config().get('registry') or 'auto')
    if isinstance(c, str) and c.startswith('http'):
        return [c]
    m = {
        'auto': ['https://registry.npmjs.org/', 'https://registry.npmmirror.com/'],
        'npmjs': ['https://registry.npmjs.org/'],
        'npmmirror': ['https://registry.npmmirror.com/']}
    return m.get(c, m['auto'])


def detect_system_proxy():
    """返回可用 HTTP 代理（env 优先，其次 Windows 系统代理(Clash 等)）；无则 ''。"""
    for k in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
        v = (os.environ.get(k) or '').strip()
        if not v:
            continue
        return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as k:
            try:
                en, _ = winreg.QueryValueEx(k, 'ProxyEnable')
            except OSError:
                en = 0
            if en:
                try:
                    srv, _ = winreg.QueryValueEx(k, 'ProxyServer')
                except OSError:
                    srv = ''
                if srv and not srv.startswith('='):
                    srv = srv.split(';')[0].strip()
                    if srv:
                        return srv if '://' in srv else 'http://' + srv
    except Exception:
        pass
    return ''


def _probe_registry(url, proxy):
    """单点可达性：GET <url>/-/ping（兜底 GET 根），收到任意 HTTP 响应即“走通”。"""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({'http': proxy, 'https': proxy}) if proxy else urllib.request.ProxyHandler({}))
    heads = {'User-Agent': 'DSH-Launcher/1 (registry probe)'}
    for path in ('/-/ping', ''):
        req = urllib.request.Request(url.rstrip('/') + path, headers=heads)
        t0 = time.time()
        try:
            with opener.open(req, timeout=_PROBE_TIMEOUT):
                return time.time() - t0
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return time.time() - t0
            continue
        except Exception:
            continue
    return None


def working_registry(cfg=None, force=False):
    """自动选出“走通”的源：(url, proxy)。

流程：并行直连探测候选源 → 全失败且有系统代理 → 再带代理探测；
仍失败回退配置首选(不改用户配置)。结果缓存 3 分钟。"""
    now = time.time()
    if not force and _PROBE_CACHE['v'] and now - _PROBE_CACHE['t'] < _PROBE_TTL:
        return _PROBE_CACHE['v']
    cands = _reg_candidates(cfg)
    proxy = detect_system_proxy()

    def _best_with(proxy_):
        results = []

        def probe(u):
            ms = _probe_registry(u, proxy_)
            if ms is not None:
                results.append((ms, u))
            return None

        threads = [threading.Thread(target=probe, args=(u,)) for u in cands]
        for t in threads:
            t.start()
        for t in threads:
            t.join(_PROBE_TIMEOUT + 1)
        if results:
            results.sort(key=lambda r: r[0])
            return results[0][1]
        return ''

    url = _best_with(None)
    used_proxy = ''
    if not url and proxy:
        url = _best_with(proxy)
        used_proxy = proxy if url else ''
    if not url:
        url = cands[0]
    _PROBE_CACHE['t'] = now
    _PROBE_CACHE['v'] = (url, used_proxy)
    return (url, used_proxy)


def net_env(url=None, proxy=None):
    """给子进程(含 npm/pnpm/node)的环境补丁：registry + 代理。"""
    patch = {}
    if url:
        patch['npm_config_registry'] = url
    if proxy:
        patch.update({
            'HTTP_PROXY': proxy,
            'HTTPS_PROXY': proxy,
            'http_proxy': proxy,
            'https_proxy': proxy})
    return patch


def _run_npm(argv, timeout, progress=None, env_extra=None):
    """跑 npm（自动拼 npm.cmd 路径、隐藏窗口、可写 cache、utf-8 解码）。
返回 (returncode, stdout_text, stderr_text)。timeout 触发时抛 CoreError。
env_extra：额外环境补丁（如代理 HTTP_PROXY/HTTPS_PROXY、npm_config_registry）。"""
    try:
        _node, npm = node_cmd()
    except CoreError as e:
        raise CoreError('无法运行 npm：%s' % e)
    os.makedirs(paths.npm_cache_dir(), exist_ok=True)
    env = dict(os.environ)
    env['npm_config_cache'] = paths.npm_cache_dir()
    if env_extra:
        env.update({k: v for k, v in env_extra.items() if v})
    if progress:
        try:
            progress('npm %s' % ' '.join(argv[:6]))
        except Exception:
            pass
    try:
        p = subprocess.run([npm] + list(argv), capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, env=env)
    except subprocess.TimeoutExpired:
        raise CoreError('npm 执行超时（>%ds）：%s' % (timeout, ' '.join(argv[:4])))
    except OSError as e:
        raise CoreError('npm 执行失败：%s' % e)
    return (p.returncode, _dec(p.stdout), _dec(p.stderr))


def _dec(b):
    if b:
        return b.decode('utf-8', 'replace')
    return ''


def _tail(text, limit=ERROR_TAIL):
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    return '…' + text[-limit:]


def _json_loads(text):
    """npm --json 输出可能带 BOM/警告前缀，定位第一个 {/[ 再解析。"""
    s = (text or '').strip().lstrip('\ufeff')
    for i, ch in enumerate(s):
        if ch in '[{':
            try:
                return json.loads(s[i:])
            except ValueError:
                continue
    raise ValueError('无法解析 npm JSON 输出')


def _ver_key(v):
    """semver 排序键：核心(数字)升序；无预发布 > 有预发布；预发布按段比较。"""
    v = str(v or '').strip()
    try:
        core, _, pre = v.partition('-')
        nums = tuple(int(x) if x else 0 for x in core.split('.'))
        nums = nums + (0,) * (3 - len(nums))
        if not pre:
            return (nums, 1, ())
        pre_ids = []
        for pid in pre.split('.'):
            pre_ids.append((0, int(pid)) if pid.isdigit() else (1, pid))
        return (nums, 0, tuple(pre_ids))
    except Exception:
        return ((), 0, (v,))


def list_remote(tag='latest', registry_override=None):
    """查询远端 dist-tags 与全部版本。返回 {'tag_ver','versions','tags','error'}。
versions 为全量列表、按 semver 降序；tags 为全部 dist-tags 映射（附加，UI 展示用）。
网络/解析失败不抛异常，error 带人类可读信息。"""
    url = _registry_url(registry_override) if registry_override is not None else registry_url()
    error = ''
    versions = []
    tag_ver = ''
    tags = {}
    try:
        rc, out, err = _run_npm(
            ['view', PKG, 'dist-tags', '--json', '--registry', url,
             '--fetch-timeout=15000', '--fetch-retries=1'] + npm_base_args(),
            timeout=VIEW_TIMEOUT)
        if rc == 0:
            obj = _json_loads(out)
            if isinstance(obj, dict):
                tags = {str(k): str(v) for k, v in obj.items()}
                tag_ver = tags.get(str(tag), '')
            else:
                error = 'dist-tags 返回格式异常'
        else:
            error = '查询 dist-tags 失败：' + _tail(err or out)
    except CoreError as e:
        error = str(e)
    except Exception as e:
        error = '解析 dist-tags 失败：%s' % e
    if not error:
        try:
            rc, out, err = _run_npm(
                ['view', PKG, 'versions', '--json', '--registry', url,
                 '--fetch-timeout=15000', '--fetch-retries=1'] + npm_base_args(),
                timeout=VIEW_TIMEOUT)
            if rc == 0:
                obj = _json_loads(out)
                if isinstance(obj, list):
                    versions = [str(x) for x in obj]
                elif isinstance(obj, dict):
                    versions = [str(k) for k in obj.keys()]
                else:
                    error = 'versions 返回格式异常'
            else:
                error = '查询 versions 失败：' + _tail(err or out)
        except CoreError as e:
            error = str(e)
        except Exception as e:
            error = '解析 versions 失败：%s' % e
    if not error:
        versions = sorted(set(versions), key=_ver_key, reverse=True)
    return {
        'tag_ver': tag_ver,
        'versions': versions,
        'tags': tags,
        'error': error}


def _looks_version(s):
    """粗略判断是否已像具体版本号（标签不可能是合法 semver，因此数字开头视为版本）。"""
    return bool(re.match('^[0-9]', s or ''))


def _strip_at(v):
    """去掉版本串里的 @scope/name@ 前缀（取最后一个 @ 之后），供目录名/标签判定用。"""
    v = str(v or '').strip()
    if '@' in v:
        return v.rsplit('@', 1)[-1].strip()
    return v


def resolve_version(ver, registry=None):
    """'latest'/'next'/'alpha' 等标签 → 查询 registry 返回具体版本号；
具体版本号原样返回；含 @ 的写法（如 @scope/name@1.2.3）剥到最后一个 @ 之后。
registry 可为 URL/预设名（npmmirror 等），None 时用配置。解析失败抛 CoreError。"""
    v = _strip_at(ver)
    if not v:
        raise CoreError('版本/标签为空')
    if _looks_version(v):
        return v
    url, _p = working_registry(registry)
    info = list_remote(tag=v, registry_override=url)
    if info.get('error'):
        raise CoreError(f'解析标签 {v!r} 失败：{info["error"]}')
    if not info.get('tag_ver'):
        raise CoreError('registry 上没有标签 %r' % v)
    return info['tag_ver']


def _iso_now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def _check_version_name(ver, what='版本'):
    if not _SAFE_VERSION.match(str(ver or '')):
        raise CoreError(f'{what}名非法（可能包含路径字符）：{ver!r}')
    return None


def _global_kernel_dir(prefix, resolved):
    """Node 全局共存内核目录：<prefix>/dsh-versions/<resolved>（不同版本互不覆盖）。"""
    return os.path.join(str(prefix), 'dsh-versions', str(resolved).strip())


def _write_global_cmd(prefix, resolved, cli):
    """在 npm 全局前缀生成 dsh-<version>.cmd（与 dsh.cmd 同目录 → 自动进入 PATH）。"""
    cmd_path = os.path.join(str(prefix), 'dsh-%s.cmd' % resolved)
    try:
        with open(cmd_path, 'w', encoding='ascii') as f:
            f.write('@echo off\r\n')
            f.write('node "%s" %%*\r\n' % cli.replace('/', '\\'))
        return cmd_path
    except OSError as e:
        raise CoreError(f'无法写入全局命令 {cmd_path}: {e}（前缀在 Program Files 下请以管理员运行，或改用「独立目录」模式）')


def install_version(ver, registry=None, progress=None, mode=None, prefix=''):
    """下载并安装 @deepseek-ai/dsh@<ver>。

mode='local'（默认；可配置 install_mode）→ 内核装 runtime/versions/<resolved>/package；
mode='global' → 内核装 <npm 全局前缀>/dsh-versions/<resolved>，并在前缀生成全局命令
  dsh-<resolved>.cmd（数据目录仍为本启动器按版本独立 home；列表统一管理并标 [全局]）。
返回 meta dict（含 install_mode/cli/global_*）。失败抛 CoreError（带 npm stderr 尾部）。"""
    if progress:
        try:
            progress('解析版本/标签：%s …' % ver)
        except Exception:
            pass
    resolved = resolve_version(ver, registry)
    _check_version_name(resolved)
    if version_exists(resolved):
        raise CoreError('版本 %s 已安装；如需重装请先删除该版本。' % resolved)
    tag = _strip_at(ver)
    tag = tag if tag and not _looks_version(tag) else ''
    url, _proxy = working_registry(registry)
    env_patch = net_env(url, _proxy)
    mode = mode or (load_config().get('install_mode') or 'local')
    install_mode = 'global' if str(mode).strip() == 'global' else 'local'
    global_prefix = ''
    if install_mode == 'global':
        global_prefix = (str(prefix).strip().strip('"') or detect_global_prefix())
        if not global_prefix:
            raise CoreError('未能确定 npm 全局前缀（npm prefix -g）。可在下载窗口手动填写，或改用「独立目录」安装模式。')
        try:
            os.makedirs(global_prefix, exist_ok=True)
        except OSError:
            pass
        if not os.path.isdir(global_prefix):
            raise CoreError('npm 全局前缀不存在：%s' % global_prefix)
        try:
            probe = os.path.join(global_prefix, '.dshw')
            with open(probe, 'w') as f:
                f.write('1')
            os.remove(probe)
        except OSError:
            raise CoreError('npm 全局前缀不可写：%s\n请以管理员运行本启动器，或改用独立目录模式。' % global_prefix)
        kernel_base = _global_kernel_dir(global_prefix, resolved)
    else:
        kernel_base = paths.package_dir(resolved)
    paths.ensure_dirs()
    vdir = paths.version_dir(resolved)
    vdir_preexisted = os.path.isdir(vdir)
    os.makedirs(kernel_base, exist_ok=True)
    spec = f'{PKG}@{resolved}'
    if progress:
        try:
            progress(f'下载安装 {spec}（registry: {url}，{"Node 全局共存" if install_mode == "global" else "独立目录"}）…')
        except Exception:
            pass
    try:
        rc, out, err = _run_npm(
            ['install', '--prefix', kernel_base, spec, '--registry', url,
             '--loglevel=error'] + npm_base_args(),
            timeout=INSTALL_TIMEOUT, progress=progress, env_extra=env_patch)
        if rc != 0:
            raise CoreError(f'安装 {spec} 失败：{_tail(err or out)}')
        installed = os.path.join(kernel_base, 'node_modules', PKG, 'package.json')
        if not os.path.isfile(installed):
            raise CoreError('安装完成但未找到 %s 的 package.json（registry 内容异常？）' % PKG)
        cli = os.path.join(kernel_base, 'node_modules', PKG, 'lib', 'bin.js')
        global_cmd = ''
        if install_mode == 'global':
            global_cmd = _write_global_cmd(global_prefix, resolved, cli)
            if progress:
                try:
                    progress(f'已生成全局命令：{os.path.basename(global_cmd)}（任何终端可直接使用 dsh-{resolved}）')
                except Exception:
                    pass
        os.makedirs(paths.home_dir(resolved), exist_ok=True)
        cli_record = cli if install_mode == 'global' else paths.cli_rel_record(cli)
        meta = {
            'name': 'dsh-%s' % resolved,
            'version': resolved,
            'tag': tag,
            'installed_at': _iso_now(),
            'registry': url,
            'node': _node_version(),
            'install_mode': install_mode,
            'cli': cli_record,
            'global_prefix': global_prefix,
            'global_cmd': global_cmd}
        # 体积一次性统计并缓存进 meta（避免列表每次刷新全量递归扫描整个版本目录）
        try:
            meta['size_mb'] = _dir_size_mb(paths.version_dir(resolved))
        except Exception:
            pass
        write_meta(resolved, meta)
        if progress:
            try:
                progress(f'安装完成：{resolved}（{"Node 全局共存" if install_mode == "global" else "独立目录"}）')
            except Exception:
                return meta
        return meta
    except CoreError:
        if install_mode == 'global':
            shutil.rmtree(kernel_base, ignore_errors=True)
            if 'global_cmd' in dir() and global_cmd:
                try:
                    os.remove(global_cmd)
                except OSError:
                    pass
            raise
        if vdir_preexisted:
            shutil.rmtree(paths.package_dir(resolved), ignore_errors=True)
        else:
            shutil.rmtree(vdir, ignore_errors=True)
        raise


def _stage_dir_as_tgz(src_dir):
    """把目录打包成 npm 布局的 tgz（顶层 package/），排除 node_modules/.git 等
（与 npm pack 语义一致）。目录导入必须走 tgz，否则 npm 会建 junction 指向源目录。"""
    base = os.path.basename(os.path.normpath(src_dir)) or 'dsh'
    tgz = os.path.join(paths.tmp_dir(), f'import-{base}-{uuid.uuid4().hex[:8]}.tgz')
    os.makedirs(paths.tmp_dir(), exist_ok=True)
    skip = {
        '__pycache__',
        'node_modules',
        '.git',
        '.DS_Store'}
    try:
        with tarfile.open(tgz, 'w:gz') as tf:
            for root, dirs, files in os.walk(src_dir):
                dirs[:] = [d for d in dirs if d not in skip]
                rel = os.path.relpath(root, src_dir)
                arc_dir = 'package' if rel == '.' else os.path.join('package', rel).replace('\\', '/')
                tf.add(root, arcname=arc_dir, recursive=False)
                for fn in files:
                    arc = os.path.join(arc_dir, fn).replace('\\', '/')
                    tf.add(os.path.join(root, fn), arcname=arc)
        return tgz
    except (OSError, tarfile.TarError) as e:
        try:
            os.remove(tgz)
        except OSError:
            pass
        raise CoreError('打包本地目录失败：%s' % e)


def _peek_tgz_meta(tgz_path):
    """读 tgz 内 package/package.json 的 name/version（不落盘）。"""
    try:
        with tarfile.open(tgz_path, 'r:gz') as tf:
            for m in tf.getmembers():
                n = m.name.replace('\\', '/')
                if n.endswith('/package.json') and '/' not in n[:-len('/package.json')]:
                    f = tf.extractfile(m)
                    if f:
                        data = json.loads(f.read().decode('utf-8', 'replace'))
                        return (str(data.get('name', '')), str(data.get('version', '') or ''))
    except (OSError, tarfile.TarError, ValueError, KeyError):
        pass
    return ('', '')


def import_local(src, progress=None):
    """导入本地 .tgz 或目录（目录须含 package.json 且 name=@deepseek-ai/dsh）。
安装到 runtime/versions/<版本>/package，版本号从已安装包 package.json 读取（该版本名即目录名），
tag='local'，返回 meta dict。失败抛 CoreError。"""
    src = str(src or '').strip().strip('"')
    if not os.path.exists(src):
        raise CoreError('本地源不存在：%s' % src)
    is_dir = os.path.isdir(src)
    ver = ''
    name = ''
    if is_dir:
        pj = os.path.join(src, 'package.json')
        if not os.path.isfile(pj):
            raise CoreError('目录 %s 中缺少 package.json' % src)
        try:
            with open(pj, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise CoreError(f'读取 {pj} 失败：{e}')
        name = str(data.get('name') or '')
        ver = str(data.get('version') or '')
    elif src.lower().endswith('.tgz'):
        name, ver = _peek_tgz_meta(src)
        if not name:
            raise CoreError('%s 不是有效的 npm tgz（找不到 package/package.json）' % src)
    else:
        raise CoreError('本地导入仅支持 .tgz 文件或包含 package.json 的目录：%s' % src)
    if name != PKG:
        raise CoreError(f'包名应为 {PKG}，实际为 {name!r}（不是 dsh 内核）')
    if not ver:
        raise CoreError('包的 package.json 缺少 version 字段')
    _check_version_name(ver)
    if version_exists(ver):
        raise CoreError('版本 %s 已存在；如需覆盖请先删除该版本。' % ver)
    if progress:
        try:
            progress(f'导入本地 {PKG}（{os.path.basename(src)} v{ver}）…')
        except Exception:
            pass
    paths.ensure_dirs()
    tgz_cleanup = None
    vdir_preexisted = os.path.isdir(paths.version_dir(ver))
    final_ver = ver
    try:
        target = src
        if is_dir:
            if progress:
                try:
                    progress('打包本地目录为 tgz …')
                except Exception:
                    pass
            target = _stage_dir_as_tgz(src)
            tgz_cleanup = target
        pkg_dir = paths.package_dir(ver)
        os.makedirs(pkg_dir, exist_ok=True)
        rc, out, err = _run_npm(
            ['install', '--prefix', pkg_dir, os.path.abspath(target),
             '--loglevel=error'] + npm_base_args(),
            timeout=INSTALL_TIMEOUT, progress=progress)
        if rc != 0:
            raise CoreError('本地导入失败：%s' % _tail(err or out))
        installed_pj = os.path.join(pkg_dir, 'node_modules', PKG, 'package.json')
        if not os.path.isfile(installed_pj):
            raise CoreError('导入完成但未找到 %s 的 package.json' % PKG)
        with open(installed_pj, 'r', encoding='utf-8') as f:
            real_ver = str(json.load(f).get('version') or '')
        if not real_ver:
            raise CoreError('已安装包缺少 version 字段')
        if real_ver != ver:
            new_vdir = paths.version_dir(real_ver)
            if os.path.exists(new_vdir):
                raise CoreError(f'已安装版本 {ver} 与包内版本 {real_ver} 不一致且目录已存在')
            os.rename(paths.version_dir(ver), new_vdir)
            ver = real_ver
        final_ver = ver
        os.makedirs(paths.home_dir(ver), exist_ok=True)
        meta = {
            'name': 'dsh-%s' % ver,
            'version': ver,
            'tag': 'local',
            'installed_at': _iso_now(),
            'registry': 'local',
            'node': _node_version()}
        try:
            meta['size_mb'] = _dir_size_mb(paths.version_dir(ver))
        except Exception:
            pass
        write_meta(ver, meta)
        if progress:
            try:
                progress('导入完成：%s' % ver)
            except Exception:
                pass
        return meta
    except CoreError:
        if vdir_preexisted:
            shutil.rmtree(paths.package_dir(final_ver), ignore_errors=True)
        else:
            shutil.rmtree(paths.version_dir(final_ver), ignore_errors=True)
        raise
    finally:
        if tgz_cleanup:
            try:
                os.remove(tgz_cleanup)
            except OSError:
                pass


def read_meta(ver):
    """读版本 meta.json；缺失/损坏时抛 CoreError。"""
    p = paths.meta_file(ver)
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        raise CoreError(f'版本 {ver} 未安装（缺少 {p}）')
    except (OSError, ValueError) as e:
        raise CoreError(f'读取 {p} 失败：{e}')
    if not isinstance(data, dict):
        raise CoreError('%s 内容不是 JSON 对象' % p)
    return data


def write_meta(ver, meta):
    """写版本 meta.json（原子替换）。"""
    p = paths.meta_file(ver)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return None
    except OSError as e:
        raise CoreError(f'写 {p} 失败：{e}')


def version_exists(ver):
    """该版本目录已完整安装（存在 meta.json）。"""
    return os.path.isfile(paths.meta_file(ver))


def _dir_size_mb(path):
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    continue
    except Exception:
        return 0.0
    return round(total / 1048576, 2)


def _size_cached_or_measure(name, meta):
    """体积优先用 meta 缓存；缺缓存（老版本升级/跨机搬迁后首次）才做一次全量
    统计并回写，避免 list_installed 每次刷新都递归扫描数万文件造成卡顿。"""
    try:
        v = meta.get('size_mb')
        if isinstance(v, (int, float)) and v >= 0:
            return round(float(v), 2)
    except Exception:
        pass
    try:
        size = _dir_size_mb(paths.version_dir(name))
        meta['size_mb'] = size
        try:
            write_meta(name, meta)
        except Exception:
            pass
        return size
    except Exception:
        return 0.0


def list_installed():
    """扫描 versions/ 下带 meta.json 的目录 → [meta + dirs/size_mb/has_bin/running]。
数据目录与内核按 meta 解析（data/cli 可指向 runtime 外：外部实例/全局共存）。
按 installed_at 降序；running 由 S2 后填（此处 False）。"""
    out = []
    base = paths.versions_dir()
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if not os.path.isfile(paths.meta_file(name)):
                continue
            paths.rewrite_cli_relative(name)
            try:
                meta = read_meta(name)
                entry = dict(meta)
                entry['dirs'] = {
                    'version': paths.version_dir(name),
                    'home': paths.data_home(name),
                    'package': paths.package_dir(name)}
                entry['size_mb'] = _size_cached_or_measure(name, meta)
                cli = paths.version_cli(name)
                entry['cli'] = cli
                entry['has_bin'] = (os.path.isfile(cli) or os.path.isfile(paths.bin_shim(name))
                                    or os.path.isfile(paths.dsh_cli_abs(name)))
                entry['running'] = False
                out.append(entry)
            except CoreError:
                continue

    def _key(e):
        try:
            return datetime.datetime.fromisoformat(str(e.get('installed_at') or '')).timestamp()
        except ValueError:
            return 0.0

    out.sort(key=_key, reverse=True)
    return out


def remove_version(ver):
    """删除版本整个目录（package+home+meta）；全局共存模式顺带清理 <prefix>/dsh-versions/<ver>
与 dsh-<ver>.cmd。目录不存在视为成功（幂等）；删不掉抛 CoreError。"""
    meta = {}
    if os.path.isfile(paths.meta_file(ver)):
        try:
            meta = read_meta(ver)
        except CoreError:
            meta = {}
    if meta.get('install_mode') == 'global':
        gp = str(meta.get('global_prefix') or '').strip()
        if gp:
            shutil.rmtree(_global_kernel_dir(gp, meta.get('version') or ver), ignore_errors=True)
        gc = str(meta.get('global_cmd') or '').strip()
        if gc:
            try:
                if os.path.isfile(gc):
                    os.remove(gc)
            except OSError:
                pass
    vdir = paths.version_dir(ver)
    if not os.path.isdir(vdir):
        return None

    def _onerr(func, path, exc_info):
        try:
            os.chmod(path, 0o700)
            func(path)
        except OSError:
            return None
        return None

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(vdir, onexc=_onerr)
        else:
            shutil.rmtree(vdir, onerror=_onerr)
    except OSError as e:
        raise CoreError(f'删除版本 {ver} 失败：{e}（可能文件被占用）')
    if os.path.exists(vdir):
        raise CoreError('删除版本 %s 未完全成功：仍有残留文件' % ver)
    return None


if __name__ == '__main__':
    print('registry_url:', registry_url())
    print('node_cmd:', node_cmd())


def detect_global_prefix():
    """npm 全局前缀（npm prefix -g，写 runtime 缓存）。失败返回 ''。"""
    try:
        node, npm = node_cmd()
        r = subprocess.run(
            [npm, '--registry', registry_url(), '--no-update-notifier',
             'prefix', '-g', '--cache', paths.npm_cache_dir()],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=25, creationflags=CREATE_NO_WINDOW)
        if r.returncode == 0:
            p = (r.stdout or '').strip()
            if p and os.path.isdir(p):
                return p
    except Exception:
        pass
    return ''


def _system_default_data_dir():
    """系统实例“默认数据目录”：env DSH_HOME > runtime\\.dsh（便携兜底，替代 ~/.dsh；
目录存在才返回该路径）。"""
    d = (os.environ.get('DSH_HOME') or '').strip().strip('"')
    if d:
        return d
    cand = paths.dot_dsh_dir()
    if os.path.isdir(cand):
        return cand
    return ''


def _pkg_info(candidate_pkg_dir):
    """candidate_pkg_dir 若真是 @deepseek-ai/dsh 包目录 -> info dict 或 None。"""
    pj = os.path.join(candidate_pkg_dir, 'package.json')
    if not os.path.isfile(pj):
        return None
    try:
        with open(pj, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None
    if (data.get('name') or '') != PKG:
        return None
    ver = str(data.get('version') or 'unknown')
    cli = os.path.join(candidate_pkg_dir, 'lib', 'bin.js')
    return {'version': ver,
            'pkg_dir': candidate_pkg_dir,
            'cli': cli if os.path.isfile(cli) else ''}


def scan_system_instances(extra_dirs=None):
    """自动扫描系统已安装的 dsh 实例（只读、不下载）。

候选：PATH 中的 dsh shim 目录、npm root -g、常见位置(APPDATA/LOCALAPPDATA/PROGRAMFILES 的
npm/nodejs/nvm)、调用方额外目录。去重后返回：
[ {version, kind, pkg_dir, cli, prefix, data_default} ]，版本号降序。"""
    found = {}
    candidates = []

    def add_dir(nm_dir, kind):
        if nm_dir and os.path.isdir(nm_dir):
            info = _pkg_info(os.path.join(nm_dir, '@deepseek-ai', 'dsh'))
            if not info:
                return None
            key = os.path.normcase(os.path.normpath(info['pkg_dir']))
            if key not in found:
                found[key] = {'version': info['version'],
                              'kind': kind,
                              'pkg_dir': info['pkg_dir'],
                              'cli': info['cli'],
                              'prefix': os.path.dirname(nm_dir),
                              'data_default': _system_default_data_dir()}
                return None
            return None
        return None

    for name in ('dsh.cmd', 'dsh.exe', 'dsh'):
        shim = shutil.which(name)
        if not shim:
            continue
        sd = os.path.dirname(shim)
        candidates.append((os.path.join(sd, 'node_modules'), 'npm 全局(shim)'))
        candidates.append((os.path.join(os.path.dirname(sd), 'node_modules'), 'npm 全局(shim 上级)'))
        break
    try:
        node, npm = node_cmd()
        r = subprocess.run(
            [npm, '--registry', registry_url(), '--no-update-notifier',
             'root', '-g', '--cache', paths.npm_cache_dir()],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=25, creationflags=CREATE_NO_WINDOW)
        if r.returncode == 0 and (r.stdout or '').strip():
            candidates.append(((r.stdout or '').strip(), 'npm 全局(root-g)'))
    except Exception:
        pass
    base_env = []
    for k in ('LOCALAPPDATA', 'APPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)'):
        v = os.environ.get(k)
        if not v:
            continue
        base_env.append(v)
    for b in base_env:
        candidates.append((os.path.join(b, 'npm', 'node_modules'), '常见位置'))
        candidates.append((os.path.join(b, 'nodejs', 'node_modules'), '常见位置'))
        candidates.append((os.path.join(b, 'nvm', 'node_modules'), '常见位置'))
        nvmdir = os.path.join(b, 'nvm')
        try:
            if os.path.isdir(nvmdir):
                for vname in os.listdir(nvmdir):
                    vp = os.path.join(nvmdir, vname, 'node_modules')
                    if os.path.isdir(vp):
                        candidates.append((vp, 'nvm'))
        except Exception:
            continue
    for d in (extra_dirs or []):
        candidates.append((os.path.join(str(d), 'node_modules'), '用户指定'))
        candidates.append((str(d), '用户指定'))
    for nm_dir, kind in candidates:
        add_dir(nm_dir, kind)
    items = list(found.values())
    items.sort(key=lambda x: x['version'], reverse=True)
    return items


def adopt_external(inst, progress=None):
    """收纳系统已装实例（引用式：不复制内核，数据沿用原数据或新独立目录）。

在 runtime/versions/<version> 写 meta{external, cli, data…}；同名已存在则抛 CoreError。
返回 meta。"""
    ver = str(inst.get('version') or '').strip()
    cli = str(inst.get('cli') or '').strip()
    if not (ver and _SAFE_VERSION.match(ver)):
        raise CoreError('无法识别该实例的版本号。')
    if version_exists(ver):
        raise CoreError('版本 %s 已存在（本启动器已管理），无需重复收纳。' % ver)
    if not (cli and os.path.isfile(cli)):
        raise CoreError('实例内核入口缺失，无法使用：%s' % (cli or '(空)'))
    paths.ensure_dirs()
    data = str(inst.get('data_default') or '').strip()
    meta = {'name': 'dsh-' + ver,
            'version': ver,
            'tag': 'external',
            'external': True,
            'external_kind': str(inst.get('kind') or '系统'),
            'pkg_dir': str(inst.get('pkg_dir') or ''),
            'cli': cli,
            'data': data,
            'installed_at': _iso_now(),
            'registry': 'system',
            'node': _node_version()}
    if not data:
        os.makedirs(paths.home_dir(ver), exist_ok=True)
    write_meta(ver, meta)
    if progress:
        try:
            progress(f'已收纳系统实例 dsh v{ver}（{("沿用原数据目录: " + data) if data else "使用独立新数据目录"}），可立即启动。')
        except Exception:
            pass
    return meta
