"""S2 core_launch — 版本启动 / 停止 / 健康检查 / 日志读取

依赖：仅 stdlib + app.paths（唯一路径源）。可被 app 包内模块引用，也可被把 app 目录
放入 sys.path 的脚本直接 import（双模式 import）。

设计要点 / 限制：
- 不 import core_versions（避免 node_cmd 耦合）——node 探测本地内联 shutil.which；
- 不 import core_plugins（S4 并行开发，避免循环依赖/未就绪）——"无插件启动"的
  全禁 overlay 文件由插件模块/UI 在启动前写入 paths.overlay_path(ver)，本模块只做
  校验：文件存在且含 `disabled: true` 才追加 --patch；否则跳过 --patch 并提示。
- 子进程用 `node <cli> web --no-open` 直跑包入口（不经 .bin shim），cwd=cli 所在目录；
  cli 与数据目录(DSH_HOME)按 meta 解析：外部/全局实例(meta.cli / meta.data 非空)沿用其
  原内核/数据路径，常规安装退回 runtime versions 布局（paths.version_cli / data_home）。
- 进程归属判定一律使用 meta 记录的 cli/data 与 runtime 布局路径做**字符串锚点**匹配
  （不做文件存在性门槛）。
- 安全约定：绝不 taskkill /im node.exe 全杀；stop/running_versions/port_owner 只认
  "已知版本"（meta 记录 cli/data 或 runtime versions 目录）对应的 node 进程。
- 跨机搬迁修复：启动任何本地版本前先执行 fix_links.repair_home（把 dsh 启动自愈
  要求但被复制工具展开成真实目录的 junction 镜像副本清除，由 dsh 自行重建）。
"""
import base64
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

import paths
import fix_links

__all__ = ['CoreError', 'healthy', 'launch', 'stop', 'running_versions',
           'latest_log', 'read_log_tail', 'port_owner', '_ps',
           'ensure_fallback_ready']

CREATE_NO_WINDOW = 0x08000000
_UA = 'DSH-Launcher-Rebuild/core_launch'
_PS_EXE = None

_ENUM_PS = ("$rows = Get-CimInstance Win32_Process | ForEach-Object { "
            "[string]$_.ProcessId + [char]1 + [string]$_.ParentProcessId + [char]1 + "
            "$_.Name + [char]1 + $_.CommandLine }; Write-Output $rows")


class CoreError(Exception):
    """核心模块业务错误（GUI 直接 str(e) 展示）。"""


def _san(version):
    """日志文件名安全化版本名（与 paths._san 对常规名一致；另兜底路径注入）。"""
    v = str(version or 'unknown').strip()
    for ch in '<>:"/\\|?*':
        v = v.replace(ch, '_')
    if not v.strip(' .') or v in ('.', '..'):
        return 'unknown'
    return v


def _cb(progress, msg):
    try:
        if callable(progress):
            progress(msg)
    except Exception:
        pass


def _find_node():
    """定位 node.exe：捆绑便携 node(runtime\\tools\\node) 优先，再 PATH，再常见安装位置。"""
    bundled = paths.node_exe()
    if os.path.isfile(bundled):
        return bundled
    p = shutil.which('node')
    if p:
        return p
    for base in (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)'),
                 os.environ.get('LOCALAPPDATA'), 'C:\\Program Files',
                 'C:\\Program Files (x86)'):
        if not base:
            continue
        cand = os.path.join(base, 'Nodejs', 'node.exe')
        if os.path.isfile(cand):
            return cand
    return None


def _socket_open(port, timeout):
    """纯 TCP 探测 127.0.0.1:port 是否可连（快速失败）。"""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(('127.0.0.1', int(port)))
        s.close()
        return True
    except Exception:
        return False
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass


def healthy(port, url_path='/'):
    """HTTP GET http://127.0.0.1:<port><url_path> —— 任何 HTTP 响应(≥100)都视为"服务在线"。

    放宽为"能收到 HTTP 应答即 True"：dsh 即使返回 302/401/404（token 墙、重定向等）
    也说明服务已监听并工作；仅网络层失败（拒绝连接/超时）为 False。
    先做 ~0.25s 的 socket 连测，再 GET；走代理无关 opener，仅查 loopback。
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if not 1 <= port <= 65535:
        return False
    if not _socket_open(port, 0.25):
        return False
    if not url_path.startswith('/'):
        url_path = '/' + url_path
    url = 'http://127.0.0.1:%d%s' % (port, url_path)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    try:
        with opener.open(req, timeout=0.9) as resp:
            return resp.status >= 100
    except urllib.error.HTTPError as e:
        return e.code >= 100
    except Exception:
        return False


def _log_path_for(ver, ts=None):
    """按规范生成日志路径：logs_dir/<san(ver)>-start-<YYYYmmdd-HHMMSS>.log"""
    if ts is None:
        ts = time.strftime('%Y%m%d-%H%M%S')
    return os.path.join(paths.logs_dir(), '%s-start-%s.log' % (_san(ver), ts))


_URL_TOKEN_RE = re.compile(r'https?://127\.0\.0\.1:\d+[^\s"\'<>]*')


def _url_with_token(log_path, port):
    """从启动日志提取带 ?token= 的完整访问 URL（dsh web 鉴权用）。"""
    if not log_path or not os.path.isfile(log_path):
        return ''
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except Exception:
        return ''
    hits = _URL_TOKEN_RE.findall(text)
    pref = 'http://127.0.0.1:%d' % int(port)
    for u in hits:
        if '?token=' in u and u.startswith(pref):
            return u
    for u in hits:
        if '?token=' in u:
            return u
    return ''


def _final_web_url(log_path, port, fallback_url):
    """决定最终对外访问 URL：web_url 配置 > 日志 token URL > fallback。"""
    try:
        override = (paths.read_config_key('web_url') or '').strip()
        if override.startswith('http://') or override.startswith('https://'):
            return override
    except Exception:
        pass
    tok = _url_with_token(log_path, port)
    if tok:
        return tok
    return fallback_url


def _overlay_has_disabled(overlay_path):
    """overlay 是否"有效全禁"：文件存在且包含 disabled:true 条目。"""
    try:
        with open(overlay_path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
        return bool(re.search(r'disabled\s*:\s*(true|yes|on)', text, re.IGNORECASE))
    except Exception:
        return False


def _version_cmdline_owner_of(ver, cmd_low):
    """cmd 是否属于该版本（锚点：meta.cli / 版本目录 / data_home）。"""
    needles = []
    try:
        cli = os.path.normpath(paths.version_cli(ver)).lower()
        if cli:
            needles.append(cli)
        vd = (paths.version_dir(ver) + os.sep).lower()
        if vd:
            needles.append(vd)
        dh = paths.data_home(ver).lower()
        if dh:
            needles.append(dh)
    except Exception:
        return False
    return any(n and n in cmd_low for n in needles)


def ensure_fallback_ready(ver):
    """启动前自愈 dsh 托管 junction 镜像（跨机搬迁后复制工具会把 junction
    展开成真实目录，dsh 启动会报错拒绝；此处清掉"与本地包重复"的真实目录副本）。

    :return: {'repaired': int, 'detail': str}
    """
    try:
        home = paths.data_home(ver)
        pkg_nm = os.path.join(paths.package_dir(ver), 'node_modules')
        if not os.path.isdir(pkg_nm):
            return {'repaired': 0, 'detail': 'no local package'}
        if not fix_links.mirror_needs_repair(home, pkg_nm):
            return {'repaired': 0, 'detail': 'ok'}
        res = fix_links.repair_home(home, pkg_nm)
        n = res['shared'] + sum(res['profiles'].values())
        return {'repaired': n, 'detail': 'removed %d expanded mirror dir(s); dsh will rebuild links at boot' % n}
    except Exception as e:
        return {'repaired': -1, 'detail': str(e)}


def launch(ver, *, no_plugins=False, port=None, timeout_s=45, progress=None,
           wait_ok=True, auto_port=True):
    """启动该版本 dsh web（显式 --port），快速健康检查。

    - 端口已被本版本占用 → ok=True, error='already-running'（不重复启动）。
    - 端口被其它程序占用：auto_port=True 自动顺延找空闲端口（最多 20 个）。
    - 进程在健康前退出 → ok=False（error 附日志尾部）。
    - 返回 dict {ok, pid, log, health_url, error, log_tail, port}。
    """
    ver = str(ver or '').strip()
    if not ver:
        raise CoreError('launch: 缺少版本号')
    try:
        port = int(port) if port is not None else int(paths.version_port(ver))
    except (TypeError, ValueError):
        port = 3080
    try:
        timeout_s = max(5, min(600, int(timeout_s)))
    except (TypeError, ValueError):
        timeout_s = 45
    paths.ensure_dirs()

    # ---- 跨机搬迁自愈：先恢复 junction 镜像，避免 dsh 启动被真实目录挡住 ----
    try:
        fix = ensure_fallback_ready(ver)
        if fix['repaired'] > 0:
            _cb(progress, '[依赖链接修复] %s' % fix['detail'])
    except Exception:
        pass

    # 端口选择
    cands = [port]
    if auto_port:
        cands += [p for p in range(port + 1, port + 21) if p <= 65535]
    use_port = None
    for p in cands:
        if not healthy(p):
            use_port = p
            break
        # 端口活着：是本版本自己？是则 already-running
        pid, _name, ocmd = _port_owner_info(p)
        if pid and _version_cmdline_owner_of(ver, (ocmd or '').lower()):
            final_url = _final_web_url(latest_log(ver), p, 'http://127.0.0.1:%d' % p)
            _cb(progress, '该版本已在运行：%s（不重复启动）' % final_url)
            return {'ok': True, 'error': 'already-running', 'port': p,
                    'health_url': final_url, 'log': latest_log(ver),
                    'log_tail': '', 'pid': pid}
        _cb(progress, '端口 %d 被其它程序占用（pid=%s），自动换下一个…' % (p, pid or '?'))
    if use_port is None:
        raise CoreError('端口 %d-%d 均被占用，请到「设置」改端口后重试。' % (cands[0], cands[-1]))
    if use_port != port:
        _cb(progress, '端口 %d 被占用 → 本次使用端口 %d' % (port, use_port))

    node = _find_node()
    if not node:
        raise CoreError('未找到 node：请在 runtime\\tools\\node 放入便携 node，或安装 Node.js 并加入 PATH')

    meta0 = paths.read_meta_light(ver)
    ext = bool(meta0.get('external')) or str(meta0.get('install_mode') or '').strip().lower() == 'global'
    if ext:
        mcli = str(meta0.get('cli') or '').strip()
        if not mcli or not os.path.isfile(mcli):
            raise CoreError('该版本 %s 的外部内核不可用：meta.cli 指向的文件不存在（%s）。\n'
                            '外部/全局共存实例内核不随目录走，请重新下载/导入/收纳该版本。' % (ver, mcli))
    cli = paths.version_cli(ver)
    if not os.path.isfile(cli):
        raise CoreError('该版本内核未安装或已损坏（缺少 %s）。\n请先在「版本 → 下载/导入/收纳」中准备 %s。' % (cli, ver))
    home = paths.data_home(ver)
    try:
        os.makedirs(home, exist_ok=True)
    except Exception:
        pass

    cmd = [node, cli, 'web', '--no-open', '--port', str(use_port)]
    if no_plugins:
        ov = paths.overlay_path(ver)
        if _overlay_has_disabled(ov):
            cmd += ['--patch', ov]
            _cb(progress, '无插件启动：应用全禁 overlay（--patch %s）' % ov)
        else:
            _cb(progress, '无插件启动：未找到有效全禁 overlay（%s 无 disabled:true 条目），本次跳过 --patch 按普通启动。' % ov)

    log = _log_path_for(ver)
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
    except Exception:
        pass
    try:
        fp = open(log, 'ab')
    except Exception as e:
        raise CoreError('无法创建日志文件 %s：%s' % (log, e))

    text = '[%s] DSH Launcher 启动 ver=%s port=%d no_plugins=%s\n  cmd: %s\n  env: DSH_HOME=%s\n' % (
        time.strftime('%Y-%m-%d %H:%M:%S'), ver, use_port, bool(no_plugins),
        ' '.join(cmd), home)
    try:
        fp.write(text.encode('utf-8', errors='replace'))
        fp.flush()
    except Exception:
        pass

    env = os.environ.copy()
    env['DSH_HOME'] = home
    env['npm_config_cache'] = paths.npm_cache_dir()
    try:
        env['PATH'] = paths.tool_path_env(env.get('PATH') or '')
    except Exception:
        pass

    _cb(progress, '正在启动 %s（端口 %d）…' % (ver, use_port))
    try:
        cwd = os.path.dirname(cli)
        proc = subprocess.Popen(cmd, stdout=fp, stderr=subprocess.STDOUT,
                                env=env, cwd=cwd, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        try:
            fp.close()
        except Exception:
            pass
        raise CoreError('启动子进程失败（node=%s）：%s' % (node, e))
    try:
        fp.close()
    except Exception:
        pass

    url = 'http://127.0.0.1:%d' % use_port
    if not wait_ok:
        _cb(progress, '进程已拉起（pid=%d，端口 %d，不等待健康）。日志：%s' % (proc.pid, use_port, log))
        return {'ok': True, 'port': use_port, 'health_url': url, 'log': log,
                'log_tail': '', 'error': '', 'pid': proc.pid}

    _cb(progress, '健康检查中（%s，最长 %ds；进程退出会立刻提示）…' % (url, int(timeout_s)))
    start = time.time()
    last_prog = start
    while True:
        if healthy(use_port):
            time.sleep(0.6)
            final_url = _final_web_url(log, use_port, url)
            _cb(progress, '启动成功：%s' % final_url)
            return {'ok': True, 'port': use_port, 'health_url': final_url,
                    'log': log, 'log_tail': '', 'error': '', 'pid': proc.pid}
        rc = proc.poll()
        if rc is not None:
            tail = _tail_file(log, 60)
            low = tail.lower()
            hint = ''
            if 'eaddrinuse' in low or 'address already in use' in low:
                hint = '\n（端口冲突：请先停止占用端口的程序，或到「设置」改端口后重试）'
            error = '进程在健康检查前退出（exit=%s）%s' % (rc, hint)
            error += '。日志尾部：\n' + tail if tail else '。日志文件：' + log
            _cb(progress, '启动失败（exit=%s）%s' % (rc, hint))
            return {'ok': False, 'port': use_port, 'health_url': url, 'log': log,
                    'log_tail': tail, 'error': error, 'pid': proc.pid}
        now = time.time()
        if now - start >= timeout_s:
            tail = _tail_file(log, 60)
            error = '健康检查超时（%ds 内 %s 未就绪）。' % (int(timeout_s), url)
            if proc.poll() is None:
                error += '进程仍在运行（pid=%s）；首次启动可能正在初始化 profile 依赖，可稍后在「操作 → 查看启动日志」观察，或先点「停止」。' % proc.pid
            if tail:
                error += '。日志尾部：\n' + tail
            _cb(progress, '健康检查超时（pid=%s 仍在运行）' % proc.pid)
            return {'ok': False, 'port': use_port, 'health_url': url, 'log': log,
                    'log_tail': tail, 'error': error, 'pid': proc.pid}
        if now - last_prog >= 5:
            last_prog = now
            _cb(progress, '…已等待 %d 秒（进程仍存活，剩余上限 %d 秒）' % (
                int(now - start), int(timeout_s - (now - start))))
        time.sleep(0.4)


def _ps(argv_list, timeout=20):
    """在隐藏窗口的 powershell 中执行脚本片段。

    argv_list: str 或 str 列表（按行拼接为一段 PS 脚本）；
    内部用 -EncodedCommand(UTF-16LE base64) 规避引号/中文转义问题。
    返回 (rc, stdout)；基础设施失败抛 CoreError。
    """
    global _PS_EXE
    if _PS_EXE is None:
        root = os.environ.get('SystemRoot') or 'C:\\Windows'
        cands = [os.path.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
                 os.path.join(root, 'SysWOW64', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
                 shutil.which('powershell') or '']
        _PS_EXE = next((c for c in cands if c and os.path.isfile(c)), '')
    if isinstance(argv_list, (str, bytes)):
        argv_list = [argv_list]
    script = '\n'.join(str(x) for x in argv_list)
    b64 = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    cmd = [_PS_EXE, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
           '-EncodedCommand', b64]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=timeout,
                            creationflags=CREATE_NO_WINDOW)
        return cp.returncode, cp.stdout or ''
    except subprocess.TimeoutExpired:
        raise CoreError('PowerShell 调用超时（>%ss）：%s' % (timeout, str(argv_list)[:120]))
    except Exception as e:
        raise CoreError('PowerShell 调用失败：%s' % e)


def _list_all_procs():
    """返回 [{pid, ppid, name, cmd}]（全进程快照）；失败返回 []。"""
    try:
        rc, out = _ps([_ENUM_PS])
        if rc != 0:
            return []
        rows = []
        for line in out.splitlines():
            if not line:
                continue
            parts = line.split('\x01', 3)
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except (TypeError, ValueError):
                continue
            rows.append({'pid': pid, 'ppid': ppid,
                         'name': parts[2] if len(parts) > 2 else '',
                         'cmd': parts[3] if len(parts) > 3 else ''})
        return rows
    except Exception:
        return []


def _with_descendants(rows, start_pids):
    """把 start_pids 连同其全部后代（按快照 ParentProcessId）合并返回。"""
    result = set(start_pids)
    stack = list(start_pids)
    while stack:
        cur = stack.pop()
        for r in rows:
            if r['ppid'] == cur and r['pid'] not in result:
                result.add(r['pid'])
                stack.append(r['pid'])
    return sorted(result)


def _stop_ids(ids):
    """对给定 pid 列表逐个 Stop-Process -Force。返回 (stopped_any, detail)。"""
    if not ids:
        return False, '未指定进程'
    lines = ['$errs = New-Object System.Collections.ArrayList', '$ok = 0']
    for i in ids:
        lines.append("try { Stop-Process -Id %d -Force -ErrorAction Stop; $ok++ } catch { $m = $_.Exception.Message; if ($m -match 'find a process|找不到|not running|未在运行') { $ok++ } else { [void]$errs.Add('%d: ' + $m) } }" % (i, i))
    lines.append("Write-Output ('STOPPED=' + $ok)")
    lines.append("if ($errs.Count -gt 0) { Write-Output ('FAILED=' + ($errs -join ' | ')) }")
    stopped = 0
    failed = ''
    try:
        rc, out = _ps(lines)
        for ln in (out or '').splitlines():
            if ln.startswith('STOPPED='):
                try:
                    stopped = int(ln[8:])
                except (TypeError, ValueError):
                    stopped = 0
            elif ln.startswith('FAILED='):
                failed = ln[7:]
    except CoreError as e:
        return False, str(e)
    if stopped > 0:
        return True, '已停止 %d 个进程（pid=%s）' % (stopped, ','.join(map(str, ids)))
    msg = '未停止任何进程'
    if failed:
        msg += '（可能权限不足）：' + failed
    return False, msg


def _port_owner_info(port):
    """Get-NetTCPConnection 查端口占用者；返回 (pid|None, name, cmdline)。"""
    script = ("$owner=''; $name=''; $cmdline=''\n"
              "try {\n"
              "  $c = Get-NetTCPConnection -LocalPort %d -ErrorAction SilentlyContinue | Where-Object { $_.State -eq 'Listen' } | Select-Object -First 1\n"
              "  if ($null -eq $c) { $c = Get-NetTCPConnection -LocalPort %d -ErrorAction SilentlyContinue | Select-Object -First 1 }\n"
              "  if ($c -and $c.OwningProcess) {\n"
              "    $owner = [string]$c.OwningProcess\n"
              "    $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $c.OwningProcess) -ErrorAction SilentlyContinue\n"
              "    if ($p) { $name = [string]$p.Name; $cmdline = [string]$p.CommandLine }\n"
              "  }\n"
              "} catch {}\n"
              "Write-Output ('OWNER=' + $owner)\n"
              "Write-Output ('NAME=' + $name)\n"
              "Write-Output ('CMD=' + $cmdline)") % (port, port)
    owner = name = cmdline = ''
    try:
        rc, out = _ps([script])
        for ln in (out or '').splitlines():
            if ln.startswith('OWNER='):
                owner = ln[6:]
            elif ln.startswith('NAME='):
                name = ln[5:]
            elif ln.startswith('CMD='):
                cmdline = ln[4:]
    except CoreError:
        pass
    try:
        pid = int(owner) if owner else None
    except (TypeError, ValueError):
        pid = None
    return pid, name, cmdline


def _version_needles(ver):
    needles = []
    try:
        cli = os.path.normpath(paths.version_cli(ver)).lower()
        if cli:
            needles.append(cli)
        vd = (paths.version_dir(ver) + os.sep).lower()
        if vd:
            needles.append(vd)
        dh = paths.data_home(ver).lower()
        if dh:
            needles.append(dh)
    except Exception:
        pass
    return needles


def _version_for_cmdline(cmdline, keys):
    low = (cmdline or '').lower()
    for key in keys:
        needles = _version_needles(key)
        if any(n and n in low for n in needles):
            return key
    return None


def stop(ver, port=None):
    """停止某版本（或某端口）的服务进程。

    优先级：
    1) ver 给出 → 匹配 node.exe 且 CommandLine 命中该版本锚点（meta.cli / meta.data /
       runtime 版本目录/内核路径，均为字符串匹配，不要求文件此刻存在）的进程
       （连同其子进程），逐个 Stop-Process -Force；
    2) 未匹配 / ver=None → Get-NetTCPConnection 找端口占用者：仅当占用者是 node.exe
       且可归属到本启动器的某个已知版本才停止；归属到其它版本或无法归属（系统 dsh
       等）→ 拒绝并说明 —— 绝不误杀。

    返回 {'stopped': bool, 'detail': str}。绝不 taskkill /im node.exe 全杀。
    """
    try:
        port = int(port) if port is not None else None
    except (TypeError, ValueError):
        port = None
    try:
        rows = _list_all_procs()
    except Exception:
        rows = []
    matched = []
    if ver:
        needles = _version_needles(ver)
        for r in rows:
            if (r.get('name') or '').lower() not in ('node.exe', 'node'):
                continue
            cmd = (r.get('cmd') or '').lower()
            if not any(n and n in cmd for n in needles):
                continue
            matched.append(r)
    if matched:
        ids = _with_descendants(rows, [r['pid'] for r in matched])
        ok, msg = _stop_ids(ids)
        return {'stopped': ok,
                'detail': '版本 %s：匹配到 %d 个 node 进程。%s' % (ver, len(matched), msg)}
    # 按端口归属
    owner_pid = owner_name = owner_cmd = ''
    if port:
        try:
            owner_pid, owner_name, owner_cmd = _port_owner_info(port)
        except Exception:
            owner_pid = owner_name = owner_cmd = ''
    if owner_pid is None or owner_pid == '':
        who = '版本 %s' % ver if ver else '进程'
        return {'stopped': False,
                'detail': '未发现运行中的%s；端口 %s 未被占用。' % (who, port)}
    if (str(owner_name) or '').lower() not in ('node.exe', 'node'):
        return {'stopped': False,
                'detail': '端口 %s 被非 node.exe 进程占用（pid=%s，name=%s），未停止。'
                          % (port, owner_pid, owner_name or '未知')}
    keys = [m.get('version') or name for name, m in _installed_version_keys()]
    owner_ver = _version_for_cmdline(owner_cmd, keys)
    if owner_ver is None:
        return {'stopped': False,
                'detail': '端口 %s 由 pid=%s 占用，但无法归属到本启动器的任何已知版本（可能为系统其他 dsh/服务），未停止。'
                          % (port, owner_pid)}
    if ver and owner_ver != str(ver).strip():
        return {'stopped': False,
                'detail': '版本 %s 未在运行；端口 %s 由本启动器的另一版本 %s（pid=%s）占用，未停止。如需停止请对该版本调用 stop(%s)。'
                          % (ver, port, owner_ver, owner_pid, owner_ver)}
    ids = _with_descendants(rows, [owner_pid])
    ok, msg = _stop_ids(ids)
    return {'stopped': ok,
            'detail': '按端口 %s 停止（版本 %s，pid=%s）：%s' % (port, owner_ver, owner_pid, msg)}


def running_versions():
    """返回当前正在运行的版本列表（读全进程快照，仅识别已知版本）。"""
    running = []
    try:
        keys = [m.get('version') or name for name, m in _installed_version_keys()]
        rows = _list_all_procs()
        for r in rows:
            if (r.get('name') or '').lower() != 'node.exe':
                continue
            v = _version_for_cmdline(r.get('cmd') or '', keys)
            if v is not None and v not in running:
                running.append(v)
    except Exception:
        pass
    return running


def _installed_version_keys():
    """迭代 versions/ 下所有带 meta 的版本 → [(目录名, meta)]。"""
    out = []
    base = paths.versions_dir()
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            meta = paths.read_meta_light(name)
            if meta:
                out.append((name, meta))
    return out


def latest_log(ver):
    """该版本最近一次启动日志路径（无则 ''）。"""
    d = paths.logs_dir()
    prefix = _san(ver) + '-start-'
    best = ''
    try:
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.startswith(prefix) and fn.endswith('.log'):
                    best = os.path.join(d, fn)
    except Exception:
        pass
    return best


def read_log_tail(ver, n=200):
    """读取该版本最近日志尾部；返回 (path, text)。"""
    path = latest_log(ver)
    if not path:
        return '', ''
    return path, _tail_file(path, n)


def _tail_file(path, n):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        return ''.join(lines[-n:])
    except Exception:
        return ''


def port_owner(port):
    """端口占用者 (pid, name, cmdline)；未占用返回 (None, '', '')。"""
    try:
        pid, name, cmd = _port_owner_info(port)
        return pid, name, cmd
    except Exception:
        return None, '', ''
