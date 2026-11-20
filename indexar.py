#!/usr/bin/env python3
"""Indexa os PDFs/TXTs de CONTEXT_DIR num corpus BM25 + embeddings em STATE_DIR.

Uso:
    ./.venv/bin/python indexar.py

PDFs passam por `pdftotext`; arquivos `.txt` sao lidos diretamente. O texto e
limpo, quebrado em trechos por paragrafo e gravado no indice usado por
ferramentas.consultar_contexto. Rode novamente sempre que adicionar/trocar
arquivos. CONTEXT_DIR (default: contexto/<AGENT>) e STATE_DIR (default: .state)
vem do ambiente; um `titulos.json` opcional em CONTEXT_DIR mapeia arquivo ->
titulo bonito (senao usa o nome do arquivo).
"""
import json
import subprocess
import sys
from pathlib import Path

import config
import ferramentas
import logsetup

CONTEXT_DIR = config.CONTEXT_DIR
OUT = config.STATE_DIR / "corpus.json"

log = logsetup.get_logger("indexar")

MAX_CHARS = 900


def load_titulos() -> dict:
    path = CONTEXT_DIR / "titulos.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def titulo_de(nome: str, mapa: dict) -> str:
    stem = Path(nome).stem
    return mapa.get(nome) or mapa.get(stem) or stem


def extrair_pdf(pdf: Path) -> str:
    r = subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"],
        capture_output=True,
        check=True,
    )
    return r.stdout.decode("utf-8", errors="replace")


def extrair_arquivo(arquivo: Path) -> str:
    if arquivo.suffix.lower() == ".pdf":
        return extrair_pdf(arquivo)
    return arquivo.read_text(encoding="utf-8", errors="replace")


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


def secoes_de_txt(texto: str) -> list[tuple[str, str]]:
    """Divide um .txt do coletor em (titulo, corpo) pelos cabecalhos `## `."""
    secoes = []
    titulo = None
    buf = []
    for ln in texto.splitlines():
        if ln.startswith("## "):
            if titulo is not None:
                secoes.append((titulo, "\n".join(buf)))
            titulo = ln[3:].strip()
            buf = []
        elif titulo is not None:
            buf.append(ln)
    if titulo is not None:
        secoes.append((titulo, "\n".join(buf)))
    return secoes


def chunks_de_txt(texto: str, livro: str):
    """Chunks por texto, repetindo o titulo em cada trecho (melhora a busca)."""
    secoes = secoes_de_txt(texto)
    if not secoes:
        return chunks_de(texto, livro)
    out = []
    for titulo, corpo in secoes:
        if not corpo.strip():
            continue
        rotulo = f"{livro} - {titulo}" if titulo else livro
        for _, trecho in chunks_de(corpo, rotulo):
            if titulo:
                trecho = f"{titulo}\n{trecho}"
            out.append((rotulo, trecho))
    return out


def build():
    arquivos = sorted([*CONTEXT_DIR.glob("*.pdf"), *CONTEXT_DIR.glob("*.txt")])
    if not arquivos:
        log.error("nenhum PDF/TXT em %s", CONTEXT_DIR)
        sys.exit(1)

    mapa = load_titulos()
    chunks = []
    livros = []
    for arquivo in arquivos:
        livro = titulo_de(arquivo.name, mapa)
        livros.append({"nome": livro, "arquivo": arquivo.name})
        try:
            texto = extrair_arquivo(arquivo)
        except Exception as e:
            log.error("falha ao extrair %s: %r", arquivo.name, e)
            continue
        if arquivo.suffix.lower() == ".txt":
            cs = chunks_de_txt(texto, livro)
        else:
            cs = chunks_de(texto, livro)
        log.info("%s -> %d trechos", arquivo.name, len(cs))
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

    if config.RETRIEVAL == "bm25":
        # Indice BM25 apenas: pula a geracao de embeddings (nao gasta API).
        log.info("RETRIEVAL=bm25: pulando embeddings (indice BM25 apenas)")
        print("embeddings: pulados (RETRIEVAL=bm25)")
        return

    gerar_embeddings(chunks)


def gerar_embeddings(chunks: list) -> None:
    """Gera e grava os vetores (OpenRouter `/embeddings`) dos trechos. Nao-fatal se falhar."""
    import time

    import numpy as np

    textos = [c["texto"] for c in chunks]
    t0 = time.time()
    try:
        vecs = ferramentas.embed_textos_api(textos, consulta=False)
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
