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
    KeepTogether,
    PageBreak,
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
    margin = 1.5 * cm

    unimed_green = colors.HexColor("#008C50")
    dark_gray = colors.HexColor("#1F2937")
    light_gray = colors.HexColor("#6B7280")
    border_gray = colors.HexColor("#E5E7EB")

    # Logomarca do Cabeçalho
    logo_path = "unimed_logo_official.png"
    if os.path.exists(logo_path):
      logo_w = 4.0 * cm
      logo_h = logo_w / (1560 / 646)
      self.drawImage(
          logo_path,
          margin,
          page_height - 2.2 * cm,
          width=logo_w,
          height=logo_h,
          mask="auto",
      )
      self.setFont("Helvetica-Bold", 10)
      self.setFillColor(dark_gray)
      self.drawString(
          margin + logo_w + 0.3 * cm,
          page_height - 1.5 * cm,
          "HOSPITAL UNIMED GRANDE FLORIANÓPOLIS",
      )
      self.setFont("Helvetica-Bold", 8)
      self.setFillColor(unimed_green)
      self.drawString(
          margin + logo_w + 0.3 * cm,
          page_height - 1.85 * cm,
          "PROGRAMA DE CIRURGIA ROBÓTICA",
      )
    else:
      self.setFont("Helvetica-Bold", 12)
      self.setFillColor(dark_gray)
      self.drawString(
          margin,
          page_height - 1.5 * cm,
          "HOSPITAL UNIMED GRANDE FLORIANÓPOLIS",
      )

    # Selo ONA
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
      ona_w = 2.6 * cm
      ona_h = 1.38 * cm
      self.drawImage(
          ona_path,
          page_width - margin - 5.8 * cm,
          page_height - 2.2 * cm,
          width=ona_w,
          height=ona_h,
          mask="auto",
      )

    # Número do Orçamento à Direita
    orc_num = getattr(self, "orc_number", "")
    self.setFont("Helvetica-Bold", 8)
    self.setFillColor(light_gray)
    self.drawRightString(
        page_width - margin, page_height - 1.2 * cm, "ORÇAMENTO Nº"
    )
    self.setFont("Helvetica-Bold", 16)
    self.setFillColor(unimed_green)
    self.drawRightString(page_width - margin, page_height - 1.8 * cm, orc_num)

    self.setStrokeColor(border_gray)
    self.setLineWidth(0.8)
    self.line(
        margin,
        page_height - 2.4 * cm,
        page_width - margin,
        page_height - 2.4 * cm,
    )

    # Rodapé
    footer_line_y = 1.5 * cm
    self.setStrokeColor(border_gray)
    self.setLineWidth(0.8)
    self.line(margin, footer_line_y, page_width - margin, footer_line_y)

    qr_url = "https://wa.me/554891815582"
    qr_obj = qr.QrCodeWidget(qr_url)
    bounds = qr_obj.getBounds()
    qr_w = bounds[2] - bounds[0]
    qr_h = bounds[3] - bounds[1]
    qr_size = 26
    d_qr = Drawing(
        qr_size,
        qr_size,
        transform=[qr_size / qr_w, 0, 0, qr_size / qr_h, 0, 0],
    )
    d_qr.add(qr_obj)
    d_qr.drawOn(self, margin, 0.35 * cm)

    text_x = margin + qr_size + 6
    self.setFont("Helvetica-Bold", 7.5)
    self.setFillColor(dark_gray)
    self.drawString(
        text_x, 1.05 * cm, "Hospital Unimed Grande Florianópolis"
    )

    self.setFont("Helvetica", 7.5)
    self.setFillColor(light_gray)
    self.drawString(
        text_x, 0.65 * cm, "+55 48 9181-5582 Concierge Robótica HUGF"
    )

    page_str = (
        f"Orçamento {orc_num}  |  Página {self._pageNumber} de {page_count}"
    )
    self.drawRightString(page_width - margin, 0.8 * cm, page_str)
    self.restoreState()


# --- FUNÇÕES AUXILIARES ---
def create_green_tag(text="HÍBRIDO (PACOTE)"):
  w = len(text) * 6.5 + 16
  h = 16
  d = Drawing(w, h)
  d.add(
      Rect(
          0,
          0,
          w,
          h,
          rx=8,
          ry=8,
          fillColor=colors.HexColor("#008C50"),
          strokeColor=None,
      )
  )
  d.add(
      String(
          w / 2.0,
          4,
          text,
          textAnchor="middle",
          fontName="Helvetica-Bold",
          fontSize=8,
          fillColor=colors.white,
      )
  )
  return d


def create_qr_code(url="https://wa.me/554891815582", size=42):
  qr_code = qr.QrCodeWidget(url)
  bounds = qr_code.getBounds()
  w = bounds[2] - bounds[0]
  h = bounds[3] - bounds[1]
  d = Drawing(size, size, transform=[size / w, 0, 0, size / h, 0, 0])
  d.add(qr_code)
  return d


def fmt_m(val):
  return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# --- MOTOR DE GERAÇÃO DO PDF REPORTLAB ---
def gerar_pdf_bytes(dados):
  buffer = io.BytesIO()
  doc = SimpleDocTemplate(
      buffer,
      pagesize=A4,
      leftMargin=1.5 * cm,
      rightMargin=1.5 * cm,
      topMargin=2.6 * cm,
      bottomMargin=1.6 * cm,
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
      fontSize=12.5,
      leading=14.5,
      textColor=dark_gray,
      spaceAfter=2,
  )
  subtitle_style = ParagraphStyle(
      "DocSubtitle",
      fontName="Helvetica-Bold",
      fontSize=7.8,
      leading=9.5,
      textColor=unimed_green,
      spaceAfter=6,
  )
  subtitle_black_style = ParagraphStyle(
      "DocSubtitleBlack",
      fontName="Helvetica",
      fontSize=7.8,
      leading=9.5,
      textColor=dark_gray,
      spaceAfter=4,
  )
  section_heading = ParagraphStyle(
      "SectionHeading",
      fontName="Helvetica-Bold",
      fontSize=8.8,
      leading=10.8,
      textColor=unimed_green,
      spaceBefore=4,
      spaceAfter=2,
  )
  normal_text = ParagraphStyle(
      "NormalText",
      fontName="Helvetica",
      fontSize=7.2,
      leading=9.2,
      textColor=dark_gray,
  )
  normal_bold = ParagraphStyle(
      "NormalBold",
      fontName="Helvetica-Bold",
      fontSize=7.2,
      leading=9.2,
      textColor=dark_gray,
  )
  table_header = ParagraphStyle(
      "TableHeader",
      fontName="Helvetica-Bold",
      fontSize=7.2,
      leading=9.0,
      textColor=dark_gray,
  )
  table_cell = ParagraphStyle(
      "TableCell",
      fontName="Helvetica",
      fontSize=7.2,
      leading=9.0,
      textColor=dark_gray,
  )
  table_cell_bold = ParagraphStyle(
      "TableCellBold",
      fontName="Helvetica-Bold",
      fontSize=7.2,
      leading=9.0,
      textColor=dark_gray,
  )
  table_cell_green = ParagraphStyle(
      "TableCellGreen",
      fontName="Helvetica-Bold",
      fontSize=7.2,
      leading=9.0,
      textColor=unimed_green,
  )
  note_style = ParagraphStyle(
      "NoteText",
      fontName="Helvetica-Oblique",
      fontSize=6.8,
      leading=8.5,
      textColor=colors.HexColor("#4B5563"),
      spaceAfter=2,
  )
  concierge_style = ParagraphStyle(
      "ConciergeBox",
      fontName="Helvetica",
      fontSize=7.2,
      leading=9.8,
      textColor=dark_gray,
  )
  qr_label_style = ParagraphStyle(
      "QRLabel",
      fontName="Helvetica-Bold",
      fontSize=6.0,
      leading=7.2,
      textColor=unimed_green,
      alignment=1,
  )

  story = []

  # PÁGINA 1
  story.append(Paragraph("Orçamento cirúrgico", title_style))
  story.append(
      Paragraph(
          "ATENDIMENTO PARTICULAR · PACOTE · CIRURGIA ROBÓTICA", subtitle_style
      )
  )

  tag_hibrido = create_green_tag("HÍBRIDO (PACOTE)")

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
              Spacer(1, 2),
              tag_hibrido,
          ],
          Paragraph(
              "<b>CONVÊNIO / MODALIDADE</b><br/>Unimed/Híbrido", normal_text
          ),
      ],
  ]

  info_table = Table(info_data, colWidths=[9.0 * cm, 9.0 * cm])
  info_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, -1), light_bg),
          ("BOX", (0, 0), (-1, -1), 0.8, border_gray),
          ("INNERGRID", (0, 0), (-1, -1), 0.5, border_gray),
          ("PADDING", (0, 0), (-1, -1), 4.0),
          ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
      ])
  )
  story.append(info_table)
  story.append(Spacer(1, 6))

  story.append(Paragraph("PROCEDIMENTOS PREVISTOS", section_heading))
  proc_data = [
      [Paragraph("Código", table_header), Paragraph("Procedimento", table_header)],
      [
          Paragraph("89999901", table_cell),
          Paragraph("Taxa de procedimento robótico híbrido", table_cell),
      ],
  ]
  proc_table = Table(proc_data, colWidths=[3.2 * cm, 14.8 * cm])
  proc_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
          ("BOX", (0, 0), (-1, -1), 0.8, border_gray),
          ("INNERGRID", (0, 0), (-1, -1), 0.5, border_gray),
          ("PADDING", (0, 0), (-1, -1), 3.5),
      ])
  )
  story.append(proc_table)
  story.append(Spacer(1, 6))

  story.append(
      Paragraph("VALORES DE RESPONSABILIDADE DO PACIENTE", section_heading)
  )
  story.append(
      Paragraph(
          "Este orçamento contempla os valores e itens estipulados para o pacote"
          " particular selecionado.",
          note_style,
      )
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
              ParagraphStyle("THeaderBig", parent=table_header, fontSize=8.0),
          ),
          Paragraph(
              fmt_m(v_total),
              ParagraphStyle(
                  "TValBig",
                  parent=table_cell_bold,
                  fontSize=8.8,
                  textColor=unimed_green,
              ),
          ),
      ],
  ]
  val_table = Table(val_data, colWidths=[13.0 * cm, 5.0 * cm])
  val_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
          ("BOX", (0, 0), (-1, -1), 0.8, border_gray),
          ("INNERGRID", (0, 0), (-1, -1), 0.5, border_gray),
          ("PADDING", (0, 0), (-1, -1), 3.8),
          ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#ECFDF5")),
      ])
  )
  story.append(val_table)

  # PÁGINA 2
  story.append(PageBreak())
  story.append(Paragraph("CONDIÇÕES DO ORÇAMENTO", title_style))
  story.append(
      Paragraph(
          "Informações para pagamento e orientações ao paciente",
          subtitle_black_style,
      )
  )

  story.append(
      Paragraph("01 - ITENS NÃO INCLUSOS NESTE ORÇAMENTO", section_heading)
  )
  no_inc_text = (
      "• <b>Honorários Médicos da Equipe Cirúrgica (Modalidade Híbrida):</b> O"
      " plano de saúde cobrirá a parcela do honorário médico correspondente ao"
      " procedimento na modalidade convencional. A diferença relativa ao ato"
      " cirúrgico robótico não possui cobertura do plano e deverá ser negociada"
      " e paga pelo paciente diretamente à equipe cirúrgica. <i>(Nota: Os"
      " honorários anestésicos já estão devidamente inclusos neste"
      " orçamento).</i><br/>• Exames laboratoriais e de"
      " imagem/diagnóstico;<br/>• Sessões de fonoaudiologia e fisioterapia;<br/>•"
      " Sangue e hemoderivados."
  )
  no_inc_box = Table(
      [[Paragraph(no_inc_text, normal_text)]], colWidths=[18.0 * cm]
  )
  no_inc_box.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, -1), red_box_bg),
          ("BOX", (0, 0), (-1, -1), 0.8, red_box_border),
          ("PADDING", (0, 0), (-1, -1), 3.5),
      ])
  )
  story.append(no_inc_box)
  story.append(Spacer(1, 3))

  story.append(Paragraph("02 - CONDIÇÕES DE PAGAMENTO", section_heading))
  notice_text = (
      "<b>Atenção:</b> A confirmação do agendamento somente se dará mediante o"
      " pagamento antecipado em até 48h úteis à realização do procedimento."
  )
  notice_box = Table(
      [[Paragraph(notice_text, normal_text)]], colWidths=[18.0 * cm]
  )
  notice_box.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, -1), yellow_box_bg),
          ("BOX", (0, 0), (-1, -1), 0.8, yellow_box_border),
          ("PADDING", (0, 0), (-1, -1), 3.5),
      ])
  )
  story.append(notice_box)
  story.append(Spacer(1, 3))

  pag_data = [
      [
          Paragraph(
              "<b>PAGAMENTO À VISTA</b><br/>Dinheiro em espécie, cartão de"
              " débito ou PIX.<br/><br/><b>Chave PIX (CNPJ):</b>"
              " 77.858.611/0014-14<br/><b>Favorecido:</b> Unimed Grande"
              " Florianópolis Cooperativa de Trabalho Médico",
              normal_text,
          ),
          Paragraph(
              "<b>TRANSFERÊNCIA BANCÁRIA</b><br/><b>Favorecido:</b> Unimed"
              " Grande Florianópolis Cooperativa de Trabalho Médico<br/><b>CNPJ:</b>"
              " 77.858.611/0014-14<br/><b>Banco Unicred (136)</b> | <b>Agência:</b>"
              " 1214-9 | <b>Conta:</b> 102646-1",
              normal_text,
          ),
      ],
      [
          Paragraph(
              "<b>CARTÃO DE CRÉDITO</b><br/>&bull; Até R$ 10.000,00:"
              " Parcelamento em até 6x sem juros.<br/>&bull; Acima de R$"
              " 10.000,00: Parcelamento em até 10x sem juros.<br/><i>Solicite o"
              " link informando nº do orçamento, bandeira e"
              " parcelas.</i><br/><b>Contato:</b> +55 48 9181-5582 |"
              " concierge@unimedflorianopolis.com.br",
              normal_text,
          ),
          Paragraph(
              "<b>ENVIO DO COMPROVANTE / LINK DE PAGAMENTO</b><br/><b>WhatsApp:</b>"
              " +55 48 9181-5582<br/><b>E-mail:</b>"
              " concierge@unimedflorianopolis.com.br",
              normal_text,
          ),
      ],
  ]
  pag_table = Table(pag_data, colWidths=[9.0 * cm, 9.0 * cm])
  pag_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, -1), light_bg),
          ("BOX", (0, 0), (-1, -1), 0.8, border_gray),
          ("INNERGRID", (0, 0), (-1, -1), 0.5, border_gray),
          ("PADDING", (0, 0), (-1, -1), 3.5),
          ("VALIGN", (0, 0), (-1, -1), "TOP"),
      ])
  )
  story.append(pag_table)
  story.append(Spacer(1, 3))

  story.append(
      Paragraph(
          "03 - INFORMAÇÕES ADICIONAIS SOBRE O ORÇAMENTO", section_heading
      )
  )
  info_add = (
      "&bull; <b>Adicionais:</b> Demais materiais, medicamentos, gases"
      " medicinais e OPME's (órteses, próteses e materiais especiais) que não"
      " compõem o orçamento, serão acrescentados ao valor final quando"
      " utilizados.<br/>&bull; <b>Variações e Excedentes:</b> Os valores"
      " contidos nesta previsão de gastos não são absolutos. Caso haja"
      " utilização de tempo excedente de sala cirúrgica ou período de"
      " permanência hospitalar superior ao estipulado, ocorrerão alterações de"
      " valores.<br/>&bull; <b>Reoperações, Complicações e Intercorrências:</b>"
      " Em casos de reoperações, complicações, intercorrências ou diárias além"
      " das previstas, os valores extras serão cobrados posteriormente no"
      " momento da alta.<br/>&bull; <b>Devoluções de OPME:</b> Em casos de"
      " necessidade de devolução de valores referentes a OPME (órteses,"
      " próteses e materiais especiais) não utilizado durante a cirurgia, o"
      " processo será concluído em até 10 dias úteis após a alta do paciente."
  )
  story.append(Paragraph(info_add, normal_text))
  story.append(Spacer(1, 3))

  story.append(
      Paragraph("SERVIÇO EXCLUSIVO DE CONCIERGE ROBÓTICA", section_heading)
  )
  concierge_text = (
      "Para proporcionar uma experiência segura, ágil e acolhedora, os"
      " pacientes de cirurgia robótica contam com o nosso serviço exclusivo de"
      " Concierge. Estamos à disposição para auxiliar em todas as etapas do"
      " seu procedimento. Em caso de dúvidas, entre em contato diretamente"
      " pelo WhatsApp: <b>+55 48 9181-5582</b> ou pelo e-mail"
      " <b>concierge@unimedflorianopolis.com.br</b>"
  )

  qr_drawing = create_qr_code("https://wa.me/554891815582", size=42)
  qr_cell = [
      qr_drawing,
      Spacer(1, 1),
      Paragraph("Aponte a câmera<br/>para o WhatsApp", qr_label_style),
  ]

  concierge_table = Table(
      [[Paragraph(concierge_text, concierge_style), qr_cell]],
      colWidths=[14.5 * cm, 3.5 * cm],
  )
  concierge_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#ECFDF5")),
          ("BOX", (0, 0), (-1, -1), 0.8, unimed_green),
          ("PADDING", (0, 0), (-1, -1), 4),
          ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("ALIGN", (1, 0), (1, 0), "CENTER"),
      ])
  )
  story.append(concierge_table)

  story.append(Spacer(1, 115))

  dt_str = dados["data_solicitacao"]
  signature_data = [[
      "",
      Paragraph(
          f"<para align='right'>São José, {dt_str}<br/><br/><br/>________________________________________<br/><b>Responsável"
          " Paciente</b></para>",
          normal_text,
      ),
  ]]
  signature_table = Table(signature_data, colWidths=[9.0 * cm, 9.0 * cm])
  signature_table.setStyle(
      TableStyle([
          ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
          ("PADDING", (0, 0), (-1, -1), 0),
      ])
  )

  orc_n = dados["numero_orcamento"]
  val_d = dados["data_validade"]
  story.append(
      KeepTogether([
          signature_table,
          Spacer(1, 4),
          Table(
              [[
                  Paragraph(
                      f"<b>ORÇAMENTO Nº {orc_n}</b>",
                      ParagraphStyle(
                          "V1",
                          parent=normal_bold,
                          textColor=unimed_green,
                          fontSize=8.0,
                      ),
                  ),
                  Paragraph(
                      f"<b>VÁLIDO ATÉ {val_d}</b>",
                      ParagraphStyle(
                          "V2", parent=normal_bold, textColor=dark_gray, fontSize=8.0
                      ),
                  ),
              ]],
              colWidths=[9.0 * cm, 9.0 * cm],
              style=[
                  ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F4F6")),
                  ("BOX", (0, 0), (-1, -1), 0.8, border_gray),
                  ("ALIGN", (0, 0), (0, 0), "LEFT"),
                  ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                  ("PADDING", (0, 0), (-1, -1), 3.5),
              ],
          ),
      ])
  )

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
      "data_solicitacao": "Data da solicitação no formato DD/MM/AAAA",
      "tempo_cc": "Tempo de centro cirúrgico ex: '180 Min CC (3h 00min)' ou similar",
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
          st.write("**Tipo:** Híbrido (Pacote)")

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
            label="📥 Baixar Orçamento Oficial em PDF",
            data=pdf_bytes,
            file_name=f"Orcamento_{dados['numero_orcamento']}_{dados['paciente'].replace(' ', '_')}.pdf",
            mime="application/pdf",
        )

      except Exception as e:
        st.error(f"Erro ao processar orçamento: {str(e)}")

elif uploaded_file and not api_key:
  st.warning("⚠️ Por favor, insira a sua API Key do Gemini para continuar.")
