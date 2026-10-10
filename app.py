import io
import json
import os
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
from google import genai
from google.genai import types

# --- CONFIGURAÇÃO DA PÁGINA STREAMLIT ---
st.set_page_config(
    page_title="Gerador de Orçamentos Robótica - HUGF",
    page_icon="🏥",
    layout="centered",
)


# --- CLASSE PARA CABEÇALHO E RODAPÉ DINÂMICOS NO REPORTLAB ---
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
    margin = 1.2 * cm

    unimed_green = colors.HexColor("#008C50")
    dark_gray = colors.HexColor("#1F2937")
    light_gray = colors.HexColor("#6B7280")
    border_gray = colors.HexColor("#E5E7EB")

    # Logomarca do Cabeçalho
    logo_path = "unimed_logo_official.png"
    if os.path.exists(logo_path):
      logo_w = 3.8 * cm
      logo_h = logo_w / (1560 / 646)
      self.drawImage(
          logo_path,
          margin,
          page_height - 1.7 * cm,
          width=logo_w,
          height=logo_h,
          mask="auto",
      )
      self.setFont("Helvetica-Bold", 9.5)
      self.setFillColor(dark_gray)
      self.drawString(
          margin + logo_w + 0.25 * cm,
          page_height - 1.15 * cm,
          "HOSPITAL UNIMED GRANDE FLORIANÓPOLIS",
      )
      self.setFont("Helvetica-Bold", 7.8)
      self.setFillColor(unimed_green)
      self.drawString(
          margin + logo_w + 0.25 * cm,
          page_height - 1.48 * cm,
          "PROGRAMA DE CIRURGIA ROBÓTICA",
      )
    else:
      self.setFont("Helvetica-Bold", 11)
      self.setFillColor(dark_gray)
      self.drawString(
          margin,
          page_height - 1.2 * cm,
          "HOSPITAL UNIMED GRANDE FLORIANÓPOLIS",
      )

    # Número do Orçamento à Direita
    orc_num = getattr(self, "orc_number", "")
    self.setFont("Helvetica-Bold", 7.5)
    self.setFillColor(light_gray)
    self.drawRightString(
        page_width - margin, page_height - 0.95 * cm, "ORÇAMENTO Nº"
    )
    self.setFont("Helvetica-Bold", 15)
    self.setFillColor(unimed_green)
    self.drawRightString(
        page_width - margin, page_height - 1.50 * cm, orc_num
    )

    self.setStrokeColor(border_gray)
    self.setLineWidth(0.8)
    self.line(
        margin,
        page_height - 1.85 * cm,
        page_width - margin,
        page_height - 1.85 * cm,
    )

    # Rodapé - Linha Divisória
    footer_line_y = 1.25 * cm
    self.setStrokeColor(border_gray)
    self.setLineWidth(0.8)
    self.line(margin, footer_line_y, page_width - margin, footer_line_y)

    # Rodapé - QR Code
    qr_url = "https://wa.me/554891815582"
    qr_obj = qr.QrCodeWidget(qr_url)
    bounds = qr_obj.getBounds()
    qr_w = bounds[2] - bounds[0]
    qr_h = bounds[3] - bounds[1]
    qr_size = 22
    d_qr = Drawing(
        qr_size,
        qr_size,
        transform=[qr_size / qr_w, 0, 0, qr_size / qr_h, 0, 0],
    )
    d_qr.add(qr_obj)
    d_qr.drawOn(self, margin, 0.25 * cm)

    # Rodapé - Texto de Contato Esquerda
    text_x = margin + qr_size + 5
    self.setFont("Helvetica-Bold", 7.0)
    self.setFillColor(dark_gray)
    self.drawString(
        text_x, 0.85 * cm, "Hospital Unimed Grande Florianópolis"
    )

    self.setFont("Helvetica", 7.0)
    self.setFillColor(light_gray)
    self.drawString(
        text_x, 0.50 * cm, "+55 48 9181-5582 Concierge Robótica HUGF"
    )

    # Rodapé - Selo ONA (Centralizado no Meio)
    ona_path = None
    for filename in [
        "selo_ona.jpg",
        "selo_ona.png",
        "selo_ona.jpeg",
        "Selo ONA Nível 3 HUGF (2).jpg",
    ]:
      if os.path.exists(filename):
        ona_path = filename
        break

    if ona_path:
      ona_w = 2.4 * cm
      ona_h = 1.2 * cm
      ona_x = (page_width - ona_w) / 2.0
      ona_y = 0.05 * cm
      self.drawImage(
          ona_path,
          ona_x,
          ona_y,
          width=ona_w,
          height=ona_h,
          mask="auto",
      )

    # Rodapé - Número da Página Direita
    page_str = (
        f"Orçamento {orc_num}  |  Página {self._pageNumber} de {page_count}"
    )
    self.setFont("Helvetica", 7.0)
    self.setFillColor(light_gray)
    self.drawRightString(page_width - margin, 0.65 * cm, page_str)
    self.restoreState()


# --- FUNÇÕES AUXILIARES ---
def create_tag(text="HÍBRIDO (PACOTE)", is_particular=False):
  bg_color = (
      colors.HexColor("#DC2626") if is_particular else colors.HexColor("#008C50")
  )
  w = len(text) * 5.8 + 14
  h = 14
  d = Drawing(w, h)
  d.add(Rect(0, 0, w, h, rx=7, ry=7, fillColor=bg_color, strokeColor=None))
  d.add(
      String(
          w / 2.0,
          3.5,
          text,
          textAnchor="middle",
          fontName="Helvetica-Bold",
          fontSize=7.5,
          fillColor=colors.white,
      )
  )
  return d


def create_qr_code(url="https://wa.me/554891815582", size=36):
  qr_code = qr.QrCodeWidget(url)
  bounds = qr_code.getBounds()
  w = bounds[2] - bounds[0]
  h = bounds[3] - bounds[1]
  d = Drawing(size, size, transform=[size / w, 0, 0, size / h, 0, 0])
  d.add(qr_code)
  return d


def fmt_m(val):
  return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# --- MOTOR DE GERAÇÃO DO PDF REPORTLAB (PÁGINA ÚNICA) ---
def gerar_pdf_bytes(dados):
  buffer = io.BytesIO()
  doc = SimpleDocTemplate(
      buffer,
      pagesize=A4,
      leftMargin=1.2 * cm,
      rightMargin=1.2 * cm,
      topMargin=2.0 * cm,
      bottomMargin=1.3 * cm,
  )

  unimed_green = colors.HexColor("#008C50")
  dark_gray = colors.HexColor("#1F2937")
  light_bg = colors.HexColor("#F9FAFB")
  border_gray = colors.HexColor("#E5E7EB")
  red_box_bg = colors.HexColor("#FEF2F2")
  red_box_border = colors.HexColor("#FCA5A5")
  yellow_box_bg = colors.HexColor("#FEFCE8")
  yellow_box_border = colors.HexColor("#FDE047")

  title_style = ParagraphStyle(
      "DocTitle",
      fontName="Helvetica-Bold",
      fontSize=11.0,
      leading=13.0,
      textColor=dark_gray,
      spaceAfter=1,
  )
  subtitle_style = ParagraphStyle(
      "DocSubtitle",
      fontName="Helvetica-Bold",
      fontSize=7.0,
      leading=8.5,
      textColor=unimed_green,
      spaceAfter=4,
  )
  section_heading = ParagraphStyle(
      "SectionHeading",
      fontName="Helvetica-Bold",
      fontSize=7.8,
      leading=9.5,
      textColor=unimed_green,
      spaceBefore=3,
      spaceAfter=1.5,
  )
  normal_text = ParagraphStyle(
      "NormalText",
      fontName="Helvetica",
      fontSize=6.4,
      leading=8.0,
      textColor=dark_gray,
  )
  normal_bold = ParagraphStyle(
      "NormalBold",
      fontName="Helvetica-Bold",
      fontSize=6.4,
      leading=8.0,
      textColor=dark_gray,
  )
  table_header = ParagraphStyle(
      "TableHeader",
      fontName="Helvetica-Bold",
      fontSize=6.4,
      leading=7.8,
      textColor=dark_gray,
  )
  table_cell = ParagraphStyle(
      "TableCell",
      fontName="Helvetica",
      fontSize=6.4,
      leading=7.8,
      textColor=dark_gray,
  )
  table_cell_bold = ParagraphStyle(
      "TableCellBold",
      fontName="Helvetica-Bold",
      fontSize=6.4,
      leading=7.8,
      textColor=dark_gray,
  )
  table_cell_green = ParagraphStyle(
      "TableCellGreen",
      fontName="Helvetica-Bold",
      fontSize=6.4,
      leading=7.8,
      textColor=unimed_green,
  )
  note_style = ParagraphStyle(
      "NoteText",
      fontName="Helvetica-Oblique",
      fontSize=6.0,
      leading=7.5,
      textColor=colors.HexColor("#4B5563"),
      spaceAfter=1,
  )
  concierge_style = ParagraphStyle(
      "ConciergeBox",
      fontName="Helvetica",
      fontSize=6.4,
      leading=8.5,
      textColor=dark_gray,
  )
  qr_label_style = ParagraphStyle(
      "QRLabel",
      fontName="Helvetica-Bold",
      fontSize=5.5,
      leading=6.5,
      textColor=unimed_green,
      alignment=1,
  )

  story = []

  # Verificação se o orçamento é Híbrido ou Particular
  tipo_str = str(dados.get("tipo_orcamento", "")).strip().upper()
  is_particular = "PARTICULAR" in tipo_str or "TOTAL" in tipo_str

  if is_particular:
    tag_label = "PARTICULAR (PACOTE)"
    convenio_label = "Particular"
  else:
    tag_label = "HÍBRIDO (PACOTE)"
    convenio_label = "Unimed/Híbrido"

  tag_obj = create_tag(tag_label, is_particular=is_particular)

  # TITULO
  story.append(Paragraph("Orçamento cirúrgico", title_style))
  story.append(
      Paragraph(
          "ATENDIMENTO PARTICULAR · PACOTE · CIRURGIA ROBÓTICA", subtitle_style
      )
  )

  # DADOS DO PACIENTE
  info_data = [
      [
          Paragraph(f"<b>PACIENTE</b><br/>{dados['paciente']}", normal_text),
          Paragraph(
              f"<b>MÉDICO RESPONSÁVEL</b><br/>{dados['medico']}", normal_text
          ),
      ],
      [
          Paragraph(
              f"<b>DATA DA SOLICITAÇÃO</b><br/>{dados['data_solicitacao']}",
              normal_text,
          ),
          Paragraph(
              f"<b>VALIDADE DO ORÇAMENTO</b><br/>{dados['data_validade']} · 30"
              " dias",
              normal_text,
          ),
      ],
      [
          Paragraph(
              f"<b>TEMPO PREVISTO DE CIRURGIA</b><br/>{dados['tempo_cc']}",
              normal_text,
          ),
          Paragraph(
              f"<b>ACOMODAÇÃO</b><br/>{dados['acomodacao']}", normal_text
          ),
      ],
      [
          [
              Paragraph("<b>TIPO DE ORÇAMENTO</b>", normal_text),
              Spacer(1, 1),
              tag_obj,
          ],
          Paragraph(
              f"<b>CONVÊNIO / MODALIDADE</b><br/>{convenio_label}", normal_text
          ),
      ],
  ]

  info_table = Table(info_data, colWidths=[9.3 * cm, 9.3 * cm])
  info_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, -1), light_bg),
          ("BOX", (0, 0), (-1, -1), 0.5, border_gray),
          ("INNERGRID", (0, 0), (-1, -1), 0.4, border_gray),
          ("PADDING", (0, 0), (-1, -1), 2.8),
          ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
      ])
  )
  story.append(info_table)
  story.append(Spacer(1, 3))

  # PROCEDIMENTOS
  story.append(Paragraph("PROCEDIMENTOS PREVISTOS", section_heading))
  proc_data = [
      [Paragraph("Código", table_header), Paragraph("Procedimento", table_header)],
      [
          Paragraph("89999901", table_cell),
          Paragraph(
              "Taxa de procedimento robótico "
              + ("particular" if is_particular else "híbrido"),
              table_cell,
          ),
      ],
  ]
  proc_table = Table(proc_data, colWidths=[3.0 * cm, 15.6 * cm])
  proc_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
          ("BOX", (0, 0), (-1, -1), 0.5, border_gray),
          ("INNERGRID", (0, 0), (-1, -1), 0.4, border_gray),
          ("PADDING", (0, 0), (-1, -1), 2.2),
      ])
  )
  story.append(proc_table)
  story.append(Spacer(1, 3))

  # VALORES
  story.append(
      Paragraph("VALORES DE RESPONSABILIDADE DO PACIENTE", section_heading)
  )

  v_hosp = dados["valor_hospital"]
  v_anest = dados["valor_anestesia"]
  v_mat = dados["valor_materiais"]
  v_total = v_hosp + v_anest + v_mat

  val_data = [
      [Paragraph("Descrição", table_header), Paragraph("Valor", table_header)],
      [
          Paragraph("Taxa de Procedimento (Hospital)", table_cell),
          Paragraph(fmt_m(v_hosp), table_cell),
      ],
      [
          Paragraph("Honorários Anestésicos", table_cell),
          Paragraph(fmt_m(v_anest), table_cell),
      ],
      [
          Paragraph("Pinças e Materiais Robóticos", table_cell),
          Paragraph(fmt_m(v_mat), table_cell),
      ],
      [
          Paragraph("Refeição Paciente e Acompanhante*", table_cell),
          Paragraph("Cortesia", table_cell_green),
      ],
      [
          Paragraph("Subtotal", table_cell_bold),
          Paragraph(fmt_m(v_total), table_cell_bold),
      ],
      [
          Paragraph("Desconto", table_cell),
          Paragraph("R$ 0,00", table_cell),
      ],
      [
          Paragraph(
              "Total a pagar pelo paciente",
              ParagraphStyle("THeaderBig", parent=table_header, fontSize=7.0),
          ),
          Paragraph(
              fmt_m(v_total),
              ParagraphStyle(
                  "TValBig",
                  parent=table_cell_bold,
                  fontSize=7.8,
                  textColor=unimed_green,
              ),
          ),
      ],
  ]
  val_table = Table(val_data, colWidths=[13.6 * cm, 5.0 * cm])
  val_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
          ("BOX", (0, 0), (-1, -1), 0.5, border_gray),
          ("INNERGRID", (0, 0), (-1, -1), 0.4, border_gray),
          ("PADDING", (0, 0), (-1, -1), 2.5),
          ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#ECFDF5")),
      ])
  )
  story.append(val_table)
  story.append(Spacer(1, 3))

  # CONDIÇÕES E ORIENTAÇÕES
  story.append(
      Paragraph("CONDIÇÕES E ORIENTAÇÕES DO ORÇAMENTO", section_heading)
  )

  if is_particular:
    no_inc_text = (
        "• <b>Honorários Médicos da Equipe Cirúrgica (Modalidade"
        " Particular):</b> Os honorários médicos da equipe cirúrgica não estão"
        " inclusos neste orçamento hospitalar e deverão ser acertados"
        " diretamente com a equipe do médico responsável. <i>(Nota: Os"
        " honorários anestésicos já estão devidamente inclusos neste"
        " orçamento).</i><br/>• Exames de imagem/diagnóstico; Sessões de"
        " fonoaudiologia/fisioterapia; Sangue e hemoderivados."
    )
  else:
    no_inc_text = (
        "• <b>Honorários Médicos da Equipe Cirúrgica (Modalidade Híbrida):</b> O"
        " plano de saúde cobrirá o ato cirúrgico convencional. A diferença"
        " robótica não possui cobertura do plano e deve ser negociada"
        " diretamente com a equipe cirúrgica. <i>(Nota: Os honorários"
        " anestésicos já estão inclusos).</i><br/>• Exames de"
        " imagem/diagnóstico; Sessões de fonoaudiologia/fisioterapia; Sangue e"
        " hemoderivados."
    )

  no_inc_box = Table(
      [[Paragraph(no_inc_text, normal_text)]], colWidths=[18.6 * cm]
  )
  no_inc_box.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, -1), red_box_bg),
          ("BOX", (0, 0), (-1, -1), 0.5, red_box_border),
          ("PADDING", (0, 0), (-1, -1), 2.8),
      ])
  )
  story.append(no_inc_box)
  story.append(Spacer(1, 2))

  notice_text = (
      "<b>Atenção:</b> A confirmação do agendamento somente se dará mediante o"
      " pagamento antecipado em até 48h úteis à realização do procedimento."
  )
  notice_box = Table(
      [[Paragraph(notice_text, normal_text)]], colWidths=[18.6 * cm]
  )
  notice_box.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, -1), yellow_box_bg),
          ("BOX", (0, 0), (-1, -1), 0.5, yellow_box_border),
          ("PADDING", (0, 0), (-1, -1), 2.8),
      ])
  )
  story.append(notice_box)
  story.append(Spacer(1, 2))

  # PAGAMENTO
  pag_data = [
      [
          Paragraph(
              "<b>PAGAMENTO À VISTA / PIX</b><br/>Dinheiro, débito ou"
              " PIX.<br/><b>Chave PIX (CNPJ):</b>"
              " 77.858.611/0014-14<br/>Favorecido: Unimed Grande Florianópolis",
              normal_text,
          ),
          Paragraph(
              "<b>TRANSFERÊNCIA BANCÁRIA</b><br/>Unimed Grande"
              " Florianópolis<br/>CNPJ: 77.858.611/0014-14<br/>Banco Unicred"
              " (136) | Ag: 1214-9 | CC: 102646-1",
              normal_text,
          ),
      ],
      [
          Paragraph(
              "<b>CARTÃO DE CRÉDITO</b><br/>Até R$ 10.000,00: em até 6x sem"
              " juros.<br/>Acima de R$ 10.000,00: em até 10x sem"
              " juros.<br/>Contato: +55 48 9181-5582",
              normal_text,
          ),
          Paragraph(
              "<b>COMPROVANTE / LINK DE PAGAMENTO</b><br/>WhatsApp: +55 48"
              " 9181-5582<br/>E-mail: concierge@unimedflorianopolis.com.br",
              normal_text,
          ),
      ],
  ]
  pag_table = Table(pag_data, colWidths=[9.3 * cm, 9.3 * cm])
  pag_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, -1), light_bg),
          ("BOX", (0, 0), (-1, -1), 0.5, border_gray),
          ("INNERGRID", (0, 0), (-1, -1), 0.4, border_gray),
          ("PADDING", (0, 0), (-1, -1), 2.8),
          ("VALIGN", (0, 0), (-1, -1), "TOP"),
      ])
  )
  story.append(pag_table)
  story.append(Spacer(1, 3))

  # CONCIERGE
  concierge_text = (
      "<b>CONCIERGE ROBÓTICA HUGF:</b> Estamos à disposição para auxiliar em"
      " todas as etapas. Em caso de dúvidas, contate-nos via WhatsApp: <b>+55 48"
      " 9181-5582</b> ou pelo e-mail <b>concierge@unimedflorianopolis.com.br</b>"
  )
  qr_drawing = create_qr_code("https://wa.me/554891815582", size=36)
  qr_cell = [
      qr_drawing,
      Paragraph("WhatsApp Concierge", qr_label_style),
  ]
  concierge_table = Table(
      [[Paragraph(concierge_text, concierge_style), qr_cell]],
      colWidths=[15.2 * cm, 3.4 * cm],
  )
  concierge_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#ECFDF5")),
          ("BOX", (0, 0), (-1, -1), 0.5, unimed_green),
          ("PADDING", (0, 0), (-1, -1), 3),
          ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("ALIGN", (1, 0), (1, 0), "CENTER"),
      ])
  )
  story.append(concierge_table)
  story.append(Spacer(1, 4))

  # ASSINATURA E RODAPÉ DA FOLHA
  dt_str = dados["data_solicitacao"]
  orc_n = dados["numero_orcamento"]
  val_d = dados["data_validade"]

  signature_data = [[
      Table(
          [[
              Paragraph(
                  f"<b>ORÇAMENTO Nº {orc_n}</b>",
                  ParagraphStyle(
                      "V1",
                      parent=normal_bold,
                      textColor=unimed_green,
                      fontSize=7.2,
                  ),
              ),
              Paragraph(
                  f"<b>VÁLIDO ATÉ {val_d}</b>",
                  ParagraphStyle(
                      "V2",
                      parent=normal_bold,
                      textColor=dark_gray,
                      fontSize=7.2,
                  ),
              ),
          ]],
          colWidths=[4.2 * cm, 4.2 * cm],
          style=[
              ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F4F6")),
              ("BOX", (0, 0), (-1, -1), 0.5, border_gray),
              ("ALIGN", (0, 0), (0, 0), "LEFT"),
              ("ALIGN", (1, 0), (1, 0), "RIGHT"),
              ("PADDING", (0, 0), (-1, -1), 2.5),
          ],
      ),
      Paragraph(
          f"<para align='right'>São José, {dt_str}<br/><br/>________________________________________<br/><b>Responsável"
          " Paciente</b></para>",
          normal_text,
      ),
  ]]
  signature_table = Table(signature_data, colWidths=[9.3 * cm, 9.3 * cm])
  signature_table.setStyle(
      TableStyle([
          ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
          ("PADDING", (0, 0), (-1, -1), 0),
      ])
  )
  story.append(signature_table)

  class CustomNumberedCanvas(NumberedCanvas):
    orc_number = str(dados["numero_orcamento"])

  doc.build(story, canvasmaker=CustomNumberedCanvas)
  pdf_value = buffer.getvalue()
  buffer.close()
  return pdf_value


# --- EXTRAÇÃO INTELIGENTE DO TASY VIA GEMINI API ---
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

  response = client.models.generate_content(
      model="gemini-3.8-flash",
      contents=[prompt, texto_ou_pdf],
      config=types.GenerateContentConfig(response_mime_type="application/json"),
  )
  data = json.loads(response.text)

  # Cálculo da Validade de exatos 30 dias
  try:
    dt_sol = datetime.strptime(data["data_solicitacao"], "%d/%m/%Y")
  except Exception:
    dt_sol = datetime.now()
    data["data_solicitacao"] = dt_sol.strftime("%d/%m/%Y")

  dt_val = dt_sol + timedelta(days=30)
  data["data_validade"] = dt_val.strftime("%d/%m/%Y")

  return data


# --- INTERFACE GRÁFICA DO STREAMLIT ---
if os.path.exists("unimed_logo_official.png"):
  st.image("unimed_logo_official.png", width=220)

st.title("Gerador de Orçamentos de Cirurgia Robótica")
st.caption(
    "Hospital Unimed Grande Florianópolis · Programa de Cirurgia Robótica"
)

st.markdown("---")

api_key = st.text_input(
    "🔑 Insira sua API Key do Google Gemini:",
    type="password",
    help="Chave de acesso obtida no Google AI Studio",
)

uploaded_file = st.file_uploader(
    "📄 Envie o PDF do Orçamento Padrão do TASY:", type=["pdf"]
)

if uploaded_file and api_key:
  if st.button("🚀 Processar e Gerar Orçamento Oficial", type="primary"):
    with st.spinner("Lendo documento TASY e formatando no padrão da Robótica..."):
      try:
        reader = pypdf.PdfReader(uploaded_file)
        texto_tasy = ""
        for page in reader.pages:
          texto_tasy += page.extract_text() + "\n"

        dados = extrair_dados_tasy(texto_tasy, api_key)

        st.success("Orçamento processado com sucesso!")
        st.subheader("📋 Resumo dos Dados Processados")

        col1, col2 = st.columns(2)
        with col1:
          st.write(f"**Paciente:** {dados['paciente']}")
          st.write(f"**Médico:** {dados['medico']}")
          st.write(f"**Orçamento Nº:** {dados['numero_orcamento']}")
          st.write(f"**Acomodação:** {dados['acomodacao']}")
        with col2:
          st.write(f"**Data Solicitação:** {dados['data_solicitacao']}")
          st.write(
              f"**Validade (30 dias):** :green[**{dados['data_validade']}**]"
          )
          st.write(f"**Tempo CC:** {dados['tempo_cc']}")
          st.write(f"**Tipo:** {dados.get('tipo_orcamento', 'HÍBRIDO')}")

        v_tot = (
            dados["valor_hospital"]
            + dados["valor_anestesia"]
            + dados["valor_materiais"]
        )
        st.markdown(
            f"### **Valor Total Paciente:** :green[{fmt_m(v_tot)}]"
        )

        pdf_bytes = gerar_pdf_bytes(dados)

        st.download_button(
            label="📥 Baixar Orçamento Oficial em PDF (Página Única)",
            data=pdf_bytes,
            file_name=f"Orcamento_{dados['numero_orcamento']}_{dados['paciente'].replace(' ', '_')}.pdf",
            mime="application/pdf",
        )

      except Exception as e:
        st.error(f"Erro ao processar orçamento: {str(e)}")

elif uploaded_file and not api_key:
  st.warning("⚠️ Por favor, insira a sua API Key do Gemini para continuar.")
