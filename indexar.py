#!/usr/bin/env python3
"""Indexa os PDFs de contexto/ num corpus BM25 em .state/corpus.json.

Uso:
    ./.venv/bin/python indexar.py

Extrai o texto (pdftotext), limpa o boilerplate, quebra em trechos por paragrafo
e grava o indice invertido usado por ferramentas.consultar_obras. Rode novamente
sempre que adicionar/trocar PDFs em contexto/.
"""
import json
import subprocess
import sys
from pathlib import Path

import ferramentas
import logsetup

BASE = Path(__file__).resolve().parent
CONTEXTO = BASE / "contexto"
OUT = BASE / ".state" / "corpus.json"

log = logsetup.get_logger("indexar")

MAX_CHARS = 900

TITULOS = {
    "mistoquente": "Misto Quente",
    "mulheres": "Mulheres",
}


def titulo_de(nome: str) -> str:
    low = nome.lower()
    for chave, titulo in TITULOS.items():
        if chave in low:
            return titulo
    return Path(nome).stem


def extrair(pdf: Path) -> str:
    r = subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"],
        capture_output=True,
        check=True,
    )
    return r.stdout.decode("utf-8", errors="replace")


def chunks_de(texto: str, livro: str):
    texto = ferramentas.limpar_texto(texto)
    paras = [p.strip() for p in texto.split("\n\n") if p.strip()]

    def sentences(p, maxlen=MAX_CHARS):
        if len(p) <= maxlen:
            return [p]
        import re

        partes = re.split(r"(?<=[.!?])\s+", p)
        out, cur = [], ""
        for s in partes:
            if cur and len(cur) + len(s) + 1 > maxlen:
                out.append(cur)
                cur = s
            else:
                cur = (cur + " " + s).strip()
        if cur:
            out.append(cur)
        return out

    out = []
    buf = ""
    for p in paras:
        if buf and len(buf) + len(p) + 1 > MAX_CHARS:
            out.append((livro, buf))
            buf = ""
        for s in sentences(p):
            if buf and len(buf) + len(s) + 1 > MAX_CHARS:
                out.append((livro, buf))
                buf = ""
            buf = (buf + " " + s).strip() if buf else s
        if buf and len(buf) >= MAX_CHARS:
            out.append((livro, buf))
            buf = ""
    if buf:
        out.append((livro, buf))
    return out


def build():
    pdfs = sorted(CONTEXTO.glob("*.pdf"))
    if not pdfs:
        log.error("nenhum PDF em %s", CONTEXTO)
        sys.exit(1)

    chunks = []
    livros = []
    for pdf in pdfs:
        livro = titulo_de(pdf.name)
        livros.append({"nome": livro, "arquivo": pdf.name})
        try:
            texto = extrair(pdf)
        except Exception as e:
            log.error("falha ao extrair %s: %r", pdf.name, e)
            continue
        cs = chunks_de(texto, livro)
        log.info("%s -> %d trechos", pdf.name, len(cs))
        for livro_, txt in cs:
            chunks.append({"livro": livro_, "texto": txt})

    tf_list = []
    dl_list = []
    df: dict = {}
    postings: dict = {}
    for cid, c in enumerate(chunks):
        toks = ferramentas.tokenize(c["texto"])
        counts: dict = {}
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
        tf_list.append(counts)
        dl_list.append(len(toks))
        for t in counts:
            df[t] = df.get(t, 0) + 1
            postings.setdefault(t, []).append(cid)

    corpus = {
        "livros": livros,
        "chunks": chunks,
        "tf": tf_list,
        "dl": dl_list,
        "df": df,
        "postings": postings,
        "N": len(chunks),
        "avgdl": (sum(dl_list) / len(dl_list)) if dl_list else 1.0,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")
    size = OUT.stat().st_size
    log.info("corpus gravado: %d trechos, %d termos, %.1f MB", len(chunks), len(df), size / 1048576)
    print(f"corpus: {OUT} | trechos={len(chunks)} termos={len(df)} | {size} bytes")

    gerar_embeddings(chunks)


def gerar_embeddings(chunks: list) -> None:
    """Gera e grava os vetores (fastembed/ONNX) dos trechos. Nao-fatal se falhar."""
    import time

    import numpy as np

    textos = [c["texto"] for c in chunks]
    t0 = time.time()
    try:
        vecs = ferramentas.embed_textos(textos, consulta=False)
    except Exception as e:
        log.warning("embeddings indisponiveis (%r); indice BM25 segue valido", e)
        print("embeddings: falhou (BM25 continua funcionando)")
        return

    np.save(ferramentas.EMB_PATH, vecs)
    meta = {
        "modelo": ferramentas.EMBED_MODEL,
        "dim": int(vecs.shape[1]),
        "n": int(vecs.shape[0]),
    }
    ferramentas.EMB_META.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    log.info(
        "embeddings gravados: %d x %d (%s) em %.1fs",
        vecs.shape[0],
        vecs.shape[1],
        ferramentas.EMBED_MODEL,
        time.time() - t0,
    )
    print(f"embeddings: {ferramentas.EMB_PATH} | {vecs.shape[0]} x {vecs.shape[1]}")


if __name__ == "__main__":
    build()
