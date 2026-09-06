'''DSH Launcher Rebuild - S4 每版本插件管理（核心模块，纯 stdlib + paths）。

职责（一切以「该版本的数据目录 + 该版本的内核 dsh CLI」为准；meta.data / meta.cli
      可指向 runtime 外的外部实例或全局共存内核）：
  * discover()                读取 home\\profiles\\web\\package.json 的 dsh.profile.bundles 清单，
                              结合用户 overlay + 插件市场 state.json 得出每插件的禁用状态与 core 标记；
  * overlay_disabled()        读用户 overlay（runtime\\overlays\\<ver>.patch.yml）被禁用的 id 集合；
  * set_overlay()             写入/清空 overlay（空集合 -> 删除文件）；
  * ensure_no_plugins_overlay()  生成「无插件启动」用的全禁 overlay（无可禁插件返回 None）；
  * install() / uninstall()   调用该版本内核的 <dsh cli> plugin --profile web add|remove <spec>；
  * catalog()                 在线拉取 awesome-dsh-plugin 插件目录（失败返回 []，绝不抛）；
  * npm_env()                 env 辅助（os.environ + npm_config_cache 指向 runtime 缓存）。

规则：
  - 只 import stdlib + 本目录 paths；不 import core_launch / core_versions（避免循环依赖）。
  - 所有子进程带 creationflags=0x08000000 (CREATE_NO_WINDOW)，禁止弹黑窗。
  - 错误统一抛本模块顶部定义的 CoreError（GUI 只负责展示 str(e)）。
  - 测试：DSLR_RUNTIME 指向 _test 内目录，绝不触碰真实 ~/.dsh / 3080。
'''
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.request

try:
    from . import paths
except ImportError:
    import paths

CREATE_NO_WINDOW = 134217728
CLI_TIMEOUT_S = 900
_CATALOG_URL = 'https://awesome-dsh-plugin.com/plugins.json'
_CATALOG_TIMEOUT_S = 10
_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) DSH-Launcher-Rebuild/0.1'
CORE_BUNDLES = {
    '@deepseek-ai/dsh-base',
    '@deepseek-ai/dsh-web-app'}


class CoreError(Exception):
    '''核心插件模块错误；GUI 直接展示 str(e)。'''


def profile_web(home):
    '''dsh web profile 目录（bundles 清单与 node_modules 所在）。'''
    return os.path.join(home, 'profiles', 'web')


def npm_env():
    '''返回可传给 npm/pnpm 子进程的环境副本：npm_config_cache + 捆绑工具 PATH 前缀
(dsh 内部调用 node/npm/pnpm 也走 runtime\\tools) + 下载源自动探测结果
(registry+代理；直连优先、失败自动带 Windows/环境代理测镜像，结果缓存、前端不展示)。
探测不可用（异常/超时）时回退跟随设置源：auto→npmmirror。'''
    env = dict(os.environ)
    env['npm_config_cache'] = paths.npm_cache_dir()
    env['PATH'] = paths.tool_path_env(env.get('PATH') or '')
    try:
        from core_versions import working_registry, net_env
        url, proxy = working_registry()
        patch = net_env(url, proxy)
        env.update({k: v for k, v in patch.items() if v})
    except Exception:
        reg = (paths.read_config_key('registry') or 'auto').strip()
        if reg in ('npmjs', 'https://registry.npmjs.org/'):
            env['npm_config_registry'] = 'https://registry.npmjs.org/'
        elif reg.startswith('http'):
            env['npm_config_registry'] = reg
        else:
            env['npm_config_registry'] = 'https://registry.npmmirror.com/'
    return env


_ID_TO_PKG = {
    'dsh-market': 'dshmarket',
    'dshmarket': 'dshmarket',
    'better-sidebar': 'dsh-better-sidebar',
    'web-search-free': 'dsh-free-search',
    'free-search': 'dsh-free-search',
    'ui-skill-explorer': '@linxin666/dsh-client-ui-skill-explorer',
    'skill-explorer': '@linxin666/dsh-client-ui-skill-explorer',
    'cost-meter': 'dsh-cost-meter',
    'hot-reload': 'dsh-hot-reload',
    'find-dsh-plugin': 'dsh-find-plugin',
    'at-file': 'dsh-at-file',
    'browser': 'dsh-builtin-browser',
    'context': 'dsh-context'}


def _canonical_spec(spec):
    '''把用户输入的插件标识规范化：注册 id → npm 包名（返回 (spec, changed_note)）。'''
    s = str(spec or '').strip()
    key = s.lower()
    if key.startswith('@'):
        return (s, '')
    base = s.split('@', 1)[0].strip().lower() if not s.startswith('@') else s.lower()
    if base in _ID_TO_PKG and _ID_TO_PKG[base] != base:
        return (_ID_TO_PKG[base],
                f'检测到 {base!r} 是插件注册 id，其 npm 包名是 {_ID_TO_PKG[base]!r} —— 已自动改用包名安装。')
    return (s, '')


def _node_exe():
    '''探测 node 可执行文件。顺序：DSLR_NODE/DSH_NODE(测试覆盖) -> 捆绑便携 node
(runtime\\tools\\node，随目录走) -> PATH -> 常见安装位置。'''
    raw = (os.environ.get('DSLR_NODE') or os.environ.get('DSH_NODE') or '').strip().strip('"')
    if raw:
        if os.path.isfile(raw):
            return raw
        raise CoreError('环境变量 DSLR_NODE/DSH_NODE 指向的 node 不存在: %s' % raw)
    bundled = paths.node_exe()
    if os.path.isfile(bundled):
        return bundled
    try:
        found = shutil.which('node')
    except Exception:
        found = None
    if found:
        return found
    cands = []
    for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)'):
        pf = os.environ.get(key)
        if not pf:
            continue
        cands.append(os.path.join(pf, 'nodejs', 'node.exe'))
    lp = os.environ.get('LOCALAPPDATA')
    if lp:
        cands.append(os.path.join(lp, 'Programs', 'nodejs', 'node.exe'))
    for c in cands:
        if os.path.isfile(c):
            return c
    raise CoreError('未检测到 Node.js：请在 runtime\\tools\\node 放入便携 node，或安装 Node.js（https://nodejs.org）并加入 PATH。')


def _parse_overlay(text):
    '''解析 overlay yml 文本 -> (enabled_ids, disabled_ids)。跳过注释与空行。'''
    enabled, disabled = set(), set()
    cur_id, cur_disabled = None, False
    for raw in text.splitlines():
        st = raw.strip()
        if not st or st.startswith('#'):
            continue
        if st.startswith('- id:'):
            if cur_id is not None:
                (disabled if cur_disabled else enabled).add(cur_id)
            cur_id = st.split(':', 1)[1].strip()
            cur_disabled = False
            continue
        if not st.startswith('disabled:'):
            continue
        if cur_id is None:
            continue
        cur_disabled = st.split(':', 1)[1].strip().lower().startswith('true')
    if cur_id is not None:
        (disabled if cur_disabled else enabled).add(cur_id)
    return enabled, disabled


def _parse_overlay_disabled(path):
    '''读 overlay 文件 -> 被禁用的 id 集合（文件缺失/损坏返回空集）。'''
    try:
        with open(path, 'r', encoding='utf-8') as f:
            _, disabled = _parse_overlay(f.read())
        return disabled
    except Exception:
        return set()


def overlay_disabled(ver):
    '''该版本当前被禁用的插件 id 集合（用户 overlay 文件；不存在则为空集）。'''
    return _parse_overlay_disabled(paths.overlay_path(ver))


def _write_overlay(disabled_ids, path):
    '''把禁用 id 集合写成 overlay yml（不写启用项）。返回 True/False。'''
    ids = sorted({str(x) for x in (disabled_ids or []) if str(x).strip()})
    lines = [
        '# DSH Launcher Rebuild - 插件禁用 overlay（启动 dsh 时以 --patch 生效）',
        '# 由「插件管理 / 无插件启动」生成；每项：不加载该 id 的插件。',
    ]
    for pid in ids:
        lines.append('- id: %s' % pid)
        lines.append('  disabled: true')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        return True
    except Exception:
        return False


def set_overlay(ver, disabled_ids):
    '''写该版本的用户 overlay：disabled_ids 为要禁用的插件 id（list|set|None）。
空集合/空列表 -> 删除 overlay 文件（若存在）。
返回 overlay 文件路径（总是返回，删除场景同样返回该路径）。'''
    path = paths.overlay_path(ver)
    ids = {str(x) for x in (disabled_ids or []) if str(x).strip()}
    if not ids:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
        return path
    d = os.path.dirname(path)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    if not _write_overlay(ids, path):
        raise CoreError('无法写入插件 overlay: %s' % path)
    return path


def ensure_no_plugins_overlay(ver):
    '''「无插件启动」辅助：生成全禁 overlay —— 该版本所有非 core 且带 id 的插件全部禁用。
返回 overlay 路径；若没有任何可禁插件则返回 None（调用方此时不传 --patch）。

注意：会覆写该版本的用户 overlay 文件（paths.overlay_path(ver)）。
如需在本次启动结束后恢复原先的禁用状态，调用方应自行保存并在结束时空集调用
set_overlay(ver, []) 删除文件或写回原禁用集合。'''
    info = discover(ver)
    ids = []
    if info.get('ok'):
        for p in (info.get('plugins') or []):
            if p.get('core'):
                continue
            if not p.get('id'):
                continue
            ids.append(p['id'])
    if not ids:
        return None
    return set_overlay(ver, ids)


def _resolve_pkg_dir(profile, pkg):
    '''node_modules'''
    base = os.path.join(profile, 'node_modules')
    if pkg.startswith('@'):
        head, _, tail = pkg.partition('/')
        return os.path.join(base, head, tail)
    return os.path.join(base, pkg)


def _read_pkg_version(pkgdir):
    '''package.json'''
    try:
        with open(os.path.join(pkgdir, 'package.json'), 'r', encoding='utf-8') as f:
            return str(json.load(f).get('version', ''))
    except Exception:
        return ''


def _patch_entries(text):
    '''结构化解析插件包内 cordis.patch.yml：返回 [{id,name,disabled}]（跳过注释）。'''
    entries = []
    cur = None
    for raw in text.splitlines():
        st = raw.strip()
        if not st or st.startswith('#'):
            continue
        if st.startswith('- id:'):
            if cur is not None:
                entries.append(cur)
            cur = {
                'id': st.split(':', 1)[1].strip(),
                'name': '',
                'disabled': False,
            }
            continue
        if cur is not None and st.startswith('name:'):
            cur['name'] = st.split(':', 1)[1].strip().strip('\'"')
            continue
        if cur is None:
            continue
        if not st.startswith('disabled:'):
            continue
        cur['disabled'] = st.split(':', 1)[1].strip().lower().startswith('true')
    if cur is not None:
        entries.append(cur)
    return entries


def _plugin_id_for_package(pdir, pkg):
    '''从插件包自己的 cordis.patch.yml 找出注册 id：优先取 name 与包名一致的条目。'''
    try:
        with open(os.path.join(pdir, 'cordis.patch.yml'), 'r', encoding='utf-8') as f:
            text = f.read()
    except Exception:
        return ''
    entries = _patch_entries(text)
    for e in entries:
        if e['name'] == pkg and e['id']:
            return e['id']
    for e in entries:
        if e['id']:
            return e['id']
    return ''


def discover(ver):
    '''发现该版本插件清单（home = paths.data_home(ver)：meta.data 指向外部实例时即其数据目录，
否则为该版本独立 home；清单与实际数据目录一致）。

return dict {ok, error, home, profile, plugins:[...]}
plugins: [{package, version, id, core, enabled, note}]
  * 清单真相源 = <home>\\profiles\\web\\package.json 的 dsh.profile.bundles；
  * id 取插件包内 cordis.patch.yml 中 name==包名的 `- id: X` 条目；
  * 禁用来源 = 用户 overlay(overlay_path(ver)) 的 disabled ∪ .dsh-market\\state.json 的 disabled；
  * core 内核 bundle 锁定，不可禁用（id 为空、enabled 恒为 True）。'''
    home = paths.data_home(ver)
    out = {
        'ok': False,
        'error': '',
        'home': home,
        'profile': '',
        'plugins': [],
    }
    prof = profile_web(home)
    out['profile'] = prof
    pj = os.path.join(prof, 'package.json')
    if not os.path.isfile(pj):
        out['error'] = '未找到 dsh 配置文件: %s\n（该版本数据目录尚未初始化 dsh profile；请先启动一次该版本，生成 profiles/web 后再管理插件）' % pj
        return out
    try:
        with open(pj, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        out['error'] = f'读取 {pj} 失败: {e}'
        return out
    bundles = ((data.get('dsh') or {}).get('profile') or {}).get('bundles') or []
    disabled = overlay_disabled(ver)
    try:
        with open(os.path.join(prof, '.dsh-market', 'state.json'), 'r', encoding='utf-8') as f:
            st = json.load(f)
        disabled |= set(st.get('disabled') or [])
    except Exception:
        pass
    for pkg in bundles:
        pdir = _resolve_pkg_dir(prof, pkg)
        version = _read_pkg_version(pdir)
        core = pkg in CORE_BUNDLES
        pid = '' if core else _plugin_id_for_package(pdir, pkg)
        if core:
            enabled = True
            note = '核心，不可禁用'
        else:
            enabled = pid != '' and pid not in disabled
            note = '' if pid else '未声明禁用 id（只能通过终端卸载）'
        out['plugins'].append({
            'package': pkg,
            'version': version,
            'id': pid,
            'core': core,
            'enabled': enabled,
            'note': note,
        })
    out['ok'] = True
    return out


def _looks_running(text):
    '''粗略判断输出是否暗示 dsh 实例正在运行（仅失败时用于附加提示，非守卫）。'''
    head = (text or '')[:1500].lower()
    markers = ('already running', 'another instance', 'instance is running',
               '正在运行', '已在运行', '实例已', '端口被占用', '被占用',
               'address already in use', 'eaddrinuse', 'listen eacces')
    return any(m in head for m in markers)


def _manual_npm(ver, action, spec):
    '''生成手工 npm 命令提示（CLI 不支持/失败时附在错误信息里）。'''
    prof = profile_web(paths.data_home(ver))

    q = lambda s: '"%s"' % s
    if action == 'remove':
        return f'npm uninstall --prefix {q(prof)} {spec} --cache {q(paths.npm_cache_dir())}'
    return f'npm install --prefix {q(prof)} {spec} --cache {q(paths.npm_cache_dir())}'


def _build_cli_cmd(ver, action, spec):
    '''构造 (cmd_argv, env)：<node> <版本 dsh cli(paths.version_cli)> plugin --profile web add|remove <spec>。
内核不可用（meta.cli 指向的文件已缺失 / 本目录未装内核）抛 CoreError。'''
    cli = paths.version_cli(ver)
    if not os.path.isfile(cli):
        meta = paths.read_meta_light(ver)
        ext = (meta.get('cli') or '').strip()
        if ext:
            raise CoreError('该版本引用的外部内核缺失: %s\n（meta.cli 指向的文件不存在）请修复该版本配置或重新导入。' % ext)
        raise CoreError('该版本内核尚未安装：找不到 %s\n请先在「版本」中安装该版本内核后再管理插件。' % cli)
    node = _node_exe()
    cmd = [node, cli, 'plugin', '--profile', 'web', action, spec]
    env = npm_env()
    env['DSH_HOME'] = paths.data_home(ver)
    return (cmd, env)


def _run_capture(cmd, env, timeout, progress):
    '''运行子进程并流式收集输出。返回 (rc, tail_text)。超时/启动失败抛 CoreError。'''
    tail_lines = []
    deadline = time.monotonic() + timeout
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace',
                                env=env, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        raise CoreError(f'启动命令失败 ({cmd[0]}): {e}')
    q = queue.Queue()

    def _reader():
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        q.put(None)

    threading.Thread(target=_reader, daemon=True).start()
    while True:
        try:
            item = q.get(timeout=0.5)
        except queue.Empty:
            if time.monotonic() >= deadline:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise CoreError(f'dsh 插件操作超时（>{timeout}s），已终止进程。输出尾部:\n{"\n".join(tail_lines[-30:])}')
            continue
        if item is None:
            break
        tail_lines.append(item)
        if progress is not None:
            try:
                progress(item.rstrip('\r\n'))
            except Exception:
                pass
    try:
        rc = proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        rc = proc.wait()
    tail = '\n'.join(tail_lines[-200:])
    return (rc, tail)


def _change_plugin(ver, action, spec, progress, timeout=CLI_TIMEOUT_S):
    '''install/uninstall 共用：dsh plugin --profile web add|remove <spec>。'''
    if not spec or not str(spec).strip():
        raise CoreError('插件标识不能为空（例如 dshmarket、dsh-cost-meter 或 @scope/pkg@1.0.0）。')
    spec = str(spec).strip()
    if action == 'add':
        canon, note = _canonical_spec(spec)
        if note and canon != spec:
            spec = canon
            if progress is not None:
                try:
                    progress(note)
                except Exception:
                    pass
    cmd, env = _build_cli_cmd(ver, action, spec)
    if progress is not None:
        try:
            progress(f'执行: {"add" if action == "add" else "remove"} {spec}')
        except Exception:
            pass
    rc, tail = _run_capture(cmd, env, timeout, progress)
    if rc != 0:
        msg = tail.strip() or 'dsh plugin 命令异常退出，退出码 %s' % rc
        low = (tail + ' ' + spec).lower()
        if _looks_running(msg):
            msg += '\n[提示] 输出显示 dsh 实例可能正在运行；如确实在运行，请先停止该版本再重试。'
        if '404' in low or 'not found' in low or 'e404' in low or 'not in the npm registry' in low:
            msg += '\n[提示] 返回 404 = npm 源里没有这个包名。请确认你输入的是【npm 包名】（如 dshmarket / dsh-cost-meter / @scope/name），而不是插件【注册 id】（如 dsh-market）；也请确认镜像/网络可用（本启动器已默认走设置里的源，可在「设置 → npm 源」切换 npmmirror）。'
        msg += '\n若 CLI 不支持/执行失败，可手工执行: %s' % _manual_npm(ver, action, spec)
        raise CoreError(msg)
    return tail


def install(ver, spec, progress=None):
    '''安装插件到该版本 web profile：<node> <版本内核 cli> plugin --profile web add <spec>。

前置：该版本内核可用（paths.version_cli(ver)：meta.cli 外部内核优先，否则本目录安装的内核；
两者都缺失抛 CoreError 提示先装版本/修复 meta.cli）。
env = os.environ + DSH_HOME=<data_home> + npm_config_cache；输出流式收集，成功返回输出尾部文本；
rc!=0 抛 CoreError(尾部)。不做「运行中不可装」守卫（由 UI 层先提示）；
但失败输出若暗示 dsh 在运行会附加提示。timeout=900s。'''
    return _change_plugin(ver, 'add', spec, progress)


def uninstall(ver, spec, progress=None):
    '''从该版本 web profile 卸载插件：<node> <内核 cli> plugin --profile web remove <spec>。
其余同 install。'''
    return _change_plugin(ver, 'remove', spec, progress)


def catalog(ver=None):
    '''在线拉取插件目录 https://awesome-dsh-plugin.com/plugins.json（timeout 10s）。
成功返回条目列表（保持原字段；缺失 name/id/version/description 时尽力补全）；
任何失败返回 []（不抛）。ver 参数保留给契约，当前版本目录与版本无关。'''
    req = urllib.request.Request(_CATALOG_URL, headers={
        'User-Agent': _UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=_CATALOG_TIMEOUT_S) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
    except Exception:
        return []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get('plugins') or data.get('packages') or data.get('items') or []
        if not isinstance(items, list):
            return []
    else:
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        e = dict(it)
        name = e.get('name') or e.get('package') or e.get('pkg') or ''
        if not name:
            continue
        e.setdefault('name', name)
        e.setdefault('id', e.get('id') or name)
        e.setdefault('version', e.get('version') or '')
        e.setdefault('description', e.get('description') or '')
        out.append(e)
    return out
