# DSH Launcher Rebuild

便携版 **DeepSeek Harness 多版本独立管理启动器** 的工程源码（自 `DeepSeek Harness Launcher.exe`
按字节码还原），并包含两项修复：

1. **跨机搬迁修复**：复制/解压工具把 dsh 管理的 junction 依赖镜像展开成真实目录后，
   dsh 会拒绝启动并报错
   `dsh: ... is not a symlink or dsh-managed module proxy; remove it so dsh can manage the installation fallback`。
   本仓库新增 `src/fix_links.py`：启动器在启动每个版本前自动清除“与本地内核重复”的镜像副本，
   让 dsh 按当前位置自动重建 junction —— 换机后无需任何手工步骤。
2. **界面卡顿修复**：`core_versions.list_installed()` 不再每次刷新都对每个版本做全量递归
   体积统计，改为安装时一次性统计并缓存进 `meta['size_mb']`。

## 技术栈

- Python 3.14 + tkinter + PyInstaller（onefile），Windows x64。
- 运行时结构：exe 同级 `runtime\versions\<版本>\{package, home, meta.json}`、
  `runtime\tools\{node,pnpm}` 等，全部路径相对启动器所在目录，随目录整体搬迁。

## 目录结构

```
src/           还原后的启动器源码（10 个模块，主入口 main.py）
  ├─ main.py            入口：单实例锁 / 图标 / 打包自检钩子(_smoke)
  ├─ paths.py           唯一路径源（runtime/versions/tools…，相对化存储规则）
  ├─ core_versions.py   版本安装/导入/删除/列表（含体积缓存）
  ├─ core_launch.py     启动/停止/健康检查/日志（启动前调用 fix_links 自愈）
  ├─ fix_links.py       ★跨机搬迁 junction 镜像自愈（新增）
  ├─ core_backup.py     快照备份/恢复（robocopy）
  ├─ core_plugins.py    web profile 插件管理与禁用 overlay
  ├─ core_runner.py     UI 后台任务队列/事件泵/有界日志
  ├─ ui_main.py         主窗口
  └─ ui_dialogs.py      对话框集合
assets/        Deepseek.ico（原启动器资源）
scripts/       build_exe.ps1（PyInstaller 打包）、smoke_core.py（核心回归冒烟）
tools/         repair_fallback.py（独立版修复工具，可直接 CLI 使用）
  └─ pycdc314/          Python 3.14 反编译支持补丁（详见该目录 README）
docs/          还原报告、模块架构说明
```

## 快速开始

```powershell
# 1) 源码冒烟（隔离临时 runtime，不触碰真实环境）
python scripts\smoke_core.py

# 2) 打包 exe（需 Python 3.14 + pyinstaller）
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
# 产物：dist\DeepSeek Harness Launcher.exe

# 3) exe 自检
$env:DSH_LAUNCHER_SMOKE = '1'
$env:DSH_SMOKE_OUT = "$PWD\smoke.json"
.\dist\DeepSeek Harness Launcher.exe   # 或用 Start-Process -Wait
Get-Content smoke.json                 # 期望 {"rc":0, "imports":true, ...}
```

修复工具单独使用（机器 B 上如无法启动某版本，可先执行）：

```powershell
python tools\repair_fallback.py <该版本 home 目录> <该版本 package\node_modules 目录>
```

## 说明与注意事项

- **模块来源**：全部模块由原 exe（PyInstaller + Python 3.14）内嵌字节码反编译还原，
  并经逐指令/差分测试核验；`ui_main.py` 与原始字节码达到指令级一致。还原方法与工具见
  `docs/还原报告.md` 与 `tools/pycdc314/`。
- 还原为“语义等价”源码，非逐字原文（注释/行号不可从字节码恢复）。
- `fix_links` 只删除“镜像源里存在同名内容”的真实目录；找不到镜像源的一律保留。
- 上游 dsh 内核（`@deepseek-ai/dsh` 等）不在本仓库；运行时从各版本 `package` 安装获得。
- 已知边界：0.1.2-rc.1 / 0.1.1-rc.2 属 RC，个别带实验性 web 状态的 home 整体复制后，
  除 junction 问题外还可能触发上游 include 装载的 EISDIR（全新 profile 不受影响），详见文档。

## 许可

请在使用前添加合适的 `LICENSE` 文件。第三方说明：
- `tools/pycdc314` 为反编译工具 zrax/pycdc（GPL-3.0）的 3.14 支持补丁（仅补丁与生成脚本）。
- `Deepseek.ico` 取自原启动器，版权归原作者。

## 关联

- DeepSeek Harness：https://github.com/deepseek-ai/dsh
- pycdc（Decompyle++）：https://github.com/zrax/pycdc
