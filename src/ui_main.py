"""DSH Launcher Rebuild - 主窗口 (S5/S6 UI)

MainApp(root, bus)：
  * root 由外部 main.py 创建；bus 为 core_runner.Bus（queue + after 泵，事件在主线程派发）。
  * 菜单栏：版本 / 操作 / 工具 / 设置 / 帮助
  * 中部：版本 Treeview（版本|状态|标签|安装时间|大小），单击选中、双击启动
  * 大按钮行（对选中版本）：启动 / 无插件启动 / 停止 / 立即备份 / 恢复(加载备份) / 插件管理
    （高频操作都在按钮上，菜单不重复；打开数据目录/终端/运行中检测等进 工具 菜单）
  * 状态区：Progressbar(0-100；收到 -1 归零) + 状态文本 Label
  * 底部：LabelFrame 运行日志（tk.Text 深色只读，批量写入 + 行数裁剪 + 仅底部时自动滚动）
         + 状态栏两个独立 Label：左侧=健康状态（每 3s 后台探测），右侧=操作提示（互不覆盖）
  * 所有耗时 core 调用一律走 run_task()（bus.run 后台线程 + 'done'/'error' 事件回收），
    UI 线程不做 npm/robocopy/网络/进程等待。

bus 事件约定：
  log      : 日志行（批量写入日志区）
  progress : 数值(int/float/'NN'/'NN%') -> 进度条；-1 -> 归零；字符串 -> 状态文本 + 日志
  done     : run_task 后台函数正常返回后的结果（主线程回收）
  error    : run_task 后台函数抛出（CoreError 等）-> 统一记 '[错误] ...' 并解除忙态
  health   : 后台健康探测结果 (bool)
  hint     : 状态栏操作提示文本
  termline : 终端弹窗输出行（转发给已注册的终端窗口 sink）
"""
import os
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import paths
import core_versions as cv
import core_launch as cl
import core_backup as cb
import ui_dialogs

APP_TITLE = 'DeepSeek Harness 管理器（多版本）'
LOG_MAX = 600
HEALTH_POLL_SECS = 3.0
SOCK_TIMEOUT = 0.8
_GREEN = '#1a7f37'
_GRAY = '#6a737d'
_BLUE = '#0b5394'
_RED = '#c0392b'
_ORANGE = '#b45309'


def _icon_path():
    '''定位 Deepseek.ico：_MEIPASS -> exe 目录 -> 本 app 上级两级 -> ../../Deepseek.ico。'''
    cands = []
    mp = getattr(sys, '_MEIPASS', None)
    if mp:
        cands.append(os.path.join(mp, 'Deepseek.ico'))
    if getattr(sys, 'frozen', False):
        cands.append(os.path.join(os.path.dirname(sys.executable), 'Deepseek.ico'))
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in ('..', '..\\..'):
        cands.append(os.path.normpath(os.path.join(here, rel, 'Deepseek.ico')))
    cands.append(os.path.join(here, 'Deepseek.ico'))
    for c in cands:
        try:
            if os.path.isfile(c):
                return os.path.normpath(c)
        except Exception:
            continue
    return None


def _port_open(port, timeout=SOCK_TIMEOUT):
    '''纯 socket 探测 127.0.0.1:port 是否可连（健康状态用，比 HTTP 更轻）。'''
    try:
        s = socket.create_connection(('127.0.0.1', int(port)), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


class MainApp:
    def __init__(self, root, bus):
        self.root = root
        self.bus = bus
        self.busy = False
        self.sel_version = None
        self._task_hint = ''
        self._task_done = None
        self._line_count = 0
        self._log_pending = []
        self._flush_id = None
        self._health = None
        self._term_sinks = []
        self._refreshing = False
        self._ver_of = {}
        self._icons = _icon_path()
        self._port = 3080
        try:
            self._port = int(cv.load_config().get('port') or 3080)
        except Exception:
            pass
        root.title(APP_TITLE)
        try:
            root.tk.call('tk', 'scaling', 1.25)
        except Exception:
            pass
        self._apply_window_geometry()
        if self._icons:
            try:
                root.iconbitmap(self._icons)
            except Exception:
                pass
        self._build_menu()
        self._build_body()
        self._build_statusbar()
        bus.on('log', self._on_log)
        bus.on('progress', self._on_progress)
        bus.on('done', self._on_done)
        bus.on('error', self._on_error)
        bus.on('health', self._on_health)
        bus.on('hint', self._on_hint)
        bus.on('termline', self._on_termline)
        bus.on('versions', self._on_versions)
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        try:
            self.root.after(300, self.do_refresh)
        except Exception:
            pass
        threading.Thread(target=self._health_loop, daemon=True).start()

    def _apply_window_geometry(self):
        try:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            w = min(1080, max(780, int(sw * 0.88)))
            h = min(820, max(600, int(sh * 0.9)))
            self.root.geometry('%dx%d+%d+%d' % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 2)))
            self.root.minsize(760, 600)
        except Exception:
            return

    def _build_menu(self):
        mb = tk.Menu(self.root)
        m_ver = tk.Menu(mb, tearoff=0)
        m_ver.add_command(label='下载新版本…', command=self.do_download)
        m_ver.add_command(label='从本地导入…', command=self.do_import_local)
        m_ver.add_command(label='扫描系统已装实例…（免下载启用）', command=self.do_scan_system)
        m_ver.add_separator()
        m_ver.add_command(label='删除选中…', command=self.do_remove_version)
        m_ver.add_command(label='刷新列表', command=self.do_refresh)
        mb.add_cascade(label='版本', menu=m_ver)
        m_op = tk.Menu(mb, tearoff=0)
        m_op.add_command(label='实例设置…（端口/备份）', command=self.do_version_settings)
        m_op.add_command(label='查看启动日志', command=self.do_view_log)
        mb.add_cascade(label='操作', menu=m_op)
        m_tool = tk.Menu(mb, tearoff=0)
        m_tool.add_command(label='终端…', command=self.do_terminal)
        m_tool.add_command(label='打开数据目录', command=self.do_open_home)
        m_tool.add_command(label='打开 runtime 目录', command=self.do_open_runtime)
        m_tool.add_separator()
        m_tool.add_command(label='运行中版本检测', command=self.do_running_scan)
        mb.add_cascade(label='工具', menu=m_tool)
        m_set = tk.Menu(mb, tearoff=0)
        m_set.add_command(label='设置…', command=self.do_settings)
        mb.add_cascade(label='设置', menu=m_set)
        m_help = tk.Menu(mb, tearoff=0)
        m_help.add_command(label='关于', command=self.do_about)
        mb.add_cascade(label='帮助', menu=m_help)
        self.root.config(menu=mb)

    def _build_body(self):
        pad = {'padx': 4, 'pady': 3}
        head = ttk.Frame(self.root)
        head.pack(fill='x', padx=8, pady=(6, 0))
        ttk.Label(head, text='DeepSeek Harness 多版本启动器',
                  font=('Microsoft YaHei UI', 13, 'bold')).pack(side='left')
        ttk.Label(head, text='各版本独立内核+数据目录，可并行安装、随时切换',
                  foreground=_GRAY).pack(side='left', padx=12)
        lf = ttk.LabelFrame(self.root, text='已安装版本（单击选择 / 双击启动）', padding=(6, 2))
        lf.pack(fill='both', expand=True, padx=8, pady=(4, 2))
        cols = ('ver', 'state', 'tag', 'installed', 'size')
        self.tree = ttk.Treeview(lf, columns=cols, show='headings', selectmode='browse')
        for c, h, w in (('ver', '版本', 210), ('state', '状态', 110), ('tag', '标签', 80),
                        ('installed', '安装时间', 150), ('size', '大小(MB)', 90)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor='w')
        vsb = ttk.Scrollbar(lf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True, padx=0, pady=2)
        self.tree.tag_configure('run', foreground=_GREEN)
        self.tree.tag_configure('idle', foreground='#333333')
        self.tree.tag_configure('broken', foreground=_RED)
        self.tree.tag_configure('external', foreground=_BLUE)
        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        self.tree.bind('<Double-1>', lambda e: self.do_start(False))
        btns = ttk.Frame(self.root)
        btns.pack(fill='x', padx=8)
        for c in range(3):
            btns.grid_columnconfigure(c, weight=1, uniform='big')
        st = ttk.Style()
        try:
            st.configure('Accent.TButton', foreground='#ffffff', background=_BLUE)
            st.map('Accent.TButton', background=[('disabled', '#a9b7c6'),
                                                 ('active', '#1a4fa0'),
                                                 ('!active', _BLUE)])
        except Exception:
            pass

        def b(text, cmd, accent=False):
            return ttk.Button(btns, text=text, command=cmd,
                              style='Accent.TButton' if accent else 'TButton')

        self.btn_start = b('启动', lambda: self.do_start(False), accent=True)
        self.btn_start.grid(row=0, column=0, sticky='we', **pad)
        self.btn_noplugins = b('无插件启动', lambda: self.do_start(True))
        self.btn_noplugins.grid(row=0, column=1, sticky='we', **pad)
        self.btn_stop = b('停止', self.do_stop)
        self.btn_stop.grid(row=0, column=2, sticky='we', **pad)
        self.btn_backup = b('立即备份', self.do_backup)
        self.btn_backup.grid(row=1, column=0, sticky='we', **pad)
        self.btn_restore = b('恢复(加载备份)', self.do_restore)
        self.btn_restore.grid(row=1, column=1, sticky='we', **pad)
        self.btn_plugins = b('插件管理', self.do_plugins)
        self.btn_plugins.grid(row=1, column=2, sticky='we', **pad)
        stat = ttk.Frame(self.root)
        stat.pack(fill='x', padx=8, pady=(0, 2))
        self.prog = ttk.Progressbar(stat, mode='determinate', maximum=100, value=0)
        self.prog.pack(side='left', fill='x', expand=True)
        self.status = ttk.Label(stat, text='就绪', anchor='w', foreground=_GRAY)
        self.status.pack(side='left', padx=(8, 0))
        logf = ttk.LabelFrame(self.root, text='运行日志 / Log', padding=(6, 2))
        logf.pack(fill='x', padx=8, pady=(0, 2))
        self.log = tk.Text(logf, wrap='word', state='disabled', height=9,
                           font=('Consolas', 9), background='#1e1e1e',
                           foreground='#dcdcdc', insertbackground='#dcdcdc',
                           relief='flat')
        lvsb = ttk.Scrollbar(logf, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=lvsb.set)
        lvsb.pack(side='right', fill='y')
        self.log.pack(fill='x', padx=0, pady=0)

    def _build_statusbar(self):
        bar = ttk.Frame(self.root, relief='sunken')
        bar.pack(fill='x', side='bottom', padx=0, pady=0)
        self.health_label = ttk.Label(bar, text='健康检测中…', foreground=_GRAY, padding=(8, 3))
        self.health_label.pack(side='left')
        self.hint_label = ttk.Label(bar, text='', foreground=_GRAY, padding=(8, 3), anchor='e')
        self.hint_label.pack(side='right', fill='x', expand=True)

    def run_task(self, label, fn, done=None):
        """后台执行 fn(progress_cb)；bus.run 保证 fn 抛出的 CoreError/异常转 'error' 事件。
done(result) 成功后在主线程调用（经 'done' 事件泵）。忙态时拒绝新任务。"""
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return
        self._task_done = done
        self._set_busy(True, label)

        def progress_cb(msg):
            self.bus.post('progress', msg)

        def fin(result):
            self.bus.post('done', result)

        self.bus.run(lambda: fn(progress_cb), done=fin)

    def _set_busy(self, on, label=''):
        self.busy = on
        self._task_hint = label if on else ''
        if on:
            try:
                self.prog.config(value=0)
                self.status.config(text=label + ' …', foreground=_BLUE)
            except Exception:
                pass
        else:
            try:
                self.prog.config(value=0)
                self.status.config(text='就绪', foreground=_GRAY)
            except Exception:
                pass
        self._set_buttons('normal' if not on else 'disabled')

    def _set_buttons(self, state):
        for w in (self.btn_start, self.btn_noplugins, self.btn_stop,
                  self.btn_backup, self.btn_restore, self.btn_plugins):
            try:
                w.config(state=state)
            except Exception:
                pass

    def _on_done(self, result):
        '''run_task 成功收尾（主线程）。'''
        try:
            cb_ = self._task_done
            self._task_done = None
            self._set_busy(False)
            if cb_:
                cb_(result)
                return
            return
        except Exception as e:
            self.post_log('[错误] 任务收尾失败: %s' % e)
            return

    def _on_error(self, exc):
        """fn 抛出（含 CoreError）—— 统一 '[错误] ...' 并解除忙态。"""
        self.post_log('[错误] %s' % str(exc))
        self._task_done = None
        if self.busy:
            self._set_busy(False)
        try:
            self._set_buttons('normal')
        except Exception:
            return

    def post_log(self, text, term=False):
        '''UI/后台线程都可调用（线程安全：仅入队）。term=True 走终端输出。'''
        if term:
            self.bus.post('termline', text)
            return
        self.bus.post('log', str(text))

    log_line = post_log

    def _on_log(self, text):
        self._log_pending.append(str(text))
        if self._flush_id is None:
            try:
                self._flush_id = self.root.after(30, self._flush_log)
            except Exception:
                self._flush_id = None
                return

    def _flush_log(self):
        self._flush_id = None
        lines = self._log_pending
        self._log_pending = []
        if not lines:
            return
        try:
            at_bottom = float(self.log.yview()[1]) >= 0.999
            self.log.config(state='normal')
            self.log.insert('end', '\n'.join(lines) + '\n')
            self._line_count += len(lines)
            if self._line_count > LOG_MAX * 2:
                cut = self._line_count - LOG_MAX
                self.log.delete('1.0', '%d.0' % (cut + 1))
                self._line_count -= cut
            if at_bottom:
                self.log.see('end')
            self.log.config(state='disabled')
        except Exception:
            if lines:
                self._log_pending = lines + self._log_pending
                return
            return

    def _on_progress(self, value):
        '''数值 -> 进度条(-1 归零)；字符串 -> 状态文本 + 日志行。'''
        try:
            if isinstance(value, bool):
                return
            if isinstance(value, (int, float)):
                v = int(value)
                self.prog.config(value=0 if v < 0 else min(100, max(0, v)))
                return
            s = str(value).strip()
            if not s:
                return
            if s.endswith('%'):
                if s[:-1].strip().lstrip('-').isdigit():
                    v = int(float(s[:-1]))
                    self.prog.config(value=0 if v < 0 else min(100, max(0, v)))
                    return
            if s.lstrip('-').isdigit():
                v = int(s)
                self.prog.config(value=0 if v < 0 else min(100, max(0, v)))
                return
            self.status.config(text=s[:160], foreground=_BLUE)
            self._on_log(s)
        except Exception:
            return

    def _on_hint(self, text):
        try:
            self.hint_label.config(text=str(text)[:200], foreground=_BLUE)
        except Exception:
            return

    def _on_health(self, ok):
        self._health = ok
        try:
            if ok:
                self.health_label.config(text='运行中（健康）', foreground=_GREEN)
                return
            self.health_label.config(text='未运行', foreground=_GRAY)
        except Exception:
            return

    def _health_loop(self):
        last = None
        while True:
            try:
                cfg = cv.load_config()
                port = int(cfg.get('port') or 3080)
                self._port = port
                ok = _port_open(port)
                if ok != last:
                    last = ok
                    self.bus.post('health', ok)
            except Exception:
                pass
            time.sleep(HEALTH_POLL_SECS)

    def refresh(self):
        self.do_refresh()

    def do_refresh(self):
        """后台读 list_installed + running_versions（PS 扫描），结果经 'versions' 事件回填。"""
        if self.busy or self._refreshing:
            return
        self._refreshing = True

        def work():
            try:
                run = set(cl.running_versions())
            except Exception:
                run = set()
            try:
                items = cv.list_installed()
            except Exception as e:
                items = []
                self.bus.post('log', '[列表] 读取失败: %s' % e)
            self.bus.post('versions', (run, items))

        threading.Thread(target=work, daemon=True).start()

    def _on_versions(self, payload):
        self._refreshing = False
        try:
            run, items = payload
        except Exception:
            return
        sel_old = self.sel_version
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._ver_of = {}
        for m in items:
            ver = m.get('version') or '?'
            key = ver
            running = ver in run
            if running:
                state = '● 运行中'
                tag = 'run'
            elif not m.get('has_bin'):
                state = '内核缺失'
                tag = 'broken'
            else:
                state = '空闲'
                tag = 'idle'
            marks = []
            if m.get('external'):
                marks.append('外部实例')
                tag = tag if running else 'external'
            elif m.get('install_mode') == 'global':
                marks.append('全局共存')
            label = ver + ('  [' + '/'.join(marks) + ']' if marks else '')
            installed = (m.get('installed_at') or '')[:16].replace('T', ' ')
            size = m.get('size_mb')
            iid = self.tree.insert('', 'end',
                                   values=(label, state, m.get('tag') or '', installed,
                                           '%.1f' % size if isinstance(size, (int, float)) else ''),
                                   tags=(tag,))
            self._ver_of[iid] = key
        first = self.tree.get_children()
        if first:
            target = None
            for iid in first:
                if self._ver_of.get(iid) == sel_old:
                    target = iid
                    break
            if target is None:
                target = first[0]
            self.tree.selection_set(target)
            self.tree.focus(target)
            ver = self._ver_of.get(target)
            if ver:
                self.sel_version = ver
        if not items:
            self._set_hint('暂无已安装版本：菜单「版本 → 下载新版本… / 从本地导入…」开始')

    def _set_hint(self, text, color=_GRAY):
        try:
            self.hint_label.config(text=text[:220], foreground=color)
        except Exception:
            return

    def _on_select(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        ver = self._ver_of.get(sel[0])
        if ver:
            self.sel_version = ver
            self._set_hint('已选择 %s（回车=启动，双击=启动）' % ver, _BLUE)

    def _require_version(self):
        if not self.sel_version:
            messagebox.showwarning('需要选择版本', '请先在列表中单击选择一个版本。')
            return False
        return True

    def do_start(self, no_plugins=False):
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return
        if not self._require_version():
            return
        ver = self.sel_version
        port = paths.version_port(ver)

        def work(pr):
            try:
                others = [v for v in cl.running_versions() if v != ver]
            except Exception:
                others = []
            if others:
                self.post_log(f'[启动] 提示：{", ".join(others)!s} 正在运行（本实例端口 {port!s} 被占时会自动换端口）')
            r = cl.launch(ver, no_plugins=no_plugins, port=port, timeout_s=45, progress=pr)
            if r.get('ok'):
                if r.get('error') == 'already-running':
                    self.post_log('[启动] 该版本已在运行：%s' % r.get('health_url'))
                    return r
                url = r.get('health_url') or ''
                used = r.get('port') or port
                self.post_log('[启动] 启动成功：%s（端口 %d；日志 %s）' % (url, used, r.get('log')))
                if url:
                    try:
                        import webbrowser
                        webbrowser.open(url)
                    except Exception:
                        pass
                    return r
                return r
            self.post_log('[启动] 失败。错误：\n%s' % (r.get('error') or '未知'))
            if r.get('log_tail'):
                self.post_log('[启动] 日志尾部：\n%s' % r['log_tail'])
            return r

        self.run_task('启动 %s' % ver, work,
                      done=lambda r: self.do_refresh() if r and r.get('ok') else None)

    def do_stop(self):
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return
        if not self._require_version():
            return
        ver = self.sel_version

        def work(pr):
            pr('正在停止 %s …' % ver)
            r = cl.stop(ver, port=paths.version_port(ver))
            self.post_log(f'[停止] {ver!s}：{r.get("detail")!s}')
            return r

        self.run_task('停止 %s' % ver, work, done=lambda r: self.do_refresh())

    def do_version_settings(self):
        if not self._require_version():
            return
        import ui_dialogs
        ui_dialogs.VersionSettingsDialog(self, self.sel_version)

    def do_backup(self):
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return
        if not self._require_version():
            return
        ver = self.sel_version

        def work(pr):
            r = cb.backup(ver, kind='manual', full=False, progress=pr)
            self.post_log('[备份] 完成: %s' % r.get('dir'))
            return r

        self.run_task('备份 %s' % ver, work)

    def do_restore(self):
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return
        if not self._require_version():
            return
        ui_dialogs.backups_dialog(self, self.sel_version)

    def do_download(self):
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return
        ui_dialogs.download_dialog(self)

    def do_import_local(self):
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return
        ui_dialogs.import_dialog(self)

    def do_remove_version(self):
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return
        if not self._require_version():
            return
        ver = self.sel_version
        if not messagebox.askyesno('删除版本', '将删除版本 %s 的完整目录（内核 package + 数据 home + meta）。\n'
                                              '说明：若该版本正在运行会先自动停止；runtime\\backups 下的备份不会删除。\n\n'
                                              '继续？' % ver):
            return

        def work(pr):
            try:
                r = cl.stop(ver, port=self._port)
                if r.get('stopped'):
                    pr('已停止该版本进程')
            except Exception:
                pass
            pr('删除版本目录…')
            cv.remove_version(ver)
            self.post_log('[版本] 已删除 %s' % ver)
            return True

        self.run_task('删除 %s' % ver, work,
                      done=lambda r: (setattr(self, 'sel_version', None), self.do_refresh()))

    def do_open_home(self):
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return
        if not self._require_version():
            return
        d = paths.data_home(self.sel_version)
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        if os.path.isdir(d):
            try:
                os.startfile(d)
            except Exception as e:
                self.post_log(f'[错误] 无法打开目录 {d!s}: {e!s}')
            return
        messagebox.showerror('目录不可用', '无法访问数据目录:\n%s' % d)

    def do_open_runtime(self):
        try:
            os.makedirs(paths.runtime_dir(), exist_ok=True)
            os.startfile(paths.runtime_dir())
        except Exception as e:
            self.post_log('[错误] %s' % e)

    def do_view_log(self):
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return
        if not self._require_version():
            return
        ui_dialogs.logs_dialog(self, self.sel_version)

    def do_settings(self):
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return
        ui_dialogs.settings_dialog(self)

    def do_plugins(self):
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return
        if not self._require_version():
            return
        ui_dialogs.plugins_dialog(self, self.sel_version)

    def do_terminal(self):
        ui_dialogs.terminal_dialog(self, self.sel_version)

    def do_running_scan(self):
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return

        def work(pr):
            pr('扫描运行中的 node 进程…')
            vs = cl.running_versions()
            if vs:
                self.post_log('[运行中] %s' % ', '.join(vs))
            else:
                self.post_log('[运行中] 本启动器管理的版本均未运行')
            return vs

        self.run_task('运行中版本检测', work, done=lambda r: self.do_refresh())

    def do_scan_system(self):
        if self.busy:
            self.post_log('[!] 已有任务在执行，请稍候')
            return
        if not hasattr(cv, 'scan_system_instances'):
            self.post_log('[提示] 当前 core_versions 未提供系统实例扫描（scan_system_instances 已移除），请直接使用「下载新版本」或「从本地导入」。')
            return
        ui_dialogs.SystemInstancesDialog(self)

    def adopt_from_system(self, inst):
        '''收纳系统已装实例为可管理版本（引用式，不复制内核）。'''
        if not hasattr(cv, 'adopt_external'):
            self.post_log('[提示] 当前 core_versions 未提供系统实例收纳（adopt_external 已移除）。')
            return

        def work(pr):
            m = cv.adopt_external(inst, progress=pr)
            self.post_log('[系统实例] 已启用 dsh v%s（引用式收纳，未复制内核）' % m.get('version'))
            return m

        self.run_task('启用系统实例 v%s' % inst.get('version'), work,
                      done=lambda r: self.do_refresh())

    def do_download_impl(self, ver, registry, mode='', prefix=''):
        def work(pr):
            m = cv.install_version(ver, registry=registry, progress=pr, mode=mode, prefix=prefix)
            self.post_log(f'[下载] 安装完成: v{m.get("version")!s} (tag={m.get("tag") or ""!s}, {m.get("install_mode") or "local"!s})')
            return m

        self.run_task('下载版本 %s' % ver, work, done=lambda r: self.do_refresh())

    def do_import_impl(self, path):
        def work(pr):
            m = cv.import_local(path, progress=pr)
            self.post_log(f'[导入] 完成: v{m.get("version")!s} (tag={m.get("tag")!s})')
            return m

        self.run_task('导入 %s' % os.path.basename(path), work,
                      done=lambda r: self.do_refresh())

    def do_about(self):
        messagebox.showinfo('关于', 'DeepSeek Harness 管理器（多版本）\n\n'
                            '· 独立下载/管理多个 @deepseek-ai/dsh 版本（npm 源可配）\n'
                            '· 每版本独立内核目录 + 独立数据目录(DSH_HOME) + 独立插件启停\n'
                            '· 启动健康检测 / 备份恢复 / 内置终端 / 插件管理\n\n'
                            'runtime 目录: %s' % paths.runtime_dir())

    def register_term(self, dialog):
        if dialog not in self._term_sinks:
            self._term_sinks.append(dialog)

    def unregister_term(self, dialog):
        try:
            self._term_sinks.remove(dialog)
        except ValueError:
            pass

    def _on_termline(self, text):
        for sink in list(self._term_sinks):
            try:
                if sink.win.winfo_exists():
                    sink.append_out(str(text))
            except Exception:
                continue

    def on_close(self):
        if self.busy:
            messagebox.showinfo('任务进行中', '当前操作尚未完成，请稍候再退出。')
            return
        self.root.destroy()
