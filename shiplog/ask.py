"""Lexical retrieval for ``shiplog ask`` -- answer an agent's specific question.

Where :mod:`shiplog.brief` produces a *fixed* digest ("what should I know before
touching this repo?"), ``ask`` answers a *specific* question an agent has right
now -- "Have we tried Redis for the cache layer?" -- by ranking log entries by
lexical relevance to the query and surfacing the highest-signal matches first.

Design goals (see issue #40):

- **Zero deps, no LLM, no network.** Pure-stdlib BM25-ish scoring so it works
  offline and in CI. The selection logic lives here as pure functions over
  :class:`~shiplog.models.Entry` lists, so ranking is unit-testable without the
  CLI and the rendering stays separate.
- **Dead-ends boosted.** The log's whole reason to exist is "what did we already
  rule out", so a matching ``deadend`` gets a score multiplier -- an equally
  relevant dead-end outranks a plain note.
- **One-line verdict.** A fast-parse summary ("Yes -- 2 dead-ends, 1 decision
  match") so an agent can branch on the answer without reading every hit.

Scoring model
-------------
Each entry is tokenized over its searchable text (``summary`` + ``why`` +
``files`` + ``tags``). We score the query against the corpus with a compact
BM25 (Okapi) ranking function -- standard IDF term weighting with document-length
normalization -- then apply a per-type boost so dead-ends float up. Entries with
a zero score (no query term present) are dropped: ``ask`` returns *matches*, not
the whole log.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .links import is_link
from .models import Entry, EntryType

# BM25 free parameters. ``k1`` controls term-frequency saturation; ``b`` controls
# how strongly document length normalizes the score. These are the textbook
# Okapi defaults and behave well for short, ADR-sized documents.
_BM25_K1 = 1.5
_BM25_B = 0.75

# Per-type score multipliers. Dead-ends are the headline signal ("already ruled
# out"), decisions are the rationale you most want, attempts/notes are context.
# A boost > 1 lifts an equally-relevant entry of that type above the rest.
_TYPE_BOOST: dict[str, float] = {
    EntryType.DEADEND.value: 1.6,
    EntryType.DECISION.value: 1.25,
    EntryType.ATTEMPT.value: 1.0,
    EntryType.NOTE.value: 0.9,
}

# Tokenizer: lowercase alphanumeric runs (keeps ``redis``, ``cache2``, ``v3``).
# Deliberately simple + stdlib-only; no stemming so scoring stays predictable.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Split ``text`` into lowercase alphanumeric tokens (no stemming/stopwords)."""
    return _TOKEN_RE.findall(text.lower())


def entry_text(entry: Entry) -> str:
    """Concatenate an entry's searchable fields into one blob for tokenizing.

    Includes ``summary``, ``why``, ``files``, and ``tags`` -- the fields an agent
    would phrase a question against. ``id``/``author``/``sha`` are intentionally
    excluded so opaque identifiers don't create phantom matches.
    """
    parts = [entry.summary or "", entry.why or ""]
    parts.extend(entry.files or [])
    parts.extend(entry.tags or [])
    return " ".join(parts)


@dataclass(slots=True)
class Hit:
    """One scored, ranked search result.

    Attributes:
        entry: The matched log entry.
        score: Final relevance score (BM25 * type boost); higher is better.
    """

    entry: Entry
    score: float


@dataclass(slots=True)
class AskResult:
    """The ranked answer to a question, ready for rendering.

    Attributes:
        query: The original question text.
        hits: Scored matches, highest score first, already limited.
        total_matches: How many entries matched before the ``--limit`` cap.
        deadends: Number of matching dead-ends (across all matches, pre-cap).
        decisions: Number of matching decisions (across all matches, pre-cap).
    """

    query: str
    hits: list[Hit]
    total_matches: int
    deadends: int
    decisions: int

    @property
    def truncated(self) -> int:
        """How many matches were dropped by the limit (0 if none)."""
        return max(0, self.total_matches - len(self.hits))

    def verdict(self) -> str:
        """One-line, agent-parseable summary of the answer.

        Leads with a yes/no on whether anything matched, then the dead-end and
        decision counts -- the two signals an agent branches on ("has this been
        ruled out? was it decided?").
        """
        if self.total_matches == 0:
            return f"No matches for {self.query!r} — nothing logged about this yet."
        de = self.deadends
        dec = self.decisions
        pieces: list[str] = []
        if de:
            pieces.append(f"{de} dead-end{'s' if de != 1 else ''}")
        if dec:
            pieces.append(f"{dec} decision{'s' if dec != 1 else ''}")
        other = self.total_matches - de - dec
        if other:
            pieces.append(f"{other} other")
        detail = ", ".join(pieces) if pieces else f"{self.total_matches} matching"
        lead = "Yes" if (de or dec) else "Maybe"
        return f"{lead} — {detail} match."


def _idf(n_docs: int, df: int) -> float:
    """BM25 inverse document frequency for a term in ``df`` of ``n_docs`` docs.

    Uses the standard non-negative Okapi variant; a term in *every* doc scores 0
    (carries no discriminating signal) rather than going negative.
    """
    return math.log(1 + (n_docs - df + 0.5) / (df + 0.5))


def score_entries(entries: list[Entry], query: str) -> list[Hit]:
    """Score ``entries`` against ``query`` and return matches, highest score first.

    Pure BM25 over the entry corpus, then a per-type boost (dead-ends up). Only
    entries with a positive score are returned -- ``ask`` surfaces matches, not
    the whole log. Ties break newest-first, then by original order, so equally
    relevant entries stay deterministic.

    Args:
        entries: Candidate entries (already filtered; links excluded upstream).
        query: The natural-language question.

    Returns:
        A list of :class:`Hit`, sorted by descending final score.
    """
    q_terms = tokenize(query)
    if not q_terms or not entries:
        return []

    # Tokenize the corpus once; keep per-doc token lists for length + frequency.
    doc_tokens: list[list[str]] = [tokenize(entry_text(e)) for e in entries]
    doc_lens = [len(toks) for toks in doc_tokens]
    n_docs = len(entries)
    avg_len = (sum(doc_lens) / n_docs) if n_docs else 0.0

    # Document frequency per *query* term (how many docs contain it at least once).
    unique_q = set(q_terms)
    df: dict[str, int] = {t: 0 for t in unique_q}
    for toks in doc_tokens:
        present = set(toks)
        for t in unique_q:
            if t in present:
                df[t] += 1

    idf = {t: _idf(n_docs, df[t]) for t in unique_q}

    hits: list[Hit] = []
    for entry, toks, dlen in zip(entries, doc_tokens, doc_lens, strict=True):
        if dlen == 0:
            continue
        # Term frequencies for this doc, restricted to query terms.
        tf: dict[str, int] = {}
        for tok in toks:
            if tok in unique_q:
                tf[tok] = tf.get(tok, 0) + 1
        if not tf:
            continue
        norm = _BM25_B * (dlen / avg_len) if avg_len else 0.0
        score = 0.0
        for term, freq in tf.items():
            denom = freq + _BM25_K1 * (1 - _BM25_B + norm)
            score += idf[term] * (freq * (_BM25_K1 + 1)) / denom
        if score <= 0:
            continue
        boost = _TYPE_BOOST.get(entry.type.value, 1.0)
        hits.append(Hit(entry=entry, score=score * boost))

    # Highest score first; ties newest-first (ts desc), then stable input order.
    hits.sort(key=lambda h: h.entry.ts, reverse=True)
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def build_ask(
    entries: list[Entry],
    query: str,
    *,
    limit: int = 5,
) -> AskResult:
    """Rank ``entries`` against ``query`` into an :class:`AskResult`.

    Link records are excluded (they annotate other entries, not standalone
    answers). Dead-end/decision counts are computed over *all* matches (pre-cap)
    so the verdict reflects the true picture even when ``--limit`` hides the tail.

    Args:
        entries: Candidate entries (callers apply ``--type``/``--file``/``--since``
            filters first; this only strips link records).
        query: The natural-language question.
        limit: Max hits to return (``<= 0`` means no cap).

    Returns:
        An :class:`AskResult` with ranked hits, counts, and a one-line verdict.
    """
    candidates = [e for e in entries if not is_link(e)]
    ranked = score_entries(candidates, query)
    total = len(ranked)
    deadends = sum(1 for h in ranked if h.entry.type.value == EntryType.DEADEND.value)
    decisions = sum(1 for h in ranked if h.entry.type.value == EntryType.DECISION.value)
    hits = ranked if limit <= 0 else ranked[:limit]
    return AskResult(
        query=query,
        hits=hits,
        total_matches=total,
        deadends=deadends,
        decisions=decisions,
    )


def ask_to_dict(result: AskResult) -> dict:
    """Serialize an :class:`AskResult` to the stable ``--json`` shape.

    Keys: ``query``, ``verdict`` (the one-liner), ``hits`` (array of
    ``{score, entry}``, ranked), ``total``/``shown``/``truncated`` accounting,
    and ``deadends``/``decisions`` counts for fast agent branching.
    """
    return {
        "query": result.query,
        "verdict": result.verdict(),
        "hits": [
            {"score": round(h.score, 4), "entry": h.entry.to_dict()}
            for h in result.hits
        ],
        "total": result.total_matches,
        "shown": len(result.hits),
        "truncated": result.truncated,
        "deadends": result.deadends,
        "decisions": result.decisions,
    }
