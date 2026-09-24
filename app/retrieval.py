"""Busca simples por relevância (BM25) sobre documentos, e-mails e decisões. Sem dependências externas."""
import math
import re
import threading
import unicodedata
from collections import Counter

from .db import Chunk, SessionLocal

STOP = set("""a o e é de da do das dos que em no na nos nas um uma uns umas para por com sem ao aos à às se
seu sua seus suas ele ela eles elas isso esse essa este esta isto como mais mas ou já não sim foi ser
são está estão tem têm ter vai vão pelo pela pelos pelas também quando onde qual quais sobre entre até
muito pode podem nós vocês você me te lhe nos vos boa tarde dia noite att tks obrigado obrigada abs
the and for with this that from are was were have has will would could should""".split())

_lock = threading.Lock()
_index = None
_dirty = True


def normalize(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return text


def tokens(text):
    return [t for t in re.findall(r"[a-z0-9_\-\.]{3,}", normalize(text)) if t not in STOP]


def chunk_text(text, size=1200, overlap=150):
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += size - overlap
    return out


def mark_dirty():
    global _dirty
    _dirty = True


class BM25:
    def __init__(self, docs):
        self.docs = docs  # lista de (chunk_id, source_kind, source_id, label, text)
        self.tf = [Counter(tokens(d[4] + " " + d[3])) for d in docs]
        self.len = [sum(c.values()) for c in self.tf]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 1
        df = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs) or 1
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query, k=10, exclude=None, kinds=None):
        q = set(tokens(query))
        scores = []
        for i, c in enumerate(self.tf):
            d = self.docs[i]
            if kinds and d[1] not in kinds:
                continue
            if exclude and (d[1], d[2]) in exclude:
                continue
            s = 0.0
            for t in q:
                f = c.get(t)
                if f:
                    s += self.idf.get(t, 0) * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * self.len[i] / self.avg))
            if s > 0:
                scores.append((s, d))
        scores.sort(key=lambda x: -x[0])
        return scores[:k]


def get_index():
    global _index, _dirty
    with _lock:
        if _index is None or _dirty:
            with SessionLocal() as s:
                rows = s.query(Chunk.id, Chunk.source_kind, Chunk.source_id, Chunk.label, Chunk.text).all()
            _index = BM25([tuple(r) for r in rows])
            _dirty = False
        return _index


def rank_texts(query, items, key):
    """Ordena uma lista pequena (ex.: decisões) por relevância à consulta."""
    q = set(tokens(query))
    scored = []
    for it in items:
        tk = Counter(tokens(key(it)))
        scored.append((sum(tk.get(t, 0) for t in q), it))
    scored.sort(key=lambda x: -x[0])
    return scored
