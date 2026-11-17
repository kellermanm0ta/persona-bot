#!/usr/bin/env python3
"""Coleta e cura o nucleo do Arquivo Pessoa para o RAG do bot.

Le as paginas de autores de arquivopessoa.net, baixa os textos selecionados e
grava um arquivo .txt por autor em CONTEXT_DIR (default: contexto/pessoa), mais
`titulos.json` e `manifesto.json`. O nucleo inclui todos os textos dos quatro
heteronimos principais (Caeiro, Reis, Soares, Campos) e, do ortonimo, a poesia,
`Mensagem` e `O Banqueiro Anarquista` / `O Marinheiro`.

Uso:
    ./.venv/bin/python coletar_arquivo_pessoa.py
    ./.venv/bin/python coletar_arquivo_pessoa.py --limite 3   # amostra

O cache de HTML fica em data/pessoa/cache_html (ignorado pelo git).
"""
import argparse
import hashlib
import html as htmlmod
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parent
BASE_URL = os.environ.get("ARQUIVO_BASE_URL", "http://arquivopessoa.net").rstrip("/")

AUTORES = {
    1: ("Fernando Pessoa", "fernando-pessoa"),
    2: ("Alberto Caeiro", "alberto-caeiro"),
    3: ("Ricardo Reis", "ricardo-reis"),
    4: ("Bernardo Soares", "bernardo-soares"),
    5: ("Álvaro de Campos", "alvaro-de-campos"),
}
HETERONIMOS = {2, 3, 4, 5}

UA = "Mozilla/5.0 (X11; Linux x86_64) persona-bots/1.0"

RE_LISTA = re.compile(r'<li>\s*<a href="/textos/(\d+)"[^>]*>(.*?)</a>\s*</li>', re.S)
RE_AUTOR = re.compile(r'<div class="autor">(.*?)</div>', re.S)
RE_TITULO = re.compile(r'<h1 class="titulo-texto">(.*?)</h1>', re.S)
RE_CORPO = re.compile(r'<div class="texto-(prosa|poesia|teatro)">(.*?)</div>', re.S)
RE_BIBLIO = re.compile(r'<div class="biblio">(.*?)</div>', re.S)
RE_DATA = re.compile(r'<div class="data">(.*?)</div>', re.S)

_sem_cache = False


def _inline(fragmento: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", fragmento or "")
    txt = htmlmod.unescape(txt).replace("\xa0", " ")
    return re.sub(r"\s+", " ", txt).strip()


def _norm(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).lower().strip()


def _corpo_de(fragmento: str, tipo: str) -> str:
    frag = re.sub(r"<(br|hr)\s*/?>", "\n", fragmento or "", flags=re.I)
    sep = "\n\n" if tipo == "prosa" else "\n"
    frag = re.sub(r"</p\s*>", sep, frag, flags=re.I)
    frag = re.sub(r"<[^>]+>", "", frag)
    frag = htmlmod.unescape(frag).replace("\xa0", " ")

    linhas = []
    vazias = 0
    for ln in frag.splitlines():
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if ln:
            linhas.append(ln)
            vazias = 0
        else:
            vazias += 1
            if vazias <= 1:
                linhas.append("")
    return "\n".join(linhas).strip()


def _sem_titulo_duplicado(texto: str, titulo: str) -> str:
    linhas = texto.splitlines()
    i = 0
    while i < len(linhas) and not linhas[i].strip():
        i += 1
    if i < len(linhas) and _norm(linhas[i]) == _norm(titulo):
        del linhas[i]
        while i < len(linhas) and not linhas[i].strip():
            del linhas[i]
    return "\n".join(linhas).strip()


def fetch(url: str, tentativas: int = 3) -> str:
    ultimo = None
    for i in range(tentativas):
        try:
            req = Request(url, headers={"User-Agent": UA})
            with urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as e:  # rede instavel / 5xx
            ultimo = e
            if i < tentativas - 1:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"falha ao baixar {url}: {ultimo!r}")


def listar_autor(aid: int) -> list[tuple[int, str]]:
    html = fetch(f"{BASE_URL}/autores/{aid}")
    itens = []
    for tid, titulo in RE_LISTA.findall(html):
        itens.append((int(tid), _inline(titulo)))
    return itens


def fetch_texto(tid: int, cache_dir: Path) -> str:
    cache = cache_dir / f"{tid}.html"
    if not _sem_cache and cache.exists():
        return cache.read_text(encoding="utf-8", errors="replace")
    html = fetch(f"{BASE_URL}/textos/{tid}")
    if not _sem_cache:
        cache.write_text(html, encoding="utf-8")
    time.sleep(0.1)
    return html


def extrair_pagina(html: str) -> dict | None:
    titulo = RE_TITULO.search(html)
    corpo = RE_CORPO.search(html)
    if not titulo or not corpo:
        return None
    autor = RE_AUTOR.search(html)
    biblio = RE_BIBLIO.search(html)
    data = RE_DATA.search(html)
    titulo_txt = _inline(titulo.group(1))
    return {
        "titulo": titulo_txt,
        "autor_pagina": _inline(autor.group(1)) if autor else "",
        "tipo": corpo.group(1),
        "texto": _sem_titulo_duplicado(_corpo_de(corpo.group(2), corpo.group(1)), titulo_txt),
        "biblio": _inline(biblio.group(1)) if biblio else "",
        "data": _inline(data.group(1)) if data else "",
    }


def incluir_ortónimo(rec: dict) -> bool:
    titulo = _norm(rec["titulo"])
    biblio = _norm(rec["biblio"])
    if rec["tipo"] == "poesia":
        return True
    if "mensagem" in biblio:
        return True
    if "banqueiro anarquista" in titulo:
        return True
    if "o marinheiro" in titulo:
        return True
    return False


def processar(item: tuple, cache_dir: Path) -> tuple[str, dict | None]:
    aid, autor, slug, ordem, tid, titulo_lista = item
    try:
        html = fetch_texto(tid, cache_dir)
        rec = extrair_pagina(html)
        if not rec:
            raise ValueError("pagina sem titulo/corpo reconheciveis")
        rec.update(
            autor_selecao=autor,
            slug=slug,
            url=f"{BASE_URL}/textos/{tid}",
            ordem=ordem,
        )
        if aid == 1 and not incluir_ortónimo(rec):
            return "skip", None
        if len(rec["texto"]) < 20:
            return "skip", None
        return "ok", rec
    except Exception as e:
        return "falha", {"id": tid, "titulo": titulo_lista, "erro": repr(e)}


def chave_dedup(texto: str) -> str:
    normalizado = re.sub(r"\s+", " ", texto).strip().lower()
    return hashlib.sha1(normalizado.encode("utf-8")).hexdigest()


def gravar(recs: list[dict], out_dir: Path, autores: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    titulos = {}
    manifesto = []
    for aid, (autor, slug) in autores.items():
        do_autor = sorted(
            (r for r in recs if r["slug"] == slug), key=lambda r: r["ordem"]
        )
        if not do_autor:
            continue
        nome_arquivo = f"{slug}.txt"
        partes = [
            f"# {autor} - Obra Edita (Arquivo Pessoa)",
            f"# Fonte: {BASE_URL}/autores/{aid}",
            "",
        ]
        for r in do_autor:
            partes.append(f"## {r['titulo']}")
            partes.append(f"Autor: {autor}")
            if r["biblio"]:
                partes.append(f"Fonte: {r['biblio']}")
            partes.append("")
            partes.append(r["texto"])
            partes.append("")
            manifesto.append(
                {
                    "autor": autor,
                    "titulo": r["titulo"],
                    "url": r["url"],
                    "tipo": r["tipo"],
                    "biblio": r["biblio"],
                    "data": r["data"],
                    "arquivo": nome_arquivo,
                }
            )
        (out_dir / nome_arquivo).write_text("\n".join(partes), encoding="utf-8")
        titulos[nome_arquivo] = f"{autor} - Obra Edita (Arquivo Pessoa)"
        chars = sum(len(r["texto"]) for r in do_autor)
        print(f"  {nome_arquivo}: {len(do_autor)} textos, {chars} chars")

    (out_dir / "titulos.json").write_text(
        json.dumps(titulos, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "manifesto.json").write_text(
        json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    global _sem_cache

    parser = argparse.ArgumentParser(description="Coleta o nucleo do Arquivo Pessoa.")
    parser.add_argument("--limite", type=int, default=0, help="max. textos por autor (amostra)")
    parser.add_argument("--workers", type=int, default=5, help="requisicoes concorrentes")
    parser.add_argument("--sem-cache", action="store_true", help="ignora o cache de HTML")
    parser.add_argument(
        "--saida",
        default=os.environ.get("CONTEXT_DIR", "contexto/pessoa"),
        help="diretorio de saida do corpus",
    )
    args = parser.parse_args()
    _sem_cache = args.sem_cache

    out_dir = Path(args.saida)
    if not out_dir.is_absolute():
        out_dir = BASE / out_dir
    cache_dir = BASE / "data" / "pessoa" / "cache_html"
    cache_dir.mkdir(parents=True, exist_ok=True)

    tarefas = []
    ordem = 0
    print(f"acervo: {BASE_URL}")
    for aid, (autor, slug) in AUTORES.items():
        itens = listar_autor(aid)
        if args.limite:
            itens = itens[: args.limite]
        print(f"  {autor}: {len(itens)} textos na pagina do autor")
        for tid, titulo in itens:
            tarefas.append((aid, autor, slug, ordem, tid, titulo))
            ordem += 1

    ok, pulados, falhas = [], 0, []
    feitos = 0
    print(f"baixando {len(tarefas)} paginas com {args.workers} workers...")
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futuros = [pool.submit(processar, t, cache_dir) for t in tarefas]
        for fut in as_completed(futuros):
            status, rec = fut.result()
            feitos += 1
            if status == "ok" and rec:
                ok.append(rec)
            elif status == "skip":
                pulados += 1
            else:
                falhas.append(rec)
            if feitos % 100 == 0 or feitos == len(tarefas):
                print(f"  {feitos}/{len(tarefas)} ({len(ok)} selecionados)", flush=True)

    vistos = set()
    final = []
    duplicados = 0
    for rec in sorted(ok, key=lambda r: r["ordem"]):
        chave = chave_dedup(rec["texto"])
        if chave in vistos:
            duplicados += 1
            continue
        vistos.add(chave)
        final.append(rec)

    print("gravando corpus...")
    gravar(final, out_dir, AUTORES)
    if falhas:
        (out_dir / "falhas.json").write_text(
            json.dumps(falhas, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    total_chars = sum(len(r["texto"]) for r in final)
    print(
        f"resumo: {len(final)} textos, {total_chars} chars, "
        f"{duplicados} duplicados removidos, {pulados} pulados, {len(falhas)} falhas"
    )
    if falhas:
        print(f"falhas registradas em {out_dir / 'falhas.json'}")
        sys.exit(1)


if __name__ == "__main__":
    main()