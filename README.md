
### 🔁 Real-Time Bi-Directional Audio Relay with OpenAI & Twilio

This project enables real-time voice translation and relaying between two users (caller and receiver) using Twilio and OpenAI's GPT-4o Real Time Streaming API.

---

## 📦 Project Setup

### 1. Clone the repository

```bash
git clone https://github.com/MuhammadAhmadSaaim/Twilio-Live-Call-Translation.git
cd your-repo-name/backend
````

### 2. Set up a virtual environment

```bash
python -m venv venv
source venv/Scripts/activate  # On Windows
# OR
source venv/bin/activate      # On macOS/Linux
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the server

You can run the server using either:

#### Option A: Simple Python

```bash
python server.py
```

#### Option B: Uvicorn (with auto-reload and custom port)

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

---

## 🔐 Environment Variables

Create a `.env` file in the `backend` directory and add the following keys:

```env
# Twilio Credentials
TWILIO_ACCOUNT_SID=your_account_sid
TWILIO_API_KEY_SID=your_api_key_sid
TWILIO_API_KEY_SECRET=your_api_key_secret
TWILIO_AUTH_TOKEN=your_auth_token
TWIML_APP_SID=your_twiml_app_sid

# CORS and Deployment
ALLOWED_ORIGIN=https://your-frontend-domain.com
YOUR_PUBLIC_DOMAIN=https://your-public-backend-domain.com

# OpenAI API Key
OPENAI_API_KEY=your_openai_api_key
```

You can load these automatically using [python-dotenv](https://pypi.org/project/python-dotenv/) or by exporting them manually depending on your deployment method.

---

## 🌐 Tunneling with ngrok (for development)

If you are running this locally and need to expose the backend to Twilio:

1. [Install ngrok](https://ngrok.com/download)
2. Run ngrok:

```bash
ngrok http 8000
```

3. Copy the HTTPS forwarding URL (e.g., `https://abcd1234.ngrok.io`)
4. Update your `.env` file:

```env
YOUR_PUBLIC_DOMAIN=abcd1234.ngrok.io
# (Remove https://)
```

5. Restart your server for changes to take effect.

---

## 📞 Audio Flow

This project sets up two WebSocket endpoints:

* `/ws/caller`: Receives audio from the caller, sends it to OpenAI, and forwards the response to the receiver
* `/ws/receiver`: Receives audio from the receiver, sends it to OpenAI, and forwards the response to the caller

Speech from either side will interrupt the assistant response and trigger intelligent truncation and clearing of the output.

---

## 🚀 Using the Project in a Browser

1. **Start your FastAPI server** and expose it via **ngrok**.
2. **Open the ngrok-exposed frontend URL** in **two separate browser tabs** (or on two different devices).
3. **In each browser tab**, enter a **unique client ID** (e.g., `alice`, `bob`) and click to **connect** and receive a token.
4. **One user** enters the other user's **client ID** to initiate the call.
5. The relay begins — audio will stream in real-time between both participants with OpenAI-powered voice transformation and interruption handling.

---

## 🤖 Technologies Used

* Python 3.9+
* FastAPI
* Websockets
* Uvicorn
* Twilio Media Streams
* OpenAI GPT-4o (Realtime API)
* ngrok (for local HTTPS tunneling)

---

## 📄 License

MIT License. See [LICENSE](./LICENSE) for more info.

```
