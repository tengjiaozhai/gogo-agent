"""034 带文档来源和版本的离线政策片段导入与只读检索。"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from xml.etree import ElementTree
from zipfile import ZipFile

from pydantic import BaseModel, ConfigDict, Field


DEFAULT_CORPUS = Path(__file__).with_name("policy_corpus.sample.json")
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class PolicySnippet(BaseModel):
    """一次命中的政策文档片段和可追溯来源。"""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(..., description="文档片段唯一标识")
    section: str = Field(..., description="片段所在章节或段落位置")
    text: str = Field(..., description="原政策片段文本，不作为精确额度结论")
    source: str = Field(..., description="政策文档来源文件或脱敏样例名称")
    version: str = Field(..., description="来源文件 SHA-256 或样例版本")
    score: float = Field(..., ge=0, le=1, description="词面重合的离线检索得分，仅用于召回排序")


def extract_docx_paragraphs(path: Path) -> list[str]:
    """从 DOCX 的正文段落与表格单元格提取可见文本。"""
    with ZipFile(path) as archive:
        document = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs: list[str] = []
    for paragraph in document.iter(f"{_WORD_NS}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{_WORD_NS}t")).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def build_policy_corpus(source: Path, destination: Path, *, chunk_size: int = 400) -> dict:
    """离线导入单份 DOCX，按段落分片并写入来源和内容指纹。"""
    if chunk_size < 1:
        raise ValueError("chunk_size 必须大于零")
    digest = sha256(source.read_bytes()).hexdigest()
    chunks = []
    for paragraph_index, paragraph in enumerate(extract_docx_paragraphs(source), 1):
        for part_index, start in enumerate(range(0, len(paragraph), chunk_size), 1):
            chunks.append({
                "chunk_id": f"{source.stem}-{paragraph_index}-{part_index}",
                "section": f"段落 {paragraph_index}",
                "text": paragraph[start:start + chunk_size],
            })
    if not chunks:
        raise ValueError("政策文档没有可导入的正文片段")
    payload = {"schema_version": 1, "source": source.name, "version": digest, "chunks": chunks}
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def _terms(text: str) -> set[str]:
    """对中文文本用相邻双字，对字母数字词用完整词生成稳定查询项。"""
    chinese = re.findall(r"[\u4e00-\u9fff]+", text)
    bigrams = {word[i:i + 2] for word in chinese for i in range(len(word) - 1)}
    latin = {part.lower() for part in re.findall(r"[A-Za-z0-9]+", text)}
    return bigrams | latin


class PolicyKnowledgeIndex:
    """只读词面索引，版本化来源明确，未命中时返回空集合。"""

    def __init__(self, path: Path | None = None):
        path = path or Path(os.getenv("GOGO_POLICY_CORPUS_PATH") or DEFAULT_CORPUS)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1 or not payload.get("source") or not payload.get("version"):
            raise ValueError("政策语料版本或来源缺失")
        self.source: str = payload["source"]
        self.version: str = payload["version"]
        self.chunks: tuple[dict[str, str], ...] = tuple(payload["chunks"])
        if not self.chunks or any(not chunk.get("text") or not chunk.get("chunk_id") for chunk in self.chunks):
            raise ValueError("政策语料片段为空或缺少标识")

    def retrieve(self, question: str, *, top_k: int = 3, min_score: float = 0.2) -> list[PolicySnippet]:
        """返回带来源的相关片段；分数仅用于召回，不判断金额合规。"""
        terms = _terms(question)
        if not terms or top_k < 1:
            return []
        scored = []
        for chunk in self.chunks:
            overlap = len(terms & _terms(chunk["text"]))
            score = overlap / len(terms)
            if score >= min_score:
                scored.append(PolicySnippet(
                    chunk_id=chunk["chunk_id"], section=chunk.get("section", "正文"),
                    text=chunk["text"], source=self.source, version=self.version, score=score,
                ))
        scored.sort(key=lambda item: (-item.score, item.chunk_id))
        return scored[:top_k]
