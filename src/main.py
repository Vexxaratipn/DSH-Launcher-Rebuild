"""DSH Launcher Rebuild - 入口（单实例 + 图标 + 打包自检钩子）。"""
import json
import os
import socket
import sys
import threading

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

SINGLE_PORT = 35901
SMOKE_ENV = 'DSH_LAUNCHER_SMOKE'
SMOKE_OUT_ENV = 'DSH_SMOKE_OUT'


def find_icon():
    """按优先级查找打包/开发目录下的 Deepseek.ico。"""
    cands = []
    mp = getattr(sys, '_MEIPASS', None)
    if mp:
        cands.append(os.path.join(mp, 'Deepseek.ico'))
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else None
    if exe_dir:
        cands.append(os.path.join(exe_dir, 'Deepseek.ico'))
    cands.append(os.path.join(APP_DIR, 'Deepseek.ico'))
    cands.append(os.path.join(APP_DIR, '..', 'Deepseek.ico'))
    cands.append(os.path.join(APP_DIR, '..', '..', 'Deepseek.ico'))
    for c in cands:
        if os.path.isfile(c):
            return os.path.normpath(c)
    return None


def hold_single():
    """单实例锁：占用 127.0.0.1:SINGLE_PORT；失败说明已有实例在运行。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('127.0.0.1', SINGLE_PORT))
        s.listen(1)
    except OSError:
        return None

    def keeper():
        while True:
            try:
                c, _ = s.accept()
                c.close()
            except Exception:
                return

    threading.Thread(target=keeper, daemon=True).start()
    return s


def _smoke():
    """打包自检钩子：设 DSH_LAUNCHER_SMOKE=1 时做静默自检并把结果 JSON
    写入 DSH_SMOKE_OUT 指定的文件（供 CI/脚本验证 exe 可用性）。"""
    arg = os.environ.get(SMOKE_ENV)
    if not arg:
        return False
    out = os.environ.get(SMOKE_OUT_ENV, '')
    res = {'rc': 0, 'arg': arg}
    try:
        import core_versions as cv
        import core_launch as cl
        import core_backup as cb  # noqa: F401
        import core_plugins as cp  # noqa: F401
        import paths
        res['imports'] = True
        res['runtime'] = paths.runtime_dir()
        res['installed'] = [m.get('version') for m in cv.list_installed()]
        res['healthy3080'] = cl.healthy(3080)
        res['bundled_node'] = os.path.isfile(paths.node_exe())
        res['bundled_pnpm'] = os.path.isfile(paths.pnpm_exe())
        res['node_exe'] = paths.node_exe() if os.path.isfile(paths.node_exe()) else ''
        res['dot_dsh'] = paths.dot_dsh_dir()
    except Exception as e:
        res['rc'] = -1
        res['err'] = str(e)
    if out:
        try:
            with open(out, 'w', encoding='utf-8') as f:
                json.dump(res, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    return True


def main():
    if _smoke():
        sys.exit(0)
    if hold_single() is None:
        import tkinter
        from tkinter import messagebox
        r = tkinter.Tk()
        r.withdraw()
        messagebox.showinfo('Deepseek Harness 管理器', '启动器已在运行，请切换到已打开的窗口。')
        r.destroy()
        sys.exit(0)
    import tkinter as tk
    import core_runner as runner
    from ui_main import MainApp
    root = tk.Tk()
    icon = find_icon()
    if icon:
        try:
            root.iconbitmap(icon)
        except Exception:
            pass
    bus = runner.Bus(root)
    app = MainApp(root, bus)
    root.mainloop()


if __name__ == '__main__':
    main()
