"""
fts_index.py — SQLite FTS5 keyword side index for fleet-memory hybrid search.

Mirrors mem0/Qdrant memories into a local FTS5 table so exact lexical tokens
(IPs, CT ids, Vault paths, env keys, error strings) are retrievable by BM25
keyword match. Qdrant stays the semantic primary and the source of truth; this
index is a derived mirror, rebuilt from Qdrant on demand (see rebuild_fts.py).

Hybrid merge uses Reciprocal Rank Fusion (RRF), which is scale-free and needs
no score normalization between cosine similarity and BM25.
"""
import json
import logging
import re
import sqlite3
import threading

log = logging.getLogger("memory-mcp.fts")

# RRF constant. 60 is the standard value from the original RRF paper
# (Cormack et al. 2009). Larger k flattens rank influence; smaller k sharpens it.
RRF_K = 60

# FTS5 MATCH treats many characters as operators/syntax. For keyword recall over
# identifiers (IPs, paths, CT-ids, error strings) we tokenize the query into bare
# words and OR them as quoted phrases, so "10.98.0.136" or "secret/foo" survive.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:/@-]+")


_ALNUM_RUN_RE = re.compile(r"[A-Za-z]+|[0-9]+")


def _identifier_variants(token: str) -> list[str]:
    """Spelling variants for an identifier-shaped token so FTS5 retrieval is
    punctuation-insensitive end-to-end (2026-08 review: a "CT314" MATCH only
    returned the compact spelling; rows saying "CT-314" or "CT 314" were never
    retrieved, so per-hit admission had nothing to admit — and vice versa).
    Only tokens mixing letters and digits expand; prose is untouched:
      CT314  -> phrase "CT 314"  (matches CT-314 / CT 314 under unicode61)
      CT-314 -> CT314            (matches the compact spelling)"""
    if not (any(c.isdigit() for c in token) and any(c.isalpha() for c in token)):
        return []
    runs = _ALNUM_RUN_RE.findall(token)
    if len(runs) < 2:
        return []
    variants = [" ".join(runs)]                  # split phrase
    squashed = "".join(runs)
    if squashed != token:
        variants.append(squashed)                # compact spelling
    return variants


def _to_match_query(query: str) -> str:
    tokens = _TOKEN_RE.findall(query or "")
    if not tokens:
        return ""
    # Quote each token to neutralize FTS5 operator chars; OR them for recall.
    # Identifier-shaped tokens additionally OR in their punctuation variants.
    terms = []
    for t in tokens:
        for v in [t, *_identifier_variants(t)]:
            terms.append('"%s"' % v.replace('"', '""'))
    return " OR ".join(terms)


# --- fusion strategies -------------------------------------------------------
# The 2026-08 recall bench showed classic RRF subtracts recall on paraphrased
# queries: with BM25 target-hit@5 at 18% on a constructed low-overlap stress
# set (NOT a precision figure), keyword votes there are mostly noise that
# outvotes good semantic ranks — fusion dragged 7-10 semantic hits per 50
# probes out of the top-5 while rescuing <=1. Strategies:
#   "classic"  — every keyword vote counts fully (original behavior, default).
#   "coverage" — each keyword vote scaled by the specificity-weighted fraction
#                of query tokens its text actually matches; BM25 votes fade
#                smoothly on paraphrased queries, no hard cutoff.
#   "gated"    — keyword arm votes only when the query carries identifier-like
#                tokens (digits, dots/slashes/colons/underscores, ALL-CAPS
#                codes) or a keyword hit contains the query verbatim;
#                otherwise semantic-only. Crude but predictable — matches the
#                FTS arm's original purpose (exact IDs/IPs/paths/error strings).
#   "gated_coverage" — gate as predicate AND coverage as vote weight when the
#                gate opens. Bare acronyms (MCP, CLI, URL) open the gate like
#                any ALL-CAPS token, but hits that barely match the query get
#                damped instead of voting fully — the failure mode the bench
#                caught with "gated" alone.
#   "per_hit"  — per-hit identifier admission (2026-08 external review design):
#                extract genuine identifier tokens from the query (boundary
#                punctuation stripped first, so "fail." is prose), admit ONLY
#                keyword hits that contain at least one of them on unicode61
#                token boundaries, and give admitted hits a FULL vote — the
#                coverage/61-vs-1/65 arithmetic of damped votes makes
#                keyword-only rescue nearly impossible, which defeats the FTS
#                arm's purpose. Non-admitted hits are dropped, not damped.

FUSION_STRATEGIES = ("classic", "coverage", "gated", "gated_coverage", "per_hit")

_IDENTIFIER_CHAR_RE = re.compile(r"[0-9./:@_]")
_ALLCAPS_RE = re.compile(r"^[A-Z][A-Z0-9_-]{2,}$")
_BOUNDARY_PUNCT = "._:/@-"
# unicode61-style subtokens: FTS5's unicode61 tokenizer splits on any
# non-alphanumeric char, so "CT-314", "CT 314" and "CT314"-adjacent forms all
# compare through the same alnum-run lens.
_SUBTOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _subtokens(text: str) -> list[str]:
    return [t.lower() for t in _SUBTOKEN_RE.findall(text or "")]


_DOTFILE_RE = re.compile(r"^\.[A-Za-z0-9_][A-Za-z0-9._-]*$")   # .env, .gitignore
_ABSPATH_RE = re.compile(r"^/[A-Za-z0-9._-][A-Za-z0-9._/-]*$")  # /etc, /opt/memory-mcp


def identifier_tokens(query: str) -> list[str]:
    """Genuine identifier tokens from `query`.

    Supported identifier grammar (ASCII only — non-ASCII identifiers are
    deliberately OUT OF SCOPE: both this classifier and FTS5's unicode61
    interplay were only validated for ASCII, so Cyrillic/accented identifiers
    fall through to the semantic arm rather than being half-handled):
      - letter+digit mixes                  CT356, NU1701, qwen3
      - interior [._:@] punctuation         MEMORY_NEEDS_WHY, server.py,
                                            10.98.0.136, user@host
      - hyphen/slash compounds ONLY with a digit, a mixed-case signal, or
        >= 3 slash-joined segments          CT-314, e-Racun,
                                            secret/infra/gitea; purely
                                            alphabetic 2-part compounds
                                            (long-term, open-source,
                                            read/write, and/or) are prose
      - pure numbers of >= 3 digits         356, 8800 ("top 5" stays prose)
      - dotfiles and absolute paths         .env, /etc/agentry — the leading
                                            dot/slash is meaningful and kept
    Boundary punctuation is stripped BEFORE classifying (so "fail." is prose),
    except when the token is a dotfile/absolute path per above. Bare acronyms
    (MCP, CLI, URL) deliberately do NOT qualify."""
    out = []
    for raw in _TOKEN_RE.findall(query or ""):
        tok = raw.rstrip(_BOUNDARY_PUNCT)
        if _DOTFILE_RE.match(tok) or _ABSPATH_RE.match(tok):
            out.append(tok)
            continue
        tok = tok.lstrip(_BOUNDARY_PUNCT)
        if not tok:
            continue
        has_digit = any(c.isdigit() for c in tok)
        has_alpha = any(c.isalpha() for c in tok)
        mixed_case = any(c.isupper() for c in tok) and any(c.islower() for c in tok)
        interior_strong = any(c in "._:@" for c in tok)
        interior_weak = any(c in "-/" for c in tok)
        if (
            (has_digit and has_alpha)
            or interior_strong
            or (interior_weak and (has_digit or mixed_case or tok.count("/") >= 2))
            or (has_digit and not has_alpha and len(tok) >= 3)
        ):
            out.append(tok)
    return out


def _contains_subtoken_seq(text_subtokens: list[str], ident: str) -> bool:
    """True when the identifier appears in the pre-tokenized text on token
    boundaries, punctuation-insensitively: consecutive text subtokens joined
    together must equal the identifier with its own punctuation squashed. So
    "CT314" finds "CT314", "CT-314" and "CT 314"; "10.98.0.136" finds itself
    inside "10.98.0.136:8765"; but "cat" never finds "concatenate" and "CT314"
    never finds "CT3141"."""
    need = _subtokens(ident)
    if not need:
        return False
    target = "".join(need)
    max_window = len(need) + 2
    n = len(text_subtokens)
    for i in range(n):
        joined = ""
        for j in range(i, min(i + max_window, n)):
            joined += text_subtokens[j]
            if joined == target:
                return True
            if len(joined) >= len(target):
                break
    return False


def admit_keyword_hits(query: str, keyword: list[dict]) -> list[dict]:
    """per_hit admission: keep only keyword hits containing at least one
    genuine query identifier on unicode61 token boundaries. No identifiers in
    the query -> no keyword votes (semantic-only)."""
    idents = identifier_tokens(query)
    if not idents:
        return []
    admitted = []
    for item in keyword:
        toks = _subtokens(str(item.get("memory") or ""))
        if any(_contains_subtoken_seq(toks, ident) for ident in idents):
            admitted.append(item)
    return admitted


def _token_weight(token: str) -> float:
    """Specificity proxy without corpus stats: longer tokens carry more mass;
    identifier-shaped tokens (digits, dots, paths, env-style caps) carry
    double. Stopword-length tokens weigh little by construction."""
    weight = float(len(token))
    if _IDENTIFIER_CHAR_RE.search(token) or _ALLCAPS_RE.match(token):
        weight *= 2.0
    return weight


def keyword_coverage(query: str, text: str) -> float:
    """Fraction (0..1) of the query's specificity-weighted token mass present
    in `text`. Case-insensitive substring per token, mirroring how unicode61
    FTS matches "10.98.0.136" inside "10.98.0.136:8765". Pure function."""
    tokens = _TOKEN_RE.findall(query or "")
    if not tokens:
        return 0.0
    hay = (text or "").lower()
    total = sum(_token_weight(t) for t in tokens)
    matched = sum(_token_weight(t) for t in tokens if t.lower() in hay)
    return matched / total if total else 0.0


def query_has_identifier(query: str) -> bool:
    """True when the query carries at least one identifier-like token —
    digits, dots/slashes/colons/underscores (IPs, CT-ids, paths, env keys) or
    an ALL-CAPS code such as MEMORY_NEEDS_WHY."""
    return any(
        _IDENTIFIER_CHAR_RE.search(t) or _ALLCAPS_RE.match(t)
        for t in _TOKEN_RE.findall(query or "")
    )


class FtsIndex:
    """Thread-safe FTS5 side index. All public methods are best-effort and never
    raise — a failing keyword index must never break the primary memory path."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5("
            "memory_id UNINDEXED, namespace UNINDEXED, memory, "
            "metadata UNINDEXED, tokenize='unicode61')"
        )
        self._db.commit()

    def mirror(self, memory_id: str, namespace: str, text: str, metadata: dict | None) -> None:
        """Insert/refresh one memory in the index. Best-effort; never raises."""
        if not memory_id or not text:
            return
        try:
            with self._lock:
                self._db.execute("DELETE FROM mem_fts WHERE memory_id = ?", (memory_id,))
                self._db.execute(
                    "INSERT INTO mem_fts(memory_id, namespace, memory, metadata) "
                    "VALUES (?,?,?,?)",
                    (memory_id, namespace, text, json.dumps(metadata or {}, default=str)),
                )
                self._db.commit()
        except Exception as exc:  # noqa: BLE001 — best-effort side index
            log.warning("fts mirror failed id=%s: %s", memory_id, exc)

    def search(self, query: str, namespaces: list[str], limit: int) -> list[dict]:
        """BM25 keyword search restricted to `namespaces`. Returns an ordered list
        of {id, memory, keyword_score} best-first. Never raises."""
        match = _to_match_query(query)
        if not match or not namespaces:
            return []
        try:
            placeholders = ",".join("?" * len(namespaces))
            sql = (
                "SELECT memory_id, memory, bm25(mem_fts) AS rank "
                "FROM mem_fts "
                f"WHERE mem_fts MATCH ? AND namespace IN ({placeholders}) "
                "ORDER BY rank LIMIT ?"
            )
            with self._lock:
                rows = self._db.execute(sql, (match, *namespaces, limit)).fetchall()
            # bm25() returns negative scores where lower = better; surfaced raw.
            return [{"id": r[0], "memory": r[1], "keyword_score": r[2]} for r in rows]
        except Exception as exc:  # noqa: BLE001 — best-effort side index
            log.warning("fts search failed q=%r: %s", query, exc)
            return []

    def rebuild(self, rows) -> int:
        """Atomically replace the whole index from an iterable of
        (memory_id, namespace, text, metadata) tuples. Returns new row count."""
        payload = [
            (i, ns, t, json.dumps(m or {}, default=str))
            for i, ns, t, m in rows
            if i and t
        ]
        with self._lock:
            self._db.execute("BEGIN")
            self._db.execute("DELETE FROM mem_fts")
            self._db.executemany(
                "INSERT INTO mem_fts(memory_id, namespace, memory, metadata) "
                "VALUES (?,?,?,?)",
                payload,
            )
            self._db.commit()
        return self.count()

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT count(*) FROM mem_fts").fetchone()[0]


def rrf_merge(semantic: list[dict], keyword: list[dict], limit: int, k: int = RRF_K,
              strategy: str = "classic", query: str = "") -> list[dict]:
    """Reciprocal Rank Fusion of two ranked result lists.

    semantic: mem0 result dicts (have 'id', 'score'=cosine, 'memory', metadata...)
    keyword:  {'id','memory','keyword_score'} dicts from FtsIndex.search
    strategy: keyword-vote weighting — "classic" (full weight, default), or one
              of FUSION_STRATEGIES (see fusion strategies block above).
    query:    the original search query; required by every strategy except
              "classic", which ignores it.

    Returns merged mem0-shaped dicts with added rrf_score / semantic_score /
    keyword_score debug fields, best-first, truncated to `limit`. Each result is
    guaranteed a 'score' key (falls back to rrf_score for keyword-only hits) so
    existing consumers keep working.
    """
    # Strategy resolution is best-effort: a failure here must degrade to
    # classic fusion, never break search (same law as FtsIndex itself).
    kw_weight = None
    try:
        if strategy == "per_hit":
            keyword = admit_keyword_hits(query, keyword)
            # admitted hits are exact identifier matches — full vote, ranks
            # compacted to the admitted list so dropped hits free their slots.
        elif strategy in ("gated", "gated_coverage"):
            gate_open = query_has_identifier(query) or (
                bool(query)
                and any((query or "").lower() in str(i.get("memory") or "").lower()
                        for i in keyword)
            )
            if not gate_open:
                keyword = []
            if strategy == "gated_coverage":
                kw_weight = lambda item: keyword_coverage(query, str(item.get("memory") or ""))  # noqa: E731
        elif strategy == "coverage":
            kw_weight = lambda item: keyword_coverage(query, str(item.get("memory") or ""))  # noqa: E731
    except Exception as exc:  # noqa: BLE001 — fusion must never break search
        log.warning("fusion strategy %r failed (%s) — falling back to classic", strategy, exc)
        kw_weight = None

    fused: dict = {}

    def _add(items: list[dict], score_key: str, out_field: str, weight_fn=None) -> None:
        for rank, item in enumerate(items):
            mid = item.get("id")
            if not mid:
                continue
            try:
                weight = weight_fn(item) if weight_fn else 1.0
            except Exception:  # noqa: BLE001 — weighting must never break search
                weight = 1.0
            if weight <= 0 and mid not in fused:
                # A zero vote must not mint a candidate: keyword-only rows with
                # rrf_score=0 would otherwise pad short result lists.
                continue
            entry = fused.setdefault(
                mid,
                {"item": item, "rrf_score": 0.0, "semantic_score": None, "keyword_score": None},
            )
            entry["rrf_score"] += weight / (k + rank + 1)
            if score_key in item:
                entry[out_field] = item[score_key]
            # Prefer the richer mem0 item (carries metadata) as the base record.
            if item.get("metadata") and not entry["item"].get("metadata"):
                entry["item"] = item

    _add(semantic, "score", "semantic_score")
    _add(keyword, "keyword_score", "keyword_score", kw_weight)

    out = []
    for mid, entry in fused.items():
        merged = dict(entry["item"])
        merged["id"] = mid
        merged["rrf_score"] = round(entry["rrf_score"], 6)
        merged["semantic_score"] = entry["semantic_score"]
        merged["keyword_score"] = entry["keyword_score"]
        merged.setdefault("score", merged["rrf_score"])
        out.append(merged)
    out.sort(key=lambda x: x["rrf_score"], reverse=True)
    return out[:limit]
