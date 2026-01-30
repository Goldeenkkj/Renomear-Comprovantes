#!/usr/bin/env python3
"""
renomear_comprovantes_v16_bradesco_pix.py
- Usa PyPDF2 como extrator EXCLUSIVO para maior consistência.
- NOVO: Adiciona padrões específicos para o novo layout de comprovante PIX do Bradesco
  com seção "Controle de Pagamento" contendo "Beneficiário:" explícito.
- Mantém compatibilidade com todos os padrões anteriores (Sicoob, Santander, etc.)
"""
import os
import re
import shutil
import zipfile
import unicodedata
from pathlib import Path
from collections import defaultdict
from PyPDF2 import PdfReader, PdfWriter

# --- CONFIG ---
PASTA_ENTRADA = "entrada"
PASTA_SAIDA = "saida"
TEMP_PARTS = "temp_parts"
ZIP_FINAL = "comprovantes_renomeados_v16.zip"
LOG_CSV = "renomeacao_log_v16.csv"
DEBUG_LOG = "debug_extracao_v16.txt"
MAX_NAME_LEN = 60
BARCODE_TAIL_LEN = 6
PIX_PREFIX = "N"

EMPRESAS = {
    "LIFE_SCIENCE": ["FARMAUSA LIFE SCIENCE", "LIFE SCIENCE"],
    "URBANBOX": ["URBANBOX"],
    "PHARMACEUTICAL": ["FARMAUSA PHARMACEUTICAL", "FARMAUSA PHARMACEUTICAL LTDA"]
}

# --- HELPERS ---
def safe_makedirs(path):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

def limpar_texto(texto: str) -> str:
    if not texto:
        return ""
    # Substitui caracteres problemáticos e normaliza
    texto = texto.replace("\ufb01", "fi").replace("\ufb02", "fl").replace("\u2028", " ")
    texto = unicodedata.normalize("NFKC", texto)
    return texto

def sanitize_filename(name: str) -> str:
    name = (name or "").strip() 
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r'[\\/\\:\*\?"<>\|]', "", name)
    name = re.sub(r"[^0-9A-Za-z_\-\s\(\)\[\]]", "", name)
    name = "_".join(name.split())
    if len(name) > MAX_NAME_LEN:
        name = name[:MAX_NAME_LEN]
    return name or "FORNECEDOR_DESCONHECIDO"

def validar_nome(nome: str, min_len: int = 5) -> bool:
    """Valida se string parece um nome válido"""
    if not nome or len(nome) < min_len:
        return False
    # Se a string só contém números, pontos, hífens ou asteriscos, não é um nome.
    if not re.search(r'[a-zA-Z]', nome):
        return False
    
    # Se a string só contém dígitos, pontos, hífens, asteriscos e espaços, rejeita.
    if re.match(r'^[\d\.\-\*\s]+$', nome):
        return False
    
    # Rejeita empresas do próprio sistema
    empresas_sistema = ['FARMAUSA', 'URBANBOX', 'PHARMACEUTICAL', 'LIFE SCIENCE', 'SICOOB']
    nome_upper = nome.upper()
    for emp in empresas_sistema:
        if emp in nome_upper:
            # Permite se for um nome de cooperativa longo que contenha SICOOB, mas não seja o nome completo do banco
            if emp == 'SICOOB' and len(nome_upper) > 25 and 'SISTEMA DE COOPERATIVAS' not in nome_upper:
                continue
            return False
    
    # Rejeita campos técnicos
    rejeitar = [
        'agencia', 'conta', 'cpf', 'cnpj', 'chave', 'instituicao',
        'banco', 'dados', 'transferencia', 'pagamento', 'valor',
        'documento', 'autenticacao', 'controle', 'debito', 'origem',
        'destino', 'corrente', 'codigo', 'barras', 'linha', 'digitavel'
    ]
    nome_lower = nome.lower()
    for r in rejeitar:
        if nome_lower == r or nome_lower.startswith(r + ' '):
            return False
    
    # Deve ter letras
    if not re.search(r'[a-zA-Z]', nome):
        return False
    
    # Deve ter pelo menos 2 palavras ou 5 caracteres consecutivos de letras
    palavras = nome.split()
    
    if len(palavras) >= 2 or len(re.findall(r'[a-zA-Z]{5,}', nome)) > 0:
        return True
    
    return False

# --- FUNÇÕES DE EXTRAÇÃO (SÓ PYPDF2) ---

def _extrair_texto_pypdf2(path: str) -> str:
    """Extrai texto usando PyPDF2 (Padrão e Único)"""
    texto = ""
    try:
        with open(path, 'rb') as f:
            reader = PdfReader(f)
            for p in reader.pages:
                t = p.extract_text()
                if t:
                    texto += t + "\n"
    except Exception as e:
        pass
    return limpar_texto(texto)

def extrair_texto_pdf(path: str, debug_info: dict) -> str:
    """Função principal: usa apenas PyPDF2"""
    
    texto_pypdf2 = _extrair_texto_pypdf2(path)
    debug_info['extrator'] = 'PyPDF2 (Padrão: Único)'
    return texto_pypdf2

# --- FUNÇÃO DE SEPARAÇÃO (MANTIDA) ---

def separar_em_partes(orig_pdf: str, dest_folder: str):
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
        print(f"⚠️  Erro ao separar PDF {orig_pdf}: {e}")
        novos.append(orig_pdf)
    return novos

# --- FUNÇÕES DE EXTRAÇÃO DE DADOS ---

def identificar_empresa(texto: str) -> str:
    t = (texto or "").upper()
    for key, aliases in EMPRESAS.items():
        for a in aliases:
            if a.upper() in t:
                return key
    return "OUTROS"

def extrair_valor(texto: str, debug_info: dict) -> str:
    """Extrai valor priorizando contextos específicos"""
    if not texto:
        debug_info['valor_erro'] = "Texto vazio"
        return "VALOR_NAO_ENCONTRADO"
    
    valores_encontrados = []
    
    # ==========================================================================
    # NOVO V16: Padrão específico para Bradesco PIX
    # No layout Bradesco PIX, o valor aparece como número isolado (ex: 8.000,00)
    # após o CNPJ do beneficiário e antes de "R$0,00" (tarifa)
    # ==========================================================================
    
    # Padrão V16: Captura valor no formato brasileiro que aparece após CNPJ
    m_bradesco_valor = re.search(
        r'(?:\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2})\s+(\d{1,3}(?:\.\d{3})*,\d{2})\s+R\$0,00',
        texto,
        re.IGNORECASE
    )
    if m_bradesco_valor:
        val = m_bradesco_valor.group(1).strip()
        try:
            val_conv = val.replace(".", "").replace(",", ".")
            valor_float = float(val_conv)
            if valor_float > 0:
                valores_encontrados.append({
                    'valor': val,
                    'tipo': 'bradesco_pix_apos_cnpj',
                    'prioridade': 0,
                    'float': valor_float
                })
        except:
            pass
    
    # Padrão V16 alternativo: valor antes de R$0,00 (tarifa zero)
    # Útil quando há email ou outros dados entre CNPJ e valor
    if not valores_encontrados:
        m_bradesco_valor_alt = re.search(
            r'(\d{1,3}(?:\.\d{3})*,\d{2})\s+R\$0,00',
            texto,
            re.IGNORECASE
        )
        if m_bradesco_valor_alt:
            val = m_bradesco_valor_alt.group(1).strip()
            try:
                val_conv = val.replace(".", "").replace(",", ".")
                valor_float = float(val_conv)
                if valor_float > 0:
                    valores_encontrados.append({
                        'valor': val,
                        'tipo': 'bradesco_pix_antes_tarifa',
                        'prioridade': 0,
                        'float': valor_float
                    })
            except:
                pass
    
    patterns = [
        (r"Valor\s+principal[:\s]*R?\$?\s*([\d\.\,]+)", 1, "principal"),
        (r"Valor\s+total\s+pago[:\s]*R?\$?\s*([\d\.\,]+)", 1, "total_pago"),
        (r"Valor\s+do\s+pagamento[:\s]*R?\$?\s*([\d\.\,]+)", 2, "pagamento"),
        (r"Valor\s+total[:\s]*R?\$?\s*([\d\.\,]+)", 3, "total"),
        (r"Favorecido:.*?Valor[:\s]*R?\$?\s*([\d\.\,]+)", 2, "favorecido_valor"),
        (r"(?:^|\n)Valor[:\s]*R?\$?\s*([\d\.\,]+)", 4, "valor_linha"),
        (r"Valor:R\$\s*([\d\.\,]+)", 1, "bradesco_pix_valor"),
        (r"R\$\s*([\d\.\,]+)(?!\s*(?:Juros|Multa|Desconto|Bonif))", 5, "rs_isolado"),
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
    
    debug_info['valores_encontrados'] = [f"{v['tipo']}={v['valor']}" for v in valores_encontrados]
    
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
    """Extração ultra específica por tipo de banco"""
    if not texto:
        debug_info['benef_erro'] = "Texto vazio"
        return "FORNECEDOR_DESCONHECIDO"
    
    texto_clean = re.sub(r'\s+', ' ', texto)
    candidatos = []
    
    # ==========================================================================
    # NOVO PADRÃO V16 (Score 22): BRADESCO PIX - Controle de Pagamento
    # ==========================================================================
    # Este é o padrão MAIS CONFIÁVEL para o novo layout do Bradesco PIX.
    # A seção "Controle de Pagamento" contém explicitamente "Beneficiário: NOME"
    # Exemplo: "Controle de Pagamento Beneficiário: MARIO PASTORE CPF/CNPJ:"
    # ==========================================================================
    m_bradesco_controle = re.search(
        r'Controle\s+de\s+Pagamento\s+Benefici[aá]rio:\s*([A-ZÀ-ÚÇ][A-ZÀ-ÚÇ\s\.\-\(\)0-9]+?)(?:\s*CPF/CNPJ:|\s*Controle:|\s*$)',
        texto_clean,
        re.IGNORECASE
    )
    if m_bradesco_controle:
        nome = m_bradesco_controle.group(1).strip()
        nome = re.sub(r'\s+', ' ', nome).upper()
        # Remove possíveis números de documento que possam ter sido capturados
        nome = re.sub(r'\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}', '', nome).strip()
        nome = re.sub(r'\d{3}\.\d{3}\.\d{3}-\d{2}', '', nome).strip()
        if validar_nome(nome, min_len=5):
            candidatos.append({'nome': nome, 'score': 22, 'metodo': 'BRADESCO-PIX-ControlePagamento-V16'})
            debug_info['tipo'] = 'BRADESCO-PIX'
    
    # ==========================================================================
    # NOVO PADRÃO V16 (Score 21): BRADESCO PIX - Após CNPJ da empresa pagadora
    # ==========================================================================
    # No layout Bradesco PIX, após "CNPJ: 37.124.240/0001-08" (empresa pagadora)
    # vem o nome do beneficiário seguido do CNPJ do beneficiário
    # Exemplo: "37.124.240/0001-08 MARIO PASTORE 50.894.589/0001-97"
    # ==========================================================================
    m_bradesco_apos_cnpj = re.search(
        r'37\.124\.240/0001-08\s+([A-ZÀ-ÚÇ][A-ZÀ-ÚÇ\s\.\-\(\)]+?)(?:\s+\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\s+\d{3}\.\d{3}\.\d{3}-\d{2}|\s*$)',
        texto_clean,
        re.IGNORECASE
    )
    if m_bradesco_apos_cnpj:
        nome = m_bradesco_apos_cnpj.group(1).strip()
        nome = re.sub(r'\s+', ' ', nome).upper()
        if validar_nome(nome, min_len=5):
            candidatos.append({'nome': nome, 'score': 21, 'metodo': 'BRADESCO-PIX-AposCNPJ-V16'})
            debug_info['tipo'] = debug_info.get('tipo', 'BRADESCO-PIX')
    
    # ==========================================================================
    # NOVO PADRÃO V16 (Score 20): BRADESCO PIX - Dados de quem recebeu + Nome:
    # ==========================================================================
    # Captura o nome na seção "Dados de quem recebeu" seguido de "Nome:"
    # O nome aparece DEPOIS do label "Nome:" neste layout específico
    # ==========================================================================
    if 'Bradesco' in texto or 'bradesco' in texto.lower():
        # Padrão para capturar nome após "Dados de quem recebeu" e "Nome:"
        m_bradesco_recebeu = re.search(
            r'Dados\s+de\s+quem\s+recebeu.*?Nome:\s*([A-ZÀ-ÚÇ][A-ZÀ-ÚÇ\s\.\-\(\)]+?)(?:\s*CPF/CNPJ:|\s*Institui[çc][aã]o|\s*$)',
            texto_clean,
            re.IGNORECASE | re.DOTALL
        )
        if m_bradesco_recebeu:
            nome = m_bradesco_recebeu.group(1).strip()
            nome = re.sub(r'\s+', ' ', nome).upper()
            if validar_nome(nome, min_len=5):
                candidatos.append({'nome': nome, 'score': 20, 'metodo': 'BRADESCO-PIX-DadosRecebeu-V16'})
                debug_info['tipo'] = debug_info.get('tipo', 'BRADESCO-PIX')

    # --- NOVO PADRÃO V19 (Score 19): SICOOB TED (Alta Prioridade) ---
    # Procura por: Crédito: → Nome: [NOME]
    m_sicoob_ted = re.search(
        r'Cr[ée]dito:\s*Nome:\s*([A-Z][A-Z\s\.\-]{5,100}?)(?:\s*CPF/CNPJ|\s*Instituição|\s*Chave|\s*Agência|\s*Conta|$)', 
        texto_clean, 
        re.IGNORECASE | re.DOTALL
    )
    if m_sicoob_ted:
        nome = m_sicoob_ted.group(1).strip()
        nome = re.sub(r'\s+', ' ', nome).upper()
        if validar_nome(nome, min_len=5):
            candidatos.append({'nome': nome, 'score': 19, 'metodo': 'SICOOB-TED-V19'})
            
    # --- NOVO PADRÃO V15 (Score 15): PIX Bradesco Invertido ---
    if "PIX" in texto.upper():
        debug_info['tipo'] = debug_info.get('tipo', 'PIX')

        # Score 15: Procura pelo nome do recebedor (que não seja a própria empresa)
        # no bloco 'Dados de quem recebeu' / 'Nome:' ou 'Destinatário : Nome :'.
        m_pix = re.search(
            r'(?:Dados de quem recebeu|Destina\s*t[aá]rio\s*:).*?Nome\s*:\s*([A-Z][A-Z\s\.\-]{5,100}?)(?:\s*CPF/CNPJ|\s*Instituição|\s*Chave|\s*Agência|\s*Conta|$)', 
            texto, 
            re.IGNORECASE | re.DOTALL
        )
        # Padrão V10/V11 (Score 14): Caso o nome esteja antes do 'Nome:' (Bradesco invertido)
        if not m_pix:
             m_pix = re.search(
                r'(?:CPF/CNPJ|CNPJ)?:?\s*([A-Z][A-Z\s\.\-]{5,100}?)\s*Nome:', 
                texto_clean, 
                re.IGNORECASE | re.DOTALL
            )
            
        if m_pix:
            raw_nome = m_pix.group(1).strip()
            nome = re.sub(r'[^A-Z\s\.]', ' ', raw_nome, flags=re.IGNORECASE).strip()
            nome = re.sub(r'\s+', ' ', nome).upper()
            
            # Checa se o nome encontrado não é o nome da própria empresa (pagador)
            if 'FARMAUSA' in nome or 'URBANBOX' in nome:
                debug_info['benef_metodo_skip'] = 'PIX-rejeitou-pagador'
            elif validar_nome(nome, min_len=8):
                score = 15 if 'Dados de quem recebeu' in texto else 14
                candidatos.append({'nome': nome, 'score': score, 'metodo': f'PIX-recebedor-V15-score{score}'})
    
    # --- NOVO PADRÃO V15 (Score 13): Santander PIX/Pagamento ---
    # Captura Santander PIX/Transferência onde o nome vem depois de 'Dados do recebedor Para'
    m_santander_pix = re.search(
        r'Dados do recebedor Para\s*(?:[0-9\s]+)?\s*([A-Z][A-Z\s\-\(\).]{5,100}?)(?:\s*Chave|\s*CPF/CNPJ|\s*Agência|\s*Conta|$)', 
        texto_clean, 
        re.IGNORECASE | re.DOTALL
    )
    if m_santander_pix:
        nome = m_santander_pix.group(1).strip()
        nome = re.sub(r'\s+', ' ', nome).upper()
        if validar_nome(nome, min_len=8):
             candidatos.append({'nome': nome, 'score': 13, 'metodo': 'SANTANDER-PIX-recebedor-V15'})
             
    # --- NOVO PADRÃO V15 (Score 12): BOLETO Reverso ---
    # Captura nomes que aparecem ANTES de suas tags (Formato Bradesco/Caixa etc.)
    m_boleto_rev = re.search(
        r'([A-Z][A-Z\s\-\(\).]{5,100}?)\s+Raz[aã]o\s+Social\s+Benefici[aá]rio(?:\s+Final)?\s*:', 
        texto_clean, 
        re.IGNORECASE
    )
    if m_boleto_rev:
        nome = m_boleto_rev.group(1).strip()
        nome = re.sub(r'\s+', ' ', nome).upper()
        if validar_nome(nome, min_len=8):
            candidatos.append({'nome': nome, 'score': 12, 'metodo': 'BOLETO-razao-social-reversa-V15'})
    
    # --- DEMAIS PADRÕES (mantidos como fallback) ---
    
    # SICOOB (Score 10)
    if 'SICOOB' in texto.upper():
        debug_info['tipo'] = debug_info.get('tipo', 'SICOOB')
        
        m = re.search(
            r'(?:Nome/Raz[aã]o Social:|Conv[êe]nio:|Benefici[aá]rio:|Nome:)\s*([A-Z0-9\s\.\-]+?)(?:\s*Nome Fantasia|\s*CPF/CNPJ|\s*Pagador|\s*Cr[ée]dito:|\s*Autentica[çc][aã]o|$)', 
            texto, 
            re.IGNORECASE | re.DOTALL
        )
        
        if m:
            benef = m.group(1).strip()
            
            # Adiciona verificação explícita para o nome do próprio banco Sicoob
            if 'SICOOB' in benef.upper() and 'SISTEMA DE COOPERATIVAS' in benef.upper():
                debug_info['benef_metodo_skip'] = 'SICOOB-rejeitou-banco'
            else:
                # Mapeamentos de nomes (mantidos)
                if benef.upper().strip() == "DALL PHYT OLAB S A": benef = "DALL PHYTO LAB S.A."
                if benef.upper().strip() == "PREF SP DAMSP": benef = "PREFEITURA MUNICIPAL DE SAO PAULO"
                if benef.upper().strip() == "PRO AN QUIM E DIAGNOSTICA LTDA": benef = "PRO AN QUIMICA E DIAGNOSTICA LTDA"
                if benef.upper().strip() == "SUPRICORP SUPRIMENTOS LTDA": benef = "SUPRICORP SUPRIMENTOS LTDA"
                if benef.upper().strip() == "SUPER EPI EQUIPAM E PROTECAO INDIVIDUAL": benef = "SUPER EPI EQUIPAMENTOS E PROTECAO INDIVIDUAL"
                if benef.upper().strip() == "ANHANGUERA COM DE FERR LTDA": benef = "ANHANGUERA COM DE FERRO LTDA"
                if benef.upper().strip() == "KALUNGA SA": benef = "KALUNGA S.A."
                if benef.upper().strip() == "XP INVESTIMENTOS": benef = "XP INVESTIMENTOS"
                if benef.upper().strip() == "FARMAUSA LIFE SCIENCE": benef = "FARMAUSA LIFE SCIENCE"
                
                if validar_nome(benef, min_len=5):
                    candidatos.append({'nome': benef, 'score': 10, 'metodo': 'SICOOB-beneficiario'})

    # SANTANDER BOLETO - Beneficiário Original (Score 10)
    if 'Santander' in texto and 'Dados do Beneficiário Original' in texto:
        debug_info['tipo'] = debug_info.get('tipo', 'SANTANDER-TITULO')
        m = re.search(r'Benefici[aá]rio Original.*?Raz[aã]o Social:\s*(?P<beneficiario>[A-Z][A-Z\s\-\(\)]+?)(?:\s*Nome Fantasia|\s*Dados do Pagador)', texto_clean, re.IGNORECASE | re.DOTALL)
        if m:
            benef = m.group('beneficiario').strip()
            if validar_nome(benef, min_len=5):
                candidatos.append({'nome': benef, 'score': 10, 'metodo': 'SANTANDER-beneficiario-original'})
    
    # IMPOSTO/TAXAS (Score 10/9)
    if re.search(r'IMPOSTO|TAXA', texto, re.IGNORECASE):
        debug_info['tipo'] = debug_info.get('tipo', 'IMPOSTO')
        m = re.search(r'Empresa[\\/\s]+[OÓ]rg[aã]o[:\s]+([A-Z][A-Z0-9\-\s]{5,50}?)(?:\s*\d{2}\.\d{3}|\s*Codigo|\s*CNPJ)', texto_clean, re.IGNORECASE)
        if m:
            orgao = m.group(1).strip()
            if validar_nome(orgao):
                candidatos.append({'nome': orgao, 'score': 10, 'metodo': 'IMPOSTO-regex'})
        
        nomes_grerj = re.findall(r'\b(RJ-[A-Z\s]+(?:ELETRONICA|DIGITAL)?)\b', texto_clean, re.IGNORECASE)
        for nome in nomes_grerj:
            if validar_nome(nome):
                candidatos.append({'nome': nome.strip(), 'score': 9, 'metodo': 'IMPOSTO-padrao-RJ'})
    
    # BOLETO Padrão Forward (Score 10/9)
    if 'Boleto' in texto or 'Beneficiário' in texto or 'Razão Social' in texto:
        debug_info['tipo'] = debug_info.get('tipo', 'BOLETO')
        
        # --- NOVO PADRÃO V19 (Score 10): SICOOB BOLETO ---
        m_sicoob_boleto = re.search(
            r'Benefici[aá]rio:\s*Nome/Raz[aã]o\s*Social:\s*([A-Z][A-Z\s\.\-]{5,100}?)(?:\s*CPF/CNPJ|\s*Instituição|\s*Chave|\s*Agência|\s*Conta|$)', 
            texto_clean, 
            re.IGNORECASE | re.DOTALL
        )
        if m_sicoob_boleto:
            nome = m_sicoob_boleto.group(1).strip()
            nome = re.sub(r'\s+', ' ', nome).upper()
            if validar_nome(nome, min_len=5):
                candidatos.append({'nome': nome, 'score': 10, 'metodo': 'SICOOB-BOLETO-V19'})
        
        # Razão Social Beneficiário (Forward)
        m = re.search(
            r'Raz[aã]o\s+Social\s+Benefici[aá]rio[:\s]+([A-Z][A-Z\s]+LTDA|[A-Z][A-Z\s]+S\.?A\.?|[A-Z][A-Z\s]{10,60}?)(?:\s*(?:013|037|CPF|CNPJ|Nome|Banco|\d{3}\.\d{3}))',
            texto_clean,
            re.IGNORECASE
        )
        if m:
            benef = m.group(1).strip()
            if validar_nome(benef, min_len=8):
                candidatos.append({'nome': benef, 'score': 10, 'metodo': 'BOLETO-razao-social-forward'})
        
        # Beneficiário Final (Forward)
        m = re.search(
            r'Benefici[aá]rio\s+Final[:\s]+([A-Z][A-Z\s]+?)(?:\s*(?:CPF|CNPJ|Razao|\d{3}\.\d{3}))',
            texto_clean,
            re.IGNORECASE
        )
        if m:
            benef = m.group(1).strip()
            if validar_nome(benef, min_len=8):
                candidatos.append({'nome': benef, 'score': 9, 'metodo': 'BOLETO-final-forward'})
    
    # Santander Favorecido (Transferência simples) (Score 10)
    m = re.search(r'Favorecido[:\s]+([A-Z][A-Z\s]+?)(?:\s+Valor|\s+CNPJ|\s+CPF)', texto_clean, re.IGNORECASE)
    if m:
        debug_info['tipo'] = debug_info.get('tipo', 'SANTANDER')
        fav = m.group(1).strip()
        if validar_nome(fav):
            candidatos.append({'nome': fav, 'score': 10, 'metodo': 'Santander-favorecido'})

    # --- DECISÃO FINAL ---
    if candidatos:
        unicos = {}
        for c in candidatos:
            nome_norm = c['nome'].upper().strip()
            # Garante que, se for o mesmo nome, o de maior score vence
            if nome_norm not in unicos or c['score'] > unicos[nome_norm]['score']:
                unicos[nome_norm] = c
        
        melhor = max(unicos.values(), key=lambda x: x['score'])
        debug_info['benef_metodo'] = melhor['metodo']
        debug_info['benef_score'] = melhor['score']
        debug_info['candidatos_total'] = len(candidatos)
        return melhor['nome']
    
    debug_info['benef_erro'] = "Nenhum candidato válido"
    return "FORNECEDOR_DESCONHECIDO"

def extrair_linha_digitavel_ou_codbarra(texto: str) -> str:
    if not texto:
        return ""
    texto_limpo = re.sub(r'\s+', '', texto)
    m = re.search(r'([0-9]{20,60})', texto_limpo)
    if m:
        return m.group(1)
    return ""

def montar_nome(benef, valor, contador, snippet):
    benef_safe = sanitize_filename(benef)
    valor_safe = valor if valor else "VALOR_NAO_ENCONTRADO"
    base = f"{benef_safe} - {valor_safe}"
    if snippet:
        base = f"{benef_safe} - {snippet} - {valor_safe}"
    if contador > 1:
        return f"{PIX_PREFIX}{contador} - {base}.pdf" 
    else:
        return f"{base}.pdf"

def tratar_fornecedor_desconhecido(texto: str) -> str:
    """
    Tenta extrair o beneficiário quando extrair_beneficiario retorna
    'FORNECEDOR_DESCONHECIDO'. Não altera nenhuma outra parte do seu código.
    """
    if not texto:
        return "FORNECEDOR_DESCONHECIDO"

    # normalize spaces and uppercase for regex matching
    texto_orig = texto
    texto_upper = re.sub(r'\s+', ' ', texto).upper()

    # padrões com prioridade alta (SICOOB/Bradesco/Santander)
    padroes = [
        # NOVO V16: Controle de Pagamento Beneficiário: (Bradesco PIX)
        (r'CONTROLE DE PAGAMENTO\s+BENEFICI[AÁ]RIO:\s*([A-Z0-9\.\-&\s\(\)\/ÇÃÕÉÊÍÓÚÀÂ]{5,120})', 105),
        # Conta: ... / NOME (ex.: "Conta: 63.498-0 / FARMAUSA LIFE SCIENCE LTDA")
        (r'Conta[:\s]*[^\n/]+/\s*([A-Z0-9\.\-&\s\(\)\/]{5,120})', 100),
        # Crédito: ... Nome:
        (r'Cr[eé]dito[:\s\S]{0,300}?Nome[:\s]*([A-Z0-9\.\-&\s\(\)\/]{5,120})', 95),
        # Débito: ... Nome:
        (r'D[eé]bito[:\s\S]{0,300}?Nome[:\s]*([A-Z0-9\.\-&\s\(\)\/]{5,120})', 94),
        # Dados de quem recebeu ... Nome:
        (r'DADOS DE QUEM RECEBEU[\s\S]{0,200}?NOME[:\s]*([A-Z0-9\.\-&\s\(\)\/]{5,120})', 92),
        # Favorecido:
        (r'FAVORECIDO[:\s]*([A-Z0-9\.\-&\s\(\)\/]{5,120})', 90),
        # Razão Social Beneficiário Final:
        (r'RAZ[ÃA]O\s+SOCIAL\s+BENEFICIAR?IO(?:\s+FINAL)?[:\s]*([A-Z0-9\.\-&\s\(\)\/]{5,120})', 88),
        # Empresa / Órgão: (caso IMPOSTO/TAXAS)
        (r'EMPRESA\s*(?:/|\\s)?\s*ÓRG[ÃA]O[:\s]*([A-Z0-9\.\-&\s\(\)\/]{5,120})', 85),
        # Linha "PARA <nome>" (PIX layouts onde 'Para' precede o nome)
        (r'\bPARA[:\s]*([A-Z0-9\.\-&\s\(\)\/]{5,120})', 80),
    ]

    candidatos = []
    for pat, score in padroes:
        m = re.search(pat, texto_upper, re.IGNORECASE | re.DOTALL)
        if m:
            nome = m.group(1).strip()
            # cleanup: collapse multiple spaces, remove trailing slashes etc.
            nome = re.sub(r'[\t\n\r]+', ' ', nome)
            nome = re.sub(r'\s+', ' ', nome).strip(" /-")
            # Remove CPF/CNPJ que possam ter sido capturados
            nome = re.sub(r'\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}', '', nome).strip()
            nome = re.sub(r'\d{3}\.\d{3}\.\d{3}-\d{2}', '', nome).strip()
            
            # keep original casing attempt
            snippet_raw = None
            try:
                idx = texto_orig.upper().find(nome.upper())
                if idx != -1:
                    snippet_raw = texto_orig[idx: idx + len(nome)]
            except Exception:
                snippet_raw = None

            candidato_texto = snippet_raw if snippet_raw else nome

            # validar com a função existente
            if validar_nome(candidato_texto, min_len=5):
                candidatos.append((candidato_texto.strip(), score))

    # Fallback heurístico: buscar a maior linha em caixa alta sem palavras técnicas
    if not candidatos:
        lines = [ln.strip() for ln in texto_orig.splitlines() if ln.strip()]
        best = None
        for ln in lines:
            ln_upper = ln.upper()
            if len(ln) >= 6 and re.search(r'[A-ZÀ-Ú]', ln_upper):
                # descarta linhas com palavras técnicas que não são nomes
                if re.search(r'\b(AGENCIA|CONTA|CPF|CNPJ|CHAVE|BANC|VALOR|LOTE|NSU|LINHA|BARRAS|AUTENTICA)\b', ln_upper):
                    continue
                # escolhe a linha mais longa válida
                if validar_nome(ln, min_len=5):
                    if not best or len(ln) > len(best):
                        best = ln
        if best:
            candidatos.append((best.strip(), 50))

    # escolher melhor candidato por score
    if candidatos:
        candidatos.sort(key=lambda x: -x[1])
        melhor = candidatos[0][0].strip()
        return melhor

    return "FORNECEDOR_DESCONHECIDO"

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
    print(f"🔄 PROCESSAMENTO - Versão V16 (Bradesco PIX Novo Layout)") 
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
        
        debug_info['texto_bruto_inicio'] = texto[:1000].replace('\n', ' ')

        # Extrai beneficiário normalmente
        beneficiario = extrair_beneficiario(texto, debug_info)

        # Caso não reconheça, tenta heurística adicional
        if beneficiario == "FORNECEDOR_DESCONHECIDO":
            beneficiario = tratar_fornecedor_desconhecido(texto)

        valor = extrair_valor(texto, debug_info)
        empresa = identificar_empresa(texto)

        print(f"[{idx}/{len(partes)}] {os.path.basename(part_path)}")
        print(f"  📖 Extrator: {debug_info['extrator']}")
        print(f"  📋 Tipo: {debug_info.get('tipo', '?')}")
        print(f"  👤 Beneficiário: {beneficiario}")
        if 'benef_metodo' in debug_info:
            print(f"     ✓ Método: {debug_info['benef_metodo']} (score: {debug_info['benef_score']})")
        else:
            print(f"     ✗ {debug_info.get('benef_erro', 'Erro desconhecido')}")
        
        print(f"  💰 Valor: {valor}")
        if 'valor_selecionado' in debug_info:
            print(f"     ✓ {debug_info['valor_selecionado']}")
        
        print(f"  🏢 Empresa: {empresa}")

        chave = (empresa, beneficiario, valor)
        contador[chave] += 1
        cnt = contador[chave]

        snippet = ""
        if re.search(r"LINHA\s+DIGIT|CODIGO\s+DE\s+BARRAS|NOSSO\s+N", texto, re.IGNORECASE):
            seq = extrair_linha_digitavel_ou_codbarra(texto)
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

    # Log e ZIP
    with open(DEBUG_LOG, "w", encoding="utf-8") as f:
        for log in debug_logs:
            f.write(f"\n{'='*60}\n")
            for k, v in log.items():
                f.write(f"{k}: {v}\n")

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
