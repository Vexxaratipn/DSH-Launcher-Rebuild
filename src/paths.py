"""DSH Launcher Rebuild - 统一路径 (与 UI/核心模块共享的唯一路径源)

运行时目录等可用环境变量覆盖，便于测试：
  DSLR_ROOT    = 重建项目根(默认本文件所在 app 目录的上级)
  DSLR_RUNTIME = 覆盖 runtime 目录
打包后(exe)默认 runtime 在 exe 同目录；不可写时退回 %LOCALAPPDATA% 下的
DSH Launcher Rebuild/runtime。
"""
import os
import sys

_APP = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_APP)


def repo_root():
    """项目根（测试用 DSLR_ROOT 覆盖）。"""
    return os.environ.get('DSLR_ROOT') or _REPO


def app_dir():
    return _APP


def _writable_probe(path):
    """目录可写性探测：创建/写入/删除一个探针文件，全部成功视为可写。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, '.wprobe')
        with open(probe, 'w') as f:
            f.write('1')
        os.remove(probe)
        return True
    except Exception:
        return False


def runtime_dir():
    """优先 env DSLR_RUNTIME；打包后在 exe 同目录；开发时在项目根；
    均不可写时退回 LOCALAPPDATA（仍不行则返回 exe 侧候选，由调用方报错）。"""
    env = (os.environ.get('DSLR_RUNTIME') or '').strip().strip('"')
    if env:
        return env
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = _REPO
    cand = os.path.join(base, 'runtime')
    if _writable_probe(cand):
        return cand
    lap = os.environ.get('LOCALAPPDATA')
    if lap:
        fallback = os.path.join(lap, 'DSH Launcher Rebuild', 'runtime')
        if _writable_probe(fallback):
            return fallback
    return cand


def read_config_key(key, default=None):
    """轻量读 config.json 单键（避免依赖 core_versions，避免循环）。"""
    import json
    try:
        with open(config_file(), 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        v = cfg.get(key)
        if v not in (None, ''):
            return v
        return default
    except Exception:
        return default


def versions_dir():
    return os.path.join(runtime_dir(), 'versions')


def version_dir(version):
    """单版本目录：package(内核npm安装区) + home(该版本数据目录，即 DSH_HOME) + meta.json"""
    return os.path.join(versions_dir(), _san(version))


def package_dir(version):
    return os.path.join(version_dir(version), 'package')


def home_dir(version):
    return os.path.join(version_dir(version), 'home')


def overlay_path(version):
    """该版本 overlay 文件：插件禁用补丁存放处。"""
    return os.path.join(runtime_dir(), 'overlays', _san(version) + '.patch.yml')


def backups_root():
    """全局备份根目录：设置 config['backup_root'](非空) 优先，否则 runtime\\backups。"""
    custom = read_config_key('backup_root')
    if custom:
        try:
            os.makedirs(custom, exist_ok=True)
            return custom
        except Exception:
            pass
    return os.path.join(runtime_dir(), 'backups')


def version_backup_root(version):
    """该版本自定义备份根（meta.backup_root，实例设置）；未设置返回 ''。"""
    return (read_meta_light(version).get('backup_root') or '').strip()


def backups_for(version):
    """某版本快照目录：<备份根>/<版本>/snap-*。备份根 = 实例自设(meta.backup_root)
    优先，否则全局（config.backup_root 或 runtime\\backups）。"""
    root = version_backup_root(version) or backups_root()
    try:
        os.makedirs(root, exist_ok=True)
    except Exception:
        pass
    return os.path.join(root, _san(version))


def version_port(version):
    """该版本启动/健康端口：meta.port（实例设置）> config.port > 3080。"""
    try:
        v = int(read_meta_light(version).get('port') or 0)
        if 1 <= v <= 65535:
            return v
    except Exception:
        pass
    try:
        v = int(read_config_key('port') or 3080)
        if 1 <= v <= 65535:
            return v
    except Exception:
        pass
    return 3080


def version_keep(version):
    """该版本备份保留份数：meta.keep（实例设置）> config.keep > 3。"""
    try:
        k = int(read_meta_light(version).get('keep') or 0)
        if 1 <= k <= 30:
            return k
    except Exception:
        pass
    try:
        k = int(read_config_key('keep') or 3)
        if 1 <= k <= 30:
            return k
    except Exception:
        pass
    return 3


def logs_dir():
    return os.path.join(runtime_dir(), 'logs')


def npm_cache_dir():
    return os.path.join(runtime_dir(), 'npm-cache')


def tmp_dir():
    return os.path.join(runtime_dir(), 'tmp')


def config_file():
    return os.path.join(runtime_dir(), 'config.json')


def meta_file(version):
    return os.path.join(version_dir(version), 'meta.json')


def read_meta_light(version):
    """读取版本 meta（不存在/损坏返回 {}）。独立实现避免与 core_versions 循环依赖。"""
    import json
    try:
        with open(meta_file(version), 'r', encoding='utf-8') as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def data_home(version):
    """该版本实际数据目录(DSH_HOME)：外部实例(meta.data)沿用其原数据目录，
    否则用本目录独立 home。"""
    data = (read_meta_light(version).get('data') or '').strip()
    if data:
        return data
    return home_dir(version)


def version_cli(version):
    """该版本 dsh CLI(node 入口)绝对路径（运行时用）。

    便携优先规则：
    * 常规安装(install_mode 为空/'local'/'local-copy' 且非 external)：内核位于本
      runtime versions\\<版本>\\package 内 → 一律返回按“当前位置”计算的路径
      dsh_cli_abs()。meta.cli 里即使残留旧机器/旧位置的绝对路径也被忽略。
    * 相对记录(meta.cli 以相对 runtime 形式存储)：local 语义相同，直接按当前
      runtime 展开校验；失效也回退 dsh_cli_abs()。
    * 外部/全局实例(meta.external / install_mode=global)：内核在 runtime 外，
      meta.cli 原样返回（调用方负责存在性校验与报错）。
    """
    meta = read_meta_light(version)
    mode = str(meta.get('install_mode') or '').strip().lower()
    is_local = (not meta.get('external')) and mode in ('', 'local', 'local-copy', 'localcopy')
    if is_local:
        return dsh_cli_abs(version)
    cli = str(meta.get('cli') or '').strip()
    if not cli:
        return dsh_cli_abs(version)
    if os.path.isabs(cli):
        return cli
    abs_cli = os.path.normpath(os.path.join(runtime_dir(), cli))
    if os.path.isfile(abs_cli):
        return abs_cli
    return dsh_cli_abs(version)


def cli_rel_record(abs_cli):
    """把(旧机器/旧位置的)绝对 cli 转成相对 runtime 的记录；不在 runtime 内返回 ''。"""
    rt = runtime_dir()
    try:
        rel = os.path.relpath(abs_cli, rt)
    except ValueError:
        return ''
    if rel.startswith('..'):
        return ''
    return os.path.normpath(rel)


def rewrite_cli_relative(version):
    """首次列出版本时把 meta 里残留的旧绝对 cli 归一化为相对记录（或清除）。"""
    meta = read_meta_light(version)
    if not meta:
        return
    mode = str(meta.get('install_mode') or '').strip().lower()
    if meta.get('external') or mode == 'global':
        return  # 外部/全局：cli 语义原样
    cli = str(meta.get('cli') or '').strip()
    if not cli:
        return
    if not os.path.isabs(cli):
        return
    rel = cli_rel_record(cli)
    if not rel:
        return
    if rel == cli:  # 已是相对记录
        return
    if rel == os.path.normpath(os.path.join('versions', _san(version), 'package',
                                            'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js')):
        # 记录与“当前布局自动推导”等价 → 直接清除旧绝对记录
        meta.pop('cli', None)
    else:
        meta['cli'] = rel.replace('/', os.sep)
    # 原子写回
    import json
    tmp = meta_file(version) + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, meta_file(version))
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def ensure_dirs():
    for d in (runtime_dir(), versions_dir(), backups_root(), logs_dir(),
              npm_cache_dir(), tmp_dir(), os.path.join(runtime_dir(), 'overlays')):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


def _san(version):
    v = str(version or 'unknown').strip().replace('/', '_').replace('\\', '_')
    return v or 'unknown'


def bin_shim(version):
    """该版本内 dsh 可执行 shim(.cmd 优先) —— npm --prefix 安装后位于
    package/node_modules/.bin 下。"""
    base = os.path.join(package_dir(version), 'node_modules', '.bin')
    for cand in ('dsh.cmd', 'dsh.ps1', 'dsh'):
        p = os.path.join(base, cand)
        if os.path.isfile(p):
            return p
    return os.path.join(base, 'dsh.cmd')


def dsh_cli_abs(version):
    """直接指向包内 node 入口（不经 shim）：node <pkg>/lib/bin.js"""
    return os.path.join(package_dir(version), 'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js')


_TOOL_CFG_KEYS = {
    'pnpm': 'pnpm_tool',
    'node': 'node_tool',
}


def tools_dir():
    """捆绑的便携工具根：<runtime>\\tools（node / pnpm …）。"""
    return os.path.join(runtime_dir(), 'tools')


def _tool_cfg(kind):
    """读取配置的工具位置并归一化为绝对路径；未配置返回 ''。"""
    key = _TOOL_CFG_KEYS.get(kind)
    if not key:
        return ''
    v = (read_config_key(key) or '').strip().strip('"')
    if not v:
        return ''
    if os.path.isabs(v):
        return v
    return os.path.normpath(os.path.join(runtime_dir(), v))


def _tool_dir(kind, bundled_dir):
    """生效的工具目录：配置位置(文件则取其所在目录)存在则用之；否则默认捆绑目录。"""
    p = _tool_cfg(kind)
    if p:
        if os.path.isfile(p):
            p = os.path.dirname(p)
        if os.path.isdir(p):
            return p
    return bundled_dir


def node_dir():
    """生效的 node 安装目录（配置优先，缺省 runtime\\tools\\node）。"""
    return _tool_dir('node', os.path.join(tools_dir(), 'node'))


def node_exe():
    """生效的 node.exe：配置可直接指向 node.exe，否则 <node目录>\\node.exe。"""
    p = _tool_cfg('node')
    if p and os.path.isfile(p):
        return p
    return os.path.join(node_dir(), 'node.exe')


def npm_exe():
    """生效的 npm.cmd：node 目录下的 npm.cmd（不存在返回 ''，由调用方回退 PATH）。"""
    c = os.path.join(node_dir(), 'npm.cmd')
    if os.path.isfile(c):
        return c
    return ''


def pnpm_dir():
    """生效的 pnpm 目录（配置优先，缺省 runtime\\tools\\pnpm）。"""
    return _tool_dir('pnpm', os.path.join(tools_dir(), 'pnpm'))


def pnpm_exe():
    """生效的 pnpm.exe：配置可直接指向 pnpm.exe，否则 <pnpm目录>\\pnpm.exe。"""
    p = _tool_cfg('pnpm')
    if p and os.path.isfile(p):
        return p
    return os.path.join(pnpm_dir(), 'pnpm.exe')


def tool_bin_dirs():
    """需要放进子进程 PATH 前缀的目录（仅返回实际存在的）。"""
    out = []
    for d in (node_dir(), pnpm_dir()):
        if os.path.isdir(d):
            out.append(d)
    return out


def tool_path_env(base_path=''):
    """给子进程的 PATH：捆绑工具目录前置 + 原 PATH。"""
    prefix = ';'.join(tool_bin_dirs())
    if prefix and base_path:
        return prefix + ';' + base_path
    if prefix:
        return prefix
    return base_path


def tool_status():
    """当前生效工具状态（供设置页展示/校验）。"""
    ne = node_exe()
    pe = pnpm_exe()
    return {
        'node': ne,
        'node_ok': os.path.isfile(ne),
        'pnpm': pe,
        'pnpm_ok': os.path.isfile(pe),
        'npm': npm_exe(),
    }


def scan_tool_candidates():
    """扫描 runtime 目录树（深度 ≤3、跳过缓存/黑名单目录）寻找可用的
    node.exe / pnpm.exe 候选，供设置页“自动扫描工具”使用。"""
    rt = runtime_dir()
    hits = {'node': [], 'pnpm': []}
    if not os.path.isdir(rt):
        return hits
    skip = frozenset({'tmp', 'overlays', 'versions', '.dsh', 'build', '__pycache__',
                      'logs', 'dist', 'npm-cache', 'node_modules', '.git', 'backups'})
    seen = set()

    def exe_names(root):
        for f in ('node.exe', 'pnpm.exe'):
            p = os.path.join(root, f)
            if os.path.isfile(p) and p not in seen:
                seen.add(p)
                hits['node' if f == 'node.exe' else 'pnpm'].append(p)

    def walk(root, depth=0):
        if depth > 3 or not os.path.isdir(root):
            return
        try:
            entries = sorted(os.listdir(root))
        except Exception:
            return
        exe_names(root)
        if depth >= 3:
            return
        for name in entries:
            if name in skip or name.startswith('.'):
                continue
            sub = os.path.join(root, name)
            if not os.path.isdir(sub) or os.path.islink(sub):
                continue
            walk(sub, depth + 1)

    walk(rt, 0)
    hits['node'] = sorted(hits['node'])
    hits['pnpm'] = sorted(hits['pnpm'])
    return hits


def dot_dsh_dir():
    """默认数据底座目录（无显式 DSH_HOME 时的数据根）：runtime\\.dsh。"""
    return os.path.join(runtime_dir(), '.dsh')


def store_rel_if_inside(p):
    """把路径转成“相对 runtime”的存储值：在 runtime 目录内 → 相对；目录外 → 原样(绝对)。"""
    p = str(p or '').strip().strip('"')
    if not p:
        return ''
    if not os.path.isabs(p):
        p = os.path.normpath(os.path.join(runtime_dir(), p))
    try:
        rel = os.path.relpath(p, runtime_dir())
        if rel != '..' and not rel.startswith('..' + os.sep):
            return rel
    except Exception:
        pass
    return p
