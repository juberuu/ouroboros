"""
Apply-patch shim for Claude Code CLI.
Writes apply_patch script to /usr/local/bin/ on import.
"""
import pathlib


APPLY_PATCH_PATH = pathlib.Path("/usr/local/bin/apply_patch")
APPLY_PATCH_CODE = r"""#!/usr/bin/env python3
import sys
import pathlib

def _norm_line(l: str) -> str:
    if l.startswith(" "):
        return l[1:]
    return l

def _find_subseq(hay, needle):
    if not needle:
        return 0
    n = len(needle)
    for i in range(0, len(hay) - n + 1):
        ok = True
        for j in range(n):
            if hay[i + j] != needle[j]:
                ok = False
                break
        if ok:
            return i
    return -1

def _find_subseq_rstrip(hay, needle):
    if not needle:
        return 0
    hay2 = [x.rstrip() for x in hay]
    needle2 = [x.rstrip() for x in needle]
    return _find_subseq(hay2, needle2)

def apply_update_file(path: str, hunks: list[list[str]]):
    p = pathlib.Path(path)
    if not p.exists():
        sys.stderr.write(f"apply_patch: file not found: {path}\n")
        sys.exit(2)

    text = p.read_text(encoding="utf-8")
    src = text.splitlines()

    for hunk in hunks:
        old_seq = []
        new_seq = []
        for line in hunk:
            if line.startswith("+"):
                new_seq.append(line[1:])
            elif line.startswith("-"):
                old_seq.append(line[1:])
            else:
                c = _norm_line(line)
                old_seq.append(c)
                new_seq.append(c)

        idx = _find_subseq(src, old_seq)
        if idx < 0:
            idx = _find_subseq_rstrip(src, old_seq)
        if idx < 0:
            sys.stderr.write("apply_patch: failed to match hunk in file: " + path + "\n")
            sys.stderr.write("HUNK (old_seq):\n" + "\n".join(old_seq) + "\n")
            sys.exit(3)

        src = src[:idx] + new_seq + src[idx + len(old_seq):]

    p.write_text("\n".join(src) + "\n", encoding="utf-8")

def main():
    lines = sys.stdin.read().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("*** Begin Patch"):
            i += 1
            continue

        if line.startswith("*** Update File:"):
            path = line.split(":", 1)[1].strip()
            i += 1

            hunks = []
            cur = []
            while i < len(lines) and not lines[i].startswith("*** "):
                if lines[i].startswith("@@"):
                    if cur:
                        hunks.append(cur)
                        cur = []
                    i += 1
                    continue
                cur.append(lines[i])
                i += 1
            if cur:
                hunks.append(cur)

            apply_update_file(path, hunks)
            continue

        if line.startswith("*** End Patch"):
            i += 1
            continue

        i += 1

if __name__ == "__main__":
    main()
"""
APPLY_PATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
APPLY_PATCH_PATH.write_text(APPLY_PATCH_CODE, encoding="utf-8")
APPLY_PATCH_PATH.chmod(0o755)


def install():
    """Install apply_patch script to /usr/local/bin/."""
    APPLY_PATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    APPLY_PATCH_PATH.write_text(APPLY_PATCH_CODE, encoding="utf-8")
    APPLY_PATCH_PATH.chmod(0o755)
