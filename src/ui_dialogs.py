"""DSH Launcher Rebuild - UI 弹窗 (S5/S6)

提供（接收 main 实例以取 bus/root/当前版本）：
  download_dialog(main)         下载新版本（标签 latest/next/alpha + 全部版本可滚选择 + 源选择）
  import_dialog(main)           从本地 .tgz / 目录导入
  BackupsDialog / backups_dialog(main, ver)   备份列表 / 恢复 / 删除 / 检查并清理
  PluginsDialog / plugins_dialog(main, ver)   插件清单启停 / 安装 / 卸载 / 在线目录
  TerminalDialog / terminal_dialog(main, ver) 内置 cmd 终端
  logs_dialog(main, ver)        查看启动日志（尾部文本）
  settings_dialog(main)         设置（registry/port/keep 等 -> core_versions.save_config）
  SystemInstancesDialog(main)   扫描系统已装实例并收纳（免下载启用）

线程约定：
  * 慢 core 调用（npm/robocopy/网络/PS 扫描）一律经 main.run_task()（bus.run 后台线程，
    完成后 'done'/'error' 事件回收），或对话框内轻量线程 + _Pipe 回主线程刷新；
    绝不在 UI 线程执行 npm/robocopy/网络/进程等待。
  * 跨线程刷新 UI 统一走 _Pipe（queue + after 泵），worker 只 post，泵在主线程派发。
"""
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import paths
import core_versions as cv
import core_backup as cb
import core_plugins as cp

CREATE_NO_WINDOW = 134217728
_GREEN = '#1a7f37'
_GRAY = '#6a737d'
_BLUE = '#0b5394'
_RED = '#c0392b'


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


def _icon(win):
    '''给窗口设置程序图标（找不到图标文件时静默忽略）。'''
    try:
        p = _icon_path()
        if p:
            win.iconbitmap(p)
    except Exception:
        pass


def _center(win, w, h):
    '''把窗口居中并设定最小尺寸。'''
    try:
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry('%dx%d+%d+%d' % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 2)))
        win.minsize(max(420, w - 160), max(320, h - 160))
    except Exception:
        pass


def _make_win(main, title, w, h):
    '''以 main.root 为父创建顶层弹窗并居中。'''
    win = tk.Toplevel(main.root)
    try:
        _icon(win)
        win.title(title)
        win.transient(main.root)
        _center(win, w, h)
    except Exception:
        pass
    return win


class _Pipe:
    '''对话框跨线程事件管道：worker 线程 post(...)，主线程 after 泵调 handler(*args)。
窗口销毁后自动停止调度。'''

    def __init__(self, win, handler):
        self.win = win
        self.handler = handler
        self.q = queue.Queue()
        self._tick()

    def post(self, *args):
        try:
            self.q.put_nowait(args)
        except Exception:
            pass

    def _tick(self):
        try:
            if not self.win.winfo_exists():
                return
        except Exception:
            return
        n = 0
        while n < 300:
            try:
                args = self.q.get_nowait()
            except queue.Empty:
                break
            except Exception:
                continue
            n += 1
            try:
                self.handler(*args)
            except Exception:
                pass
        try:
            self.win.after(50, self._tick)
        except Exception:
            return


def download_dialog(main):
    '''下载新版本'''
    win = _make_win(main, '下载新版本', 640, 600)
    frm = ttk.Frame(win, padding=10)
    frm.pack(fill='both', expand=True)
    cfg = cv.load_config()
    r1 = ttk.Frame(frm)
    r1.pack(fill='x', pady=2)
    ttk.Label(r1, text='npm 源:').pack(side='left')
    cur_reg = cfg.get('registry') or 'auto'
    reg_vals = ['auto', 'npmjs', 'npmmirror']
    if str(cur_reg).startswith('http'):
        reg_vals.append(str(cur_reg))
    reg_var = tk.StringVar(value=cur_reg if str(cur_reg) in reg_vals else 'auto')
    reg = ttk.Combobox(r1, textvariable=reg_var, width=32, values=reg_vals)
    reg.pack(side='left', padx=6)
    ttk.Label(r1, text='auto=官方源失败自动切镜像；可输入自定义 URL', foreground=_GRAY).pack(side='left')
    r2 = ttk.Frame(frm)
    r2.pack(fill='x', pady=(8, 2))
    ttk.Label(r2, text='标签:').pack(side='left')
    tag_var = tk.StringVar(value=str(cfg.get('tag') or 'latest'))
    tag_lbl = ttk.Label(r2, text='', foreground=_GRAY)
    for t in ('latest', 'next', 'alpha'):
        ttk.Radiobutton(r2, text=t, value=t, variable=tag_var,
                        command=lambda: _reload_remote()).pack(side='left', padx=4)
    tag_lbl.pack(side='left', padx=8)
    r3 = ttk.Frame(frm)
    r3.pack(fill='x', pady=(6, 2))
    ttk.Label(r3, text='安装位置:').pack(side='left')
    mode_var = tk.StringVar(value=str(cfg.get('install_mode') or 'local'))
    ttk.Radiobutton(r3, text='独立目录（默认）', variable=mode_var, value='local').pack(side='left', padx=2)
    ttk.Radiobutton(r3, text='Node 全局共存（另装 dsh-<版本> 命令）', variable=mode_var, value='global').pack(side='left', padx=2)
    r3b = ttk.Frame(frm)
    r3b.pack(fill='x', pady=(0, 2))
    ttk.Label(r3b, text='npm 全局前缀:').pack(side='left')
    gpre = tk.StringVar()
    gen = ttk.Entry(r3b, textvariable=gpre)
    gen.pack(side='left', fill='x', expand=True, padx=6)
    ghint = ttk.Label(r3b, text='检测中…', foreground=_GRAY)
    ghint.pack(side='left')

    def _fill_prefix():
        p = ''
        try:
            if hasattr(cv, 'detect_global_prefix'):
                p = cv.detect_global_prefix()
        except Exception:
            p = ''
        gpre.set(p)
        ghint.config(text=('（自动检测，可改）' if p else '（未检测到，请手动填可写目录）'),
                     foreground=(_GREEN if p else _RED))

    threading.Thread(target=_fill_prefix, daemon=True).start()
    lf = ttk.LabelFrame(frm, text='全部版本（可滚选择；默认预选标签指向的版本）', padding=(4, 2))
    lf.pack(fill='both', expand=True, pady=6)
    lst = tk.Listbox(lf, font=('Consolas', 9), exportselection=False)
    sb = ttk.Scrollbar(lf, orient='vertical', command=lst.yview)
    lst.configure(yscrollcommand=sb.set)
    sb.pack(side='right', fill='y')
    lst.pack(side='left', fill='both', expand=True)
    hint = ttk.Label(frm, text='正在查询远端版本…', foreground=_BLUE)
    hint.pack(anchor='w', pady=(4, 0))
    bt = ttk.Frame(frm)
    bt.pack(fill='x', pady=(4, 0))
    btn_dl = ttk.Button(bt, text='下载并安装', state='disabled')
    btn_dl.pack(side='left')
    ttk.Button(bt, text='关闭', command=win.destroy).pack(side='right')
    pipe = _Pipe(win, lambda *a: _on_pipe(a))

    def _fetch(tag, registry_override):
        try:
            r = cv.list_remote(tag=tag, registry_override=registry_override)
        except Exception as e:
            r = {'error': str(e)}
        pipe.post('remote', r)

    def _reload_remote():
        tag = (tag_var.get() or 'latest').strip()
        reg_sel = (reg_var.get() or 'auto').strip() or None
        threading.Thread(target=_fetch, args=(tag, reg_sel), daemon=True).start()

    def _on_pipe(a):
        kind = a[0]
        if kind != 'remote':
            return
        r = a[1]
        lst.delete(0, 'end')
        if r.get('error'):
            hint.config(text='查询失败: ' + r['error'][:160], foreground=_RED)
            return
        tv = r.get('tag_ver') or ''
        vers = r.get('versions') or []
        for v in vers:
            lst.insert('end', v)
        tag_lbl.config(text=f'标签 {tag_var.get()} → {tv}' if tv else '（该标签无对应版本）',
                       foreground=_GRAY)
        if tv and tv in vers:
            lst.selection_clear(0, 'end')
            i = vers.index(tv)
            lst.see(i)
            lst.selection_set(i)
        hint.config(text='共 %d 个版本（按标签刷新）——双击列表或点「下载并安装」' % len(vers),
                    foreground=_GREEN)
        btn_dl.config(state='normal')

    def _dl():
        ver = ''
        sel = lst.curselection()
        if sel:
            ver = lst.get(sel[0])
        if not ver:
            ver = (tag_var.get() or 'latest').strip()
        if not ver:
            messagebox.showwarning('下载版本', '请先选择要下载的版本。', parent=win)
            return
        reg_sel = reg_var.get().strip()
        if reg_sel not in ('auto', 'npmjs', 'npmmirror') and not reg_sel.startswith('http'):
            reg_sel = 'auto'
        mode = mode_var.get() or 'local'
        prefix = gpre.get().strip().strip('"') if mode == 'global' else ''
        win.destroy()
        main.do_download_impl(ver, reg_sel, mode, prefix)

    _reload_remote()
    reg.bind('<<ComboboxSelected>>', lambda e: _reload_remote())
    lst.bind('<Double-1>', lambda e: _dl())
    btn_dl.config(command=_dl)


def import_dialog(main):
    '''从本地导入 dsh 内核'''
    win = _make_win(main, '从本地导入 dsh 内核', 600, 220)
    frm = ttk.Frame(win, padding=12)
    frm.pack(fill='both', expand=True)
    ttk.Label(frm, wraplength=560, justify='left',
              text='选择本地 .tgz 打包文件，或已解压且顶层含 package.json 的目录\n'
                   '（包名须为 @deepseek-ai/dsh，将按包内真实版本复制安装到 runtime\\versions\\<版本>）。'
              ).pack(anchor='w')
    path_var = tk.StringVar()
    ent = ttk.Entry(frm, textvariable=path_var)
    ent.pack(fill='x', pady=8)

    def pick_dir():
        '''选择内核目录（顶层含 package.json）'''
        d = filedialog.askdirectory(title='选择内核目录（顶层含 package.json）')
        if d:
            path_var.set(os.path.normpath(d))

    def pick_tgz():
        '''选择 .tgz'''
        f = filedialog.askopenfilename(title='选择 .tgz', filetypes=[
            ('npm 包', '*.tgz'),
            ('tar', '*.tar.gz'),
            ('全部', '*.*')])
        if f:
            path_var.set(os.path.normpath(f))

    def go():
        p = path_var.get().strip().strip('"')
        if not p or not os.path.exists(p):
            messagebox.showwarning('导入', '请选择存在的本地路径。', parent=win)
            return
        win.destroy()
        main.do_import_impl(p)

    bt = ttk.Frame(frm)
    bt.pack(fill='x')
    ttk.Button(bt, text='选择目录…', command=pick_dir).pack(side='left')
    ttk.Button(bt, text='选择 .tgz…', command=pick_tgz).pack(side='left', padx=6)
    ttk.Button(bt, text='导入', command=go).pack(side='left', padx=6)
    ttk.Button(bt, text='关闭', command=win.destroy).pack(side='right')


class BackupsDialog:
    '''备份列表（恢复）- %s'''

    def __init__(self, main, ver):
        '''加载备份（恢复）- %s'''
        self.main = main
        self.ver = ver
        self.win = _make_win(main, '加载备份（恢复）- %s' % ver, 780, 430)
        top = ttk.Frame(self.win, padding=6)
        top.pack(fill='x')
        ttk.Label(top, text='备份目录: %s' % paths.backups_for(ver),
                  foreground=_GRAY, wraplength=740, justify='left').pack(anchor='w')
        cols = ('idx', 'time', 'kind', 'state', 'dir')
        self.tree = ttk.Treeview(self.win, columns=cols, show='headings', selectmode='browse')
        for c, h, w in (('idx', '#', 44), ('time', '时间', 150), ('kind', '类型', 80),
                        ('state', '状态', 80), ('dir', '路径', 360)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor='w')
        self.tree.tag_configure('BAD', foreground=_RED)
        self.tree.tag_configure('GOOD', foreground=_GREEN)
        vsb = ttk.Scrollbar(self.win, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True, padx=6, pady=4)
        btns = ttk.Frame(self.win)
        btns.pack(fill='x', padx=6, pady=4)
        ttk.Button(btns, text='恢复选中', command=self._restore).pack(side='left')
        ttk.Button(btns, text='删除选中', command=self._delete_sel).pack(side='left', padx=6)
        ttk.Button(btns, text='立即备份', command=self._backup_now).pack(side='left', padx=6)
        ttk.Button(btns, text='检查并清理（仅保留健康 N 份）', command=self._check_prune).pack(side='left', padx=6)
        ttk.Button(btns, text='刷新', command=self._reload).pack(side='left', padx=6)
        ttk.Button(btns, text='关闭', command=self.win.destroy).pack(side='right')
        self.status = ttk.Label(self.win, text='读取备份列表…', foreground=_GRAY)
        self.status.pack(anchor='w', padx=8, pady=(0, 6))
        self._snaps = []
        self._reload()

    def _reload(self):
        pipe = _Pipe(self.win, self._on_pipe)

        def work():
            try:
                snaps = cb.list_backups(self.ver)
                return ('snaps', snaps)
            except Exception as e:
                return ('err', str(e))

        threading.Thread(target=lambda: pipe.post(*work()), daemon=True).start()

    def _on_pipe(self, *a):
        kind = a[0]
        if kind == 'err':
            self.status.config(text='读取失败: %s' % a[1], foreground=_RED)
            return
        if kind == 'snaps':
            self._fill(a[1])
            return

    def _fill(self, snaps):
        self._snaps = snaps
        for i in self.tree.get_children():
            self.tree.delete(i)
        for s in snaps:
            tag = s['state']
            self.tree.insert('', 'end', iid=str(s['idx']),
                             values=(s['idx'], s['time'], s['kind'], s['state'], s['dir']),
                             tags=(tag,))
        if snaps:
            self.tree.selection_set(str(snaps[0]['idx']))
            self.status.config(text='共 %d 份（GOOD/OK=健康可恢复；BAD 不可恢复）' % len(snaps),
                               foreground=_GREEN)
            return
        self.status.config(text='暂无备份。可点「立即备份」或主窗口「立即备份」。', foreground=_GRAY)

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            idx = int(sel[0])
            snaps = getattr(self, '_snaps', []) or []
            if not snaps:
                snaps = cb.list_backups(self.ver)
            return next((s for s in snaps if s['idx'] == idx), None)
        except Exception:
            return None

    def _restore(self):
        if self.main.busy:
            return
        s = self._selected()
        if not s:
            messagebox.showwarning('恢复', '请先选择一份备份。', parent=self.win)
            return
        if not s['healthy']:
            messagebox.showwarning('不可恢复',
                                   f'该备份未通过健康检查（{s.get("issues") or "BAD"}），不能恢复：\n{s["dir"]}',
                                   parent=self.win)
            return
        if not messagebox.askyesno('确认恢复',
                                   f'将用该备份覆盖版本 {self.ver} 的整个数据目录(home)：\n{s["dir"]}'
                                   '\n\n该版本须处于停止状态（运行中会自动拒绝）。确定恢复吗？',
                                   parent=self.win):
            return

        def work(pr):
            return cb.restore(self.ver, s['idx'], progress=pr)

        def done(r):
            self.status.config(text='恢复完成 ✓（%s）' % (r.get('hint') or ''), foreground=_GREEN)
            self._reload()

        self.main.run_task('恢复 %s' % self.ver, work, done)

    def _delete_sel(self):
        if self.main.busy:
            return
        s = self._selected()
        if not s:
            messagebox.showwarning('删除', '请先选择一份备份。', parent=self.win)
            return
        if not messagebox.askyesno('删除备份', '将永久删除该备份：\n%s\n\n确定？' % s['dir'],
                                   parent=self.win):
            return
        d = os.path.normpath(s['dir'])
        root = os.path.normpath(paths.backups_for(self.ver))
        if d == root or d.startswith(root + os.sep):
            pass
        else:
            messagebox.showerror('删除', '路径越界，已取消（%s）' % d, parent=self.win)
            return

        def work(pr):
            pr('删除备份目录…')
            shutil.rmtree(d, ignore_errors=True)
            return not os.path.isdir(d)

        def done(ok):
            self.status.config(text='已删除 ✓' if ok else '删除不完整（目录可能被占用）',
                               foreground=_GREEN if ok else _RED)
            self._reload()

        self.main.run_task('删除备份', work, done)

    def _backup_now(self):
        if self.main.busy:
            return

        def work(pr):
            return cb.backup(self.ver, kind='manual', full=False, progress=pr)

        def done(r):
            self.status.config(text='备份完成 ✓ %s' % r.get('dir'), foreground=_GREEN)
            self._reload()

        self.main.run_task('备份 %s' % self.ver, work, done)

    def _check_prune(self):
        if self.main.busy:
            return
        keep = cb.keep_count()
        if not messagebox.askyesno('检查并清理',
                                   '将检查该版本备份健康状态并清理：\n'
                                   '  · 不健康备份删除（若仅剩 1 份则保留）\n'
                                   '  · 健康备份只保留最新 %d 份（可在「设置」调整）\n\n继续？' % keep,
                                   parent=self.win):
            return

        def work(pr):
            return cb.check(self.ver, prune=True, progress=pr)

        def done(r):
            rm = r.get('removed') or []
            self.main.post_log('[备份] 检查结果：共 %d，健康 %d，不健康 %d，清理 %d 项' % (
                r.get('total', 0), r.get('healthy', 0), r.get('bad', 0), len(rm)))
            for p in rm:
                self.main.post_log('   - ' + os.path.basename(p))
            self.status.config(text='清理完成：移除 %d 项' % len(rm), foreground=_GREEN)
            self._reload()

        self.main.run_task('检查并清理备份', work, done)


def backups_dialog(main, ver):
    BackupsDialog(main, ver)


class PluginsDialog:
    '''插件管理 - %s'''

    def __init__(self, main, ver):
        '''插件管理 - %s'''
        self.main = main
        self.ver = ver
        self.win = _make_win(main, '插件管理 - %s' % ver, 740, 660)
        self.vars = []
        head = ttk.LabelFrame(self.win, text='说明', padding=(8, 4))
        head.pack(fill='x', padx=8, pady=(6, 2))
        ttk.Label(head, wraplength=700, justify='left', foreground='#333333',
                  text='勾选 = 启用；不勾选 = 下次启动该版本时禁用（写 overlay，--patch 生效）。核心 bundle 不可禁用。\n'
                       '安装/卸载调用该版本自带 dsh CLI：dsh plugin --profile web add|remove <spec>（需内核已装；dsh 运行时建议先停止）。'
                  ).pack(anchor='w')
        lf = ttk.LabelFrame(self.win, text='插件清单（勾选=启用）', padding=(4, 2))
        lf.pack(fill='both', expand=True, padx=8, pady=4)
        canvas = tk.Canvas(lf, highlightthickness=0)
        vsb = ttk.Scrollbar(lf, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor='nw')

        def cfg(e):
            canvas.configure(scrollregion=canvas.bbox('all'))

        inner.bind('<Configure>', cfg)
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(win_id, width=e.width))
        self.inner = inner
        self.msg = ttk.Label(self.win, text='', foreground=_GRAY)
        self.msg.pack(anchor='w', padx=10)
        inst = ttk.LabelFrame(self.win, text='安装 / 更新 / 卸载', padding=(8, 4))
        inst.pack(fill='x', padx=8, pady=(2, 4))
        row = ttk.Frame(inst)
        row.pack(fill='x')
        self.spec_var = tk.StringVar()
        ent = ttk.Entry(row, textvariable=self.spec_var)
        ent.pack(side='left', fill='x', expand=True, padx=(0, 6))
        ent.bind('<Return>', lambda e: self._install())
        ttk.Button(row, text='安装/更新', command=self._install).pack(side='left', padx=2)
        ttk.Button(row, text='卸载', command=self._uninstall).pack(side='left', padx=2)
        ttk.Button(row, text='浏览本地目录…', command=self._browse_local).pack(side='left', padx=2)
        ttk.Button(row, text='在线目录(需要网络)…', command=self._catalog).pack(side='left', padx=2)
        ttk.Label(inst, foreground=_GRAY, wraplength=700, justify='left',
                  text='填写【npm 包名】（不是插件注册 id）：如 dshmarket / dsh-cost-meter / @scope/name；'
                       '常见 id→包名：dsh-market→dshmarket、better-sidebar→dsh-better-sidebar、'
                       'web-search-free→dsh-free-search、ui-skill-explorer→@linxin666/dsh-client-ui-skill-explorer。\n'
                       '填了注册 id 会自动纠正；也支持 name@1.2.3 / github:user/repo / 本地目录（浏览）。卸载同理（填包名后点卸载）。'
                  ).pack(anchor='w', pady=(2, 0))
        btns = ttk.Frame(self.win)
        btns.pack(fill='x', padx=8, pady=(0, 6))
        ttk.Button(btns, text='全选', command=lambda: self._set_all(True)).pack(side='left')
        ttk.Button(btns, text='全不选', command=lambda: self._set_all(False)).pack(side='left', padx=6)
        ttk.Button(btns, text='保存（下次启动生效）', command=self._save).pack(side='left', padx=6)
        ttk.Button(btns, text='刷新', command=self._reload).pack(side='left', padx=6)
        ttk.Button(btns, text='关闭', command=self.win.destroy).pack(side='right')
        self._reload()

    def _reload(self):
        '''读取插件清单…'''
        self.msg.config(text='读取插件清单…', foreground=_GRAY)
        pipe = _Pipe(self.win, self._on_pipe)

        def work():
            try:
                info = cp.discover(self.ver)
                return ('info', info)
            except Exception as e:
                return ('err', str(e))

        threading.Thread(target=lambda: pipe.post(*work()), daemon=True).start()

    def _on_pipe(self, *a):
        kind = a[0]
        if kind == 'err':
            self.msg.config(text='读取失败: %s' % a[1], foreground=_RED)
            return
        if kind == 'catalog':
            items = a[1]
            if not items:
                self.msg.config(text='在线目录不可用（网络/接口失败）——可手动输入包名安装。', foreground=_RED)
                return
            self._catalog_picker(items)
            return
        info = a[1]
        for w in self.inner.winfo_children():
            w.destroy()
        self.vars = []
        if not info.get('ok'):
            ttk.Label(self.inner, text=info.get('error') or '未知错误', foreground=_RED,
                      wraplength=680, justify='left').pack(anchor='w', padx=8, pady=6)
            self.msg.config(text='内核 profile 尚未初始化：请先启动过该版本 dsh 一次。', foreground=_RED)
            return
        plist = info.get('plugins') or []
        if not plist:
            ttk.Label(self.inner, text='未发现任何插件（profile bundles 为空）。', foreground=_GRAY
                      ).pack(anchor='w', padx=8, pady=6)
            self.msg.config(text='无插件', foreground=_GRAY)
            return
        for p in plist:
            locked = bool(p['core']) or not p['id']
            var = tk.BooleanVar(value=True if locked else bool(p['enabled']))
            label = f"{p['package']}  {'v' + p['version'] if p['version'] else ''}"
            if p['id']:
                label += '   [id=%s]' % p['id']
            note = p.get('note') or ('核心，不可禁用' if p['core'] else '')
            if note:
                label += '   （%s）' % note
            chk = ttk.Checkbutton(self.inner, text=label, variable=var,
                                  state='disabled' if locked else 'normal')
            chk.pack(anchor='w', padx=6, pady=1)
            self.vars.append((p['package'], p['id'], p['core'], var))
        self.msg.config(text='共 %d 个 bundle（核心锁定不可禁用）' % len(plist), foreground=_GREEN)

    def _set_all(self, on):
        for pkg, pid, core, var in self.vars:
            if core:
                continue
            if not pid:
                continue
            var.set(on)

    def _save(self):
        try:
            disabled = []
            for pkg, pid, core, var in self.vars:
                if core:
                    continue
                if not pid:
                    continue
                if var.get():
                    continue
                disabled.append(pid)
            cp.set_overlay(self.ver, disabled)
            self.main.log_line('[插件] %s 禁用列表已保存（%d 个）：%s' % (
                self.ver, len(disabled), ', '.join(sorted(disabled)) or '(无)'))
            self.msg.config(text='已保存：%d 个插件将禁用，下次启动该版本时生效 ✓' % len(disabled),
                            foreground=_GREEN)
        except Exception as e:
            self.msg.config(text='保存失败: %s' % e, foreground=_RED)

    def _install(self):
        if self.main.busy:
            return
        spec = self.spec_var.get().strip()
        if not spec:
            messagebox.showwarning('安装插件', '请输入插件标识，例如：\n  dsh-cost-meter\n  @scope/pkg@1.0.0\n  github:user/repo',
                                   parent=self.win)
            return

        def work(pr):
            out = cp.install(self.ver, spec, progress=pr)
            return out

        def done(out):
            self.msg.config(text='安装/更新完成 ✓ 已刷新清单', foreground=_GREEN)
            self._reload()

        self.main.run_task('安装插件 %s' % spec, work, done)

    def _uninstall(self):
        if self.main.busy:
            return
        spec = self.spec_var.get().strip()
        if not spec:
            messagebox.showwarning('卸载插件', '请先在输入框填入要卸载的包名。', parent=self.win)
            return
        if not messagebox.askyesno('确认卸载',
                                   f'将从版本 {self.ver} 的 web profile 卸载：\n  {spec}\n\n确定？',
                                   parent=self.win):
            return

        def work(pr):
            return cp.uninstall(self.ver, spec, progress=pr)

        def done(out):
            self.msg.config(text='卸载完成 ✓ 已刷新清单', foreground=_GREEN)
            self._reload()

        self.main.run_task('卸载插件 %s' % spec, work, done)

    def _browse_local(self):
        '''选择本地插件目录（含 package.json 的包目录）'''
        d = filedialog.askdirectory(title='选择本地插件目录（含 package.json 的包目录）')
        if d:
            self.spec_var.set(os.path.normpath(d))

    def _catalog(self):
        '''在线目录获取中（≤10s）…'''
        self.msg.config(text='在线目录获取中（≤10s）…', foreground=_BLUE)
        pipe = _Pipe(self.win, self._on_pipe)

        def work():
            try:
                items = cp.catalog(self.ver)
            except Exception:
                items = []
            return ('catalog', items)

        threading.Thread(target=lambda: pipe.post(*work()), daemon=True).start()

    def _catalog_picker(self, items):
        '''在线插件目录（双击或选中后点「使用」填入输入框）'''
        win = tk.Toplevel(self.win)
        _icon(win)
        win.title('在线插件目录（双击或选中后点「使用」填入输入框）')
        _center(win, 720, 460)
        cols = ('name', 'version', 'desc')
        tree = ttk.Treeview(win, columns=cols, show='headings')
        for c, h, w in (('name', '包名', 260), ('version', '版本', 100), ('desc', '说明', 320)):
            tree.heading(c, text=h)
            tree.column(c, width=w, anchor='w')
        vsb = ttk.Scrollbar(win, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        tree.pack(fill='both', expand=True, padx=6, pady=6)
        for it in items[:600]:
            name = it.get('name') or it.get('id') or ''
            ver = it.get('version') or ''
            desc = (it.get('description') or '')[:100]
            tree.insert('', 'end', values=(name, ver, desc))
        tree.bind('<Double-1>', lambda e: _use())

        def _use():
            sel = tree.selection()
            if not sel:
                return
            name = tree.item(sel[0], 'values')[0]
            self.spec_var.set(name)
            self.msg.config(text='已填入：%s —— 点「安装/更新」开始' % name, foreground=_GREEN)
            win.destroy()

        bt = ttk.Frame(win)
        bt.pack(fill='x', padx=6, pady=(0, 6))
        ttk.Button(bt, text='使用选中', command=_use).pack(side='left')
        ttk.Button(bt, text='关闭', command=win.destroy).pack(side='right')


def plugins_dialog(main, ver):
    PluginsDialog(main, ver)


def settings_dialog(main):
    '''设置'''
    cfg = cv.load_config()
    win = _make_win(main, '设置', 760, 760)
    frm = ttk.Frame(win, padding=14)
    frm.pack(fill='both', expand=True)

    def row(parent, text):
        r = ttk.Frame(parent)
        r.pack(fill='x', pady=4)
        ttk.Label(r, text=text, width=16).pack(side='left')
        return r

    r = row(frm, 'npm 源:')
    reg_var = tk.StringVar(value=str(cfg.get('registry') or 'auto'))
    reg = ttk.Combobox(r, textvariable=reg_var, width=28, values=['auto', 'npmjs', 'npmmirror'])
    reg.pack(side='left', padx=6)
    ttk.Label(r, text='auto=官方失败切镜像；可输入 http(s) URL', foreground=_GRAY).pack(side='left')
    r2 = row(frm, '默认标签:')
    tag_var = tk.StringVar(value=str(cfg.get('tag') or 'latest'))
    ttk.Combobox(r2, textvariable=tag_var, width=14, values=['latest', 'next', 'alpha']
                 ).pack(side='left', padx=6)
    r3 = row(frm, '端口:')
    port_var = tk.StringVar(value=str(int(cfg.get('port') or 3080)))
    ttk.Spinbox(r3, from_=1, to=65535, textvariable=port_var, width=10).pack(side='left', padx=6)
    ttk.Label(r3, text='dsh web 健康检测端口（默认 3080）', foreground=_GRAY).pack(side='left')
    r4 = row(frm, '备份保留份数:')
    keep_var = tk.StringVar(value=str(int(cfg.get('keep') or 3)))
    ttk.Spinbox(r4, from_=1, to=30, textvariable=keep_var, width=8).pack(side='left', padx=6)
    ttk.Label(r4, text='每版本保留的健康备份份数（默认 3）', foreground=_GRAY).pack(side='left')

    def pick_bk():
        '''选择备份根目录（快照放 <目录>\\<版本>\\snap-*）'''
        init = bk_var.get() or paths.runtime_dir()
        d = filedialog.askdirectory(initialdir=init, title='选择备份根目录（快照放 <目录>\\<版本>\\snap-*）')
        if d:
            bk_var.set(os.path.normpath(d))

    r6 = row(frm, '备份根目录:')
    bk_var = tk.StringVar(value=str(cfg.get('backup_root') or ''))
    ttk.Entry(r6, textvariable=bk_var, width=30).pack(side='left', padx=6, fill='x', expand=True)
    ttk.Button(r6, text='浏览…', command=pick_bk).pack(side='left')
    r7 = row(frm, '默认安装位置:')
    mode_var = tk.StringVar(value=str(cfg.get('install_mode') or 'local'))
    ttk.Radiobutton(r7, text='独立目录', variable=mode_var, value='local').pack(side='left', padx=2)
    ttk.Radiobutton(r7, text='Node 全局共存', variable=mode_var, value='global').pack(side='left', padx=2)
    ttk.Label(r7, text='新版本默认装到哪里（下载窗口可临时改）', foreground=_GRAY).pack(side='left', padx=6)
    ttk.Label(frm, foreground=_GRAY,
              text='备份根目录留空 = 默认 runtime\\backups（可改到任意磁盘/U 盘）').pack(anchor='w', padx=18)
    ttk.Separator(frm).pack(fill='x', pady=(10, 6))
    ttk.Label(frm, text='便携工具（Node.js / pnpm）—— 绕开全局 node/npm',
              font=('', 10, 'bold')).pack(anchor='w', padx=18)
    ttk.Label(frm, foreground=_GRAY, wraplength=700, justify='left',
              text='留空 = 自动使用 runtime\\tools\\node、runtime\\tools\\pnpm（推荐，随文件夹走）。\n'
                   '也可自选目录/exe 文件：位于 runtime 内的会存为相对路径，整个文件夹搬到任何机器都有效；\n'
                   '「自动扫描 runtime…」会扫 runtime 里所有 node.exe / pnpm.exe 并填入空白项。'
              ).pack(anchor='w', padx=18)
    node_var = tk.StringVar(value=str(cfg.get('node_tool') or ''))
    pnpm_var = tk.StringVar(value=str(cfg.get('pnpm_tool') or ''))
    tool_status_labels = {}

    def _tool_dir_pick(var, kind):
        init = var.get().strip().strip('"') or paths.runtime_dir()
        d = filedialog.askdirectory(initialdir=init, title='选择 %s 所在目录' % kind)
        if d:
            var.set(paths.store_rel_if_inside(d))
            _refresh_tool_status()

    def _tool_file_pick(var, kind):
        init = var.get().strip().strip('"')
        if init and os.path.isfile(init):
            initialdir = os.path.dirname(init)
        elif init:
            initialdir = init
        else:
            initialdir = paths.runtime_dir()
        f = filedialog.askopenfilename(initialdir=initialdir, title='选择 %s 可执行文件' % kind,
                                       filetypes=[('可执行文件', '*.exe *.cmd'), ('所有文件', '*.*')])
        if f:
            var.set(paths.store_rel_if_inside(f))
            _refresh_tool_status()

    def _live_abs(kind, var):
        v = var.get().strip().strip('"')
        if not v:
            return ''
        p = v if os.path.isabs(v) else os.path.normpath(os.path.join(paths.runtime_dir(), v))
        if os.path.isfile(p):
            return p
        if os.path.isdir(p):
            cand = os.path.join(p, 'node.exe' if kind == 'node' else 'pnpm.exe')
            if os.path.isfile(cand):
                return cand
        return p

    def _refresh_tool_status():
        for kind, var in (('node', node_var), ('pnpm', pnpm_var)):
            exe = _live_abs(kind, var)
            lab = tool_status_labels.get(kind)
            if lab is None:
                continue
            if not exe:
                lab.config(text='留空：自动使用 runtime\\tools\\%s' % ('node' if kind == 'node' else 'pnpm'))
                continue
            if os.path.isfile(exe):
                lab.config(text='将使用：%s   ✓' % exe, foreground='darkgreen')
                continue
            lab.config(text='路径不存在：%s   ✗（留空可回退自动 tools）' % exe, foreground='firebrick')

    def scan_runtime():
        res = paths.scan_tool_candidates()
        note = []
        for kind, var in (('node', node_var), ('pnpm', pnpm_var)):
            if var.get().strip():
                continue
            if not res.get(kind):
                continue
            var.set(paths.store_rel_if_inside(res[kind][0]))
            note.append(f'{kind}: {res[kind][0]}')
        _refresh_tool_status()
        msg = '扫描到 runtime 内：node=%d 个、pnpm=%d 个' % (len(res['node']), len(res['pnpm']))
        if note:
            msg += '\n已填入：' + '；'.join(note)
        else:
            msg += '\n（已配置的路径保持不变；若未找到可执行文件，请确认已放入 runtime\\tools 下）'
        messagebox.showinfo('自动扫描', msg, parent=win)

    def tool_row(label, kind, var):
        r = ttk.Frame(frm)
        r.pack(fill='x', pady=(4, 0))
        ttk.Label(r, text=label, width=16).pack(side='left')
        ttk.Entry(r, textvariable=var, width=34).pack(side='left', padx=6, fill='x', expand=True)
        ttk.Button(r, text='目录…', command=lambda: _tool_dir_pick(var, kind)).pack(side='left')
        ttk.Button(r, text='exe…', command=lambda: _tool_file_pick(var, kind)).pack(side='left', padx=2)
        ttk.Button(r, text='默认', command=lambda v=var: (v.set(''), _refresh_tool_status())
                   ).pack(side='left', padx=2)
        lab = ttk.Label(frm, foreground=_GRAY, anchor='w')
        lab.pack(fill='x', padx=(34, 10))
        tool_status_labels[kind] = lab
        return r

    tool_row('Node.js:', 'node', node_var)
    tool_row('pnpm:', 'pnpm', pnpm_var)
    rscan = ttk.Frame(frm)
    rscan.pack(fill='x', pady=(2, 2))
    ttk.Button(rscan, text='自动扫描 runtime…', command=scan_runtime).pack(side='left', padx=(34, 4))
    ttk.Label(rscan, text='（扫描 runtime 目录内所有 node.exe / pnpm.exe，填入空白的路径框）',
              foreground=_GRAY).pack(side='left')
    _refresh_tool_status()
    r8 = row(frm, 'Web 访问地址:')
    web_var = tk.StringVar(value=str(cfg.get('web_url') or ''))
    web_ent = ttk.Entry(r8, textvariable=web_var, width=30)
    web_ent.pack(side='left', padx=6, fill='x', expand=True)
    ttk.Label(frm, foreground=_GRAY, wraplength=520, justify='left',
              text='可选。留空 = 自动：每次启动自动取 dsh 打印的带 token 地址并打开浏览器（推荐，token 每次会变）。\n'
                   '若有固定地址/token（固定端口或远程访问），把完整 URL 粘贴到上面，启动时优先使用它：\n'
                   '形如 http://127.0.0.1:端口/?token=… ；token 失效后请更新，或清空回到自动。'
              ).pack(anchor='w', padx=18)

    def save():
        reg_v = reg_var.get().strip()
        if reg_v not in ('auto', 'npmjs', 'npmmirror') and not (reg_v.startswith('http://') or reg_v.startswith('https://')):
            messagebox.showwarning('设置', 'npm 源只能是 auto/npmjs/npmmirror 或 http(s) URL。', parent=win)
            return
        try:
            port = int(port_var.get())
            keep = int(keep_var.get())
        except Exception:
            messagebox.showwarning('设置', '端口与保留份数必须是数字。', parent=win)
            return
        if not (1 <= port <= 65535 and 1 <= keep <= 30):
            messagebox.showwarning('设置', '端口 1-65535，保留份数 1-30。', parent=win)
            return
        try:
            c2 = dict(cfg)
            c2.update({
                'registry': reg_v,
                'tag': tag_var.get().strip() or 'latest',
                'port': port,
                'keep': keep,
                'install_mode': mode_var.get() or 'local',
                'backup_root': bk_var.get().strip().strip('"'),
                'web_url': web_var.get().strip().strip('"'),
                'node_tool': paths.store_rel_if_inside(node_var.get()),
                'pnpm_tool': paths.store_rel_if_inside(pnpm_var.get()),
            })
            cv.save_config(c2)
            main._port = port
            main.log_line('[设置] 已保存：registry=%s tag=%s port=%d keep=%d 安装=%s 备份目录=%s web_url=%s' % (
                reg_v, c2['tag'], port, keep, c2['install_mode'],
                c2['backup_root'] or '(默认 runtime\\backups)',
                c2['web_url'] or '(自动取 token 地址)'))
            main.log_line(f'[设置] 便携工具：node={c2["node_tool"] or "(自动 tools\\node)"} '
                          f'pnpm={c2["pnpm_tool"] or "(自动 tools\\pnpm)"}')
            win.destroy()
        except Exception as e:
            messagebox.showerror('设置', '写入配置失败：%s' % e, parent=win)

    bar = ttk.Frame(frm)
    bar.pack(fill='x', pady=(6, 0))
    ttk.Button(bar, text='保存', command=save).pack(side='left')
    ttk.Button(bar, text='关闭', command=win.destroy).pack(side='right')
    win._vars = {
        'registry': reg_var,
        'tag': tag_var,
        'port': port_var,
        'keep': keep_var,
        'backup_root': bk_var,
        'install_mode': mode_var,
        'web_url': web_var,
        'node_tool': node_var,
        'pnpm_tool': pnpm_var,
    }
    win._save = save
    win._scan_tools = scan_runtime
    win._cfg_before = cfg
    return win


def VersionSettingsDialog(main, ver):
    '''实例设置（per-version 覆盖：port / 备份目录 / 保留份数）'''
    meta = cv.read_meta(ver) if hasattr(cv, 'read_meta') else {}
    win = _make_win(main, '实例设置 - %s' % ver, 640, 300)
    frm = ttk.Frame(win, padding=14)
    frm.pack(fill='both', expand=True)

    def row(parent, text):
        r = ttk.Frame(parent)
        r.pack(fill='x', pady=5)
        ttk.Label(r, text=text, width=16).pack(side='left')
        return r

    rp = row(frm, '端口:')
    port_var = tk.StringVar(value=str(meta.get('port') or ''))
    ttk.Spinbox(rp, from_=1024, to=65535, textvariable=port_var, width=10).pack(side='left', padx=6)
    ttk.Label(rp, text='留空=跟随全局（当前 %d）' % paths.version_port(ver), foreground=_GRAY).pack(side='left')

    def pick_bk():
        '''该实例备份根目录（快照放 <目录>\\<版本>\\snap-*）'''
        init = bk_var.get() or paths.runtime_dir()
        d = filedialog.askdirectory(initialdir=init, title='该实例备份根目录（快照放 <目录>\\<版本>\\snap-*）')
        if d:
            bk_var.set(os.path.normpath(d))

    rb = row(frm, '备份根目录:')
    bk_var = tk.StringVar(value=str(meta.get('backup_root') or ''))
    ttk.Entry(rb, textvariable=bk_var, width=30).pack(side='left', padx=6, fill='x', expand=True)
    ttk.Button(rb, text='浏览…', command=pick_bk).pack(side='left')
    ttk.Label(frm, foreground=_GRAY,
              text='留空=跟随全局设置（当前目录：%s）' % paths.backups_for(ver)).pack(anchor='w', padx=18)
    rk = row(frm, '保留备份份数:')
    keep_var = tk.StringVar(value=str(meta.get('keep') or ''))
    ttk.Spinbox(rk, from_=1, to=30, textvariable=keep_var, width=8).pack(side='left', padx=6)
    ttk.Label(rk, text='留空=跟随全局（当前 %d）' % paths.version_keep(ver), foreground=_GRAY).pack(side='left')

    def save():
        try:
            m2 = dict(meta)
            for key, var, conv in (('port', port_var, int), ('keep', keep_var, int)):
                raw = var.get().strip()
                if raw == '':
                    m2.pop(key, None)
                    continue
                v = conv(raw)
                if key == 'port' and not (1 <= v <= 65535):
                    raise ValueError
                if key == 'keep' and not (1 <= v <= 30):
                    raise ValueError
                m2[key] = v
            raw_bk = bk_var.get().strip().strip('"')
            if raw_bk:
                m2['backup_root'] = os.path.normpath(raw_bk)
            else:
                m2.pop('backup_root', None)
            cv.write_meta(ver, m2)
            main.log_line(f'[实例设置] {ver}：port={m2.get("port", "跟随全局")} '
                          f'备份目录={m2.get("backup_root", "跟随全局")} '
                          f'保留份数={m2.get("keep", "跟随全局")}')
            win.destroy()
            main.do_refresh()
        except Exception as e:
            if isinstance(e, ValueError):
                messagebox.showwarning('实例设置', '%s 须为数字且在有效范围。' % key, parent=win)
            else:
                messagebox.showerror('实例设置', '保存失败：%s' % e, parent=win)

    bar = ttk.Frame(frm)
    bar.pack(fill='x', pady=8)
    ttk.Button(bar, text='保存', command=save).pack(side='left')
    ttk.Button(bar, text='关闭', command=win.destroy).pack(side='right')


def logs_dialog(main, ver):
    '''启动日志 - %s'''
    win = _make_win(main, '启动日志 - %s' % ver, 760, 480)
    top = ttk.Frame(win, padding=6)
    top.pack(fill='x')
    path_lbl = ttk.Label(top, text='', foreground=_GRAY, wraplength=720, justify='left')
    path_lbl.pack(anchor='w')
    txt = tk.Text(win, wrap='none', state='disabled', font=('Consolas', 9),
                  background='#1e1e1e', foreground='#dcdcdc')
    sb = ttk.Scrollbar(win, orient='vertical', command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    sb.pack(side='right', fill='y')
    txt.pack(fill='both', expand=True, padx=6, pady=4)

    def load():
        txt.config(state='normal')
        txt.delete('1.0', 'end')
        txt.config(state='disabled')
        try:
            lp = cl_latest(ver)
        except Exception:
            lp = None
        if not lp:
            path_lbl.config(text='尚无启动日志（请先启动该版本）。', foreground=_RED)
            return
        try:
            body = cl_tail_text(ver, 400)
        except Exception as e:
            body = '读取失败: %s' % e
        path_lbl.config(text=lp, foreground=_GRAY)
        txt.config(state='normal')
        txt.insert('end', body or '(空)')
        txt.config(state='disabled')
        txt.see('1.0')

    def open_file():
        '''打开日志'''
        try:
            lp = cl_latest(ver)
            if lp:
                os.startfile(lp)
        except Exception as e:
            messagebox.showerror('打开日志', str(e), parent=win)

    def open_dir():
        try:
            os.makedirs(paths.logs_dir(), exist_ok=True)
            os.startfile(paths.logs_dir())
        except Exception as e:
            messagebox.showerror('打开目录', str(e), parent=win)

    bt = ttk.Frame(win)
    bt.pack(fill='x', padx=6, pady=(0, 6))
    ttk.Button(bt, text='打开日志文件', command=open_file).pack(side='left')
    ttk.Button(bt, text='打开日志目录', command=open_dir).pack(side='left', padx=6)
    ttk.Button(bt, text='刷新', command=load).pack(side='left', padx=6)
    ttk.Button(bt, text='关闭', command=win.destroy).pack(side='right')
    load()


def cl_latest(ver):
    '''延迟 import core_launch（仅本对话框需要；文件读取很快，可在主线程）。'''
    import core_launch as _cl
    return _cl.latest_log(ver)


def cl_tail_text(ver, n=400):
    '''读该版本最新日志尾部最多 n 行（core_launch.read_log_tail）。'''
    import core_launch as _cl
    return _cl.read_log_tail(ver, n)


class TerminalDialog:
    '''DeepSeek Harness 终端'''

    def __init__(self, main, ver):
        '''DeepSeek Harness 终端'''
        self.main = main
        self.ver = ver
        self.win = _make_win(main, 'DeepSeek Harness 终端' + (' - ' + ver if ver else ''), 800, 560)
        self.running = False
        self.proc = None
        self.hist = []
        self.hist_i = -1
        self._pipe = _Pipe(self.win, self._on_pipe)
        top = ttk.Frame(self.win)
        top.pack(fill='x', padx=8, pady=(8, 4))
        ttk.Label(top, text='工作目录:').pack(side='left')
        default_cwd = self._default_cwd()
        self.cwd_var = tk.StringVar(value=default_cwd)
        ent = ttk.Entry(top, textvariable=self.cwd_var)
        ent.pack(side='left', fill='x', expand=True, padx=6)
        ent.bind('<Return>', lambda e: self._cd())
        ttk.Button(top, text='更改…', command=self._pick).pack(side='left')
        outf = ttk.LabelFrame(self.win, text='输出 / Output')
        outf.pack(fill='both', expand=True, padx=8, pady=4)
        self.out = tk.Text(outf, wrap='word', state='disabled', font=('Consolas', 9),
                           background='#0d1117', foreground='#d0d7de',
                           insertbackground='#d0d7de')
        sb = ttk.Scrollbar(outf, orient='vertical', command=self.out.yview)
        self.out.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.out.pack(fill='both', expand=True, padx=4, pady=4)
        self._append('输入命令后回车执行（cmd /c）。内置：cd <目录> / cls / exit。\n'
                     '常用：\n'
                     '  dsh plugin --profile web remove <包名>\n'
                     '  npm uninstall --prefix <profile\\web> <包名>\n'
                     '停止正在执行的命令用「停止」按钮（taskkill /T /F）。')
        inrow = ttk.Frame(self.win)
        inrow.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Label(inrow, text='> ').pack(side='left')
        self.inp = tk.Entry(inrow, font=('Consolas', 10))
        self.inp.pack(side='left', fill='x', expand=True)
        self.inp.bind('<Return>', lambda e: self._run())
        self.inp.bind('<Up>', lambda e: self._hist_nav(-1))
        self.inp.bind('<Down>', lambda e: self._hist_nav(1))
        self.go_btn = ttk.Button(inrow, text='执行', command=self._run)
        self.go_btn.pack(side='left', padx=4)
        self.kill_btn = ttk.Button(inrow, text='停止', state='disabled', command=self._kill)
        self.kill_btn.pack(side='left', padx=2)
        ttk.Button(inrow, text='清屏', command=self._clear).pack(side='left', padx=2)

        def on_close():
            if self.running:
                self._kill()
            self.main.unregister_term(self)
            self.win.destroy()

        self.win.protocol('WM_DELETE_WINDOW', on_close)
        self.main.register_term(self)
        self.inp.focus_set()

    def _default_cwd(self):
        if self.ver:
            home = paths.data_home(self.ver)
            web = os.path.join(home, 'profiles', 'web')
            if os.path.isdir(web):
                return web
            if os.path.isdir(home):
                return home
        return paths.runtime_dir()

    def append_out(self, text):
        self._append(text)

    def _append(self, text):
        try:
            self.out.config(state='normal')
            self.out.insert('end', str(text) + '\n')
            self.out.see('end')
            self.out.config(state='disabled')
        except Exception:
            pass

    def _on_pipe(self, *a):
        kind = a[0]
        if kind == 'done':
            rc = a[1]
            self._append('―― 命令结束（退出码 %d）――' % rc)
            self.running = False
            self.proc = None
            self.go_btn.config(state='normal')
            self.kill_btn.config(state='disabled')
            self.inp.focus_set()
            return

    def _cd(self):
        d = self.cwd_var.get().strip().strip('"')
        if os.path.isdir(d):
            self.cwd_var.set(os.path.normpath(d))
            return
        self._append('目录无效: %s' % d)

    def _pick(self):
        '''选择终端工作目录'''
        d = filedialog.askdirectory(initialdir=self.cwd_var.get(), title='选择终端工作目录')
        if d:
            self.cwd_var.set(os.path.normpath(d))

    def _run(self):
        if self.running:
            return
        cmd = self.inp.get().strip()
        if not cmd:
            return
        self.inp.delete(0, 'end')
        self.hist.append(cmd)
        self.hist_i = -1
        cwd = self.cwd_var.get().strip().strip('"')
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser('~')
            self.cwd_var.set(cwd)
        low = cmd.lower()
        if low in ('cls', 'clear'):
            self._clear()
            return
        if low.startswith('cd '):
            d = cmd[3:].strip().strip('"')
            if os.path.isdir(d):
                self.cwd_var.set(os.path.normpath(d))
                self._append(f'> {cmd}   -> {os.path.normpath(d)}')
                return
            self._append(f'> {cmd}\n目录无效: {d}')
            return
        if low in ('exit', 'quit'):
            self.win.destroy()
            return
        self._append(f'> {cmd}   [{cwd}]')
        self.running = True
        self.go_btn.config(state='disabled')
        self.kill_btn.config(state='normal')
        env = dict(os.environ)
        if self.ver:
            env['DSH_HOME'] = paths.data_home(self.ver)
        env['npm_config_cache'] = paths.npm_cache_dir()

        def worker():
            rc = -1
            try:
                p = subprocess.Popen(['cmd.exe', '/d', '/s', '/c', cmd],
                                     cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding='cp936', errors='replace',
                                     env=env, creationflags=CREATE_NO_WINDOW)
                self.proc = p
                for chunk in p.stdout:
                    line = chunk.rstrip('\r\n')
                    if not line:
                        continue
                    self.main.post_log(line, term=True)
                p.wait()
                rc = p.returncode
                self._pipe.post('done', rc)
            except Exception as e:
                self.main.post_log('执行失败: %s' % e, term=True)
                self._pipe.post('done', rc)

        threading.Thread(target=worker, daemon=True).start()

    def _clear(self):
        try:
            self.out.config(state='normal')
            self.out.delete('1.0', 'end')
            self.out.config(state='disabled')
        except Exception:
            pass

    def _kill(self):
        try:
            p = self.proc
            if p is not None and p.poll() is None:
                subprocess.run(['taskkill', '/pid', str(p.pid), '/T', '/F'],
                               capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15)
        except Exception:
            pass
        self.running = False

    def _hist_nav(self, step):
        if not self.hist:
            return
        i = max(-1, min(len(self.hist) - 1, self.hist_i + step))
        self.hist_i = i
        self.inp.delete(0, 'end')
        if i >= 0:
            self.inp.insert(0, self.hist[i])


def terminal_dialog(main, ver=None):
    TerminalDialog(main, ver)


class SystemInstancesDialog:
    '''扫描系统已装实例（免下载启用）'''

    def __init__(self, main):
        '''扫描系统已装实例（免下载启用）'''
        self.main = main
        self.win = _make_win(main, '扫描系统已装实例（免下载启用）', 880, 500)
        head = ttk.LabelFrame(self.win, text='说明', padding=(8, 4))
        head.pack(fill='x', padx=8, pady=(8, 4))
        ttk.Label(head, wraplength=840, justify='left', foreground='#333333',
                  text='自动检测这台电脑已安装的 DeepSeek Harness（npm 全局 / 常见位置）。\n'
                       '点「启用」即交给本启动器管理：引用原内核与数据目录、不复制，立即能启动/备份/管理插件，无需重新下载。'
                  ).pack(anchor='w')
        lf = ttk.LabelFrame(self.win, text='发现结果')
        lf.pack(fill='both', expand=True, padx=8, pady=4)
        cols = ('ver', 'kind', 'prefix', 'data')
        self.tree = ttk.Treeview(lf, columns=cols, show='headings')
        for c, h, w in (('ver', '版本', 110), ('kind', '类型', 170),
                        ('prefix', '安装位置', 300), ('data', '数据目录', 240)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor='w')
        vsb = ttk.Scrollbar(lf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True, padx=4, pady=4)
        self._insts = []
        btns = ttk.Frame(self.win)
        btns.pack(fill='x', padx=8, pady=6)
        ttk.Button(btns, text='启用选中实例', command=self._adopt_sel).pack(side='left')
        ttk.Button(btns, text='全部启用', command=self._adopt_all).pack(side='left', padx=6)
        ttk.Button(btns, text='手动选择已安装目录…', command=self._manual_dir).pack(side='left', padx=6)
        ttk.Button(btns, text='刷新', command=self._load).pack(side='left', padx=6)
        ttk.Button(btns, text='关闭', command=self.win.destroy).pack(side='right')
        self.status = ttk.Label(self.win, text='正在扫描…', foreground=_BLUE)
        self.status.pack(anchor='w', padx=10, pady=(0, 6))
        self._load()

    def _load(self):
        '''正在扫描…'''
        self.status.config(text='正在扫描…', foreground=_BLUE)
        pipe = _Pipe(self.win, self._on_pipe)

        def work():
            try:
                insts = cv.scan_system_instances()
                return ('found', insts)
            except Exception as e:
                return ('err', str(e))

        threading.Thread(target=lambda: pipe.post(*work()), daemon=True).start()

    def _on_pipe(self, *a):
        if a[0] == 'err':
            self.status.config(text='扫描失败: %s' % a[1], foreground=_RED)
            return
        self._fill(a[1])

    def _fill(self, insts):
        self._insts = insts
        for i in self.tree.get_children():
            self.tree.delete(i)
        for it in insts:
            prefix = it.get('prefix') or os.path.dirname(it.get('pkg_dir') or '')
            data = it.get('data_default') or '（将使用独立新数据目录）'
            self.tree.insert('', 'end', values=(it.get('version'), it.get('kind'), prefix, data))
        if insts:
            self.status.config(text='发现 %d 个已装实例 —— 点「启用选中实例」即可使用（无需下载）' % len(insts),
                               foreground=_GREEN)
            first = self.tree.get_children()
            if first:
                self.tree.selection_set(first[0])
            return
        self.status.config(text='未发现系统已装实例。可「手动选择已安装目录…」或用菜单「版本 → 下载新版本…」。',
                           foreground=_RED)

    def _sel(self):
        sel = self.tree.selection()
        if not sel:
            return None
        ver = self.tree.item(sel[0], 'values')[0]
        for it in self._insts:
            if it.get('version') == ver:
                return it
        return None

    def _adopt_sel(self):
        if self.main.busy:
            return
        it = self._sel()
        if not it:
            messagebox.showwarning('启用', '请先选择一个实例。', parent=self.win)
            return
        self.main.adopt_from_system(it)
        self.status.config(text='已启用 dsh v%s ✓（回主窗口启动/备份/插件）' % it.get('version'),
                           foreground=_GREEN)
        self.win.after(900, self.win.destroy)

    def _adopt_all(self):
        if self.main.busy:
            return
        okc = 0
        for it in self._insts:
            try:
                self.main.adopt_from_system(it)
                okc += 1
            except Exception:
                pass
        self.status.config(text='已启用 %d 个 ✓' % okc, foreground=_GREEN)
        self.win.after(900, self.win.destroy)

    def _manual_dir(self):
        '''选择已安装 dsh 的目录（含 @deepseek-ai/dsh 的 node_modules 或其上级）'''
        d = filedialog.askdirectory(title='选择已安装 dsh 的目录（含 @deepseek-ai/dsh 的 node_modules 或其上级）')
        if not d:
            return
        self.status.config(text='扫描 %s …' % d, foreground=_BLUE)
        pipe = _Pipe(self.win, self._on_pipe)

        def work():
            try:
                insts = cv.scan_system_instances(extra_dirs=[d])
                return ('found', insts)
            except Exception as e:
                return ('err', str(e))

        threading.Thread(target=lambda: pipe.post(*work()), daemon=True).start()
