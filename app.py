from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
from openai import OpenAI
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
    FTP_SERVER = "ftp.abydosai.com"
    FTP_USER = "u938222440.openai"
    FTP_PASS = "Vilaguralom1472!"

def initialize_openai_client():
    """Initializes and returns the OpenAI client along with the assistant object."""
    client = OpenAI(api_key=Config.API_KEY)
    assistant = client.beta.assistants.retrieve(Config.ASSISTANT_KEY)
    return client, assistant

app = Flask(__name__, static_folder='static')
CORS(app)

# Tároló a beszélgetések számára
conversations = {}

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/start_chat', methods=['POST'])
def start_chat():
    client, assistant = initialize_openai_client()
    thread = client.beta.threads.create()
    thread_id = thread.id

    # Új beszélgetés indítása és mentése
    conversations[thread_id] = []
    return jsonify({"thread_id": thread_id})

@app.route('/send_message', methods=['POST'])
def send_message():
    data = request.json
    thread_id = data.get("thread_id")
    user_input = data.get("message")

    if not thread_id or not user_input:
        return jsonify({"error": "Missing thread_id or message"}), 400

    client, assistant = initialize_openai_client()

    # Felhasználói üzenet mentése dátummal és időbélyeggel
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conversations.setdefault(thread_id, [])
    conversations[thread_id].append({"role": "user", "content": user_input, "timestamp": timestamp})

    client.beta.threads.messages.create(
        thread_id=thread_id, role="user", content=user_input
    )

    def generate():
        with client.beta.threads.runs.create_and_stream(
            thread_id=thread_id,
            assistant_id=assistant.id,
            model=Config.OPENAI_MODEL,
            instructions=Config.INSTRUCTIONS,
        ) as stream:
            assistant_response = ""
            for delta in stream.text_deltas:
                assistant_response += delta
                yield delta

            # Asszisztens válasz mentése a beszélgetésbe dátummal és időbélyeggel
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conversations[thread_id].append({"role": "assistant", "content": assistant_response, "timestamp": timestamp})

            # A beszélgetés frissítése/mentése JSON fájlba
            file_name = save_conversation_to_file(thread_id)
            
            # Fájl feltöltése FTP-re
            upload_to_ftp(file_name)

    return Response(generate(), content_type='text/plain')

def save_conversation_to_file(thread_id):
    """Mentés vagy frissítés JSON fájlba, PDF generálása mellé."""
    file_name = f"{thread_id}.json"
    try:
        # Ha a fájl létezik, frissítsük a meglévő adatokat
        existing_data = []
        if os.path.exists(file_name):
            with open(file_name, "r", encoding="utf-8") as f:
                existing_data = json.load(f)

        # Új üzenetek hozzáadása
        new_messages = [msg for msg in conversations[thread_id] if msg not in existing_data]
        if new_messages:
            existing_data.extend(new_messages)
            with open(file_name, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, ensure_ascii=False, indent=4)

        # PDF generálása a beszélgetésből
        pdf_file = create_pdf(thread_id, conversations[thread_id])
        print(f"PDF generated: {pdf_file}")
        
    except Exception as e:
        print(f"Error saving conversation to file: {e}")
    
    return file_name

def create_pdf(thread_id, conversation_data):
    """Generates a PDF file of the conversation."""
    file_name = f"{thread_id}.pdf"
    pdf_path = os.path.join("/mnt/data", file_name)
    
    story = []
    doc = SimpleDocTemplate(pdf_path, pagesize=letter, rightMargin=50, leftMargin=50, topMargin=50, bottomMargin=50)

    for i in range(0, len(conversation_data), 2):
        if i < len(conversation_data) - 1:
            user_message = conversation_data[i]
            assistant_message = conversation_data[i + 1]

            # Add user message (question)
            user_role_paragraph = Paragraph(f"<b>Felhasználó ({user_message['timestamp']}):</b>", user_style)
            user_content_paragraph = Paragraph(sanitize_text(user_message["content"]), user_style)

            # Add assistant message (answer)
            assistant_role_paragraph = Paragraph(f"<b>Asszisztens ({assistant_message['timestamp']}):</b>", assistant_style)
            elements = process_content_unordered(sanitize_text(assistant_message["content"]))

            # Keep the question and answer together
            story.append(KeepTogether([user_role_paragraph, user_content_paragraph, assistant_role_paragraph] + elements))
            story.append(Spacer(1, 0.2 * inch))
    
    doc.build(story)
    
    return file_name

def sanitize_text(content):
    """Removes unwanted patterns like sources from the text."""
    return re.sub(r"【\d+:\d+†[\w\.]+】", '', content)

def process_content_unordered(content):
    """Handles unordered lists in the content."""
    lines = content.splitlines()
    elements = []
    list_items = []

    for line in lines:
        if re.match(r"^\d+\.\s", line):  # Detect numbered list but treat it as unordered
            list_items.append(line)
        elif list_items:
            elements.append(create_unordered_list_flowable(list_items))
            list_items = []
            elements.append(Paragraph(line, assistant_style))
        else:
            elements.append(Paragraph(line, assistant_style))
    
    if list_items:
        elements.append(create_unordered_list_flowable(list_items))
    
    return elements

def create_unordered_list_flowable(items):
    """Creates an unordered list in the PDF."""
    return ListFlowable(
        [ListItem(Paragraph(item, assistant_style), bulletType='bullet') for item in items],
        bulletType='bullet'
    )

def upload_to_ftp(file_name):
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
