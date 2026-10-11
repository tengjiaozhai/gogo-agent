"""034 离线导入：cd 仓库根目录；PYTHONPATH=src .venv/bin/python scripts/build_policy_corpus.py 文档.docx 语料.json。

仅在本地读取 DOCX 并输出 JSON；替换运行时语料需显式设置 GOGO_POLICY_CORPUS_PATH。
PyCharm：Script path 为本文件，Working directory 为仓库根目录，Parameters 为源文件与目标文件。
断点：build_policy_corpus 的 extract_docx_paragraphs 和 destination.write_text。
逐步指南：docs/契约样例/阶段D-029-041-调试指南.md。
"""

from argparse import ArgumentParser
from pathlib import Path

from gogo_agent.travel.knowledge import build_policy_corpus


def main() -> None:
    parser = ArgumentParser(description="离线 DOCX 政策切片及版本索引")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--chunk-size", type=int, default=400)
    args = parser.parse_args()
    if not args.source.is_file() or args.source.suffix.lower() != ".docx":
        parser.error("source 必须是存在的 DOCX 文件")
    if args.destination.exists():
        parser.error("destination 已存在，请选择新路径，避免覆盖政策语料")
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    result = build_policy_corpus(args.source, args.destination, chunk_size=args.chunk_size)
    print("source:", result["source"])
    print("version:", result["version"])
    print("chunks:", len(result["chunks"]))
    print("output:", args.destination.resolve())


if __name__ == "__main__":
    main()
