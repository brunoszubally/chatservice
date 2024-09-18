from quart import Quart, request, jsonify, Response, send_from_directory
from quart_cors import cors
import asyncio
import openai
from dotenv import load_dotenv
import os
import json
from datetime import datetime
import asyncssh
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.units import inch
from reportlab.lib import colors
import re
import aiosmtplib
from email.message import EmailMessage
import logging
import aiofiles
import uuid

load_dotenv()  # Betölti a környezeti változókat a .env fájlból

# Betűtípus regisztráció PDF generálásához
pdfmetrics.registerFont(TTFont('DejaVuSans', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
pdfmetrics.registerFont(TTFont('DejaVuSans-Bold', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'))

# Naplózás beállítása
logging.basicConfig(level=logging.INFO)

class Config:
    """Configuration class for hardcoded values."""
    API_KEY = os.getenv("API_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
    INSTRUCTIONS = os.getenv("INSTRUCTIONS", "You are a helpful assistant.")
    
    # SFTP beállítások
    SFTP_SERVER = os.getenv("SFTP_SERVER")
    SFTP_USER = os.getenv("SFTP_USER")
    SFTP_PASS = os.getenv("SFTP_PASS")
    
    # SMTP beállítások e-mail küldéshez
    SMTP_SERVER = os.getenv("SMTP_SERVER")
    SMTP_PORT = int(os.getenv("SMTP_PORT"))
    SMTP_USER = os.getenv("SMTP_USER")
    SMTP_PASS = os.getenv("SMTP_PASS")
    RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

    # Engedélyezett domainek beállítása
    ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

# Tároló a beszélgetések és időzítők számára
conversations = {}
email_tasks = {}

async def send_email_with_pdf(pdf_file):
    """E-mail küldése a PDF fájllal mellékletként."""
    smtp_server = Config.SMTP_SERVER
    smtp_port = Config.SMTP_PORT
    smtp_user = Config.SMTP_USER
    smtp_password = Config.SMTP_PASS
    recipient_email = Config.RECIPIENT_EMAIL

    from_email = smtp_user
    to_email = recipient_email

    message = EmailMessage()
    message['From'] = from_email
    message['To'] = to_email
    message['Subject'] = "Beszélgetés PDF melléklete"

    body = "Kérem, találja mellékelve a generált PDF fájlt a beszélgetésről."
    message.set_content(body)

    # PDF melléklet csatolása
    try:
        async with aiofiles.open(pdf_file, "rb") as f:
            pdf_data = await f.read()
            message.add_attachment(pdf_data, maintype='application', subtype='pdf', filename=pdf_file)

        # Kapcsolódás az SMTP szerverhez és e-mail küldése
        await aiosmtplib.send(
            message,
            hostname=smtp_server,
            port=smtp_port,
            username=smtp_user,
            password=smtp_password,
            start_tls=True
        )
        logging.info(f"E-mail sikeresen elküldve {to_email} címre.")
    except Exception as e:
        logging.error(f"E-mail küldési hiba: {e}")

async def start_email_timer(thread_id, pdf_file_name):
    """Elindít egy időzítőt, amely 10 perc múlva elküldi a PDF-et."""
    global email_tasks

    # Ha már van egy időzítő ehhez a thread_id-hoz, töröljük
    if thread_id in email_tasks and email_tasks[thread_id] is not None:
        email_tasks[thread_id].cancel()

    async def delayed_email():
        await asyncio.sleep(600)  # 10 perc várakozás
        await send_email_with_pdf(pdf_file_name)
        logging.info(f"E-mail sent for thread {thread_id}.")

    # Új időzítő indítása
    task = asyncio.create_task(delayed_email())
    email_tasks[thread_id] = task
    logging.info(f"E-mail időzítő beállítva a PDF küldésére 10 perc múlva a {thread_id}-hoz.")

app = Quart(__name__, static_folder='static')
app = cors(app, allow_origin=Config.ALLOWED_ORIGINS)

@app.route('/')
async def index():
    return await send_from_directory(app.static_folder, 'index.html')

@app.route('/start_chat', methods=['POST'])
async def start_chat():
    # Egyedi thread_id generálása
    thread_id = str(uuid.uuid4())

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

    # Felhasználói üzenet mentése dátummal és időbélyeggel
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conversations.setdefault(thread_id, [])
    conversations[thread_id].append({"role": "user", "content": user_input, "timestamp": timestamp})

    # OpenAI API hívás előkészítése
    messages = [{"role": msg["role"], "content": msg["content"]} for msg in conversations[thread_id]]

    # OpenAI API hívása aszinkron módon
    async def generate():
        try:
            response = await openai.ChatCompletion.acreate(
                model=Config.OPENAI_MODEL,
                messages=messages,
                temperature=0.7,
                stream=True  # Streamelés engedélyezése
            )

            assistant_response = ""
            async for chunk in response:
                delta = chunk.choices[0].delta.get('content', '')
                assistant_response += delta
                yield delta

            # Asszisztens válasz mentése a beszélgetésbe dátummal és időbélyeggel
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conversations[thread_id].append({"role": "assistant", "content": assistant_response, "timestamp": timestamp})

            # A beszélgetés frissítése/mentése JSON fájlba
            file_name = await save_conversation_to_file(thread_id)

            # Fájl feltöltése SFTP-re
            await upload_to_sftp(file_name)

            # E-mail időzítő elindítása
            await start_email_timer(thread_id, f"{thread_id}.pdf")

        except Exception as e:
            logging.error(f"Hiba az OpenAI API hívása során: {e}")
            yield "[Hiba a válasz generálása során]"

    return Response(generate(), content_type='text/plain')

async def save_conversation_to_file(thread_id):
    """Mentés vagy frissítés JSON fájlba, PDF generálása mellé és e-mail küldés."""
    json_file_name = f"{thread_id}.json"
    try:
        # Beszélgetés adatainak mentése JSON fájlba aszinkron módon
        async with aiofiles.open(json_file_name, "w", encoding="utf-8") as f:
            await f.write(json.dumps(conversations[thread_id], ensure_ascii=False, indent=4))

        # PDF generálása a teljes beszélgetésről
        pdf_file_name = create_pdf(thread_id, conversations[thread_id])
        logging.info(f"PDF generated: {pdf_file_name}")

        # JSON és PDF feltöltése SFTP-re
        await upload_to_sftp(json_file_name)
        await upload_to_sftp(pdf_file_name)

    except Exception as e:
        logging.error(f"Error saving conversation to file: {e}")

    return json_file_name

# Stílusok definiálása a PDF generáláshoz
styles = getSampleStyleSheet()
user_style = ParagraphStyle(
    'UserStyle',
    parent=styles['Normal'],
    fontName='DejaVuSans-Bold',
    fontSize=12,
    leading=18,
    textColor=colors.black,
    backColor=colors.lightgrey,
    alignment=2  # Right align for user messages
)
assistant_style = ParagraphStyle(
    'AssistantStyle',
    parent=styles['Normal'],
    fontName='DejaVuSans',
    fontSize=12,
    leading=18,
    textColor=colors.black,
    backColor=colors.whitesmoke,
    alignment=0  # Left align for assistant messages
)

def create_pdf(thread_id, conversation_data):
    """Generates a PDF file of the entire conversation."""
    file_name = f"{thread_id}.pdf"
    pdf_path = os.path.join(os.getcwd(), file_name)

    story = []
    doc = SimpleDocTemplate(pdf_path, pagesize=letter, rightMargin=50, leftMargin=50, topMargin=50, bottomMargin=50)

    # Végigmegyünk az összes beszélgetésen
    for message in conversation_data:
        role = "Felhasználó" if message["role"] == "user" else "Asszisztens"
        content = sanitize_text(message["content"])
        timestamp = message["timestamp"]

        role_paragraph = Paragraph(f"<b>{role} ({timestamp}):</b>", user_style if role == "Felhasználó" else assistant_style)
        content_paragraph = Paragraph(content, user_style if role == "Felhasználó" else assistant_style)

        story.append(role_paragraph)
        story.append(content_paragraph)
        story.append(Spacer(1, 0.2 * inch))

    # PDF létrehozása
    doc.build(story)

    return file_name

def sanitize_text(content):
    """Removes unwanted patterns like sources from the text."""
    return re.sub(r"【\d+:\d+†[\w\.]+】", '', content)

async def upload_to_sftp(file_name):
    """Fájl feltöltése az SFTP szerverre."""
    try:
        async with asyncssh.connect(Config.SFTP_SERVER, username=Config.SFTP_USER, password=Config.SFTP_PASS) as conn:
            async with conn.start_sftp_client() as sftp:
                await sftp.put(file_name, file_name)
        logging.info(f"Sikeres feltöltés az SFTP szerverre: {file_name}")
    except Exception as e:
        logging.error(f"SFTP feltöltési hiba: {e}")

if __name__ == "__main__":
    openai.api_key = Config.API_KEY
    app.run(host='0.0.0.0', port=5000)
