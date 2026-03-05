from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from tools.email_resend import send_email_resend

router = APIRouter()

class SendEmailReq(BaseModel):
    to: EmailStr
    subject: str
    html: str

@router.post("/tools/send-email")
async def tools_send_email(req: SendEmailReq):
    try:
        resp = send_email_resend(
            to=req.to,
            subject=req.subject,
            html=req.html,
            from_email="Code Studio <noreply@code-studio.eu>",  # verified sender
            reply_to="Rej Aliaj <info@code-studio.eu>",
        )
        return {"ok": True, "resend": resp}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))