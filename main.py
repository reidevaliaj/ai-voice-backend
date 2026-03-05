import os
import json
import asyncio
import time
from typing import Optional
import base64
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from routes.tools_email import router as email_router
from routes.events import router as events_router


import websockets
load_dotenv()
app = FastAPI()

app.include_router(email_router)
app.include_router(events_router)

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    print(">>> incoming-call HIT", flush=True)

    vr = VoiceResponse()
    dial = vr.dial(
        answer_on_bridge=True,
        timeout=20
     )

    dial.sip(
        "sip:inbound@4xqezis1h25.sip.livekit.cloud;transport=udp",
        username="livekit_trunk",
        password="Admin@web123",
        status_callback="https://voice.code-studio.eu/sip-status",
        status_callback_method="POST",
        
    )

    return Response(content=str(vr), media_type="application/xml")


@app.post("/sip-status")
async def sip_status(request: Request):
    form = await request.form()
    print(">>> SIP STATUS:", dict(form), flush=True)
    return Response("OK")


