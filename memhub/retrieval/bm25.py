# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: In-memory BM25+ scoring over a small candidate pool (stats computed on the
pool, not a global corpus) with bilingual tokenization; scores feed RRF ranking.
"""
import math
import re
from collections import Counter

BM25_K1 = 1.5
BM25_B = 0.75

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """Tokenize text into lowercased ascii words and CJK runs."""
    if not text:
        return []
    return [tok.lower() if tok.isascii() else tok for tok in _TOKEN_RE.findall(text)]


def compute_bm25_scores(
    query_terms: list[str],
    candidate_contents: list[str],
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> list[float]:
    """Compute a BM25+ score per candidate content; zeros if query or pool is empty."""
    if not query_terms or not candidate_contents:
        return [0.0] * len(candidate_contents)

    n = len(candidate_contents)
    tokenized = [tokenize(c) for c in candidate_contents]
    doc_lens = [len(toks) for toks in tokenized]
    avgdl = (sum(doc_lens) / n) or 1.0

    q_terms = list({t for t in query_terms if t})
    df = {t: sum(1 for toks in tokenized if t in toks) for t in q_terms}
    tf_per_doc = [Counter(toks) for toks in tokenized]

    scores: list[float] = []
    for i, _ in enumerate(tokenized):
        dl = doc_lens[i] or 1
        tf_map = tf_per_doc[i]
        score = 0.0
        for t in q_terms:
            tf = tf_map.get(t, 0)
            if tf == 0:
                continue
            idf = math.log((n - df[t] + 0.5) / (df[t] + 0.5) + 1.0)
            denom = tf + k1 * (1 - b + b * dl / avgdl)
            score += idf * (tf * (k1 + 1)) / denom
        scores.append(score)

    return scores



class LexicalIndex:
    """An inverted index of a fixed corpus, scored with the same BM25+ as
    ``compute_bm25_scores`` over the whole corpus.

    Building it tokenizes every document once; a query then touches only the
    documents that contain one of its terms instead of rescoring the corpus.
    """

    def __init__(self, documents: list[str], k1: float = BM25_K1, b: float = BM25_B) -> None:
        self.k1, self.b = k1, b
        self.n = len(documents)
        self.doc_lens: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, text in enumerate(documents):
            counts = Counter(tokenize(text))
            self.doc_lens.append(sum(counts.values()))
            for term, tf in counts.items():
                self.postings.setdefault(term, []).append((i, tf))
        self.avgdl = (sum(self.doc_lens) / self.n) if self.n else 1.0
        self.avgdl = self.avgdl or 1.0

    def scores(self, query_terms: list[str]) -> dict[int, float]:
        """Document index -> score, for every document sharing a query term."""
        out: dict[int, float] = {}
        for t in {t for t in query_terms if t}:
            posting = self.postings.get(t)
            if not posting:
                continue
            df = len(posting)
            idf = math.log((self.n - df + 0.5) / (df + 0.5) + 1.0)
            for i, tf in posting:
                dl = self.doc_lens[i] or 1
                denom = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                out[i] = out.get(i, 0.0) + idf * (tf * (self.k1 + 1)) / denom
        return out

def compute_bm25_confidences(
    query_terms: list[str],
    candidate_contents: list[str],
    raw_scores: list[float],
) -> list[float]:
    """Calibrate relative BM25 scores by query coverage and term rarity.

    Max-normalization alone always gives the best lexical candidate a score of 1,
    even when it only matches a term shared by the entire candidate pool. This
    confidence keeps exact, discriminative matches strong while making ubiquitous
    or partial matches contribute less to hybrid retrieval.
    """
    if not query_terms or not candidate_contents:
        return [0.0] * len(candidate_contents)
    if len(raw_scores) != len(candidate_contents):
        raise ValueError("raw_scores and candidate_contents must have the same length")

    terms = set(query_terms)
    tokenized = [set(tokenize(content)) for content in candidate_contents]
    n = len(tokenized)
    df = {term: sum(term in tokens for tokens in tokenized) for term in terms}
    rare_match_idf = math.log((n - 0.5) / 1.5 + 1.0)
    max_raw = max(raw_scores, default=0.0)
    if rare_match_idf <= 0 or max_raw <= 0:
        return [0.0] * len(candidate_contents)

    confidences: list[float] = []
    for tokens, raw_score in zip(tokenized, raw_scores, strict=True):
        rarity_coverage = sum(
            min(
                math.log((n - df[term] + 0.5) / (df[term] + 0.5) + 1.0)
                / rare_match_idf,
                1.0,
            )
            for term in terms
            if term in tokens
        ) / len(terms)
        confidences.append((raw_score / max_raw) * rarity_coverage)
    return confidences


def score_and_rank(
    query_terms: list[str],
    candidates: list[tuple[str, str]],
) -> list[tuple[str, float]]:
    """Score (id, content) pairs and return them max-normalized and sorted by BM25 desc."""
    contents = [c for _, c in candidates]
    raw = compute_bm25_scores(query_terms, contents)
    max_s = max(raw) if raw else 0.0
    if max_s <= 0:
        norm = [0.0] * len(raw)
    else:
        norm = [s / max_s for s in raw]
    ranked = [(cid, norm[i]) for i, (cid, _) in enumerate(candidates)]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked
