from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()  # Betölti a környezeti változókat a .env fájlból

class Config:
    """Configuration class for hardcoded values."""
    API_KEY = os.getenv("API_KEY")
    ASSISTANT_KEY = os.getenv("ASSISTANT_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL")
    INSTRUCTIONS = os.getenv("INSTRUCTIONS")

def initialize_openai_client():
    """Initializes and returns the OpenAI client along with the assistant object."""
    client = OpenAI(api_key=Config.API_KEY)
    assistant = client.beta.assistants.retrieve(Config.ASSISTANT_KEY)
    return client, assistant

app = Flask(__name__, static_folder='static')
CORS(app)

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/start_chat', methods=['POST'])
def start_chat():
    client, assistant = initialize_openai_client()
    thread = client.beta.threads.create()
    thread_id = thread.id
    return jsonify({"thread_id": thread_id})

@app.route('/send_message', methods=['POST'])
def send_message():
    data = request.json
    thread_id = data.get("thread_id")
    user_input = data.get("message")

    if not thread_id or not user_input:
        return jsonify({"error": "Missing thread_id or message"}), 400

    client, assistant = initialize_openai_client()
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
            for delta in stream.text_deltas:
                yield delta

    return Response(generate(), content_type='text/plain')

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
