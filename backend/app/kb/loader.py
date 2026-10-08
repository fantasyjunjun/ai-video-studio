"""知识库加载器。

把 kb/ 下的 Markdown（带 YAML front matter）解析为结构化知识片段，
按 tag / priority 检索，并渲染成可直接注入 LLM system prompt 的文本块。

front matter 示例：
    ---
    id: rules-core
    title: 通用铁律
    tags: [always, prompt]
    priority: 1
    ---
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import yaml

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


@dataclass
class KnowledgeDoc:
    id: str
    title: str
    tags: List[str] = field(default_factory=list)
    priority: int = 99
    path: str = ""
    body: str = ""

    @property
    def full_text(self) -> str:
        return f"# {self.title}\n\n{self.body}".strip()


class KnowledgeBase:
    def __init__(self, docs: Iterable[KnowledgeDoc]) -> None:
        self.docs: List[KnowledgeDoc] = sorted(docs, key=lambda d: (d.priority, d.id))
        self._by_id: Dict[str, KnowledgeDoc] = {d.id: d for d in self.docs}

    # ---------- 加载 ----------
    @classmethod
    def load(cls, root: str | Path) -> "KnowledgeBase":
        root = Path(root)
        if not root.is_dir():
            raise NotADirectoryError(f"知识库目录不存在: {root}")
        docs: List[KnowledgeDoc] = []
        for p in sorted(root.rglob("*.md")):
            docs.append(cls._parse(p))
        if not docs:
            raise ValueError(f"知识库目录为空: {root}")
        return cls(docs)

    @staticmethod
    def _parse(path: Path) -> KnowledgeDoc:
        raw = path.read_text(encoding="utf-8")
        m = _FM_RE.match(raw)
        meta: dict = {}
        body = raw
        if m:
            meta = yaml.safe_load(m.group(1)) or {}
            body = raw[m.end():]
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        return KnowledgeDoc(
            id=str(meta.get("id") or path.stem),
            title=str(meta.get("title") or path.stem),
            tags=[str(t) for t in tags],
            priority=int(meta.get("priority", 99)),
            path=str(path),
            body=body.strip(),
        )

    # ---------- 检索 ----------
    def by_tag(self, tag: str) -> List[KnowledgeDoc]:
        return [d for d in self.docs if tag in d.tags]

    def get(self, doc_id: str) -> Optional[KnowledgeDoc]:
        return self._by_id.get(doc_id)

    def select(self, tags: Optional[List[str]] = None) -> List[KnowledgeDoc]:
        """按 tag 取并集；tags 为空则取全部（按 priority 排序）。"""
        if not tags:
            return list(self.docs)
        seen, out = set(), []
        for t in tags:
            for d in self.by_tag(t):
                if d.id not in seen:
                    seen.add(d.id)
                    out.append(d)
        return sorted(out, key=lambda d: (d.priority, d.id))

    # ---------- 渲染 ----------
    def render(self, tags: Optional[List[str]] = None, max_chars: Optional[int] = None) -> str:
        """渲染为可注入 system prompt 的文本块。"""
        blocks = [d.full_text for d in self.select(tags)]
        text = "\n\n---\n\n".join(blocks)
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "\n…（知识库已截断）"
        return text

    def summary(self) -> str:
        return "\n".join(
            f"  - [{d.priority}] {d.id}: {d.title} (tags={','.join(d.tags)})"
            for d in self.docs
        )
