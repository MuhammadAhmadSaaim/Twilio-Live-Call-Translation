# server.py
import asyncio
import os
import logging
from fastapi import FastAPI, HTTPException, Request, Query, Form, WebSocket
from fastapi.responses import PlainTextResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.twiml.voice_response import VoiceResponse, Dial
from dotenv import load_dotenv
from twilio.rest import Client
import uvicorn
import traceback
import json
import base64
from vosk import Model, KaldiRecognizer
from pydub import AudioSegment
import io

#################################################################################################################

model_path = os.path.join(os.getcwd(), "vosk_model", "vosk-model-small-en-us-0.15")  # Change path if needed
asr_model = Model(model_path)
#################################################################################################################

# --- Configuration ---
load_dotenv() # Load environment variables from .env file

# Configure basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Environment Variable Validation ---
# !! Make sure these are set in your .env file !!
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN") # Needed for some API calls
TWILIO_API_KEY_SID = os.getenv("TWILIO_API_KEY_SID")
TWILIO_API_KEY_SECRET = os.getenv("TWILIO_API_KEY_SECRET")
TWIML_APP_SID = os.getenv("TWIML_APP_SID")
TWILIO_CALLER_ID = os.getenv("TWILIO_CALLER_ID", None) # Optional

# Simple validation
if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, TWIML_APP_SID]):
    logger.error("CRITICAL: Missing one or more required Twilio environment variables. Please check your .env file.")
    # In a real application, you might want to exit here
    # exit(1)

# --- FastAPI App Initialization ---
app = FastAPI(title="Twilio Voice Backend")

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- CORS Configuration ---
# !! Adjust origins if needed for production !!
origins = [
    "*", # Allows all origins - BE CAREFUL IN PRODUCTION
    # "http://localhost:3000", # Example for local React dev server
    # "https://your-frontend-domain.com", # Example for deployed frontend
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"], # Allows all methods (GET, POST, etc.)
    allow_headers=["*"], # Allows all headers
)

# --- Endpoints ---
##################################################################################################################

@app.get("/token")
async def get_token(identity: str = Query(..., min_length=1, description="The identity of the client user")):
    """
    Generates a Twilio Access Token with Voice Grant for the given identity.
    """
    if not identity:
        logger.warning("Token request rejected: Missing identity.")
        raise HTTPException(status_code=400, detail="Identity parameter is required.")

    try:
        # Create access token with credentials
        access_token = AccessToken(TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, identity=identity)

        # Create Voice grant
        voice_grant = VoiceGrant(
            outgoing_application_sid=TWIML_APP_SID,
            incoming_allow=True # Set to True if you want this client to receive incoming calls
        )
        access_token.add_grant(voice_grant)

        # Generate the token (JWT)
        jwt_token = access_token.to_jwt()
        logger.info(f"Successfully generated token for identity: {identity} : {jwt_token}")
        return {"token": jwt_token}

    except Exception as e:
        logger.error(f"Error generating token for identity '{identity}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not generate token: {str(e)}")

##################################################################################################################

import os
import json
import base64
from fastapi import WebSocket, WebSocketDisconnect
from vosk import Model, KaldiRecognizer
import numpy as np
import wave
import collections

model_path = os.path.join(os.getcwd(), "vosk_model", "vosk-model-small-en-us-0.15")
asr_model = Model(model_path)

SAMPLE_RATE = 8000
CHUNK_DURATION_MS = 20  # each Twilio chunk is 20ms
BUFFER_TARGET_MS = 300  # how much to buffer before processing
CHUNKS_PER_BATCH = BUFFER_TARGET_MS // CHUNK_DURATION_MS

@app.websocket("/media")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    print("connection open")

    recognizer = KaldiRecognizer(asr_model, SAMPLE_RATE)
    audio_buffer = bytearray()
    chunk_count = 0
    chunk_queue = collections.deque()

    try:
        while True:
            message = await websocket.receive_text()
            msg = json.loads(message)

            if msg.get("event") == "media":
                chunk_count += 1
                audio_data = base64.b64decode(msg["media"]["payload"])

                if len(audio_data.strip(b"\x00")) == 0:
                    print(f"[{chunk_count}] Silence detected. Triggering ASR with buffered audio.")
                    if audio_buffer:
                        result = recognizer.AcceptWaveform(audio_buffer)
                        if result:
                            text = json.loads(recognizer.Result()).get("text", "")
                            if text:
                                print(f"🔊 [Silence-Triggered Transcript]: {text}")
                        audio_buffer.clear()

                else:
                    audio_buffer.extend(audio_data)
                    chunk_queue.append(audio_data)

                    if len(chunk_queue) >= CHUNKS_PER_BATCH:
                        print(f"[{chunk_count}] Processing {len(chunk_queue)} chunks (~{BUFFER_TARGET_MS}ms audio)")
                        result = recognizer.AcceptWaveform(audio_buffer)
                        if result:
                            text = json.loads(recognizer.Result()).get("text", "")
                            if text:
                                print(f"🧠 [Buffered Transcript]: {text}")
                        audio_buffer.clear()
                        chunk_queue.clear()


            elif msg.get("event") == "stop":
                print("Call ended by Twilio.")
                break

    except WebSocketDisconnect:
        print("WebSocket disconnected.")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        print("connection closed")
        await websocket.close()



##################################################################################################################

@app.post("/voice")
async def voice(request: Request,
                To: str = Form(None),
                From: str = Form(None)):
    # --- START DEBUG LOGGING ---
    print(">>> /voice endpoint HIT!")

    try:
        # Log the full request headers to debug any issues related to the request
        headers = request.headers
        print(f">>> Request Headers: {headers}")

        # Log the form data (incoming parameters)
        form_data = await request.form()
        print(f">>> Incoming Form Data: {form_data}")

        # Log 'To' and 'From' parameters
        print(f">>> 'To' Parameter received: {To}")
        print(f">>> 'From' Parameter received: {From}")  # This is the caller

        if not To:
            print(">>> ERROR: 'To' parameter is missing!")
            response = VoiceResponse()
            response.say("Sorry, I didn't get a destination to call.")
            response.hangup()
            print(f">>> Generating TwiML (Error Case - Missing To): {str(response)}")
            return Response(content=str(response), media_type="application/xml")

        if not From:
            print(">>> ERROR: 'From' parameter is missing!")
            response = VoiceResponse()
            response.say("Sorry, I didn't get your caller information.")
            response.hangup()
            print(f">>> Generating TwiML (Error Case - Missing From): {str(response)}")
            return Response(content=str(response), media_type="application/xml")

        # --- Generate TwiML ---
        response = VoiceResponse()

        # Add Media Stream before dialing
        start = response.start()
        start.stream(url="wss://c8b0-139-135-36-24.ngrok-free.app/media")  # Update with your real WebSocket URL

        dial = Dial()

        # Check if 'To' looks like a phone number or a client identity
        if To.startswith("+") and len(To) > 8:  # Basic check for E.164 number
            print(f">>> Dialing NUMBER: {To}")
            dial.number(To)
        else:
            print(f">>> Dialing CLIENT: {To}")
            dial.client(To)

        response.append(dial)

        twiML_string = str(response)
        print(f">>> Generating TwiML (Success Case): {twiML_string}")  # Log the final TwiML

        return Response(content=twiML_string, media_type="application/xml")

    except Exception as e:
        print(f">>> !!! EXCEPTION in /voice endpoint: {e}")
        # Log the full traceback for detailed debugging
        traceback.print_exc()

        # Return a valid TwiML response even on error, explaining the issue
        error_response = VoiceResponse()
        error_response.say("I encountered an internal error. Please try again later.")
        error_response.hangup()
        print(f">>> Generating TwiML (Exception Case): {str(error_response)}")

        # Return 200 OK with error TwiML to avoid Twilio retries with non-XML response
        return Response(content=str(error_response), media_type="application/xml")
    
##################################################################################################################

@app.get("/", include_in_schema=False)
async def root():
    return FileResponse("static/index.html")

#return 204 for favicon.ico
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

##################################################################################################################

# --- Uvicorn Runner (for local development) ---
if __name__ == "__main__":
    logger.info("Starting Uvicorn server...")
    uvicorn.run(
        "main:app",
        host="0.0.0.0", # Listen on all available network interfaces
        port=8000,     # Standard port, change if needed
        reload=True    # Enable auto-reload for development
    )