"""034 零准备政策语料演示：cd 仓库根目录；PYTHONPATH=src .venv/bin/python scripts/demo_034_policy_knowledge.py。

在临时目录创建脱敏 DOCX、离线导入、检索，再修改文档重建索引；不读取真实公司资料。
PyCharm：Working directory 为仓库根目录，Interpreter 为 .venv/bin/python；无参数。
断点：build_policy_corpus 内 extract_docx_paragraphs、PolicyKnowledgeIndex.retrieve 的 overlap 计算。
逐步指南：docs/契约样例/阶段D-029-041-调试指南.md。
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from gogo_agent.travel.knowledge import PolicyKnowledgeIndex, build_policy_corpus


def write_docx(path: Path, text: str) -> None:
    """仅给教学演示写最小合法 DOCX 正文。"""
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
        ))


def main() -> None:
    with TemporaryDirectory(prefix="gogo-policy-034-") as directory:
        source = Path(directory) / "sanitized_policy.docx"
        target = Path(directory) / "policy_index.json"
        write_docx(source, "上海酒店住宿标准依员工职级确定，超额费用需个人承担。")
        first = build_policy_corpus(source, target)
        index = PolicyKnowledgeIndex(target)
        print("首版片段数:", len(first["chunks"]))
        print("已知问题片段:", [(item.chunk_id, item.source) for item in index.retrieve("上海酒店标准")])
        print("未知问题片段:", index.retrieve("量子火箭推进器"))
        write_docx(source, "北京交通补贴按每日政策标准执行。")
        second = build_policy_corpus(source, target)
        print("文档更新后版本变化:", first["version"] != second["version"])
        print("新问题片段:", [(item.chunk_id, item.source) for item in PolicyKnowledgeIndex(target).retrieve("北京交通补贴")])
    print("真实文档导入命令: PYTHONPATH=src .venv/bin/python scripts/build_policy_corpus.py 源.docx 新索引.json")


if __name__ == "__main__":
    main()
