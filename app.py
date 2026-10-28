
"""Gerador de Orçamentos de Cirurgia Robótica — HUGF.

Aplicação Streamlit autônoma. Modalidade obrigatória e preservada em:
formulário, dados, SQLite, PDF, exclusões e relatório comercial.

ATENÇÃO: informações financeiras, textos jurídicos, LGPD e condições
comerciais devem ser validados pelas áreas responsáveis antes da produção.
"""

import html
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
import streamlit as st
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas
from reportlab.platypus import KeepInFrame, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# -----------------------------------------------------------------------------
# CONFIGURAÇÃO
# -----------------------------------------------------------------------------
st.set_page_config(page_title="Orçamentos Robótica - HUGF", page_icon="🏥", layout="wide")
VERDE = colors.HexColor("#008C50")
CINZA = colors.HexColor("#1F2937")
BORDA = colors.HexColor("#E5E7EB")
FUNDO = colors.HexColor("#F9FAFB")
DB_FILE = os.getenv("HUGF_DB_PATH", "orcamentos.db")
WHATSAPP = "554891815582"  # conferir com Concierge antes de publicar
DESTINATARIOS_ALERTA = [
    "comercialhospital@unimedflorianopolis.com.br",
    "cliente.particular@unimedflorianopolis.com.br",
]
STATUS = ("Pendente", "Em negociação", "Aprovado", "Recusado", "Cancelado")


def normalizar_tipo_orcamento(valor):
    """Nunca presume HÍBRIDO em caso de campo vazio/inválido."""
    s = unicodedata.normalize("NFKD", str(valor or ""))
    s = s.encode("ascii", "ignore").decode("ascii").strip().upper()
    if "HIBRID" in s:
        return "HÍBRIDO"
    if "PARTICULAR" in s or s in {"TOTAL", "100%"}:
        return "PARTICULAR"
    raise ValueError("Selecione HÍBRIDO ou 100% PARTICULAR para gerar o orçamento.")


def dinheiro(valor):
    return "R$ {:,.2f}".format(float(valor or 0)).replace(",", "X").replace(".", ",").replace("X", ".")


def seguro(valor):
    return html.escape(str(valor if valor is not None else ""), quote=False).replace("\n", "<br/>")


def valor_float(valor):
    """Converte número retornado pela IA (inclusive R$ 1.234,56)."""
    if isinstance(valor, (int, float)):
        return max(0.0, float(valor))
    s = str(valor or "").replace("R$", "").replace(" ", "")
    if not s:
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return max(0.0, float(s))
    except ValueError:
        return 0.0


# -----------------------------------------------------------------------------
# SQLite: mantém a estrutura da versão recebida; não cria outro banco/tabela.
# -----------------------------------------------------------------------------
def conectar():
    conn = sqlite3.connect(DB_FILE, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with conectar() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orcamentos (
                numero_orcamento TEXT PRIMARY KEY,
                paciente TEXT,
                medico TEXT,
                tipo_orcamento TEXT,
                data_solicitacao TEXT,
                data_validade TEXT,
                tempo_cc TEXT,
                acomodacao TEXT,
                valor_total REAL,
                created_at TIMESTAMP,
                status TEXT DEFAULT 'Pendente',
                alerta_enviado INTEGER DEFAULT 0
            )
        """)
        conn.commit()


def salvar_orcamento_db(dados):
    modalidade = normalizar_tipo_orcamento(dados.get("tipo_orcamento"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conectar() as conn:
        conn.execute("""
            INSERT INTO orcamentos
              (numero_orcamento, paciente, medico, tipo_orcamento,
               data_solicitacao, data_validade, tempo_cc, acomodacao,
               valor_total, created_at, status, alerta_enviado)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT status FROM orcamentos WHERE numero_orcamento=?), 'Pendente'),
                    COALESCE((SELECT alerta_enviado FROM orcamentos WHERE numero_orcamento=?), 0))
            ON CONFLICT(numero_orcamento) DO UPDATE SET
              paciente=excluded.paciente,
              medico=excluded.medico,
              tipo_orcamento=excluded.tipo_orcamento,
              data_solicitacao=excluded.data_solicitacao,
              data_validade=excluded.data_validade,
              tempo_cc=excluded.tempo_cc,
              acomodacao=excluded.acomodacao,
              valor_total=excluded.valor_total,
              created_at=excluded.created_at,
              alerta_enviado=0
        """, (
            dados["numero_orcamento"], dados["paciente"], dados["medico"],
            modalidade, dados["data_solicitacao"], dados["data_validade"],
            dados["tempo_cc"], dados["acomodacao"], dados["valor_total"], now,
            dados["numero_orcamento"], dados["numero_orcamento"],
        ))
        conn.commit()


def carregar_orcamentos_df():
    with conectar() as conn:
        return pd.read_sql_query(
            "SELECT * FROM orcamentos ORDER BY created_at DESC", conn
        )


def atualizar_status_db(numero, status):
    if status not in STATUS:
        raise ValueError("Status inválido")
    with conectar() as conn:
        conn.execute("UPDATE orcamentos SET status=? WHERE numero_orcamento=?", (status, numero))
        conn.commit()


def pendentes_48h():
    prazo = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    with conectar() as conn:
        rows = conn.execute("""
            SELECT * FROM orcamentos
            WHERE status='Pendente' AND alerta_enviado=0 AND created_at <= ?
            ORDER BY created_at ASC
        """, (prazo,)).fetchall()
    return [dict(r) for r in rows]


def marcar_alerta_enviado(numeros):
    if not numeros:
        return
    with conectar() as conn:
        conn.executemany(
            "UPDATE orcamentos SET alerta_enviado=1 WHERE numero_orcamento=?",
            [(n,) for n in numeros],
        )
        conn.commit()


def segredos(chave, padrao=""):
    try:
        valor = st.secrets.get(chave, os.getenv(chave, padrao))
        return str(valor).strip().strip('"\'')
    except Exception:
        return str(os.getenv(chave, padrao)).strip()


def enviar_email_alerta(orcamentos_pendentes):
    """Somente envia quando o usuário clicar no botão; nunca em import/teste."""
    if not orcamentos_pendentes:
        return False, "Nenhum orçamento pendente há mais de 48 horas."
    servidor = segredos("SMTP_SERVER", "smtp.gmail.com")
    porta = int(segredos("SMTP_PORT", "587"))
    usuario = segredos("SMTP_USER")
    senha = segredos("SMTP_PASSWORD")
    if not usuario or not senha:
        return False, "Configure SMTP_USER e SMTP_PASSWORD nos Secrets do Streamlit."
    linhas = []
    for item in orcamentos_pendentes:
        colunas = (
            item["numero_orcamento"], item["paciente"], item["medico"],
            item["tipo_orcamento"], item["data_solicitacao"],
            dinheiro(item["valor_total"]),
        )
        linhas.append("<tr>" + "".join(
            f"<td style='padding:7px;border:1px solid #ddd'>{html.escape(str(x))}</td>"
            for x in colunas
        ) + "</tr>")
    corpo = (
        "<html><body style='font-family:Arial'>"
        "<h2 style='color:#008C50'>Hospital Unimed Grande Florianópolis</h2>"
        "<p><b>Alerta: orçamentos de cirurgia robótica pendentes há mais de 48 horas.</b></p>"
        "<table style='border-collapse:collapse'>"
        "<tr><th>Orçamento</th><th>Paciente</th><th>Médico</th><th>Modalidade</th>"
        "<th>Solicitação</th><th>Valor</th></tr>" + "".join(linhas) +
        "</table><p>Verifique o acompanhamento e atualize o status no painel.</p>"
        "</body></html>"
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "ALERTA 48H - Orçamentos de Cirurgia Robótica Pendentes"
    msg["From"] = usuario
    msg["To"] = ", ".join(DESTINATARIOS_ALERTA)
    msg.attach(MIMEText(corpo, "html", "utf-8"))
    try:
        with smtplib.SMTP(servidor, porta, timeout=25) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(usuario, senha)
            server.sendmail(usuario, DESTINATARIOS_ALERTA, msg.as_string())
        marcar_alerta_enviado([x["numero_orcamento"] for x in orcamentos_pendentes])
        return True, f"Alertas enviados para {len(orcamentos_pendentes)} orçamento(s)."
    except Exception as exc:
        return False, f"Falha no SMTP: {exc}"


# -----------------------------------------------------------------------------
# PDF REPORTLAB
# -----------------------------------------------------------------------------
class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, orc_number="", **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []
        self.orc_number = orc_number

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.decorate(total_pages)
            super().showPage()
        super().save()

    def decorate(self, page_count):
        self.saveState()
        w, h = A4
        m = 1.1 * cm
        logo = Path("unimed_logo_official.png")
        if logo.exists():
            lw = 3.4 * cm
            lh = lw / (1560 / 646)
            try:
                self.drawImage(str(logo), m, h - 1.65 * cm, width=lw, height=lh, mask="auto")
                start_x = m + lw + 0.24 * cm
            except Exception:
                start_x = m
        else:
            start_x = m
        self.setFillColor(CINZA)
        self.setFont("Helvetica-Bold", 8)
        self.drawString(start_x, h - 0.95 * cm, "HOSPITAL UNIMED GRANDE FLORIANÓPOLIS")
        self.setFillColor(VERDE)
        self.setFont("Helvetica-Bold", 7)
        self.drawString(start_x, h - 1.25 * cm, "PROGRAMA DE CIRURGIA ROBÓTICA")
        self.setFillColor(CINZA)
        self.setFont("Helvetica-Bold", 7)
        self.drawRightString(w - m, h - 0.8 * cm, "ORÇAMENTO Nº")
        self.setFillColor(VERDE)
        self.setFont("Helvetica-Bold", 12)
        self.drawRightString(w - m, h - 1.27 * cm, str(self.orc_number))
        self.setStrokeColor(BORDA)
        self.line(m, h - 1.76 * cm, w - m, h - 1.76 * cm)
        self.line(m, 1.13 * cm, w - m, 1.13 * cm)
        # QR do concierge
        q = qr.QrCodeWidget(f"https://wa.me/{WHATSAPP}")
        bounds = q.getBounds()
        qw, qh = bounds[2] - bounds[0], bounds[3] - bounds[1]
        size = 18
        drawing = Drawing(size, size, transform=[size / qw, 0, 0, size / qh, 0, 0])
        drawing.add(q)
        drawing.drawOn(self, m, 0.28 * cm)
        self.setFont("Helvetica-Bold", 6.6)
        self.setFillColor(CINZA)
        self.drawString(m + size + 5, 0.76 * cm, "Hospital Unimed Grande Florianópolis")
        self.setFillColor(colors.HexColor("#6B7280"))
        self.setFont("Helvetica", 6.4)
        self.drawString(m + size + 5, 0.46 * cm, "+55 48 9181-5582 | Concierge Robótica")
        for fn in ("selo_ona.jpg", "selo_ona.png", "selo_ona.jpeg", "Selo ONA Nível 3 HUGF (2).jpg"):
            if os.path.exists(fn):
                try:
                    self.drawImage(fn, (w - 2.1 * cm) / 2, 0.2 * cm,
                                   width=2.1 * cm, height=0.85 * cm, mask="auto")
                except Exception:
                    pass
                break
        self.setFont("Helvetica", 6.4)
        self.drawRightString(w - m, 0.57 * cm,
                             f"Orçamento {self.orc_number} | Página {self._pageNumber} de {page_count}")
        self.restoreState()


def tag_modalidade(texto, particular=False):
    cor = colors.HexColor("#B91C1C") if particular else VERDE
    largura = max(108, len(texto) * 4.7 + 16)
    desenho = Drawing(largura, 16)
    desenho.add(Rect(0, 0, largura, 16, rx=6, ry=6, fillColor=cor, strokeColor=None))
    desenho.add(String(largura / 2, 5.0, texto, textAnchor="middle",
                       fontName="Helvetica-Bold", fontSize=7, fillColor=colors.white))
    return desenho


def gerar_pdf_bytes(dados):
    """Retorna bytes do PDF; PDF nunca presume modalidade híbrida."""
    tipo = normalizar_tipo_orcamento(dados.get("tipo_orcamento"))
    particular = tipo == "PARTICULAR"
    total = round(float(dados["valor_total"]), 2)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=1.1 * cm, rightMargin=1.1 * cm,
        topMargin=1.91 * cm, bottomMargin=1.27 * cm,
        title=f"Orçamento de Cirurgia Robótica {dados['numero_orcamento']}",
        author="Hospital Unimed Grande Florianópolis",
    )
    estilo_titulo = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=10.2,
                                   leading=11.4, textColor=CINZA, spaceAfter=1)
    estilo_sub = ParagraphStyle("sub", fontName="Helvetica-Bold", fontSize=6.7,
                                leading=8, textColor=VERDE, spaceAfter=3)
    sec = ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=7.6,
                         leading=9.2, textColor=VERDE, spaceBefore=4, spaceAfter=2)
    p = ParagraphStyle("p", fontName="Helvetica", fontSize=7.0,
                       leading=8.6, textColor=CINZA)
    pb = ParagraphStyle("pb", parent=p, fontName="Helvetica-Bold")
    ph = ParagraphStyle("ph", parent=p, fontName="Helvetica-Bold", fontSize=7.0)
    ptotal = ParagraphStyle("pt", parent=p, fontName="Helvetica-Bold", fontSize=7.9, textColor=VERDE)

    def par(texto, style=p):
        return Paragraph(texto, style)

    def tabela(dados_tabela, widths, header=False, destaque_final=False):
        estilo = [
            ("BOX", (0, 0), (-1, -1), 0.5, BORDA),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, BORDA),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3.5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3.5),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ]
        if header:
            estilo.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")))
        if destaque_final:
            estilo.append(("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#ECFDF5")))
        t = Table(dados_tabela, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
        t.setStyle(TableStyle(estilo))
        return t

    story = [
        par("Orçamento cirúrgico", estilo_titulo),
        par("ATENDIMENTO 100% PARTICULAR · PACOTE · CIRURGIA ROBÓTICA" if particular
            else "ATENDIMENTO HÍBRIDO · PACOTE · CIRURGIA ROBÓTICA", estilo_sub),
    ]
    modalidade = ("100% PARTICULAR (PACOTE)" if particular else "HÍBRIDO (PACOTE)")
    convenio = "Particular - sem convênio" if particular else "Unimed/Híbrido"
    info = [
        [par(f"<b>PACIENTE</b><br/>{seguro(dados['paciente'])}"),
         par(f"<b>MÉDICO RESPONSÁVEL</b><br/>{seguro(dados['medico'])}")],
        [par(f"<b>DATA DA SOLICITAÇÃO</b><br/>{seguro(dados['data_solicitacao'])}"),
         par(f"<b>VALIDADE DO ORÇAMENTO</b><br/>{seguro(dados['data_validade'])}")],
        [par(f"<b>TEMPO PREVISTO DE CIRURGIA</b><br/>{seguro(dados['tempo_cc'])}"),
         par(f"<b>ACOMODAÇÃO</b><br/>{seguro(dados['acomodacao'])}")],
        [[par("<b>TIPO DE ORÇAMENTO</b>"), Spacer(1, 2),
          tag_modalidade(modalidade, particular)],
         par(f"<b>CONVÊNIO / MODALIDADE</b><br/>{convenio}")],
    ]
    tinfo = tabela(info, [9.4 * cm, 9.4 * cm])
    tinfo.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), FUNDO)]))
    story.append(tinfo)

    story.append(par("PROCEDIMENTOS PREVISTOS", sec))
    linhas_proc = [[par("Código", ph), par("Procedimento", ph)]]
    for proc in dados.get("procedimentos", []):
        linhas_proc.append([par(seguro(proc.get("codigo", ""))), par(seguro(proc.get("descricao", "")))])
    linhas_proc.append([
        par("89999901"),
        par("Taxa de procedimento robótico particular" if particular
            else "Taxa de procedimento robótico híbrido"),
    ])
    story.append(tabela(linhas_proc, [3.0 * cm, 15.8 * cm], header=True))

    story.append(par("VALORES DE RESPONSABILIDADE DO PACIENTE", sec))
    if particular:
        intro = "Orçamento 100% particular. Os valores abaixo correspondem aos itens expressamente discriminados neste pacote, sem faturamento ao convênio."
    else:
        intro = "Orçamento híbrido. Os valores abaixo correspondem aos itens particulares informados; itens assistenciais cobertos pelo plano seguem autorização e faturamento próprios."
    story.append(par(intro, p))
    story.append(Spacer(1, 2))
    financeiro = [
        [par("Descrição", ph), par("Valor", ph)],
        [par("Taxa de Procedimento (Hospital)"), par(dinheiro(dados["valor_hospital"]))],
        [par("Honorários Anestésicos"), par(dinheiro(dados["valor_anestesia"]))],
        [par("Pinças e Materiais Robóticos"), par(dinheiro(dados["valor_materiais"]))],
    ]
    if dados.get("valor_outros", 0) > 0:
        financeiro.append([par("Outros itens hospitalares previstos"), par(dinheiro(dados["valor_outros"]))])
    if dados.get("valor_acomodacao", 0) > 0:
        financeiro.append([par("Diferença / adicional de acomodação"), par(dinheiro(dados["valor_acomodacao"]))])
    financeiro.extend([
        [par("Refeição Paciente e Acompanhante*"), par("Cortesia", ptotal)],
        [par("Subtotal", pb), par(dinheiro(dados["subtotal"]), pb)],
        [par("Desconto", p), par(dinheiro(dados["desconto"]))],
        [par("Total a pagar pelo paciente", ptotal), par(dinheiro(total), ptotal)],
    ])
    story.append(tabela(financeiro, [13.9 * cm, 4.9 * cm], header=True, destaque_final=True))

    materiais = dados.get("itens_materiais", [])
    if materiais:
        story.append(par("PINÇAS E MATERIAIS ROBÓTICOS — DETALHAMENTO", sec))
        analitico = [[par("Item / Descrição", ph), par("Qtd.", ph), par("Valor total", ph)]]
        for item in materiais:
            analitico.append([
                par(seguro(item["descricao"])), par(str(int(item["quantidade"]))),
                par(dinheiro(item["valor_total"])),
            ])
        analitico.append([
            par("Subtotal de materiais", pb), par(str(sum(int(x["quantidade"]) for x in materiais)), pb),
            par(dinheiro(dados["valor_materiais"]), pb),
        ])
        story.append(tabela(analitico, [12.0 * cm, 1.6 * cm, 5.2 * cm], header=True))
        story.append(par("Os valores acima já estão contemplados no total do orçamento.", p))

    story.append(par("01 - ITENS NÃO INCLUSOS NESTE ORÇAMENTO", sec))
    if particular:
        honorarios = (
            "<b>Honorários médicos da equipe cirúrgica (modalidade particular):</b> "
            "Os honorários da equipe cirúrgica não estão incluídos neste orçamento hospitalar "
            "e deverão ser negociados e pagos pelo paciente diretamente à equipe cirúrgica."
        )
    else:
        honorarios = (
            "<b>Honorários médicos da equipe cirúrgica (modalidade híbrida):</b> "
            "A cobertura da parcela convencional está condicionada às regras e autorizações "
            "do plano. Eventual diferença relativa ao ato robótico não está incluída neste "
            "orçamento hospitalar e deverá ser negociada diretamente com a equipe cirúrgica."
        )
    if dados["valor_anestesia"] > 0:
        anestesia = "<i>Os honorários anestésicos estão incluídos nos valores discriminados acima.</i>"
    else:
        anestesia = "<i>Honorários anestésicos não foram incluídos (valor zerado); confirmar composição do pacote.</i>"
    exclusoes = (
        honorarios + "<br/>" + anestesia +
        "<br/>&bull; Exames laboratoriais e de imagem/diagnóstico;"
        "<br/>&bull; Sessões de fonoaudiologia e fisioterapia;"
        "<br/>&bull; Sangue e hemoderivados."
    )
    extras = str(dados.get("observacoes_exclusoes", "")).strip()
    if extras:
        exclusoes += "<br/>&bull; " + seguro(extras)
    nao_inclusos = Table([[par(exclusoes)]], colWidths=[18.8 * cm])
    nao_inclusos.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF2F2")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#FCA5A5")),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(nao_inclusos)

    secao_pagamento = [par("02 - CONDIÇÕES DE PAGAMENTO", sec)]
    aviso = Table([[par(
        "<b>Atenção:</b> a confirmação do agendamento depende da validação do pagamento "
        "antecipado em até 48 horas úteis antes do procedimento, conforme regras institucionais."
    )]], colWidths=[18.8 * cm])
    aviso.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEFCE8")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#FDE047")),
        ("PADDING", (0, 0), (-1, -1), 3),
    ]))
    secao_pagamento.append(aviso)
    secao_pagamento.append(Spacer(1, 2))
    pagamentos = [
        [par("<b>PAGAMENTO À VISTA</b><br/>Dinheiro, cartão de débito ou PIX.<br/>"
             "<b>Chave PIX (CNPJ):</b> 77.858.611/0014-14<br/>"
             "<b>Favorecido:</b> Unimed Grande Florianópolis Cooperativa de Trabalho Médico"),
         par("<b>TRANSFERÊNCIA BANCÁRIA</b><br/>"
             "<b>Favorecido:</b> Unimed Grande Florianópolis Cooperativa de Trabalho Médico<br/>"
             "<b>CNPJ:</b> 77.858.611/0014-14<br/>"
             "<b>Banco Unicred (136)</b> | <b>Agência:</b> 1214-9 | <b>Conta:</b> 102646-1")],
        [par("<b>CARTÃO DE CRÉDITO</b><br/>"
             "&bull; Até R$ 10.000,00: em até 6x sem juros.<br/>"
             "&bull; Acima de R$ 10.000,00: em até 10x sem juros.<br/>"
             "Solicitar link informando número do orçamento, bandeira e parcelas."),
         par("<b>ENVIO DE COMPROVANTE / LINK</b><br/>"
             "<b>WhatsApp:</b> +55 48 9181-5582<br/>"
             "<b>E-mail:</b> concierge@unimedflorianopolis.com.br")],
    ]
    tpag = tabela(pagamentos, [9.4 * cm, 9.4 * cm])
    tpag.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), FUNDO)]))
    secao_pagamento.append(tpag)
    secao_pagamento.append(Spacer(1, 2))
    secao_pagamento.append(par("*Cortesia condicionada às condições do pacote; conferir elegibilidade. "
                                "Valores e itens adicionais não discriminados exigem avaliação e orçamento complementares.", p))
    if dados.get("observacoes_gerais"):
        secao_pagamento.append(par("03 - OBSERVAÇÕES COMPLEMENTARES", sec))
        secao_pagamento.append(par(seguro(dados["observacoes_gerais"]), p))
    story.extend(secao_pagamento)

    # Página única: nunca divide os quadros ou descarta conteúdo.
    # Reduz proporcionalmente o conjunto apenas se necessário.
    # Em orçamentos excessivamente longos, interrompe e solicita revisão
    # em vez de gerar um PDF ilegível ou de várias páginas.
    quadro = KeepInFrame(
        maxWidth=18.8 * cm,
        maxHeight=doc.height - 13,
        content=story,
        mode="shrink",
        hAlign="LEFT",
        vAlign="TOP",
    )
    paginas_geradas = []

    def canvas_unico(*args, **kwargs):
        obj = NumberedCanvas(*args, orc_number=dados["numero_orcamento"], **kwargs)
        salvar_original = obj.save

        def salvar_com_contagem():
            paginas_geradas.append(len(obj._saved_page_states))
            salvar_original()

        obj.save = salvar_com_contagem
        return obj

    doc.build([quadro], canvasmaker=canvas_unico)
    fator = getattr(quadro, "_scale", 1.0)
    if paginas_geradas != [1]:
        raise ValueError("O PDF precisa ter exatamente uma página. Revise o conteúdo informado.")
    if fator > 1.15:
        raise ValueError(
            "Há informações demais para uma página A4 com leitura adequada. "
            "Reduza a quantidade de procedimentos/materiais ou sintetize "
            "as observações adicionais, sem omitir condições obrigatórias."
        )
    return buffer.getvalue()


# -----------------------------------------------------------------------------
# IMPORTAÇÃO OPCIONAL COM GEMINI (somente com autorização LGPD do usuário)
# -----------------------------------------------------------------------------
def importar_documento_gemini(arquivo):
    api_key = segredos("GEMINI_API_KEY") or segredos("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Configure GEMINI_API_KEY nos Secrets do Streamlit.")
    if arquivo.size > 12 * 1024 * 1024:
        raise ValueError("Arquivo maior que 12 MB.")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("Instale a dependência google-genai.") from exc
    tipo_mime = arquivo.type or ("application/pdf" if arquivo.name.lower().endswith(".pdf") else "image/png")
    prompt = (
        "Extraia APENAS o conteúdo explicitamente presente neste documento de solicitação "
        "de orçamento de cirurgia robótica. Retorne JSON (sem texto adicional) com chaves: "
        "paciente, medico, acomodacao, tempo_minutos, procedimentos (lista de objetos "
        "{codigo, descricao}), valor_hospital, valor_anestesia, valor_materiais, "
        "valor_outros, valor_acomodacao, itens_materiais (lista de objetos "
        "{descricao, quantidade, valor_total}). "
        "Se o campo não existir, use string vazia, zero ou lista vazia. "
        "NÃO classifique nem invente a modalidade, não estime preços e não crie dados."
    )
    client = genai.Client(api_key=api_key)
    resultado = client.models.generate_content(
        model=segredos("GEMINI_MODEL", "gemini-2.5-flash"),
        contents=[types.Part.from_bytes(data=arquivo.getvalue(), mime_type=tipo_mime), prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    )
    parsed = json.loads(resultado.text or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("IA não retornou um objeto JSON válido.")
    return parsed


def carregar_campos_importados(extraidos):
    chaves = {
        "paciente": "paciente_ui", "medico": "medico_ui",
        "acomodacao": "acomodacao_ui", "tempo_minutos": "tempo_ui",
        "valor_hospital": "hospital_ui", "valor_anestesia": "anestesia_ui",
        "valor_materiais": "materiais_ui", "valor_outros": "outros_ui",
        "valor_acomodacao": "acomodacao_valor_ui",
    }
    for origem, destino in chaves.items():
        dado = extraidos.get(origem)
        if dado is None or dado == "":
            continue
        if origem.startswith("valor_"):
            st.session_state[destino] = valor_float(dado)
        elif origem == "tempo_minutos":
            try:
                st.session_state[destino] = max(1, int(dado))
            except (ValueError, TypeError):
                pass
        else:
            st.session_state[destino] = str(dado)
    procedimentos = extraidos.get("procedimentos", [])
    if isinstance(procedimentos, list):
        st.session_state["procedimentos_ui"] = "\n".join(
            f"{str(x.get('codigo', '')).strip()} | {str(x.get('descricao', '')).strip()}"
            for x in procedimentos if isinstance(x, dict)
        )
    itens = extraidos.get("itens_materiais", [])
    if isinstance(itens, list) and itens:
        st.session_state["itens_editor_ui"] = pd.DataFrame([
            {"Descrição": str(x.get("descricao", "")),
             "Quantidade": max(1, int(x.get("quantidade", 1))),
             "Valor total (R$)": valor_float(x.get("valor_total", 0))}
            for x in itens if isinstance(x, dict)
        ])
    st.session_state.pop("itens_tabela_widget", None)
    # CRÍTICO: nunca altera "modalidade_ui"; seleção sempre explícita.


def lista_procedimentos(texto):
    saida = []
    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha:
            continue
        if "|" in linha:
            codigo, descricao = linha.split("|", 1)
        else:
            codigo, descricao = "", linha
        saida.append({"codigo": codigo.strip(), "descricao": descricao.strip()})
    return saida


def lista_materiais(df):
    saida = []
    for _, linha in df.iterrows():
        descricao = str(linha.get("Descrição", "")).strip()
        if descricao.lower() == "nan" or not descricao:
            continue
        try:
            quantidade = int(linha.get("Quantidade", 1))
            valor_total = valor_float(linha.get("Valor total (R$)", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Quantidade/valor inválido para o item {descricao}.") from exc
        if quantidade <= 0:
            raise ValueError(f"A quantidade de {descricao} deve ser positiva.")
        saida.append({"descricao": descricao, "quantidade": quantidade,
                      "valor_total": round(valor_total, 2)})
    return saida


def novo_numero():
    return f"ROB-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"


def formulario():
    st.subheader("Novo orçamento de cirurgia robótica")
    st.info("Selecione a modalidade. O sistema não presume Híbrido e não altera a modalidade com base no documento ou na IA.")

    with st.expander("Importar solicitação com IA (opcional)"):
        st.warning("LGPD: o arquivo pode conter dados de saúde. Utilize integração externa somente após autorização institucional e avaliação da base legal.")
        up = st.file_uploader("PDF ou imagem da solicitação", type=["pdf", "png", "jpg", "jpeg"], key="up_ia")
        consentiu = st.checkbox("Confirmo que tenho autorização institucional para tratar este documento por este serviço de IA", key="consentimento_ia")
        if st.button("Extrair dados com Gemini", disabled=(not up or not consentiu)):
            try:
                with st.spinner("Extraindo os campos identificáveis..."):
                    carregar_campos_importados(importar_documento_gemini(up))
                st.success("Campos importados. Revise valores, procedimentos e modalidade antes de gerar.")
                st.rerun()
            except Exception as exc:
                st.error(f"Falha ao importar: {exc}")

    col_m, col_num = st.columns([2, 1])
    with col_m:
        modo = st.selectbox("Modalidade obrigatória *", ["Selecione", "HÍBRIDO", "100% PARTICULAR"], key="modalidade_ui")
    with col_num:
        if "numero_ui" not in st.session_state:
            st.session_state["numero_ui"] = novo_numero()
        st.text_input("Número do orçamento *", key="numero_ui")
    c1, c2 = st.columns(2)
    with c1:
        st.text_input("Paciente *", key="paciente_ui")
        st.date_input("Data da solicitação", value=date.today(), format="DD/MM/YYYY", key="data_solicitacao_ui")
        st.number_input("Tempo previsto de centro cirúrgico (minutos)", min_value=1, value=240, step=15, key="tempo_ui")
    with c2:
        st.text_input("Médico responsável *", key="medico_ui")
        st.date_input("Validade do orçamento", value=date.today() + timedelta(days=30), format="DD/MM/YYYY", key="data_validade_ui")
        st.text_input("Acomodação", value="Apartamento", key="acomodacao_ui")
    st.text_area("Procedimentos (um por linha: CÓDIGO | DESCRIÇÃO)", height=92, key="procedimentos_ui")

    st.markdown("#### Composição financeira")
    a, b, c = st.columns(3)
    with a:
        hospital = st.number_input("Taxa de procedimento (hospital) R$", min_value=0.0, value=0.0, step=100.0, format="%.2f", key="hospital_ui")
        outros = st.number_input("Outros itens hospitalares do pacote R$", min_value=0.0, value=0.0, step=100.0, format="%.2f", key="outros_ui")
    with b:
        anest = st.number_input("Honorários anestésicos R$", min_value=0.0, value=0.0, step=100.0, format="%.2f", key="anestesia_ui")
        adicional = st.number_input("Adicional/diferença de acomodação R$", min_value=0.0, value=0.0, step=100.0, format="%.2f", key="acomodacao_valor_ui")
    with c:
        mat_manual = st.number_input("Pinças e materiais robóticos R$", min_value=0.0, value=0.0, step=100.0, format="%.2f", key="materiais_ui")
        desconto = st.number_input("Desconto autorizado R$", min_value=0.0, value=0.0, step=50.0, format="%.2f", key="desconto_ui")

    with st.expander("Discriminar pinças e materiais (opcional)"):
        st.caption("Se preencher o detalhamento, a soma de seus valores deverá corresponder ao total informado para materiais robóticos.")
        if "itens_editor_ui" not in st.session_state:
            st.session_state["itens_editor_ui"] = pd.DataFrame(
                [{"Descrição": "", "Quantidade": 1, "Valor total (R$)": 0.0}]
            )
        df_editor = st.data_editor(
            st.session_state["itens_editor_ui"], num_rows="dynamic", hide_index=True,
            use_container_width=True, key="itens_tabela_widget",
            column_config={
                "Descrição": st.column_config.TextColumn("Descrição"),
                "Quantidade": st.column_config.NumberColumn("Quantidade", min_value=1, step=1),
                "Valor total (R$)": st.column_config.NumberColumn("Valor total (R$)", min_value=0.0, format="R$ %.2f"),
            },
        )
    st.text_area("Itens não inclusos adicionais (opcional)", height=65, key="exclusoes_ui")
    st.text_area("Observações gerais (opcional)", height=65, key="observacoes_ui")

    subtotal = round(hospital + anest + mat_manual + outros + adicional, 2)
    total = round(subtotal - desconto, 2)
    x1, x2, x3 = st.columns(3)
    x1.metric("Subtotal", dinheiro(subtotal))
    x2.metric("Desconto", dinheiro(desconto))
    x3.metric("Total do orçamento", dinheiro(total))
    if modo == "100% PARTICULAR":
        st.warning("100% PARTICULAR: informe o valor completo dos itens hospitalares aplicáveis. O sistema não calcula nem presume automaticamente a composição integral do pacote.")
    elif modo == "HÍBRIDO":
        st.caption("HÍBRIDO: informe apenas os valores de responsabilidade particular do paciente, conforme negociação e autorizações aplicáveis.")
    confirmar = st.checkbox("Confirmo a modalidade e que os itens, valores, condições e exclusões foram conferidos com a área responsável.")
    if st.button("Gerar PDF e salvar orçamento", type="primary", use_container_width=True):
        try:
            tipo = normalizar_tipo_orcamento(modo)
            numero = st.session_state["numero_ui"].strip()
            paciente = st.session_state["paciente_ui"].strip()
            medico = st.session_state["medico_ui"].strip()
            if not numero or not paciente or not medico:
                raise ValueError("Informe o número, o paciente e o médico responsável.")
            if not confirmar:
                raise ValueError("Confirme a validação dos dados antes de gerar.")
            if st.session_state["data_validade_ui"] < st.session_state["data_solicitacao_ui"]:
                raise ValueError("A validade não pode ser anterior à solicitação.")
            if total <= 0 or desconto > subtotal:
                raise ValueError("Total inválido. Confira os valores e o desconto.")
            itens = lista_materiais(df_editor)
            if itens and abs(sum(x["valor_total"] for x in itens) - mat_manual) > 0.01:
                raise ValueError("A soma dos itens detalhados não corresponde ao valor total de materiais robóticos.")
            duracao = int(st.session_state["tempo_ui"])
            dados = {
                "numero_orcamento": numero,
                "paciente": paciente,
                "medico": medico,
                "tipo_orcamento": tipo,
                "data_solicitacao": st.session_state["data_solicitacao_ui"].strftime("%d/%m/%Y"),
                "data_validade": st.session_state["data_validade_ui"].strftime("%d/%m/%Y"),
                "tempo_cc": f"{duracao} Min CC ({duracao // 60}h {duracao % 60:02d}min)",
                "acomodacao": st.session_state["acomodacao_ui"],
                "procedimentos": lista_procedimentos(st.session_state["procedimentos_ui"]),
                "valor_hospital": round(hospital, 2),
                "valor_anestesia": round(anest, 2),
                "valor_materiais": round(mat_manual, 2),
                "valor_outros": round(outros, 2),
                "valor_acomodacao": round(adicional, 2),
                "subtotal": subtotal,
                "desconto": round(desconto, 2),
                "valor_total": total,
                "itens_materiais": itens,
                "observacoes_exclusoes": st.session_state["exclusoes_ui"],
                "observacoes_gerais": st.session_state["observacoes_ui"],
            }
            arquivo_pdf = gerar_pdf_bytes(dados)
            salvar_orcamento_db(dados)
            st.session_state["ultimo_pdf"] = arquivo_pdf
            st.session_state["ultimo_numero"] = numero
            st.session_state["ultima_modalidade"] = tipo
            st.success(f"Orçamento {numero} salvo como {tipo}. PDF gerado.")
        except Exception as exc:
            st.error(f"Não foi possível gerar o orçamento: {exc}")
    if "ultimo_pdf" in st.session_state:
        st.download_button(
            "Baixar último orçamento em PDF",
            data=st.session_state["ultimo_pdf"],
            file_name=f"Orcamento_Robotica_{st.session_state['ultimo_numero']}.pdf",
            mime="application/pdf", use_container_width=True,
        )
        st.caption(f"Arquivo disponível: {st.session_state.get('ultima_modalidade', '')}. Corresponde à última geração; se alterar os campos, gere novamente.")


def painel():
    st.subheader("Painel de acompanhamento comercial")
    df = carregar_orcamentos_df()
    if df.empty:
        st.info("Nenhum orçamento salvo no banco.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Orçamentos cadastrados", len(df))
    c2.metric("Pendentes", int((df["status"] == "Pendente").sum()))
    c3.metric("Valor total cadastrado", dinheiro(df["valor_total"].sum()))
    st.dataframe(df[["numero_orcamento", "paciente", "medico", "tipo_orcamento",
                     "data_solicitacao", "valor_total", "status", "created_at"]],
                 use_container_width=True, hide_index=True)
    csv = df.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig")
    st.download_button("Exportar acompanhamento (CSV)", data=csv,
                       file_name="acompanhamento_robotica_hugf.csv", mime="text/csv")
    c1, c2 = st.columns(2)
    with c1:
        numero = st.selectbox("Orçamento para atualizar", df["numero_orcamento"].tolist())
    atual = str(df.loc[df["numero_orcamento"] == numero, "status"].iloc[0])
    with c2:
        novo_status = st.selectbox("Novo status", STATUS,
                                  index=STATUS.index(atual) if atual in STATUS else 0)
    if st.button("Atualizar status"):
        atualizar_status_db(numero, novo_status)
        st.success("Status atualizado.")
        st.rerun()
    st.markdown("#### Alertas de orçamentos pendentes há mais de 48 horas")
    encontrados = pendentes_48h()
    st.write(f"Orçamentos elegíveis para alerta: **{len(encontrados)}**")
    if encontrados:
        st.dataframe(pd.DataFrame(encontrados)[["numero_orcamento", "paciente",
                                                  "medico", "tipo_orcamento", "created_at"]],
                     hide_index=True, use_container_width=True)
    st.caption("O e-mail é enviado somente quando o botão abaixo é acionado. A aplicação não faz agendamento automático em segundo plano.")
    if st.button("Enviar alertas por e-mail", disabled=not encontrados):
        ok, mensagem = enviar_email_alerta(encontrados)
        (st.success if ok else st.error)(mensagem)


def main():
    init_db()
    st.title("Hospital Unimed Grande Florianópolis")
    st.caption("Gerador de orçamentos — Programa de Cirurgia Robótica")
    aba_orcamento, aba_painel = st.tabs(["Gerar orçamento", "Acompanhamento comercial"])
    with aba_orcamento:
        formulario()
    with aba_painel:
        painel()
    st.divider()
    st.caption("Versão com modalidade obrigatória. Valores, exclusões, condições de pagamento e integrações devem ser homologados antes da publicação. O SQLite local pode não persistir em hospedagens efêmeras.")


if __name__ == "__main__":
    main()
