#!/usr/bin/env python3
"""docs 内部相对链接检查:markdown 里的相对链接必须解析到存在的文件。

只查相对链接(跳过 http/https/mailto/纯锚点);剥离 #anchor 与 ?query 后,
按链接所在 md 文件的目录解析。图片链接一并检查。发现断链 exit 1。
"""

import re
import sys
from pathlib import Path

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)[^)]*\)")
SKIP = ("http://", "https://", "mailto:", "#")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    broken = []
    for md in sorted(root.glob("docs/**/*.md")):
        for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
            for target in LINK.findall(line):
                if target.startswith(SKIP):
                    continue
                path = target.split("#", 1)[0].split("?", 1)[0]
                if not path:
                    continue
                if not (md.parent / path).resolve().exists():
                    broken.append(f"{md.relative_to(root)}:{lineno} -> {target}")
    for b in broken:
        print("BROKEN:", b)
    print(f"checked docs/**/*.md, broken links: {len(broken)}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
