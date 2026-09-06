"""dsh 'installation fallback' 镜像自愈（跨机搬迁修复）

背景
----
dsh（@deepseek-ai/dsh-app-boot）每次启动都会把"安装依赖闭包"以 junction 形式
镜像到 $DSH_HOME/profiles/node_modules（以及每个 profile 的
.dsh-module-fallback/node_modules 镜像到该 profile 自己的 node_modules）。
这些 junction 指向**本机绝对路径**。用可移植方式搬迁后：
  * 若复制工具把 junction 原样保留 → 链接指向旧机器路径（悬空）——dsh 会自行重建；
  * 若复制工具把 junction 展开成真实目录（robocopy /E、解压等）→ dsh 启动时
    ensureSymlink 直接抛错：
      "dsh: <link> exists and is not a symlink or dsh-managed module proxy;
       remove it so dsh can manage the installation fallback"
这就是跨机启动报错的根因。

本模块在启动 dsh 前把镜像里"内容与本地包重复"的真实目录清掉（纯镜像副本，
不丢数据——真实内容在本地 package node_modules / profile node_modules 里），
随后 dsh 启动时的自愈逻辑会按"当前位置"重建全部 junction。

安全原则
--------
* 只删除"镜像源里存在同名同路径内容"的真实目录；找不到镜像源的条目一律保留
  （可能是 pnpm 安装产物等，删除有风险）。
* 删除范围限定在 home\\profiles\\node_modules（共享镜像）与
  home\\profiles\\<profile>\\.dsh-module-fallback\\node_modules（profile 专属镜像）。
"""
import os
import shutil

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_junction(path):
    """Windows junction/符号链接（reparse point）判定，兼容普通目录/文件。"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(st, 'st_file_attributes', 0)
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


class _NameIndex:
    """镜像源（package node_modules 等）里存在的包名索引，用于判断
    "这个真实目录是否只是镜像副本"。"""

    def __init__(self, source_nm):
        self.basenames = set()
        self.scope_children = {}
        if not os.path.isdir(source_nm):
            return
        try:
            entries = os.listdir(source_nm)
        except OSError:
            return
        for e in entries:
            self.basenames.add(e)
        for s in entries:
            if not s.startswith('@'):
                continue
            sp = os.path.join(source_nm, s)
            if not os.path.isdir(sp):
                continue
            try:
                children = set(os.listdir(sp))
            except OSError:
                continue
            for c in children:
                self.basenames.add(c)
                cnm = os.path.join(sp, c, 'node_modules')
                if os.path.isdir(cnm):
                    try:
                        for e2 in os.listdir(cnm):
                            self.basenames.add(e2)
                            if e2.startswith('@') and os.path.isdir(os.path.join(cnm, e2)):
                                for e3 in os.listdir(os.path.join(cnm, e2)):
                                    self.basenames.add(e3)
                    except OSError:
                        pass
            self.scope_children[s] = children

    def has_basename(self, name):
        return name in self.basenames


def repair_mirror(nm, source_nm):
    """清理单个镜像目录里"与镜像源重复"的真实目录；返回删除数量。"""
    if not os.path.isdir(nm) or not os.path.isdir(source_nm):
        return 0
    index = _NameIndex(source_nm)
    removed = 0
    try:
        names = os.listdir(nm)
    except OSError:
        return 0
    for name in names:
        p = os.path.join(nm, name)
        if _is_junction(p):
            continue
        try:
            if os.path.isdir(p):
                if name.startswith('@'):
                    # 作用域目录：dsh 只会往镜像作用域里写"闭包条目"的链接。
                    # 真实(非 junction)子包只要在本地包树里同名存在，就是镜像副本
                    # （0.1.1 嵌套布局里镜像源位于 @deepseek-ai/dsh/node_modules 下，
                    # 顶层不一定存在同名 @scope —— 因此按名字索引判断，不看顶层作用域）。
                    for child in os.listdir(p):
                        cp = os.path.join(p, child)
                        if _is_junction(cp):
                            continue
                        if index.has_basename(child) or \
                                os.path.isdir(os.path.join(source_nm, name, child)):
                            if os.path.isdir(cp):
                                shutil.rmtree(cp, ignore_errors=True)
                            else:
                                try:
                                    os.remove(cp)
                                except OSError:
                                    pass
                            removed += 1
                elif index.has_basename(name):
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
            elif index.has_basename(name):
                try:
                    os.remove(p)
                except OSError:
                    pass
                removed += 1
        except OSError:
            continue
    return removed


def repair_home(home_dir, pkg_node_modules=None):
    """修复一个 DSH_HOME 的全部 dsh 托管镜像。

    :param home_dir: DSH_HOME（通常是 runtime/versions/<ver>/home）
    :param pkg_node_modules: 该版本内核的 package/node_modules；缺省时按
        home_dir 的上级布局推导（home 的兄弟目录 package/node_modules）。
    :return: {'shared': n, 'profiles': {<name>: n}}（各镜像删除的真实目录数）
    """
    if pkg_node_modules is None:
        pkg_node_modules = os.path.join(os.path.dirname(home_dir), 'package', 'node_modules')
    result = {'shared': 0, 'profiles': {}}
    # 1) 共享安装闭包镜像
    shared = os.path.join(home_dir, 'profiles', 'node_modules')
    if os.path.isdir(shared):
        result['shared'] = repair_mirror(shared, pkg_node_modules)
    # 2) 各 profile 专属镜像（.dsh-module-fallback → 该 profile 的 node_modules）
    pdir = os.path.join(home_dir, 'profiles')
    if os.path.isdir(pdir):
        for prof in os.listdir(pdir):
            if prof.startswith('.'):
                continue
            fb = os.path.join(pdir, prof, '.dsh-module-fallback', 'node_modules')
            if os.path.isdir(fb):
                src = os.path.join(pdir, prof, 'node_modules')
                result['profiles'][prof] = repair_mirror(fb, src)
    return result


def mirror_needs_repair(home_dir, pkg_node_modules=None):
    """快速预检：镜像里是否存在会阻挡 dsh 启动的真实目录/文件（两层级扫描：
    顶层真实目录不算问题——健康镜像的作用域目录本身就是真实目录）。"""
    if pkg_node_modules is None:
        pkg_node_modules = os.path.join(os.path.dirname(home_dir), 'package', 'node_modules')
    shared = os.path.join(home_dir, 'profiles', 'node_modules')

    def _has_real(nm):
        if not os.path.isdir(nm):
            return False
        try:
            names = os.listdir(nm)
        except OSError:
            return False
        for name in names:
            p = os.path.join(nm, name)
            if _is_junction(p):
                continue
            if os.path.isdir(p):
                if name.startswith('@'):
                    # 作用域目录本身合法；看其子项是否有真实目录/文件
                    try:
                        for child in os.listdir(p):
                            cp = os.path.join(p, child)
                            if not _is_junction(cp) and (os.path.isdir(cp) or os.path.isfile(cp)):
                                return True
                    except OSError:
                        pass
                else:
                    return True
            elif os.path.isfile(p):
                return True
        return False

    if _has_real(shared):
        return True
    pdir = os.path.join(home_dir, 'profiles')
    if os.path.isdir(pdir):
        for prof in os.listdir(pdir):
            fb = os.path.join(pdir, prof, '.dsh-module-fallback', 'node_modules')
            if _has_real(fb):
                return True
    return False
