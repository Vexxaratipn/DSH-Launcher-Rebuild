"""Preflight repair for dsh 'installation fallback' mirror under <home>/profiles/node_modules.

dsh-app-boot mirrors the dsh installation dependency closure into
$DSH_HOME/profiles/node_modules using junctions (symlinkSync "junction").
Copy tools that expand junctions into real directories (robocopy /E without
/XJ, archive extract, ...) make dsh refuse to start with:

    dsh: <link> exists and is not a symlink or dsh-managed module proxy;
         remove it so dsh can manage the installation fallback

This script deletes every non-junction entry under the mirror whose content
also exists inside the local version package's node_modules (i.e. pure mirror
duplicates; nested copies like react-dom under @deepseek-ai/dsh/node_modules
are detected too).  The next dsh boot then rebuilds the junction mirror from
its own installation closure.

Usage:
    python repair_fallback.py <versionHome> <packageNodeModules> [--dry-run]
"""
import os
import shutil
import sys

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def is_junction(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(st, 'st_file_attributes', 0)
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def _is_dir(p):
    return os.path.isdir(p) and not is_junction(p)


class NameIndex:
    """Basenames that exist somewhere under package node_modules
    (top level, inside every scope, and one nested node_modules level)."""

    def __init__(self, pkg_nm):
        self.direct = {}     # rel path -> exists (for scoped direct matches)
        self.basenames = set()
        self.scope_children = {}  # '@scope' -> set of child names at pkg_nm/@scope
        self.pkg_nm = pkg_nm
        if not os.path.isdir(pkg_nm):
            return
        for e in os.listdir(pkg_nm):
            self.basenames.add(e)
        for s in os.listdir(pkg_nm):
            if not s.startswith('@'):
                continue
            sp = os.path.join(pkg_nm, s)
            if not os.path.isdir(sp):
                continue
            children = set()
            for c in os.listdir(sp):
                children.add(c)
                self.basenames.add(c)
                cnm = os.path.join(sp, c, 'node_modules')
                if os.path.isdir(cnm):
                    for e in os.listdir(cnm):
                        self.basenames.add(e)
                        # nested scope under nested node_modules
                        if e.startswith('@') and os.path.isdir(os.path.join(cnm, e)):
                            for e2 in os.listdir(os.path.join(cnm, e)):
                                self.basenames.add(e2)
            self.scope_children[s] = children

    def has_basename(self, name):
        return name in self.basenames

    def scope_child_exists(self, rel):
        """rel like '@deepseek-ai/dsh'."""
        parts = rel.split('/', 1)
        if len(parts) != 2 or not parts[0].startswith('@'):
            return False
        return parts[1] in self.scope_children.get(parts[0], ())


def repair_mirror(nm, source_nm, dry_run=False, progress=None):
    """Delete real (non-junction) entries under a dsh-managed junction mirror
    (nm) whose content also exists under the mirror source (source_nm)."""
    if not os.path.isdir(nm):
        return {'removed': 0, 'kept': 0, 'note': 'missing mirror'}
    if not os.path.isdir(source_nm):
        return {'removed': 0, 'kept': 0, 'note': 'missing source (skip)'}
    index = NameIndex(source_nm)
    removed = 0
    kept = 0
    log = []

    def do_remove(p, why):
        nonlocal removed
        if dry_run:
            log.append('would remove %s (%s)' % (p, why))
            removed += 1
            return
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=False)
            elif os.path.exists(p) or os.path.islink(p):
                os.remove(p)
            removed += 1
            log.append('removed %s (%s)' % (p, why))
        except OSError as e:
            log.append('KEEP(fail) %s: %s' % (p, e))
            kept += 1

    for name in sorted(os.listdir(nm)):
        p = os.path.join(nm, name)
        if is_junction(p):
            continue
        if os.path.isdir(p):
            if name.startswith('@'):
                if os.path.isdir(os.path.join(source_nm, name)):
                    for child in sorted(os.listdir(p)):
                        cp = os.path.join(p, child)
                        if is_junction(cp):
                            continue
                        rel = name + '/' + child
                        if os.path.isdir(os.path.join(source_nm, rel)) or index.has_basename(child):
                            do_remove(cp, 'mirror child %s' % rel)
                        else:
                            kept += 1
                else:
                    kept += 1
            else:
                if index.has_basename(name):
                    do_remove(p, 'mirror entry')
                else:
                    kept += 1
                    log.append('keep(nonmirror) %s' % p)
        else:
            if index.has_basename(name):
                do_remove(p, 'mirror file')
            else:
                kept += 1
                log.append('keep(file) %s' % p)
    if progress:
        for line in log:
            progress(line)
    return {'removed': removed, 'kept': kept}


def repair_home(home, pkg_nm, dry_run=False, progress=None):
    """Repair every dsh-managed junction mirror of one Harness home:
    1) <home>/profiles/node_modules            mirrored from package node_modules
    2) <home>/profiles/<profile>/.dsh-module-fallback/node_modules
                                              mirrored from the profile's node_modules
    """
    results = []
    # 1) shared installation-closure mirror
    nm = os.path.join(home, 'profiles', 'node_modules')
    r = repair_mirror(nm, pkg_nm, dry_run=dry_run, progress=progress)
    r['where'] = nm
    results.append(r)
    # 2) per-profile owned-bundle fallback mirrors
    pdir = os.path.join(home, 'profiles')
    if os.path.isdir(pdir):
        for prof in sorted(os.listdir(pdir)):
            fb = os.path.join(pdir, prof, '.dsh-module-fallback', 'node_modules')
            if os.path.isdir(fb):
                src = os.path.join(pdir, prof, 'node_modules')
                r2 = repair_mirror(fb, src, dry_run=dry_run, progress=progress)
                r2['where'] = fb
                results.append(r2)
    return results


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    home = sys.argv[1]
    pkg_nm = sys.argv[2]
    dry = '--dry-run' in sys.argv
    res = repair_home(home, pkg_nm, dry_run=dry, progress=print)
    print('RESULT', res)
    sys.exit(0)
