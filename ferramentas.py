#!/usr/bin/env python3
"""Ferramentas do runtime: busca RAG (BM25) sobre o corpus das obras de Bukowski.

O indice e gerado por `indexar.py` em `.state/corpus.json`. Aqui so ha leitura
e busca em memoria (Python puro, sem dependencias externas).
"""
import json
import math
import re
import unicodedata

import config
import logsetup

CORPUS = config.STATE_DIR / "corpus.json"
EMB_PATH = config.STATE_DIR / "embeddings.npy"
EMB_META = config.STATE_DIR / "index_meta.json"

EMBED_MODEL = config.EMBED_MODEL

log = logsetup.get_logger("ferramentas")

_NOISE = [
    "le livros",
    "lelivros",
    "ios books",
    "iosbooks",
    "legilibro",
    "dados de copyright",
    "expressamente proibida",
    "totalmente repud",
    "domínio público e propriedade",
    "dominio publico e propriedade",
    "conteúdo de domínio",
    "venda desse ebook",
    "equipe ios",
    "nosso site",
    "nossos sites",
    "sites parceiros",
    "sobre a obra",
    "sobre nós",
    "objetivo de oferecer conteúdo",
    "pesquisas e estudos acadêmicos",
    "compra futura",
    "conhecimento e a educação devem",
    "deve ser acessíveis e livres",
    "poderá enfim evoluir",
    "quando o mundo estiver unido",
    "parceria com o grupo",
    "proporcionar, de maneira totalmente gratuita",
    "adquirir o original",
    "incentivando o autor",
    "visite nossos sites",
    "http://",
    "https://",
    "www.",
]

_STOP = {
    "a", "à", "às", "ao", "aos", "as", "o", "os", "um", "uma", "uns", "umas",
    "de", "da", "das", "do", "dos", "em", "no", "na", "nos", "nas", "por",
    "para", "pra", "com", "sem", "sob", "sobre", "entre", "que", "e", "é",
    "ou", "mas", "se", "como", "eu", "tu", "ele", "ela", "nós", "vos", "eles",
    "elas", "me", "te", "lhe", "lhes", "meu", "minha", "seu", "sua", "isso",
    "isto", "aquilo", "já", "não", "sim", "muito", "mais", "menos", "também",
    "the", "of", "and", "to", "in", "a", "is", "it", "you", "i",
}

_WORD_RE = re.compile(r"[a-zà-öø-ÿ0-9]+", re.IGNORECASE)


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def tokenize(text: str) -> list[str]:
    out = []
    for w in _WORD_RE.findall(text.lower()):
        w = _strip_accents(w)
        if len(w) >= 3 and w not in _STOP:
            out.append(w)
    return out


def limpar_texto(text: str) -> str:
    text = text.replace("\f", "\n")
    linhas = []
    for ln in text.splitlines():
        low = _strip_accents(ln.lower())
        if any(n in low for n in _NOISE):
            continue
        linhas.append(ln.rstrip())
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Carregamento do corpus (cacheado)
# ---------------------------------------------------------------------------

_CACHE: dict = {"data": None, "mtime": None}


def load_corpus() -> dict:
    if not CORPUS.exists():
        return {}
    mtime = CORPUS.stat().st_mtime
    if _CACHE["data"] is not None and _CACHE["mtime"] == mtime:
        return _CACHE["data"]
    try:
        data = json.loads(CORPUS.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("falha ao ler corpus: %r", e)
        return {}
    _CACHE["data"] = data
    _CACHE["mtime"] = mtime
    return data


def listar_contexto() -> list[dict]:
    data = load_corpus()
    return data.get("livros", [])


# ---------------------------------------------------------------------------
# Busca BM25
# ---------------------------------------------------------------------------

_K1 = 1.5
_B = 0.75


def _bm25_cids(consulta: str, limite: int) -> list[tuple[int, float]]:
    """Retorna [(chunk_id, score)] por BM25, ordenado desc."""
    data = load_corpus()
    chunks = data.get("chunks") or []
    if not chunks:
        return []
    df = data.get("df", {})
    N = data.get("N", len(chunks))
    avgdl = data.get("avgdl", 1.0) or 1.0

    termos = tokenize(consulta)
    if not termos:
        return []

    scores = {}
    for t in termos:
        if t not in df:
            continue
        idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
        for cid in data.get("postings", {}).get(t, []):
            tf = data["tf"][cid].get(t, 0)
            dl = data["dl"][cid]
            score = idf * (tf * (_K1 + 1)) / (tf + _K1 * (1 - _B + _B * dl / avgdl))
            scores[cid] = scores.get(cid, 0.0) + score

    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limite]


def buscar(consulta: str, limite: int = 4) -> list[dict]:
    data = load_corpus()
    chunks = data.get("chunks") or []
    out = []
    for cid, sc in _bm25_cids(consulta, limite):
        c = chunks[cid]
        out.append({"livro": c["livro"], "texto": c["texto"], "score": round(sc, 2)})
    return out


# ---------------------------------------------------------------------------
# Embeddings (fastembed/ONNX) e busca hibrida (BM25 + vetores via RRF)
# ---------------------------------------------------------------------------

_EMB_CACHE = {"model": None, "vectors": None, "mtime": None}


def _get_embedder():
    if _EMB_CACHE["model"] is not None:
        return _EMB_CACHE["model"]
    from fastembed import TextEmbedding

    _EMB_CACHE["model"] = TextEmbedding(model_name=EMBED_MODEL)
    log.info("embedder carregado: %s", EMBED_MODEL)
    return _EMB_CACHE["model"]


def _com_prefixo(textos: list[str], consulta: bool) -> list[str]:
    # Modelos da familia e5 exigem "query:"/"passage:"; os demais nao.
    if "e5" in EMBED_MODEL.lower():
        p = "query: " if consulta else "passage: "
        return [p + t for t in textos]
    return textos


def embed_textos(textos: list[str], consulta: bool = False):
    import numpy as np

    model = _get_embedder()
    vecs = np.asarray(list(model.embed(_com_prefixo(textos, consulta))), dtype="float32")
    normas = np.linalg.norm(vecs, axis=1, keepdims=True)
    normas[normas == 0] = 1.0
    return vecs / normas


def load_embeddings():
    meta = {}
    try:
        meta = json.loads(EMB_META.read_text(encoding="utf-8"))
    except Exception:
        pass
    if not EMB_PATH.exists():
        return None, meta
    mtime = EMB_PATH.stat().st_mtime
    if _EMB_CACHE["vectors"] is not None and _EMB_CACHE["mtime"] == mtime:
        return _EMB_CACHE["vectors"], meta
    try:
        import numpy as np

        vecs = np.load(EMB_PATH, mmap_mode="r")
    except Exception as e:
        log.warning("falha ao carregar embeddings: %r", e)
        return None, meta
    _EMB_CACHE["vectors"] = vecs
    _EMB_CACHE["mtime"] = mtime
    return vecs, meta


_RRF_K = 60


def _rrf(*listas: list[int]) -> dict:
    scores: dict = {}
    for lista in listas:
        for rank, cid in enumerate(lista):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank + 1)
    return scores


def buscar_hibrido(consulta: str, limite: int = 4, k_bm25: int = 20) -> list[dict]:
    """BM25 (top-k) + similaridade semantica, fundidos por RRF.

    Sem embeddings (ou em caso de falha) cai no BM25 puro.
    """
    data = load_corpus()
    chunks = data.get("chunks") or []
    if not chunks:
        return []

    bm_ids = [cid for cid, _ in _bm25_cids(consulta, k_bm25)]

    vec_ids: list = []
    vecs, _meta = load_embeddings()
    if vecs is not None and len(vecs) == len(chunks):
        try:
            import numpy as np

            q = embed_textos([consulta], consulta=True)[0]
            sims = np.asarray(vecs) @ q
            k = min(k_bm25, len(sims))
            vec_ids = list(np.argsort(-sims)[:k])
        except Exception as e:
            log.warning("busca vetorial falhou, usando BM25: %r", e)
            vec_ids = []

    if not vec_ids:
        ids = bm_ids[:limite]
    else:
        fused = _rrf(bm_ids, vec_ids)
        ids = [
            cid
            for cid, _ in sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:limite]
        ]

    return [{"livro": chunks[cid]["livro"], "texto": chunks[cid]["texto"]} for cid in ids]


# ---------------------------------------------------------------------------
# Tool do agente
# ---------------------------------------------------------------------------

def consultar_contexto(args, context=None) -> str:
    """Busca trechos no contexto indexado para embasar a resposta."""
    args = args or {}
    consulta = (args.get("consulta") or "").strip()
    if not consulta:
        return json.dumps({"erro": "informe consulta"})
    limite = int(args.get("limite") or 4)
    if config.RETRIEVAL == "bm25":
        trechos = buscar(consulta, limite)
    else:
        trechos = buscar_hibrido(consulta, limite)
    log.info("consultar_contexto(%r) -> %d trechos", consulta, len(trechos))
    if not trechos:
        return json.dumps(
            {"consulta": consulta, "trechos": [], "aviso": "nada relevante encontrado"}
        )
    return json.dumps({"consulta": consulta, "trechos": trechos}, ensure_ascii=False)


if __name__ == "__main__":
    import sys

    print(consultar_contexto({"consulta": " ".join(sys.argv[1:])}))
