from quart import Quart, request, jsonify, Response, send_from_directory
from quart_cors import cors
import asyncio
from openai import AsyncOpenAI
from dotenv import load_dotenv
import os
import json
from datetime import datetime
import ftplib
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.units import inch
from reportlab.lib import colors
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
import threading

load_dotenv()  # Betölti a környezeti változókat a .env fájlból

# Betűtípus regisztráció PDF generálásához
pdfmetrics.registerFont(TTFont('DejaVuSans', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
pdfmetrics.registerFont(TTFont('DejaVuSans-Bold', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'))

class Config:
    """Configuration class for hardcoded values."""
    API_KEY = os.getenv("API_KEY")
    ASSISTANT_KEY = os.getenv("ASSISTANT_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL")
    INSTRUCTIONS = os.getenv("INSTRUCTIONS")
    FTP_SERVER = os.getenv("FTP_SERVER")
    FTP_USER = os.getenv("FTP_USER")
    FTP_PASS = os.getenv("FTP_PASS")
    
    # SMTP beállítások e-mail küldéshez
    SMTP_SERVER = os.getenv("SMTP_SERVER")
    SMTP_PORT = int(os.getenv("SMTP_PORT"))
    SMTP_USER = os.getenv("SMTP_USER")
    SMTP_PASS = os.getenv("SMTP_PASS")
    RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

# Tároló a beszélgetések és időzítők számára
conversations = {}
email_timers = {}

def send_email_with_pdf(pdf_file):
    """E-mail küldése a PDF fájllal mellékletként."""
    smtp_server = Config.SMTP_SERVER
    smtp_port = Config.SMTP_PORT
    smtp_user = Config.SMTP_USER
    smtp_password = Config.SMTP_PASS
    recipient_email = Config.RECIPIENT_EMAIL

    from_email = smtp_user
    to_email = recipient_email

    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = "Beszélgetés PDF melléklete"

    body = "Kérem, találja mellékelve a generált PDF fájlt a beszélgetésről."
    msg.attach(MIMEText(body, 'plain'))

    # PDF melléklet csatolása
    try:
        with open(pdf_file, "rb") as f:
            attach = MIMEApplication(f.read(), _subtype="pdf")
            attach.add_header('Content-Disposition', 'attachment', filename=pdf_file)
            msg.attach(attach)

        # Kapcsolódás az SMTP szerverhez és e-mail küldése
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        text = msg.as_string()
        server.sendmail(from_email, to_email, text)
        server.quit()
        print(f"E-mail sikeresen elküldve {to_email} címre.")
    except Exception as e:
        print(f"E-mail küldési hiba: {e}")

def start_email_timer(thread_id, pdf_file_name):
    """Elindít egy időzítőt, amely 10 perc múlva elküldi a PDF-et."""
    global email_timers
    
    # Ha már van egy időzítő ehhez a thread_id-hoz, töröljük (új üzenet érkezett, újra kell indítani)
    if thread_id in email_timers and email_timers[thread_id] is not None:
        email_timers[thread_id].cancel()

    # Új időzítő indítása (10 perc = 600 másodperc)
    timer = threading.Timer(600, send_email_with_pdf, [pdf_file_name])
    email_timers[thread_id] = timer
    timer.start()
    print(f"E-mail időzítő beállítva a PDF küldésére 10 perc múlva a {thread_id}-hoz.")

async def initialize_openai_client():
    """Aszinkron kliens és asszisztens inicializálás OpenAI-hoz."""
    client = AsyncOpenAI(api_key=Config.API_KEY)
    assistant = await client.beta.assistants.retrieve(Config.ASSISTANT_KEY)
    return client, assistant

app = Quart(__name__, static_folder='static')
cors(app)

@app.route('/')
async def index():
    return await send_from_directory(app.static_folder, 'index.html')

@app.route('/start_chat', methods=['POST'])
async def start_chat():
    client, assistant = await initialize_openai_client()
    thread = await client.beta.threads.create()
    thread_id = thread.id

    # Új beszélgetés indítása és mentése
    conversations[thread_id] = []
    return jsonify({"thread_id": thread_id})

@app.route('/send_message', methods=['POST'])
async def send_message():
    data = await request.json
    thread_id = data.get("thread_id")
    user_input = data.get("message")

    if not thread_id or not user_input:
        return jsonify({"error": "Missing thread_id or message"}), 400

    client, assistant = await initialize_openai_client()

    # Felhasználói üzenet mentése dátummal és időbélyeggel
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conversations.setdefault(thread_id, [])
    conversations[thread_id].append({"role": "user", "content": user_input, "timestamp": timestamp})

    await client.beta.threads.messages.create(
        thread_id=thread_id, role="user", content=user_input
    )

    async def generate():
        async with client.beta.threads.runs.create_and_stream(
            thread_id=thread_id,
            assistant_id=assistant.id,
            model=Config.OPENAI_MODEL,
            instructions=Config.INSTRUCTIONS,
        ) as stream:
            assistant_response = ""
            async for delta in stream.text_deltas:
                assistant_response += delta
                yield delta

            # Asszisztens válasz mentése a beszélgetésbe dátummal és időbélyeggel
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conversations[thread_id].append({"role": "assistant", "content": assistant_response, "timestamp": timestamp})

            # A beszélgetés frissítése/mentése JSON fájlba
            file_name = await save_conversation_to_file(thread_id)
            
            # Fájl feltöltése FTP-re
            await upload_to_ftp(file_name)

    return Response(generate(), content_type='text/plain')

async def save_conversation_to_file(thread_id):
    """Mentés vagy frissítés JSON fájlba, PDF generálása mellé és e-mail küldés."""
    json_file_name = f"{thread_id}.json"
    try:
        # Ha a fájl létezik, frissítsük a meglévő adatokat
        existing_data = []
        if os.path.exists(json_file_name):
            with open(json_file_name, "r", encoding="utf-8") as f:
                existing_data = json.load(f)

        # Új üzenetek hozzáadása
        new_messages = [msg for msg in conversations[thread_id] if msg not in existing_data]
        if new_messages:
            existing_data.extend(new_messages)
            with open(json_file_name, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, ensure_ascii=False, indent=4)

        # PDF generálása a teljes beszélgetésről
        pdf_file_name = create_pdf(thread_id, conversations[thread_id])
        print(f"PDF generated: {pdf_file_name}")
        
        # JSON és PDF feltöltése FTP-re
        await upload_to_ftp(json_file_name)  # JSON fájl feltöltése
        await upload_to_ftp(pdf_file_name)   # PDF fájl feltöltése
        
        # E-mail időzítő elindítása (10 perc múlva küldjük el az e-mailt)
        start_email_timer(thread_id, pdf_file_name)

    except Exception as e:
        print(f"Error saving conversation to file: {e}")
    
    return json_file_name

# Define styles for PDF generation
styles = getSampleStyleSheet()
user_style = ParagraphStyle(
    'UserStyle',
    parent=styles['Normal'],
    fontName='DejaVuSans-Bold',
    fontSize=12,
    leading=18,  # Increased line height for better readability
    textColor=colors.black,
    backColor=colors.lightgrey,
    alignment=1  # Right align for user messages
)
assistant_style = ParagraphStyle(
    'AssistantStyle',
    parent=styles['Normal'],
    fontName='DejaVuSans',
    fontSize=12,
    leading=18,  # Increased line height for better readability
    textColor=colors.black,
    backColor=colors.whitesmoke,
    alignment=0  # Left align for assistant messages
)

def create_pdf(thread_id, conversation_data):
    """Generates a PDF file of the entire conversation."""
    file_name = f"{thread_id}.pdf"
    pdf_path = os.path.join(os.getcwd(), file_name)  # PDF fájl mentése a futási könyvtárba
    
    story = []
    doc = SimpleDocTemplate(pdf_path, pagesize=letter, rightMargin=50, leftMargin=50, topMargin=50, bottomMargin=50)

    # Végigmegyünk az összes beszélgetésen
    for message in conversation_data:
        role = "Felhasználó" if message["role"] == "user" else "Asszisztens"
        content = sanitize_text(message["content"])
        timestamp = message["timestamp"]

        role_paragraph = Paragraph(f"<b>{role} ({timestamp}):</b>", user_style if role == "Felhasználó" else assistant_style)
        content_paragraph = Paragraph(content, user_style if role == "Felhasználó" else assistant_style)

        # Mindig hozzáadjuk a beszélgetést a PDF-hez
        story.append(role_paragraph)
        story.append(content_paragraph)
        story.append(Spacer(1, 0.2 * inch))

    # PDF létrehozása
    doc.build(story)
    
    return file_name  # Csak a fájlnév visszaadása

def sanitize_text(content):
    """Removes unwanted patterns like sources from the text."""
    return re.sub(r"【\d+:\d+†[\w\.]+】", '', content)

async def upload_to_ftp(file_name):
    """Fájl feltöltése az FTP szerverre."""
    try:
        with ftplib.FTP(Config.FTP_SERVER) as ftp:
            ftp.login(user=Config.FTP_USER, passwd=Config.FTP_PASS)
            with open(file_name, "rb") as file:
                ftp.storbinary(f"STOR {file_name}", file)
        print(f"Successfully uploaded {file_name} to FTP server.")
    except ftplib.all_errors as e:
        print(f"FTP upload error: {e}")

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
