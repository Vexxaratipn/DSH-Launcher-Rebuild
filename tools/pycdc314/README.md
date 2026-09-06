# pycdc Python 3.14 支持补丁（逆向工具，GPL-3.0）

用于反编译 Python 3.14 字节码（本项目还原工具链的一部分）。
补丁基于 zrax/pycdc master（编译工具链：Windows + Zig）：

- `python_3_14.cpp` —— 由本地 Python 3.14.7 的 `opcode/dis` 生成的真实 3.14 指令表
  （3.14 大量重排了指令号，不能沿用 3.13 表）。
- `gen314map.py` —— 生成该表的脚本（读取 `dis.opmap` / `_opcode.has_arg`）。
- 另需对 pycdc 源码做 3 处小改（均在字节码反汇编后可验证）：
  1. `pyc_module.h`：在 `PycMagic` 枚举加入 `MAGIC_3_14 = 0x0A0D0E2B`；
  2. `pyc_module.cpp`：`setVersion()` 增加 `case MAGIC_3_14: m_maj=3; m_min=14; m_unicode=true; break;`，
     `isSupportedVersion()` 的 3.x 分支上限放开到 14；
  3. `bytecode.cpp`：`DECLARE_PYTHON(3, 14)` 与 `case 14: return python_3_14_map(opcode);`，
     并把 `bytes/python_3_14.cpp` 加入编译。

使用方法：

```powershell
# 1) 下载 zrax/pycdc 源码，应用上述修改并放入 python_3_14.cpp
# 2) 用任意 C++17 编译器编译 pycdc/pycdas（本项目使用 zig c++，逐个 TU 编译后链接）
# 3) 反编译：pycdc.exe <module.pyc> > module.py
```

注意：pycdc 对 3.14 的控制流还原仍非 100%（异常表/with/生成器等处有噪声），
本项目以“pycdc 语义草图 + 逐字节码核对 + 与原 pyc 差分测试”的方式保证还原质量。
