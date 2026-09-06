"""UI 后台任务基础设施：queue + 主线程 after 泵 + 有界日志。

用法：
  bus = Bus(root)
  bus.post("log", "text")                 # 任意线程入队
  bus.post("progress", 42)
  bus.run(fn, done=on_done)               # 后台执行 fn(可能抛异常)，完成回调在泵里执行
主线程事件处理：定义 root/_bus 事件名对应处理函数即可，见 ui_main 约定：
  bus.on("log", handler) 注册 —— handler 在泵线程(主线程)中被调用，可安全操作 tk。
"""
import queue
import threading

PUMP_MS = 40
MAX_PER_PUMP = 400


class Bus:
    def __init__(self, root, pump_ms=PUMP_MS):
        self.root = root
        self.pump_ms = pump_ms
        self.q = queue.Queue()
        self._handlers = {}
        self._pump()

    def post(self, kind, *args):
        """任意线程入队事件；队列异常时静默（例如程序退出期间）。"""
        try:
            self.q.put_nowait((kind, args))
        except Exception:
            return

    def on(self, kind, handler):
        """注册事件处理函数（在主线程泵中被调用）。"""
        self._handlers[kind] = handler

    def run(self, fn, done=None):
        """在后台线程执行 fn()；fn 抛出的异常会转成 ('error', ex) 事件。
        done(结果) 在泵里被派发：注册 bus.on('done'/'error') 或在 run 里给回调。"""

        def worker():
            try:
                result = fn()
            except Exception as e:
                self.post('error', e)
                return
            if done:
                try:
                    done(result)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _pump(self):
        """主线程事件泵：每 pump_ms 由 root.after 重新调度；
        每轮最多处理 MAX_PER_PUMP 条事件，避免阻塞 UI。"""
        try:
            n = 0
            while n < MAX_PER_PUMP:
                try:
                    kind, args = self.q.get_nowait()
                except queue.Empty:
                    break
                n += 1
                try:
                    h = self._handlers.get(kind)
                    if h:
                        h(*args)
                except Exception:
                    # 单个处理器异常不得拖垮泵（也避免影响同批其它事件）
                    pass
            self.root.after(self.pump_ms, self._pump)
        except Exception:
            # root 已销毁等情况：停止泵
            return


class LogBuffer:
    """有界日志：自动裁剪到 max_lines 行（>2x 时批量删最旧到 max_lines）。"""

    def __init__(self, max_lines=1000):
        self.max_lines = max_lines
        self.lines = []
        self.count = 0

    def append(self, line):
        self.lines.append(line)
        self.count += 1
        if self.count > self.max_lines * 2:
            cut = self.count - self.max_lines
            del self.lines[:cut]
            self.count -= cut

    def tail(self, n):
        return self.lines[-n:]

    def reset(self):
        self.lines.clear()
        self.count = 0
