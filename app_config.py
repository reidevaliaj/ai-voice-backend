import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


DATABASE_URL = _env("DATABASE_URL", "sqlite:///./data/ai_voice_assistant.db")
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "https://voice.code-studio.eu")
FASTAPI_INTERNAL_API_KEY = _env("INTERNAL_API_KEY")
SESSION_SECRET_KEY = _env("SESSION_SECRET_KEY", "change-me-session-secret")
PLATFORM_ENCRYPTION_KEY = _env("PLATFORM_ENCRYPTION_KEY")
DEFAULT_TENANT_SLUG = _env("DEFAULT_TENANT_SLUG", "codestudio")
DEFAULT_INBOUND_PHONE_NUMBER = _env("DEFAULT_INBOUND_PHONE_NUMBER")
ADMIN_BOOTSTRAP_EMAIL = _env("ADMIN_BOOTSTRAP_EMAIL", "admin@code-studio.eu")
ADMIN_BOOTSTRAP_PASSWORD = _env("ADMIN_BOOTSTRAP_PASSWORD", "Admin12345!")

LIVEKIT_SIP_URI = _env(
    "LIVEKIT_SIP_URI",
    "sip:inbound@4xqezis1h25.sip.livekit.cloud;transport=udp",
)
LIVEKIT_SIP_USERNAME = _env("LIVEKIT_SIP_USERNAME", "livekit_trunk")
LIVEKIT_SIP_PASSWORD = _env("LIVEKIT_SIP_PASSWORD", "Admin@web123")

OPENAI_API_KEY = _env("OPENAI_API_KEY")
LIVEKIT_URL = _env("LIVEKIT_URL")
LIVEKIT_API_KEY = _env("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = _env("LIVEKIT_API_SECRET")
RESEND_API_KEY = _env("RESEND_API_KEY")

DEFAULT_OWNER_EMAIL = _env("OWNER_EMAIL", "info@cod-st.com")
DEFAULT_FROM_EMAIL = _env("FROM_EMAIL", "Code Studio <noreply@code-studio.eu>")
DEFAULT_REPLY_TO_EMAIL = _env("REPLY_TO_EMAIL", "Rej Aliaj <info@code-studio.eu>")
DEFAULT_MEETING_OWNER_EMAIL = _env("MEETING_OWNER_EMAIL", "aliajrei@gmail.com")
DEFAULT_BUSINESS_TIMEZONE = _env("BUSINESS_TIMEZONE", "Europe/Budapest")
DEFAULT_BUSINESS_HOURS = _env("BUSINESS_HOURS", "09:00-17:00")
DEFAULT_BUSINESS_DAYS = _env("BUSINESS_DAYS", "1,2,3,4,5")
DEFAULT_MEETING_DURATION_MINUTES = int(_env("DEFAULT_MEETING_DURATION_MINUTES", "30") or "30")
DEFAULT_BOOKING_HORIZON_DAYS = int(_env("DEFAULT_BOOKING_HORIZON_DAYS", "14") or "14")
DEFAULT_LLM_MODEL = _env("LLM_MODEL", "gpt-4.1-mini")
DEFAULT_TTS_VOICE = _env("DEFAULT_TTS_VOICE", "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc")

ENABLE_LEGACY_CALL_END_EMAIL = _env("ENABLE_LEGACY_CALL_END_EMAIL", "false").lower() == "true"
GOOGLE_CLIENT_ID = _env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = _env("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = _env("GOOGLE_REFRESH_TOKEN")
GOOGLE_CALENDAR_ID = _env("GOOGLE_CALENDAR_ID", "primary")
ZOOM_CLIENT_ID = _env("CLIENT_ID_ZOOM")
ZOOM_CLIENT_SECRET = _env("CLIENT_SECRET_ZOOM")
ZOOM_TOKEN_URL = _env("ZOOM_TOKEN_URL", "https://zoom.us/oauth/token")
ZOOM_OWNER_EMAIL = _env("ZOOM_OWNER_EMAIL", DEFAULT_MEETING_OWNER_EMAIL)
