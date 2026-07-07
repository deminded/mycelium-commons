#!/usr/bin/env python3
"""
Associative memory v2 — Concept-level PPR with residual salience scoring.

Changes over v1:
  1. RESIDUAL PPR: salience = rank(ppr_query - ppr_uniform). Hub bias removed
     structurally: uniform-PPR captures what PageRank gives to a node regardless
     of the query; subtracting it isolates the query contribution.
  2. CONCEPT-LEVEL NODES: files split into chunks by ## headings (or paragraphs).
     Each chunk = separate graph node. Intra-file edges: chunk→file + chain between
     adjacent chunks. Cross-file edges: wikilinks (file↔file). Result: concepts
     like grибница / отчуждение / Hope become first-class nodes.
  3. LEX CLEANUP: body-only TF-IDF (frontmatter excluded); lex_sim_with_filter
     requires ≥2 shared significant terms (IDF > IDF_MIN_THRESHOLD) to avoid
     one-weak-word false positives.
  4. FRAGMENTATION FIX: add_auto_edges() connects small isolated components
     to the main graph via TF-IDF cosine similarity.
"""

import os
import sys
import re
import math
import json
import pickle
import time as _time
from pathlib import Path
from collections import defaultdict

import networkx as nx


# ---------------------------------------------------------------------------
# Paths — fork config
# ---------------------------------------------------------------------------
# Форкающий агент указывает СВОЙ субстрат через env, не правя код: пакет
# отдаётся форкаемым, а не «под мой носитель». Дефолты — субстрат Арета.
#   MYCELIUM_MEMORY_DIR       — каталог memory/ (файлы-факты)
#   MYCELIUM_REFLECTIONS_DIR  — каталог рефлексий (Reflections-*/)

MEMORY_DIR = Path(os.environ.get(
    "MYCELIUM_MEMORY_DIR",
    "/home/claude-user/.claude/projects/-home-claude-user/memory"))
REFLECTIONS_DIR = Path(os.environ.get(
    "MYCELIUM_REFLECTIONS_DIR",
    "/opt/workspace/vault/Reflections-claude-user"))

# Chunking
MIN_CHUNK_TOKENS = 25       # drop trivially small chunks

# Auto-edges
AUTO_EDGE_TOP_K = 3
AUTO_EDGE_MIN_SIM = 0.10    # cosine threshold for semantic auto-edge

# PPR
ALPHA = 0.85

# Seed construction: softmax temperature + uniform floor
SEED_SOFTMAX_TEMP = 3.0
SEED_UNIFORM_BETA = 0.10    # 10% uniform floor prevents full teleport to one node

# Lex quality gate
IDF_MIN_THRESHOLD = 2.5     # term must have IDF >= this to count as "significant"
MIN_SIGNIFICANT_SHARED = 2  # require this many significant shared terms

# Cache
CACHE_PATH = Path(__file__).parent / "assoc_cache.pkl"

# Associate API — tune these constants to adjust recall behaviour
ASSOC_TOP_K = 3
ASSOC_MIN_RESIDUAL = 0.25   # min (residual / max_residual); below → noise
ASSOC_BLOCKLIST = {"MEMORY", "CLAUDE", "MEMORY.md", "CLAUDE.md"}  # служебные индексы-мегахабы: structural lift даже на нерелевантном запросе

# Degree-gate (L5 шаг 4): мегахаб при слабом seed всплывает чистой структурой
# (query-PPR≈uniform). Различитель — КОНЪЮНКЦИЯ: топ-степень И низкий own_lex.
# Ни степень одна (убьёт легитимные presence_surfaces/projects_state на
# релевантных запросах), ни lex один (не отличит хаб от косвенной ассоциации)
# не работают — калибровка на 17 запросах, зазор own_lex [0.035, 0.091].
HUB_DEGREE_PCTL = 99        # hub = узел в топ-проценте по степени (порог динамический, не абсолютный)
HUB_OWN_LEX_TAU = 0.06      # хаб пропускается, только если own_lex_raw ниже — иначе попадание легитимно


# ---------------------------------------------------------------------------
# Stopwords & tokenizer
# ---------------------------------------------------------------------------

RU_STOPWORDS = {
    "и", "в", "на", "с", "по", "для", "не", "это", "как", "но", "а", "к",
    "из", "от", "до", "за", "при", "без", "об", "под", "над", "между",
    "через", "после", "перед", "о", "или", "то", "же", "всё", "все", "что",
    "он", "она", "они", "его", "её", "их", "мой", "свой", "так", "если",
    "уже", "ещё", "только", "очень", "даже", "более", "такой", "где",
    "когда", "который", "которые", "быть", "есть", "был", "была", "были",
    "будет", "нет", "ни", "также", "при", "про", "этот", "это", "эту",
    "эти", "себя", "того", "тем", "том", "тот", "чем", "её", "—", "–",
}

EN_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "not",
    "no", "so", "if", "it", "its", "this", "that", "these", "those",
    "i", "we", "you", "he", "she", "they", "my", "our", "your", "his",
    "her", "their", "what", "which", "who", "when", "where", "how", "all",
}

STOPWORDS = RU_STOPWORDS | EN_STOPWORDS


def tokenize(text: str) -> list[str]:
    """Lowercased Cyrillic/Latin tokens >=3 chars, no stopwords.
    Regex [a-zа-яё]{3,} already strips all digits and punctuation."""
    tokens = re.findall(r'[a-zа-яё]{3,}', text.lower())
    return [t for t in tokens if t not in STOPWORDS]


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (meta_dict, body_text). Strips YAML frontmatter block."""
    meta = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm_block = text[3:end]
            body = text[end + 4:]
            for line in fm_block.splitlines():
                m = re.match(r'^(\w+):\s*(.+)$', line.strip())
                if m:
                    meta[m.group(1)] = m.group(2).strip()
    return meta, body


# ---------------------------------------------------------------------------
# Chunking by ## headings (fallback to paragraphs)
# ---------------------------------------------------------------------------

def split_into_chunks(body: str) -> list[tuple[str, str]]:
    """
    Split body into (slug, text) pairs by ## or ### headings.
    Falls back to paragraph chunking if no headings found.
    """
    parts = re.split(r'\n(#{1,3} [^\n]+)', body)

    chunks: list[tuple[str, str]] = []
    current_slug = "intro"
    current_text = ""

    for part in parts:
        if re.match(r'^#{1,3} ', part):
            if current_text.strip() and len(tokenize(current_text)) >= MIN_CHUNK_TOKENS:
                chunks.append((current_slug, current_text.strip()))
            heading = re.sub(r'^#{1,3} ', '', part).strip()
            # build a safe slug
            slug = re.sub(r'[^\w\s-]', '', heading.lower())
            slug = re.sub(r'[\s_-]+', '_', slug)[:40].strip('_')
            current_slug = slug if slug else "section"
            current_text = ""
        else:
            current_text += part

    if current_text.strip() and len(tokenize(current_text)) >= MIN_CHUNK_TOKENS:
        chunks.append((current_slug, current_text.strip()))

    # If heading-split gave only 1 chunk, try paragraph split
    if len(chunks) <= 1:
        single_body = chunks[0][1] if chunks else body
        paras = [p.strip() for p in re.split(r'\n\n+', single_body) if p.strip()]
        para_chunks = [(f"p{i}", p) for i, p in enumerate(paras)
                       if len(tokenize(p)) >= MIN_CHUNK_TOKENS]
        if len(para_chunks) >= 2:
            chunks = para_chunks

    # Always ensure at least one chunk
    if not chunks:
        text = body.strip()
        if text:
            chunks = [("main", text)]

    return chunks


# ---------------------------------------------------------------------------
# Node loading with concept-level splitting
# ---------------------------------------------------------------------------

def load_nodes() -> dict:
    """
    Returns node_id -> {
        path, source, kind (file|chunk), parent,
        name, aliases, text (body-only)
    }
    File nodes aggregate chunks; wikilinks are extracted from file nodes only.
    Chunk nodes hold the actual semantic content for TF-IDF.
    """
    nodes = {}

    def _process(path: Path, source: str) -> None:
        raw = path.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_frontmatter(raw)
        stem = path.stem
        prefix = "mem:" if source == "memory" else "ref:"

        if source == "memory":
            canonical = meta.get("name", "").strip() or stem
            aliases = {canonical, stem}
            if meta.get("title"):
                aliases.add(meta["title"].strip())
        else:
            canonical = stem
            aliases = {stem}
            if meta.get("title"):
                aliases.add(meta["title"].strip())

        file_id = prefix + stem

        # File node: used for wikilink resolution and as parent
        nodes[file_id] = {
            "path": str(path),
            "source": source,
            "kind": "file",
            "parent": None,
            "name": canonical,
            "aliases": list(aliases),
            "text": body,           # body-only for TF-IDF
        }

        # Chunk nodes: semantic blocks within this file
        chunks = split_into_chunks(body)
        for slug, chunk_text in chunks:
            chunk_id = file_id + "#" + slug
            # Deduplicate slugs within a file
            if chunk_id in nodes:
                chunk_id = chunk_id + "_2"
            nodes[chunk_id] = {
                "path": str(path),
                "source": source,
                "kind": "chunk",
                "parent": file_id,
                "name": f"{canonical}/{slug}",
                "aliases": [],      # chunks don't resolve wikilinks
                "text": chunk_text,
            }

    for path in sorted(MEMORY_DIR.glob("*.md")):
        _process(path, "memory")

    for path in sorted(REFLECTIONS_DIR.glob("*.md")):
        _process(path, "reflection")

    return nodes


# ---------------------------------------------------------------------------
# Wikilink extraction
# ---------------------------------------------------------------------------

def extract_wikilinks(text: str) -> list[str]:
    """[[link]] and [[link|display]] -> list of link targets."""
    return [r.split("|")[0].strip() for r in re.findall(r'\[\[([^\]]+)\]\]', text)]


# ---------------------------------------------------------------------------
# Graph building
# ---------------------------------------------------------------------------

def build_graph(nodes: dict) -> tuple[nx.Graph, int]:
    """
    Edges:
      (a) intra-file: chunk ↔ parent file; adjacent chunks chained
      (b) cross-file: [[wikilinks]] between file nodes
    Returns (G, dangling_link_count).
    """
    # Alias lookup: only file nodes participate in wikilink resolution
    alias_to_node: dict[str, str] = {}
    for nid, data in nodes.items():
        if data["kind"] == "file":
            for alias in data["aliases"]:
                key = alias.lower().strip()
                if key:
                    alias_to_node[key] = nid

    G = nx.Graph()
    for nid in nodes:
        G.add_node(nid)

    # (a) Intra-file edges
    file_chunks: dict[str, list[str]] = defaultdict(list)
    for nid, data in nodes.items():
        if data["kind"] == "chunk":
            parent = data["parent"]
            G.add_edge(nid, parent)
            file_chunks[parent].append(nid)

    for parent_id, chunk_ids in file_chunks.items():
        for i in range(len(chunk_ids) - 1):
            G.add_edge(chunk_ids[i], chunk_ids[i + 1])

    # (b) Cross-file wikilinks (file ↔ file)
    dangling = 0
    for nid, data in nodes.items():
        if data["kind"] != "file":
            continue
        for link in extract_wikilinks(data["text"]):
            target = alias_to_node.get(link.lower().strip())
            if target is None:
                dangling += 1
            elif target != nid:
                G.add_edge(nid, target)

    return G, dangling


# ---------------------------------------------------------------------------
# TF-IDF
# ---------------------------------------------------------------------------

def build_tfidf(nodes: dict) -> tuple[dict, dict]:
    """TF-IDF from body-only node texts. Returns (tf_idf, idf)."""
    doc_tf: dict[str, dict[str, float]] = {}
    for nid, data in nodes.items():
        tokens = tokenize(data["text"])
        tf: dict[str, float] = defaultdict(float)
        for t in tokens:
            tf[t] += 1.0
        total = len(tokens) or 1
        doc_tf[nid] = {t: c / total for t, c in tf.items()}

    N = len(nodes)
    doc_freq: dict[str, int] = defaultdict(int)
    for tf in doc_tf.values():
        for term in tf:
            doc_freq[term] += 1

    idf = {term: math.log((N + 1) / (df + 1)) + 1.0 for term, df in doc_freq.items()}

    tf_idf = {nid: {term: doc_tf[nid][term] * idf[term] for term in doc_tf[nid]}
              for nid in doc_tf}
    return tf_idf, idf


def cosine_sim(vec_a: dict, vec_b: dict) -> float:
    common = set(vec_a) & set(vec_b)
    if not common:
        return 0.0
    dot = sum(vec_a[t] * vec_b[t] for t in common)
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def lex_sim_filtered(vec_q: dict, vec_doc: dict, idf: dict) -> float:
    """Cosine sim gated on ≥MIN_SIGNIFICANT_SHARED terms with IDF >= IDF_MIN_THRESHOLD.
    Eliminates one-common-weak-word false positives (57/71 in v1)."""
    common = set(vec_q) & set(vec_doc)
    significant = [t for t in common if idf.get(t, 0) >= IDF_MIN_THRESHOLD]
    if len(significant) < MIN_SIGNIFICANT_SHARED:
        return 0.0
    return cosine_sim(vec_q, vec_doc)


def query_vector(query: str, idf: dict) -> dict[str, float]:
    tokens = tokenize(query)
    tf: dict[str, float] = defaultdict(float)
    for t in tokens:
        tf[t] += 1.0
    total = len(tokens) or 1
    return {t: (c / total) * idf.get(t, 1.0) for t, c in tf.items()}


# ---------------------------------------------------------------------------
# Auto-edges for fragmentation reduction
# ---------------------------------------------------------------------------

def add_auto_edges(G: nx.Graph, nodes: dict, tf_idf: dict) -> int:
    """
    Connect small isolated components (size<=5) to the rest of the graph
    via top-AUTO_EDGE_TOP_K TF-IDF cosine neighbors above AUTO_EDGE_MIN_SIM.
    Candidates = nodes in small components.
    Targets = file nodes (avoid chunk-to-chunk cross-file clutter).
    Returns count of edges added.
    """
    components = list(nx.connected_components(G))
    small = {n for comp in components if len(comp) <= 5 for n in comp}
    candidates = list(small)

    # Potential targets: file nodes not in the same small component
    file_nodes = [n for n, d in nodes.items()
                  if d["kind"] == "file" and n not in small]

    added = 0
    for cand in candidates:
        vec_a = tf_idf.get(cand, {})
        if not vec_a:
            continue
        sims = []
        for target in file_nodes:
            if G.has_edge(cand, target):
                continue
            sim = cosine_sim(vec_a, tf_idf.get(target, {}))
            if sim >= AUTO_EDGE_MIN_SIM:
                sims.append((sim, target))
        sims.sort(reverse=True)
        for sim, target in sims[:AUTO_EDGE_TOP_K]:
            G.add_edge(cand, target, weight=sim, auto=True)
            added += 1

    return added


# ---------------------------------------------------------------------------
# PPR power iteration
# ---------------------------------------------------------------------------

def _ppr_power(
    G: nx.Graph,
    personalization: dict | None,
    alpha: float = ALPHA,
    max_iter: int = 300,
    tol: float = 1.0e-6,
) -> dict[str, float]:
    """Personalized PageRank via power iteration."""
    nodes_list = list(G.nodes())
    N = len(nodes_list)
    idx = {n: i for i, n in enumerate(nodes_list)}

    neighbors: list[list[int]] = [[] for _ in range(N)]
    for u, v in G.edges():
        neighbors[idx[u]].append(idx[v])
        neighbors[idx[v]].append(idx[u])

    if personalization:
        total = sum(personalization.values())
        p = [personalization.get(n, 0.0) / total for n in nodes_list]
    else:
        p = [1.0 / N] * N

    r = [1.0 / N] * N
    for _ in range(max_iter):
        new_r = [0.0] * N
        for i in range(N):
            nbrs = neighbors[i]
            if nbrs:
                contrib = r[i] / len(nbrs)
                for j in nbrs:
                    new_r[j] += alpha * contrib
            else:
                for j in range(N):
                    new_r[j] += alpha * r[i] * p[j]
            new_r[i] += (1 - alpha) * p[i]
        err = sum(abs(new_r[i] - r[i]) for i in range(N))
        r = new_r
        if err < tol:
            break
    return {nodes_list[i]: r[i] for i in range(N)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def softmax(scores: list[float], temperature: float = 1.0) -> list[float]:
    if not scores:
        return []
    scaled = [s / temperature for s in scores]
    max_s = max(scaled)
    exp_s = [math.exp(s - max_s) for s in scaled]
    total = sum(exp_s)
    return [e / total for e in exp_s]


def rank_normalize(values: list[float]) -> list[float]:
    """Percentile rank in [0, 1]. Robust to outlier distributions."""
    n = len(values)
    if n == 0:
        return []
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * n
    for rank, (orig_idx, _) in enumerate(indexed):
        ranks[orig_idx] = rank / (n - 1) if n > 1 else 0.5
    return ranks


def pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return num / (da * db) if da > 0 and db > 0 else 0.0


# ---------------------------------------------------------------------------
# Main PPR run — residual scoring
# ---------------------------------------------------------------------------

def run_ppr_v2(
    G: nx.Graph,
    nodes: dict,
    tf_idf: dict,
    idf: dict,
    query: str,
    top_seeds: int = 15,
) -> dict:
    """
    Compute residual PPR and return:
        node_id -> {ppr_query, ppr_uniform, residual, residual_rank,
                    lex_sim, lex_rank, salience, label}

    salience = residual_rank - lex_rank
      Positive = node activated by graph structure above what lexis explains.
    """
    q_vec = query_vector(query, idf)

    # Raw cosine lex (single-term ok) — used for salience comparison and display
    lex_raw: dict[str, float] = {
        nid: cosine_sim(q_vec, tf_idf.get(nid, {})) for nid in nodes
    }

    # Quality-filtered lex (≥2 significant shared terms) — used only for seeds.
    # Prevents one-weak-word nodes from dominating the restart vector.
    lex_filtered: dict[str, float] = {
        nid: lex_sim_filtered(q_vec, tf_idf.get(nid, {}), idf) for nid in nodes
    }

    # Seeds from filtered lex; fall back to raw if filter is too aggressive
    seed_lex = lex_filtered if any(v > 0 for v in lex_filtered.values()) else lex_raw
    sorted_lex = sorted(seed_lex.items(), key=lambda x: x[1], reverse=True)
    seeds = [(nid, s) for nid, s in sorted_lex if s > 0][:top_seeds]

    N = G.number_of_nodes()
    if seeds:
        nids, scores = zip(*seeds)
        sm = softmax(list(scores), temperature=SEED_SOFTMAX_TEMP)
        personalization: dict | None = {nid: SEED_UNIFORM_BETA / N for nid in nodes}
        for nid, w in zip(nids, sm):
            personalization[nid] += (1 - SEED_UNIFORM_BETA) * w
    else:
        personalization = None

    # Two PPR runs
    ppr_query = _ppr_power(G, personalization, alpha=ALPHA)
    ppr_uniform = _ppr_power(G, None, alpha=ALPHA)

    # Residual = query lift above structural baseline
    residual = {nid: ppr_query[nid] - ppr_uniform[nid] for nid in nodes}

    # Rank-normalize residual and RAW lex (not filtered).
    # Using raw lex here ensures the salience denominator is populated
    # (filtered lex leaves ~95% of nodes at 0, making rank differences meaningless).
    nids_list = list(nodes.keys())
    residual_ranks = rank_normalize([residual[n] for n in nids_list])
    lex_ranks = rank_normalize([lex_raw[n] for n in nids_list])

    results = {}
    for i, nid in enumerate(nids_list):
        d = nodes[nid]
        kind_tag = "chnk" if d["kind"] == "chunk" else d["source"][:3]
        label = f"[{kind_tag}] {d['name']}"
        rr = residual_ranks[i]
        lr = lex_ranks[i]
        # salience: graph-structural lift that exceeds lexical explanation.
        # Clamped to 0 for negative residual (node suppressed by query, not surprising).
        sal = (rr - lr) if residual[nid] > 0 else max(0.0, rr - lr) - 1.0
        results[nid] = {
            "ppr_query": ppr_query[nid],
            "ppr_uniform": ppr_uniform[nid],
            "residual": residual[nid],
            "residual_rank": rr,
            "lex_sim": lex_raw[nid],
            "lex_filtered": lex_filtered[nid],
            "lex_rank": lr,
            "salience": sal,
            "label": label,
        }

    return results


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def top_n(results: dict, key: str, n: int = 12, reverse: bool = True) -> list:
    return sorted(results.items(), key=lambda x: x[1][key], reverse=reverse)[:n]


def print_table(title: str, rows: list, keys: list[str]) -> None:
    width = 78
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print('=' * width)
    hdr = f"{'#':>3}  {'Node':56}  " + "  ".join(f"{k:>10}" for k in keys)
    print(hdr)
    print("-" * len(hdr))
    for rank, (nid, data) in enumerate(rows, 1):
        label = data["label"][:55]
        vals = "  ".join(f"{data[k]:10.5f}" for k in keys)
        print(f"{rank:>3}  {label:56}  {vals}")


# ---------------------------------------------------------------------------
# Cache — precompute everything that is query-independent
# ---------------------------------------------------------------------------

def get_source_mtimes() -> dict:
    """Max mtime of .md files in each source dir — used to detect stale cache."""
    mtimes: dict[str, float] = {}
    for d in [MEMORY_DIR, REFLECTIONS_DIR]:
        if d.exists():
            mts = [p.stat().st_mtime for p in d.glob("*.md")]
            mtimes[str(d)] = max(mts) if mts else 0.0
    return mtimes


def first_significant_line(text: str, max_len: int = 80) -> str:
    """First readable non-heading line from node body — one-line essence."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('---'):
            continue
        line = re.sub(r'^\s*[-*]\s+', '', line)
        line = re.sub(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', r'\1', line)
        if len(line) >= 10:
            return line[:max_len] + ('…' if len(line) > max_len else '')
    return "(—)"


def build_cache() -> None:
    """Build and persist all query-independent structures to CACHE_PATH."""
    t0 = _time.perf_counter()
    print("Loading nodes...", end=" ", flush=True)
    nodes = load_nodes()
    print(f"{len(nodes)} nodes. Building graph...", end=" ", flush=True)
    G, _ = build_graph(nodes)
    print(f"{G.number_of_edges()} edges. TF-IDF...", end=" ", flush=True)
    tf_idf, idf = build_tfidf(nodes)
    print("Auto-edges...", end=" ", flush=True)
    add_auto_edges(G, nodes, tf_idf)
    print("Uniform PPR...", end=" ", flush=True)
    ppr_uniform = _ppr_power(G, None, alpha=ALPHA)
    print("done.")

    # Hub set for the degree-gate: percentile is computed per build, so the
    # threshold adapts as the memory graph grows (no hardcoded degree).
    degs = sorted((G.degree(n) for n in G.nodes()), reverse=True)
    hub_threshold = degs[int(len(degs) * (1 - HUB_DEGREE_PCTL / 100))] if degs else 0
    hub_nodes = {n for n in G.nodes() if G.degree(n) >= hub_threshold}

    cache = {
        "nodes": nodes,
        "G": G,
        "tf_idf": tf_idf,
        "idf": idf,
        "ppr_uniform": ppr_uniform,
        "hub_nodes": hub_nodes,
        "hub_degree_threshold": hub_threshold,
        "source_mtimes": get_source_mtimes(),
    }
    with open(CACHE_PATH, "wb") as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)

    elapsed = _time.perf_counter() - t0
    print(f"\nCache saved → {CACHE_PATH}  ({elapsed:.1f}s)")
    print(f"  nodes={len(nodes)}, "
          f"graph={G.number_of_nodes()}N/{G.number_of_edges()}E, "
          f"idf_terms={len(idf)}, "
          f"hubs={len(hub_nodes)} (deg>={hub_threshold})")
    print("Invalidate: delete the .pkl file or re-run --build-cache")


def load_cache() -> dict:
    """Load pre-built cache. Raises FileNotFoundError if missing."""
    with open(CACHE_PATH, "rb") as f:
        return pickle.load(f)


def associate(
    prompt: str,
    cache: dict | None = None,
    top_k: int = ASSOC_TOP_K,
    min_residual: float = ASSOC_MIN_RESIDUAL,
) -> list[dict]:
    """
    Fast associative recall from pre-built cache.

    One query-PPR run + residual scoring; cache provides everything else.
    Returns top_k dicts: {label, source, kind, snippet, residual, residual_norm}.
    Only nodes with residual/max_residual >= min_residual are returned.
    """
    if cache is None:
        cache = load_cache()

    nodes    = cache["nodes"]
    G        = cache["G"]
    tf_idf   = cache["tf_idf"]
    idf      = cache["idf"]
    ppr_unif = cache["ppr_uniform"]
    # Older caches lack hub_nodes → gate silently inactive until next --build-cache
    hub_nodes = cache.get("hub_nodes") or set()

    q_vec = query_vector(prompt, idf)

    # Quality-filtered seeds (≥2 significant shared terms); fall back to raw
    lex_filtered = {
        nid: lex_sim_filtered(q_vec, tf_idf.get(nid, {}), idf) for nid in nodes
    }
    lex_raw = {
        nid: cosine_sim(q_vec, tf_idf.get(nid, {})) for nid in nodes
    }
    seed_lex = lex_filtered if any(v > 0 for v in lex_filtered.values()) else lex_raw
    seeds = sorted(seed_lex.items(), key=lambda x: x[1], reverse=True)
    seeds = [(nid, s) for nid, s in seeds if s > 0][:15]

    N = G.number_of_nodes()
    if seeds:
        nids_s, scores_s = zip(*seeds)
        sm = softmax(list(scores_s), temperature=SEED_SOFTMAX_TEMP)
        personalization: dict | None = {nid: SEED_UNIFORM_BETA / N for nid in nodes}
        for nid, w in zip(nids_s, sm):
            personalization[nid] += (1 - SEED_UNIFORM_BETA) * w
    else:
        personalization = None

    ppr_query = _ppr_power(G, personalization, alpha=ALPHA)

    # Residual = query lift above uniform structural baseline
    residual = {nid: ppr_query[nid] - ppr_unif[nid] for nid in nodes}
    max_res = max(residual.values()) if residual else 0.0
    if max_res <= 0:
        return []

    results = []
    for nid, res in residual.items():
        norm = res / max_res
        if norm < min_residual:
            continue
        d = nodes[nid]
        if d["kind"] == "chunk":
            parent_name = nodes.get(d["parent"], {}).get("name", "?")
            base_name = parent_name
            label = f"{parent_name} › {d['name'].split('/')[-1]}"
        else:
            base_name = d["name"]
            label = d["name"]
        if base_name in ASSOC_BLOCKLIST:
            continue
        # Degree-gate: drop a top-degree node ONLY when the query barely touches
        # its own text — structural lift, not content. A hub hit with decent
        # own_lex is a legitimate answer and must pass (e.g. explicit queries).
        is_hub = nid in hub_nodes or (d["kind"] == "chunk" and d["parent"] in hub_nodes)
        if is_hub and lex_raw.get(nid, 0.0) < HUB_OWN_LEX_TAU:
            continue
        results.append({
            "nid": nid,
            "label": label,
            "source": d["source"],
            "kind": d["kind"],
            "snippet": first_significant_line(d["text"]),
            "residual": res,
            "residual_norm": norm,
        })

    results.sort(key=lambda x: x["residual"], reverse=True)
    return results[:top_k]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if "--build-cache" in sys.argv:
        build_cache()
        return

    query = (" ".join(a for a in sys.argv[1:] if not a.startswith("--"))
             or "семинар для учителей по работе с агентом и проектированию учебных программ, "
                "главный провал дизайна")

    print(f"\nQuery: «{query}»\n")

    # 1. Nodes with concept-level chunking
    print("Loading nodes (concept-level)...", end=" ", flush=True)
    nodes = load_nodes()
    file_cnt = sum(1 for d in nodes.values() if d["kind"] == "file")
    chunk_cnt = sum(1 for d in nodes.values() if d["kind"] == "chunk")
    print(f"{len(nodes)} total ({file_cnt} file + {chunk_cnt} chunk nodes).")

    # 2. Base graph
    print("Building graph (intra-file + wikilinks)...", end=" ", flush=True)
    G, dangling = build_graph(nodes)
    comps_pre = len(list(nx.connected_components(G)))
    iso_pre = len(list(nx.isolates(G)))
    print(f"{G.number_of_nodes()} nodes, {G.number_of_edges()} edges, "
          f"{dangling} dangling, {comps_pre} components, {iso_pre} isolates.")

    # 3. TF-IDF (body-only)
    print("Computing TF-IDF (body-only)...", end=" ", flush=True)
    tf_idf, idf = build_tfidf(nodes)
    print("done.")

    # 4. Auto-edges for isolated components
    print("Adding auto-edges for small components...", end=" ", flush=True)
    auto_added = add_auto_edges(G, nodes, tf_idf)
    comps_post = len(list(nx.connected_components(G)))
    iso_post = len(list(nx.isolates(G)))
    print(f"{auto_added} edges added. "
          f"Components: {comps_pre}→{comps_post}. "
          f"Isolates: {iso_pre}→{iso_post}.")

    # 5. PPR (query + uniform baseline)
    print("Running PPR (query + uniform)...", end=" ", flush=True)
    results = run_ppr_v2(G, nodes, tf_idf, idf, query)
    print("done.\n")

    # 6. Correlation metrics
    nids = list(nodes.keys())
    degrees = [G.degree(n) for n in nids]
    residuals = [results[n]["residual"] for n in nids]
    lex_sims = [results[n]["lex_sim"] for n in nids]
    saliences = [results[n]["salience"] for n in nids]

    corr_deg_res = pearson(degrees, residuals)
    corr_deg_sal = pearson(degrees, saliences)
    corr_deg_lex = pearson(degrees, lex_sims)
    print(f"corr(degree, residual)   = {corr_deg_res:.4f}  [v1 raw_ppr≈0.73, surprise≈0.88]")
    print(f"corr(degree, salience)   = {corr_deg_sal:.4f}")
    print(f"corr(degree, lex_sim)    = {corr_deg_lex:.4f}")

    # 7. Tables
    top_sal = top_n(results, "salience")
    top_res = top_n(results, "residual")
    top_lex = top_n(results, "lex_sim")

    print_table("TOP-12 SALIENCE (residual_rank − lex_rank) — associative hits",
                top_sal, ["residual", "lex_sim", "salience"])
    print_table("TOP-12 RESIDUAL (query lift above structural baseline)",
                top_res, ["residual", "ppr_query", "ppr_uniform"])
    print_table("TOP-12 LEXICAL (direct search baseline)",
                top_lex, ["lex_sim", "residual", "salience"])

    # 8. Associator value: salience-top but not lex-top
    lex_set = {nid for nid, _ in top_lex}
    pure_assoc = [(nid, data) for nid, data in top_sal if nid not in lex_set]
    print(f"\n{'=' * 78}")
    print("  SALIENCE top NOT in LEX top — true associative discoveries")
    print('=' * 78)
    if pure_assoc:
        for nid, data in pure_assoc:
            print(f"  {data['label']}")
    else:
        print("  (none — all salience-top nodes also lexically close)")

    # 9. Graph stats summary
    print(f"\n{'=' * 78}")
    print("  Graph stats")
    print('=' * 78)
    print(f"  {len(nodes)} nodes  /  {G.number_of_edges()} edges  /  {comps_post} components")
    print(f"  auto-edges: {auto_added}  |  isolates remaining: {iso_post}")

    # 10. Save JSON
    out = Path(__file__).parent / "results_v2.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "query": query,
            "graph": {
                "nodes": G.number_of_nodes(),
                "edges": G.number_of_edges(),
                "dangling": dangling,
                "components": comps_post,
                "isolates": iso_post,
                "auto_edges": auto_added,
            },
            "corr": {
                "degree_residual": round(corr_deg_res, 4),
                "degree_salience": round(corr_deg_sal, 4),
                "degree_lex": round(corr_deg_lex, 4),
            },
            "top_salience": [[nid, d] for nid, d in top_sal],
            "top_residual": [[nid, d] for nid, d in top_res],
            "top_lex": [[nid, d] for nid, d in top_lex],
        }, f, ensure_ascii=False, indent=2)
    print(f"\nResults: {out}")


if __name__ == "__main__":
    main()
