import io
import json
import os
import sqlite3
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from PIL import Image
import pypdf
import reportlab
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
import streamlit as st
import pandas as pd
from google import genai
from google.genai import types

# --- CONFIGURAÇÃO DA PÁGINA STREAMLIT ---
st.set_page_config(
    page_title="Gerador de Orçamentos Robótica - HUGF",
    page_icon="🏥",
    layout="wide",
)

# --- BANCO DE DADOS LOCAL (SQLITE) ---
DB_FILE = "orcamentos.db"

def init_db():
  conn = sqlite3.connect(DB_FILE)
  c = conn.cursor()
  c.execute("""
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
  conn.close()

init_db()

def salvar_orcamento_db(dados, valor_total):
  conn = sqlite3.connect(DB_FILE)
  c = conn.cursor()
  created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  
  c.execute("""
      INSERT OR REPLACE INTO orcamentos 
      (numero_orcamento, paciente, medico, tipo_orcamento, data_solicitacao, data_validade, tempo_cc, acomodacao, valor_total, created_at, status, alerta_enviado)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT status FROM orcamentos WHERE numero_orcamento = ?), 'Pendente'), COALESCE((SELECT alerta_enviado FROM orcamentos WHERE numero_orcamento = ?), 0))
  """, (
      dados["numero_orcamento"],
      dados["paciente"],
      dados["medico"],
      dados.get("tipo_orcamento", "HÍBRIDO"),
      dados["data_solicitacao"],
      dados["data_validade"],
      dados["tempo_cc"],
      dados["acomodacao"],
      valor_total,
      created_at,
      dados["numero_orcamento"],
      dados["numero_orcamento"]
  ))
  conn.commit()
  conn.close()

def carregar_orcamentos_df():
  conn = sqlite3.connect(DB_FILE)
  df = pd.read_sql_query("SELECT * FROM orcamentos ORDER BY created_at DESC", conn)
  conn.close()
  return df

def atualizar_status_db(numero_orcamento, novo_status):
  conn = sqlite3.connect(DB_FILE)
  c = conn.cursor()
  c.execute("UPDATE orcamentos SET status = ? WHERE numero_orcamento = ?", (novo_status, numero_orcamento))
  conn.commit()
  conn.close()

def marcar_alerta_enviado(numero_orcamento):
  conn = sqlite3.connect(DB_FILE)
  c = conn.cursor()
  c.execute("UPDATE orcamentos SET alerta_enviado = 1 WHERE numero_orcamento = ?", (numero_orcamento,))
  conn.commit()
  conn.close()

# --- FUNÇÃO DE ENVIO DE E-MAIL SMTP ---
def enviar_email_alerta(orcamentos_pendentes):
  try:
    smtp_server = st.secrets.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(st.secrets.get("SMTP_PORT", 587))
    smtp_user = st.secrets.get("SMTP_USER", "")
    smtp_password = st.secrets.get("SMTP_PASSWORD", "")

    if not smtp_user or not smtp_password:
      return False, "Credenciais de e-mail (SMTP_USER / SMTP_PASSWORD) não configuradas nos Secrets."

    destinatarios = [
        "comercialhospital@unimedflorianopolis.com.br",
        "cliente.particular@unimedflorianopolis.com.br",
    ]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚠️ ALERTA 48H: Orçamentos de Cirurgia Robótica Pendentes ({len(orcamentos_pendentes)})"
    msg["From"] = smtp_user
    msg["To"] = ", ".join(destinatarios)

    html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #1F2937;">
        <h2 style="color: #008C50;">Hospital Unimed Grande Florianópolis</h2>
        <h3>Programa de Cirurgia Robótica - Acompanhamento Comercial</h3>
        <p>Atenção, os seguintes orçamentos foram emitidos há <b>mais de 48 horas</b> e continuam marcados como <b>PENDENTES</b> no sistema:</p>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%; border-color: #E5E7EB;">
          <tr style="background-color: #F3F4F6; text-align: left;">
            <th>Orçamento Nº</th>
            <th>Paciente</th>
            <th>Médico</th>
            <th>Modalidade</th>
            <th>Data Emissão</th>
            <th>Valor Total</th>
          </tr>
    """
    for item in orcamentos_pendentes:
      html += f"""
          <tr>
            <td><b>{item['numero_orcamento']}</b></td>
            <td>{item['paciente']}</td>
            <td>{item['medico']}</td>
            <td>{item['tipo_orcamento']}</td>
            <td>{item['data_solicitacao']}</td>
            <td>R$ {item['valor_total']:,.2f}</td>
          </tr>
      """
    html += """
        </table>
        <p style="margin-top: 20px;">Por favor, verifique a negociação e atualize o status no painel do sistema.</p>
        <hr style="border: 0; border-top: 1px solid #E5E7EB;"/>
        <p style="font-size: 11px; color: #6B7280;">Este é um e-mail automático gerado pelo Gerador de Orçamentos de Cirurgia Robótica.</p>
      </body>
    </html>
    """

    msg.attach(MIMEText(html, "html"))

    server = smtplib.SMTP(smtp_server, smtp_port)
    server.starttls()
    server.login(smtp_user, smtp_password)
    server.sendmail(smtp_user, destinatarios, msg.as_string())
    server.quit()

    for item in orcamentos_pendentes:
      marcar_alerta_enviado(item['numero_orcamento'])

    return True, "Alertas enviados com sucesso por e-mail!"
  except Exception as e:
    return False, f"Erro ao enviar e-mail: {str(e)}"


# --- REPORTLAB CANVAS E MODELOS DE PDF ---
class NumberedCanvas(canvas.Canvas):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self._saved_page_states = []

  def showPage(self):
    self._saved_page_states.append(dict(self.__dict__))
    self._startPage()

  def save(self):
    num_pages = len(self._saved_page_states)
    for state in self._saved_page_states:
      self.__dict__.update(state)
      self.draw_page_decorations(num_pages)
      super().showPage()
    super().save()

  def draw_page_decorations(self, page_count):
    self.saveState()
    page_width, page_height = A4
    margin = 1.1 * cm

    unimed_green = colors.HexColor("#008C50")
    dark_gray = colors.HexColor("#1F2937")
    light_gray = colors.HexColor("#6B7280")
    border_gray = colors.HexColor("#E5E7EB")

    logo_path = "unimed_logo_official.png"
    if os.path.exists(logo_path):
      logo_w = 3.6 * cm
      logo_h = logo_w / (1560 / 646)
      self.drawImage(
          logo_path,
          margin,
          page_height - 1.55 * cm,
          width=logo_w,
          height=logo_h,
          mask="auto",
      )
      self.setFont("Helvetica-Bold", 9.0)
      self.setFillColor(dark_gray)
      self.drawString(margin + logo_w + 0.25 * cm, page_height - 1.0 * cm, "HOSPITAL UNIMED GRANDE FLORIANÓPOLIS")
      self.setFont("Helvetica-Bold", 7.5)
      self.setFillColor(unimed_green)
      self.drawString(margin + logo_w + 0.25 * cm, page_height - 1.3 * cm, "PROGRAMA DE CIRURGIA ROBÓTICA")

    orc_num = getattr(self, "orc_number", "")
    self.setFont("Helvetica-Bold", 7.0)
    self.setFillColor(light_gray)
    self.drawRightString(page_width - margin, page_height - 0.8 * cm, "ORÇAMENTO Nº")
    self.setFont("Helvetica-Bold", 14.0)
    self.setFillColor(unimed_green)
    self.drawRightString(page_width - margin, page_height - 1.35 * cm, orc_num)

    self.setStrokeColor(border_gray)
    self.setLineWidth(0.6)
    self.line(margin, page_height - 1.6 * cm, page_width - margin, page_height - 1.6 * cm)

    footer_line_y = 1.05 * cm
    self.setStrokeColor(border_gray)
    self.setLineWidth(0.6)
    self.line(margin, footer_line_y, page_width - margin, footer_line_y)

    qr_url = "https://wa.me/554891815582"
    qr_obj = qr.QrCodeWidget(qr_url)
    bounds = qr_obj.getBounds()
    qr_w = bounds[2] - bounds[0]
    qr_h = bounds[3] - bounds[1]
    qr_size = 18
    d_qr = Drawing(qr_size, qr_size, transform=[qr_size / qr_w, 0, 0, qr_size / qr_h, 0, 0])
    d_qr.add(qr_obj)
    d_qr.drawOn(self, margin, 0.2 * cm)

    text_x = margin + qr_size + 4
    self.setFont("Helvetica-Bold", 6.5)
    self.setFillColor(dark_gray)
    self.drawString(text_x, 0.7 * cm, "Hospital Unimed Grande Florianópolis")
    self.setFont("Helvetica", 6.5)
    self.setFillColor(light_gray)
    self.drawString(text_x, 0.4 * cm, "+55 48 9181-5582 Concierge Robótica HUGF")

    ona_path = None
    for filename in ["selo_ona.jpg", "selo_ona.png", "selo_ona.jpeg", "Selo ONA Nível 3 HUGF (2).jpg"]:
      if os.path.exists(filename):
        ona_path = filename
        break

    if ona_path:
      ona_w = 2.2 * cm
      ona_h = 1.1 * cm
      ona_x = (page_width - ona_w) / 2.0
      ona_y = 0.02 * cm
      self.drawImage(ona_path, ona_x, ona_y, width=ona_w, height=ona_h, mask="auto")

    page_str = f"Orçamento {orc_num}  |  Página {self._pageNumber} de {page_count}"
    self.setFont("Helvetica", 6.5)
    self.setFillColor(light_gray)
    self.drawRightString(page_width - margin, 0.55 * cm, page_str)
    self.restoreState()

def create_tag(text="HÍBRIDO (PACOTE)", is_particular=False):
  bg_color = colors.HexColor("#DC2626") if is_particular else colors.HexColor("#008C50")
  w = len(text) * 5.5 + 12
  h = 13
  d = Drawing(w, h)
  d.add(Rect(0, 0, w, h, rx=6, ry=6, fillColor=bg_color, strokeColor=None))
  d.add(String(w / 2.0, 3.2, text, textAnchor="middle", fontName="Helvetica-Bold", fontSize=7.0, fillColor=colors.white))
  return d

def create_qr_code(url="https://wa.me/554891815582", size=40):
  qr_code = qr.QrCodeWidget(url)
  bounds = qr_code.getBounds()
  w = bounds[2] - bounds[0]
  h = bounds[3] - bounds[1]
  d = Drawing(size, size, transform=[size / w, 0, 0, size / h, 0, 0])
  d.add(qr_code)
  return d

def fmt_m(val):
  return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def gerar_pdf_bytes(dados):
  buffer = io.BytesIO()
  doc = SimpleDocTemplate(
      buffer,
      pagesize=A4,
      leftMargin=1.1 * cm,
      rightMargin=1.1 * cm,
      topMargin=1.7 * cm,
      bottomMargin=1.1 * cm,
  )

  unimed_green = colors.HexColor("#008C50")
  dark_gray = colors.HexColor("#1F2937")
  light_bg = colors.HexColor("#F9FAFB")
  border_gray = colors.HexColor("#E5E7EB")
  red_box_bg = colors.HexColor("#FEF2F2")
  red_box_border = colors.HexColor("#FCA5A5")
  yellow_box_bg = colors.HexColor("#FEFCE8")
  yellow_box_border = colors.HexColor("#FDE047")

  title_style = ParagraphStyle("DocTitle", fontName="Helvetica-Bold", fontSize=10.5, leading=12.5, textColor=dark_gray, spaceAfter=1.0)
  subtitle_style = ParagraphStyle("DocSubtitle", fontName="Helvetica-Bold", fontSize=6.8, leading=8.2, textColor=unimed_green, spaceAfter=4.0)
  section_heading = ParagraphStyle("SectionHeading", fontName="Helvetica-Bold", fontSize=7.2, leading=8.8, textColor=unimed_green, spaceBefore=4.0, spaceAfter=2.0)
  normal_text = ParagraphStyle("NormalText", fontName="Helvetica", fontSize=6.1, leading=7.8, textColor=dark_gray)
  normal_bold = ParagraphStyle("NormalBold", fontName="Helvetica-Bold", fontSize=6.1, leading=7.8, textColor=dark_gray)
  table_header = ParagraphStyle("TableHeader", fontName="Helvetica-Bold", fontSize=6.1, leading=7.5, textColor=dark_gray)
  table_cell = ParagraphStyle("TableCell", fontName="Helvetica", fontSize=6.1, leading=7.5, textColor=dark_gray)
  table_cell_bold = ParagraphStyle("TableCellBold", fontName="Helvetica-Bold", fontSize=6.1, leading=7.5, textColor=dark_gray)
  table_cell_green = ParagraphStyle("TableCellGreen", fontName="Helvetica-Bold", fontSize=6.1, leading=7.5, textColor=unimed_green)
  concierge_style = ParagraphStyle("ConciergeBox", fontName="Helvetica", fontSize=6.1, leading=8.0, textColor=dark_gray)
  qr_label_style = ParagraphStyle("QRLabel", fontName="Helvetica-Bold", fontSize=5.8, leading=6.8, textColor=unimed_green, alignment=1)

  story = []

  tipo_str = str(dados.get("tipo_orcamento", "")).strip().upper()
  is_particular = "PARTICULAR" in tipo_str or "TOTAL" in tipo_str

  if is_particular:
    tag_label = "PARTICULAR (PACOTE)"
    convenio_label = "Particular"
  else:
    tag_label = "HÍBRIDO (PACOTE)"
    convenio_label = "Unimed/Híbrido"

  tag_obj = create_tag(tag_label, is_particular=is_particular)

  story.append(Paragraph("Orçamento cirúrgico", title_style))
  story.append(Paragraph("ATENDIMENTO PARTICULAR · PACOTE · CIRURGIA ROBÓTICA", subtitle_style))

  info_data = [
      [Paragraph(f"<b>PACIENTE</b><br/>{dados['paciente']}", normal_text), Paragraph(f"<b>MÉDICO RESPONSÁVEL</b><br/>{dados['medico']}", normal_text)],
      [Paragraph(f"<b>DATA DA SOLICITAÇÃO</b><br/>{dados['data_solicitacao']}", normal_text), Paragraph(f"<b>VALIDADE DO ORÇAMENTO</b><br/>{dados['data_validade']} · 30 dias", normal_text)],
      [Paragraph(f"<b>TEMPO PREVISTO DE CIRURGIA</b><br/>{dados['tempo_cc']}", normal_text), Paragraph(f"<b>ACOMODAÇÃO</b><br/>{dados['acomodacao']}", normal_text)],
      [[Paragraph("<b>TIPO DE ORÇAMENTO</b>", normal_text), Spacer(1, 1), tag_obj], Paragraph(f"<b>CONVÊNIO / MODALIDADE</b><br/>{convenio_label}", normal_text)],
  ]

  info_table = Table(info_data, colWidths=[9.4 * cm, 9.4 * cm])
  info_table.setStyle(TableStyle([
      ("BACKGROUND", (0, 0), (-1, -1), light_bg),
      ("BOX", (0, 0), (-1, -1), 0.5, border_gray),
      ("INNERGRID", (0, 0), (-1, -1), 0.4, border_gray),
      ("PADDING", (0, 0), (-1, -1), 3.5),
      ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
  ]))
  story.append(info_table)
  story.append(Spacer(1, 7.0))

  story.append(Paragraph("PROCEDIMENTOS PREVISTOS", section_heading))
  proc_data = [
      [Paragraph("Código", table_header), Paragraph("Procedimento", table_header)],
      [Paragraph("89999901", table_cell), Paragraph("Taxa de procedimento robótico " + ("particular" if is_particular else "híbrido"), table_cell)],
  ]
  proc_table = Table(proc_data, colWidths=[2.8 * cm, 16.0 * cm])
  proc_table.setStyle(TableStyle([
      ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
      ("BOX", (0, 0), (-1, -1), 0.5, border_gray),
      ("INNERGRID", (0, 0), (-1, -1), 0.4, border_gray),
      ("PADDING", (0, 0), (-1, -1), 3.0),
  ]))
  story.append(proc_table)
  story.append(Spacer(1, 7.0))

  story.append(Paragraph("VALORES DE RESPONSABILIDADE DO PACIENTE", section_heading))
  v_hosp = dados["valor_hospital"]
  v_anest = dados["valor_anestesia"]
  v_mat = dados["valor_materiais"]
  v_total = v_hosp + v_anest + v_mat

  val_data = [
      [Paragraph("Descrição", table_header), Paragraph("Valor", table_header)],
      [Paragraph("Taxa de Procedimento (Hospital)", table_cell), Paragraph(fmt_m(v_hosp), table_cell)],
      [Paragraph("Honorários Anestésicos", table_cell), Paragraph(fmt_m(v_anest), table_cell)],
      [Paragraph("Pinças e Materiais Robóticos", table_cell), Paragraph(fmt_m(v_mat), table_cell)],
      [Paragraph("Refeição Paciente e Acompanhante*", table_cell), Paragraph("Cortesia", table_cell_green)],
      [Paragraph("Subtotal", table_cell_bold), Paragraph(fmt_m(v_total), table_cell_bold)],
      [Paragraph("Desconto", table_cell), Paragraph("R$ 0,00", table_cell)],
      [Paragraph("Total a pagar pelo paciente", ParagraphStyle("THB", parent=table_header, fontSize=6.8)), Paragraph(fmt_m(v_total), ParagraphStyle("TVB", parent=table_cell_bold, fontSize=7.5, textColor=unimed_green))],
  ]
  val_table = Table(val_data, colWidths=[14.0 * cm, 4.8 * cm])
  val_table.setStyle(TableStyle([
      ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
      ("BOX", (0, 0), (-1, -1), 0.5, border_gray),
      ("INNERGRID", (0, 0), (-1, -1), 0.4, border_gray),
      ("PADDING", (0, 0), (-1, -1), 3.0),
      ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#ECFDF5")),
  ]))
  story.append(val_table)
  story.append(Spacer(1, 8.0))

  story.append(Paragraph("01 - ITENS NÃO INCLUSOS NESTE ORÇAMENTO", section_heading))
  if is_particular:
    no_inc_text = (
        "• <b>Honorários Médicos da Equipe Cirúrgica (Modalidade Particular):</b> Os honorários da equipe cirúrgica não possuem cobertura deste orçamento hospitalar e deverão ser acertados e pagos pelo paciente diretamente à equipe cirúrgica. <i>(Nota: Os honorários anestésicos já estão devidamente inclusos neste orçamento).</i><br/>"
        "• Exames laboratoriais e de imagem/diagnóstico;<br/>• Sessões de fonoaudiologia e fisioterapia;<br/>• Sangue e hemoderivados."
    )
  else:
    no_inc_text = (
        "• <b>Honorários Médicos da Equipe Cirúrgica (Modalidade Híbrida):</b> O plano de saúde cobrirá a parcela do honorário médico correspondente ao procedimento na modalidade convencional. A diferença relativa ao ato cirúrgico robótico não possui cobertura do plano e deverá ser negociada e paga pelo paciente diretamente à equipe cirúrgica. <i>(Nota: Os honorários anestésicos já estão devidamente inclusos neste orçamento).</i><br/>"
        "• Exames laboratoriais e de imagem/diagnóstico;<br/>• Sessões de fonoaudiologia e fisioterapia;<br/>• Sangue e hemoderivados."
    )

  no_inc_box = Table([[Paragraph(no_inc_text, normal_text)]], colWidths=[18.8 * cm])
  no_inc_box.setStyle(TableStyle([
      ("BACKGROUND", (0, 0), (-1, -1), red_box_bg),
      ("BOX", (0, 0), (-1, -1), 0.5, red_box_border),
      ("PADDING", (0, 0), (-1, -1), 3.5),
  ]))
  story.append(no_inc_box)
  story.append(Spacer(1, 7.0))

  story.append(Paragraph("02 - CONDIÇÕES DE PAGAMENTO", section_heading))
  notice_text = "<b>Atenção:</b> A confirmação do agendamento somente se dará mediante o pagamento antecipado em até 48h úteis à realização do procedimento."
  notice_box = Table([[Paragraph(notice_text, normal_text)]], colWidths=[18.8 * cm])
  notice_box.setStyle(TableStyle([
      ("BACKGROUND", (0, 0), (-1, -1), yellow_box_bg),
      ("BOX", (0, 0), (-1, -1), 0.5, yellow_box_border),
      ("PADDING", (0, 0), (-1, -1), 3.2),
  ]))
  story.append(notice_box)
  story.append(Spacer(1, 7.0))

  pag_data = [
      [
          Paragraph("<b>PAGAMENTO À VISTA</b><br/>Dinheiro em espécie, cartão de débito ou PIX.<br/><b>Chave PIX (CNPJ):</b> 77.858.611/0014-14<br/><b>Favorecido:</b> Unimed Grande Florianópolis Cooperativa de Trabalho Médico", normal_text),
          Paragraph("<b>TRANSFERÊNCIA BANCÁRIA</b><br/><b>Favorecido:</b> Unimed Grande Florianópolis Cooperativa de Trabalho Médico<br/><b>CNPJ:</b> 77.858.611/0014-14<br/><b>Banco Unicred (136)</b> | <b>Agência:</b> 1214-9 | <b>Conta:</b> 102646-1", normal_text),
      ],
      [
          Paragraph("<b>CARTÃO DE CRÉDITO</b><br/>• Até R$ 10.000,00: Parcelamento em até 6x sem juros.<br/>• Acima de R$ 10.000,00: Parcelamento em até 10x sem juros.<br/><i>Solicite o link informando nº do orçamento, bandeira e parcelas.</i><br/><b>Contato:</b> +55 48 9181-5582 | concierge@unimedflorianopolis.com.br", normal_text),
          Paragraph("<b>ENVIO DO COMPROVANTE / LINK DE PAGAMENTO</b><br/><b>WhatsApp:</b> +55 48 9181-5582<br/><b>E-mail:</b> concierge@unimedflorianopolis.com.br", normal_text),
      ],
  ]
  pag_table = Table(pag_data, colWidths=[9.4 * cm, 9.4 * cm])
  pag_table.setStyle(TableStyle([
      ("BACKGROUND", (0, 0), (-1, -1), light_bg),
      ("BOX", (0, 0), (-1, -1), 0.5, border_gray),
      ("INNERGRID", (0, 0), (-1, -1), 0.4, border_gray),
      ("PADDING", (0, 0), (-1, -1), 3.5),
      ("VALIGN", (0, 0), (-1, -1), "TOP"),
  ]))
  story.append(pag_table)
  story.append(Spacer(1, 7.0))

  story.append(Paragraph("03 - INFORMAÇÕES ADICIONAIS SOBRE O ORÇAMENTO", section_heading))
  info_add = (
      "• <b>Adicionais:</b> Demais materiais, medicamentos, gases medicinais e OPME's (órteses, próteses e materiais especiais) que não compõem o orçamento, serão acrescentados ao valor final quando utilizados.<br/>"
      "• <b>Variações e Excedentes:</b> Os valores contidos nesta previsão de gastos não são absolutos. Caso haja utilização de tempo excedente de sala cirúrgica ou período de permanência hospitalar superior ao estipulado, ocorrerão alterações de valores.<br/>"
      "• <b>Reoperações, Complicações e Intercorrências:</b> Em casos de reoperações, complicações, intercorrências ou diárias além das previstas, os valores extras serão cobrados posteriormente no momento da alta.<br/>"
      "• <b>Devoluções de OPME:</b> Em casos de necessidade de devolução de valores referentes a OPME não utilizado durante a cirurgia, o processo será concluído em até 10 dias úteis após a alta do paciente."
  )
  story.append(Paragraph(info_add, normal_text))
  story.append(Spacer(1, 7.0))

  story.append(Paragraph("SERVIÇO EXCLUSIVO DE CONCIERGE ROBÓTICA", section_heading))
  concierge_text = (
      "Para proporcionar uma experiência segura, ágil e acolhedora, os pacientes de cirurgia robótica contam com o nosso serviço exclusivo de Concierge. Estamos à disposição para auxiliar em todas as etapas do seu procedimento. Em caso de dúvidas, entre em contato diretamente pelo WhatsApp: <b>+55 48 9181-5582</b> ou pelo e-mail <b>concierge@unimedflorianopolis.com.br</b>"
  )
  qr_drawing = create_qr_code("https://wa.me/554891815582", size=40)
  qr_cell = [
      qr_drawing,
      Spacer(1, 1),
      Paragraph("Aponte a câmera<br/>para o WhatsApp", qr_label_style),
  ]

  concierge_table = Table([[Paragraph(concierge_text, concierge_style), qr_cell]], colWidths=[15.2 * cm, 3.6 * cm])
  concierge_table.setStyle(TableStyle([
      ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#ECFDF5")),
      ("BOX", (0, 0), (-1, -1), 0.5, unimed_green),
      ("PADDING", (0, 0), (-1, -1), 3.8),
      ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
      ("ALIGN", (1, 0), (1, 0), "CENTER"),
  ]))
  story.append(concierge_table)
  story.append(Spacer(1, 8.0))

  dt_str = dados["data_solicitacao"]
  orc_n = dados["numero_orcamento"]
  val_d = dados["data_validade"]

  signature_data = [[
      Table(
          [[
              Paragraph(f"<b>ORÇAMENTO Nº {orc_n}</b>", ParagraphStyle("V1", parent=normal_bold, textColor=unimed_green, fontSize=7.0)),
              Paragraph(f"<b>VÁLIDO ATÉ {val_d}</b>", ParagraphStyle("V2", parent=normal_bold, textColor=dark_gray, fontSize=7.0)),
          ]],
          colWidths=[4.2 * cm, 4.2 * cm],
          style=[
              ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F4F6")),
              ("BOX", (0, 0), (-1, -1), 0.5, border_gray),
              ("ALIGN", (0, 0), (0, 0), "LEFT"),
              ("ALIGN", (1, 0), (1, 0), "RIGHT"),
              ("PADDING", (0, 0), (-1, -1), 2.8),
          ],
      ),
      Paragraph(f"<para align='right'>São José, {dt_str}<br/><br/>________________________________________<br/><b>Responsável Paciente</b></para>", normal_text),
  ]]
  signature_table = Table(signature_data, colWidths=[9.4 * cm, 9.4 * cm])
  signature_table.setStyle(TableStyle([
      ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
      ("PADDING", (0, 0), (-1, -1), 0),
  ]))
  story.append(signature_table)

  class CustomNumberedCanvas(NumberedCanvas):
    orc_number = str(dados["numero_orcamento"])

  doc.build(story, canvasmaker=CustomNumberedCanvas)
  pdf_value = buffer.getvalue()
  buffer.close()
  return pdf_value


# --- EXTRAÇÃO INTELIGENTE VIA GEMINI API (MODELO OFICIAL GEMINI-3.8-FLASH) ---
def extrair_dados_tasy(texto_ou_pdf, api_key):
  client = genai.Client(api_key=api_key)
  prompt = """
    Você é um extrator especialista de orçamentos hospitalares do TASY.
    Analise o documento e retorne ESTRITAMENTE um JSON com as seguintes chaves:
    {
      "paciente": "Nome completo do paciente",
      "medico": "Nome do médico responsável (incluindo Dr. e CRM se houver)",
      "numero_orcamento": "Número do orçamento",
      "tipo_orcamento": "HÍBRIDO ou PARTICULAR (identifique se é pacote híbrido ou 100% particular)",
      "data_solicitacao": "Data da solicitação no formato DD/MM/AAAA",
      "tempo_cc": "Tempo de centro cirúrgico ex: '180 Min CC'",
      "acomodacao": "Tipo de acomodação (ex: Apartamento)",
      "valor_hospital": 0.00 (float da taxa hospitalar),
      "valor_anestesia": 0.00 (float da taxa/honorário anestésico),
      "valor_materiais": 0.00 (float da soma de todas as pinças/materiais/drapes robóticos)
    }
    Retorne APENAS o JSON puro.
    """

  # Modelo oficial do Gemini configurado
  model_name = "gemini-3.8-flash"
  response = None
  ultimo_erro = None

  for tentativa in range(3):
    try:
      response = client.models.generate_content(
          model=model_name,
          contents=[prompt, texto_ou_pdf],
          config=types.GenerateContentConfig(response_mime_type="application/json"),
      )
      if response and hasattr(response, "text") and response.text:
        break
    except Exception as e:
      ultimo_erro = e
      err_str = str(e).upper()
      if "503" in err_str or "UNAVAILABLE" in err_str or "RESOURCE_EXHAUSTED" in err_str or "429" in err_str:
        time.sleep(2)
        continue
      else:
        break

  if not response or not hasattr(response, "text") or not response.text:
    raise Exception(f"Servidores do Google ocupados. Por favor, tente novamente em instantes. ({str(ultimo_erro)})")

  data = json.loads(response.text)

  try:
    dt_sol = datetime.strptime(data["data_solicitacao"], "%d/%m/%Y")
  except Exception:
    dt_sol = datetime.now()
    data["data_solicitacao"] = dt_sol.strftime("%d/%m/%Y")

  dt_val = dt_sol + timedelta(days=30)
  data["data_validade"] = dt_val.strftime("%d/%m/%Y")

  return data


# --- ESTRUTURA DE ABAS STREAMLIT ---
if os.path.exists("unimed_logo_official.png"):
  st.image("unimed_logo_official.png", width=220)

st.title("Gerador e Gestor de Orçamentos de Cirurgia Robótica")
st.caption("Hospital Unimed Grande Florianópolis · Programa de Cirurgia Robótica")

tab1, tab2 = st.tabs(["📄 Gerar Orçamento", "📊 Painel de Acompanhamento e Conversão"])

# --- ABA 1: GERAR ORÇAMENTO ---
with tab1:
  st.markdown("---")
  try:
    api_key = st.secrets["GEMINI_API_KEY"]
  except Exception:
    api_key = ""

  if not api_key:
    st.error("⚠️ Chave GEMINI_API_KEY não encontrada nos Secrets do Streamlit.")

  uploaded_file = st.file_uploader("📄 Envie o PDF do Orçamento Padrão do TASY:", type=["pdf"])

  if uploaded_file and api_key:
    if st.button("🚀 Processar e Gerar Orçamento Oficial", type="primary"):
      with st.spinner("Lendo documento TASY e salvando no histórico..."):
        try:
          reader = pypdf.PdfReader(uploaded_file)
          texto_tasy = ""
          for page in reader.pages:
            texto_tasy += page.extract_text() + "\n"

          dados = extrair_dados_tasy(texto_tasy, api_key)
          v_tot = dados["valor_hospital"] + dados["valor_anestesia"] + dados["valor_materiais"]

          salvar_orcamento_db(dados, v_tot)

          st.success("Orçamento processado e gravado no Histórico!")
          st.subheader("📋 Resumo dos Dados Processados")

          col1, col2 = st.columns(2)
          with col1:
            st.write(f"**Paciente:** {dados['paciente']}")
            st.write(f"**Médico:** {dados['medico']}")
            st.write(f"**Orçamento Nº:** {dados['numero_orcamento']}")
            st.write(f"**Acomodação:** {dados['acomodacao']}")
          with col2:
            st.write(f"**Data Solicitação:** {dados['data_solicitacao']}")
            st.write(f"**Validade (30 dias):** :green[**{dados['data_validade']}**]")
            st.write(f"**Tempo CC:** {dados['tempo_cc']}")
            st.write(f"**Tipo:** {dados.get('tipo_orcamento', 'HÍBRIDO')}")

          st.markdown(f"### **Valor Total Paciente:** :green[{fmt_m(v_tot)}]")

          pdf_bytes = gerar_pdf_bytes(dados)

          st.download_button(
              label="📥 Baixar Orçamento Oficial em PDF",
              data=pdf_bytes,
              file_name=f"Orcamento_{dados['numero_orcamento']}_{dados['paciente'].replace(' ', '_')}.pdf",
              mime="application/pdf",
          )

        except Exception as e:
          st.error(f"Erro ao processar orçamento: {str(e)}")

# --- ABA 2: PAINEL DE ACOMPANHAMENTO ---
with tab2:
  st.subheader("📊 Painel de Controle e Histórico de Conversão")
  
  df = carregar_orcamentos_df()

  if df.empty:
    st.info("Nenhum orçamento foi gerado até o momento. Gerando orçamentos na Aba 1, eles aparecerão aqui automaticamente!")
  else:
    total_emitidos = len(df)
    convertidos = len(df[df["status"] == "Convertido / Fechado"])
    pendentes = len(df[df["status"] == "Pendente"])
    taxa_conversao = (convertidos / total_emitidos * 100) if total_emitidos > 0 else 0.0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total de Orçamentos", total_emitidos)
    m2.metric("Convertidos / Fechados", convertidos)
    m3.metric("Pendentes", pendentes)
    m4.metric("Taxa de Conversão", f"{taxa_conversao:.1f}%")

    st.markdown("---")

    st.subheader("🔔 Gestão de Alertas (48 Horas)")
    
    agora = datetime.now()
    pendentes_48h = []

    for _, row in df.iterrows():
      if row["status"] == "Pendente":
        try:
          dt_cria = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
          if agora - dt_cria >= timedelta(hours=48):
            pendentes_48h.append({
                "numero_orcamento": row["numero_orcamento"],
                "paciente": row["paciente"],
                "medico": row["medico"],
                "tipo_orcamento": row["tipo_orcamento"],
                "data_solicitacao": row["data_solicitacao"],
                "valor_total": row["valor_total"]
            })
        except Exception:
          pass

    if pendentes_48h:
      st.warning(f"⚠️ Existem **{len(pendentes_48h)} orçamento(s)** emitido(s) há mais de 48 horas e ainda pendente(s)!")
      if st.button("📧 Enviar Alerta de 48h por E-mail para Comercial & Particular"):
        sucesso, msg_retorno = enviar_email_alerta(pendentes_48h)
        if sucesso:
          st.success(msg_retorno)
        else:
          st.error(msg_retorno)
    else:
      st.success("✅ Nenhum orçamento pendente com mais de 48 horas de emissão.")

    st.markdown("---")
    st.subheader("📋 Tabela Interativa de Acompanhamento")
    st.caption("Altere o status de cada orçamento na caixa de seleção abaixo:")

    for idx, row in df.iterrows():
      with st.expander(f"📌 Orçamento Nº {row['numero_orcamento']} - {row['paciente']} | Status Atual: {row['status']}"):
        c1, c2, c3 = st.columns([2, 2, 2])
        with c1:
          st.write(f"**Médico:** {row['medico']}")
          st.write(f"**Modalidade:** {row['tipo_orcamento']}")
        with c2:
          st.write(f"**Data Solicitação:** {row['data_solicitacao']}")
          st.write(f"**Valor Total:** R$ {row['valor_total']:,.2f}")
        with c3:
          opcoes_status = ["Pendente", "Convertido / Fechado", "Não Convertido / Cancelado"]
          idx_atual = opcoes_status.index(row["status"]) if row["status"] in opcoes_status else 0
          
          novo_st = st.selectbox(
              f"Alterar Status ({row['numero_orcamento']}):",
              opcoes_status,
              index=idx_atual,
              key=f"status_{row['numero_orcamento']}"
          )
          
          if novo_st != row["status"]:
            atualizar_status_db(row["numero_orcamento"], novo_st)
            st.success(f"Status do Orçamento {row['numero_orcamento']} atualizado para '{novo_st}'!")
            st.rerun()
