"""
Renomeador otimizado de PDFs de notas fiscais.

Objetivo:
- Processar somente os PDFs diretamente em PASTA_NOTAS.
- Encontrar uma chave fiscal válida de 44 dígitos.
- Validar também a estrutura da chave para reduzir falsos positivos.
- Fazer backup do original em "_backup_originais".
- Renomear o PDF para <CHAVE>.pdf.

Estratégia de desempenho:
1. Tenta extrair texto nativo do PDF (muito rápido).
2. Se necessário, executa OCR rápido a 180 DPI, limitado a números.
3. Só se ainda não encontrar uma chave, repete OCR a 300 DPI.
4. Para a análise assim que encontra uma chave válida inequívoca.

ATENÇÂO!!!!!!!!!!!!!!!!
Instalar:
    pip install pymupdf easyocr

Na primeira execução, o EasyOCR pode baixar os modelos necessários.
"""

import re
import shutil
import time
from pathlib import Path

import pymupdf
import easyocr


# ============================================================
# CONFIGURAÇÃO
# ALTERE SOMENTE ESTA LINHA.
# ============================================================
PASTA_NOTAS = Path(r"d:\NotasFiscais")

PASTA_BACKUP = PASTA_NOTAS / "_backup_originais"

DPI_RAPIDO = 180
DPI_REFORCO = 300

# O modelo é carregado uma única vez para todos os PDFs.
reader = easyocr.Reader(["en"], gpu=False, verbose=False)


def validar_chave(chave: str) -> bool:
    """
    Valida uma chave fiscal de 44 dígitos em duas etapas:

    1. Estrutura semântica da chave:
       - código de UF válido;
       - AAMM com mês entre 01 e 12;
       - CNPJ não zerado;
       - modelo fiscal conhecido;
       - tipo de emissão entre 1 e 9.

    2. Dígito verificador pelo módulo 11.

    Isso reduz falsos positivos formados por sequências numéricas que,
    por coincidência, passam somente no cálculo do dígito verificador.
    """
    if not re.fullmatch(r"\d{44}", chave):
        return False

    if len(set(chave)) == 1:
        return False

    # Estrutura oficial da chave de acesso:
    # cUF(2) + AAMM(4) + CNPJ(14) + mod(2) + serie(3) +
    # nNF(9) + tpEmis(1) + cNF(8) + cDV(1)

    codigos_uf = {
        "11", "12", "13", "14", "15", "16", "17",
        "21", "22", "23", "24", "25", "26", "27", "28", "29",
        "31", "32", "33", "35",
        "41", "42", "43",
        "50", "51", "52", "53",
    }

    cuf = chave[0:2]
    aamm = chave[2:6]
    cnpj = chave[6:20]
    modelo = chave[20:22]
    tp_emis = chave[34]

    if cuf not in codigos_uf:
        return False

    # AAMM: ano com 2 dígitos + mês com 2 dígitos.
    mes = int(aamm[2:4])
    if not 1 <= mes <= 12:
        return False

    if cnpj == "0" * 14:
        return False

    # Modelos com chave de acesso de 44 dígitos mais comuns.
    # 55 = NF-e
    # 65 = NFC-e
    # 57 = CT-e
    # 67 = CT-e OS
    # 58 = MDF-e (alguns ambientes/documentações podem variar por uso)
    # 59 = CF-e SAT
    modelos_validos = {"55", "65", "57", "67", "58", "59"}

    if modelo not in modelos_validos:
        return False

    if tp_emis not in "123456789":
        return False

    # Validação do dígito verificador.
    base = chave[:43]
    dv_informado = int(chave[43])

    soma = 0
    peso = 2

    for digito in reversed(base):
        soma += int(digito) * peso
        peso += 1
        if peso > 9:
            peso = 2

    resto = soma % 11
    dv = 11 - resto

    if dv >= 10:
        dv = 0

    return dv == dv_informado


def procurar_chaves_em_texto(texto: str) -> set[str]:
    """
    Procura chaves de 44 dígitos no texto.

    Faz duas tentativas:
    - sequência contínua de 44 dígitos;
    - números separados por espaços/pontuação/quebras de linha.
    """
    encontradas = set()

    # Caso mais simples: 44 dígitos contínuos.
    for chave in re.findall(r"(?<!\d)\d{44}(?!\d)", texto):
        if validar_chave(chave):
            encontradas.add(chave)

    # Chave formatada/quebrada: ex. grupos separados por espaços, pontos etc.
    padrao = r"(?<!\d)(?:\d[\s.\-:/]*){44}(?!\d)"
    for trecho in re.findall(padrao, texto):
        chave = re.sub(r"\D", "", trecho)
        if validar_chave(chave):
            encontradas.add(chave)

    return encontradas


def procurar_no_texto_nativo(documento) -> set[str]:
    """Primeira tentativa: texto já existente no PDF, sem OCR."""
    encontradas = set()

    for pagina in documento:
        texto = pagina.get_text("text") or ""
        encontradas.update(procurar_chaves_em_texto(texto))

        # Uma chave válida já é suficiente para a grande maioria das notas.
        if len(encontradas) > 1:
            return encontradas

    return encontradas


def ocr_pagina(pagina, dpi: int) -> list[str]:
    """
    Executa OCR da página limitando o reconhecimento aos dígitos.
    Isso reduz trabalho e erros, pois só queremos a chave numérica.
    """
    pix = pagina.get_pixmap(dpi=dpi, alpha=False)
    imagem = pix.tobytes("png")

    return reader.readtext(
        imagem,
        detail=0,
        paragraph=False,
        allowlist="0123456789",
    )


def procurar_com_ocr(documento, dpi: int) -> set[str]:
    """Executa OCR página a página e interrompe cedo quando possível."""
    encontradas = set()

    for pagina in documento:
        textos = ocr_pagina(pagina, dpi)

        # Mantém separação entre blocos, mas testa também cada bloco.
        for bloco in textos:
            encontradas.update(procurar_chaves_em_texto(bloco))

        encontradas.update(procurar_chaves_em_texto(" ".join(textos)))

        # Mais de uma chave é ambíguo e deve ser tratado como pendência.
        if len(encontradas) > 1:
            return encontradas

        # Otimização: se uma chave válida foi encontrada nesta página,
        # ela é retornada imediatamente.
        if len(encontradas) == 1:
            return encontradas

    return encontradas


def encontrar_chaves(caminho_pdf: Path):
    """
    Retorna (chaves, metodo).

    Método:
    - TEXTO
    - OCR 180 DPI
    - OCR 300 DPI
    - NÃO ENCONTRADA
    """
    with pymupdf.open(caminho_pdf) as documento:
        # Etapa 1: praticamente gratuita quando o PDF contém texto.
        chaves = procurar_no_texto_nativo(documento)
        if chaves:
            return chaves, "texto nativo"

        # Etapa 2: OCR rápido.
        chaves = procurar_com_ocr(documento, DPI_RAPIDO)
        if chaves:
            return chaves, f"OCR {DPI_RAPIDO} DPI"

        # Etapa 3: somente PDFs difíceis chegam aqui.
        chaves = procurar_com_ocr(documento, DPI_REFORCO)
        if chaves:
            return chaves, f"OCR {DPI_REFORCO} DPI"

    return set(), "não encontrada"


def nome_backup_disponivel(arquivo: Path) -> Path:
    destino = PASTA_BACKUP / arquivo.name

    if not destino.exists():
        return destino

    contador = 1
    while True:
        destino = PASTA_BACKUP / f"{arquivo.stem}_{contador}{arquivo.suffix}"
        if not destino.exists():
            return destino
        contador += 1


def fazer_backup(arquivo: Path) -> Path:
    """Copia o original e confirma a cópia antes da renomeação."""
    PASTA_BACKUP.mkdir(parents=True, exist_ok=True)

    destino = nome_backup_disponivel(arquivo)
    shutil.copy2(arquivo, destino)

    if not destino.exists():
        raise RuntimeError("O arquivo de backup não foi criado.")

    if destino.stat().st_size != arquivo.stat().st_size:
        try:
            destino.unlink()
        except OSError:
            pass
        raise RuntimeError("A cópia de backup ficou com tamanho diferente do original.")

    return destino


def processar_pdf(arquivo: Path) -> str:
    inicio = time.perf_counter()

    print(f"Analisando: {arquivo.name}")

    chaves, metodo = encontrar_chaves(arquivo)
    duracao = time.perf_counter() - inicio

    if not chaves:
        print(f"  [PENDENTE] Chave não encontrada ({duracao:.1f}s).")
        return "pendente"

    if len(chaves) > 1:
        print(f"  [PENDENTE] Mais de uma chave válida encontrada ({duracao:.1f}s).")
        for chave in sorted(chaves):
            print(f"             {chave}")
        print("             O arquivo não foi alterado.")
        return "pendente"

    chave = next(iter(chaves))
    destino = arquivo.with_name(f"{chave}.pdf")

    if arquivo.name.lower() == destino.name.lower():
        print(f"  [OK] Já está correto: {arquivo.name} ({duracao:.1f}s)")
        return "ja_correto"

    if destino.exists():
        print(f"  [PENDENTE] O arquivo {destino.name} já existe.")
        print("             O original não foi alterado.")
        return "pendente"

    backup = fazer_backup(arquivo)
    arquivo.rename(destino)

    print(f"  [OK] {chave}")
    print(f"       Método: {metodo}")
    print(f"       Tempo: {duracao:.1f}s")
    print(f"       Backup: {backup.name}")
    print(f"       Renomeado para: {destino.name}")

    return "renomeado"


def main():
    if not PASTA_NOTAS.exists() or not PASTA_NOTAS.is_dir():
        print("=" * 72)
        print("ERRO: a pasta configurada não existe ou não é uma pasta:")
        print(PASTA_NOTAS)
        print("\nAltere PASTA_NOTAS no início deste arquivo.")
        print("=" * 72)
        return

    # Somente a pasta principal. Não percorre subpastas.
    arquivos = sorted(
        p for p in PASTA_NOTAS.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf"
    )

    if not arquivos:
        print(f"Nenhum PDF encontrado em: {PASTA_NOTAS}")
        return

    resultado = {
        "renomeado": 0,
        "ja_correto": 0,
        "pendente": 0,
        "erro": 0,
    }

    inicio_total = time.perf_counter()

    print("=" * 72)
    print("RENOMEADOR OTIMIZADO DE NOTAS FISCAIS")
    print("=" * 72)
    print(f"Pasta: {PASTA_NOTAS}")
    print(f"PDFs: {len(arquivos)}")
    print("Estratégia: texto -> OCR 180 DPI -> OCR 300 DPI")
    print("=" * 72)

    for indice, arquivo in enumerate(arquivos, 1):
        print(f"\n[{indice}/{len(arquivos)}] ", end="")

        try:
            status = processar_pdf(arquivo)
            resultado[status] += 1
        except Exception as exc:
            resultado["erro"] += 1
            print(f"  [ERRO] {exc}")
            print("         O arquivo original foi mantido.")

    duracao_total = time.perf_counter() - inicio_total

    print("\n" + "=" * 72)
    print("RESUMO")
    print("=" * 72)
    print(f"Renomeados : {resultado['renomeado']}")
    print(f"Já corretos: {resultado['ja_correto']}")
    print(f"Pendentes  : {resultado['pendente']}")
    print(f"Erros      : {resultado['erro']}")
    print(f"Total      : {len(arquivos)}")
    print(f"Tempo total: {duracao_total:.1f}s")

    if arquivos:
        print(f"Média      : {duracao_total / len(arquivos):.1f}s por PDF")

    print("=" * 72)


if __name__ == "__main__":
    main()
