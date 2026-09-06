"""为 zrax/pycdc 生成 Python 3.14 指令表（bytes/python_3_14.cpp）。

用法（在装有 Python 3.14 的机器上）：
    python gen314map.py <pycdc-source-dir> [输出文件]
- <pycdc-source-dir> 指向 zrax/pycdc 源码根目录（含 bytecode_ops.inl）；
- 输出默认写入 <pycdc-source-dir>/bytes/python_3_14.cpp。
表内容由本机 Python 3.14 的 dis.opmap / _opcode.has_arg 决定（3.14 重排了指令号，
不能沿用 3.13 表）。生成后还需对 pycdc 源码做 3 处小改，见 README.md。
"""
import dis
import re
import sys
import _opcode

PSEUDO = {"JUMP", "JUMP_IF_FALSE", "JUMP_IF_TRUE", "JUMP_NO_INTERRUPT",
          "SETUP_CLEANUP", "SETUP_FINALLY", "SETUP_WITH", "POP_BLOCK",
          "LOAD_CLOSURE", "STORE_FAST_MAYBE_NULL", "ANNOTATIONS_PLACEHOLDER",
          "ENTER_EXECUTOR"}


def parse_inl(inl_path):
    noarg, arg = set(), set()
    for line in open(inl_path, encoding='utf-8'):
        m = re.match(r"\s*OPCODE\(([A-Z0-9_]+)\)", line)
        if m:
            noarg.add(m.group(1))
            continue
        m = re.match(r"\s*OPCODE_A(?:_FIRST)?\(([A-Z0-9_]+)\)", line)
        if m:
            arg.add(m.group(1))
    return noarg, arg


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else src + r'\bytes\python_3_14.cpp'
    noarg, arg = parse_inl(src + r'\bytecode_ops.inl')

    missing_noarg, missing_arg = set(), set()
    rows = []
    for name, oid in dis.opmap.items():
        if oid >= 255 or name in PSEUDO:
            continue
        has_a = bool(_opcode.has_arg(oid))
        if has_a:
            if name not in arg:
                missing_arg.add(name)
            enum = name + '_A'
        else:
            if name not in noarg:
                missing_noarg.add(name)
            enum = name
        rows.append((oid, enum))
    rows.sort()
    print('missing NO-ARG entries to add to bytecode_ops.inl:', sorted(missing_noarg))
    print('missing ARG entries to add to bytecode_ops.inl :', sorted(missing_arg))

    # 补丁说明：缺失的 NO-ARG 名需插到 bytecode_ops.inl 中首个 OPCODE_A_FIRST 之前；
    # 缺失的 ARG 名以 OPCODE_A(<名>) 追加到文件末尾（重新编译后枚举值自动一致）。
    with open(out, 'w', encoding='utf-8', newline='\n') as f:
        f.write('#include "bytecode_map.h"\n\nBEGIN_MAP(3, 14)\n')
        for oid, enum in rows:
            f.write('    MAP_OP(%d, %s)\n' % (oid, enum))
        f.write('END_MAP()\n')
    print('wrote %d entries to %s' % (len(rows), out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
