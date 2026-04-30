from __future__ import annotations

import json
import logging
from typing import Any

from app_config import (
    LIVEKIT_API_KEY,
    LIVEKIT_API_SECRET,
    LIVEKIT_OUTGOING_AGENT_NAME,
    LIVEKIT_TELNYX_OUTBOUND_HOST,
    LIVEKIT_TELNYX_OUTBOUND_NUMBERS,
    LIVEKIT_TELNYX_OUTBOUND_PASSWORD,
    LIVEKIT_TELNYX_OUTBOUND_TRANSPORT,
    LIVEKIT_TELNYX_OUTBOUND_TRUNK_ID,
    LIVEKIT_TELNYX_OUTBOUND_TRUNK_NAME,
    LIVEKIT_TELNYX_OUTBOUND_USERNAME,
    LIVEKIT_URL,
)

logger = logging.getLogger("livekit-voice")


def livekit_management_configured() -> bool:
    return bool(LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET)


def telnyx_livekit_outbound_configured() -> bool:
    return livekit_management_configured() and bool(
        LIVEKIT_TELNYX_OUTBOUND_TRUNK_ID
        or (
            LIVEKIT_TELNYX_OUTBOUND_HOST
            and LIVEKIT_TELNYX_OUTBOUND_USERNAME
            and LIVEKIT_TELNYX_OUTBOUND_PASSWORD
        )
    )


def _require_livekit_management() -> None:
    if not livekit_management_configured():
        raise RuntimeError("LiveKit management API credentials are not configured on the backend server")


def _require_telnyx_livekit_outbound() -> None:
    _require_livekit_management()
    if not telnyx_livekit_outbound_configured():
        raise RuntimeError("LiveKit Telnyx outbound trunk settings are not configured on the backend server")


def _parse_outbound_numbers() -> list[str]:
    raw = str(LIVEKIT_TELNYX_OUTBOUND_NUMBERS or "*").strip()
    if not raw:
        return ["*"]
    numbers = [item.strip() for item in raw.split(",") if item.strip()]
    return numbers or ["*"]


def _transport_enum_value(sip_module: Any) -> int:
    candidate = str(LIVEKIT_TELNYX_OUTBOUND_TRANSPORT or "udp").strip().lower()
    return {
        "auto": sip_module.SIPTransport.SIP_TRANSPORT_AUTO,
        "udp": sip_module.SIPTransport.SIP_TRANSPORT_UDP,
        "tcp": sip_module.SIPTransport.SIP_TRANSPORT_TCP,
        "tls": sip_module.SIPTransport.SIP_TRANSPORT_TLS,
    }.get(candidate, sip_module.SIPTransport.SIP_TRANSPORT_UDP)


async def _api_client():
    from livekit import api as livekit_api

    _require_livekit_management()
    return livekit_api.LiveKitAPI(
        url=LIVEKIT_URL,
        api_key=LIVEKIT_API_KEY,
        api_secret=LIVEKIT_API_SECRET,
    )


async def ensure_telnyx_outbound_trunk() -> dict[str, str]:
    from livekit.protocol import sip

    _require_telnyx_livekit_outbound()
    if LIVEKIT_TELNYX_OUTBOUND_TRUNK_ID:
        return {
            "sip_trunk_id": LIVEKIT_TELNYX_OUTBOUND_TRUNK_ID,
            "name": LIVEKIT_TELNYX_OUTBOUND_TRUNK_NAME or "telnyx-outgoing-livekit",
        }

    api = await _api_client()
    try:
        response = await api.sip.list_sip_outbound_trunk(sip.ListSIPOutboundTrunkRequest())
        for item in response.items:
            if str(item.name or "").strip() == (LIVEKIT_TELNYX_OUTBOUND_TRUNK_NAME or "telnyx-outgoing-livekit").strip():
                return {
                    "sip_trunk_id": str(item.sip_trunk_id or ""),
                    "name": str(item.name or ""),
                }

        created = await api.sip.create_sip_outbound_trunk(
            sip.CreateSIPOutboundTrunkRequest(
                trunk=sip.SIPOutboundTrunkInfo(
                    name=(LIVEKIT_TELNYX_OUTBOUND_TRUNK_NAME or "telnyx-outgoing-livekit").strip() or "telnyx-outgoing-livekit",
                    metadata=json.dumps({"provider": "telnyx", "managed_by": "ai-voice-backend"}),
                    address=LIVEKIT_TELNYX_OUTBOUND_HOST.strip(),
                    numbers=_parse_outbound_numbers(),
                    auth_username=LIVEKIT_TELNYX_OUTBOUND_USERNAME.strip(),
                    auth_password=LIVEKIT_TELNYX_OUTBOUND_PASSWORD.strip(),
                    transport=_transport_enum_value(sip),
                )
            )
        )
        return {
            "sip_trunk_id": str(created.sip_trunk_id or ""),
            "name": str(created.name or ""),
        }
    finally:
        await api.aclose()


async def create_agent_dispatch(*, room_name: str, metadata: dict[str, Any], agent_name: str | None = None) -> dict[str, str]:
    from livekit.protocol import agent_dispatch
    from livekit.protocol import room as room_proto

    api = await _api_client()
    try:
        target_agent_name = str(agent_name or LIVEKIT_OUTGOING_AGENT_NAME or "outgoing-agent").strip() or "outgoing-agent"
        try:
            await api.room.create_room(
                room_proto.CreateRoomRequest(
                    name=room_name,
                    empty_timeout=300,
                    departure_timeout=30,
                )
            )
        except Exception as exc:
            logger.info("[LIVEKIT_VOICE] create_room skipped room=%s error=%s", room_name, exc)

        existing = await api.agent_dispatch.list_dispatch(room_name=room_name)
        for item in existing:
            if str(item.agent_name or "").strip() == target_agent_name:
                return {"dispatch_id": str(item.id or ""), "room_name": str(item.room or room_name)}

        created = await api.agent_dispatch.create_dispatch(
            agent_dispatch.CreateAgentDispatchRequest(
                agent_name=target_agent_name,
                room=room_name,
                metadata=json.dumps(metadata),
            )
        )
        return {"dispatch_id": str(created.id or ""), "room_name": str(created.room or room_name)}
    finally:
        await api.aclose()


async def cleanup_outgoing_room(*, room_name: str, participant_identity: str = "", dispatch_id: str = "") -> None:
    from livekit.api import RoomParticipantIdentity
    from livekit.protocol import room as room_proto

    api = await _api_client()
    try:
        if participant_identity:
            try:
                await api.room.remove_participant(RoomParticipantIdentity(room=room_name, identity=participant_identity))
            except Exception as exc:
                logger.warning("[LIVEKIT_VOICE] remove participant failed room=%s identity=%s error=%s", room_name, participant_identity, exc)
        if dispatch_id:
            try:
                await api.agent_dispatch.delete_dispatch(dispatch_id, room_name)
            except Exception as exc:
                logger.warning("[LIVEKIT_VOICE] delete dispatch failed room=%s dispatch=%s error=%s", room_name, dispatch_id, exc)
        try:
            await api.room.delete_room(room_proto.DeleteRoomRequest(room=room_name))
        except Exception as exc:
            logger.warning("[LIVEKIT_VOICE] delete room failed room=%s error=%s", room_name, exc)
    finally:
        await api.aclose()
