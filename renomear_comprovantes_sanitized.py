#!/usr/bin/env python3
"""
renomear_comprovantes_v3.py

Script para extração automática de dados e renomeação de comprovantes de pagamento
em PDF de múltiplos bancos brasileiros.

NOTA: Esta é uma versão sanitizada para uso público/educacional.
      Configure as constantes EMPRESAS com suas próprias empresas/divisões.
"""

import os
import re
import shutil
import zipfile
import unicodedata
from pathlib import Path
from collections import defaultdict
from PyPDF2 import PdfReader, PdfWriter

# --- CONFIGURAÇÃO ---
PASTA_ENTRADA = "entrada"
PASTA_SAIDA = "saida"
TEMP_PARTS = "temp_parts"
ZIP_FINAL = "comprovantes_renomeados.zip"
LOG_CSV = "renomeacao_log.csv"
DEBUG_LOG = "debug_extracao.txt"
MAX_NAME_LEN = 60
BARCODE_TAIL_LEN = 6
PIX_PREFIX = "N"

# CONFIGURAÇÃO: Defina suas empresas/divisões aqui
EMPRESAS = {
    "DIVISION_A": ["COMPANY A NAME", "COMPANY A ALIAS"],
    "DIVISION_B": ["COMPANY B NAME"],
    "DIVISION_C": ["COMPANY C LEGAL NAME", "COMPANY C TRADING NAME"]
}

# --- HELPERS ---
def safe_makedirs(path):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

def limpar_texto(texto: str) -> str:
    if not texto:
        return ""
    texto = texto.replace("\ufb01", "fi").replace("\ufb02", "fl").replace("\u2028", " ")
    texto = unicodedata.normalize("NFKC", texto)
    return texto

def sanitize_filename(name: str) -> str:
    name = (name or "").strip()
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r'[\\/\:*?"<>|]', "", name)
    name = re.sub(r"[^0-9A-Za-z_\-\s\(\)\[\]]", "", name)
    name = "_".join(name.split())
    if len(name) > MAX_NAME_LEN:
        name = name[:MAX_NAME_LEN]
    return name or "FORNECEDOR_DESCONHECIDO"

def validar_nome(nome: str, min_len: int = 5) -> bool:
    """Valida se string parece um nome válido"""
    if not nome or len(nome) < min_len:
        return False

    if not re.search(r'[a-zA-Z]', nome):
        return False

    if re.match(r'^[\d\.\-\*\s]+$', nome):
        return False

    # CONFIGURAÇÃO: Adicione nomes de empresas do sistema para rejeitar
    empresas_sistema = ['SYSTEM_BANK', 'YOUR_COMPANY']
    nome_upper = nome.upper()
    for emp in empresas_sistema:
        if emp in nome_upper:
            return False

    rejeitar = [
        'agencia', 'conta', 'cpf', 'cnpj', 'chave', 'instituicao',
        'banco', 'dados', 'transferencia', 'pagamento', 'valor',
        'documento', 'autenticacao', 'controle', 'debito', 'origem'
    ]

    nome_lower = nome.lower()
    for r in rejeitar:
        if nome_lower == r or nome_lower.startswith(r + ' '):
            return False

    palavras = nome.split()
    if len(palavras) >= 2 or len(re.findall(r'[a-zA-Z]{5,}', nome)) > 0:
        return True

    return False

# --- FUNÇÕES DE EXTRAÇÃO ---
def extrair_texto_pdf(path: str, debug_info: dict) -> str:
    """Extrai texto usando PyPDF2"""
    texto = ""
    try:
        with open(path, 'rb') as f:
            reader = PdfReader(f)
            for p in reader.pages:
                t = p.extract_text()
                if t:
                    texto += t + "\n"
    except Exception:
        pass

    debug_info['extrator'] = 'PyPDF2'
    return limpar_texto(texto)

def separar_em_partes(orig_pdf: str, dest_folder: str):
    """Separa PDF multi-página em arquivos individuais"""
    novos = []
    try:
        reader = PdfReader(orig_pdf)
        total = len(reader.pages)
        for i in range(total):
            writer = PdfWriter()
            writer.add_page(reader.pages[i])
            stem = Path(orig_pdf).stem
            novo_nome = f"{stem}_part{i+1}.pdf"
            novo_path = os.path.join(dest_folder, novo_nome)
            with open(novo_path, "wb") as f:
                writer.write(f)
            novos.append(novo_path)
    except Exception as e:
        print(f"⚠️ Erro ao separar PDF: {e}")
        novos.append(orig_pdf)
    return novos

def identificar_empresa(texto: str) -> str:
    """Identifica empresa pagadora baseado em aliases"""
    t = (texto or "").upper()
    for key, aliases in EMPRESAS.items():
        for a in aliases:
            if a.upper() in t:
                return key
    return "OUTROS"

def extrair_valor(texto: str, debug_info: dict) -> str:
    """Extrai valor do pagamento com priorização por contexto"""
    if not texto:
        debug_info['valor_erro'] = "Texto vazio"
        return "VALOR_NAO_ENCONTRADO"

    valores_encontrados = []

    patterns = [
        (r"Valor\s+principal[:\s]*R?\$?\s*([\d\.,]+)", 1, "principal"),
        (r"Valor\s+total\s+pago[:\s]*R?\$?\s*([\d\.,]+)", 1, "total_pago"),
        (r"Valor\s+do\s+pagamento[:\s]*R?\$?\s*([\d\.,]+)", 2, "pagamento"),
        (r"Valor\s+total[:\s]*R?\$?\s*([\d\.,]+)", 3, "total"),
        (r"(?:^|\n)Valor[:\s]*R?\$?\s*([\d\.,]+)", 4, "valor_linha"),
        (r"R\$\s*([\d\.,]+)", 5, "rs_isolado"),
    ]

    for pattern, prioridade, tipo in patterns:
        for m in re.finditer(pattern, texto, re.IGNORECASE):
            val = m.group(1).strip()
            if "," in val and "." in val:
                val = val.replace(".", "")
            val_conv = val.replace(",", ".")
            try:
                valor_float = float(val_conv)
                if valor_float > 0:
                    valores_encontrados.append({
                        'valor': val.replace(".", ","),
                        'tipo': tipo,
                        'prioridade': prioridade,
                        'float': valor_float
                    })
            except:
                continue

    if valores_encontrados:
        unicos = {}
        for v in valores_encontrados:
            if v['float'] not in unicos or v['prioridade'] < unicos[v['float']]['prioridade']:
                unicos[v['float']] = v

        melhor = min(unicos.values(), key=lambda x: x['prioridade'])
        debug_info['valor_selecionado'] = f"{melhor['tipo']}={melhor['valor']}"
        return melhor['valor']

    debug_info['valor_erro'] = "Nenhum padrão"
    return "VALOR_NAO_ENCONTRADO"

def extrair_beneficiario(texto: str, debug_info: dict) -> str:
    """Extração de beneficiário com padrões multi-banco"""
    if not texto:
        debug_info['benef_erro'] = "Texto vazio"
        return "FORNECEDOR_DESCONHECIDO"

    texto_clean = re.sub(r'\s+', ' ', texto)
    candidatos = []

    # PADRÃO: Controle de Pagamento (Bradesco PIX novo)
    m_controle = re.search(
        r'Controle\s+de\s+Pagamento\s+Benefici[aá]rio:\s*([A-ZÀ-ÚÇ][A-ZÀ-ÚÇ\s\.\-\(\)0-9]+?)(?:\s*CPF/CNPJ:|\s*Controle:|\s*$)',
        texto_clean,
        re.IGNORECASE
    )
    if m_controle:
        nome = m_controle.group(1).strip()
        nome = re.sub(r'\s+', ' ', nome).upper()
        if validar_nome(nome, min_len=5):
            candidatos.append({'nome': nome, 'score': 22, 'metodo': 'BRADESCO-PIX-Controle'})
            debug_info['tipo'] = 'BRADESCO-PIX'

    # PADRÃO: PIX - Dados de quem recebeu
    m_pix = re.search(
        r'(?:Dados de quem recebeu|Destinatário).*?Nome\s*:\s*([A-Z][A-Z\s\.\-]{5,100}?)(?:\s*CPF/CNPJ|\s*Instituição|$)',
        texto,
        re.IGNORECASE | re.DOTALL
    )
    if m_pix:
        nome = m_pix.group(1).strip()
        nome = re.sub(r'\s+', ' ', nome).upper()
        if validar_nome(nome, min_len=8):
            candidatos.append({'nome': nome, 'score': 15, 'metodo': 'PIX-recebedor'})
            debug_info['tipo'] = 'PIX'

    # PADRÃO: TED - Crédito Nome
    m_ted = re.search(
        r'Crédito:\s*Nome:\s*([A-Z][A-Z\s\.\-]{5,100}?)(?:\s*CPF/CNPJ|\s*Agência|$)',
        texto_clean,
        re.IGNORECASE | re.DOTALL
    )
    if m_ted:
        nome = m_ted.group(1).strip()
        nome = re.sub(r'\s+', ' ', nome).upper()
        if validar_nome(nome, min_len=5):
            candidatos.append({'nome': nome, 'score': 19, 'metodo': 'TED'})

    # PADRÃO: BOLETO - Razão Social Beneficiário
    m_boleto = re.search(
        r'Razão\s+Social\s+Beneficiário[:\s]+([A-Z][A-Z\s]+?)(?:\s*(?:CPF|CNPJ|Nome|\d{3}\.\d{3}))',
        texto_clean,
        re.IGNORECASE
    )
    if m_boleto:
        benef = m_boleto.group(1).strip()
        if validar_nome(benef, min_len=8):
            candidatos.append({'nome': benef, 'score': 10, 'metodo': 'BOLETO-razao-social'})
            debug_info['tipo'] = 'BOLETO'

    # PADRÃO: Favorecido
    m_fav = re.search(
        r'Favorecido[:\s]+([A-Z][A-Z\s]+?)(?:\s+Valor|\s+CNPJ|\s+CPF)',
        texto_clean,
        re.IGNORECASE
    )
    if m_fav:
        fav = m_fav.group(1).strip()
        if validar_nome(fav):
            candidatos.append({'nome': fav, 'score': 10, 'metodo': 'Favorecido'})

    # DECISÃO FINAL
    if candidatos:
        unicos = {}
        for c in candidatos:
            nome_norm = c['nome'].upper().strip()
            if nome_norm not in unicos or c['score'] > unicos[nome_norm]['score']:
                unicos[nome_norm] = c

        melhor = max(unicos.values(), key=lambda x: x['score'])
        debug_info['benef_metodo'] = melhor['metodo']
        debug_info['benef_score'] = melhor['score']
        debug_info['candidatos_total'] = len(candidatos)
        return melhor['nome']

    debug_info['benef_erro'] = "Nenhum candidato válido"
    return "FORNECEDOR_DESCONHECIDO"

def extrair_linha_digitavel(texto: str) -> str:
    """Extrai código de barras ou linha digitável"""
    if not texto:
        return ""
    texto_limpo = re.sub(r'\s+', '', texto)
    m = re.search(r'([0-9]{20,60})', texto_limpo)
    if m:
        return m.group(1)
    return ""

def montar_nome(benef, valor, contador, snippet):
    """Monta nome final do arquivo"""
    benef_safe = sanitize_filename(benef)
    valor_safe = valor if valor else "VALOR_NAO_ENCONTRADO"
    base = f"{benef_safe} - {valor_safe}"
    if snippet:
        base = f"{benef_safe} - {snippet} - {valor_safe}"
    if contador > 1:
        return f"{PIX_PREFIX}{contador} - {base}.pdf"
    else:
        return f"{base}.pdf"

# --- MAIN ---
def main():
    safe_makedirs(PASTA_SAIDA)
    if os.path.exists(TEMP_PARTS):
        shutil.rmtree(TEMP_PARTS, ignore_errors=True)
    safe_makedirs(TEMP_PARTS)

    arquivos_log = []
    debug_logs = []
    contador = defaultdict(int)
    processed_sources = set()

    originais = [f for f in os.listdir(PASTA_ENTRADA) if f.lower().endswith(".pdf") and "_part" not in f]

    print(f"\n{'='*70}")
    print(f"🔄 PROCESSAMENTO - Renomeação de Comprovantes")
    print(f"{'='*70}\n")
    print(f"📊 {len(originais)} arquivos PDF encontrados\n")

    for arq in originais:
        caminho = os.path.join(PASTA_ENTRADA, arq)
        separar_em_partes(caminho, TEMP_PARTS)

    partes = [os.path.join(TEMP_PARTS, f) for f in os.listdir(TEMP_PARTS) if f.lower().endswith(".pdf")]
    partes.sort()

    print(f"📄 {len(partes)} páginas para processar\n")

    for idx, part_path in enumerate(partes, 1):
        if part_path in processed_sources or not os.path.exists(part_path):
            continue

        processed_sources.add(part_path)
        debug_info = {'arquivo': os.path.basename(part_path)}

        texto = extrair_texto_pdf(part_path, debug_info)
        beneficiario = extrair_beneficiario(texto, debug_info)
        valor = extrair_valor(texto, debug_info)
        empresa = identificar_empresa(texto)

        print(f"[{idx}/{len(partes)}] {os.path.basename(part_path)}")
        print(f"  👤 Beneficiário: {beneficiario}")
        if 'benef_metodo' in debug_info:
            print(f"  ✓ Método: {debug_info['benef_metodo']} (score: {debug_info['benef_score']})")
        print(f"  💰 Valor: {valor}")
        print(f"  🏢 Empresa: {empresa}")

        chave = (empresa, beneficiario, valor)
        contador[chave] += 1
        cnt = contador[chave]

        snippet = ""
        if re.search(r"LINHA\s+DIGIT|CODIGO\s+DE\s+BARRAS", texto, re.IGNORECASE):
            seq = extrair_linha_digitavel(texto)
            if seq:
                snippet = seq[-BARCODE_TAIL_LEN:] if len(seq) >= BARCODE_TAIL_LEN else seq

        nome_arquivo = montar_nome(beneficiario, valor, cnt, snippet)
        pasta_empresa = os.path.join(PASTA_SAIDA, empresa)
        safe_makedirs(pasta_empresa)

        dest = os.path.join(pasta_empresa, nome_arquivo)
        sufixo = 1
        base_no_ext = os.path.splitext(nome_arquivo)[0]
        while os.path.exists(dest):
            dest = os.path.join(pasta_empresa, f"{base_no_ext}_{sufixo}.pdf")
            sufixo += 1

        shutil.copy2(part_path, dest)
        print(f"  ✅ {nome_arquivo}\n")

        debug_info['nome_final'] = nome_arquivo
        debug_logs.append(debug_info)

        arquivos_log.append({
            "source": os.path.basename(part_path),
            "empresa": empresa,
            "beneficiario": beneficiario,
            "valor": valor,
            "nome_final": os.path.basename(dest)
        })

    # Gerar logs
    with open(DEBUG_LOG, "w", encoding="utf-8") as f:
        for log in debug_logs:
            f.write(f"\n{'='*60}\n")
            for k, v in log.items():
                f.write(f"{k}: {v}\n")

    # Criar ZIP
    if os.path.exists(ZIP_FINAL):
        os.remove(ZIP_FINAL)

    with zipfile.ZipFile(ZIP_FINAL, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(PASTA_SAIDA):
            for f in files:
                full = os.path.join(root, f)
                arc = os.path.relpath(full, PASTA_SAIDA)
                zf.write(full, arc)

    with open(LOG_CSV, "w", encoding="utf-8") as csvf:
        csvf.write("source;empresa;beneficiario;valor;nome_final\n")
        for r in arquivos_log:
            csvf.write(f"{r['source']};{r['empresa']};{r['beneficiario']};{r['valor']};{r['nome_final']}\n")

    try:
        shutil.rmtree(TEMP_PARTS, ignore_errors=True)
    except:
        pass

    print(f"\n{'='*70}")
    print("✅ CONCLUÍDO!")
    print(f"{'='*70}")
    print(f"📦 ZIP: {ZIP_FINAL}")
    print(f"📋 Log: {LOG_CSV}")
    print(f"🐛 Debug: {DEBUG_LOG}")
    print(f"{'='*70}\n")

if __name__ == "__main__":
    main()
