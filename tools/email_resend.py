import os
import logging
from typing import Optional, List, Dict, Any
import resend
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("email_resend")

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY


def send_email_resend(
    to: str,
    subject: str,
    html: str,
    from_email: str,
    reply_to: Optional[str] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    tags: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY missing")

    payload: Dict[str, Any] = {
        "from": from_email,     # must be verified in Resend
        "to": [to],
        "subject": subject,
        "html": html,
    }

    if reply_to:
        payload["reply_to"] = reply_to
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    if tags:
        payload["tags"] = tags

    logger.info(
        "[EMAIL resend] sending to=%s subject=%s from=%s",
        to,
        subject,
        from_email,
    )
    result = resend.Emails.send(payload)
    logger.info(
        "[EMAIL resend] sent to=%s subject=%s result=%s",
        to,
        subject,
        result,
    )
    return result
