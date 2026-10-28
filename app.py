import io
import json
import os
import re
import sqlite3
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from html import escape

import pandas as pd
import pypdf
import streamlit as st
from google import genai
from google.genai import types
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

st.set_page_config(page_title='Gerador de Orçamentos Robótica - HUGF', page_icon='🏥', layout='wide')
DB_FILE = 'orcamentos.db'
MODALIDADES = ['HÍBRIDO', 'PARTICULAR', '100% CONVÊNIO']
GREEN = colors.HexColor('#008C50')
DARK = colors.HexColor('#1F2937')
GRAY = colors.HexColor('#6B7280')
BORDER = colors.HexColor('#E5E7EB')
LIGHT = colors.HexColor('#F9FAFB')


def fmt_m(valor):
    return f'R$ {float(valor):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def parse_moeda(valor):
    """Converte valores monetários brasileiros sem interpretar ponto de milhar como decimal."""
    if isinstance(valor, (int, float)):
        return float(valor)
    txt = re.sub(r'[^\d,.-]', '', str(valor or ''))
    if not txt:
        return 0.0
    if ',' in txt:
        txt = txt.replace('.', '').replace(',', '.')
    elif txt.count('.') > 1:
        txt = txt.replace('.', '')
    elif '.' in txt and len(txt.rsplit('.', 1)[1]) == 3:
        txt = txt.replace('.', '')
    try:
        return float(txt)
    except (TypeError, ValueError):
        return 0.0


def normalizar_modalidade(valor):
    texto = str(valor or '').upper().strip()
    if 'CONVÊNIO' in texto or 'CONVENIO' in texto or 'PLANO' in texto:
        if '100%' in texto or 'INTEGRAL' in texto or 'TOTAL' in texto:
            return '100% CONVÊNIO'
    if 'PARTICULAR' in texto:
        return 'PARTICULAR'
    return 'HÍBRIDO'


def valor_paciente(dados):
    if dados['tipo_orcamento'] == '100% CONVÊNIO':
        return 0.0
    return sum(parse_moeda(dados.get(k, 0)) for k in ('valor_hospital', 'valor_anestesia', 'valor_materiais'))


def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS orcamentos (
            numero_orcamento TEXT PRIMARY KEY, paciente TEXT, medico TEXT,
            tipo_orcamento TEXT, data_solicitacao TEXT, data_validade TEXT,
            tempo_cc TEXT, acomodacao TEXT, valor_total REAL, created_at TIMESTAMP,
            status TEXT DEFAULT 'Pendente', alerta_enviado INTEGER DEFAULT 0)''')


def salvar_orcamento_db(dados, valor_total):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''INSERT INTO orcamentos
            (numero_orcamento, paciente, medico, tipo_orcamento, data_solicitacao,
             data_validade, tempo_cc, acomodacao, valor_total, created_at, status, alerta_enviado)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            COALESCE((SELECT status FROM orcamentos WHERE numero_orcamento=?), 'Pendente'),
            COALESCE((SELECT alerta_enviado FROM orcamentos WHERE numero_orcamento=?), 0))
            ON CONFLICT(numero_orcamento) DO UPDATE SET
            paciente=excluded.paciente, medico=excluded.medico,
            tipo_orcamento=excluded.tipo_orcamento,
            data_solicitacao=excluded.data_solicitacao,
            data_validade=excluded.data_validade, tempo_cc=excluded.tempo_cc,
            acomodacao=excluded.acomodacao, valor_total=excluded.valor_total''',
            (dados['numero_orcamento'], dados['paciente'], dados['medico'],
             dados['tipo_orcamento'], dados['data_solicitacao'], dados['data_validade'],
             dados['tempo_cc'], dados['acomodacao'], valor_total,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             dados['numero_orcamento'], dados['numero_orcamento']))


def carregar_orcamentos_df():
    with sqlite3.connect(DB_FILE) as conn:
        return pd.read_sql_query('SELECT * FROM orcamentos ORDER BY created_at DESC', conn)


def atualizar_status_db(numero_orcamento, novo_status):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('UPDATE orcamentos SET status=? WHERE numero_orcamento=?', (novo_status, numero_orcamento))


def marcar_alerta_enviado(numero_orcamento):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('UPDATE orcamentos SET alerta_enviado=1 WHERE numero_orcamento=?', (numero_orcamento,))


def enviar_email_alerta(orcamentos_pendentes):
    try:
        server_name = st.secrets.get('SMTP_SERVER', 'smtp.gmail.com')
        port = int(st.secrets.get('SMTP_PORT', 587))
        user = str(st.secrets.get('SMTP_USER', '')).strip().strip('"\'')
        password = str(st.secrets.get('SMTP_PASSWORD', '')).strip().strip('"\'')
        if not user or not password:
            return False, 'Credenciais SMTP_USER / SMTP_PASSWORD não configuradas.'
        destinatarios = ['comercialhospital@unimedflorianopolis.com.br',
                         'cliente.particular@unimedflorianopolis.com.br']
        mensagem = MIMEMultipart('alternative')
        mensagem['Subject'] = f'ALERTA 48H: Orçamentos de Cirurgia Robótica Pendentes ({len(orcamentos_pendentes)})'
        mensagem['From'] = user
        mensagem['To'] = ', '.join(destinatarios)
        linhas = ''.join(
            '<tr>' + ''.join(f'<td>{escape(str(x))}</td>' for x in (
                r['numero_orcamento'], r['paciente'], r['medico'], r['tipo_orcamento'],
                r['data_solicitacao'], fmt_m(r['valor_total']))) + '</tr>'
            for r in orcamentos_pendentes)
        html = f'''<html><body style="font-family:Arial;color:#1F2937">
        <h2 style="color:#008C50">Hospital Unimed Grande Florianópolis</h2>
        <h3>Programa de Cirurgia Robótica - Acompanhamento Comercial</h3>
        <p>Os seguintes orçamentos foram emitidos há mais de 48 horas e continuam pendentes:</p>
        <table border="1" cellpadding="8" cellspacing="0"><tr>
        <th>Orçamento Nº</th><th>Paciente</th><th>Médico</th><th>Modalidade</th>
        <th>Data Emissão</th><th>Valor Total</th></tr>{linhas}</table>
        <p>Verifique a negociação e atualize o status no painel.</p></body></html>'''
        mensagem.attach(MIMEText(html, 'html', 'utf-8'))
        with smtplib.SMTP(server_name, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.sendmail(user, destinatarios, mensagem.as_string())
        for item in orcamentos_pendentes:
            marcar_alerta_enviado(item['numero_orcamento'])
        return True, 'Alertas enviados com sucesso!'
    except Exception as erro:
        return False, f'Erro ao enviar e-mail: {erro}'


class NumberedCanvas(canvas.Canvas):
    orc_number = ''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        paginas = len(self._saved_page_states)
        for estado in self._saved_page_states:
            self.__dict__.update(estado)
            self.draw_page_decorations(paginas)
            super().showPage()
        super().save()

    def draw_page_decorations(self, paginas):
        self.saveState()
        largura, altura = A4
        margem = 1.1 * cm
        caminho_logo = 'unimed_logo_official.png'
        if os.path.exists(caminho_logo):
            largura_logo = 3.6 * cm
            self.drawImage(caminho_logo, margem, altura - 1.55 * cm,
                           width=largura_logo, height=largura_logo / (1560 / 646), mask='auto')
            self.setFont('Helvetica-Bold', 9)
            self.setFillColor(DARK)
            self.drawString(margem + largura_logo + .25 * cm, altura - 1 * cm,
                            'HOSPITAL UNIMED GRANDE FLORIANÓPOLIS')
            self.setFont('Helvetica-Bold', 7.5)
            self.setFillColor(GREEN)
            self.drawString(margem + largura_logo + .25 * cm, altura - 1.3 * cm,
                            'PROGRAMA DE CIRURGIA ROBÓTICA')
        self.setFillColor(GRAY)
        self.setFont('Helvetica-Bold', 7)
        self.drawRightString(largura - margem, altura - .8 * cm, 'ORÇAMENTO Nº')
        self.setFillColor(GREEN)
        self.setFont('Helvetica-Bold', 14)
        self.drawRightString(largura - margem, altura - 1.35 * cm, str(self.orc_number))
        self.setStrokeColor(BORDER)
        self.line(margem, altura - 1.6 * cm, largura - margem, altura - 1.6 * cm)
        self.line(margem, 1.05 * cm, largura - margem, 1.05 * cm)
        codigo = qr.QrCodeWidget('https://wa.me/554891815582')
        b = codigo.getBounds()
        desenho = Drawing(18, 18, transform=[18/(b[2]-b[0]), 0, 0, 18/(b[3]-b[1]), 0, 0])
        desenho.add(codigo)
        desenho.drawOn(self, margem, .2 * cm)
        self.setFillColor(DARK)
        self.setFont('Helvetica-Bold', 6.5)
        self.drawString(margem + 22, .7 * cm, 'Hospital Unimed Grande Florianópolis')
        self.setFillColor(GRAY)
        self.setFont('Helvetica', 6.5)
        self.drawString(margem + 22, .4 * cm, '+55 48 9181-5582 Concierge Robótica HUGF')
        for nome in ['selo_ona.jpg', 'selo_ona.png', 'selo_ona.jpeg', 'Selo ONA Nível 3 HUGF (2).jpg']:
            if os.path.exists(nome):
                self.drawImage(nome, (largura - 2.2 * cm) / 2, .02 * cm,
                               width=2.2 * cm, height=1.1 * cm, mask='auto')
                break
        self.drawRightString(largura - margem, .55 * cm,
                             f'Orçamento {self.orc_number}  |  Página {self._pageNumber} de {paginas}')
        self.restoreState()


def create_tag(texto, particular=False):
    cor = colors.HexColor('#DC2626') if particular else GREEN
    largura = len(texto) * 5.5 + 12
    desenho = Drawing(largura, 13)
    desenho.add(Rect(0, 0, largura, 13, rx=6, ry=6, fillColor=cor, strokeColor=None))
    desenho.add(String(largura/2, 3.2, texto, textAnchor='middle',
                       fontName='Helvetica-Bold', fontSize=7, fillColor=colors.white))
    return desenho


def create_qr_code(url='https://wa.me/554891815582', size=40):
    codigo = qr.QrCodeWidget(url)
    b = codigo.getBounds()
    desenho = Drawing(size, size, transform=[size/(b[2]-b[0]), 0, 0, size/(b[3]-b[1]), 0, 0])
    desenho.add(codigo)
    return desenho


def gerar_pdf_bytes(dados):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=1.1*cm, rightMargin=1.1*cm,
                            topMargin=1.7*cm, bottomMargin=1.1*cm)
    estilo = lambda nome, tamanho=6.1, leading=7.8, negrito=False, cor=DARK, **kw: ParagraphStyle(
        nome, fontName='Helvetica-Bold' if negrito else 'Helvetica',
        fontSize=tamanho, leading=leading, textColor=cor, **kw)
    titulo = estilo('Titulo', 10.5, 12.5, True)
    subtitulo = estilo('Subtitulo', 6.8, 8.2, True, GREEN)
    secao = estilo('Secao', 7.2, 8.8, True, GREEN, spaceBefore=4, spaceAfter=2)
    texto = estilo('Texto')
    cab = estilo('Cabecalho', 6.1, 7.5, True)
    cel = estilo('Celula', 6.1, 7.5)
    cel_b = estilo('CelulaB', 6.1, 7.5, True)
    cel_v = estilo('CelulaV', 6.1, 7.5, True, GREEN)
    pequeno = estilo('Pequeno', 5.8, 6.8, True, GREEN, alignment=1)
    p = lambda t, e=texto: Paragraph(str(t), e)
    seguro = lambda chave: escape(str(dados.get(chave, '')))
    modalidade = dados['tipo_orcamento']
    particular = modalidade == 'PARTICULAR'
    convenio = modalidade == '100% CONVÊNIO'
    rotulo = {'PARTICULAR': 'PARTICULAR (PACOTE)', 'HÍBRIDO': 'HÍBRIDO (PACOTE)',
              '100% CONVÊNIO': '100% CONVÊNIO'}[modalidade]
    subt = ('ATENDIMENTO POR CONVÊNIO · CIRURGIA ROBÓTICA' if convenio else
            'ATENDIMENTO PARTICULAR · PACOTE · CIRURGIA ROBÓTICA')
    story = [p('Orçamento cirúrgico', titulo), p(subt, subtitulo)]

    infos = [
        [p(f'<b>PACIENTE</b><br/>{seguro("paciente")}'), p(f'<b>MÉDICO RESPONSÁVEL</b><br/>{seguro("medico")}')],
        [p(f'<b>DATA DA SOLICITAÇÃO</b><br/>{seguro("data_solicitacao")}'), p(f'<b>VALIDADE DO ORÇAMENTO</b><br/>{seguro("data_validade")} · 30 dias')],
        [p(f'<b>TEMPO PREVISTO DE CIRURGIA</b><br/>{seguro("tempo_cc")}'), p(f'<b>ACOMODAÇÃO</b><br/>{seguro("acomodacao")}')],
        [[p('<b>TIPO DE ORÇAMENTO</b>'), Spacer(1, 1), create_tag(rotulo, particular)],
         p(f'<b>CONVÊNIO / MODALIDADE</b><br/>{"Particular" if particular else "Convênio integral" if convenio else "Unimed/Híbrido"}')],
    ]
    tabela = Table(infos, colWidths=[9.4*cm, 9.4*cm])
    tabela.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),LIGHT),('BOX',(0,0),(-1,-1),.5,BORDER),
                               ('INNERGRID',(0,0),(-1,-1),.4,BORDER),('PADDING',(0,0),(-1,-1),3.5),
                               ('VALIGN',(0,0),(-1,-1),'MIDDLE')]))
    story.extend([tabela, Spacer(1, 7), p('PROCEDIMENTOS PREVISTOS', secao)])
    descricao = ('Taxa de procedimento robótico particular' if particular else
                 'Procedimento robótico pelo convênio' if convenio else
                 'Taxa de procedimento robótico híbrido')
    proc = Table([[p('Código', cab), p('Procedimento', cab)],
                  [p('89999901', cel), p(descricao, cel)]], colWidths=[2.8*cm, 16*cm])
    proc.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#F3F4F6')),
                              ('BOX',(0,0),(-1,-1),.5,BORDER),('INNERGRID',(0,0),(-1,-1),.4,BORDER),
                              ('PADDING',(0,0),(-1,-1),3)]))
    story.extend([proc, Spacer(1, 7)])

    if convenio:
        story.append(p('VALORES DE RESPONSABILIDADE DO PACIENTE', secao))
        aviso = Table([[p('<b>Modalidade 100% convênio.</b> Não há valor particular a pagar neste orçamento. '
                           'A cobertura e as autorizações devem ser confirmadas junto à operadora.')]],
                       colWidths=[18.8*cm])
        aviso.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#ECFDF5')),
                                   ('BOX',(0,0),(-1,-1),.5,GREEN),('PADDING',(0,0),(-1,-1),4)]))
        story.extend([aviso, Spacer(1, 8)])
    else:
        v_hosp = parse_moeda(dados['valor_hospital'])
        v_anes = parse_moeda(dados['valor_anestesia'])
        v_mat = parse_moeda(dados['valor_materiais'])
        total = v_hosp + v_anes + v_mat
        story.append(p('VALORES DE RESPONSABILIDADE DO PACIENTE', secao))
        linhas = [[p('Descrição', cab), p('Valor', cab)],
                  [p('Taxa de Procedimento (Hospital)', cel), p(fmt_m(v_hosp), cel)],
                  [p('Honorários Anestésicos', cel), p(fmt_m(v_anes), cel)],
                  [p('Pinças e Materiais Robóticos', cel), p(fmt_m(v_mat), cel)],
                  [p('Refeição Paciente e Acompanhante*', cel), p('Cortesia', cel_v)],
                  [p('Subtotal', cel_b), p(fmt_m(total), cel_b)],
                  [p('Desconto', cel), p('R$ 0,00', cel)],
                  [p('Total a pagar pelo paciente', cel_b), p(fmt_m(total), cel_v)]]
        valores = Table(linhas, colWidths=[14*cm, 4.8*cm])
        valores.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#F3F4F6')),
                                     ('BACKGROUND',(0,-1),(-1,-1),colors.HexColor('#ECFDF5')),
                                     ('BOX',(0,0),(-1,-1),.5,BORDER),('INNERGRID',(0,0),(-1,-1),.4,BORDER),
                                     ('PADDING',(0,0),(-1,-1),3)]))
        story.extend([valores, Spacer(1, 8)])

    story.append(p('01 - ITENS NÃO INCLUSOS NESTE ORÇAMENTO', secao))
    if particular:
        honor = ('<b>Honorários Médicos da Equipe Cirúrgica (Modalidade Particular):</b> Os honorários '
                 'da equipe cirúrgica não possuem cobertura deste orçamento hospitalar e deverão ser '
                 'acertados e pagos pelo paciente diretamente à equipe cirúrgica. '
                 '<i>(Nota: Os honorários anestésicos já estão devidamente inclusos neste orçamento).</i>')
    elif convenio:
        honor = ('<b>Honorários e cobertura assistencial:</b> Os itens e honorários sujeitos à cobertura '
                 'do convênio dependem das regras contratuais e das autorizações da operadora. '
                 'Este documento não constitui autorização de cobertura.')
    else:
        honor = ('<b>Honorários Médicos da Equipe Cirúrgica (Modalidade Híbrida):</b> O plano de saúde '
                 'cobrirá a parcela do honorário médico correspondente ao procedimento na modalidade '
                 'convencional. A diferença relativa ao ato cirúrgico robótico não possui cobertura '
                 'do plano e deverá ser negociada e paga pelo paciente diretamente à equipe cirúrgica. '
                 '<i>(Nota: Os honorários anestésicos já estão devidamente inclusos neste orçamento).</i>')
    nao_inclusos = honor + ('<br/>• Exames laboratoriais e de imagem/diagnóstico;'
                             '<br/>• Sessões de fonoaudiologia e fisioterapia;'
                             '<br/>• Sangue e hemoderivados.') if not convenio else honor
    quadro = Table([[p(nao_inclusos)]], colWidths=[18.8*cm])
    quadro.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#FEF2F2')),
                                ('BOX',(0,0),(-1,-1),.5,colors.HexColor('#FCA5A5')),
                                ('PADDING',(0,0),(-1,-1),3.5)]))
    story.extend([quadro, Spacer(1, 7)])

    story.append(p('02 - CONDIÇÕES DE PAGAMENTO' if not convenio else '02 - CONDIÇÕES DO ATENDIMENTO', secao))
    aviso_texto = ('<b>Atenção:</b> A confirmação do agendamento somente se dará mediante o pagamento '
                   'antecipado em até 48h úteis à realização do procedimento.' if not convenio else
                   '<b>Atenção:</b> O agendamento está sujeito à confirmação das autorizações e da '
                   'elegibilidade junto ao convênio, conforme o fluxo do hospital.')
    aviso = Table([[p(aviso_texto)]], colWidths=[18.8*cm])
    aviso.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#FEFCE8')),
                               ('BOX',(0,0),(-1,-1),.5,colors.HexColor('#FDE047')),
                               ('PADDING',(0,0),(-1,-1),3.2)]))
    story.extend([aviso, Spacer(1, 7)])
    if not convenio:
        pagamento = [
            [p('<b>PAGAMENTO À VISTA</b><br/>Dinheiro em espécie, cartão de débito ou PIX.<br/>'
               '<b>Chave PIX (CNPJ):</b> 77.858.611/0014-14<br/><b>Favorecido:</b> Unimed Grande '
               'Florianópolis Cooperativa de Trabalho Médico'),
             p('<b>TRANSFERÊNCIA BANCÁRIA</b><br/><b>Favorecido:</b> Unimed Grande Florianópolis '
               'Cooperativa de Trabalho Médico<br/><b>CNPJ:</b> 77.858.611/0014-14<br/>'
               '<b>Banco Unicred (136)</b> | <b>Agência:</b> 1214-9 | <b>Conta:</b> 102646-1')],
            [p('<b>CARTÃO DE CRÉDITO</b><br/>• Até R$ 10.000,00: Parcelamento em até 6x sem juros.'
               '<br/>• Acima de R$ 10.000,00: Parcelamento em até 10x sem juros.<br/>'
               '<i>Solicite o link informando nº do orçamento, bandeira e parcelas.</i><br/>'
               '<b>Contato:</b> +55 48 9181-5582 | concierge@unimedflorianopolis.com.br'),
             p('<b>ENVIO DO COMPROVANTE / LINK DE PAGAMENTO</b><br/><b>WhatsApp:</b> '
               '+55 48 9181-5582<br/><b>E-mail:</b> concierge@unimedflorianopolis.com.br')]
        ]
        tb_pag = Table(pagamento, colWidths=[9.4*cm, 9.4*cm])
        tb_pag.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),LIGHT),
                                    ('BOX',(0,0),(-1,-1),.5,BORDER),
                                    ('INNERGRID',(0,0),(-1,-1),.4,BORDER),
                                    ('PADDING',(0,0),(-1,-1),3.5),
                                    ('VALIGN',(0,0),(-1,-1),'TOP')]))
        story.extend([tb_pag, Spacer(1, 7)])

    story.append(p('03 - INFORMAÇÕES ADICIONAIS SOBRE O ORÇAMENTO', secao))
    adicionais = ('• <b>Adicionais:</b> Demais materiais, medicamentos, gases medicinais e OPMEs '
                 '(órteses, próteses e materiais especiais) que não compõem o orçamento serão '
                 'acrescentados ao valor final quando utilizados.<br/>'
                 '• <b>Variações e Excedentes:</b> Os valores contidos nesta previsão de gastos não são '
                 'absolutos. Caso haja utilização de tempo excedente de sala cirúrgica ou período de '
                 'permanência hospitalar superior ao estipulado, ocorrerão alterações de valores.<br/>'
                 '• <b>Reoperações, Complicações e Intercorrências:</b> Em casos de reoperações, '
                 'complicações, intercorrências ou diárias além das previstas, os valores extras serão '
                 'cobrados posteriormente no momento da alta.<br/>'
                 '• <b>Devoluções de OPME:</b> Em casos de necessidade de devolução de valores referentes '
                 'a OPME não utilizado durante a cirurgia, o processo será concluído em até 10 dias '
                 'úteis após a alta do paciente.')
    if convenio:
        adicionais = ('• <b>Cobertura e autorizações:</b> A realização e a cobertura de procedimentos, '
                     'materiais, exames, diárias e eventuais intercorrências dependem das regras do '
                     'convênio e da autorização da operadora.<br/>'
                     '• <b>Excedentes:</b> Tempo de sala, permanência e materiais não previstos '
                     'serão submetidos às regras de cobertura e faturamento aplicáveis.<br/>'
                     '• <b>Intercorrências:</b> Reoperações e complicações exigem análise de cobertura '
                     'e autorização conforme as regras assistenciais e contratuais.')
    story.extend([p(adicionais), Spacer(1, 7), p('SERVIÇO EXCLUSIVO DE CONCIERGE ROBÓTICA', secao)])
    concierge = ('Para proporcionar uma experiência segura, ágil e acolhedora, os pacientes de '
                'cirurgia robótica contam com o nosso serviço exclusivo de Concierge. Estamos à '
                'disposição para auxiliar em todas as etapas do seu procedimento. Em caso de dúvidas, '
                'entre em contato diretamente pelo WhatsApp: <b>+55 48 9181-5582</b> ou pelo e-mail '
                '<b>concierge@unimedflorianopolis.com.br</b>')
    qr_celula = [create_qr_code(), Spacer(1, 1), p('Aponte a câmera<br/>para o WhatsApp', pequeno)]
    tb_con = Table([[p(concierge), qr_celula]], colWidths=[15.2*cm, 3.6*cm])
    tb_con.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#ECFDF5')),
                                ('BOX',(0,0),(-1,-1),.5,GREEN),('PADDING',(0,0),(-1,-1),3.8),
                                ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ALIGN',(1,0),(1,0),'CENTER')]))
    story.extend([tb_con, Spacer(1, 8)])
    mini = Table([[p(f'<b>ORÇAMENTO Nº {seguro("numero_orcamento")}</b>', cel_v),
                   p(f'<b>VÁLIDO ATÉ {seguro("data_validade")}</b>', cel_b)]],
                 colWidths=[4.2*cm, 4.2*cm])
    mini.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#F3F4F6')),
                              ('BOX',(0,0),(-1,-1),.5,BORDER),('PADDING',(0,0),(-1,-1),2.8)]))
    assinatura = Table([[mini, p(f'<para align="right">São José, {seguro("data_solicitacao")}<br/><br/>'
                                '________________________________________<br/><b>Responsável Paciente</b></para>')]],
                       colWidths=[9.4*cm, 9.4*cm])
    assinatura.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'BOTTOM'),
                                    ('PADDING',(0,0),(-1,-1),0)]))
    story.append(assinatura)

    class OrcamentoCanvas(NumberedCanvas):
        orc_number = str(dados['numero_orcamento'])

    doc.build(story, canvasmaker=OrcamentoCanvas)
    resultado = buffer.getvalue()
    buffer.close()
    return resultado


# --- EXTRAÇÃO TASY: SEM PREENCHIMENTO FICTÍCIO ---
# A extração é feita em linhas, preservando colunas. Etiquetas sem valor
# não podem consumir a linha seguinte, sobretudo "Acomodação" e "Tempo CC".
import unicodedata

VALOR_BR = re.compile(
    r"(?<![\dA-Za-z])(?:R\$\s*)?(-?\d{1,3}(?:\.\d{3})+(?:,\d{2})?|-?\d+(?:,\d{2})|-?\d+\.\d{2})(?![\dA-Za-z])",
    re.IGNORECASE
)
VALOR_COM_R = re.compile(r"R\$\s*(-?\d+(?:[\.,]\d{3})*(?:[\.,]\d{2})?)", re.IGNORECASE)


def simplificar(txt):
    sem_acento = unicodedata.normalize('NFKD', str(txt or ''))
    return ''.join(ch for ch in sem_acento if not unicodedata.combining(ch)).lower()


def linhas_documento(texto, dividir_colunas=False):
    linhas = []
    for original in (texto or '').splitlines():
        partes = re.split(r'\s{3,}', original.strip()) if dividir_colunas else [original.strip()]
        linhas.extend(re.sub(r'\s+', ' ', parte).strip(' |\t')
                      for parte in partes if parte.strip())
    return linhas


def rotulo_linha(linha, rotulos):
    """Exige rótulo no início da linha e obtém só a parte após ':' ou o rótulo."""
    for padrao in rotulos:
        m = re.match(r'^\s*(?:' + padrao + r')\s*(?:[:\-–|]\s*)?(.*)$', linha, re.I)
        if m:
            return m.group(1).strip()
    return None


def obter_campo(linhas, rotulos, valida=None):
    """Aceita valor adjacente se plausível; nunca aceita qualquer linha aleatória."""
    for i, linha in enumerate(linhas):
        resto = rotulo_linha(linha, rotulos)
        if resto is None:
            continue
        candidatas = [resto]
        if i+1 < len(linhas) and not resto:
            candidatas.append(linhas[i+1])
        for candidato in candidatas:
            candidato = candidato.strip(' :|-')
            if candidato and (valida is None or valida(candidato)):
                return candidato
    return ''


def acomodacao_valida(txt):
    s = simplificar(txt)
    opcoes = ['apartamento', 'enfermaria', 'hospital dia', 'hospital-dia', 'suite', 'quarto',
              'privativo', 'coletivo', 'semi privativo', 'semiprivativo']
    if re.search(r'\d+\s*(min|hora|hrs|h)\b', s) or ' min cc' in s:
        return False
    return any(p in s for p in opcoes) and len(s) < 90


def tempo_valido(txt):
    s = simplificar(txt)
    return bool(re.search(r'\b\d{1,4}\s*(?:min(?:utos?)?|horas?|h|hrs?)(?:\s*cc)?\b', s))


def tempo_formatado(txt):
    s = simplificar(txt)
    m = re.search(r'\b(\d{1,4})\s*(min(?:utos?)?|horas?|h|hrs?)(?:\s*(?:cc|centro cirurgico))?\b', s)
    if not m:
        return ''
    unidade = m.group(2)
    if unidade.startswith('h') and not unidade.startswith('hora'):
        return f'{int(m.group(1))} hora(s)'
    if unidade.startswith('hora'):
        return f'{int(m.group(1))} hora(s)'
    return f'{int(m.group(1))} Min CC'


def valores_na_linha(linha):
    """Identifica quantias (não quantidades, códigos, horários ou datas)."""
    achados = [(m.start(), parse_moeda(m.group(1))) for m in VALOR_BR.finditer(linha)]
    if not achados:
        achados = [(m.start(), parse_moeda(m.group(1))) for m in VALOR_COM_R.finditer(linha)]
    return [valor for _, valor in sorted(achados)]


def quantidade_da_linha(linha):
    # Para valores unitários + quantidade, usar o valor monetário da última coluna.
    vals = valores_na_linha(linha)
    return vals[-1] if vals else None


def encontrar_valores_categoria(linhas, categoria):
    """Prioriza o valor total de cada categoria; soma materiais apenas se houver linhas específicas."""
    regras = {
        'valor_hospital': [r'\btaxa\s+(?:de\s+)?procedimento\b', r'\btaxa\s+(?:de\s+)?uso\s+(?:de\s+)?robo',
                           r'\bprocedimento\s+robotic', r'\bpacote\s+robotic',
                           r'\bvalor\s+hospitalar', r'\bservicos?\s+hospitalares?',
                           r'\btotal\s+hospital', r'(?<!\d)89999901(?!\d)', r'\btaxa.*\brobo\b'],
        'valor_anestesia': [r'honorarios?\s+(?:medicos?\s+)?anestesic',
                            r'honorarios?\s+(?:de\s+)?anestesista', r'\bvalor\s+(?:da\s+)?anestesia',
                            r'\btaxa\s+(?:de\s+)?anestesia', r'^anestesia\b'],
        'valor_materiais': [r'pin[cç]as?', r'materiais?\s+robotic', r'instrumentais?\s+robotic',
                           r'\bdrape', r'\bkit\s+robotic', r'\binsumos?\s+robotic',
                           r'\btotal\s+(?:de\s+)?materiais?\s+robotic', r'\bvessel\s+sealer', r'\bprograsp', r'\bbipolar', r'\bmonopolar']
    }
    padroes = [re.compile(x, re.I) for x in regras[categoria]]
    encontrados = []
    for i, linha in enumerate(linhas):
        x = simplificar(linha)
        if not any(p.search(x) for p in padroes):
            continue
        if re.search(r'nao inclus|nao cobre|exceto|pagamento|instruc|observa[cç]|termos? e condi[cç]', x):
            continue
        valor = quantidade_da_linha(linha)
        if valor is None and i+1 < len(linhas):
            # Aceita a próxima linha somente quando for numérica, sem texto descritivo.
            prox = linhas[i+1]
            if re.fullmatch(r'[\sR$\d.,|\-]+', prox):
                valor = quantidade_da_linha(prox)
        if valor is not None:
            encontrados.append((linha, valor))
    if not encontrados:
        return None
    # Uma linha TOTAL explícita prevalece sobre as demais, evitando dupla contagem.
    totais = [v for linha, v in encontrados if 'total' in simplificar(linha)]
    if totais:
        return totais[-1]
    # Materiais podem constar em várias linhas; evita itens replicados na mesma lista.
    if categoria == 'valor_materiais' and len(encontrados) > 1:
        unicos = {(simplificar(l), v) for l, v in encontrados}
        return round(sum(v for _, v in unicos), 2)
    return encontrados[-1][1]


def ler_campos_nativos(texto_normal, texto_layout=''):
    """Extrai campos do TASY e preserva evidências de quais foram localizados."""
    fontes = [linhas_documento(texto_layout), linhas_documento(texto_normal)]
    # O layout preserva melhor tabelas; texto normal ajuda com títulos concatenados.
    fontes = [f for f in fontes if f]
    linhas = fontes[0] if fontes else []
    linhas_aux = fontes[1] if len(fontes) > 1 else []
    linhas_celulas = linhas_documento(texto_layout, dividir_colunas=True)
    dados = dict(paciente='', medico='', numero_orcamento='', tipo_orcamento='HÍBRIDO',
                 data_solicitacao='', data_validade='', tempo_cc='', acomodacao='',
                 valor_hospital=0.0, valor_anestesia=0.0, valor_materiais=0.0)
    encontrados = set()

    testes = {
        'paciente': ([r'(?:nome\s+(?:do\s+)?)?paciente', r'nome\s+cliente'],
                     lambda v: bool(re.search(r'[A-Za-zÀ-ÿ]{2,}', v)) and len(v) < 140 and not re.match(r'(?i)^(m[ée]dico|or[çc]amento|data|acomoda[çc][ãa]o|tempo|conv[êe]nio)', v)),
        'medico': ([r'm[eé]dico\s+(?:respons[aá]vel|solicitante|cirurgi[aã]o)', r'cirurgi[aã]o\s+(?:respons[aá]vel)?', r'm[eé]dico'],
                   lambda v: bool(re.search(r'[A-Za-zÀ-ÿ]{2,}', v)) and len(v) < 140),
        'numero_orcamento': ([r'(?:n[úu]mero\s+(?:do\s+)?)?or[cç]amento\s*(?:n[ºo.]|numero|nro|no)?',
                              r'orc\s*(?:n[ºo.]|numero|nro)?'],
                             lambda v: bool(re.search(r'\d{3,}', v))),
        'data_solicitacao': ([r'data\s+(?:da\s+)?solicita[cç][aã]o', r'data\s+(?:de\s+)?emiss[aã]o',
                             r'emiss[aã]o', r'data\s+(?:do\s+)?or[cç]amento'],
                            lambda v: bool(re.search(r'\d{2}/\d{2}/\d{4}', v))),
        'acomodacao': ([r'(?:tipo\s+(?:de\s+)?)?acomoda[cç][aã]o', r'tipo\s+de\s+quarto'],
                       acomodacao_valida),
        'tempo_cc': ([r'tempo\s+previsto(?:\s+(?:de|da)\s+cirurgia)?', r'tempo\s+cir[úu]rgico',
                      r'dura[cç][aã]o\s+(?:do\s+)?(?:procedimento|cirurgia)', r'tempo\s+de\s+cc'],
                     tempo_valido),
        'data_validade': ([r'(?:data\s+de\s+)?validade', r'v[aá]lido\s+at[eé]'],
                          lambda v: bool(re.search(r'\d{2}/\d{2}/\d{4}', v))),
    }
    for chave, (rotulos, valido) in testes.items():
        valor = obter_campo(linhas, rotulos, valido)
        if not valor and linhas_celulas:
            valor = obter_campo(linhas_celulas, rotulos, valido)
        if not valor and linhas_aux:
            valor = obter_campo(linhas_aux, rotulos, valido)
        if valor:
            if chave in ('data_solicitacao', 'data_validade'):
                valor = re.search(r'\d{2}/\d{2}/\d{4}', valor).group(0)
            elif chave == 'numero_orcamento':
                m = re.search(r'\d{3,}', valor)
                valor = m.group(0) if m else ''
            elif chave == 'tempo_cc':
                valor = tempo_formatado(valor)
            elif chave == 'acomodacao':
                # Registra somente o tipo de acomodação, não os demais campos da linha.
                m = re.search(r'apartamento|enfermaria|hospital[ -]?dia|su[íi]te|quarto(?:\s+privativo)?|semiprivativo|coletivo', valor, re.I)
                valor = m.group(0).capitalize() if m else valor
            dados[chave] = valor
            encontrados.add(chave)

    if not dados['tempo_cc']:
        # Alguns arquivos TASY indicam a duração apenas em uma linha "300 Min CC".
        for linha in linhas + linhas_aux:
            if re.fullmatch(r'\s*\d{1,4}\s*min(?:utos?)?\s*cc\s*', simplificar(linha)):
                dados['tempo_cc'] = tempo_formatado(linha)
                encontrados.add('tempo_cc')
                break
    if not dados['acomodacao']:
        # Retém como dado confirmado apenas um tipo inequivocamente presente.
        opções = set()
        for linha in linhas + linhas_aux:
            if re.fullmatch(r'(?:apartamento|enfermaria|hospital[ -]?dia|su[ií]te|quarto privativo)', linha, re.I):
                opções.add(linha.capitalize())
        if len(opções) == 1:
            dados['acomodacao'] = opções.pop()
            encontrados.add('acomodacao')

    for campo in ('valor_hospital', 'valor_anestesia', 'valor_materiais'):
        valor = encontrar_valores_categoria(linhas, campo)
        if valor is None and linhas_aux:
            valor = encontrar_valores_categoria(linhas_aux, campo)
        if valor is not None:
            dados[campo] = float(valor)
            encontrados.add(campo)

    # Orçamento híbrido/particular é escolhido manualmente e prevalece sobre o PDF.
    tipo = obter_campo(linhas, [r'tipo\s+de\s+or[cç]amento', r'modalidade'], lambda v: len(v) < 45)
    if tipo:
        dados['tipo_orcamento'] = normalizar_modalidade(tipo)
    elif re.search(r'100%\s*conv[eê]nio|100%\s*convenio', texto_normal, re.I):
        dados['tipo_orcamento'] = '100% CONVÊNIO'

    if dados['data_solicitacao'] and not dados['data_validade']:
        dados['data_validade'] = formatar_validade(dados['data_solicitacao'])
    dados['_campos_localizados'] = sorted(encontrados)
    return dados


def valores_reconhecidos_no_texto(texto):
    return [parse_moeda(m.group(1)) for m in VALOR_BR.finditer(texto)]


def extrair_dados_tasy(texto, api_key, texto_layout=''):
    dados = ler_campos_nativos(texto, texto_layout)
    chave = str(api_key or '').strip().strip('\"\'').replace(' ', '')
    if not chave:
        dados['_aviso_motor'] = ('Gemini não configurado. Extração local utilizada; campos não '
                                 'localizados precisam ser conferidos no orçamento original.')
        return dados, 'Nativo'
    try:
        cliente = genai.Client(api_key=chave)
        prompt = """Extraia dados APENAS do orçamento hospitalar TASY abaixo. Não deduza, não estime,
        não preencha valores ou tempos padrão. Extraia números da coluna VALOR TOTAL, não preço
        unitário. Acomodação só pode ser Apartamento, Enfermaria, Hospital-dia, Suíte ou outro tipo
        de leito explicitamente escrito. "300 Min CC" é tempo, NÃO acomodação.
        Separe os valores de: (1) taxa hospitalar de cirurgia robótica, (2) honorários anestésicos,
        (3) TOTAL de pinças, drapes e materiais robóticos. Se a fonte mostrar apenas um total geral,
        NÃO distribua esse total entre as três categorias. Caso uma categoria não esteja explícita,
        retorne null. Campos: paciente, medico, numero_orcamento, tipo_orcamento,
        data_solicitacao (DD/MM/AAAA), tempo_cc, acomodacao, valor_hospital,
        valor_anestesia, valor_materiais. JSON puro, com null nos campos ausentes.
        TEXTO ORIGINAL DO DOCUMENTO:
        """
        entrada = (texto_layout or texto)[:75000]
        resposta = cliente.models.generate_content(
            model=st.secrets.get('GEMINI_MODEL', 'gemini-2.5-flash'),
            contents=[prompt, entrada],
            config=types.GenerateContentConfig(response_mime_type='application/json', temperature=0)
        )
        extraidos = json.loads(resposta.text)
        monetarios = valores_reconhecidos_no_texto(texto + '\n' + texto_layout)
        encontrados = set(dados['_campos_localizados'])
        for campo in ('paciente', 'medico', 'numero_orcamento', 'data_solicitacao',
                      'tempo_cc', 'acomodacao', 'valor_hospital', 'valor_anestesia', 'valor_materiais'):
            item = extraidos.get(campo)
            if item is None or str(item).strip() == '':
                continue
            if campo in ('valor_hospital', 'valor_anestesia', 'valor_materiais'):
                numero = parse_moeda(item)
                # Proteção: valor sugerido pela IA precisa existir no arquivo original.
                if not any(abs(numero - x) < 0.011 for x in monetarios):
                    continue
                if campo not in encontrados:
                    dados[campo] = numero
                    encontrados.add(campo)
            elif campo == 'acomodacao':
                if campo not in encontrados and acomodacao_valida(str(item)):
                    dados[campo] = str(item).strip()
                    encontrados.add(campo)
            elif campo == 'tempo_cc':
                if campo not in encontrados and tempo_valido(str(item)):
                    duracao = tempo_formatado(str(item))
                    # Só aceita número se houver duração correspondente no original.
                    m = re.search(r'\d+', duracao)
                    if m and re.search(r'(?<!\d)' + re.escape(m.group(0)) + r'\s*(?:min|hora|hrs?|h)\b',
                                       simplificar(texto + '\n' + texto_layout)):
                        dados[campo] = duracao
                        encontrados.add(campo)
            elif campo == 'numero_orcamento':
                achado = re.search(r'\d{3,}', str(item))
                if campo not in encontrados and achado and achado.group(0) in texto:
                    dados[campo] = achado.group(0)
                    encontrados.add(campo)
            elif campo == 'data_solicitacao':
                if campo not in encontrados and re.fullmatch(r'\d{2}/\d{2}/\d{4}', str(item)) and str(item) in texto:
                    dados[campo] = str(item)
                    encontrados.add(campo)
            elif campo not in encontrados:
                # Nomes precisam aparecer no texto (normalizados para espaços).
                if simplificar(re.sub(r'\s+', ' ', str(item))).strip() in simplificar(re.sub(r'\s+', ' ', texto)):
                    dados[campo] = str(item).strip()
                    encontrados.add(campo)
        if not dados['data_validade'] and dados['data_solicitacao']:
            dados['data_validade'] = formatar_validade(dados['data_solicitacao'])
        dados['_campos_localizados'] = sorted(encontrados)
        return dados, 'Nativo + IA'
    except Exception as erro:
        # Não oculta falhas: o operador poderá conferir os dados com o PDF original.
        dados['_aviso_motor'] = ('Não foi possível complementar com o Gemini ('
                                 + type(erro).__name__ + '). Extração local utilizada.')
        return dados, 'Nativo'


def formatar_validade(data_str):
    try:
        return (datetime.strptime(data_str, '%d/%m/%Y') + timedelta(days=30)).strftime('%d/%m/%Y')
    except (ValueError, TypeError):
        return ''


def extrair_textos_pdf(arquivo_bytes):
    leitor = pypdf.PdfReader(io.BytesIO(arquivo_bytes))
    texto_normal, texto_layout = [], []
    for pagina in leitor.pages:
        texto_normal.append(pagina.extract_text() or '')
        try:
            texto_layout.append(pagina.extract_text(extraction_mode='layout',
                                                    layout_mode_space_vertically=False) or '')
        except (TypeError, ValueError, AttributeError):
            texto_layout.append(pagina.extract_text() or '')
    return '\n'.join(texto_normal), '\n'.join(texto_layout)


init_db()
if os.path.exists('unimed_logo_official.png'):
    st.image('unimed_logo_official.png', width=220)
st.title('Gerador e Gestor de Orçamentos de Cirurgia Robótica')
st.caption('Hospital Unimed Grande Florianópolis · Programa de Cirurgia Robótica')
tab1, tab2 = st.tabs(['📄 Gerar Orçamento', '📊 Painel de Acompanhamento e Conversão'])

with tab1:
    st.markdown('---')
    try:
        api_key = str(st.secrets.get('GEMINI_API_KEY', '')).strip().strip('"\'')
    except Exception:
        api_key = ''
    arquivo = st.file_uploader('📄 Envie o PDF do Orçamento Padrão do TASY:', type=['pdf'])
    if arquivo is not None:
        arquivo_bytes = arquivo.getvalue()
        import hashlib
        hash_arquivo = hashlib.sha256(arquivo_bytes).hexdigest()
        if st.session_state.get('hash_orcamento') != hash_arquivo:
            st.session_state['hash_orcamento'] = hash_arquivo
            st.session_state.pop('dados_orcamento', None)
            st.session_state.pop('pdf_final', None)
        if st.button('🚀 Processar e Gerar Orçamento Oficial', type='primary'):
            with st.spinner('Lendo o orçamento original...'):
                try:
                    texto_tasy, texto_layout = extrair_textos_pdf(arquivo_bytes)
                    if not texto_tasy.strip():
                        st.error('PDF sem texto selecionável. Envie o PDF original exportado do TASY.')
                    else:
                        dados, motor = extrair_dados_tasy(texto_tasy, api_key, texto_layout)
                        if not dados.get('data_validade') and dados.get('data_solicitacao'):
                            dados['data_validade'] = formatar_validade(str(dados['data_solicitacao']))
                        st.session_state['dados_orcamento'] = dados
                        st.session_state.pop('pdf_final', None)
                        st.success(f'Leitura realizada ({motor}). Confira os campos antes de gerar.')
                        if dados.get('_aviso_motor'):
                            st.warning(dados['_aviso_motor'])
                except Exception as erro:
                    st.error(f'Erro ao processar orçamento: {erro}')

    if 'dados_orcamento' in st.session_state and arquivo is not None:
        d = st.session_state['dados_orcamento']
        st.subheader('📋 Resumo dos Dados Processados — Edição e Conferência')
        st.info('Todos os campos podem ser corrigidos. A modalidade escolhida aqui prevalece sobre a identificada no PDF.')
        obrigatorios = ('paciente', 'medico', 'numero_orcamento', 'acomodacao', 'tempo_cc',
                        'data_solicitacao', 'valor_hospital', 'valor_anestesia', 'valor_materiais')
        nomes = {'paciente': 'Paciente', 'medico': 'Médico', 'numero_orcamento': 'Número do orçamento',
                 'acomodacao': 'Acomodação', 'tempo_cc': 'Tempo cirúrgico',
                 'data_solicitacao': 'Data da solicitação', 'valor_hospital': 'Taxa hospitalar',
                 'valor_anestesia': 'Honorários anestésicos', 'valor_materiais': 'Materiais robóticos'}
        campos_nao_localizados = [nomes[k] for k in obrigatorios if k not in set(d.get('_campos_localizados', []))]
        if campos_nao_localizados:
            st.warning('Não localizados com segurança no TASY: ' + ', '.join(campos_nao_localizados)
                       + '. Esses campos permanecem disponíveis para preenchimento manual.')
        if d.get('_aviso_motor'):
            st.caption(d['_aviso_motor'])
        with st.form('form_revisao_orcamento'):
            c1, c2 = st.columns(2)
            with c1:
                paciente = st.text_input('Paciente', value=str(d['paciente']))
                medico = st.text_input('Médico responsável / solicitante', value=str(d['medico']))
                numero = st.text_input('Número do orçamento', value=str(d['numero_orcamento']))
                acomodacao = st.text_input('Acomodação', value=str(d['acomodacao']))
                data_sol = st.text_input('Data da solicitação (DD/MM/AAAA)', value=str(d['data_solicitacao']))
            with c2:
                tipo = st.selectbox('Modelo do orçamento', MODALIDADES,
                                    index=MODALIDADES.index(normalizar_modalidade(d['tipo_orcamento'])))
                tempo = st.text_input('Tempo previsto de cirurgia', value=str(d['tempo_cc']))
                data_val = st.text_input('Validade do orçamento (DD/MM/AAAA)',
                                         value=str(d.get('data_validade', '')))
                hosp = st.number_input('Taxa de Procedimento (Hospital) — R$', min_value=0.0,
                                       value=max(0.0, parse_moeda(d['valor_hospital'])), step=100.0, format='%.2f')
                anest = st.number_input('Honorários Anestésicos — R$', min_value=0.0,
                                        value=max(0.0, parse_moeda(d['valor_anestesia'])), step=100.0, format='%.2f')
                mat = st.number_input('Pinças e Materiais Robóticos — R$', min_value=0.0,
                                      value=max(0.0, parse_moeda(d['valor_materiais'])), step=100.0, format='%.2f')
            if tipo == '100% CONVÊNIO':
                st.caption('100% convênio: os valores editados acima não serão cobrados do paciente nem exibidos como pagamento particular.')
            else:
                st.caption('Os valores acima serão apresentados como responsabilidade financeira do paciente.')
            campos_sem_extracao = any(
                chave not in d.get('_campos_localizados', [])
                for chave in ('valor_hospital', 'valor_anestesia', 'valor_materiais')
            )
            exigir_confirmacao = tipo != '100% CONVÊNIO' and (
                campos_sem_extracao or hosp == 0 or anest == 0 or mat == 0
            )
            confirmacao_valores = True
            if exigir_confirmacao:
                st.warning('Há valores zerados ou não localizados automaticamente. Confira no TASY antes de continuar.')
                confirmacao_valores = st.checkbox(
                    'Conferi os valores no PDF original e confirmo os valores indicados, inclusive eventuais zeros.',
                    value=False)
            confirmar = st.form_submit_button('✅ Confirmar dados e gerar PDF oficial', type='primary')
        if confirmar:
            try:
                datetime.strptime(data_sol, '%d/%m/%Y')
                datetime.strptime(data_val, '%d/%m/%Y')
                if not numero.strip() or not paciente.strip() or not medico.strip():
                    raise ValueError('Informe número do orçamento, paciente e médico responsável.')
                if not acomodacao.strip() or not acomodacao_valida(acomodacao.strip()):
                    raise ValueError('Acomodação não identificada ou inválida. Confira e informe o tipo de acomodação do TASY.')
                if not tempo.strip() or not tempo_valido(tempo.strip()):
                    raise ValueError('Tempo cirúrgico não identificado ou inválido. Confira e informe a duração do TASY.')
                if tipo != '100% CONVÊNIO' and (hosp + anest + mat <= 0):
                    raise ValueError('Os três valores estão zerados. Confira o PDF e informe os valores antes de gerar.')
                if tipo != '100% CONVÊNIO' and not confirmacao_valores:
                    raise ValueError('É necessário conferir e confirmar os valores financeiros do orçamento original.')
                atualizados = dict(numero_orcamento=numero.strip(), paciente=paciente.strip(),
                                   medico=medico.strip(), tipo_orcamento=tipo,
                                   data_solicitacao=data_sol, data_validade=data_val,
                                   tempo_cc=tempo.strip(), acomodacao=acomodacao.strip(),
                                   valor_hospital=float(hosp), valor_anestesia=float(anest),
                                   valor_materiais=float(mat))
                pdf = gerar_pdf_bytes(atualizados)
                if len(pypdf.PdfReader(io.BytesIO(pdf)).pages) != 1:
                    raise ValueError('O conteúdo excedeu uma página A4. Reduza campos muito extensos.')
                salvar_orcamento_db(atualizados, valor_paciente(atualizados))
                atualizados['_campos_localizados'] = d.get('_campos_localizados', [])
                st.session_state['dados_orcamento'] = atualizados
                st.session_state['pdf_final'] = pdf
                st.session_state['nome_pdf'] = (f'Orcamento_{numero.strip()}_'
                    f'{re.sub(r"[^\w-]", "_", paciente.strip())}.pdf')
                st.success('Orçamento gerado e salvo no histórico com os dados confirmados.')
            except Exception as erro:
                st.error(f'Não foi possível gerar o orçamento: {erro}')
        if st.session_state.get('pdf_final'):
            atual = st.session_state['dados_orcamento']
            st.write(f"**Modalidade:** {atual['tipo_orcamento']}")
            st.markdown(f"### **Valor total do paciente:** :green[{fmt_m(valor_paciente(atual))}]")
            st.download_button('📥 Baixar Orçamento Oficial em PDF',
                               data=st.session_state['pdf_final'],
                               file_name=st.session_state['nome_pdf'], mime='application/pdf')

with tab2:
    st.subheader('📊 Painel de Controle e Histórico de Conversão')
    df = carregar_orcamentos_df()
    if df.empty:
        st.info('Nenhum orçamento foi gerado até o momento. Ao gerar propostas, elas aparecerão aqui.')
    else:
        total = len(df)
        convertidos = len(df[df['status'] == 'Convertido / Fechado'])
        pendentes = len(df[df['status'] == 'Pendente'])
        cols = st.columns(4)
        cols[0].metric('Total de Orçamentos', total)
        cols[1].metric('Convertidos / Fechados', convertidos)
        cols[2].metric('Pendentes', pendentes)
        cols[3].metric('Taxa de Conversão', f'{convertidos/total*100:.1f}%')
        st.markdown('---')
        st.subheader('🔔 Gestão de Alertas (48 Horas)')
        pendentes_48h = []
        for _, linha in df.iterrows():
            if linha['status'] != 'Pendente':
                continue
            try:
                criado = datetime.strptime(str(linha['created_at']), '%Y-%m-%d %H:%M:%S')
                if datetime.now() - criado >= timedelta(hours=48):
                    pendentes_48h.append(linha.to_dict())
            except (ValueError, TypeError):
                continue
        if pendentes_48h:
            st.warning(f'Existem {len(pendentes_48h)} orçamento(s) há mais de 48 horas ainda pendente(s)!')
            if st.button('📧 Enviar Alerta de 48h por E-mail para Comercial & Particular'):
                sucesso, mensagem = enviar_email_alerta(pendentes_48h)
                (st.success if sucesso else st.error)(mensagem)
        else:
            st.success('✅ Nenhum orçamento pendente com mais de 48 horas de emissão.')
        st.markdown('---')
        st.subheader('📋 Tabela Interativa de Acompanhamento')
        st.caption('Altere o status de cada orçamento na caixa de seleção abaixo:')
        for _, linha in df.iterrows():
            with st.expander(f"📌 Orçamento Nº {linha['numero_orcamento']} - {linha['paciente']} | Status Atual: {linha['status']}"):
                c1, c2, c3 = st.columns(3)
                with c1:
                    st.write(f"**Médico:** {linha['medico']}")
                    st.write(f"**Modalidade:** {linha['tipo_orcamento']}")
                with c2:
                    st.write(f"**Data Solicitação:** {linha['data_solicitacao']}")
                    st.write(f"**Valor Total:** {fmt_m(linha['valor_total'])}")
                with c3:
                    opcoes = ['Pendente', 'Convertido / Fechado', 'Não Convertido / Cancelado']
                    indice = opcoes.index(linha['status']) if linha['status'] in opcoes else 0
                    novo = st.selectbox(f"Alterar Status ({linha['numero_orcamento']}):",
                                        opcoes, index=indice, key=f"status_{linha['numero_orcamento']}")
                    if novo != linha['status']:
                        atualizar_status_db(linha['numero_orcamento'], novo)
                        st.success(f"Status atualizado para '{novo}'!")
                        st.rerun()

