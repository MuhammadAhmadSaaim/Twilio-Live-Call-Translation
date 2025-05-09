import asyncio
import base64
import os
import logging
import json # Added for WebSocket message parsing
from fastapi import FastAPI, HTTPException, Request, Query, Form, WebSocket, WebSocketDisconnect # Added WebSocket, WebSocketDisconnect
from fastapi.responses import Response, FileResponse, JSONResponse # Added JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.twiml.voice_response import VoiceResponse, Dial, Start, Stream # Added Start, Stream
from dotenv import load_dotenv
import uvicorn
import traceback
import websockets
# Load environment variables
load_dotenv()

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

active_call_streams = {}
 

# Twilio credentials from .env
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_API_KEY_SID = os.getenv("TWILIO_API_KEY_SID")
TWILIO_API_KEY_SECRET = os.getenv("TWILIO_API_KEY_SECRET")
TWIML_APP_SID = os.getenv("TWIML_APP_SID")
# IMPORTANT: Add your server's public base URL (e.g., from ngrok or your deployed server)
# For WebSocket URLs. Ensure it starts with wss:// if using secure WebSockets.
# Example: BASE_URL="wss://your-ngrok-id.ngrok.io" or "ws://localhost:8000" for local non-HTTPS testing (Twilio usually requires WSS for production)
BASE_URL = os.getenv("YOUR_PUBLIC_DOMAIN", "ws://localhost:8000") # Default to ws for local, Twilio needs wss for public

PUBLIC_HOSTNAME = os.getenv("YOUR_PUBLIC_DOMAIN")

if not BASE_URL.startswith("ws"):
    logger.warning("BASE_URL does not start with ws:// or wss://. WebSockets might not connect correctly with Twilio.")


# Validate Twilio config
if not all([TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, TWIML_APP_SID]):
    logger.error("Missing required Twilio environment variables.")
    # exit(1) # Optional: uncomment to fail fast

# In-memory store for stream SIDs, keyed by CallSid
# In a production app, use Redis or another persistent/shared store
# Example structure:
# {
#   "CAxxxxxxxxxxxx": {
#       "initiator_spoken_stream_sid": "SSyyyyyyyyyyyy_initiator", # e.g., Patient's audio stream
#       "recipient_spoken_stream_sid": "SSzzzzzzzzzzzz_recipient"  # e.g., Doctor's audio stream
#   }
# }
active_call_streams = {}

# FastAPI app init
app = FastAPI(title="Twilio Voice Backend with Media Streams")

# Mount static files (e.g., index.html)
app.mount("/static", StaticFiles(directory="static"), name="static")

# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allows all origins
    allow_credentials=True,
    allow_methods=["*"], # Allows all methods
    allow_headers=["*"], # Allows all headers
)

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

# --- Twilio Access Token ---
@app.get("/token")
async def get_token(identity: str = Query(..., min_length=1)):
    try:
        token = AccessToken(
            TWILIO_ACCOUNT_SID,
            TWILIO_API_KEY_SID,
            TWILIO_API_KEY_SECRET,
            identity=identity
        )
        voice_grant = VoiceGrant(outgoing_application_sid=TWIML_APP_SID, incoming_allow=True)
        token.add_grant(voice_grant)
        logger.info(f"Generated token for identity: {identity}")
        return {"token": token.to_jwt()}
    except Exception as e:
        logger.error(f"Token error: {e}")
        raise HTTPException(status_code=500, detail="Token generation failed.")

# --- OpenAi Config ---

LOG_EVENT_TYPES = ['response.text.delta', 'response.audio.delta']
SHOW_TIMING_MATH = True
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
SYSTEM_MESSAGE = (
    "You are a live translation assistant. "
    "Whenever you receive input speech, translate its meaning into simple, conversational Urdu, "
    "and **speak** the translation in Urdu—do not output any English or written text. "
    "All your responses must be spoken in clear Urdu audio."
)

VOICE = 'alloy'

async def initialize_session(openai_ws):
    """Control initial session with OpenAI."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))


# --- Twilio Voice Call Webhook ---

@app.post("/voice")
async def voice(request: Request):
    try:
        form_data = await request.form()
        to_target = form_data.get("To")
        from_caller = form_data.get("From")
        call_sid = form_data.get("CallSid")

        logger.info(f"Voice webhook called. From: {from_caller}, To: {to_target}, CallSid: {call_sid}")
        response = VoiceResponse()

        if not call_sid:
            logger.error("CallSid missing in /voice request.")
            response.say("An error occurred: Call identifier is missing.")
            response.hangup()
            return Response(content=str(response), media_type="application/xml")

        if not PUBLIC_HOSTNAME:
            logger.error(f"[{call_sid}] Server configuration error: PUBLIC_HOSTNAME is not set. Cannot start media streams.")
            response.say("A server configuration error occurred. Cannot stream audio.")
            response.hangup()
            return Response(content=str(response), media_type="application/xml")

        # Ensure PUBLIC_HOSTNAME does not contain scheme or trailing slashes for safety
        clean_hostname = PUBLIC_HOSTNAME
        if "://" in clean_hostname:
            clean_hostname = clean_hostname.split("://")[-1]
        clean_hostname = clean_hostname.rstrip('/')

        if call_sid not in active_call_streams:
            active_call_streams[call_sid] = {}

        initiator_stream_url = f"wss://{clean_hostname}/ws/caller"
        recipient_stream_url = f"wss://{clean_hostname}/ws/receiver"

        logger.info(f"[{call_sid}] Initiator Stream URL for TwiML: {initiator_stream_url}")
        logger.info(f"[{call_sid}] Recipient Stream URL for TwiML: {recipient_stream_url}")

        start_initiator = Start()
        start_initiator.stream(url=initiator_stream_url, track="inbound_track")
        response.append(start_initiator)

        start_recipient = Start()
        start_recipient.stream(url=recipient_stream_url, track="outbound_track")
        response.append(start_recipient)



        dial = Dial()
        # ... (your existing dial logic is fine) ...
        if to_target.startswith("client:"):
            dial.client(to_target[len("client:"):])
            logger.info(f"[{call_sid}] TwiML: Dialing client: {to_target[len('client:'):]}")
        elif "@" in to_target and "." in to_target:
            dial.sip(to_target)
            logger.info(f"[{call_sid}] TwiML: Dialing SIP: {to_target}")
        elif to_target.isalnum() and not to_target.startswith("+"):
            dial.client(to_target)
            logger.info(f"[{call_sid}] TwiML: Dialing client identity: {to_target}")
        else:
            dial.number(to_target)
            logger.info(f"[{call_sid}] TwiML: Dialing number: {to_target}")
        response.append(dial)

        logger.info(f"[{call_sid}] Generated TwiML for /voice: {str(response)}")
        return Response(content=str(response), media_type="application/xml")

    except Exception as e:
        logger.error(f"Error in /voice endpoint: {e}")
        traceback.print_exc()
        response = VoiceResponse()
        response.say("An error occurred while processing your call.")
        response.hangup()
        return Response(content=str(response), media_type="application/xml")


# !!# --- WebSocket for audio streaming --- !!
"""
In this modified version, each WebSocket endpoint (`/ws/caller` and `/ws/receiver`) maintains its own separate connection to OpenAI’s real-time API,
rather than centralizing the OpenAI connection per call session. To achieve bi-directional communication—where the caller’s speech is processed by 
OpenAI and sent to the receiver, and vice versa—the logic is adjusted so that the AI-generated audio responses are not returned to the same WebSocket 
that provided the input audio. Instead, each endpoint forwards OpenAI’s output to the opposite party's WebSocket. This is done using a shared dictionary 
(`active_call_streams`) keyed by the `callSid`, which stores references to both the caller’s and receiver’s WebSocket connections 
(`caller_ws` and `receiver_ws`) and their respective stream IDs. When OpenAI returns audio data, the endpoint retrieves the opposing party’s WebSocket 
and stream ID from the shared dictionary and sends the audio there. This design maintains independent OpenAI sessions for each direction of audio while
enabling true cross-stream interaction between the two parties, effectively enabling real-time AI-mediated dialogue without requiring a unified session object.
"""

# --- WebSocket for audio streaming ---
@app.websocket("/ws/caller")
async def websocket_stream_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connected for /ws/caller")

    async with websockets.connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        await initialize_session(openai_ws)

        callerSID = None
        callSID = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp = None

        async def receive_from_twilio():
            nonlocal callerSID, callSID, latest_media_timestamp
            try:
                async for message_str in websocket.iter_text():
                    message_json = json.loads(message_str)
                    event = message_json.get("event")

                    if event == "start":
                        callerSID = message_json.get("streamSid")
                        callSID = message_json.get("start", {}).get("callSid")
                        active_call_streams.setdefault(callSID, {})["callerSID"] = callerSID
                        active_call_streams[callSID]["caller_ws"] = websocket
                        logger.info(f"Caller stream started: {callerSID}")
                        latest_media_timestamp = 0
                        last_assistant_item = None
                        response_start_timestamp = None
                        mark_queue.clear()

                    elif event == "media":
                        payload = message_json.get("media", {}).get("payload")
                        timestamp = int(message_json.get("media", {}).get("timestamp", 0))
                        if payload:
                            latest_media_timestamp = timestamp
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": payload
                            }))

                    elif event == "mark":
                        if mark_queue:
                            mark_queue.pop(0)

                    elif event == "stop":
                        logger.info("Caller stream received 'stop'. Closing.")
                        break

            except WebSocketDisconnect:
                logger.warning("Caller WebSocket disconnected.")
                await openai_ws.close()

        async def send_to_twilio():
            nonlocal last_assistant_item, response_start_timestamp
            try:
                async for message_str in openai_ws:
                    msg = json.loads(message_str)
                    msg_type = msg.get("type")

                    # ——— Log any text deltas as they come in ———
                    if msg_type == "response.text.delta" and "delta" in msg:
                        logger.info(f"Translated text (delta): {msg['delta']}")

                    # ——— Optionally log the final text when it completes ———
                    elif msg_type == "response.text.completed" and "completion" in msg:
                        logger.info(f"Translated text (complete): {msg['completion']}")

                    elif msg_type == "response.content_part.done":
                        part = msg.get("part", {})
                        if part.get("transcript"):
                            logger.info(f"Content part done (transcript): {part['transcript']}")

                    if msg_type == "response.audio.delta" and "delta" in msg:
                        audio_data = msg["delta"]
                        encoded = base64.b64encode(base64.b64decode(audio_data)).decode("utf-8")

                        receiver_ws = active_call_streams.get(callSID, {}).get("receiver_ws")
                        receiverSID = active_call_streams.get(callSID, {}).get("receiverSID")

                        if receiver_ws and receiverSID:
                            await receiver_ws.send_text(json.dumps({
                                "event": "media",
                                "streamSid": receiverSID,
                                "media": {"payload": encoded}
                            }))

                        if response_start_timestamp is None:
                            response_start_timestamp = latest_media_timestamp
                            if SHOW_TIMING_MATH:
                                logger.info(f"Caller -> Receiver response starts at {response_start_timestamp} ms")

                        if msg.get("item_id"):
                            last_assistant_item = msg["item_id"]

                        await send_mark(receiver_ws, receiverSID)

                    elif msg_type == "input_audio_buffer.speech_started":
                        logger.info("Caller started speaking again — interrupting AI.")
                        await handle_interrupt()

            except Exception as e:
                logger.error(f"Error in Caller OpenAI stream: {e}")

        async def handle_interrupt():
            nonlocal last_assistant_item, response_start_timestamp
            if last_assistant_item and response_start_timestamp is not None:
                elapsed = latest_media_timestamp - response_start_timestamp
                if SHOW_TIMING_MATH:
                    logger.info(f"Caller interrupt — truncating response at {elapsed} ms")

                await openai_ws.send(json.dumps({
                    "type": "conversation.item.truncate",
                    "item_id": last_assistant_item,
                    "content_index": 0,
                    "audio_end_ms": elapsed
                }))

                receiver_ws = active_call_streams.get(callSID, {}).get("receiver_ws")
                receiverSID = active_call_streams.get(callSID, {}).get("receiverSID")

                if receiver_ws and receiverSID:
                    await receiver_ws.send_text(json.dumps({
                        "event": "clear",
                        "streamSid": receiverSID
                    }))

                last_assistant_item = None
                response_start_timestamp = None
                mark_queue.clear()

        async def send_mark(ws, sid):
            if ws and sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": sid,
                    "mark": {"name": "responsePart"}
                }
                await ws.send_text(json.dumps(mark_event))
                mark_queue.append("responsePart")

        await asyncio.gather(receive_from_twilio(), send_to_twilio())

# --- WebSocket for audio streaming ---
@app.websocket("/ws/receiver")
async def websocket_stream_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connected for /ws/receiver")

    async with websockets.connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        await initialize_session(openai_ws)

        receiverSID = None
        callSID = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp = None

        async def receive_from_twilio():
            nonlocal receiverSID, callSID, latest_media_timestamp
            try:
                async for message_str in websocket.iter_text():
                    message_json = json.loads(message_str)
                    event = message_json.get("event")

                    if event == "start":
                        receiverSID = message_json.get("streamSid")
                        callSID = message_json.get("start", {}).get("callSid")
                        active_call_streams.setdefault(callSID, {})["receiverSID"] = receiverSID
                        active_call_streams[callSID]["receiver_ws"] = websocket
                        logger.info(f"Receiver stream started: {receiverSID}")
                        latest_media_timestamp = 0
                        last_assistant_item = None
                        response_start_timestamp = None
                        mark_queue.clear()

                    elif event == "media":
                        payload = message_json.get("media", {}).get("payload")
                        timestamp = int(message_json.get("media", {}).get("timestamp", 0))
                        if payload:
                            latest_media_timestamp = timestamp
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": payload
                            }))

                    elif event == "mark":
                        if mark_queue:
                            mark_queue.pop(0)

                    elif event == "stop":
                        logger.info("Receiver stream received 'stop'. Closing.")
                        break

            except WebSocketDisconnect:
                logger.warning("Receiver WebSocket disconnected.")
                await openai_ws.close()

        async def send_to_twilio():
            nonlocal last_assistant_item, response_start_timestamp
            try:
                async for message_str in openai_ws:
                    msg = json.loads(message_str)
                    msg_type = msg.get("type")

                    # ——— Log any text deltas as they come in ———
                    if msg_type == "response.text.delta" and "delta" in msg:
                        logger.info(f"Translated text (delta): {msg['delta']}")

                    # ——— Optionally log the final text when it completes ———
                    elif msg_type == "response.text.completed" and "completion" in msg:
                        logger.info(f"Translated text (complete): {msg['completion']}")

                    elif msg_type == "response.content_part.done":
                        part = msg.get("part", {})
                        if part.get("transcript"):
                            logger.info(f"Content part done (transcript): {part['transcript']}")

                    if msg_type == "response.audio.delta" and "delta" in msg:
                        audio_data = msg["delta"]
                        encoded = base64.b64encode(base64.b64decode(audio_data)).decode("utf-8")

                        caller_ws = active_call_streams.get(callSID, {}).get("caller_ws")
                        callerSID = active_call_streams.get(callSID, {}).get("callerSID")

                        if caller_ws and callerSID:
                            await caller_ws.send_text(json.dumps({
                                "event": "media",
                                "streamSid": callerSID,
                                "media": {"payload": encoded}
                            }))

                        if response_start_timestamp is None:
                            response_start_timestamp = latest_media_timestamp
                            if SHOW_TIMING_MATH:
                                logger.info(f"Receiver -> Caller response starts at {response_start_timestamp} ms")

                        if msg.get("item_id"):
                            last_assistant_item = msg["item_id"]

                        await send_mark(caller_ws, callerSID)

                    elif msg_type == "input_audio_buffer.speech_started":
                        logger.info("Receiver started speaking again — interrupting AI.")
                        await handle_interrupt()

            except Exception as e:
                logger.error(f"Error in Receiver OpenAI stream: {e}")

        async def handle_interrupt():
            nonlocal last_assistant_item, response_start_timestamp
            if last_assistant_item and response_start_timestamp is not None:
                elapsed = latest_media_timestamp - response_start_timestamp
                if SHOW_TIMING_MATH:
                    logger.info(f"Receiver interrupt — truncating response at {elapsed} ms")

                await openai_ws.send(json.dumps({
                    "type": "conversation.item.truncate",
                    "item_id": last_assistant_item,
                    "content_index": 0,
                    "audio_end_ms": elapsed
                }))

                caller_ws = active_call_streams.get(callSID, {}).get("caller_ws")
                callerSID = active_call_streams.get(callSID, {}).get("callerSID")

                if caller_ws and callerSID:
                    await caller_ws.send_text(json.dumps({
                        "event": "clear",
                        "streamSid": callerSID
                    }))

                last_assistant_item = None
                response_start_timestamp = None
                mark_queue.clear()

        async def send_mark(ws, sid):
            if ws and sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": sid,
                    "mark": {"name": "responsePart"}
                }
                await ws.send_text(json.dumps(mark_event))
                mark_queue.append("responsePart")

        await asyncio.gather(receive_from_twilio(), send_to_twilio())

# --- Helper endpoint to get stored Stream SIDs (for testing) ---
@app.get("/get_stream_sids")
async def get_stream_sids():
        return active_call_streams["m"]

# Run with Uvicorn if needed
if __name__ == "__main__":
    # Ensure BASE_URL is correctly set, especially if not using localhost for WebSockets
    logger.info(f"Starting server. WebSocket BASE_URL: {BASE_URL}")
    if "localhost" in BASE_URL and not BASE_URL.startswith("ws://localhost"):
         logger.warning("BASE_URL is localhost but not ws://localhost. Twilio might require wss:// for non-local connections.")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

    