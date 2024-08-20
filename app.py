from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
from openai import OpenAI
from dotenv import load_dotenv
import os
import json
import ftplib

load_dotenv()  # Betölti a környezeti változókat a .env fájlból

class Config:
    """Configuration class for hardcoded values."""
    API_KEY = os.getenv("API_KEY")
    ASSISTANT_KEY = os.getenv("ASSISTANT_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL")
    INSTRUCTIONS = os.getenv("INSTRUCTIONS")
    FTP_SERVER = "ftp.abydosai.com"
    FTP_USER = "u938222440.openai"
    FTP_PASS = "Vilaguralo1472"

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

    # Felhasználói üzenet mentése
    conversations.setdefault(thread_id, [])
    conversations[thread_id].append({"role": "user", "content": user_input})

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

            # Asszisztens válasz mentése a beszélgetésbe
            conversations[thread_id].append({"role": "assistant", "content": assistant_response})

            # A beszélgetés frissítése/mentése JSON fájlba
            file_name = save_conversation_to_file(thread_id)
            
            # Fájl feltöltése FTP-re
            upload_to_ftp(file_name)

    return Response(generate(), content_type='text/plain')

def save_conversation_to_file(thread_id):
    """Mentés vagy frissítés JSON fájlba."""
    file_name = f"{thread_id}.json"
    try:
        # Ha a fájl már létezik, olvassuk be és frissítsük
        if os.path.exists(file_name):
            with open(file_name, "r") as f:
                existing_data = json.load(f)
                existing_data.extend(conversations[thread_id])
            with open(file_name, "w") as f:
                json.dump(existing_data, f, indent=4)
        else:
            # Ha a fájl nem létezik, hozzuk létre és írjuk bele az adatokat
            with open(file_name, "w") as f:
                json.dump(conversations[thread_id], f, indent=4)
    except Exception as e:
        print(f"Error saving conversation to file: {e}")
    
    return file_name

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
